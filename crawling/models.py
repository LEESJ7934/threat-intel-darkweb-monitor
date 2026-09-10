"""Metadata normalization and legacy document identities; no I/O."""
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from bs4 import Comment, NavigableString

UNKNOWN = "unknown"
METADATA_FIELDS = ("company_name", "company_url", "country", "data_contents", "data_size",
                   "publication_date", "description", "source_url")
BLOCK_TAGS = {"div", "p", "li", "ul", "ol", "section", "article", "h1", "h2", "h3", "h4", "tr"}


def normalize_text(value: str | None) -> str:
    if value is None:
        return UNKNOWN
    if not isinstance(value, str):
        raise TypeError("Metadata text must be a string")
    return value.strip() or UNKNOWN


def metadata_text(value) -> str:
    """Storage normalization, including the pre-Day 8 missing-value spellings."""
    if not isinstance(value, str):
        return UNKNOWN
    value = " ".join(value.split())
    return UNKNOWN if value.casefold() in {"", "unknown", "unknwon", "unknonn"} else value


def canonical_url(value) -> str | None:
    """Ignore HTTP(S) scheme/root slash; retain path case, www, port and query.

    No DNS lookup. Invalid URLs or URLs with credentials cannot identify an event.
    """
    value = metadata_text(value)
    if value == UNKNOWN or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        if parsed.scheme and parsed.scheme.casefold() not in {"http", "https"}:
            return None
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.casefold()
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port is not None:
            host += ":" + str(parsed.port)
        return urlunsplit(("", host, parsed.path.rstrip("/"), parsed.query, parsed.fragment))[2:]
    except ValueError:
        return None


def normalized_metadata(record) -> dict:
    """Allowlisted plain metadata only; never copy raw_html, tokens or other extras."""
    result = {name: metadata_text(record.get(name)) for name in METADATA_FIELDS}
    for name in ("company_url", "source_url"):
        # Do not propagate embedded URL credentials or unusable URL placeholders.
        if canonical_url(result[name]) is None:
            result[name] = UNKNOWN
    return result


def canonical_metadata(record) -> dict:
    result = normalized_metadata(record)
    for name in ("company_url", "source_url"):
        result[name] = canonical_url(result[name]) or UNKNOWN
    return result


def element_text(element) -> str:
    """Approximate Selenium text for static markup, including <br>/block lines.

    CSS layout is not evaluated offline. Inline tags do not introduce spaces;
    visible text whitespace is collapsed before the legacy ID inputs are built.
    """
    def visit(node):
        if isinstance(node, Comment):
            return ""
        if isinstance(node, NavigableString):
            return re.sub(r"[\s\u00a0]+", " ", str(node))
        if node.name in {"script", "style", "template"} or node.has_attr("hidden"):
            return ""
        style = re.sub(r"\s+", "", node.get("style", "")).lower()
        if "display:none" in style or "visibility:hidden" in style:
            return ""
        if node.name == "br":
            return "\n"
        content = "".join(visit(child) for child in node.children)
        return "\n" + content + "\n" if node.name in BLOCK_TAGS else content

    if element is None:
        return ""
    return "\n".join(line.strip() for line in visit(element).splitlines() if line.strip())


def text_at(item, selector: str, *, required=False, missing="") -> str:
    element = item.select_one(selector)
    value = element_text(element) if element is not None else missing
    if required and not value:
        raise ValueError("Missing item identity")
    return value


def legacy_id(prefix: str, *parts: str) -> str:
    # Compatibility with existing leaked_data IDs, not a security digest.
    raw_id = "_".join(parts)
    return prefix + hashlib.md5(raw_id.encode("utf-8"), usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class LeakRecord:
    _id: str
    scraped_time: datetime
    source: str
    source_url: str
    company_name: str = UNKNOWN
    company_url: str = UNKNOWN
    country: str = UNKNOWN
    data_contents: str = UNKNOWN
    data_size: str = UNKNOWN
    publication_date: str = UNKNOWN
    description: str = UNKNOWN
    schema_version: int = 1

    def to_document(self) -> dict:
        result = asdict(self)
        for field in ("company_name", "company_url", "country", "data_contents", "data_size",
                      "publication_date", "description", "source", "source_url"):
            result[field] = normalize_text(result[field])
        return result
