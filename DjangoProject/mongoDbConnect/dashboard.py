"""Read-only analyst queries and bounded view models; no clients at import.

Search/source/date select the latest MAX_SCAN candidates. Risk classification,
pagination and summary counts refer to that bounded window, not the whole DB.
All aggregate pipelines are reads ($out/$merge are never used).
"""
import base64
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import logging
import os
import re
from urllib.parse import urlencode, urlsplit

from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from alert.risk import LEVELS, MATERIAL_FIELDS, METADATA_FIELDS, classify_risk, utc_datetime

LOGGER = logging.getLogger(__name__)
MAX_SCAN = 1000
PAGE_SIZE = 25
HISTORY_LIMIT = 100
ALERT_LIMIT = 100
SOURCE_LIMIT = 100
QUERY_TIMEOUT_MS = 5000
RISK_LEVELS = tuple(reversed(LEVELS))
SEARCH_FIELDS = ("company_name", "company_url", "description", "data_contents", "country")
CURRENT_FIELDS = (*METADATA_FIELDS, "_id", "source", "event_key", "identity_basis",
                  "first_seen", "last_seen", "scraped_time", "observation_count", "schema_version")
HISTORY_FIELDS = {"_id": 1, "changed_at": 1, **{f"changes.{name}": 1 for name in METADATA_FIELDS}}
ALERT_FIELDS = dict.fromkeys(("_id", "event_type", "risk_level", "status", "attempt_count",
                             "created_at", "updated_at", "sent_at", "suppression_reason", "changed_fields"), 1)
FIELD_LABELS = {"company_name": "회사명", "company_url": "회사 URL", "country": "국가",
                "data_contents": "유출 내용", "data_size": "규모", "description": "설명",
                "publication_date": "게시일", "source_url": "출처 URL"}
DATABASE_ERROR = "MongoDB에서 데이터를 불러오지 못했습니다."


class DashboardUnavailable(Exception):
    pass


class EventNotFound(Exception):
    pass


@dataclass(frozen=True)
class Filters:
    q: str = ""
    source: str = ""
    risk: str = ""
    date_from: str = ""
    date_to: str = ""
    start: datetime | None = None
    end: datetime | None = None
    page: int = 1

    def query_params(self):
        return {key: value for key, value in (("q", self.q), ("source", self.source),
                ("risk", self.risk), ("from", self.date_from), ("to", self.date_to)) if value}


def parse_filters(params):
    errors = []
    def value(name, limit):
        raw = params.get(name, "")
        if not isinstance(raw, str):
            errors.append("검색 조건을 확인하세요.")
            return ""
        raw = raw.strip()
        if len(raw) > limit:
            errors.append("검색어는 100자, source는 64자 이내로 입력하세요." if name in {"q", "source"}
                          else "날짜 형식을 확인하세요. (YYYY-MM-DD)")
        return raw[:limit]
    q, source, risk = value("q", 100), value("source", 64), value("risk", 16)
    if risk and risk not in RISK_LEVELS:
        errors.append("위험도 선택값을 확인하세요.")
        risk = ""
    lower, upper = value("from", 10), value("to", 10)
    start = end = None
    try:
        for text in (lower, upper):
            if text and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
                raise ValueError
        if lower:
            start = datetime.combine(date.fromisoformat(lower), datetime.min.time(), timezone.utc)
        if upper:
            end = datetime.combine(date.fromisoformat(upper) + timedelta(days=1),
                                   datetime.min.time(), timezone.utc)
        if start and end and start >= end:
            errors.append("시작 날짜는 종료 날짜보다 늦을 수 없습니다.")
    except (ValueError, OverflowError):
        errors.append("날짜 형식을 확인하세요. (YYYY-MM-DD)")
    page = params.get("page", "1")
    try:
        page = int(page) if isinstance(page, str) and len(page) <= 8 else 1
    except ValueError:
        page = 1
    return Filters(q, source, risk, lower, upper, start, end, max(page, 1)), list(dict.fromkeys(errors))


def search_query(filters):
    query = {}
    if filters.q:
        literal = re.escape(filters.q)
        query["$or"] = [{name: {"$regex": literal, "$options": "i"}} for name in SEARCH_FIELDS]
    if filters.source:
        query["source"] = filters.source  # A scalar string, never a caller-supplied operator.
    return query


def candidate_pipeline(filters, max_scan=MAX_SCAN):
    observed = {"$ifNull": [
        {"$convert": {"input": "$last_seen", "to": "date", "onError": None, "onNull": None}},
        {"$convert": {"input": "$scraped_time", "to": "date", "onError": None, "onNull": None}},
    ]}
    pipeline = [{"$match": search_query(filters)},
                {"$project": dict.fromkeys(CURRENT_FIELDS, 1)},
                {"$addFields": {"_observed_at": observed}}]
    bounds = {}
    if filters.start:
        bounds["$gte"] = filters.start
    if filters.end:
        bounds["$lt"] = filters.end
    if bounds:
        pipeline.append({"$match": {"_observed_at": bounds}})
    pipeline.extend(({"$sort": {"_observed_at": -1, "_id": -1}}, {"$limit": max_scan + 1}))
    return pipeline


