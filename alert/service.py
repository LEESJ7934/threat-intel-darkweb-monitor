"""Durable alert reservations, atomic delivery leases and a database change stream.

MongoDB and Telegram are separate systems: a send accepted just before a process
crash can be delivered again after lease expiry. This is not exactly-once delivery.
"""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
from uuid import uuid4

from bson import ObjectId
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

from alert.risk import (LEVELS, MATERIAL_FIELDS, METADATA_FIELDS, classify_risk,
                        format_telegram_message, meets_threshold, safe_text, utc_datetime)
from crawling.storage import event_identity

LOGGER = logging.getLogger(__name__)
STATE_ID = "main_change_stream"
PIPELINE = [{"$match": {"operationType": "insert",
                      "ns.coll": {"$in": ["leaked_data", "leak_history"]}}}]
CURRENT_PROJECTION = dict.fromkeys((*METADATA_FIELDS, "_id", "event_key", "source",
                                    "first_seen", "scraped_time"), 1)
INVALID_RESUME_CODES = {260, 280, 286}


def utc_now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AlertPolicy:
    min_level: str = "MEDIUM"
    max_attempts: int = 5
    retry_seconds: int = 60
    lease_seconds: int = 120

    def __post_init__(self):
        if self.min_level not in LEVELS:
            raise ValueError("ALERT_MIN_LEVEL must be CRITICAL/HIGH/MEDIUM/LOW/INFO")
        for field, name in (("max_attempts", "ALERT_MAX_ATTEMPTS"),
                            ("retry_seconds", "ALERT_RETRY_SECONDS"),
                            ("lease_seconds", "ALERT_LEASE_SECONDS")):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def from_mapping(cls, values):
        numbers = []
        for name, default in (("ALERT_MAX_ATTEMPTS", 5), ("ALERT_RETRY_SECONDS", 60),
                              ("ALERT_LEASE_SECONDS", 120)):
            value = str(values.get(name, default)).strip()
            if not re.fullmatch(r"[0-9]+", value):
                raise ValueError(f"{name} must be a positive integer")
            numbers.append(int(value))
        return cls(str(values.get("ALERT_MIN_LEVEL", "MEDIUM")).strip(), *numbers)


class InvalidAlertEvent(ValueError):
    pass


def alert_identity(event_type, identity):
    if event_type not in {"NEW", "UPDATED"} or identity is None:
        raise InvalidAlertEvent("Invalid alert identity")
    return hashlib.sha256((event_type + str(identity)).encode("utf-8")).hexdigest()


def _document_id(value):
    if isinstance(value, ObjectId):
        return value
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or safe_text(value, limit=256) != value):
        raise InvalidAlertEvent("Invalid document identity")
    return value


def _event_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvalidAlertEvent("Invalid event key")
    return value


def convert_change(change, current):
    """Return an allowlisted event; overlay history.after without mutating Mongo data."""
    if not isinstance(change, dict) or change.get("operationType") != "insert":
        return None
    namespace = change.get("ns")
    collection = namespace.get("coll") if isinstance(namespace, dict) else None
    if collection not in {"leaked_data", "leak_history"}:
        return None
    document = change.get("fullDocument")
    if not isinstance(document, dict):
        raise InvalidAlertEvent("Missing fullDocument")
    fields = []
    history_id = None
    if collection == "leak_history":
        event_type = "UPDATED"
        history_id = _document_id(document.get("_id"))
        document_id = _document_id(document.get("document_id"))
        existing = current.find_one({"_id": document_id}, CURRENT_PROJECTION)
        if existing is None:
            raise InvalidAlertEvent("Missing current document")
        effective = {name: deepcopy(existing[name]) for name in CURRENT_PROJECTION if name in existing}
        changes = document.get("changes")
        if not isinstance(changes, dict):
            raise InvalidAlertEvent("Invalid changes")
        for name in METADATA_FIELDS:
            delta = changes.get(name)
            if delta is not None:
                if not isinstance(delta, dict) or not isinstance(delta.get("after"), str):
                    raise InvalidAlertEvent("Invalid changed metadata")
                effective[name] = delta["after"]
                if name in MATERIAL_FIELDS:
                    fields.append(name)
        key = _event_key(document.get("event_key"))
        source = document.get("source", effective.get("source", "unknown"))
    else:
        event_type = "NEW"
        document_id = _document_id(document.get("_id"))
        effective = {name: deepcopy(document[name]) for name in CURRENT_PROJECTION if name in document}
        # Read-only compatibility for a v1 insert; reuse Day 9's identity function.
        key = document.get("event_key")
        if key is None:
            try:
                key = event_identity(effective)[0]
            except (ValueError, TypeError) as exc:
                raise InvalidAlertEvent("Missing event identity") from exc
        key = _event_key(key)
        source = effective.get("source", "unknown")
    effective["source"] = source
    return {"_id": alert_identity(event_type, history_id if history_id is not None else key),
            "event_type": event_type, "event_key": key, "document_id": document_id,
            "history_id": history_id, "source": source,
            "document": effective, "changed_fields": fields}


