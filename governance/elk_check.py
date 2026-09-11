"""Optional read-only ES exposure check; never downloads metadata bodies."""
from elk.config import COLLECTIONS, ElkError
from elk.setup import existing_mapping

from .policy import prohibited_fields


def check_exposure(config, es):
    for kind, collection in COLLECTIONS.items():
        # Day12 verifies strict mappings, allowed fields and DB/prefix ownership.
        if existing_mapping(es, config, kind) is None:
            raise ElkError("index_missing", "Elasticsearch")
        # Inspect _source KEYS on the server as well: an old non-indexed field
        # could remain in _source despite no longer being present in the mapping.
        body = {
            "size": 0,
            "timeout": "5s",
            "runtime_mappings": {"day13_private_source_field": {
                "type": "boolean",
                "script": {
                    "source": "boolean found = false; for (def k : params._source.keySet()) { "
                              "if (params.blocked.contains(k.toLowerCase())) { found = true; break; } } emit(found);",
                    "params": {"blocked": sorted(prohibited_fields(collection, analytics=True))},
                },
            }},
            "query": {"term": {"day13_private_source_field": True}},
            "track_total_hits": True,
        }
        result = es.request("POST", "/" + config.index(kind) + "/_search", body)
        total = result.get("hits", {}).get("total")
        if (result.get("timed_out") is not False or result.get("_shards", {}).get("failed") != 0
                or not isinstance(total, dict) or total.get("relation") != "eq"
                or type(total.get("value")) is not int):
            raise ElkError("incomplete_exposure_check", "Elasticsearch")
        if total["value"] != 0:
            raise ElkError("prohibited_source_fields", "Elasticsearch")
    return True
