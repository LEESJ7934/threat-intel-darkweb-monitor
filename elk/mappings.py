"""Explicit ES 8 mappings; no risk policy or Mongo document mutation."""
from .config import TIME_FIELDS, VIEW_NAMES

METADATA_FIELDS = ("company_name", "company_url", "country", "data_contents",
                   "data_size", "publication_date", "description", "source_url")
DATE = {"type": "date", "format": "strict_date_optional_time||epoch_millis"}
INTEGER = {"type": "integer", "coerce": False}
KEYWORD = {"type": "keyword", "ignore_above": 2048}
TEXT = {"type": "text"}


def properties(kind):
    result = {}
    if kind == "events":
        keywords = ("source", "company_url", "country", "data_size", "publication_date",
                    "event_key", "identity_basis", "content_fingerprint", "source_url")
        result.update({field: dict(KEYWORD) for field in keywords})
        result["company_name"] = {"type": "text", "fields": {
            "keyword": {"type": "keyword", "ignore_above": 512}}}
        result.update({field: dict(TEXT) for field in ("data_contents", "description")})
        dates = ("first_seen", "last_seen", "scraped_time")
        integers = ("observation_count", "schema_version")
    elif kind == "history":
        result.update({field: dict(KEYWORD) for field in ("event_key", "document_id", "source")})
        result["changes"] = {"type": "object", "dynamic": "strict", "properties": {
            field: {"type": "object", "dynamic": "strict",
                    "properties": {"before": dict(TEXT), "after": dict(TEXT)}}
            for field in METADATA_FIELDS}}
        dates, integers = ("changed_at",), ("schema_version",)
    elif kind == "alerts":
        keywords = ("event_type", "event_key", "document_id", "history_id", "source",
                    "risk_level", "actor", "status", "suppression_reason", "changed_fields")
        result.update({field: dict(KEYWORD) for field in keywords})
        result["risk_reason"] = dict(TEXT)
        dates = ("created_at", "updated_at", "sent_at")
        integers = ("attempt_count", "schema_version")
    else:
        raise ValueError("Unknown mapping kind")
    result.update({field: dict(DATE) for field in dates})
    result.update({field: dict(INTEGER) for field in integers})
    return result


def mapping(config, kind):
    return {"dynamic": "strict",
            "_meta": {"day12_schema": 1, "source_database": config.database,
                      "index_prefix": config.prefix},
            "properties": properties(kind)}


def index_template(config, kind):
    return {"index_patterns": [config.index(kind)], "priority": 300, "version": 1,
            "template": {"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                         "mappings": mapping(config, kind)}}


def data_view(config, kind):
    # Kibana requires data view names to be unique within a space. Include the
    # prefix so isolated E2E/production prefixes can coexist without a 400
    # duplicate-name response while keeping the human-readable base name.
    return {"id": config.view_id(kind), "name": f"{VIEW_NAMES[kind]} [{config.prefix}]",
            "title": config.index(kind), "timeFieldName": TIME_FIELDS[kind],
            "allowNoIndex": True}


def mapping_matches(actual, expected):
    """ES may insert defaults; required types/options and ownership must match."""
    if not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        if key == "type" and value == "object" and "type" not in actual and "properties" in actual:
            continue  # ES can serialize an object mapping without the implicit type.
        if (key == "format" and value == "strict_date_optional_time||epoch_millis"
                and "format" not in actual and actual.get("type") == "date"):
            continue  # ES may omit its default date format when returning _mapping.
        if isinstance(value, dict):
            if not mapping_matches(actual.get(key), value):
                return False
        elif actual.get(key) != value:
            return False
    # Strict mappings with unexpected fields could expose obsolete private data.
    if "properties" in expected and set(actual.get("properties", {})) != set(expected["properties"]):
        return False
    return True