class AlertService:
    def __init__(self, database, sender, policy=None, *, clock=utc_now, secrets=()):
        self.current = database["leaked_data"]
        self.logs = database["alert_log"]
        self.sender = sender
        self.policy = policy or AlertPolicy()
        self.clock = clock
        self.secrets = tuple(s for s in secrets if s)

    def now(self):
        stamp = utc_datetime(self.clock())
        if stamp is None:
            raise ValueError("Invalid clock")
        return stamp

    def reserve(self, change):
        # Replayed reservations already contain their durable message/policy snapshot.
        # In particular, a deleted current document must not break a SENT history replay.
        if isinstance(change, dict) and change.get("operationType") == "insert":
            raw = change.get("fullDocument")
            ns = change.get("ns")
            if isinstance(raw, dict) and isinstance(ns, dict):
                prior_id = None
                try:
                    if ns.get("coll") == "leak_history":
                        prior_id = alert_identity("UPDATED", _document_id(raw.get("_id")))
                    elif ns.get("coll") == "leaked_data" and raw.get("event_key") is not None:
                        prior_id = alert_identity("NEW", _event_key(raw["event_key"]))
                except InvalidAlertEvent:
                    pass
                if prior_id and self.logs.find_one({"_id": prior_id}, {"_id": 1}) is not None:
                    return prior_id
        try:
            event = convert_change(change, self.current)
        except InvalidAlertEvent:
            # Quarantine malformed selected events durably; never log the raw event.
            token = change.get("_id") if isinstance(change, dict) else None
            if token is None:
                raise
            identity = hashlib.sha256(("INVALID" + json.dumps(token, sort_keys=True, default=str)).encode()).hexdigest()
            event = {"_id": identity, "event_type": "UPDATED" if change.get("ns", {}).get("coll") == "leak_history" else "NEW",
                     "event_key": None, "document_id": None, "history_id": None,
                     "source": "unknown", "document": None, "changed_fields": []}
        if event is None:
            return None
        level, reason, actor = classify_risk(event["document"])
        suppression = None
        if event["document"] is None:
            suppression = "invalid_event"
        elif event["event_type"] == "UPDATED" and not event["changed_fields"]:
            suppression = "non_material_change"
        elif not meets_threshold(level, self.policy.min_level):
            suppression = "below_threshold"
        clean = lambda value, limit=500: safe_text(value, secrets=self.secrets, limit=limit)
        stamp = self.now()
        row = {name: event[name] for name in ("_id", "event_type", "event_key", "document_id", "history_id")}
        # Identifiers are references, but must not contain configured credentials.
        for name in ("document_id", "history_id"):
            if isinstance(row[name], str) and any(s in row[name] for s in self.secrets):
                raise InvalidAlertEvent("Credential in identifier")
        row.update(source=clean(event["source"], 64), risk_level=level, risk_reason=clean(reason),
                   actor=clean(actor, 64), status="SUPPRESSED" if suppression else "PENDING",
                   attempt_count=0, created_at=stamp, updated_at=stamp, sent_at=None,
                   last_error_type="InvalidAlertEvent" if suppression == "invalid_event" else None,
                   suppression_reason=suppression, schema_version=1,
                   next_attempt_at=stamp, lease_until=None, changed_fields=event["changed_fields"],
                   message=None if suppression else format_telegram_message(
                       event["document"], level, reason, actor, event_type=event["event_type"],
                       changed_fields=event["changed_fields"], secrets=self.secrets))
        try:
            self.logs.update_one({"_id": row["_id"]}, {"$setOnInsert": row}, upsert=True)
        except DuplicateKeyError:
            if self.logs.find_one({"_id": row["_id"]}, {"_id": 1}) is None:
                raise
        return row["_id"]

    def due_query(self, now):
        return {"$or": [
            {"status": "PENDING", "attempt_count": {"$lt": self.policy.max_attempts}},
            {"status": "FAILED", "attempt_count": {"$lt": self.policy.max_attempts},
             "next_attempt_at": {"$lte": now}},
            {"status": "SENDING", "lease_until": {"$lte": now}},
        ]}

    async def deliver(self, identity):
        now = self.now()
        # Expired final attempts become terminal FAILED without another send.
        self.logs.update_one(
            {"_id": identity, "status": "SENDING", "attempt_count": {"$gte": self.policy.max_attempts},
             "lease_until": {"$lte": now}},
            {"$set": {"status": "FAILED", "updated_at": now, "last_error_type": "LeaseExpired",
                      "next_attempt_at": None}, "$unset": {"claim_token": "", "lease_until": ""}})
        claim = uuid4().hex
        query = {"_id": identity, "attempt_count": {"$lt": self.policy.max_attempts}, **self.due_query(now)}
        row = self.logs.find_one_and_update(
            query, {"$set": {"status": "SENDING", "updated_at": now, "claim_token": claim,
                             "lease_until": now + timedelta(seconds=self.policy.lease_seconds)},
                    "$inc": {"attempt_count": 1}}, return_document=ReturnDocument.AFTER)
        if row is None:
            return False
        ownership = {"_id": identity, "status": "SENDING", "claim_token": claim}
        try:
            # Bound in-flight work below the lease. Cancellation/crash uses lease recovery.
            await asyncio.wait_for(self.sender.send(row["message"]), timeout=self.policy.lease_seconds * 0.8)
        except Exception as exc:
            stamp = self.now()
            error_type = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__)[:64] or "DeliveryError"
            self.logs.update_one(ownership, {
                "$set": {"status": "FAILED", "updated_at": stamp, "last_error_type": error_type,
                         "next_attempt_at": stamp + timedelta(seconds=self.policy.retry_seconds)},
                "$unset": {"claim_token": "", "lease_until": ""}})
            LOGGER.warning("Telegram delivery failed (%s)", error_type)
            return False
        stamp = self.now()
        result = self.logs.update_one(ownership, {
            "$set": {"status": "SENT", "updated_at": stamp, "sent_at": stamp,
                     "last_error_type": None, "next_attempt_at": None},
            "$unset": {"claim_token": "", "lease_until": ""}})
        if result.matched_count == 1:
            LOGGER.info("Alert delivered (%s)", row["event_type"])
        return result.matched_count == 1

    async def retry_due(self):
        for row in self.logs.find(self.due_query(self.now()), {"_id": 1}).limit(100):
            await self.deliver(row["_id"])


