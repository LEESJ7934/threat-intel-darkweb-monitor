"""Day13 controls against synthetic dictionaries/fakes; no external services."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from crawling import models, storage
from governance import audit, elk_check, policy, retention, runtime, sources
from elk.config import Config as ElkConfig, ElkError
from scripts import apply_retention, check_governance, check_runtime_config
from tests.test_storage import MemoryCollection, observation

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
RULES = policy.retention_rules({})
ROOT = Path(__file__).resolve().parents[1]
MISSING = object()


def get_path(doc, path):
    for part in path.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return MISSING
        doc = doc[part]
    return doc


def mongo_type(value):
    if value is MISSING: return "missing"
    if value is None: return "null"
    if isinstance(value, datetime): return "date"
    if isinstance(value, str): return "string"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    if type(value) is bool: return "bool"
    if type(value) is int: return "int"
    return "double"


def evaluate(expr, doc, variables=None):
    """Small expression interpreter; no policy decisions or cutoff calculation."""
    variables = {} if variables is None else variables
    if isinstance(expr, str):
        if expr == "$$ROOT": return doc
        if expr.startswith("$$"):
            head, _, tail = expr[2:].partition(".")
            value = variables[head]
            return get_path(value, tail) if tail else value
        return get_path(doc, expr[1:]) if expr.startswith("$") else expr
    if isinstance(expr, list):
        return [evaluate(value, doc, variables) for value in expr]
    if not isinstance(expr, dict):
        return expr
    if len(expr) != 1: raise AssertionError("Unsupported expression shape")
    op, arg = next(iter(expr.items()))
    if op == "$type": return mongo_type(evaluate(arg, doc, variables))
    if op == "$cond":
        test, yes, no = arg
        return evaluate(yes if evaluate(test, doc, variables) else no, doc, variables)
    if op == "$map":
        return [evaluate(arg["in"], doc, {**variables, arg["as"]: item})
                for item in evaluate(arg["input"], doc, variables)]
    if op == "$objectToArray":
        return [{"k": key, "v": value} for key, value in evaluate(arg, doc, variables).items()]
    if op == "$toLower": return evaluate(arg, doc, variables).lower()
    if op == "$size": return len(evaluate(arg, doc, variables))
    args = evaluate(arg, doc, variables)
    if op == "$and": return all(args)
    if op == "$or": return any(args)
    if op == "$not": return not args[0]
    if op == "$in": return args[0] in args[1]
    if op == "$eq": return args[0] == args[1]
    if op == "$setIntersection": return set(args[0]) & set(args[1])
    if op in {"$lt", "$gt"}:
        left, right = args
        # Date-vs-invalid values needn't model BSON sort order because the
        # separate scalar-type guard must reject them whichever order is used.
        if mongo_type(left) != mongo_type(right): return False
        return left < right if op == "$lt" else left > right
    raise AssertionError("Unsupported expression operator")


class CountDeleteCollection:
    def __init__(self, docs=()):
        self.docs, self.calls, self.fail_delete = deepcopy(list(docs)), [], False

    def __getattr__(self, name):
        raise AssertionError("Unapproved Mongo operation: " + name)

    def count_documents(self, query, **kwargs):
        if kwargs != {"maxTimeMS": 5000}: raise AssertionError("Unbounded read")
        self.calls.append(("count", deepcopy(query)))
        return sum(evaluate(query["$expr"], doc) for doc in self.docs)

    def delete_many(self, query):
        self.calls.append(("delete_many", deepcopy(query)))
        if self.fail_delete: raise RuntimeError("synthetic private database exception")
        survivors = [doc for doc in self.docs if not evaluate(query["$expr"], doc)]
        count = len(self.docs) - len(survivors)
        self.docs = survivors
        return SimpleNamespace(deleted_count=count)


class FakeDatabase:
    name = "day13_governance_e2e"

    def __init__(self, **rows):
        self.collections = {name: CountDeleteCollection(rows.get(name, [])) for name in policy.COLLECTIONS}

    def __getitem__(self, name):
        if name not in self.collections: raise AssertionError("Unapproved collection")
        return self.collections[name]


class OfflineCase(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.getaddrinfo", "pymongo.MongoClient"):
            self.enterContext(patch(target, side_effect=AssertionError("External service forbidden")))
        self.audit = self.enterContext(patch.object(audit, "emit", wraps=lambda *a, **k: None))


class MetadataPolicyTests(OfflineCase):
    def test_allowlist_is_same_object_as_crawler_and_limits_cover_it(self):
        self.assertIs(policy.METADATA_FIELDS, models.METADATA_FIELDS)
        self.assertEqual(set(policy.TEXT_LIMITS), set(models.METADATA_FIELDS))

    def test_normal_metadata_unchanged(self):
        row = observation()
        self.assertEqual(policy.bounded_metadata(row), models.normalized_metadata(row))

    def test_non_identity_metadata_has_exact_bounds(self):
        for field, limit in policy.TEXT_LIMITS.items():
            if field in {"company_name", "company_url"}: continue
            value = ("https://source.example/" + "a" * limit) if field == "source_url" else "가" * (limit + 50)
            bounded = policy.bounded_metadata(observation(**{field: value}))
            self.assertEqual(len(bounded[field]), limit)

    def test_overlong_company_identity_is_rejected_without_any_storage_write(self):
        for field, value in (("company_name", "x" * 513), ("company_url", "https://company.example/" + "x" * 2048)):
            current, history = MemoryCollection(), MemoryCollection()
            with self.assertRaises(policy.PolicyError):
                storage.save_record(current, history, observation(**{field: value}))
            self.assertFalse(current.events)
            self.assertFalse(history.events)

    def test_source_bound_is_enforced(self):
        self.assertEqual(policy.bounded_source(" Bitlock "), "bitlock")
        with self.assertRaises(policy.PolicyError): policy.bounded_source("a" * 129)

    def test_parser_identity_is_not_recomputed_from_capped_text(self):
        raw = observation(description="A" * 9000)
        raw["_id"] = models.legacy_id("bitlock_", raw["company_name"], raw["description"])
        current, history = MemoryCollection(), MemoryCollection()
        storage.save_record(current, history, raw)
        saved = current.documents[raw["_id"]]
        self.assertEqual(saved["_id"], raw["_id"])
        self.assertEqual(len(saved["description"]), 8000)
        self.assertNotEqual(raw["_id"], models.legacy_id("bitlock_", raw["company_name"], saved["description"]))

    def test_normalization_idempotent_and_event_identity_deterministic(self):
        raw = observation(description=" x " * 5000)
        clean = {**policy.bounded_metadata(raw), "source": "bitlock", "_id": raw["_id"]}
        self.assertEqual(policy.bounded_metadata(clean), policy.bounded_metadata(raw))
        self.assertEqual(storage.event_identity(clean), storage.event_identity(deepcopy(clean)))
        self.assertEqual(storage.event_identity(raw), storage.event_identity(clean))

    def test_raw_html_extra_fields_and_forged_identity_are_not_persisted(self):
        current, history = MemoryCollection(), MemoryCollection()
        storage.save_record(current, history, observation(raw_html="<html>not data</html>", password="synthetic",
                                                          claim_token="synthetic", event_key="forged"))
        row = next(iter(current.documents.values()))
        self.assertFalse(set(row) & policy.PROHIBITED_FIELDS)
        self.assertNotIn("claim_token", row)
        self.assertNotEqual(row["event_key"], "forged")

    def test_mongodb_uri_redacted(self):
        secret = "mongodb" + "://demo:synthetic@host.invalid/db"
        self.assertEqual(policy.redact_credentials("See " + secret), "See [redacted]")

    def test_telegram_token_redacted(self):
        token = "123456789:" + "Z" * 36
        self.assertEqual(policy.redact_credentials(token), "[redacted]")

    def test_embedded_credentials_in_http_and_ftp_are_redacted(self):
        for scheme in ("https", "http", "ftp"):
            self.assertEqual(policy.redact_credentials(scheme + "://user:synthetic@host.invalid/file"), "[redacted]")

    def test_assignments_include_quoted_values_and_json_keys(self):
        for name in ("password", "passwd", "token", "api_key", "api-key", "secret"):
            for raw in (name + "=private", name + ': "two words"', '"' + name + '": "two words"'):
                clean = policy.redact_credentials(raw)
                self.assertNotIn("private", clean)
                self.assertNotIn("two words", clean)

    def test_private_key_block_and_unclosed_block_redacted(self):
        header = "-----BEGIN " + "PRIVATE KEY-----"
        end = "-----END " + "PRIVATE KEY-----"
        for raw in (header + "\nSYNTHETIC_BODY\n" + end, header + "\nSYNTHETIC_BODY"):
            self.assertEqual(policy.redact_credentials(raw), "[redacted]")

    def test_normal_names_urls_and_security_terms_not_overredacted(self):
        text = "Example Company https://company.example/about country ZZ password policy token-based service"
        self.assertEqual(policy.redact_credentials(text), text)

    def test_explicit_configured_secrets_are_hidden(self):
        self.assertEqual(policy.redact_credentials("alpha secret material", secrets=("secret material",)), "alpha [redacted]")

    def test_redaction_happens_before_truncation_at_token_boundary(self):
        token = "123456789:" + "Z" * 36
        clean = policy.bounded_metadata(observation(description="a" * 7990 + " " + token))
        self.assertNotIn("123456789", clean["description"])
        self.assertLessEqual(len(clean["description"]), 8000)

    def test_saved_history_does_not_copy_legacy_credentials(self):
        current, history = MemoryCollection(), MemoryCollection()
        raw = observation(description="password=synthetic-old")
        current.documents[raw["_id"]] = deepcopy(raw)
        storage.save_record(current, history, observation(description="Clean new overview"))
        self.assertEqual(len(history.documents), 1)
        self.assertNotIn("synthetic-old", repr(history.documents))
        self.assertNotIn("synthetic-old", repr(current.documents))

    def test_input_dictionary_not_mutated(self):
        row = observation(description="password=synthetic", extra={"raw": "ignored"})
        original = deepcopy(row)
        policy.bounded_metadata(row)
        self.assertEqual(row, original)

    def test_risk_safe_text_uses_same_credential_policy(self):
        from alert.risk import safe_text
        self.assertNotIn("private", safe_text('token="private words"'))
        self.assertEqual(safe_text("Example Company"), "Example Company")

    def test_unknown_merge_and_repeated_capped_observation_still_dedupe(self):
        current, history = MemoryCollection(), MemoryCollection()
        raw = observation(description="a" * 9000)
        storage.save_record(current, history, raw)
        storage.save_record(current, history, {**raw, "_id": "different-parser-id", "data_size": "unknown"})
        self.assertEqual(len(current.documents), 1)
        saved = next(iter(current.documents.values()))
        self.assertEqual(saved["data_size"], "10 GB")
        self.assertEqual(saved["observation_count"], 2)
        self.assertEqual(len(history.documents), 0)


class RetentionTests(OfflineCase):
    def report(self, **rows):
        return retention.inspect_retention(FakeDatabase(**rows), RULES, now=NOW)

    def test_event_cutoff_is_exclusive_and_last_seen_preferred(self):
        edge = NOW - timedelta(days=365)
        rows = [{"last_seen": edge - timedelta(milliseconds=1)}, {"last_seen": edge}, {"last_seen": NOW},
                {"last_seen": NOW, "scraped_time": OLD}]
        self.assertEqual(self.report(leaked_data=rows)["leaked_data"]["expired"], 1)

    def test_event_fallback_for_missing_and_null_primary_only(self):
        rows = [{"scraped_time": OLD}, {"last_seen": None, "scraped_time": OLD},
                {"last_seen": "invalid", "scraped_time": OLD}]
        counts = self.report(leaked_data=rows)["leaked_data"]
        self.assertEqual(counts["expired"], 2)
        self.assertEqual(counts["invalid"], 1)

    def test_history_uses_365_day_changed_at(self):
        rows = [{"changed_at": OLD}, {"changed_at": NOW - timedelta(days=365)}, {"scraped_time": OLD}]
        counts = self.report(leak_history=rows)["leak_history"]
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(counts["missing"], 1)

    def test_alert_uses_180_days_updated_at_then_created_at(self):
        rows = [{"updated_at": NOW - timedelta(days=181)}, {"created_at": NOW - timedelta(days=181)},
                {"updated_at": NOW, "created_at": OLD}, {"updated_at": NOW - timedelta(days=180)}]
        self.assertEqual(self.report(alert_log=rows)["alert_log"]["expired"], 2)

    def test_missing_and_invalid_timestamps_skipped_in_all_collections(self):
        values = [None, "2020-01-01T00:00:00Z", "not-a-time", 0, True, [OLD], {"$date": OLD}]
        for rule in RULES:
            rows = [{}] + [{rule.primary: value} for value in values]
            counts = self.report(**{rule.collection: rows})[rule.collection]
            self.assertEqual(counts["expired"], 0)
            self.assertEqual(counts["missing"], 2)
            self.assertEqual(counts["invalid"], 6)

    def test_future_preferred_date_never_uses_old_fallback(self):
        rows = [{"last_seen": NOW + timedelta(days=1), "scraped_time": OLD}]
        counts = self.report(leaked_data=rows)["leaked_data"]
        self.assertEqual(counts["expired"], 0)
        self.assertEqual(counts["future"], 1)

    def test_invalid_or_future_fallback_also_protects_document_with_old_primary(self):
        rows = [{"last_seen": OLD, "scraped_time": "invalid"},
                {"last_seen": OLD, "scraped_time": NOW + timedelta(days=1)}]
        counts = self.report(leaked_data=rows)["leaked_data"]
        self.assertEqual(counts["expired"], 0)
        self.assertEqual(counts["invalid"], 1)
        self.assertEqual(counts["future"], 1)

    def test_cutoffs_aware_utc_across_offsets(self):
        local = NOW.astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(retention.queries(RULES[0], local), retention.queries(RULES[0], NOW))
        stamp = policy.cutoff(RULES[0], local)
        self.assertIs(stamp.tzinfo, timezone.utc)
        with self.assertRaises(policy.PolicyError): policy.cutoff(RULES[0], NOW.replace(tzinfo=None))

    def test_configured_days_and_invalid_values(self):
        rules = policy.retention_rules({"GOV_EVENT_RETENTION_DAYS": "30", "GOV_ALERT_RETENTION_DAYS": "60"})
        self.assertEqual([rule.days for rule in rules], [30, 365, 60])
        for raw in ("0", "-1", "x", "1.5", "", "１２", "9" * 5000):
            with self.assertRaises(policy.PolicyError):
                policy.retention_rules({"GOV_EVENT_RETENTION_DAYS": raw})

    def test_very_long_retention_cannot_overflow_to_a_deleting_query(self):
        rule = replace(RULES[0], days=9_999_999)
        self.assertEqual(policy.cutoff(rule, NOW), datetime.min.replace(tzinfo=timezone.utc))

    def test_dry_run_never_deletes(self):
        db = FakeDatabase(leaked_data=[{"last_seen": OLD}])
        result = retention.execute_retention(db, RULES, now=NOW)
        self.assertEqual(result["leaked_data"]["expired"], 1)
        self.assertEqual(len(db["leaked_data"].docs), 1)
        self.assertTrue(all(call[0] == "count" for c in db.collections.values() for call in c.calls))

    def test_apply_confirmation_missing_or_mismatched_prevents_all_database_calls(self):
        for requested, confirmed in ((None, None), (FakeDatabase.name, None), (FakeDatabase.name, "other"),
                                     ("other", "other"), (" " + FakeDatabase.name, FakeDatabase.name)):
            db = FakeDatabase()
            with self.assertRaises(policy.PolicyError):
                retention.execute_retention(db, RULES, apply=True, configured_name=db.name,
                                            database_name=requested, confirmation=confirmed, now=NOW)
            self.assertTrue(all(not c.calls for c in db.collections.values()))

    def test_protected_database_name_is_rejected(self):
        for name in ("monstache", "config", "local", "admin"):
            with self.assertRaises(policy.PolicyError): retention.confirm_apply(name, name, name)

    def test_connected_database_mismatch_rejected(self):
        with self.assertRaises(policy.PolicyError):
            retention.execute_retention(FakeDatabase(), RULES, apply=True, configured_name="other",
                                        database_name="other", confirmation="other", now=NOW)

    def test_only_whitelisted_collections_and_time_fields_are_accepted(self):
        for bad in (replace(RULES[0], collection="alert_state"), replace(RULES[0], primary="created_at"),
                    replace(RULES[0], days=0)):
            db = FakeDatabase()
            with self.assertRaises(policy.PolicyError):
                retention.execute_retention(db, (bad, *RULES[1:]), now=NOW)
            self.assertTrue(all(not c.calls for c in db.collections.values()))

    def test_apply_deletes_only_old_documents_and_each_collection_has_own_query(self):
        db = FakeDatabase(leaked_data=[{"_id": "e1", "last_seen": OLD}, {"_id": "e2", "last_seen": NOW}],
                          leak_history=[{"_id": "h1", "changed_at": OLD}, {"_id": "h2", "changed_at": NOW}],
                          alert_log=[{"_id": "a1", "updated_at": NOW - timedelta(days=181)},
                                     {"_id": "a2", "updated_at": NOW}])
        result = retention.execute_retention(db, RULES, apply=True, configured_name=db.name,
                                            database_name=db.name, confirmation=db.name, now=NOW)
        for name, survivor in (("leaked_data", "e2"), ("leak_history", "h2"), ("alert_log", "a2")):
            self.assertEqual([doc["_id"] for doc in db[name].docs], [survivor])
            self.assertEqual(result[name]["deleted"], 1)
            queries = [q for method, q in db[name].calls if method == "delete_many"]
            self.assertEqual(len(queries), 1)
        self.assertNotEqual(db["leaked_data"].calls[-1][1], db["leak_history"].calls[-1][1])
        self.assertNotEqual(db["alert_log"].calls[-1][1], db["leak_history"].calls[-1][1])

    def test_no_cascade_from_expired_event_to_recent_history_or_alert(self):
        db = FakeDatabase(leaked_data=[{"_id": "event", "last_seen": OLD}],
                          leak_history=[{"document_id": "event", "changed_at": NOW}],
                          alert_log=[{"document_id": "event", "updated_at": NOW}])
        result = retention.execute_retention(db, RULES, apply=True, configured_name=db.name,
                                            database_name=db.name, confirmation=db.name, now=NOW)
        self.assertEqual([result[name]["deleted"] for name in policy.COLLECTIONS], [1, 0, 0])

    def test_concurrent_recent_update_survives_delete_predicate(self):
        db = FakeDatabase(leaked_data=[{"last_seen": OLD}])
        original = db["leaked_data"].delete_many
        def changed_before_delete(query):
            db["leaked_data"].docs[0]["last_seen"] = NOW
            return original(query)
        db["leaked_data"].delete_many = changed_before_delete
        result = retention.execute_retention(db, RULES, apply=True, configured_name=db.name,
                                            database_name=db.name, confirmation=db.name, now=NOW)
        self.assertEqual(result["leaked_data"]["expired"], 1)
        self.assertEqual(result["leaked_data"]["deleted"], 0)

    def test_partial_delete_failure_audited_without_raw_exception(self):
        db = FakeDatabase(leaked_data=[{"last_seen": OLD}], leak_history=[{"changed_at": OLD}])
        db["leak_history"].fail_delete = True
        with self.assertRaises(RuntimeError):
            retention.execute_retention(db, RULES, apply=True, configured_name=db.name,
                                        database_name=db.name, confirmation=db.name, now=NOW)
        self.assertEqual(len(db["leaked_data"].docs), 0)
        self.assertEqual(len(db["leak_history"].docs), 1)
        self.assertEqual(self.audit.call_args.kwargs["result"], "error")
        self.assertNotIn("synthetic private", repr(self.audit.call_args_list))

    def test_count_failure_prevents_any_deletion(self):
        db = FakeDatabase(leaked_data=[{"last_seen": OLD}])
        db["leak_history"].count_documents = Mock(side_effect=RuntimeError("synthetic read failure"))
        with self.assertRaises(RuntimeError):
            retention.execute_retention(db, RULES, apply=True, configured_name=db.name,
                                        database_name=db.name, confirmation=db.name, now=NOW)
        self.assertTrue(all(method != "delete_many" for c in db.collections.values() for method, _ in c.calls))


class CheckerAndCLITests(OfflineCase):
    def cli(self, module, args, database=None):
        db = database or FakeDatabase()
        @contextmanager
        def connect(*a, **k):
            try: yield db
            finally: db.closed = True
        with patch.object(module, "load_config", return_value=runtime.Config(db.name, RULES)), \
                patch.object(module, "mongo_uri", return_value="mongodb://unused"), \
                patch.object(module, "mongo_database", side_effect=connect) as mongo, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            result = module.main(args)
        return result, output.getvalue(), mongo, db

    def test_readonly_checker_reports_counts_and_no_bodies_or_ids(self):
        db = FakeDatabase(leaked_data=[{"_id": "PRIVATE-DOCUMENT-ID", "description": "PRIVATE BODY", "last_seen": OLD}])
        code, output, _, _ = self.cli(check_governance, [], db)
        self.assertEqual(code, 0)
        self.assertIn("expired=1", output)
        self.assertNotIn("PRIVATE", output)
        self.assertIn("DAY13_GOVERNANCE_CHECK: PASS", output)
        self.assertTrue(all(method == "count" for c in db.collections.values() for method, _ in c.calls))

    def test_checker_detects_prohibited_field_even_with_null_or_mixed_case(self):
        for field in ("raw_html", "PASSWORD", "Db_URI", "session_id"):
            code, output, _, _ = self.cli(check_governance, [], FakeDatabase(leaked_data=[{field: None}]))
            self.assertEqual(code, 1)
            self.assertIn("prohibited_top_level_documents=1", output)

    def test_alert_internal_fields_allowed_only_in_mongo_alerts(self):
        for collection in policy.COLLECTIONS:
            row = {"message": "synthetic", "claim_token": "synthetic"}
            code, _, _, _ = self.cli(check_governance, [], FakeDatabase(**{collection: [row]}))
            self.assertEqual(code, 0 if collection == "alert_log" else 1)
        self.assertTrue(policy.PRIVATE_ANALYTICS_FIELDS <= policy.prohibited_fields("alert_log", analytics=True))

    def test_resume_token_belongs_in_exempt_alert_state_not_threat_collections(self):
        code, _, _, _ = self.cli(check_governance, [], FakeDatabase(alert_log=[{"resume_token": {"x": 1}}]))
        self.assertEqual(code, 1)

    def test_message_is_not_exemption_for_other_prohibited_alert_fields(self):
        code, _, _, _ = self.cli(check_governance, [], FakeDatabase(alert_log=[{"message": "synthetic", "password": "synthetic"}]))
        self.assertEqual(code, 1)

    def test_checker_is_top_level_only_and_documents_this_limitation(self):
        code, _, _, _ = self.cli(check_governance, [], FakeDatabase(leaked_data=[{"nested": {"password": "synthetic"}}]))
        self.assertEqual(code, 0)

    def test_default_apply_cli_is_dry_run_even_when_names_provided(self):
        name = FakeDatabase.name
        for args in ([], ["--database", name, "--confirm-database", name]):
            db = FakeDatabase(leaked_data=[{"last_seen": OLD}])
            code, output, _, _ = self.cli(apply_retention, args, db)
            self.assertEqual(code, 0)
            self.assertIn("DRY RUN", output)
            self.assertTrue(all(method != "delete_many" for c in db.collections.values() for method, _ in c.calls))

    def test_apply_cli_confirmation_failure_precedes_mongo_open(self):
        code, output, mongo, _ = self.cli(apply_retention, ["--apply", "--database", FakeDatabase.name,
                                                            "--confirm-database", "wrong"])
        self.assertEqual(code, 1)
        mongo.assert_not_called()
        self.assertNotIn("wrong", output)

    def test_apply_cli_explicit_confirmation_uses_only_document_deletes(self):
        name = FakeDatabase.name
        code, output, _, db = self.cli(apply_retention, ["--apply", "--database", name, "--confirm-database", name],
                                      FakeDatabase(leaked_data=[{"last_seen": OLD}]))
        self.assertEqual(code, 0)
        self.assertIn("deleted=1", output)
        self.assertTrue(db.closed)

    def test_apply_database_failure_closes_client_and_hides_exception(self):
        db = FakeDatabase(leaked_data=[{"last_seen": OLD}])
        db["leaked_data"].fail_delete = True
        code, output, _, db = self.cli(apply_retention, ["--apply", "--database", db.name,
                                                        "--confirm-database", db.name], db)
        self.assertEqual(code, 1)
        self.assertTrue(db.closed)
        self.assertNotIn("synthetic private", output)

    def test_service_failure_is_safe_and_client_closes(self):
        from elk.mongo import mongo_database
        from pymongo.errors import PyMongoError
        client = Mock()
        client.__getitem__ = Mock(side_effect=PyMongoError("PRIVATE raw exception"))
        with self.assertRaises(ElkError) as error:
            with mongo_database(runtime.Config("synthetic", RULES), "mongodb://unused", factory=lambda *a, **k: client):
                pass
        client.close.assert_called_once()
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_timezone_and_timeout_options_reused(self):
        from elk.mongo import mongo_database
        client = Mock()
        client.__getitem__ = Mock(return_value=FakeDatabase())
        factory = Mock(return_value=client)
        with mongo_database(runtime.Config("synthetic", RULES), "mongodb://unused", factory=factory):
            pass
        self.assertTrue(factory.call_args.kwargs["tz_aware"])
        self.assertIs(factory.call_args.kwargs["tzinfo"], timezone.utc)
        self.assertEqual(factory.call_args.kwargs["serverSelectionTimeoutMS"], 5000)
        client.close.assert_called_once()

    def test_optional_elk_is_not_required_for_default_checker(self):
        with patch.object(elk_check, "check_exposure", side_effect=AssertionError("Unexpected ES")):
            code, _, _, _ = self.cli(check_governance, [])
        self.assertEqual(code, 0)


class AuditAndSourceTests(OfflineCase):
    def test_safe_audit_schema_hashes_identifiers_and_has_utc(self):
        record = audit.make_record("event_detail_access", result="success", category="event_detail",
                                   authenticated=True, user_id=17, document_id="PRIVATE ID", status=200, now=NOW)
        self.assertEqual(set(record), {"timestamp", "event", "category", "result", "authenticated",
                                       "user_hash", "document_hash", "status"})
        self.assertTrue(record["timestamp"].endswith("+00:00"))
        self.assertNotIn("PRIVATE ID", json.dumps(record))
        self.assertEqual(len(record["document_hash"]), 64)

    def test_audit_never_accepts_arbitrary_metadata_or_exception_fields(self):
        for key in ("description", "password", "query", "db_uri", "company_url", "exception"):
            with self.assertRaises(TypeError):
                audit.make_record("dashboard_access", result="success", category="dashboard", **{key: "private"})

    def test_audit_rejects_secret_content_in_event_or_category(self):
        for overrides in ({"event": "password=private"}, {"category": "mongodb://private.invalid"}, {"result": "raw error"}):
            with self.assertRaises(ValueError) as error:
                audit.make_record(**{"event": "dashboard_access", "category": "dashboard", "result": "success", **overrides})
            self.assertNotIn("private", str(error.exception))

    def test_audit_identifiers_with_secret_content_are_only_hashed(self):
        record = audit.make_record("event_detail_access", result="success", category="event_detail",
                                   document_id="password=synthetic private", user_id="mongodb://private.invalid")
        self.assertNotIn("private", repr(record))
        self.assertNotIn("password", repr(record))

    def test_audit_has_rotation_and_creates_directory_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = audit.configure(Path(directory) / "new")
            handler = logger.handlers[0]
            try:
                self.assertEqual(handler.maxBytes, 1_048_576)
                self.assertEqual(handler.backupCount, 5)
                self.assertFalse(logger.propagate)
                handler.maxBytes = 100
                for _ in range(10): logger.info('{"event":"synthetic_rotation","result":"success"}')
                self.assertTrue((Path(directory) / "new/audit.jsonl.1").exists())
            finally: handler.close()

    def test_audit_directory_failure_uses_safe_console_fallback(self):
        out = io.StringIO()
        with patch.object(Path, "mkdir", side_effect=PermissionError("PRIVATE PATH")):
            logger = audit.configure("/unused", stream=out)
        logger.info('{"event":"synthetic"}')
        self.assertIn("console_fallback", out.getvalue())
        self.assertNotIn("PRIVATE", out.getvalue())
        for handler in logger.handlers: handler.close()

    def test_registry_has_four_sources_and_only_bitlock_active(self):
        rows = sources.validate_registry()
        self.assertEqual({row.name for row in rows}, {"bitlock", "gunra", "black_shrantac", "dragonforce"})
        self.assertEqual(sources.active_modules(), ("crawling.bitlock_crawler",))
        self.assertTrue(all(row.status in sources.STATUSES for row in rows))
        self.assertNotIn(".onion", repr(rows))

    def test_registry_rejects_unknown_status_duplicate_and_bad_module(self):
        for bad in (replace(sources.SOURCES[0], status="UNKNOWN"), replace(sources.SOURCES[0], module="evil.module"),
                    replace(sources.SOURCES[0], reason="")):
            with self.assertRaises(ValueError): sources.validate_registry((bad, *sources.SOURCES[1:]))
        with self.assertRaises(ValueError): sources.validate_registry((sources.SOURCES[0], sources.SOURCES[0]))

    def test_scheduler_evaluates_active_state_before_build_and_execution(self):
        from scheduler import scheduler
        for status in ("PAUSED", "UNAVAILABLE", "RETIRED"):
            rows = (replace(sources.SOURCES[0], status=status), *sources.SOURCES[1:])
            with patch.object(sources, "SOURCES", rows), patch.object(scheduler.subprocess, "run") as run:
                self.assertEqual(scheduler.build_scheduler().get_jobs(), [])
                scheduler.run_crawler("crawling.bitlock_crawler")
                run.assert_not_called()

    def test_governance_import_is_pure_in_fresh_process(self):
        code = """
