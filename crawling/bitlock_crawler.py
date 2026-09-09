"""Bitlock metadata parser; preserve the legacy ID inputs and 50-record limit."""
from crawling.common import crawl_page, parse_records, run_main
from crawling.models import UNKNOWN, legacy_id, text_at

SOURCE = "bitlock"
SOURCE_URL = "http://lockbit3753ekiocyo5epmpy6klmejchjtzddoekjlnt6mu3qh4de2id.onion/"
ITEM_SELECTOR = ".post-block.good"


def _parse_item(item):
    overview = text_at(item, "div:nth-of-type(1)", required=True)
    company_url = overview.replace("published", "").split("\n")[0].strip()
    if not company_url:
        raise ValueError("Missing item identity")
    description = text_at(item, "div.post-block-text", missing=UNKNOWN)
    # Legacy code strips BEFORE replacing Updated:, leaving a possible leading space.
    publication_date = text_at(item, "div.updated-post-date", missing=UNKNOWN).replace("Updated:", "")
    return {
        "_id": legacy_id("bitlock_", company_url, description, publication_date),
        "company_name": company_url.split(".")[0].strip(),
        "company_url": company_url, "description": description,
        "publication_date": publication_date,
    }


def parse_html(html, scraped_time=None) -> list[dict]:
    return parse_records(html, selector=ITEM_SELECTOR, source=SOURCE, source_url=SOURCE_URL,
                         parse_item=_parse_item, scraped_time=scraped_time, limit=50)


def crawl(config=None) -> int:
    return crawl_page(source_url=SOURCE_URL, selector=ITEM_SELECTOR, parser=parse_html, config=config)


def main() -> int:
    return run_main(SOURCE, crawl)


if __name__ == "__main__":
    raise SystemExit(main())
