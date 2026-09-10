"""Day 10 offline contracts. Synthetic metadata and fake Mongo/Telegram only."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

from alert import alert as entry
from alert.risk import classify_risk, format_telegram_message, meets_threshold, utc_datetime
from alert.service import (AlertPolicy, AlertService, STATE_ID, TelegramSender,
                           alert_identity, convert_change, watch_forever)
from scripts.check_runtime_config import validate_config

NOW = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)
KEY = "a" * 64
DOC_ID = "bitlock_" + "b" * 32


def document(**overrides):
    return {"_id": DOC_ID, "event_key": KEY, "source": "bitlock",
            "company_name": "Example Corp", "company_url": "https://company.example/",
            "country": "ZZ", "data_size": "2 TB", "data_contents": "synthetic overview",
            "description": "synthetic description", "publication_date": "2026-09-10",
            "source_url": "https://source.example/", "first_seen": NOW, "scraped_time": NOW,
            **overrides}


def history(changes=None, **overrides):
    return {"_id": "c" * 64, "event_key": KEY, "document_id": DOC_ID, "source": "bitlock",
            "changed_at": NOW, "schema_version": 2,
            "changes": changes if changes is not None else {"data_size": {"before": "1 GB", "after": "2 TB"}},
            **overrides}


def change(collection="leaked_data", doc=None, token="one", **overrides):
    return {"_id": {"_data": token}, "operationType": "insert", "ns": {"coll": collection},
            "fullDocument": document() if doc is None else deepcopy(doc), **overrides}


class Cursor(list):
    def limit(self, count):
        return Cursor(self[:count])


class Collection:
    """Stateful Mongo contract fake, rejecting unsupported operators."""
    def __init__(self, name, events):
        self.name, self.events = name, events
        self.docs = {}
        self.fail_update = False
        self.fail_claim = False

    def matches(self, doc, query):
        for key, value in query.items():
            if key == "$or":
                if not any(self.matches(doc, part) for part in value):
                    return False
            elif isinstance(value, dict):
                actual = doc.get(key)
                for operator, operand in value.items():
                    if operator not in {"$lt", "$lte", "$gte"}:
                        raise AssertionError("Unsupported comparison")
                    if actual is None:
                        return False
                    if operator == "$lt" and not actual < operand:
                        return False
                    if operator == "$lte" and not actual <= operand:
                        return False
                    if operator == "$gte" and not actual >= operand:
                        return False
            elif doc.get(key) != value:
                return False
        return True

    def find(self, query, projection=None):
        result = []
        for doc in self.docs.values():
            if self.matches(doc, query):
                result.append(deepcopy(doc if projection is None else {
                    k: v for k, v in doc.items() if k == "_id" or projection.get(k)}))
        return Cursor(result)

    def find_one(self, query, projection=None):
        return next(iter(self.find(query, projection)), None)

    def update_one(self, query, update, upsert=False):
        self.events.append((self.name, deepcopy(query), deepcopy(update), upsert))
        if self.fail_update:
            self.fail_update = False
            raise PyMongoError("synthetic failure")
        if set(update) - {"$set", "$setOnInsert", "$inc", "$unset"}:
            raise AssertionError("Unsupported write operator")
        old = self.find_one(query)
        if old is None and not upsert:
            return SimpleNamespace(matched_count=0, upserted_id=None)
        doc = old if old is not None else {"_id": query["_id"]}
        if old is None:
            doc.update(deepcopy(update.get("$setOnInsert", {})))
        doc.update(deepcopy(update.get("$set", {})))
        for key, value in update.get("$inc", {}).items():
            doc[key] = doc.get(key, 0) + value
        for key in update.get("$unset", {}):
            doc.pop(key, None)
        if old is None and doc["_id"] in self.docs:
            raise DuplicateKeyError("synthetic duplicate")
        self.docs[doc["_id"]] = deepcopy(doc)
        return SimpleNamespace(matched_count=int(old is not None),
                               upserted_id=doc["_id"] if old is None else None)

    def find_one_and_update(self, query, update, *, return_document):
        if return_document != ReturnDocument.AFTER:
            raise AssertionError("Delivery must read the claimed document")
        if self.fail_claim or self.find_one(query) is None:
            return None
        identity = self.find_one(query)["_id"]
        self.update_one(query, update)
        return self.find_one({"_id": identity})


class Database:
    def __init__(self):
        self.events = []
        self.collections = {}
        self.streams = []
        self.watch_calls = []
        self.stopped = False

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = Collection(name, self.events)
        return self.collections[name]

    def watch(self, pipeline, **options):
        self.watch_calls.append((deepcopy(pipeline), deepcopy(options)))
        if not self.streams:
            raise AssertionError("Unexpected watcher reconnect")
        item = self.streams.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Stream:
    alive = True

    def __init__(self, database, events):
        self.database, self.events = database, list(events)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def try_next(self):
        if not self.events:
            self.database.stopped = True
            return None
        value = self.events.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value() if callable(value) else deepcopy(value)


class Client:
    def __init__(self, database):
        self.database, self.closed = database, False

    def __getitem__(self, name):
        return self.database

    def close(self):
        self.closed = True


class AlertsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.getaddrinfo", "pymongo.MongoClient",
                       "alert.service.MongoClient"):
            self.enterContext(patch(target, side_effect=AssertionError("External I/O forbidden")))
        self.db = Database()
        self.db["leaked_data"].docs[DOC_ID] = document()
        self.sender = SimpleNamespace(send=AsyncMock())
        self.stamp = NOW
        self.service = AlertService(self.db, self.sender, clock=lambda: self.stamp)
        self.clients = []

    def row(self, identity):
        return self.db["alert_log"].find_one({"_id": identity})

    async def process(self, event):
        identity = self.service.reserve(event)
        if identity is not None:
            await self.service.deliver(identity)
        return identity

    def client_factory(self, *args, **kwargs):
        self.client_options = kwargs
        client = Client(self.db)
        self.clients.append(client)
        return client

    async def watch(self, *, sleep=None):
        await watch_forever("mongodb://unused", "synthetic", self.sender,
                            client_factory=self.client_factory, clock=lambda: self.stamp,
                            sleep=sleep or AsyncMock(), stop=lambda: self.db.stopped)

    def test_import_without_environment_or_clients_and_django_reexport(self):
        script = """