from unittest.mock import patch
import os
with patch.dict(os.environ, {}, clear=True), \
     patch('pymongo.MongoClient', side_effect=AssertionError('Mongo')), \
     patch('socket.socket.connect', side_effect=AssertionError('network')), \
     patch('socket.getaddrinfo', side_effect=AssertionError('DNS')), \
     patch('dotenv.load_dotenv', side_effect=AssertionError('env')), \
     patch('logging.FileHandler.__init__', side_effect=AssertionError('log file')):
    from governance import policy, sources, audit, retention, runtime, elk_check
    assert sources.active_modules() == ('crawling.bitlock_crawler',)
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)


class ConfigurationAndELKTests(OfflineCase):
    def test_dashboard_default_is_auth_required_with_http_development(self):
        self.assertEqual(policy.dashboard_config_errors({}), [])
        self.assertTrue(policy.boolean("True", "DASHBOARD_REQUIRE_AUTH"))

    def test_production_requires_auth(self):
        values = {"DJANGO_DEBUG": "False", "DJANGO_SECRET_KEY": "s" * 60,
                  "DJANGO_ALLOWED_HOSTS": "localhost", "DASHBOARD_REQUIRE_AUTH": "False"}
        self.assertTrue(any("DASHBOARD_REQUIRE_AUTH" in value for value in policy.dashboard_config_errors(values)))

    def test_production_rejects_wildcards_and_empty_host_list(self):
        for hosts in ("*", "localhost,*", "", ", ,", "*.example.org"):
            errors = policy.dashboard_config_errors({"DJANGO_DEBUG": "False", "DJANGO_SECRET_KEY": "s" * 60,
                                                      "DJANGO_ALLOWED_HOSTS": hosts})
            self.assertTrue(any("DJANGO_ALLOWED_HOSTS" in value for value in errors))

    def test_boolean_values_are_strict_in_runtime_checker(self):
        for name in ("DASHBOARD_REQUIRE_AUTH", "DJANGO_SECURE_COOKIES"):
            self.assertTrue(any(name in msg for msg in check_runtime_config.validate_config({name: "bad"})))

    def test_all_retention_settings_checked_by_runtime_checker(self):
        for name, _, _, _ in policy.RETENTION_SETTINGS.values():
            self.assertTrue(any(name in msg for msg in check_runtime_config.validate_config({name: "0"})))

    def test_secret_check_recognizes_hardcoded_key_without_matching_dev_placeholders(self):
        from scripts.check_secrets import RULES as secret_rules
        pattern = secret_rules["hardcoded_django_secret"]
        self.assertIsNotNone(pattern.search("SECRET_KEY = '" + "a" * 60 + "'"))
        self.assertIsNotNone(pattern.search('DJANGO_SECRET_KEY = "' + "b" * 60 + '"'))
        self.assertIsNone(pattern.search("SECRET_KEY = 'dev-only-" + "a" * 60 + "'"))

    def test_development_can_explicitly_disable_auth_and_keep_http(self):
        self.assertEqual(policy.dashboard_config_errors({"DJANGO_DEBUG": "True", "DASHBOARD_REQUIRE_AUTH": "False",
                                                        "DJANGO_SECURE_COOKIES": "False"}), [])

    def test_elk_check_makes_read_requests_and_never_returns_bodies(self):
        config = ElkConfig("synthetic", "day13-test", "http://localhost:9200", "http://localhost:5601", "http://localhost:8080")
        es = Mock()
        es.request.return_value = {"hits": {"total": {"relation": "eq", "value": 0}}, "timed_out": False, "_shards": {"failed": 0}}
        with patch.object(elk_check, "existing_mapping", return_value={}):
            self.assertTrue(elk_check.check_exposure(config, es))
        self.assertEqual(es.request.call_count, 3)
        for call in es.request.call_args_list:
            self.assertEqual(call.args[0], "POST")
            self.assertTrue(call.args[1].endswith("/_search"))
            self.assertEqual(call.args[2]["size"], 0)
            blocked = call.args[2]["runtime_mappings"]["day13_private_source_field"]["script"]["params"]["blocked"]
            self.assertTrue({"message", "claim_token", "resume_token"} <= set(blocked))

    def test_elk_prohibited_source_field_or_partial_search_fails(self):
        config = ElkConfig("synthetic", "day13-test", "http://localhost:9200", "http://localhost:5601", "http://localhost:8080")
        good = {"hits": {"total": {"relation": "eq", "value": 0}}, "timed_out": False, "_shards": {"failed": 0}}
        for result in ({**good, "hits": {"total": {"relation": "eq", "value": 1}}},
                       {**good, "timed_out": True}, {**good, "_shards": {"failed": 1}},
                       {**good, "hits": {"total": {"relation": "gte", "value": 0}}}):
            es = Mock()
            es.request.return_value = result
            with patch.object(elk_check, "existing_mapping", return_value={}), self.assertRaises(ElkError):
                elk_check.check_exposure(config, es)


if __name__ == "__main__":
    unittest.main()
