"""Gunra metadata parser. Run explicitly with python -m crawling.gunra_crawler."""
from crawling.common import crawl_page, parse_records, run_main
from crawling.models import UNKNOWN, legacy_id, text_at

SOURCE = "gunra"
SOURCE_URL = "http://safepaypfxntwixwjrlcscft433ggemlhgkkdupi2ynhtcmvdgubmoyd.onion/"
ITEM_SELECTOR = ".tile"


def _parse_item(item):
    company_names = text_at(item, "strong a", required=True)
    parts = company_names.split("|")
    # Legacy ID includes the full heading and UNSTRIPPED size segment.
    data_size = parts[1] if len(parts) > 1 else UNKNOWN
    overview = text_at(item, "div:nth-of-type(2)").split("|")
    country = overview[1].strip() if len(overview) > 2 else UNKNOWN
    company_url = overview[2].strip() if len(overview) > 2 else UNKNOWN
    data_contents = text_at(item, "ul > li > a", missing=UNKNOWN)
    return {
        "_id": legacy_id("gunra_", company_names, data_contents, data_size),
        "company_name": parts[0], "company_url": company_url, "country": country,
        "data_contents": data_contents, "data_size": data_size,
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
