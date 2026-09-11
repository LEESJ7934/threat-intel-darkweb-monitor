"""Idempotent, prefix-scoped setup. No deletes or Mongo operations."""
from urllib.parse import quote

from .config import COLLECTIONS, ElkError
from .http import elasticsearch_ready, kibana_ready, wait_until
from .mappings import data_view, index_template, mapping, mapping_matches


def existing_mapping(es, config, kind):
    index = config.index(kind)
    response = es.request("GET", f"/{index}/_mapping", allow_missing=True)
    if response is None:
        return None
    result = response.get(index)
    actual = result.get("mappings", {}) if isinstance(result, dict) else {}
    if not mapping_matches(actual, mapping(config, kind)):
        raise ElkError("mapping_or_database_conflict", index)
    return actual


def view_matches(view, wanted):
    return isinstance(view, dict) and all(
        view.get(key) == wanted[key] for key in ("title", "timeFieldName"))


def find_view(kibana, wanted):
    response = kibana.request("GET", "/api/data_views/data_view/" + quote(wanted["id"], safe=""),
                             allow_missing=True)
    if response is not None:
        if not view_matches(response.get("data_view"), wanted):
            raise ElkError("data_view_conflict", "Kibana")
        return response["data_view"]
    response = kibana.request("GET", "/api/data_views")
    views = response.get("data_view")
    if not isinstance(views, list):
        raise ElkError("invalid_response", "Kibana")
    for view in views:
        if not isinstance(view, dict) or view.get("title") != wanted["title"]:
            continue
        identity = view.get("id")
        if not isinstance(identity, str) or not 0 < len(identity) <= 256:
            raise ElkError("invalid_response", "Kibana")
        full = kibana.request("GET", "/api/data_views/data_view/" + quote(identity, safe=""))
        if not view_matches(full.get("data_view"), wanted):
            raise ElkError("data_view_time_field_conflict", "Kibana")
        return full["data_view"]
    return None


def setup(config, es, kibana, *, wait=60, emit=print):
    wait_until(lambda: elasticsearch_ready(es), wait)
    emit("[PASS] Elasticsearch ready")
    # Check existing ownership/types before changing templates for this prefix.
    for kind in COLLECTIONS:
        existing_mapping(es, config, kind)
    for kind in COLLECTIONS:
        result = es.request("PUT", "/_index_template/" + config.template_name(kind),
                            index_template(config, kind))
        if result.get("acknowledged") is not True:
            raise ElkError("template_not_acknowledged", config.index(kind))
        if existing_mapping(es, config, kind) is None:
            try:
                result = es.request("PUT", "/" + config.index(kind), {})
                if result.get("acknowledged") is not True:
                    raise ElkError("index_not_acknowledged", config.index(kind))
            except ElkError as error:
                if error.status not in {400, 409}:
                    raise
                # A concurrent setup may have created the same index.
            if existing_mapping(es, config, kind) is None:
                raise ElkError("index_missing_after_create", config.index(kind))
        emit("[PASS] template/index " + config.index(kind))
    wait_until(lambda: kibana_ready(kibana), wait)
    emit("[PASS] Kibana ready")
    for kind in COLLECTIONS:
        wanted = data_view(config, kind)
        if find_view(kibana, wanted) is None:
            try:
                result = kibana.request("POST", "/api/data_views/data_view",
                                        {"data_view": wanted, "override": False})
                if not view_matches(result.get("data_view"), wanted):
                    raise ElkError("invalid_data_view_response", "Kibana")
            except ElkError as error:
                if error.status != 409 or find_view(kibana, wanted) is None:
                    raise
        emit("[PASS] data view " + config.index(kind))
    emit("DAY12_ELK_SETUP: PASS")
