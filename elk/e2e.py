"""Synthetic PC fixtures only; never imported by crawlers or alert delivery."""
from datetime import datetime, timezone
from hashlib import sha256
import re

from .config import COLLECTIONS, ElkError
from .http import wait_until
from .setup import existing_mapping

EVENT_ID = "day12-synthetic-event"
EVENT_KEY = sha256(b"day12-synthetic-event-key").hexdigest()
SOURCE = "day12_synthetic"
PRIVATE_MARKER = "day12-private-field-must-not-reach-es"


def guard(config, database):
    if (database != config.database or re.fullmatch(r"day12_elk_e2e(?:_[a-z0-9_]{1,40})?", database) is None
            or not config.prefix.startswith(("day12-", "day12_"))):
        raise ElkError("test_database_and_prefix_required", "E2E")


def history_id(revision):
    return sha256(f"day12-history-{revision}".encode()).hexdigest()


def alert_id(revision):
    return sha256(f"day12-alert-{revision}".encode()).hexdigest()


def fingerprint(revision):
    return sha256(f"day12-synthetic-content-{revision}".encode()).hexdigest()


def current(database):
    row = database["leaked_data"].find_one({"_id": EVENT_ID}, max_time_ms=5000)
    if row is None or row.get("event_key") != EVENT_KEY or row.get("source") != SOURCE:
        raise ElkError("fixture_missing_or_identity_conflict", "E2E")
    return row


def seed(config, database_name, database, *, now=None):
    guard(config, database_name)
    stamp = now or datetime.now(timezone.utc)
    record = {"_id": EVENT_ID, "event_key": EVENT_KEY, "source": SOURCE,
              "identity_basis": "source+company_url", "company_name": "Synthetic Day12 Company",
              "company_url": "https://day12.invalid", "country": "ZZ",
              "description": "Synthetic metadata only", "data_contents": "Synthetic test overview",
              "data_size": "10 GB", "publication_date": "revision-1", "source_url": "unknown",
              "first_seen": stamp, "last_seen": stamp, "scraped_time": stamp,
              "observation_count": 1, "content_fingerprint": fingerprint(1), "schema_version": 2}
    database["leaked_data"].update_one({"_id": EVENT_ID}, {"$setOnInsert": record}, upsert=True)
    row = current(database)
    if row.get("observation_count") != 1:
        raise ElkError("fixture_already_updated_use_new_test_database_and_prefix", "E2E")
    # Ensure all three namespaces exist before Monstache's initial direct read.
    history = {"_id": history_id(1), "event_key": EVENT_KEY, "document_id": EVENT_ID,
               "source": SOURCE, "changed_at": row["first_seen"], "schema_version": 2,
               "changes": {"data_size": {"before": "unknown", "after": "10 GB"}}}
    alert = {"_id": alert_id(1), "event_type": "NEW", "event_key": EVENT_KEY,
             "document_id": EVENT_ID, "history_id": None, "source": SOURCE, "actor": SOURCE,
             "risk_level": "INFO", "risk_reason": "Synthetic initial ELK fixture",
             "status": "SUPPRESSED", "suppression_reason": "day12_synthetic_test",
             "attempt_count": 0, "changed_fields": [], "schema_version": 1,
             "created_at": row["first_seen"], "updated_at": row["first_seen"], "sent_at": None,
             "claim_token": PRIVATE_MARKER, "resume_token": PRIVATE_MARKER,
             "message": PRIVATE_MARKER, "raw_exception": PRIVATE_MARKER}
    database["leak_history"].update_one({"_id": history["_id"]}, {"$setOnInsert": history}, upsert=True)
    database["alert_log"].update_one({"_id": alert["_id"]}, {"$setOnInsert": alert}, upsert=True)


