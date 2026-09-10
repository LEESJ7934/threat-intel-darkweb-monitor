"""Shared, explicit crawler runtime. Importing this module opens no resources."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from crawling.models import LeakRecord
from crawling.storage import save_records

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
TRUE_VALUES = {"true", "1", "yes", "on"}
FALSE_VALUES = {"false", "0", "no", "off"}


class CrawlerConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CrawlerConfig:
    db_uri: str = field(repr=False)
    db_name: str
    tor_socks_proxy: str = "socks5://127.0.0.1:9150"
    headless: bool = True
    page_timeout_seconds: int = 60
    wait_seconds: int = 20


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError as exc:
        raise CrawlerConfigError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise CrawlerConfigError(f"{name} must be a positive integer")
    return value


def load_config() -> CrawlerConfig:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    uri = os.getenv("DB_URI", "").strip()
    db_name = os.getenv("DB_NAME", "").strip()
    if (not uri.startswith(("mongodb://", "mongodb+srv://"))
            or not uri.partition("://")[2].split("/", 1)[0]):
        raise CrawlerConfigError("DB_URI must be a MongoDB URI")
    if "username:password@" in uri.lower() or "replace_locally" in uri.lower():
        raise CrawlerConfigError("DB_URI still contains example settings")
    if not db_name or any(char in db_name for char in '/\\ ."$\x00'):
        raise CrawlerConfigError("DB_NAME must be a valid, non-empty database name")
    proxy = os.getenv("TOR_SOCKS_PROXY", "socks5://127.0.0.1:9150").strip()
    try:
        parsed = urlsplit(proxy)
        valid_proxy = (parsed.scheme == "socks5" and parsed.hostname and parsed.port
                       and not parsed.username and not parsed.password
                       and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError as exc:
        raise CrawlerConfigError("TOR_SOCKS_PROXY must be socks5://host:port") from exc
    if not valid_proxy:
        raise CrawlerConfigError("TOR_SOCKS_PROXY must be socks5://host:port")
    headless = os.getenv("CRAWLER_HEADLESS", "True").strip().lower()
    if headless not in TRUE_VALUES | FALSE_VALUES:
        raise CrawlerConfigError("CRAWLER_HEADLESS must be True or False")
    return CrawlerConfig(
        db_uri=uri, db_name=db_name, tor_socks_proxy=proxy,
        headless=headless in TRUE_VALUES,
        page_timeout_seconds=_positive_int("CRAWLER_PAGE_TIMEOUT_SECONDS", 60),
        wait_seconds=_positive_int("CRAWLER_WAIT_SECONDS", 20),
    )


def utc_timestamp(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scraped_time must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def chrome_options(config: CrawlerConfig) -> Options:
    options = Options()
    options.add_argument(f"--proxy-server={config.tor_socks_proxy}")
    if config.headless:
        options.add_argument("--headless=new")
    return options


def driver_service() -> Service:
    # webdriver-manager reads dotenv during its own import; defer it to runtime.
    from webdriver_manager.chrome import ChromeDriverManager
    return Service(ChromeDriverManager().install())


@contextmanager
def browser(config: CrawlerConfig):
    driver = None
    try:
        options = chrome_options(config)
        service = driver_service()
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(config.page_timeout_seconds)
        yield driver
    finally:
        if driver is not None:
            driver.quit()


@contextmanager
def mongo_collection(config: CrawlerConfig):
    client = None
    try:
        client = MongoClient(config.db_uri, serverSelectionTimeoutMS=10_000,
                             connectTimeoutMS=10_000, tz_aware=True, tzinfo=timezone.utc)
        database = client[config.db_name]
        yield database["leaked_data"], database["leak_history"]
    finally:
        if client is not None:
            client.close()


def rendered_html(driver, source_url: str, selector: str, config: CrawlerConfig) -> str:
    driver.get(source_url)
    WebDriverWait(driver, config.wait_seconds).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector))
    )
    return driver.page_source


def parse_records(html: str, *, selector: str, source: str, source_url: str,
                  parse_item, scraped_time=None, limit=None) -> list[dict]:
    """Offline card parsing: one invalid item cannot reuse the previous record."""
    if not isinstance(html, str):
        raise TypeError("html must be a string")
    stamp = utc_timestamp(scraped_time)
    soup = BeautifulSoup(html, "html.parser")
    records = []
    for index, item in enumerate(soup.select(selector), start=1):
        if limit is not None and len(records) >= limit:
            break
        try:
            fields = parse_item(item)
            record = LeakRecord(scraped_time=stamp, source=source, source_url=source_url, **fields)
            records.append(record.to_document())
        except Exception as exc:
            # Never log page text, company metadata or an exception's raw message.
            LOGGER.warning("%s item %d skipped (%s)", source, index, type(exc).__name__)
    return records


def crawl_page(*, source_url: str, selector: str, parser, config=None) -> int:
    config = config if config is not None else load_config()
    with browser(config) as driver:
        html = rendered_html(driver, source_url, selector, config)
        records = parser(html)
    # Loading/parsing failures never open MongoDB; the driver is already closed.
    if not records:
        return 0
    with mongo_collection(config) as (collection, history):
        return save_records(collection, history, records)


def run_main(source: str, crawl) -> int:
    try:
        count = crawl()
        print(f"{source}: upserted {count} metadata records")
        return 0
    except CrawlerConfigError as exc:
        code, detail = "CONFIG_ERROR", str(exc)
    except TimeoutException:
        code, detail = "TIMEOUT", "page load or content wait timed out"
    except WebDriverException:
        code, detail = "BROWSER_ERROR", "browser operation failed"
    except PyMongoError:
        code, detail = "DB_ERROR", "MongoDB operation failed"
    except Exception as exc:
        code, detail = "CRAWLER_ERROR", type(exc).__name__
    print(f"{source}: {code}: {detail}", file=sys.stderr)
    return 1