class TelegramSender:
    """One initialized Bot for the service lifetime; tests inject a fake factory."""
    def __init__(self, token, chat_id, *, bot_factory=None):
        self.token, self.chat_id, self.bot_factory = token, chat_id, bot_factory
        self.bot = None

    async def __aenter__(self):
        factory = self.bot_factory
        if factory is None:
            from telegram import Bot
            factory = Bot
        self.bot = factory(token=self.token)
        try:
            await self.bot.initialize()
        except BaseException:
            await self.bot.shutdown()
            raise
        return self

    async def __aexit__(self, *args):
        await self.bot.shutdown()

    async def send(self, message):
        await self.bot.send_message(chat_id=self.chat_id, text=message, parse_mode=None,
                                    disable_web_page_preview=True, connect_timeout=10,
                                    read_timeout=20, write_timeout=20, pool_timeout=10)


async def watch_forever(db_uri, db_name, sender, policy=None, *, client_factory=None,
                        clock=utc_now, sleep=asyncio.sleep, stop=lambda: False, secrets=()):
    """Resume after a durable reservation; drain retries even on idle streams."""
    policy = policy or AlertPolicy()
    factory = client_factory or MongoClient
    while not stop():
        client = None
        state = None
        delay = min(policy.retry_seconds, 30)
        try:
            client = factory(db_uri, tz_aware=True, tzinfo=timezone.utc, w="majority",
                             serverSelectionTimeoutMS=10_000, connectTimeoutMS=10_000,
                             socketTimeoutMS=10_000)
            database = client[db_name]
            state = database["alert_state"]
            saved = state.find_one({"_id": STATE_ID}) or {}
            token = saved.get("resume_token")
            if token is not None and not isinstance(token, dict):
                state.update_one({"_id": STATE_ID}, {"$unset": {"resume_token": ""},
                                 "$set": {"last_error_type": "InvalidResumeToken", "updated_at": utc_datetime(clock())}})
                token = None
            options = {"max_await_time_ms": 1000}
            if token is not None:
                options["resume_after"] = token
            service = AlertService(database, sender, policy, clock=clock, secrets=secrets)
            with database.watch(PIPELINE, **options) as stream:
                LOGGER.info("Alert change stream ready (NEW/UPDATED)")
                while not stop():
                    await service.retry_due()
                    change = stream.try_next()
                    if change is None:
                        if not stream.alive:
                            raise PyMongoError("Change stream ended")
                        continue
                    identity = service.reserve(change)
                    # Reservation failures raise before checkpointing. PENDING
                    # messages survive checkpoint failure and process restart.
                    state.update_one({"_id": STATE_ID},
                                     {"$set": {"resume_token": change["_id"],
                                               "updated_at": service.now()}}, upsert=True)
                    if identity is not None:
                        await service.deliver(identity)
        except KeyboardInterrupt:
            return
        except OperationFailure as exc:
            if exc.code in INVALID_RESUME_CODES and state is not None:
                try:
                    state.update_one({"_id": STATE_ID}, {"$unset": {"resume_token": ""},
                                     "$set": {"last_error_type": "InvalidResumeToken",
                                              "updated_at": utc_datetime(clock())}}, upsert=True)
                except PyMongoError:
                    LOGGER.error("Resume checkpoint reset failed")
                LOGGER.error("Resume token unavailable; restart from current time (a gap is possible)")
                delay = 1
            else:
                LOGGER.error("MongoDB operation failed (OperationFailure)")
        except (PyMongoError, InvalidAlertEvent, OSError):
            LOGGER.error("Watcher processing failed; reconnecting")
        finally:
            if client is not None:
                client.close()
        if not stop():
            await sleep(delay)
