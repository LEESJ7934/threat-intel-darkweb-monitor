"""Day 9 event identity and observed metadata changes. No network at import.

History is written before current state, without cross-collection transactions.
Replaying a failed change reuses its deterministic history ID.
"""
from datetime import datetime, timezone
import hashlib
import json
import re

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from crawling.models import (LeakRecord, METADATA_FIELDS, UNKNOWN, canonical_metadata,
                             canonical_url, metadata_text, normalized_metadata)

LEGACY_PREFIXES = {"gunra": "gunra_", "black_shrantac": "black_shrantac",
                   "dragonforce": "dragonforce_", "bitlock": "bitlock_"}
PROJECTION = dict.fromkeys((*METADATA_FIELDS, "_id", "source", "event_key", "identity_basis",
                           "scraped_time", "first_seen", "last_seen", "observation_count",
                           "content_fingerprint", "schema_version"), 1)


class StorageError(PyMongoError):
    pass


class IdentityConflict(StorageError):
    pass


class AmbiguousLegacyMatch(StorageError):
    pass


def _sha256(material) -> str:
    content = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def record_source(record) -> str:
    source = metadata_text(record.get("source")).casefold()
    if source != UNKNOWN:
        return source
    identity = record.get("_id")
    if isinstance(identity, str):
        for source, prefix in LEGACY_PREFIXES.items():
            if re.fullmatch(re.escape(prefix) + r"[0-9a-fA-F]{32}", identity):
                return source
    return UNKNOWN


def event_identity(record) -> tuple[str, str]:
    """Return deterministic event_key/identity_basis using only identity fields."""
    source = record_source(record)
    if source == UNKNOWN:
        raise ValueError("A known source is required")
    url = canonical_url(record.get("company_url"))
    name = metadata_text(record.get("company_name")).casefold()
    if url is not None:
        basis, value = "source+company_url", url
    elif name != UNKNOWN:
        basis, value = "source+company_name", name
    else:
        identity = record.get("_id")
        if identity is None or (isinstance(identity, str) and not identity.strip()):
            raise ValueError("A legacy document ID is required for fallback identity")
        basis, value = "source+legacy_id", [type(identity).__name__, str(identity)]
    return _sha256(["event-v1", source, basis, value]), basis


def content_fingerprint(record) -> str:
    return _sha256(canonical_metadata(record))


