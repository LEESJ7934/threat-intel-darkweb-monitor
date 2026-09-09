"""Dragonforce metadata parser; retain the observed description and publication date."""
from crawling.common import crawl_page, parse_records, run_main
from crawling.models import UNKNOWN, legacy_id, text_at

SOURCE = "dragonforce"
SOURCE_URL = "http://xjhmtitnrdrgzw4vmsghirdoo2fk35a3tzj4enlmah4pvehdspydsiyd.onion/blog"
ITEM_SELECTOR = ".publications-list__publication"


def _parse_item(item):
    company_name = text_at(item, ".list-publication__name", required=True)
    description = text_at(item, ".list-publication__description", missing=UNKNOWN)
    data_size = text_at(
        item, "p.publication-addictional__row:last-of-type span.addictional-row__text", missing=UNKNOWN)
    company_url = text_at(item,
        "div.list-publication__addictional p.publication-addictional__row "
        "a.addiction-row__text.addictional-row__link")
    return {
        "_id": legacy_id("dragonforce_", company_name, description, data_size),
        "company_name": company_name, "company_url": company_url, "data_size": data_size,
        "description": description,
        "publication_date": text_at(item, ".publication-footer__date"),
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