import os, sys, types
from unittest.mock import patch, Mock
fake = types.ModuleType('telegram')
fake.Bot = Mock(side_effect=AssertionError('Bot at import'))
with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {'telegram': fake}), \\
     patch('dotenv.load_dotenv', side_effect=AssertionError('env load')), \\
     patch('pymongo.MongoClient', side_effect=AssertionError('Mongo at import')), \\
     patch('socket.socket.connect', side_effect=AssertionError('network')), \\
     patch('socket.getaddrinfo', side_effect=AssertionError('DNS')), \\
     patch('sys.exit', side_effect=AssertionError('exit at import')):
    from alert.alert import classify_risk
    assert classify_risk({})[0] == 'INFO'
    fake.Bot.assert_not_called()
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_identity_is_sha256_new_plus_event_key(self):
        self.assertEqual(alert_identity("NEW", KEY), hashlib.sha256(("NEW" + KEY).encode()).hexdigest())
        self.assertEqual(alert_identity("NEW", KEY), alert_identity("NEW", KEY))

    def test_updated_identity_is_history_based_and_distinct(self):
        self.assertEqual(alert_identity("UPDATED", "c" * 64),
                         hashlib.sha256(("UPDATED" + "c" * 64).encode()).hexdigest())
        self.assertNotEqual(alert_identity("UPDATED", "c" * 64), alert_identity("UPDATED", "d" * 64))
        self.assertNotEqual(alert_identity("NEW", KEY), alert_identity("UPDATED", KEY))

    def test_default_and_configurable_thresholds(self):
        for level in ("CRITICAL", "HIGH", "MEDIUM"):
            self.assertTrue(meets_threshold(level))
        for level in ("LOW", "INFO", "ERROR"):
            self.assertFalse(meets_threshold(level))
        self.assertTrue(meets_threshold("LOW", "LOW"))
        self.assertTrue(meets_threshold("INFO", "INFO"))
        self.assertFalse(meets_threshold("HIGH", "CRITICAL"))

    async def test_new_event_reserves_before_send_and_records_success(self):
        async def send(message):
            rows = list(self.db["alert_log"].docs.values())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "SENDING")
            self.assertIn("[NEW]", message)
        self.sender.send.side_effect = send
        identity = await self.process(change())
        row = self.row(identity)
        self.assertEqual((row["event_type"], row["status"], row["attempt_count"]), ("NEW", "SENT", 1))
        self.assertEqual(row["sent_at"], NOW)
        self.assertEqual(row["document_id"], DOC_ID)
        self.assertIsNone(row["last_error_type"])

    async def test_low_and_info_are_suppressed_durably(self):
        for description, level, key in (("mentions samsung", "LOW", "b" * 64),
                                         ("synthetic overview", "INFO", "c" * 64)):
            identity = await self.process(change(doc=document(event_key=key, data_size="1 GB", description=description)))
            self.assertEqual(self.row(identity)["risk_level"], level)
            self.assertEqual(self.row(identity)["status"], "SUPPRESSED")
            self.assertEqual(self.row(identity)["suppression_reason"], "below_threshold")
        self.sender.send.assert_not_awaited()

    async def test_lowered_threshold_sends_info(self):
        self.service = AlertService(self.db, self.sender, AlertPolicy(min_level="INFO"), clock=lambda: NOW)
        identity = await self.process(change(doc=document(data_size="1 GB")))
        self.assertEqual(self.row(identity)["status"], "SENT")
        self.sender.send.assert_awaited_once()

    async def test_duplicate_new_even_with_different_resume_token_sends_once(self):
        identity = await self.process(change())
        await self.process(change(token="two"))
        self.assertEqual(len(self.db["alert_log"].docs), 1)
        self.assertEqual(self.row(identity)["attempt_count"], 1)
        self.sender.send.assert_awaited_once()

    async def test_duplicate_updated_sends_once(self):
        event = change("leak_history", history())
        identity = await self.process(event)
        await self.process(event)
        self.assertEqual(self.row(identity)["history_id"], "c" * 64)
        self.assertIn("[UPDATED]", self.row(identity)["message"])
        self.sender.send.assert_awaited_once()

    async def test_already_sent_and_suppressed_are_not_reclaimed(self):
        sent = await self.process(change())
        suppressed = await self.process(change(doc=document(event_key="d" * 64, data_size="1 GB")))
        for identity in (sent, suppressed):
            self.assertFalse(await self.service.deliver(identity))
        self.sender.send.assert_awaited_once()

    async def test_publication_date_only_is_non_material(self):
        item = history({"publication_date": {"before": "old", "after": "new"}})
        identity = await self.process(change("leak_history", item))
        self.assertEqual(self.row(identity)["suppression_reason"], "non_material_change")
        self.assertEqual(self.row(identity)["status"], "SUPPRESSED")
        self.sender.send.assert_not_awaited()

    async def test_source_url_only_is_non_material(self):
        item = history({"source_url": {"before": "https://old.example", "after": "https://new.example"}})
        identity = await self.process(change("leak_history", item))
        self.assertEqual(self.row(identity)["suppression_reason"], "non_material_change")
        self.sender.send.assert_not_awaited()

    async def test_material_and_date_change_sends_only_material_field_names(self):
        item = history({"publication_date": {"before": "old", "after": "new"},
                        "description": {"before": "before-value-not-for-message", "after": "updated overview"}})
        identity = await self.process(change("leak_history", item))
        self.assertEqual(self.row(identity)["changed_fields"], ["description"])
        message = self.row(identity)["message"]
        self.assertIn("■ 변경 필드: description", message)
        self.assertNotIn("publication_date", message)
        self.assertNotIn("before-value-not-for-message", message)
        self.sender.send.assert_awaited_once()

    async def test_history_after_overlay_classifies_new_value_without_current_write(self):
        self.db["leaked_data"].docs[DOC_ID] = document(data_size="1 GB")
        original = deepcopy(self.db["leaked_data"].docs)
        event = change("leak_history", history())
        original_event = deepcopy(event)
        identity = await self.process(event)
        self.assertEqual(self.row(identity)["risk_level"], "HIGH")
        self.assertIn("2 TB", self.row(identity)["message"])
        self.assertEqual(self.db["leaked_data"].docs, original)
        self.assertEqual(event, original_event)
        self.assertFalse(any(name == "leaked_data" for name, *_ in self.db.events))

    async def test_simple_observation_update_is_ignored(self):
        self.assertIsNone(await self.process(change(operationType="update")))
        self.assertEqual(self.db["alert_log"].docs, {})
        self.sender.send.assert_not_awaited()

    async def test_unrelated_collection_is_ignored(self):
        self.assertIsNone(await self.process(change("alert_log")))
        self.sender.send.assert_not_awaited()

    async def test_send_failure_records_only_type_and_retries_after_delay(self):
        self.sender.send.side_effect = [RuntimeError("sensitive-exception-detail"), None]
        identity = await self.process(change())
        self.assertEqual(self.row(identity)["status"], "FAILED")
        self.assertEqual(self.row(identity)["attempt_count"], 1)
        self.assertEqual(self.row(identity)["last_error_type"], "RuntimeError")
        self.assertNotIn("sensitive-exception-detail", repr(self.row(identity)))
        await self.service.retry_due()
        self.assertEqual(self.sender.send.await_count, 1)
        self.stamp += timedelta(seconds=60)
        await self.service.retry_due()
        row = self.row(identity)
        self.assertEqual((row["status"], row["attempt_count"]), ("SENT", 2))
        self.assertIsNone(row["last_error_type"])

    async def test_max_attempts_are_terminal(self):
        self.service = AlertService(self.db, self.sender, AlertPolicy(max_attempts=2), clock=lambda: self.stamp)
        self.sender.send.side_effect = RuntimeError("synthetic failure")
        identity = await self.process(change())
        self.stamp += timedelta(seconds=60)
        await self.service.retry_due()
        self.stamp += timedelta(seconds=3600)
        await self.service.retry_due()
        await self.process(change())
        self.assertEqual(self.sender.send.await_count, 2)
        self.assertEqual((self.row(identity)["status"], self.row(identity)["attempt_count"]), ("FAILED", 2))

    async def test_failed_atomic_claim_never_sends(self):
        identity = self.service.reserve(change())
        self.db["alert_log"].fail_claim = True
        self.assertFalse(await self.service.deliver(identity))
        self.assertEqual(self.row(identity)["attempt_count"], 0)
        self.sender.send.assert_not_awaited()

    async def test_two_workers_share_one_atomic_claim(self):
        identity = self.service.reserve(change())
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_send(message):
            started.set()
            await release.wait()
        self.sender.send.side_effect = slow_send
        first = asyncio.create_task(self.service.deliver(identity))
        await started.wait()
        second = AlertService(self.db, self.sender, clock=lambda: NOW)
        self.assertFalse(await second.deliver(identity))
        release.set()
        self.assertTrue(await first)
        self.sender.send.assert_awaited_once()

    async def test_expired_sending_lease_recovers_but_live_lease_does_not(self):
        identity = self.service.reserve(change())
        row = self.db["alert_log"].docs[identity]
        row.update(status="SENDING", attempt_count=1, lease_until=NOW + timedelta(seconds=120), claim_token="old")
        await self.service.retry_due()
        self.sender.send.assert_not_awaited()
        self.stamp += timedelta(seconds=121)
        await self.service.retry_due()
        self.assertEqual((self.row(identity)["status"], self.row(identity)["attempt_count"]), ("SENT", 2))

    async def test_expired_final_attempt_is_failed_without_send(self):
        identity = self.service.reserve(change())
        self.db["alert_log"].docs[identity].update(status="SENDING", attempt_count=5, lease_until=NOW, claim_token="old")
        await self.service.retry_due()
        self.assertEqual(self.row(identity)["status"], "FAILED")
        self.assertEqual(self.row(identity)["last_error_type"], "LeaseExpired")
        self.sender.send.assert_not_awaited()

    async def test_old_owner_cannot_overwrite_a_new_claim(self):
        identity = self.service.reserve(change())
        async def ownership_changed(message):
            self.db["alert_log"].docs[identity]["claim_token"] = "new-owner"
        self.sender.send.side_effect = ownership_changed
        self.assertFalse(await self.service.deliver(identity))
        self.assertEqual(self.row(identity)["status"], "SENDING")
        self.assertEqual(self.row(identity)["claim_token"], "new-owner")

    async def test_database_failure_after_send_keeps_lease_until_recovery(self):
        identity = self.service.reserve(change())
        async def send(message):
            self.db["alert_log"].fail_update = True
        self.sender.send.side_effect = send
        with self.assertRaises(PyMongoError):
            await self.service.deliver(identity)
        self.assertEqual(self.row(identity)["status"], "SENDING")
        self.assertFalse(await self.service.deliver(identity))
        self.sender.send.assert_awaited_once()

    async def test_success_dates_are_aware_utc(self):
        self.stamp = NOW.astimezone(timezone(timedelta(hours=9)))
        identity = await self.process(change())
        for key in ("created_at", "updated_at", "sent_at"):
            self.assertEqual(self.row(identity)[key].tzinfo, timezone.utc)

    async def test_allowlist_and_redaction_exclude_secrets_and_raw_html(self):
        token = "987654321:" + "x" * 35
        uri = "mongodb" + "://user:credential@unused/"
        self.service = AlertService(self.db, self.sender, clock=lambda: NOW, secrets=(token, uri))
        event = change(doc=document(company_name="Example <b>Corp</b>",
                                   data_contents="internal document " + token + " " + uri,
                                   raw_html="<html>never-store-this</html>", token=token, DB_URI=uri))
        identity = await self.process(event)
        saved = repr(self.row(identity))
        for forbidden in (token, uri, "raw_html", "never-store-this", "<b>", "DB_URI"):
            self.assertNotIn(forbidden, saved)
        self.assertIn("[redacted]", saved)

    def test_datetime_formats_kst_without_overwriting_aware_offset(self):
        plus_two = datetime(2026, 9, 10, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        message = format_telegram_message(document(first_seen=plus_two), "HIGH", "reason", "bitlock")
        self.assertIn("2026-09-10 19:00:00 KST", message)
        self.assertEqual(plus_two.utcoffset(), timedelta(hours=2))

    def test_legacy_naive_and_extended_json_dates(self):
        for value in (datetime(2026, 9, 10, 1), {"$date": "2026-09-10T01:00:00Z"}):
            self.assertEqual(utc_datetime(value), NOW)
        self.assertIsNone(utc_datetime({"$date": "bad"}))

    def test_new_message_contains_required_metadata_and_first_seen(self):
        data = document()
        message = format_telegram_message(data, "HIGH", "test reason", "bitlock")
        for part in ("[NEW]", "HIGH", "test reason", "Example Corp", "bitlock", "2 TB",
                     "synthetic overview", "ZZ", "https://company.example/", "2026-09-10 10:00:00 KST"):
            self.assertIn(part, message)

    def test_message_is_bounded_plain_text(self):
        data = document(company_name="😀" * 10000, data_contents="😀" * 10000)
        message = format_telegram_message(data, "HIGH", "x" * 10000, "actor")
        self.assertLessEqual(len(message.encode("utf-16-le")) // 2, 3900)

    def test_korean_hostname_with_path_query_and_port_is_critical(self):
        for url in ("https://demo.co.kr/path?q=1", "http://demo.kr:8080/", "DEMO.CO.KR/",
                    "https://demo.co.kr./path"):
            with self.subTest(url=url):
                self.assertEqual(classify_risk(document(data_size="1 GB", company_url=url))[0], "CRITICAL")

    def test_korean_domain_text_in_path_query_or_suffix_is_not_korean_host(self):
        for url in ("https://company.example/demo.co.kr", "https://demo.co.kr.example/",
                    "https://company.example/?redirect=demo.kr", "https://demo.kr@company.example/"):
            with self.subTest(url=url):
                self.assertEqual(classify_risk(document(data_size="1 GB", company_url=url))[0], "INFO")

    def test_risk_keeps_existing_heuristics_and_source_actor(self):
        for changes, expected in (({"data_contents": "customer data"}, "CRITICAL"),
                                  ({"company_name": "Government Demo"}, "CRITICAL"),
                                  ({"company_name": "Medical Demo"}, "CRITICAL"),
                                  ({"data_contents": "internal document"}, "HIGH"),
                                  ({"company_name": "Samsung Demo"}, "MEDIUM"),
                                  ({"description": "mentions samsung"}, "LOW"), ({}, "INFO")):
            self.assertEqual(classify_risk(document(data_size="1 GB", **changes))[0], expected)
        self.assertEqual(classify_risk({"_id": "black_shrantac" + "a" * 32})[2], "black_shrantac")

    def test_invalid_risk_input_is_safe_and_pure(self):
        self.assertEqual(classify_risk(None), ("ERROR", "Invalid data format", "unknown"))
        data = {"country": [], "company_url": {}, "data_contents": None}
        before = deepcopy(data)
        self.assertEqual(classify_risk(data)[0], "INFO")
        self.assertEqual(data, before)

    def test_entry_configuration_defaults_and_secrets_hidden_in_repr(self):
        values = {"DB_URI": "mongodb://unused", "DB_NAME": "synthetic",
                  "TELEGRAM_TOKEN": "synthetic-token", "TELEGRAM_CHAT_ID": "-10099"}
        with patch("dotenv.load_dotenv", side_effect=AssertionError("Unexpected env load")):
            config = entry.load_config(values)
        self.assertEqual(config.policy, AlertPolicy())
        self.assertEqual(config.chat_id, -10099)
        for value in values.values():
            self.assertNotIn(value, repr(config))

    def test_alert_config_rejects_invalid_level_and_positive_integer_values(self):
        for name, value in (("ALERT_MIN_LEVEL", "WRONG"), ("ALERT_MAX_ATTEMPTS", "0"),
                            ("ALERT_RETRY_SECONDS", "-1"), ("ALERT_LEASE_SECONDS", "1.5"),
                            ("ALERT_MAX_ATTEMPTS", ""), ("ALERT_MIN_LEVEL", "medium")):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ValueError, name):
                    AlertPolicy.from_mapping({name: value})

    def test_runtime_config_is_injected_offline_and_validates_alert_settings(self):
        values = {"DB_URI": "mongodb://127.0.0.1", "DB_NAME": "synthetic",
                  "TELEGRAM_TOKEN": "synthetic-token", "TELEGRAM_CHAT_ID": "-10099",
                  "ELASTICSEARCH_URL": "http://localhost:9200", "KIBANA_URL": "http://localhost:5601"}
        with patch("scripts.check_runtime_config.load_dotenv", side_effect=AssertionError("env read")):
            self.assertEqual(validate_config(values), [])
            for name in ("ALERT_MAX_ATTEMPTS", "ALERT_RETRY_SECONDS", "ALERT_LEASE_SECONDS"):
                for value in ("0", "-1", "x", "1.5", ""):
                    self.assertTrue(any(name in error for error in validate_config({**values, name: value})))
            self.assertTrue(any("ALERT_MIN_LEVEL" in error for error in validate_config({**values, "ALERT_MIN_LEVEL": "BAD"})))
            self.assertEqual(validate_config({**values, "ALERT_MIN_LEVEL": "INFO", "ALERT_MAX_ATTEMPTS": "2"}), [])

    def test_runtime_existing_production_checks_remain(self):
        errors = validate_config({"DJANGO_DEBUG": "False"})
        self.assertTrue(any("DB_URI" in error for error in errors))
        self.assertTrue(any("DJANGO_SECRET_KEY" in error for error in errors))
        self.assertTrue(any("DJANGO_ALLOWED_HOSTS" in error for error in errors))

    def test_main_configuration_error_does_not_start_service_or_print_values(self):
        with patch.object(entry, "load_config", side_effect=ValueError("ALERT_MIN_LEVEL invalid")), \
                patch.object(entry, "run") as run, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(entry.main(), 2)
        self.assertIn("ALERT_MIN_LEVEL", output.getvalue())
        run.assert_not_called()

    async def test_legacy_insert_reuses_day9_identity_without_mutation(self):
        data = document()
        data.pop("event_key")
        before = deepcopy(data)
        event = convert_change(change(doc=data), self.db["leaked_data"])
        from crawling.storage import event_identity
        self.assertEqual(event["event_key"], event_identity(data)[0])
        self.assertEqual(data, before)

    async def test_malformed_event_is_quarantined_without_raw_payload(self):
        event = change(fullDocument={"raw_html": "never-store-this"})
        identity = await self.process(event)
        self.assertEqual(self.row(identity)["suppression_reason"], "invalid_event")
        self.assertNotIn("never-store-this", repr(self.row(identity)))
        self.sender.send.assert_not_awaited()

    async def test_missing_current_history_is_durably_suppressed(self):
        event = change("leak_history", history(document_id="missing"))
        identity = await self.process(event)
        self.assertEqual(self.row(identity)["suppression_reason"], "invalid_event")
        self.sender.send.assert_not_awaited()

    async def test_duplicate_reservation_race_keeps_existing_terminal_row(self):
        identity = await self.process(change())
        before = self.row(identity)
        del self.db["alert_log"].docs[identity]
        def competing_insert(*args, **kwargs):
            self.db["alert_log"].docs[identity] = deepcopy(before)
            raise DuplicateKeyError("synthetic")
        with patch.object(self.db["alert_log"], "update_one", side_effect=competing_insert):
            self.assertEqual(self.service.reserve(change()), identity)
        self.assertEqual(self.row(identity), before)

    async def test_sent_history_replay_does_not_require_current_document(self):
        event = change("leak_history", history())
        identity = await self.process(event)
        del self.db["leaked_data"].docs[DOC_ID]
        self.assertEqual(await self.process(event), identity)
        self.assertEqual(len(self.db["alert_log"].docs), 1)
        self.sender.send.assert_awaited_once()

    async def test_inflight_timeout_fails_before_lease_expiry(self):
        self.service = AlertService(self.db, self.sender, AlertPolicy(lease_seconds=1), clock=lambda: NOW)
        async def blocked(message):
            await asyncio.Event().wait()
        self.sender.send.side_effect = blocked
        identity = await self.process(change())
        self.assertEqual(self.row(identity)["status"], "FAILED")
        self.assertEqual(self.row(identity)["last_error_type"], "TimeoutError")
        self.assertEqual(self.row(identity)["attempt_count"], 1)

    async def test_watcher_database_scope_checkpoint_order_and_client_cleanup(self):
        stream = Stream(self.db, [change()])
        self.db.streams = [stream]
        await self.watch()
        self.assertTrue(stream.closed)
        self.assertTrue(self.clients[0].closed)
        self.assertTrue(self.client_options["tz_aware"])
        self.assertEqual(self.client_options["tzinfo"], timezone.utc)
        self.assertEqual(self.client_options["w"], "majority")
        pipeline, options = self.db.watch_calls[0]
        self.assertEqual(pipeline[0]["$match"]["ns.coll"]["$in"], ["leaked_data", "leak_history"])
        self.assertNotIn("resume_after", options)
        operations = [name for name, *_ in self.db.events]
        self.assertLess(operations.index("alert_log"), operations.index("alert_state"))
        self.assertEqual(self.db["alert_state"].docs[STATE_ID]["resume_token"], {"_data": "one"})

    async def test_watcher_uses_saved_resume_token(self):
        self.db["alert_state"].docs[STATE_ID] = {"_id": STATE_ID, "resume_token": {"_data": "saved"}}
        self.db.streams = [Stream(self.db, [])]
        await self.watch()
        self.assertEqual(self.db.watch_calls[0][1]["resume_after"], {"_data": "saved"})
        self.sender.send.assert_not_awaited()

    async def test_no_resume_token_does_not_backfill_historical_current_docs(self):
        self.db.streams = [Stream(self.db, [])]
        await self.watch()
        self.assertEqual(self.db["alert_log"].docs, {})
        self.sender.send.assert_not_awaited()

    async def test_invalid_resume_token_clears_and_reopens_without_backfill(self):
        self.db["alert_state"].docs[STATE_ID] = {"_id": STATE_ID, "resume_token": {"_data": "expired"}}
        stream = Stream(self.db, [])
        self.db.streams = [OperationFailure("sensitive-detail", code=286), stream]
        with self.assertLogs("alert.service", level="ERROR") as logs:
            await self.watch()
        self.assertNotIn("sensitive-detail", repr(logs.output))
        self.assertNotIn("resume_after", self.db.watch_calls[1][1])
        self.assertEqual(self.db["alert_state"].docs[STATE_ID]["last_error_type"], "InvalidResumeToken")
        self.assertTrue(all(client.closed for client in self.clients))
        self.sender.send.assert_not_awaited()

    async def test_bad_token_shape_is_cleared_without_connection_crash(self):
        self.db["alert_state"].docs[STATE_ID] = {"_id": STATE_ID, "resume_token": "invalid"}
        self.db.streams = [Stream(self.db, [])]
        await self.watch()
        self.assertNotIn("resume_after", self.db.watch_calls[0][1])
        self.assertNotIn("resume_token", self.db["alert_state"].docs[STATE_ID])

    async def test_reservation_failure_never_advances_checkpoint(self):
        stream = Stream(self.db, [change()])
        self.db.streams = [stream]
        self.db["alert_log"].fail_update = True
        async def stop(delay):
            self.assertTrue(stream.closed)
            self.assertTrue(self.clients[0].closed)
            self.db.stopped = True
        await self.watch(sleep=stop)
        self.assertEqual(self.db["alert_state"].docs, {})
        self.sender.send.assert_not_awaited()

    async def test_checkpoint_failure_leaves_pending_for_restart_retry(self):
        first, second = Stream(self.db, [change()]), Stream(self.db, [])
        self.db.streams = [first, second]
        self.db["alert_state"].fail_update = True
        await self.watch()
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(next(iter(self.db["alert_log"].docs.values()))["status"], "SENT")
        self.sender.send.assert_awaited_once()

    async def test_idle_stream_drains_due_failed_reservations(self):
        self.sender.send.side_effect = [RuntimeError("synthetic"), None]
        identity = await self.process(change())
        self.stamp += timedelta(seconds=61)
        self.db.streams = [Stream(self.db, [])]
        await self.watch()
        self.assertEqual(self.row(identity)["status"], "SENT")
        self.assertEqual(self.sender.send.await_count, 2)

    async def test_send_failure_does_not_stop_later_event(self):
        self.sender.send.side_effect = [RuntimeError("synthetic"), None]
        self.db.streams = [Stream(self.db, [change(), change(doc=document(event_key="d" * 64), token="two")])]
        await self.watch()
        self.assertEqual(sorted(row["status"] for row in self.db["alert_log"].docs.values()), ["FAILED", "SENT"])

    async def test_keyboard_interrupt_closes_stream_and_client(self):
        stream = Stream(self.db, [KeyboardInterrupt()])
        self.db.streams = [stream]
        await self.watch()
        self.assertTrue(stream.closed)
        self.assertTrue(self.clients[0].closed)

    async def test_client_closes_when_database_selection_fails(self):
        class BrokenClient(Client):
            def __getitem__(self, name):
                raise PyMongoError("synthetic")
        client = BrokenClient(self.db)
        async def stop(delay):
            self.assertTrue(client.closed)
            self.db.stopped = True
        await watch_forever("mongodb://unused", "synthetic", self.sender, client_factory=lambda *a, **kw: client,
                            stop=lambda: self.db.stopped, sleep=stop)
        self.assertTrue(client.closed)

    async def test_bot_is_reused_and_closed_once(self):
        bot = SimpleNamespace(initialize=AsyncMock(), shutdown=AsyncMock(), send_message=AsyncMock())
        factory = Mock(return_value=bot)
        async with TelegramSender("synthetic-token", -10099, bot_factory=factory) as sender:
            await sender.send("one")
            await sender.send("two")
        factory.assert_called_once()
        bot.initialize.assert_awaited_once()
        bot.shutdown.assert_awaited_once()
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertIsNone(bot.send_message.call_args.kwargs["parse_mode"])
        self.assertTrue(bot.send_message.call_args.kwargs["disable_web_page_preview"])

    async def test_bot_send_failure_propagates_and_cleanup_runs(self):
        bot = SimpleNamespace(initialize=AsyncMock(), shutdown=AsyncMock(),
                              send_message=AsyncMock(side_effect=RuntimeError("synthetic")))
        with self.assertRaises(RuntimeError):
            async with TelegramSender("synthetic", -10099, bot_factory=lambda **kw: bot) as sender:
                await sender.send("one")
        bot.shutdown.assert_awaited_once()

    async def test_bot_initialization_failure_runs_cleanup(self):
        bot = SimpleNamespace(initialize=AsyncMock(side_effect=RuntimeError("synthetic")), shutdown=AsyncMock())
        with self.assertRaises(RuntimeError):
            async with TelegramSender("synthetic", -10099, bot_factory=lambda **kw: bot):
                self.fail("Initialization should fail")
        bot.shutdown.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
