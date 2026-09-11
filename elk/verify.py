"""Read-only pipeline verification. Equal counts are not a content checksum."""
from .config import COLLECTIONS, ElkError
from .http import elasticsearch_ready, kibana_ready, wait_until
from .mappings import data_view
from .setup import existing_mapping, find_view


def check_once(config, es, kibana, monstache, database):
    elasticsearch_ready(es)
    kibana_ready(kibana)
    if monstache.request("GET", "/healthz", text=True) != "ok":
        raise ElkError("unhealthy", "Monstache")
    counts = {}
    for kind, collection in COLLECTIONS.items():
        if existing_mapping(es, config, kind) is None:
            raise ElkError("index_missing", config.index(kind))
        mongo_count = database[collection].count_documents({}, maxTimeMS=5000)
        response = es.request("GET", "/" + config.index(kind) + "/_count")
        es_count = response.get("count")
        shards = response.get("_shards", {})
        if (type(es_count) is not int or es_count < 0 or not isinstance(shards, dict)
                or shards.get("failed", 0) != 0):
            raise ElkError("invalid_or_partial_count", config.index(kind))
        if mongo_count != es_count:
            raise ElkError("count_mismatch", config.index(kind))
        counts[kind] = (mongo_count, es_count)
        if find_view(kibana, data_view(config, kind)) is None:
            raise ElkError("data_view_missing", "Kibana")
    return counts


def verify(config, es, kibana, monstache, database, *, wait=60, emit=print):
    counts = wait_until(lambda: check_once(config, es, kibana, monstache, database), wait)
    emit("[PASS] Elasticsearch reachable")
    emit("[PASS] Kibana reachable")
    # /healthz is liveness only; mappings/counts/data views are checked separately.
    emit("[PASS] Monstache HTTP healthy")
    for kind, (mongo_count, es_count) in counts.items():
        emit(f"[PASS] {kind} Mongo={mongo_count} / ES={es_count}")
    emit("[PASS] Kibana data views=3")
    emit("DAY12_ELK_PIPELINE: PASS")
    return counts