def safe_text(value, limit=500, secrets=()):
    """Preserve text for autoescape/json_script; remove credentials before truncation."""
    if not isinstance(value, str):
        return "-"
    value = value.strip() or "-"
    from governance.policy import redact_credentials
    value = redact_credentials(value, secrets=secrets)
    return value[:limit]


def safe_url(value, secrets=()):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (not value or len(value) > 2048 or "\\" in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or safe_text(value, 2048, secrets) != value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            return None
    except ValueError:
        return None
    return value


def encode_id(value):
    if isinstance(value, ObjectId):
        return "oid-" + str(value)
    if isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 256:
        return "str-" + base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
    if type(value) is int:
        return "int-" + str(value)
    return None


def decode_id(token):
    try:
        if token.startswith("oid-") and ObjectId.is_valid(token[4:]):
            return ObjectId(token[4:])
        if token.startswith("int-") and re.fullmatch(r"-?[0-9]{1,19}", token[4:]):
            number = int(token[4:])
            if -(2**63) <= number < 2**63:
                return number
        if token.startswith("str-") and re.fullmatch(r"[A-Za-z0-9_-]{1,342}", token[4:]):
            raw = base64.b64decode(token[4:] + "=" * (-len(token[4:]) % 4), altchars=b"-_", validate=True)
            if raw and len(raw) <= 256:
                value = raw.decode("utf-8")
                if encode_id(value) == token:
                    return value
    except (ValueError, UnicodeError):
        pass
    raise EventNotFound


def number(value, default=None):
    return value if type(value) is int and value >= 0 else default


def event_model(document, secrets=()):
    level, reason, actor = classify_risk(document)
    model = {name: safe_text(document.get(name), 2000, secrets) for name in METADATA_FIELDS}
    model.update(company_name=safe_text(document.get("company_name"), 200, secrets),
                 source=safe_text(document.get("source", "unknown"), 64, secrets),
                 company_href=safe_url(document.get("company_url"), secrets),
                 description_preview=safe_text(document.get("description"), 150, secrets),
                 first_seen=utc_datetime(document.get("first_seen")) or utc_datetime(document.get("scraped_time")),
                 last_seen=utc_datetime(document.get("_observed_at")) or utc_datetime(document.get("last_seen"))
                           or utc_datetime(document.get("scraped_time")),
                 observation_count=number(document.get("observation_count")),
                 schema_version=number(document.get("schema_version")),
                 event_key=safe_text(document.get("event_key"), 256, secrets),
                 identity_basis=safe_text(document.get("identity_basis"), 80, secrets),
                 document_id=safe_text(str(document.get("_id", "-")), 256, secrets),
                 link_token=encode_id(document.get("_id")),
                 risk_level=level if level in RISK_LEVELS else "INFO",
                 risk_reason=safe_text(reason, 400, secrets), risk_actor=safe_text(actor, 64, secrets))
    return model


def paginate(rows, filters):
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = filters.page if filters.page <= pages else 1
    start = (page - 1) * PAGE_SIZE
    def link(number):
        return "?" + urlencode({**filters.query_params(), "page": number})
    return {"data_list": rows[start:start + PAGE_SIZE], "page": page, "page_count": pages,
            "previous_url": link(page - 1) if page > 1 else None,
            "next_url": link(page + 1) if page < pages else None,
            "page_start": start + 1 if rows else 0, "page_end": min(start + PAGE_SIZE, len(rows))}


def summary_data(rows, *, changed_count=0, sent_count=0, now=None):
    now = utc_datetime(now) or datetime.now(timezone.utc)
    sources = Counter(row["source"] for row in rows)
    risks = Counter(row["risk_level"] for row in rows)
    return {"summary": {"event_count": len(rows), "source_count": len(sources),
                        "new_count": sum(row["first_seen"] is not None
                                         and now - timedelta(days=7) <= row["first_seen"] <= now for row in rows),
                        "changed_count": changed_count, "sent_count": sent_count},
            "chart_data": {"risk": [{"label": level, "count": risks[level]} for level in RISK_LEVELS],
                           "source": [{"label": source, "count": count} for source, count in
                                      sorted(sources.items(), key=lambda item: (-item[1], item[0]))]}}


def _aggregate(collection, pipeline):
    return collection.aggregate(pipeline, maxTimeMS=QUERY_TIMEOUT_MS, batchSize=50)


def fetch_dashboard(database, filters, *, max_scan=MAX_SCAN, now=None, secrets=()):
    max_scan = max(1, min(MAX_SCAN, max_scan))
    models, identities = [], []
    truncated = False
    with closing(_aggregate(database["leaked_data"], candidate_pipeline(filters, max_scan))) as cursor:
        for index, document in enumerate(cursor):
            if index == max_scan:
                truncated = True
                break
            row = event_model(document, secrets)
            if not filters.risk or row["risk_level"] == filters.risk:
                models.append(row)
                identities.append(document["_id"])
    options = []
    source_pipeline = [{"$match": {"source": {"$type": "string", "$ne": ""}}},
                       {"$group": {"_id": "$source"}}, {"$sort": {"_id": 1}}, {"$limit": SOURCE_LIMIT}]
    with closing(_aggregate(database["leaked_data"], source_pipeline)) as cursor:
        for item in cursor:
            source = item.get("_id")
            if isinstance(source, str) and len(source) <= 64 and safe_text(source, 64, secrets) == source:
                options.append(source)
    changed_count = sent_count = 0
    if identities:
        with closing(_aggregate(database["leak_history"], [
            {"$match": {"document_id": {"$in": identities}}}, {"$group": {"_id": "$document_id"}},
            {"$count": "total"}])) as cursor:
            changed_count = number(next(iter(cursor), {}).get("total"), 0)
        sent_count = database["alert_log"].count_documents(
            {"document_id": {"$in": identities}, "status": "SENT"}, maxTimeMS=QUERY_TIMEOUT_MS)
    return {**paginate(models, filters), **summary_data(models, changed_count=changed_count,
            sent_count=sent_count, now=now), "source_options": options,
            "candidate_truncated": truncated, "scan_limit": max_scan}


def history_model(document, secrets=()):
    raw = document.get("changes")
    changes = []
    if isinstance(raw, dict):
        for name in METADATA_FIELDS:
            delta = raw.get(name)
            if isinstance(delta, dict):
                changes.append({"field": name, "label": FIELD_LABELS[name],
                                "before": safe_text(delta.get("before"), 500, secrets),
                                "after": safe_text(delta.get("after"), 500, secrets)})
    return {"changed_at": utc_datetime(document.get("changed_at")), "changes": changes}


def alert_model(document, secrets=()):
    fields = document.get("changed_fields", [])
    return {"event_type": safe_text(document.get("event_type"), 16, secrets),
            "risk_level": safe_text(document.get("risk_level"), 16, secrets),
            "status": safe_text(document.get("status"), 16, secrets),
            "attempt_count": number(document.get("attempt_count")),
            "created_at": utc_datetime(document.get("created_at")),
            "sent_at": utc_datetime(document.get("sent_at")),
            "suppression_reason": safe_text(document.get("suppression_reason"), 80, secrets),
            "changed_fields": [name for name in MATERIAL_FIELDS if isinstance(fields, list) and name in fields]}


def fetch_detail(database, token, *, secrets=()):
    identity = decode_id(token)
    document = database["leaked_data"].find_one({"_id": identity},
                                               dict.fromkeys(CURRENT_FIELDS, 1), max_time_ms=QUERY_TIMEOUT_MS)
    if document is None:
        raise EventNotFound
    history, alerts = [], []
    history_more = alerts_more = False
    with closing(database["leak_history"].find({"document_id": identity}, HISTORY_FIELDS)
                 .sort([("changed_at", -1), ("_id", -1)]).limit(HISTORY_LIMIT + 1)
                 .max_time_ms(QUERY_TIMEOUT_MS)) as cursor:
        for index, item in enumerate(cursor):
            if index == HISTORY_LIMIT:
                history_more = True
                break
            history.append(history_model(item, secrets))
    with closing(database["alert_log"].find({"document_id": identity}, ALERT_FIELDS)
                 .sort([("created_at", -1), ("_id", -1)]).limit(ALERT_LIMIT + 1)
                 .max_time_ms(QUERY_TIMEOUT_MS)) as cursor:
        for index, item in enumerate(cursor):
            if index == ALERT_LIMIT:
                alerts_more = True
                break
            alerts.append(alert_model(item, secrets))
    return {"event": event_model(document, secrets), "history": history, "alerts": alerts,
            "history_more": history_more, "alerts_more": alerts_more,
            "history_limit": HISTORY_LIMIT, "alert_limit": ALERT_LIMIT}


@contextmanager
def mongo_database(environ=None, client_factory=None):
    values = os.environ if environ is None else environ
    uri, name = values.get("DB_URI"), values.get("DB_NAME", "darkweb")
    if not isinstance(uri, str) or not uri.strip() or not isinstance(name, str) or not name.strip():
        raise DashboardUnavailable
    client = None
    try:
        client = (client_factory or MongoClient)(
            uri, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000, socketTimeoutMS=5000)
        yield client[name]
    except (PyMongoError, ValueError, TypeError):
        raise DashboardUnavailable from None
    finally:
        if client is not None:
            client.close()


def configured_secrets():
    return tuple(os.environ.get(name, "") for name in ("DB_URI", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"))