def update(config, database_name, database, revision, *, now=None):
    guard(config, database_name)
    if revision not in {2, 3}:
        raise ElkError("invalid_revision", "E2E")
    row = current(database)
    previous = row.get("observation_count")
    if previous not in {revision - 1, revision}:
        raise ElkError("fixture_revision_out_of_order", "E2E")
    stamp = now or datetime.now(timezone.utc)
    before, after = f"{(revision - 1) * 10} GB", f"{revision * 10} GB"
    history = {"_id": history_id(revision), "event_key": EVENT_KEY, "document_id": EVENT_ID,
               "source": SOURCE, "changed_at": stamp, "schema_version": 2,
               "changes": {"data_size": {"before": before, "after": after}}}
    alert = {"_id": alert_id(revision), "event_type": "UPDATED", "event_key": EVENT_KEY,
             "document_id": EVENT_ID, "history_id": history_id(revision), "source": SOURCE,
             "risk_level": "INFO", "risk_reason": "Synthetic ELK fixture; no Telegram delivery",
             "actor": SOURCE, "status": "SUPPRESSED", "attempt_count": 0,
             "created_at": stamp, "updated_at": stamp, "sent_at": None,
             "suppression_reason": "day12_synthetic_test", "changed_fields": ["data_size"],
             "schema_version": 1, "claim_token": PRIVATE_MARKER, "resume_token": PRIVATE_MARKER,
             "message": PRIVATE_MARKER, "raw_exception": PRIVATE_MARKER}
    database["leak_history"].update_one({"_id": history["_id"]}, {"$setOnInsert": history}, upsert=True)
    database["alert_log"].update_one({"_id": alert["_id"]}, {"$setOnInsert": alert}, upsert=True)
    if previous == revision - 1:
        result = database["leaked_data"].update_one(
            {"_id": EVENT_ID, "observation_count": previous},
            {"$set": {"data_size": after, "publication_date": f"revision-{revision}",
                      "observation_count": revision, "content_fingerprint": fingerprint(revision),
                      "last_seen": stamp, "scraped_time": stamp}})
        if result.matched_count != 1:
            raise ElkError("fixture_concurrent_update", "E2E")


def timestamp(value):
    try:
        if isinstance(value, datetime):
            stamp = value
        elif type(value) in {int, float}:
            stamp = datetime.fromtimestamp(value / 1000, timezone.utc)
        else:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        return int(stamp.timestamp() * 1000)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ElkError("invalid_timestamp", "E2E") from None


def verify_once(config, database_name, database, es, revision):
    guard(config, database_name)
    if revision not in {1, 2, 3}:
        raise ElkError("invalid_revision", "E2E")
    row = current(database)
    if row.get("observation_count") != revision:
        raise ElkError("fixture_revision_mismatch", "E2E")
    identities = [("events", EVENT_ID)]
    for number in range(1, revision + 1):
        identities.extend((("history", history_id(number)), ("alerts", alert_id(number))))
    for kind in COLLECTIONS:
        if existing_mapping(es, config, kind) is None:
            raise ElkError("index_missing", config.index(kind))
    for kind, identity in identities:
        mongo = database[COLLECTIONS[kind]].find_one({"_id": identity}, max_time_ms=5000)
        response = es.request("GET", f"/{config.index(kind)}/_doc/{identity}", allow_missing=True)
        if (mongo is None or not response or response.get("_id") != identity
                or response.get("found") is not True):
            raise ElkError("document_id_not_synced", config.index(kind))
        document = response.get("_source", {})
        if (not isinstance(document, dict) or document.get("event_key") != EVENT_KEY
                or "_id" in document):
            raise ElkError("document_content_mismatch", config.index(kind))
        fields = {"events": ("data_size", "publication_date", "observation_count", "content_fingerprint"),
                  "history": ("document_id", "changes"),
                  "alerts": ("document_id", "history_id", "status", "risk_level", "changed_fields")}[kind]
        if any(document.get(field) != mongo.get(field) for field in fields):
            raise ElkError("document_content_mismatch", config.index(kind))
        for field in {"events": ("first_seen", "last_seen", "scraped_time"),
                      "history": ("changed_at",), "alerts": ("created_at", "updated_at")}[kind]:
            if timestamp(document.get(field)) != timestamp(mongo.get(field)):
                raise ElkError("timestamp_not_synced", config.index(kind))
        if kind == "alerts" and any(field in document for field in
                                   ("claim_token", "resume_token", "message", "raw_exception")):
            raise ElkError("private_field_leak", config.index(kind))
    return len(identities)


def verify_fixture(config, database_name, database, es, revision, *, wait=60, emit=print):
    count = wait_until(lambda: verify_once(config, database_name, database, es, revision), wait)
    emit(f"[PASS] synthetic revision={revision}, document IDs/content/timestamps={count}")
    emit("[PASS] private alert fields excluded")
    emit("DAY12_ELK_E2E: PASS")
