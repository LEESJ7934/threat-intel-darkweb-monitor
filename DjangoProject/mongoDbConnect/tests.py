"""Offline Day 11 tests. SimpleTestCase also forbids Django SQL access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from bson import ObjectId
from bs4 import BeautifulSoup
from django.test import SimpleTestCase
from django.urls import reverse
from pymongo.errors import PyMongoError

from alert.risk import classify_risk, utc_datetime
from . import dashboard as ui

NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]


def sample(identity="bitlock_demo", **overrides):
    return {"_id": identity, "source": "bitlock", "company_name": "Example Corp",
            "company_url": "https://company.example/", "country": "ZZ", "data_size": "2 TB",
            "data_contents": "synthetic overview", "description": "synthetic metadata only",
            "publication_date": "2026-09-10", "first_seen": NOW - timedelta(days=1),
            "last_seen": NOW, "scraped_time": NOW, "observation_count": 3,
            "event_key": "a" * 64, "identity_basis": "source+company_url", "schema_version": 2,
            **overrides}


def nested(document, path):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def matches(document, query):
    for key, condition in query.items():
        if key == "$or":
            if not any(matches(document, item) for item in condition):
                return False
            continue
        value = nested(document, key)
        if not isinstance(condition, dict):
            if value != condition:
                return False
            continue
        for operator, operand in condition.items():
            if operator == "$options":
                continue
            if operator == "$regex":
                flags = re.I if condition.get("$options") == "i" else 0
                if not isinstance(value, str) or not re.search(operand, value, flags):
                    return False
            elif operator == "$in":
                if value not in operand:
                    return False
            elif operator == "$ne":
                if value == operand:
                    return False
            elif operator == "$type":
                if operand != "string":
                    raise AssertionError("Unsupported fake type")
                if not isinstance(value, str):
                    return False
            elif operator in {"$gte", "$lt"}:
                if value is None or (operator == "$gte" and value < operand) or (operator == "$lt" and value >= operand):
                    return False
            else:
                raise AssertionError("Unsupported read operator")
    return True


def project(document, projection):
    if projection is None:
        return deepcopy(document)
    output = {"_id": document["_id"]} if "_id" in document else {}
    for path, included in projection.items():
        if not included:
            continue
        value = nested(document, path)
        if "." in path:
            parent, key = path.split(".", 1)
            if value is not None:
                output.setdefault(parent, {})[key] = deepcopy(value)
        elif path in document:
            output[path] = deepcopy(value)
    return output


def sort_value(value):
    if value is None:
        return (0, "")
    if isinstance(value, datetime):
        return (1, utc_datetime(value).timestamp())
    return (2, str(value))


def sort_rows(rows, fields):
    for key, direction in reversed(list(fields)):
        rows.sort(key=lambda row: sort_value(nested(row, key)), reverse=direction < 0)
    return rows


def expression(document, expr):
    if isinstance(expr, str) and expr.startswith("$"):
        return nested(document, expr[1:])
    if isinstance(expr, dict) and "$convert" in expr:
        return utc_datetime(expression(document, expr["$convert"]["input"]))
    if isinstance(expr, dict) and "$ifNull" in expr:
        first, second = expr["$ifNull"]
        value = expression(document, first)
        return value if value is not None else expression(document, second)
    raise AssertionError("Unsupported aggregation expression")


class ReadCursor:
    def __init__(self, rows):
        self.rows, self.closed, self.read_count = deepcopy(rows), False, 0

    def __iter__(self):
        for row in self.rows:
            self.read_count += 1
            yield deepcopy(row)

    def sort(self, fields):
        self.rows = sort_rows(self.rows, fields)
        return self

    def limit(self, count):
        if not 0 < count <= ui.MAX_SCAN + 1:
            raise AssertionError("Unbounded find")
        self.rows = self.rows[:count]
        return self

    def max_time_ms(self, timeout):
        if timeout != ui.QUERY_TIMEOUT_MS:
            raise AssertionError("Missing query timeout")
        return self

    def close(self):
        self.closed = True


class ReadCollection:
    def __init__(self, documents=()):
        self.documents = deepcopy(list(documents))
        self.calls, self.cursors = [], []
        self.fail = False

    def __getattr__(self, name):
        raise AssertionError("Non-read collection operation: " + name)

    def _read(self, operation, query, projection=None):
        if self.fail:
            raise PyMongoError("synthetic-private-host mongodb://private.invalid raw-exception")
        self.calls.append((operation, deepcopy(query), deepcopy(projection)))

    def cursor(self, rows):
        cursor = ReadCursor(rows)
        self.cursors.append(cursor)
        return cursor

    def aggregate(self, pipeline, **options):
        self._read("aggregate", pipeline)
        if options != {"maxTimeMS": ui.QUERY_TIMEOUT_MS, "batchSize": 50}:
            raise AssertionError("Missing aggregation bounds")
        rows = deepcopy(self.documents)
        for stage in pipeline:
            if len(stage) != 1:
                raise AssertionError("Malformed aggregation stage")
            operator, body = next(iter(stage.items()))
            if operator == "$match":
                rows = [row for row in rows if matches(row, body)]
            elif operator == "$project":
                rows = [project(row, body) for row in rows]
            elif operator == "$addFields":
                rows = [{**row, **{key: expression(row, expr) for key, expr in body.items()}} for row in rows]
            elif operator == "$sort":
                rows = sort_rows(rows, body.items())
            elif operator == "$limit":
                if not 0 < body <= ui.MAX_SCAN + 1:
                    raise AssertionError("Unbounded aggregate")
                rows = rows[:body]
            elif operator == "$group":
                if set(body) != {"_id"}:
                    raise AssertionError("Unsupported fake group")
                rows = [{"_id": value} for value in dict.fromkeys(expression(row, body["_id"]) for row in rows)]
            elif operator == "$count":
                rows = [{body: len(rows)}] if rows else []
            else:
                raise AssertionError("Non-read/unsupported aggregation stage: " + operator)
        return self.cursor(rows)

    def find(self, query, projection=None):
        self._read("find", query, projection)
        return self.cursor([project(row, projection) for row in self.documents if matches(row, query)])

    def find_one(self, query, projection=None, **options):
        self._read("find_one", query, projection)
        if options.get("max_time_ms") != ui.QUERY_TIMEOUT_MS:
            raise AssertionError("Missing detail timeout")
        return next((project(row, projection) for row in self.documents if matches(row, query)), None)

    def count_documents(self, query, **options):
        self._read("count_documents", query)
        if options.get("maxTimeMS") != ui.QUERY_TIMEOUT_MS:
            raise AssertionError("Missing count timeout")
        return sum(matches(row, query) for row in self.documents)


class ReadDatabase:
    def __init__(self, documents=(), history=(), alerts=()):
        self.collections = {"leaked_data": ReadCollection(documents), "leak_history": ReadCollection(history),
                            "alert_log": ReadCollection(alerts)}

    def __getitem__(self, name):
        if name not in self.collections:
            raise AssertionError("Unexpected collection")
        return self.collections[name]


class ReadClient:
    def __init__(self, database):
        self.database, self.closed = database, False

    def __getitem__(self, name):
        return self.database

    def close(self):
        self.closed = True


class DashboardTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"DB_URI": "mongodb://unused", "DB_NAME": "synthetic"}, clear=False))
        for name in ("socket.socket.connect", "socket.getaddrinfo", "pymongo.MongoClient"):
            self.enterContext(patch(name, side_effect=AssertionError("External I/O forbidden")))
        self.database = ReadDatabase([sample()])
        self.clients, self.client_options = [], []
        self.mongo = self.enterContext(patch("mongoDbConnect.dashboard.MongoClient", side_effect=self.factory))

    def factory(self, *args, **options):
        self.client_options.append(options)
        client = ReadClient(self.database)
        self.clients.append(client)
        return client

    def data(self, params=None, **options):
        filters, errors = ui.parse_filters(params or {})
        self.assertEqual(errors, [])
        return ui.fetch_dashboard(self.database, filters, now=NOW, **options)

    def detail(self, identity="bitlock_demo"):
        return self.client.get(reverse("event_detail", args=[ui.encode_id(identity)]))

    def test_case_insensitive_substring_search_across_all_required_fields(self):
        for field in ui.SEARCH_FIELDS:
            with self.subTest(field=field):
                self.database = ReadDatabase([sample("hit", **{field: "prefix NeEdLe suffix"}), sample("miss")])
                self.assertEqual([row["document_id"] for row in self.data({"q": "needle"})["data_list"]], ["hit"])

    def test_regex_metacharacters_are_literal(self):
        text = "(a+)+$.*[x]"
        self.database = ReadDatabase([sample("literal", description=text), sample("other", description="aaaaax")])
        result = self.data({"q": text})
        self.assertEqual([row["document_id"] for row in result["data_list"]], ["literal"])
        query = ui.search_query(ui.parse_filters({"q": text})[0])
        self.assertEqual(query["$or"][0]["company_name"]["$regex"], re.escape(text))

    def test_search_length_is_bounded_and_invalid_request_does_not_read_mongo(self):
        response = self.client.get("/", {"q": "a" * 101})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["filters"].q), 100)
        self.assertTrue(response.context["filter_errors"])
        self.mongo.assert_not_called()

    def test_empty_search_has_no_regex(self):
        self.assertEqual(ui.search_query(ui.parse_filters({"q": "  "})[0]), {})

    def test_source_filter_is_exact_and_not_an_operator(self):
        self.database = ReadDatabase([sample("one", source="bitlock"), sample("two", source="bitlock2")])
        self.assertEqual(self.data({"source": "bitlock"})["summary"]["event_count"], 1)
        self.assertEqual(ui.search_query(ui.parse_filters({"source": '{"$ne":""}'})[0])["source"], '{"$ne":""}')
        self.assertEqual(self.data({"source": '{"$ne":""}'})["summary"]["event_count"], 0)

    def test_operator_object_input_is_rejected(self):
        filters, errors = ui.parse_filters({"q": {"$where": "malicious"}, "source": {"$ne": ""}})
        self.assertTrue(errors)
        self.assertEqual(ui.search_query(filters), {})

    def test_risk_filter_uses_shared_pure_classifier(self):
        self.assertIs(ui.classify_risk, classify_risk)
        self.database = ReadDatabase([sample("high"), sample("info", data_size="1 GB")])
        result = self.data({"risk": "HIGH"})
        self.assertEqual([row["document_id"] for row in result["data_list"]], ["high"])
        self.assertEqual(result["data_list"][0]["risk_reason"], classify_risk(sample())[1])

    def test_invalid_risk_is_safe_and_not_queried(self):
        response = self.client.get("/", {"risk": '{"$ne":null}'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filter_errors"])
        self.assertEqual(response.context["filters"].risk, "")
        self.mongo.assert_not_called()

    def test_five_risk_levels_and_badges(self):
        self.database = ReadDatabase([
            sample("critical", data_contents="customer data"), sample("high"),
            sample("medium", company_name="Samsung Demo", data_size="1 GB"),
            sample("low", description="mentions samsung", data_size="1 GB"),
            sample("info", data_size="1 GB")])
        response = self.client.get("/")
        self.assertEqual({row["risk_level"] for row in response.context["data_list"]}, set(ui.RISK_LEVELS))
        for level in ui.RISK_LEVELS:
            self.assertContains(response, "risk-" + level.lower())

    def test_date_bounds_are_utc_inclusive_start_exclusive_next_day(self):
        filters, errors = ui.parse_filters({"from": "2026-09-01", "to": "2026-09-10"})
        self.assertEqual(errors, [])
        self.assertEqual(filters.start, datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(filters.end, datetime(2026, 9, 11, tzinfo=timezone.utc))
        self.database = ReadDatabase([
            sample("start", last_seen=filters.start), sample("last", last_seen=filters.end - timedelta(microseconds=1)),
            sample("too_early", last_seen=filters.start - timedelta(microseconds=1)),
            sample("too_late", last_seen=filters.end)])
        self.assertEqual({row["document_id"] for row in self.data({"from": "2026-09-01", "to": "2026-09-10"})["data_list"]},
                         {"start", "last"})

    def test_invalid_dates_do_not_crash_or_access_mongo(self):
        for query in ({"from": "invalid"}, {"to": "2026-02-30"}, {"to": "9999-12-31"},
                      {"from": "2026-9-1"}, {"from": "2026-09-11", "to": "2026-09-10"}):
            with self.subTest(query=query):
                response = self.client.get("/", query)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["filter_errors"])
        self.mongo.assert_not_called()

    def test_last_seen_takes_precedence_over_scraped_time(self):
        self.database = ReadDatabase([sample("old", last_seen=NOW - timedelta(days=20), scraped_time=NOW)])
        self.assertEqual(self.data({"from": "2026-09-10"})["summary"]["event_count"], 0)

    def test_legacy_scraped_time_fallback_and_recent_sort(self):
        legacy = sample("legacy", scraped_time=NOW + timedelta(hours=1))
        legacy.pop("last_seen")
        self.database = ReadDatabase([sample("new"), legacy])
        result = self.data()
        self.assertEqual([row["document_id"] for row in result["data_list"]], ["legacy", "new"])
        self.assertEqual(result["data_list"][0]["last_seen"], legacy["scraped_time"])

    def test_null_or_invalid_last_seen_falls_back_to_scraped_time(self):
        self.database = ReadDatabase([sample("null", last_seen=None), sample("bad", last_seen="unknown")])
        self.assertEqual(self.data({"from": "2026-09-10"})["summary"]["event_count"], 2)

    def test_aware_offset_dates_sort_by_instant(self):
        zone = timezone(timedelta(hours=9))
        self.database = ReadDatabase([sample("later", last_seen=NOW), sample("earlier", last_seen=(NOW - timedelta(hours=1)).astimezone(zone))])
        self.assertEqual([row["document_id"] for row in self.data()["data_list"]], ["later", "earlier"])

    def test_pagination_has_25_per_page(self):
        self.database = ReadDatabase([sample(str(index), last_seen=NOW - timedelta(minutes=index)) for index in range(60)])
        first, second, third = self.data(), self.data({"page": "2"}), self.data({"page": "3"})
        self.assertEqual([len(part["data_list"]) for part in (first, second, third)], [25, 25, 10])
        self.assertEqual(second["data_list"][0]["document_id"], "25")
        self.assertEqual(first["summary"]["event_count"], 60)

    def test_pagination_links_preserve_all_filters(self):
        self.database = ReadDatabase([sample(str(index)) for index in range(60)])
        result = self.data({"q": "Example", "source": "bitlock", "risk": "HIGH",
                            "from": "2026-09-01", "to": "2026-09-10", "page": "2"})
        expected = {"q": ["Example"], "source": ["bitlock"], "risk": ["HIGH"],
                    "from": ["2026-09-01"], "to": ["2026-09-10"]}
        for key, page in (("previous_url", "1"), ("next_url", "3")):
            self.assertEqual(parse_qs(urlsplit(result[key]).query), {**expected, "page": [page]})

    def test_invalid_page_falls_back_to_one(self):
        for page in ("bad", "0", "-4", "2.5", "999999999999", "999"):
            self.assertEqual(self.data({"page": page})["page"], 1)

    def test_candidate_bound_is_applied_before_risk_filter(self):
        self.database = ReadDatabase([sample("new_info", data_size="1 GB"),
                                      sample("next_info", data_size="1 GB", last_seen=NOW - timedelta(minutes=1)),
                                      sample("older_high", last_seen=NOW - timedelta(minutes=2))])
        result = self.data({"risk": "HIGH"}, max_scan=2)
        self.assertEqual(result["summary"]["event_count"], 0)
        self.assertTrue(result["candidate_truncated"])
        self.assertEqual(self.database["leaked_data"].cursors[0].read_count, 3)
        self.assertTrue(self.database["leaked_data"].cursors[0].closed)

    def test_exact_bound_without_extra_candidate_is_not_marked_truncated(self):
        self.database = ReadDatabase([sample("one"), sample("two")])
        self.assertFalse(self.data(max_scan=2)["candidate_truncated"])

    def test_query_projections_and_limits_do_not_copy_whole_document(self):
        self.database = ReadDatabase([sample(raw_html="must-not-be-read")])
        self.data()
        pipeline = self.database["leaked_data"].calls[0][1]
        self.assertNotIn("raw_html", pipeline[1]["$project"])
        self.assertEqual(pipeline[-1], {"$limit": 1001})
        self.assertTrue(all(cursor.closed for collection in self.database.collections.values() for cursor in collection.cursors))

    def test_summary_and_distributions_count_filtered_events(self):
        self.database = ReadDatabase(
            [sample("a"), sample("b", source="gunra", data_size="1 GB", first_seen=NOW - timedelta(days=8)),
             sample("c", source="gunra", first_seen=NOW + timedelta(days=1))],
            history=[{"_id": "h1", "document_id": "a"}, {"_id": "h2", "document_id": "a"},
                     {"_id": "h3", "document_id": "b"}, {"_id": "other", "document_id": "not-in-results"}],
            alerts=[{"_id": "a1", "document_id": "a", "status": "SENT"},
                    {"_id": "a2", "document_id": "a", "status": "SENT"},
                    {"_id": "a3", "document_id": "b", "status": "FAILED"},
                    {"_id": "a4", "document_id": "not-in-results", "status": "SENT"}])
        result = self.data()
        self.assertEqual(result["summary"], {"event_count": 3, "source_count": 2,
                                             "new_count": 1, "changed_count": 2, "sent_count": 2})
        self.assertEqual(result["chart_data"]["source"], [{"label": "gunra", "count": 2}, {"label": "bitlock", "count": 1}])
        self.assertEqual({item["label"]: item["count"] for item in result["chart_data"]["risk"]},
                         {"CRITICAL": 0, "HIGH": 2, "MEDIUM": 0, "LOW": 0, "INFO": 1})
        filtered = self.data({"source": "bitlock"})
        self.assertEqual(filtered["summary"]["changed_count"], 1)
        self.assertEqual(filtered["summary"]["sent_count"], 2)

    def test_source_options_use_actual_database_sources(self):
        self.database = ReadDatabase([sample("one", source="gunra"), sample("two", source="bitlock")])
        result = self.data({"source": "bitlock"})
        self.assertEqual(result["source_options"], ["bitlock", "gunra"])

    def test_no_results_skip_history_and_alert_reads(self):
        result = self.data({"q": "no-match"})
        self.assertEqual(result["summary"]["event_count"], 0)
        self.assertEqual(self.database["leak_history"].calls, [])
        self.assertEqual(self.database["alert_log"].calls, [])

    def test_legacy_v1_document_renders_without_writes(self):
        self.database = ReadDatabase([{"_id": "legacy", "company_name": "Legacy Demo", "scraped_time": NOW}])
        response = self.client.get("/")
        self.assertContains(response, "Legacy Demo")
        row = response.context["data_list"][0]
        self.assertEqual(row["event_key"], "-")
        self.assertIsNone(row["observation_count"])
        self.assertEqual(row["first_seen"], NOW)
        self.assertEqual(self.detail("legacy").status_code, 200)

    def test_missing_db_uri_returns_safe_error_context(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["database_error"], ui.DATABASE_ERROR)
        self.mongo.assert_not_called()

    def test_mongo_exception_is_safe_in_response_and_logs(self):
        self.database["leaked_data"].fail = True
        with self.assertLogs("mongoDbConnect.views", level="WARNING") as logs:
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ui.DATABASE_ERROR)
        for value in ("synthetic-private-host", "private.invalid", "raw-exception"):
            self.assertNotIn(value, response.content.decode() + repr(logs.output))
        self.assertTrue(self.clients[0].closed)

    def test_mongo_client_closes_after_success_with_utc_and_timeouts(self):
        self.client.get("/")
        self.assertTrue(self.clients[0].closed)
        self.assertEqual(self.client_options[0]["tzinfo"], timezone.utc)
        self.assertTrue(self.client_options[0]["tz_aware"])
        self.assertEqual(self.client_options[0]["serverSelectionTimeoutMS"], 5000)

    def test_mongo_constructor_failure_is_safe(self):
        self.mongo.side_effect = PyMongoError("sensitive-constructor-detail")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "sensitive-constructor-detail")

    def test_post_requests_are_rejected_before_database_access(self):
        self.assertEqual(self.client.post("/").status_code, 405)
        self.assertEqual(self.client.post(reverse("event_detail", args=[ui.encode_id("bitlock_demo")])).status_code, 405)
        self.mongo.assert_not_called()

    def test_dashboard_and_detail_only_read_and_do_not_mutate_input(self):
        before = deepcopy(self.database["leaked_data"].documents)
        self.client.get("/")
        self.detail()
        self.assertEqual(self.database["leaked_data"].documents, before)
        for collection in self.database.collections.values():
            self.assertTrue(all(call[0] in {"aggregate", "find", "find_one", "count_documents"} for call in collection.calls))

    def test_detail_displays_current_metadata_identity_and_risk(self):
        response = self.detail()
        self.assertEqual(response.status_code, 200)
        for value in ("Example Corp", "bitlock", "synthetic overview", "synthetic metadata only",
                      "source+company_url", "a" * 64, "HIGH"):
            self.assertContains(response, value)

    def test_missing_event_returns_404_and_closes_client(self):
        response = self.detail("missing")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "사건을 찾을 수 없습니다.", status_code=404)
        self.assertTrue(self.clients[0].closed)

    def test_invalid_id_returns_404_without_mongo(self):
        for token in ("invalid", "str-!!!!", "oid-bad", "str-A"):
            self.assertEqual(self.client.get("/events/" + token + "/").status_code, 404)
        self.mongo.assert_not_called()

    def test_object_id_and_string_id_are_distinct_and_reversible(self):
        identity = ObjectId()
        self.database = ReadDatabase([sample(identity, company_name="Object Demo"),
                                      sample(str(identity), company_name="String Demo")])
        self.assertNotEqual(ui.encode_id(identity), ui.encode_id(str(identity)))
        self.assertEqual(ui.decode_id(ui.encode_id(identity)), identity)
        self.assertEqual(ui.decode_id(ui.encode_id(str(identity))), str(identity))
        self.assertContains(self.detail(identity), "Object Demo")
        self.assertContains(self.detail(str(identity)), "String Demo")

    def test_string_id_with_slash_unicode_roundtrips_without_path_injection(self):
        identity = "합성/문서?x=1"
        self.database = ReadDatabase([sample(identity)])
        token = ui.encode_id(identity)
        self.assertNotIn("/", token)
        self.assertEqual(ui.decode_id(token), identity)
        self.assertEqual(self.detail(identity).status_code, 200)

    def test_history_is_document_scoped_recent_first_and_renders_changes(self):
        self.database = ReadDatabase([sample()], history=[
            {"_id": "old", "document_id": "bitlock_demo", "changed_at": NOW - timedelta(days=1),
             "changes": {"data_size": {"before": "1 GB", "after": "2 TB"}}},
            {"_id": "new", "document_id": "bitlock_demo", "changed_at": NOW,
             "changes": {"description": {"before": "old description", "after": "new description"}}},
            {"_id": "other", "document_id": "another-event", "changed_at": NOW, "changes": {}}])
        response = self.detail()
        rows = response.context["history"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["changes"][0]["field"], "description")
        self.assertContains(response, "old description")
        self.assertContains(response, "new description")
        self.assertContains(response, "1 GB")

    def test_alerts_are_document_scoped_recent_first(self):
        self.database = ReadDatabase([sample()], alerts=[
            {"_id": "older", "document_id": "bitlock_demo", "event_type": "NEW", "status": "SENT", "risk_level": "HIGH",
             "attempt_count": 1, "created_at": NOW - timedelta(hours=1), "sent_at": NOW, "changed_fields": []},
            {"_id": "newer", "document_id": "bitlock_demo", "event_type": "UPDATED", "status": "SUPPRESSED",
             "created_at": NOW, "suppression_reason": "non_material_change"},
            {"_id": "other", "document_id": "other", "event_type": "NEW", "status": "SENT", "created_at": NOW}])
        response = self.detail()
        self.assertEqual([row["event_type"] for row in response.context["alerts"]], ["UPDATED", "NEW"])
        for value in ("SUPPRESSED", "SENT", "non_material_change"):
            self.assertContains(response, value)

    def test_detail_history_and_alerts_are_bounded(self):
        self.database = ReadDatabase([sample()],
            history=[{"_id": str(i), "document_id": "bitlock_demo", "changed_at": NOW, "changes": {}} for i in range(110)],
            alerts=[{"_id": str(i), "document_id": "bitlock_demo", "created_at": NOW} for i in range(110)])
        response = self.detail()
        self.assertEqual(len(response.context["history"]), 100)
        self.assertEqual(len(response.context["alerts"]), 100)
        self.assertTrue(response.context["history_more"])
        self.assertTrue(response.context["alerts_more"])

    def test_secret_fields_and_payloads_are_excluded_from_detail_context(self):
        token = "987654321:" + "x" * 35
        uri = "mongodb" + "://test-user:synthetic@private.invalid/"
        self.database = ReadDatabase([sample(TELEGRAM_TOKEN=token, raw_html="raw-marker", DB_URI=uri)],
            history=[{"_id": "h", "document_id": "bitlock_demo", "changed_at": NOW,
                      "changes": {"claim_token": {"before": "claim-marker", "after": "private"},
                                  "description": {"before": uri, "after": token}}}],
            alerts=[{"_id": "a", "document_id": "bitlock_demo", "created_at": NOW,
                     "claim_token": "claim-marker", "resume_token": "resume-marker", "message": "message-marker",
                     "exception": "error-marker", "changed_fields": ["data_size", "TELEGRAM_TOKEN"]}])
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": token, "DB_URI": uri}):
            response = self.detail()
        context = repr({key: response.context[key] for key in ("event", "history", "alerts")})
        for value in (token, uri, "claim-marker", "resume-marker", "message-marker", "error-marker", "raw-marker", "claim_token"):
            self.assertNotIn(value, context + response.content.decode())
        self.assertContains(response, "[redacted]")

    def test_long_history_and_description_are_limited(self):
        self.database = ReadDatabase([sample(description="x" * 10000)], history=[
            {"_id": "h", "document_id": "bitlock_demo", "changed_at": NOW,
             "changes": {"description": {"before": "b" * 10000, "after": "a" * 10000}}}])
        response = self.detail()
        self.assertEqual(len(response.context["event"]["description"]), 2000)
        self.assertEqual(len(response.context["history"][0]["changes"][0]["after"]), 500)

    def test_detail_database_failure_does_not_return_500(self):
        self.database["leak_history"].fail = True
        response = self.detail()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["database_error"], ui.DATABASE_ERROR)
        self.assertTrue(self.clients[0].closed)

    def test_empty_history_and_alert_sections_render(self):
        response = self.detail()
        self.assertContains(response, "아직 기록된 변경 이력이 없습니다.")
        self.assertContains(response, "아직 기록된 알림 이력이 없습니다.")

    def test_javascript_data_and_other_unsafe_urls_are_not_clickable(self):
        for url in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///etc/passwd",
                    "//company.example", "https://user:pass@company.example/", "https://company.example\\@other.example",
                    "https://company.example/\nunsafe"):
            with self.subTest(url=url):
                self.assertIsNone(ui.safe_url(url))
                self.database = ReadDatabase([sample(company_url=url)])
                soup = BeautifulSoup(self.detail().content, "html.parser")
                self.assertFalse(any(link.get("href") == url for link in soup.find_all("a")))

    def test_http_https_external_links_have_safe_attributes(self):
        for url in ("http://company.example/path", "https://company.example/path?q=1&x=2"):
            self.assertEqual(ui.safe_url(url), url)
            self.database = ReadDatabase([sample(company_url=url)])
            soup = BeautifulSoup(self.detail().content, "html.parser")
            link = soup.find("a", href=url)
            self.assertEqual(link["target"], "_blank")
            self.assertEqual(set(link["rel"]), {"noopener", "noreferrer"})

    def test_template_autoescapes_metadata_and_history(self):
        payload = '<img src=x onerror="alert(1)">'
        self.database = ReadDatabase([sample(company_name=payload, description=payload)], history=[
            {"_id": "h", "document_id": "bitlock_demo", "changed_at": NOW,
             "changes": {"description": {"before": payload, "after": payload}}}])
        for response in (self.client.get("/"), self.detail()):
            soup = BeautifulSoup(response.content, "html.parser")
            self.assertIsNone(soup.find("img"))
            self.assertIn(payload, soup.get_text())

    def test_chart_json_script_prevents_script_context_escape(self):
        payload = "</script><script>alert('synthetic')</script>"
        self.database = ReadDatabase([sample(source=payload)])
        response = self.client.get("/")
        soup = BeautifulSoup(response.content, "html.parser")
        node = soup.find("script", id="dashboard-chart-data")
        self.assertEqual(node["type"], "application/json")
        self.assertNotIn("</script>", node.string)
        data = json.loads(node.string)
        self.assertEqual(data["source"][0]["label"], payload)
        self.assertEqual(len(soup.find_all("script")), 2)

    def test_dashboard_uses_local_assets_without_external_cdn(self):
        soup = BeautifulSoup(self.client.get("/").content, "html.parser")
        self.assertTrue(all(not script.get("src", "").startswith(("http:", "https:")) for script in soup.find_all("script")))
        self.assertTrue(all(not link.get("href", "").startswith(("http:", "https:")) for link in soup.find_all("link")))

    def test_empty_dashboard_state(self):
        self.database = ReadDatabase()
        response = self.client.get("/")
        self.assertContains(response, "조건에 맞는 사건이 없습니다.")
        self.assertEqual(response.context["summary"]["event_count"], 0)

    def test_head_request_is_read_only_and_successful(self):
        self.assertEqual(self.client.head("/").status_code, 200)
        self.assertTrue(self.clients[0].closed)

    def test_canonical_django_import_has_no_external_client_side_effect(self):
        code = """
import os, sys, types
from unittest.mock import patch, Mock
os.environ['DJANGO_SETTINGS_MODULE'] = 'DjangoProject.settings'
os.environ['DJANGO_DEBUG'] = 'True'
sys.path.insert(0, 'DjangoProject')
fake = types.ModuleType('telegram')
fake.Bot = Mock(side_effect=AssertionError('Bot import side effect'))
with patch.dict(sys.modules, {'telegram': fake}), \\
     patch('pymongo.MongoClient', side_effect=AssertionError('Mongo import side effect')) as mongo, \\
     patch('socket.socket.connect', side_effect=AssertionError('network')), \\
     patch('socket.getaddrinfo', side_effect=AssertionError('DNS')), \\
     patch('sys.exit', side_effect=AssertionError('sys.exit')):
    import django
    django.setup()
    from mongoDbConnect import dashboard, views
    from alert.risk import classify_risk
    assert dashboard.classify_risk is classify_risk
    assert classify_risk({})[0] == 'INFO'
    mongo.assert_not_called()
    fake.Bot.assert_not_called()
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