def _utc(value, *, legacy=False):
    if isinstance(value, str) and legacy:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if value.tzinfo is None:
            return None  # A string without an offset does not establish its timezone.
    if not isinstance(value, datetime):
        if legacy:
            return None
        raise ValueError("scraped_time must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        if not legacy:
            raise ValueError("scraped_time must be a timezone-aware datetime")
        # Old PyMongo reads returned naive datetimes for UTC BSON dates.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _incoming(record) -> dict:
    raw = record.to_document() if isinstance(record, LeakRecord) else dict(record)
    if raw.get("_id") is None or (isinstance(raw["_id"], str) and not raw["_id"].strip()):
        raise ValueError("A parser document ID is required")
    result = {**normalized_metadata(raw), "_id": raw["_id"], "source": record_source(raw),
              "scraped_time": _utc(raw.get("scraped_time"))}
    event_identity(result)  # Validate before writes, without accepting supplied storage fields.
    return result


def ensure_indexes(collection):
    # Schema v1 documents without event_key remain untouched and are not indexed.
    collection.create_index([("event_key", ASCENDING)], name="event_key_unique",
                            unique=True, partialFilterExpression={"event_key": {"$type": "string"}})


def _compatible(existing, incoming, *, exact_id=False) -> bool:
    source = record_source(existing)
    if source != incoming["source"] and not (source == UNKNOWN and exact_id):
        return False
    old_url = canonical_url(existing.get("company_url"))
    new_url = canonical_url(incoming.get("company_url"))
    if old_url is not None and new_url is not None:
        return old_url == new_url
    old_name = metadata_text(existing.get("company_name")).casefold()
    new_name = metadata_text(incoming.get("company_name")).casefold()
    if old_name != UNKNOWN and new_name != UNKNOWN:
        return old_name == new_name
    return exact_id


def _source_candidates(incoming):
    source = incoming["source"]
    expression = r"^\s*" + r"\s+".join(re.escape(part) for part in source.split()) + r"\s*$"
    clauses = [{"source": {"$regex": expression, "$options": "i"}}]
    if source in LEGACY_PREFIXES:
        clauses.append({"_id": {"$regex": "^" + re.escape(LEGACY_PREFIXES[source]) + r"[0-9a-fA-F]{32}$"}})
    return {"$or": clauses}


def _find_existing(collection, incoming, key):
    existing = collection.find_one({"event_key": key}, PROJECTION)
    if existing is not None:
        if not _compatible(existing, incoming, exact_id=existing["_id"] == incoming["_id"]):
            raise IdentityConflict("Stored event identity conflicts with its metadata")
        return existing
    existing = collection.find_one({"_id": incoming["_id"]}, PROJECTION)
    if existing is not None:
        if not _compatible(existing, incoming, exact_id=True):
            raise IdentityConflict("Legacy ID belongs to a different source or company")
        return existing
    # Read only this source's candidates, and compare canonical values in Python.
    # Also handles missing identity fields or their enrichment in managed records.
    matches = []
    for candidate in collection.find(_source_candidates(incoming), PROJECTION):
        if _compatible(candidate, incoming):
            matches.append(candidate)
            if len(matches) > 1:
                raise AmbiguousLegacyMatch("Multiple matching documents require explicit review")
    return matches[0] if matches else None


def _effective_metadata(existing, incoming):
    old = normalized_metadata(existing)
    new = normalized_metadata(incoming)
    old_canonical, new_canonical = canonical_metadata(old), canonical_metadata(new)
    effective = {}
    changes = {}
    for name in METADATA_FIELDS:
        if new[name] == UNKNOWN or old_canonical[name] == new_canonical[name]:
            effective[name] = old[name]
        else:
            effective[name] = new[name]
            changes[name] = {"before": old[name], "after": new[name]}
    return old, effective, changes


def _update_existing(collection, history, existing, incoming):
    old, effective, changes = _effective_metadata(existing, incoming)
    identity_record = {**effective, "_id": existing["_id"], "source": incoming["source"]}
    key, basis = event_identity(identity_record)
    # Missing identity information cannot demote a known URL. Enrichment may
    # promote a name/legacy fallback; document_id remains the history linkage.
    if key != existing.get("event_key"):
        owner = collection.find_one({"event_key": key}, {"_id": 1})
        if owner is not None and owner["_id"] != existing["_id"]:
            raise IdentityConflict("Enriched identity already belongs to another document")
    old_fingerprint = content_fingerprint(old)
    new_fingerprint = content_fingerprint(effective)
    stamp = incoming["scraped_time"]
    if changes:
        history_id = _sha256([key, old_fingerprint, new_fingerprint])
        change = {"_id": history_id, "event_key": key, "document_id": existing["_id"],
                  "source": incoming["source"], "changed_at": stamp, "changes": changes,
                  "schema_version": 2}
        history.update_one({"_id": history_id}, {"$setOnInsert": change}, upsert=True)
    first_seen = (_utc(existing.get("first_seen"), legacy=True)
                  or _utc(existing.get("scraped_time"), legacy=True) or stamp)
    updates = {**effective, "source": incoming["source"], "event_key": key, "identity_basis": basis,
               "first_seen": first_seen, "last_seen": stamp, "scraped_time": stamp,
               "content_fingerprint": new_fingerprint, "schema_version": 2}
    count = existing.get("observation_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
        update = {"$set": updates, "$inc": {"observation_count": 1}}
    else:
        # An existing v1 document represents at least one earlier observation.
        update = {"$set": {**updates, "observation_count": 2}}
    result = collection.update_one({"_id": existing["_id"]}, update)
    if result.matched_count != 1:
        raise StorageError("Current document disappeared before the observation was saved")


def save_record(collection, history, record) -> None:
    incoming = _incoming(record)
    key, basis = event_identity(incoming)
    existing = _find_existing(collection, incoming, key)
    if existing is None:
        document = {**incoming, "event_key": key, "identity_basis": basis,
                    "first_seen": incoming["scraped_time"], "last_seen": incoming["scraped_time"],
                    "observation_count": 1, "content_fingerprint": content_fingerprint(incoming),
                    "schema_version": 2}
        try:
            result = collection.update_one({"event_key": key}, {"$setOnInsert": document}, upsert=True)
        except DuplicateKeyError:
            existing = _find_existing(collection, incoming, key)
            if existing is None:
                raise
        else:
            if result.upserted_id is not None:
                return
            existing = _find_existing(collection, incoming, key)
            if existing is None:
                raise StorageError("Concurrent insert could not be read")
    _update_existing(collection, history, existing, incoming)


def save_records(collection, history, records) -> int:
    records = list(records)
    if not records:
        return 0
    ensure_indexes(collection)
    for record in records:
        save_record(collection, history, record)
    return len(records)
