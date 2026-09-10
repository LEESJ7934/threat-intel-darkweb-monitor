"""Offline-only Day 8 tests: synthetic HTML, mocked browser and MongoDB."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
from pymongo.errors import PyMongoError
from selenium.common.exceptions import TimeoutException, WebDriverException

from crawling import common, models, storage
from crawling import gunra_crawler as gunra
from crawling import Black_Shrantac_crawler as black
from crawling import dragonforce_crawler as dragon
from crawling import bitlock_crawler as bitlock

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 1, 6, 12, 30, tzinfo=timezone.utc)
CASES = (
    (gunra, "gunra_sample.html"), (black, "black_shrantac_sample.html"),
    (dragon, "dragonforce_sample.html"), (bitlock, "bitlock_sample.html"),
)
TEXT_FIELDS = ("company_name", "company_url", "country", "data_contents", "data_size",
               "publication_date", "description")


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class OfflineParserTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ("crawling.common.webdriver.Chrome", "crawling.common.MongoClient",
                       "crawling.common.driver_service", "crawling.common.load_config",
                       "socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("Unexpected I/O")))
        self.stack.enter_context(patch("crawling.common.LOGGER"))

    def assert_fields(self, module, filename, expected):
        document = module.parse_html(fixture(filename), NOW)[0]
        self.assertEqual({key: document[key] for key in TEXT_FIELDS}, expected)

    def test_gunra_fields(self):
        self.assert_fields(gunra, "gunra_sample.html", {
            "company_name": "Example Gunra", "company_url": "https://gunra-fixture.example",
            "country": "ZZ", "data_contents": "Synthetic metadata listing", "data_size": "10 GB",
            "publication_date": "unknown", "description": "unknown",
        })

    def test_black_shrantac_fields(self):
        self.assert_fields(black, "black_shrantac_sample.html", {
            "company_name": "Example Black", "company_url": "https://black-fixture.example",
            "country": "ZZ", "data_contents": "unknown", "data_size": "20 GB",
            "publication_date": "unknown", "description": "unknown",
        })

    def test_dragonforce_retains_description_and_publication_date(self):
        self.assert_fields(dragon, "dragonforce_sample.html", {
            "company_name": "Example Dragon", "company_url": "https://dragon-fixture.example",
            "country": "unknown", "data_contents": "unknown", "data_size": "30 GB",
            "publication_date": "2026-01-03", "description": "Synthetic company overview",
        })

    def test_bitlock_fields_and_updated_label(self):
        self.assert_fields(bitlock, "bitlock_sample.html", {
            "company_name": "bitlock-fixture", "company_url": "bitlock-fixture.example",
            "country": "unknown", "data_contents": "unknown", "data_size": "unknown",
            "publication_date": "2026-01-05", "description": "Synthetic publication overview",
        })

    def test_missing_optional_fields_are_unknown(self):
        for module, filename in CASES:
            with self.subTest(source=module.SOURCE):
                document = module.parse_html(fixture(filename), NOW)[1]
                for field in TEXT_FIELDS:
                    if field == "company_name" or (module is bitlock and field == "company_url"):
                        continue
                    self.assertEqual(document[field], "unknown", field)

    def test_normalized_fields_are_stripped(self):
        for module, filename in CASES:
            for document in module.parse_html(fixture(filename), NOW):
                for field in TEXT_FIELDS:
                    self.assertIsInstance(document[field], str)
                    self.assertTrue(document[field])
                    self.assertEqual(document[field], document[field].strip())

    def test_ids_match_original_source_rules_including_legacy_spaces(self):
        expected = (
            "gunra_16df927965a28ae9fe26c1354c37608e",
            "black_shrantac87fb332d44b6cae1ed8231535cbf2935",
            "dragonforce_f0d42cb9c66a18ac3d475990bd7302c2",
            "bitlock_639b7836712836cf8e1a5b1f74948b2e",
        )
        for (module, filename), identity in zip(CASES, expected):
            self.assertEqual(module.parse_html(fixture(filename), NOW)[0]["_id"], identity)

    def test_blank_text_is_unknown_without_changing_existing_empty_id_inputs(self):
        changes = (
            (gunra, CASES[0][1], "Synthetic metadata listing", "", "gunra_",
             "Example Gunra | 10 GB__ 10 GB", "data_contents"),
            (black, CASES[1][1], " ZZ ", " ", "black_shrantac",
             "Example Black__20 GB", "country"),
            (dragon, CASES[2][1], "Synthetic company overview", "", "dragonforce_",
             "Example Dragon__30 GB", "description"),
            (bitlock, CASES[3][1], "Updated: 2026-01-05", "Updated:", "bitlock_",
             "bitlock-fixture.example_Synthetic publication overview_", "publication_date"),
        )
        for module, filename, before, after, prefix, old_raw_id, field in changes:
            with self.subTest(source=module.SOURCE):
                document = module.parse_html(fixture(filename).replace(before, after), NOW)[0]
                self.assertEqual(document[field], "unknown")
                self.assertEqual(document["_id"], prefix + hashlib.md5(
                    old_raw_id.encode(), usedforsecurity=False).hexdigest())

    def test_ids_are_stable_across_observation_times(self):
        for module, filename in CASES:
            first = module.parse_html(fixture(filename), NOW)
            second = module.parse_html(fixture(filename), NOW + timedelta(days=1))
            self.assertEqual([row["_id"] for row in first], [row["_id"] for row in second])
            self.assertNotEqual(first[0]["scraped_time"], second[0]["scraped_time"])

    def test_source_and_schema_are_explicit_and_only_metadata_is_stored(self):
        seen = set()
        expected_keys = set(TEXT_FIELDS) | {"_id", "scraped_time", "source", "source_url", "schema_version"}
        for module, filename in CASES:
            for document in module.parse_html(fixture(filename), NOW):
                self.assertEqual(set(document), expected_keys)
                self.assertEqual(document["source"], module.SOURCE)
                self.assertEqual(document["source_url"], module.SOURCE_URL)
                self.assertEqual(document["schema_version"], 1)
                seen.add(document["source"])
        self.assertEqual(seen, {"gunra", "black_shrantac", "dragonforce", "bitlock"})

    def test_default_timestamp_is_current_aware_utc_and_shared_within_page(self):
        before = datetime.now(timezone.utc)
        for module, filename in CASES:
            documents = module.parse_html(fixture(filename))
            times = {row["scraped_time"] for row in documents}
            self.assertEqual(len(times), 1)
            timestamp = times.pop()
            self.assertIs(timestamp.tzinfo, timezone.utc)
            self.assertGreaterEqual(timestamp, before)
            self.assertLessEqual(timestamp, datetime.now(timezone.utc))

    def test_supplied_timezone_is_converted_without_changing_instant(self):
        local_time = datetime(2026, 1, 6, 21, 30, tzinfo=timezone(timedelta(hours=9)))
        for module, filename in CASES:
            timestamp = module.parse_html(fixture(filename), local_time)[0]["scraped_time"]
            self.assertEqual(timestamp, NOW)
            self.assertIs(timestamp.tzinfo, timezone.utc)

    def test_naive_timestamp_is_rejected(self):
        for module, filename in CASES:
            with self.assertRaises(ValueError):
                module.parse_html(fixture(filename), datetime(2026, 1, 6))

    def test_malformed_middle_item_does_not_reuse_previous_document(self):
        for module, filename in CASES:
            documents = module.parse_html(fixture(filename), NOW)
            self.assertEqual(len(documents), 2)
            self.assertNotEqual(documents[0]["_id"], documents[1]["_id"])
            self.assertIn("minimal", documents[1]["company_name"].lower())

    def test_unexpected_single_item_exception_does_not_stop_following_items(self):
        parser = gunra._parse_item
        seen = 0

        def parse_item(item):
            nonlocal seen
            seen += 1
            if seen == 2:
                raise RuntimeError("synthetic card error")
            return parser(item)

        with patch.object(gunra, "_parse_item", side_effect=parse_item):
            records = gunra.parse_html(fixture("gunra_sample.html"), NOW)
        self.assertEqual(seen, 3)
        self.assertEqual([row["company_name"] for row in records], ["Example Gunra", "Minimal Gunra"])

    def test_empty_html_is_empty_and_unidentifiable_card_is_skipped(self):
        for module, _ in CASES:
            self.assertEqual(module.parse_html("<html></html>", NOW), [])
        self.assertEqual(gunra.parse_html('<div class="tile"><strong><a> </a></strong></div>', NOW), [])

    def test_bitlock_keeps_existing_fifty_successful_item_limit(self):
        invalid = '<div class="post-block good"><span>invalid</span></div>'
        cards = [f'<div class="post-block good"><div>fixture-{i}.example</div></div>' for i in range(55)]
        records = bitlock.parse_html(invalid + "".join(cards), NOW)
        self.assertEqual(len(records), 50)
        self.assertEqual(len({row["_id"] for row in records}), 50)
        self.assertEqual(records[-1]["company_url"], "fixture-49.example")

    def test_static_text_handles_inline_tags_lines_and_hidden_content(self):
        element = BeautifulSoup('<div>com<span>pany</span>.example<br>second line'
                                '<script>ignore</script><span hidden>hidden</span></div>', "html.parser").div
        self.assertEqual(models.element_text(element), "company.example\nsecond line")

    def test_parsers_work_without_runtime_environment_or_io(self):
        with patch.dict(os.environ, {}, clear=True):
            for module, filename in CASES:
                self.assertEqual(len(module.parse_html(fixture(filename), NOW)), 2)


class ImportSafetyTests(unittest.TestCase):
    def test_fresh_import_opens_no_browser_database_network_or_env_file(self):
        code = '''
from contextlib import ExitStack
import importlib
import sys
from unittest.mock import patch
with ExitStack() as stack:
    stack.enter_context(patch.dict(sys.modules, {"webdriver_manager": None, "webdriver_manager.chrome": None}))
    mocks = [stack.enter_context(patch(target, side_effect=AssertionError("Import I/O")))
             for target in ("selenium.webdriver.Chrome", "pymongo.MongoClient", "dotenv.load_dotenv",
                            "socket.socket.connect", "socket.getaddrinfo")]
    for name in ("crawling", "crawling.models", "crawling.common", "crawling.gunra_crawler",
                 "crawling.Black_Shrantac_crawler", "crawling.dragonforce_crawler", "crawling.bitlock_crawler"):
        importlib.import_module(name)
    for mock in mocks:
        mock.assert_not_called()
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"DB_URI": "mongodb://db.invalid", "DB_NAME": "test_metadata"}, clear=True))
        self.dotenv = self.stack.enter_context(patch("crawling.common.load_dotenv"))

    def test_defaults_and_project_env_path(self):
        config = common.load_config()
        self.dotenv.assert_called_once_with(ROOT / ".env", override=False)
        self.assertEqual((config.tor_socks_proxy, config.headless, config.page_timeout_seconds, config.wait_seconds),
                         ("socks5://127.0.0.1:9150", True, 60, 20))
        self.assertNotIn(config.db_uri, repr(config))

    def test_valid_overrides_and_chrome_options(self):
        with patch.dict(os.environ, {"TOR_SOCKS_PROXY": "socks5://127.0.0.1:9050", "CRAWLER_HEADLESS": "False",
                                   "CRAWLER_PAGE_TIMEOUT_SECONDS": "45", "CRAWLER_WAIT_SECONDS": "8"}):
            config = common.load_config()
        self.assertEqual((config.headless, config.page_timeout_seconds, config.wait_seconds), (False, 45, 8))
        self.assertEqual(common.chrome_options(config).arguments, ["--proxy-server=socks5://127.0.0.1:9050"])

    def test_invalid_config_is_rejected_without_resource_creation(self):
        cases = {"DB_URI": ["", "https://db.invalid", "mongodb://"], "DB_NAME": ["", "bad/name"],
                 "TOR_SOCKS_PROXY": ["", "http://127.0.0.1:9150", "socks5://127.0.0.1", "socks5://127.0.0.1:0",
                                     "socks5://127.0.0.1:65536", "socks5://127.0.0.1:9150/path"],
                 "CRAWLER_HEADLESS": ["maybe"], "CRAWLER_PAGE_TIMEOUT_SECONDS": ["0", "-1", "abc"],
                 "CRAWLER_WAIT_SECONDS": ["0", "1.5"]}
        with patch("crawling.common.webdriver.Chrome") as chrome, patch("crawling.common.MongoClient") as mongo:
            for name, values in cases.items():
                for value in values:
                    with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                        with self.assertRaises(common.CrawlerConfigError):
                            common.load_config()
            chrome.assert_not_called()
            mongo.assert_not_called()


class CrawlerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("Unexpected network")))
        self.config = common.CrawlerConfig(db_uri="mongodb://db.invalid", db_name="test_metadata")
        self.service = self.stack.enter_context(patch("crawling.common.driver_service"))
        self.chrome = self.stack.enter_context(patch("crawling.common.webdriver.Chrome"))
        self.driver = self.chrome.return_value
        self.driver.find_elements.return_value = [object()]
        self.mongo = self.stack.enter_context(patch("crawling.common.MongoClient"))
        self.client = self.mongo.return_value
        self.collection = MagicMock()
        self.history = MagicMock()
        self.client.__getitem__.return_value.__getitem__.side_effect = {
            "leaked_data": self.collection, "leak_history": self.history,
        }.__getitem__
        self.collection.find_one.return_value = None
        self.collection.find.return_value = []
        self.collection.update_one.return_value.upserted_id = "synthetic_insert"
        self.stack.enter_context(patch("crawling.common.LOGGER"))

    def test_all_crawlers_upsert_only_valid_items_and_close_resources(self):
        for module, filename in CASES:
            with self.subTest(source=module.SOURCE):
                self.chrome.reset_mock()
                self.mongo.reset_mock()
                self.collection.reset_mock()
                self.history.reset_mock()
                self.driver.page_source = fixture(filename)
                saved = module.crawl(self.config)
                self.assertEqual(saved, 2)
                self.chrome.assert_called_once()
                self.driver.get.assert_called_once_with(module.SOURCE_URL)
                self.driver.find_elements.assert_called_once_with("css selector", module.ITEM_SELECTOR)
                self.driver.set_page_load_timeout.assert_called_once_with(60)
                self.driver.quit.assert_called_once()
                self.client.close.assert_called_once()
                self.assertTrue(self.mongo.call_args.kwargs["tz_aware"])
                self.assertIs(self.mongo.call_args.kwargs["tzinfo"], timezone.utc)
                self.assertEqual(self.collection.update_one.call_count, 2)
                ids = set()
                for call in self.collection.update_one.call_args_list:
                    query, update = call.args
                    record = update["$setOnInsert"]
                    ids.add(record["_id"])
                    self.assertEqual(query, {"event_key": record["event_key"]})
                    self.assertEqual(call.kwargs, {"upsert": True})
                    self.assertEqual(record["source"], module.SOURCE)
                    self.assertIs(record["scraped_time"].tzinfo, timezone.utc)
                    self.assertNotIn("html", record)
                    self.assertEqual(record["schema_version"], 2)
                    self.assertEqual(record["observation_count"], 1)
                    self.assertEqual(record["first_seen"], record["last_seen"])
                self.assertEqual(len(ids), 2)
                self.history.update_one.assert_not_called()

    def test_options_are_set_before_single_dragonforce_driver_creation(self):
        self.driver.page_source = fixture("dragonforce_sample.html")

        def make_driver(*, service, options):
            self.assertIn("--proxy-server=socks5://127.0.0.1:9150", options.arguments)
            self.assertIn("--headless=new", options.arguments)
            return self.driver

        self.chrome.side_effect = make_driver
        dragon.crawl(self.config)
        self.chrome.assert_called_once()
        self.service.assert_called_once()

    def test_page_timeout_closes_driver_and_never_opens_database(self):
        self.driver.get.side_effect = TimeoutException("synthetic timeout")
        with self.assertRaises(TimeoutException):
            gunra.crawl(self.config)
        self.driver.quit.assert_called_once()
        self.mongo.assert_not_called()

    def test_wait_timeout_is_propagated_and_closes_driver(self):
        with patch("crawling.common.WebDriverWait") as wait:
            wait.return_value.until.side_effect = TimeoutException("synthetic wait timeout")
            with self.assertRaises(TimeoutException):
                gunra.crawl(self.config)
            wait.assert_called_once_with(self.driver, 20)
        self.driver.quit.assert_called_once()
        self.mongo.assert_not_called()

    def test_timeout_configuration_failure_still_closes_created_driver(self):
        self.driver.set_page_load_timeout.side_effect = WebDriverException("synthetic failure")
        with self.assertRaises(WebDriverException):
            gunra.crawl(self.config)
        self.driver.quit.assert_called_once()
        self.mongo.assert_not_called()

    def test_parser_failure_closes_driver_without_opening_database(self):
        with patch.object(gunra, "parse_html", side_effect=ValueError("synthetic parser failure")):
            with self.assertRaises(ValueError):
                gunra.crawl(self.config)
        self.driver.quit.assert_called_once()
        self.mongo.assert_not_called()

    def test_empty_parser_result_does_not_open_database(self):
        self.driver.page_source = "<html></html>"
        self.assertEqual(gunra.crawl(self.config), 0)
        self.driver.quit.assert_called_once()
        self.mongo.assert_not_called()

    def test_database_failure_closes_client_after_driver_has_closed(self):
        self.driver.page_source = fixture("gunra_sample.html")

        def fail_write(*args, **kwargs):
            self.driver.quit.assert_called_once()
            raise PyMongoError("synthetic database failure")

        self.collection.update_one.side_effect = fail_write
        with self.assertRaises(PyMongoError):
            gunra.crawl(self.config)
        self.client.close.assert_called_once()

    def test_collection_selection_failure_still_closes_client(self):
        self.driver.page_source = fixture("gunra_sample.html")
        self.client.__getitem__.side_effect = PyMongoError("synthetic selection failure")
        with self.assertRaises(PyMongoError):
            gunra.crawl(self.config)
        self.client.close.assert_called_once()
        self.driver.quit.assert_called_once()

    def test_history_write_failure_closes_client_without_updating_current_document(self):
        self.driver.page_source = fixture("gunra_sample.html")
        previous = gunra.parse_html(self.driver.page_source, NOW)[0]
        previous.update(data_size="5 GB", observation_count=1, first_seen=NOW, schema_version=2)
        previous["event_key"], previous["identity_basis"] = storage.event_identity(previous)
        self.collection.find_one.return_value = previous
        self.history.update_one.side_effect = PyMongoError("synthetic history failure")
        with self.assertRaises(PyMongoError):
            gunra.crawl(self.config)
        self.client.close.assert_called_once()
        self.driver.quit.assert_called_once()
        self.history.update_one.assert_called_once()
        self.collection.update_one.assert_not_called()

    def test_save_records_preserves_parser_id_and_adds_storage_fields_without_mutating_input(self):
        record = models.LeakRecord(_id="synthetic_id", scraped_time=NOW, source="test", source_url="https://fixture.example")
        self.assertEqual(common.save_records(self.collection, self.history, [record]), 1)
        self.collection.update_one.assert_called_once()
        call = self.collection.update_one.call_args
        document = call.args[1]["$setOnInsert"]
        self.assertEqual(call.args[0], {"event_key": document["event_key"]})
        self.assertEqual(call.kwargs, {"upsert": True})
        self.assertEqual(document["_id"], "synthetic_id")
        for name, value in record.to_document().items():
            if name != "schema_version":
                self.assertEqual(document[name], value)
        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(document["observation_count"], 1)
        self.assertEqual(document["first_seen"], NOW)
        self.assertEqual(document["last_seen"], NOW)
        self.history.update_one.assert_not_called()
        self.assertEqual(record.scraped_time, NOW)
        self.assertEqual(record.schema_version, 1)

    def test_mains_return_nonzero_and_hide_raw_exception_details(self):
        cases = ((TimeoutException("private metadata"), "TIMEOUT"),
                 (WebDriverException("private metadata"), "BROWSER_ERROR"),
                 (PyMongoError("private metadata"), "DB_ERROR"),
                 (RuntimeError("private metadata"), "CRAWLER_ERROR"))
        for module, _ in CASES:
            for error, code in cases:
                stderr = io.StringIO()
                with patch.object(module, "crawl", side_effect=error), redirect_stderr(stderr):
                    self.assertEqual(module.main(), 1)
                self.assertIn(code, stderr.getvalue())
                self.assertNotIn("private metadata", stderr.getvalue())

    def test_mains_report_saved_count_without_printing_records(self):
        for module, _ in CASES:
            stdout = io.StringIO()
            with patch.object(module, "crawl", return_value=2), redirect_stdout(stdout):
                self.assertEqual(module.main(), 0)
            self.assertEqual(stdout.getvalue(), f"{module.SOURCE}: upserted 2 metadata records\n")


if __name__ == "__main__":
    unittest.main()
