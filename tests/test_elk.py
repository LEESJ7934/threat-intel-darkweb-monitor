"""Offline Day 12 contracts: no real HTTP, Docker or MongoDB."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from pymongo.errors import PyMongoError
from elk import e2e
from elk.config import COLLECTIONS, Config, ElkError, load_config, validate_prefix, validate_url
from elk.http import Http, MAX_RESPONSE, NoRedirect, wait_until
from elk.mappings import data_view, index_template, mapping, mapping_matches, properties
from elk.mongo import mongo_database
from elk.setup import setup
from elk.verify import verify
from scripts import day12_elk_e2e, setup_elk
from scripts.check_runtime_config import validate_config

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
CONFIG = Config("day12_elk_e2e", "day12-e2e", "http://localhost:9200",
                "http://localhost:5601", "http://127.0.0.1:8080")
ENV = {"DB_URI": "mongodb://unused", "DB_NAME": CONFIG.database,
       "ELASTICSEARCH_URL": CONFIG.elasticsearch, "KIBANA_URL": CONFIG.kibana,
       "ELK_INDEX_PREFIX": CONFIG.prefix, "TELEGRAM_TOKEN": "synthetic", "TELEGRAM_CHAT_ID": "-10099"}


class FakeHTTP:
    def __init__(self):
        self.calls, self.indices, self.templates, self.views, self.documents, self.counts = [], {}, {}, {}, {}, {}
        self.ready, self.health, self.failed_shards, self.failure = True, "ok", 0, None

    def request(self, method, path, body=None, *, allow_missing=False, text=False):
        self.calls.append((method, path, deepcopy(body)))
        assert method in {"GET", "PUT", "POST"}, "Unexpected HTTP mutation"
        if self.failure:
            raise self.failure
        if path == "/_cluster/health":
            return {"status": "yellow" if self.ready else "red", "timed_out": False}
        if path == "/api/status":
            return {"status": {"overall": {"level": "available" if self.ready else "unavailable"}}}
        if path == "/healthz":
            return self.health
        if method == "PUT" and path.startswith("/_index_template/"):
            self.templates[path.rsplit("/", 1)[1]] = deepcopy(body)
            return {"acknowledged": True}
        if method == "GET" and path.endswith("/_mapping"):
            index = path.split("/")[1]
            return {index: {"mappings": deepcopy(self.indices[index])}} if index in self.indices else None
        if method == "PUT" and path.count("/") == 1:
            index = path[1:]
            template = next(t for t in self.templates.values() if index in t["index_patterns"])
            self.indices[index] = deepcopy(template["template"]["mappings"])
            return {"acknowledged": True}
        if path.endswith("/_count"):
            return {"count": self.counts.get(path.split("/")[1], 0), "_shards": {"failed": self.failed_shards}}
        if "/_doc/" in path:
            index, identity = path.strip("/").split("/_doc/")
            return deepcopy(self.documents.get((index, identity)))
        if path == "/api/data_views":
            return {"data_view": [{"id": key, "title": view["title"]} for key, view in self.views.items()]}
        if method == "GET" and path.startswith("/api/data_views/data_view/"):
            identity = path.rsplit("/", 1)[1]
            return {"data_view": deepcopy(self.views[identity])} if identity in self.views else None
        if method == "POST" and path == "/api/data_views/data_view":
            assert body["override"] is False
            view = body["data_view"]
            if view["id"] in self.views:
                raise ElkError("http_error", "Kibana", 409)
            self.views[view["id"]] = deepcopy(view)
            return {"data_view": deepcopy(view)}
        raise AssertionError((method, path))


class FakeCollection:
    def __init__(self, db):
        self.db, self.docs = db, {}
    def __getattr__(self, name):
        raise AssertionError("Unexpected Mongo operation: " + name)
    def count_documents(self, query, **options):
        assert query == {} and options == {"maxTimeMS": 5000}
        return len(self.docs)
    def find_one(self, query, **options):
        assert options == {"max_time_ms": 5000}
        return next((deepcopy(doc) for doc in self.docs.values()
                     if all(doc.get(key) == value for key, value in query.items())), None)
    def update_one(self, query, update, upsert=False):
        assert self.db.writes_allowed, "Mongo write prohibited"
        self.db.writes += 1
        found = next((doc for doc in self.docs.values()
                      if all(doc.get(key) == value for key, value in query.items())), None)
        matched = int(found is not None)
        if found is None and upsert:
            found = {**query, **deepcopy(update.get("$setOnInsert", {}))}
            self.docs[found["_id"]] = found
        if found is not None:
            found.update(deepcopy(update.get("$set", {})))
        return SimpleNamespace(matched_count=matched)


class FakeDatabase:
    def __init__(self, writes_allowed=False):
        self.writes_allowed, self.writes = writes_allowed, 0
        self.collections = {name: FakeCollection(self) for name in COLLECTIONS.values()}
    def __getitem__(self, name):
        assert name in self.collections, "Unexpected collection"
        return self.collections[name]


class ELKTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.getaddrinfo", "pymongo.MongoClient"):
            self.enterContext(patch(target, side_effect=AssertionError("External I/O prohibited")))
        self.es, self.kibana, self.monstache = FakeHTTP(), FakeHTTP(), FakeHTTP()
        self.db, self.messages = FakeDatabase(), []

    def prepare(self):
        setup(CONFIG, self.es, self.kibana, wait=0, emit=self.messages.append)

    def check(self):
        return verify(CONFIG, self.es, self.kibana, self.monstache, self.db,
                      wait=0, emit=self.messages.append)

    def rendered(self, db="day12_elk_e2e", prefix="day12-e2e"):
        text = (ROOT / "monstache/monstache.config.toml").read_text()
        self.assertNotIn("darkweb.leaked_data", text)
        text = text.replace('{{index . "DB_NAME"}}', db).replace('{{index . "ELK_INDEX_PREFIX"}}', prefix)
        self.assertNotIn("{{", text)
        return tomllib.loads(text)

    def test_valid_prefixes(self):
        for value in ("darkweb-monitor", "day12_e2e", "day12-e2e", "a", "9" * 64):
            self.assertEqual(validate_prefix(value), value)

    def test_invalid_prefixes(self):
        for value in ("", "DarkWeb", "foo/bar", "foo*", "-foo", "_foo", "한글", "a" * 65, "a\n", None):
            with self.subTest(value=value), self.assertRaises(ElkError):
                validate_prefix(value)

    def test_exact_index_names_and_stable_view_ids(self):
        self.assertEqual([CONFIG.index(k) for k in COLLECTIONS],
                         ["day12-e2e-events", "day12-e2e-history", "day12-e2e-alerts"])
        self.assertEqual(len({CONFIG.view_id(k) for k in COLLECTIONS}), 3)
        self.assertNotEqual(CONFIG.index("events"), replace(CONFIG, prefix="other").index("events"))

    def test_url_validation(self):
        for value in ("file:///tmp", "http://u:p@host", "http://host/?token=x", "//host",
                      "http://host/#x", "http://host:70000", "http://ho st", "http://host/path", ""):
            with self.subTest(value=value), self.assertRaises(ElkError):
                validate_url(value, "URL")
        self.assertEqual(validate_url("http://localhost:9200/", "URL"), "http://localhost:9200")

    def test_config_and_default_prefix(self):
        self.assertEqual(load_config(ENV), CONFIG)
        self.assertEqual(load_config({k: v for k, v in ENV.items() if k != "ELK_INDEX_PREFIX"}).prefix,
                         "darkweb-monitor")

    def test_database_namespace_validation(self):
        for name in ('bad"name', "db.name", "monstache", "config", "", "a" * 64):
            with self.subTest(name=name), self.assertRaises(ElkError):
                load_config({**ENV, "DB_NAME": name})

    def test_direct_read_exact_three_namespaces(self):
        self.assertEqual(self.rendered()["direct-read-namespaces"],
                         [CONFIG.database + "." + name for name in COLLECTIONS.values()])

    def test_change_stream_exact_three_namespaces(self):
        self.assertEqual(self.rendered()["change-stream-namespaces"],
                         [CONFIG.database + "." + name for name in COLLECTIONS.values()])
        self.assertNotIn("namespace-regex", self.rendered())

    def test_internal_state_excluded(self):
        config = self.rendered()
        names = config["direct-read-namespaces"] + config["change-stream-namespaces"]
        self.assertFalse(any(n.endswith(".alert_state") or n.startswith("monstache.") for n in names))
        self.assertEqual(config["config-database-name"], "monstache")

    def test_mapping_and_projection_for_each_namespace(self):
        config = self.rendered()
        wanted = dict(zip(config["direct-read-namespaces"], [CONFIG.index(k) for k in COLLECTIONS]))
        self.assertEqual({r["namespace"]: r["index"] for r in config["mapping"]}, wanted)
        self.assertEqual({r["namespace"] for r in config["script"]}, set(wanted))
        self.assertTrue(all(r["path"] == "/config/project_document.js" for r in config["script"]))

    def test_stateful_token_resume_scope(self):
        config = self.rendered()
        self.assertTrue(config["resume"])
        self.assertTrue(config["direct-read-stateful"])
        self.assertEqual(config["resume-strategy"], 1)
        self.assertFalse(config["resume-write-unsafe"])
        self.assertFalse(config["exit-after-direct-reads"])
        self.assertNotEqual(config["resume-name"], self.rendered(prefix="other")["resume-name"])
        self.assertNotEqual(config["resume-name"], self.rendered(db="other")["resume-name"])

    def test_drop_propagation_disabled(self):
        config = self.rendered()
        self.assertFalse(config["dropped-collections"])
        self.assertFalse(config["dropped-databases"])
        self.assertFalse(config["index-as-update"])
        self.assertEqual(config["delete-index-pattern"], ",".join(CONFIG.index(k) for k in COLLECTIONS))

    def test_http_stats_and_no_verbose_body_logs(self):
        config = self.rendered()
        self.assertTrue(config["enable-http-server"])
        self.assertTrue(config["stats"])
        self.assertEqual(config["http-server-addr"], ":8080")
        self.assertFalse(config["verbose"])

    def test_compose_ports_pins_env_and_template_flag(self):
        text = (ROOT / "docker-compose.yml").read_text()
        for value in ("elasticsearch:8.15.3", "kibana:8.15.3", "monstache:6.7.7",
                      "127.0.0.1:9200:9200", "127.0.0.1:5601:5601", "127.0.0.1:8080:8080",
                      "DB_NAME:", "ELK_INDEX_PREFIX:", '"-tpl"', "MONSTACHE_MONGO_URL:",
                      "condition: service_healthy", 'profiles: ["sync"]'):
            self.assertIn(value, text)
        self.assertNotIn("latest", text)
        self.assertNotIn("0.0.0.0:", text)

    def test_event_mapping(self):
        p = properties("events")
        self.assertEqual(p["company_name"]["type"], "text")
        self.assertEqual(p["company_name"]["fields"]["keyword"]["type"], "keyword")
        for f in ("source", "company_url", "country", "data_size", "publication_date",
                  "event_key", "identity_basis", "content_fingerprint"):
            self.assertEqual(p[f]["type"], "keyword")
        self.assertNotIn("risk_level", p)
        self.assertNotIn("_id", p)

    def test_history_metadata_allowlist(self):
        from crawling.models import METADATA_FIELDS
        p = properties("history")
        self.assertEqual(set(p["changes"]["properties"]), set(METADATA_FIELDS))
        for field in METADATA_FIELDS:
            self.assertEqual(set(p["changes"]["properties"][field]["properties"]), {"before", "after"})

    def test_alert_mapping_and_private_fields(self):
        p = properties("alerts")
        self.assertEqual(p["risk_level"]["type"], "keyword")
        self.assertEqual(p["risk_reason"]["type"], "text")
        for field in ("claim_token", "resume_token", "message", "raw_exception", "_id"):
            self.assertNotIn(field, p)

    def test_date_mappings(self):
        for kind, fields in (("events", ("first_seen", "last_seen", "scraped_time")),
                             ("history", ("changed_at",)), ("alerts", ("created_at", "updated_at", "sent_at"))):
            for field in fields:
                self.assertEqual(properties(kind)[field]["type"], "date")
                self.assertIn("epoch_millis", properties(kind)[field]["format"])

    def test_default_date_format_omission_from_es_mapping_is_compatible(self):
        expected = mapping(CONFIG, "events")
        actual = deepcopy(expected)
        for field in ("first_seen", "last_seen", "scraped_time"):
            actual["properties"][field].pop("format")
        self.assertTrue(mapping_matches(actual, expected))

        actual["properties"]["last_seen"]["type"] = "keyword"
        self.assertFalse(mapping_matches(actual, expected))

    def test_integer_mappings(self):
        for kind, fields in (("events", ("observation_count", "schema_version")),
                             ("history", ("schema_version",)), ("alerts", ("attempt_count", "schema_version"))):
            for field in fields:
                self.assertEqual(properties(kind)[field], {"type": "integer", "coerce": False})

    def test_template_exact_scope_strict_mapping_and_database_ownership(self):
        for kind in COLLECTIONS:
            t = index_template(CONFIG, kind)
            self.assertEqual(t["index_patterns"], [CONFIG.index(kind)])
            self.assertEqual(t["template"]["mappings"]["dynamic"], "strict")
            self.assertEqual(t["template"]["mappings"]["_meta"]["source_database"], CONFIG.database)

    def test_implicit_object_type_from_es_mapping_is_compatible(self):
        expected = mapping(CONFIG, "history")
        actual = deepcopy(expected)
        changes = actual["properties"]["changes"]
        changes.pop("type")
        for value in changes["properties"].values():
            value.pop("type")
        self.assertTrue(mapping_matches(actual, expected))
        changes["properties"]["data_size"]["properties"]["after"]["type"] = "keyword"
        self.assertFalse(mapping_matches(actual, expected))

    def test_malformed_nested_readiness_response_is_safe_failure(self):
        with patch.object(self.kibana, "request", return_value={"status": []}):
            with self.assertRaises(ElkError):
                self.prepare()

    def test_data_view_time_fields(self):
        for kind, field in (("events", "last_seen"), ("history", "changed_at"), ("alerts", "created_at")):
            view = data_view(CONFIG, kind)
            self.assertEqual(view["timeFieldName"], field)
            self.assertEqual(view["title"], CONFIG.index(kind))
            self.assertIn(CONFIG.prefix, view["name"])

    def test_data_view_names_are_unique_across_prefixes(self):
        other = replace(CONFIG, prefix="day12-e2e-other")
        for kind in COLLECTIONS:
            self.assertNotEqual(data_view(CONFIG, kind)["name"], data_view(other, kind)["name"])

    def test_setup_idempotent(self):
        self.prepare()
        snapshot = deepcopy((self.es.indices, self.es.templates, self.kibana.views))
        self.prepare()
        self.assertEqual(snapshot, (self.es.indices, self.es.templates, self.kibana.views))
        self.assertEqual(sum(m == "POST" for m, _, _ in self.kibana.calls), 3)
        self.assertEqual(len(self.es.indices), 3)

    def test_existing_manual_data_view_reused(self):
        self.kibana.views["manual"] = {**data_view(CONFIG, "events"), "id": "manual"}
        self.prepare()
        self.assertEqual(len(self.kibana.views), 3)
        self.assertNotIn(CONFIG.view_id("events"), self.kibana.views)

    def test_wrong_time_field_is_conflict_not_overridden(self):
        self.kibana.views[CONFIG.view_id("events")] = {**data_view(CONFIG, "events"), "timeFieldName": "wrong"}
        with self.assertRaises(ElkError):
            self.prepare()
        self.assertFalse(any(m == "POST" for m, _, _ in self.kibana.calls))

    def test_wrong_database_or_mapping_fails_before_writes(self):
        for actual in (mapping(replace(CONFIG, database="other"), "events"), {"properties": {}}):
            self.es.indices[CONFIG.index("events")] = actual
            with self.assertRaises(ElkError):
                self.prepare()
            self.assertEqual(self.es.templates, {})

    def test_missing_es_is_safe_failure(self):
        self.es.failure = ElkError("unreachable", "Elasticsearch")
        with self.assertRaisesRegex(ElkError, "unreachable"):
            self.prepare()
        self.assertEqual(self.kibana.calls, [])

    def test_unready_kibana_does_not_create_views(self):
        self.kibana.ready = False
        with self.assertRaisesRegex(ElkError, "not_ready"):
            self.prepare()
        self.assertFalse(any(m == "POST" for m, _, _ in self.kibana.calls))

    def test_three_counts_and_read_only_verifier(self):
        self.prepare()
        for count, (kind, collection) in enumerate(COLLECTIONS.items(), 2):
            self.db[collection].docs = {str(i): {"_id": str(i)} for i in range(count)}
            self.es.counts[CONFIG.index(kind)] = count
        self.assertEqual(self.check(), {"events": (2, 2), "history": (3, 3), "alerts": (4, 4)})
        self.assertEqual(self.db.writes, 0)
        self.assertIn("DAY12_ELK_PIPELINE: PASS", self.messages)

    def test_each_count_mismatch_fails(self):
        self.prepare()
        for kind in COLLECTIONS:
            self.es.counts = {CONFIG.index(kind): 1}
            with self.subTest(kind=kind), self.assertRaisesRegex(ElkError, "count_mismatch"):
                self.check()

    def test_unhealthy_monstache_fails(self):
        self.prepare()
        self.monstache.health = "not ok"
        with self.assertRaisesRegex(ElkError, "unhealthy"):
            self.check()

    def test_missing_index_fails(self):
        self.prepare()
        self.es.indices.pop(CONFIG.index("history"))
        with self.assertRaisesRegex(ElkError, "index_missing"):
            self.check()

    def test_missing_data_view_fails(self):
        self.prepare()
        self.kibana.views.pop(CONFIG.view_id("alerts"))
        with self.assertRaisesRegex(ElkError, "data_view_missing"):
            self.check()

    def test_partial_count_fails(self):
        self.prepare()
        self.es.failed_shards = 1
        with self.assertRaisesRegex(ElkError, "partial_count"):
            self.check()

    def test_no_delete_http_calls(self):
        self.prepare()
        self.check()
        self.assertFalse(any(m == "DELETE" for c in (self.es, self.kibana, self.monstache) for m, _, _ in c.calls))

    def test_wait_eventual_sync_and_timeout_without_sleep(self):
        clock, attempts = [0], []
        def action():
            attempts.append(1)
            if len(attempts) < 3:
                raise ElkError("not_synced")
            return "done"
        self.assertEqual(wait_until(action, 5, clock=lambda: clock[0],
                                    sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), "done")
        with self.assertRaises(ElkError):
            wait_until(lambda: (_ for _ in ()).throw(ElkError("not_synced")), 0)

    def test_http_json_xsrf_timeout(self):
        calls = []
        def open_request(request, timeout):
            calls.append((request, timeout))
            return io.BytesIO(b'{"acknowledged":true}')
        Http(CONFIG.kibana, "Kibana", opener=SimpleNamespace(open=open_request)).request("POST", "/api/test", {})
        self.assertEqual(calls[0][0].get_header("Kbn-xsrf"), "day12")
        self.assertEqual(calls[0][1], 5)
        self.assertEqual(json.loads(calls[0][0].data), {})

    def test_http_error_excludes_body_credentials(self):
        private = "mongodb://private.invalid private-response"
        error = HTTPError("http://localhost/?token=private", 401, private, {}, io.BytesIO(private.encode()))
        def fail(*a, **k):
            raise error
        with self.assertRaises(ElkError) as result:
            Http(CONFIG.elasticsearch, "ES", opener=SimpleNamespace(open=fail)).request("GET", "/")
        self.assertEqual(str(result.exception), "ES: http_error")
        self.assertEqual(result.exception.status, 401)
        self.assertTrue(error.fp.closed)

    def test_http_timeout_and_unreachable(self):
        for error, category in ((TimeoutError("private"), "timeout"), (URLError("private"), "unreachable")):
            def fail(*a, **k):
                raise error
            with self.assertRaises(ElkError) as caught:
                Http(CONFIG.elasticsearch, "ES", opener=SimpleNamespace(open=fail)).request("GET", "/")
            self.assertEqual(caught.exception.category, category)

    def test_bad_and_oversized_response(self):
        for payload in (b"not-json", b"[]", b"x" * (MAX_RESPONSE + 1)):
            with self.assertRaises(ElkError):
                Http(CONFIG.elasticsearch, "ES", opener=SimpleNamespace(
                    open=lambda *a, **k: io.BytesIO(payload))).request("GET", "/")

    def test_http_delete_and_redirects_blocked(self):
        with self.assertRaises(ElkError):
            Http(CONFIG.elasticsearch, "ES").request("DELETE", "/index")
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "http://private"))

    def test_mongo_utc_timeouts_and_close_including_failure(self):
        calls = []
        class Client:
            closed = False
            def __getitem__(self, name):
                return self
            def close(self):
                self.closed = True
        for failure in (False, True):
            client = Client()
            def factory(uri, **kwargs):
                calls.append(kwargs)
                return client
            try:
                with mongo_database(CONFIG, "mongodb://unused", factory=factory):
                    if failure:
                        raise PyMongoError("mongodb://private.invalid")
            except ElkError as error:
                self.assertEqual(str(error), "MongoDB: database_error")
            self.assertTrue(client.closed)
        self.assertTrue(calls[0]["tz_aware"])
        self.assertEqual(calls[0]["tzinfo"], timezone.utc)
        self.assertEqual(calls[0]["serverSelectionTimeoutMS"], 5000)

    def test_runtime_config_old_contract_and_prefix(self):
        self.assertEqual(validate_config(ENV), [])
        for value in ("", "DarkWeb", "foo/bar", "foo*", "x" * 65):
            self.assertTrue(any("ELK_INDEX_PREFIX" in e for e in validate_config({**ENV, "ELK_INDEX_PREFIX": value})))
        for key, value in (("ALERT_MAX_ATTEMPTS", "0"), ("ALERT_MIN_LEVEL", "BAD"),
                           ("TELEGRAM_CHAT_ID", "bad"), ("DJANGO_DEBUG", "bad"),
                           ("ELASTICSEARCH_URL", "file://bad")):
            self.assertTrue(validate_config({**ENV, key: value}))

    def test_cli_failure_no_traceback(self):
        out = io.StringIO()
        with patch.object(setup_elk, "load_config", return_value=CONFIG), \
                patch.object(setup_elk, "setup", side_effect=ElkError("unreachable", "Elasticsearch")), \
                patch("sys.stdout", out):
            self.assertEqual(setup_elk.main(["--wait", "0"]), 1)
        self.assertIn("DAY12_ELK_SETUP: FAIL", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())

    def test_e2e_guards_before_clients(self):
        for config, name in ((replace(CONFIG, database="darkweb"), "darkweb"),
                             (replace(CONFIG, prefix="darkweb-monitor"), CONFIG.database),
                             (CONFIG, "different")):
            with patch.object(day12_elk_e2e, "load_config", return_value=config), \
                    patch.object(day12_elk_e2e, "mongo_database") as mongo, patch("sys.stdout", io.StringIO()):
                self.assertEqual(day12_elk_e2e.main(["seed", "--database", name]), 1)
                mongo.assert_not_called()

    def test_synthetic_seed_idempotent(self):
        db = FakeDatabase(True)
        e2e.seed(CONFIG, CONFIG.database, db, now=NOW)
        first = deepcopy(db["leaked_data"].docs)
        e2e.seed(CONFIG, CONFIG.database, db)
        self.assertEqual(first, db["leaked_data"].docs)
        self.assertEqual(len(first), 1)

    def test_synthetic_update_id_history_alerts_and_first_seen(self):
        db = FakeDatabase(True)
        e2e.seed(CONFIG, CONFIG.database, db, now=NOW)
        e2e.update(CONFIG, CONFIG.database, db, 2, now=NOW)
        e2e.update(CONFIG, CONFIG.database, db, 2)
        self.assertEqual([len(db[n].docs) for n in COLLECTIONS.values()], [1, 2, 2])
        row = e2e.current(db)
        self.assertEqual((row["_id"], row["data_size"], row["first_seen"]), (e2e.EVENT_ID, "20 GB", NOW))
        self.assertEqual(db["alert_log"].docs[e2e.alert_id(2)]["status"], "SUPPRESSED")

    def test_synthetic_out_of_order_update(self):
        db = FakeDatabase(True)
        e2e.seed(CONFIG, CONFIG.database, db, now=NOW)
        with self.assertRaisesRegex(ElkError, "out_of_order"):
            e2e.update(CONFIG, CONFIG.database, db, 3)

    def test_e2e_verifies_ids_content_dates_and_private_fields(self):
        self.prepare()
        db = FakeDatabase(True)
        e2e.seed(CONFIG, CONFIG.database, db, now=NOW)
        e2e.update(CONFIG, CONFIG.database, db, 2, now=NOW)
        for kind, collection in COLLECTIONS.items():
            for identity, row in db[collection].docs.items():
                source = {k: deepcopy(v) for k, v in row.items() if k in properties(kind)}
                self.es.documents[(CONFIG.index(kind), identity)] = {"_id": identity, "found": True, "_source": source}
        self.assertEqual(e2e.verify_once(CONFIG, CONFIG.database, db, self.es, 2), 5)
        event = self.es.documents[(CONFIG.index("events"), e2e.EVENT_ID)]
        event["_id"] = "wrong"
        with self.assertRaisesRegex(ElkError, "document_id"):
            e2e.verify_once(CONFIG, CONFIG.database, db, self.es, 2)
        event["_id"] = e2e.EVENT_ID
        self.es.documents[(CONFIG.index("alerts"), e2e.alert_id(2))]["_source"]["claim_token"] = "private"
        with self.assertRaisesRegex(ElkError, "private_field"):
            e2e.verify_once(CONFIG, CONFIG.database, db, self.es, 2)

    def test_e2e_timestamp_utc_and_invalid(self):
        self.assertEqual(e2e.timestamp(NOW), e2e.timestamp("2026-09-10T21:00:00+09:00"))
        with self.assertRaises(ElkError):
            e2e.timestamp("invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
