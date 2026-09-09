"""Black Shrantac metadata parser; importing this module performs no crawling."""
from crawling.common import crawl_page, parse_records, run_main
from crawling.models import UNKNOWN, legacy_id, text_at

SOURCE = "black_shrantac"
SOURCE_URL = "http://jvkpexgkuaw5toiph7fbgucycvnafaqmfvakymfh5pdxepvahw3xryqd.onion/"
ITEM_SELECTOR = ".book-card"


def _parse_item(item):
    company_name = text_at(item, "div > h3", required=True)
    country = text_at(item, "div > span", missing=UNKNOWN)
    data_size = text_at(item, "div:nth-of-type(4)", missing=UNKNOWN)
    return {
        # The missing underscore after the source name is intentional compatibility.
        "_id": legacy_id("black_shrantac", company_name, country, data_size),
        "company_name": company_name,
        "company_url": text_at(item, "div:nth-of-type(3)"),
        "country": country, "data_size": data_size,
    }


def parse_html(html, scraped_time=None) -> list[dict]:
    return parse_records(html, selector=ITEM_SELECTOR, source=SOURCE, source_url=SOURCE_URL,
                         parse_item=_parse_item, scraped_time=scraped_time)


def crawl(config=None) -> int:
    return crawl_page(source_url=SOURCE_URL, selector=ITEM_SELECTOR, parser=parse_html, config=config)


def main() -> int:
    return run_main(SOURCE, crawl)


if __name__ == "__main__":
    raise SystemExit(main())
