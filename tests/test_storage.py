"""Offline stateful tests for Day 9. Never creates a real MongoClient."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bson import ObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError

from crawling import storage
from crawling.models import METADATA_FIELDS

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(days=1)


def observation(**overrides):
    record = {"_id": "bitlock_" + "a" * 32, "source": "bitlock", "schema_version": 1,
              "scraped_time": NOW, "company_name": "Example Company", "company_url": "https://company.example/",
              "country": "ZZ", "data_contents": "Synthetic metadata", "data_size": "10 GB",
              "publication_date": "2026-01-01", "description": "Synthetic company overview",
              "source_url": "http://source.example/"}
    record.update(overrides)
    return record


class MemoryCollection:
    """Small Mongo contract fake: projection, upsert, $inc and unique indexes.

    Unsupported operations fail so the tests cannot silently accept a new query.
    """
    def __init__(self, documents=(), *, name="current", events=None):
        self.documents = {doc["_id"]: deepcopy(doc) for doc in documents}
        self.name = name
        self.events = events if events is not None else []
        self.indexes = []
        self.unique_event_key = False
        self.fail_next_update = False
        self.before_update = None

    def _matches(self, document, query):
        for key, condition in query.items():
            if key == "$or":
                if not any(self._matches(document, clause) for clause in condition):
                    return False
            elif isinstance(condition, dict):
                if set(condition) - {"$regex", "$options"}:
                    raise AssertionError("Unsupported test query")
                value = document.get(key)
                flags = re.IGNORECASE if condition.get("$options") == "i" else 0
                if not isinstance(value, str) or not re.search(condition["$regex"], value, flags):
                    return False
            elif document.get(key) != condition:
                return False
        return True

    def _project(self, document, projection):
        if projection is None:
            return deepcopy(document)
        return deepcopy({key: value for key, value in document.items() if projection.get(key) or key == "_id"})

    def find(self, query, projection=None):
        return [self._project(doc, projection) for doc in self.documents.values() if self._matches(doc, query)]

    def find_one(self, query, projection=None):
        return next(iter(self.find(query, projection)), None)

    def _check_unique(self, document, excluding=None):
        for identity, other in self.documents.items():
            if identity == excluding:
                continue
            if identity == document["_id"]:
                raise DuplicateKeyError("synthetic duplicate document ID")
            if (self.unique_event_key and isinstance(document.get("event_key"), str)
                    and other.get("event_key") == document["event_key"]):
                raise DuplicateKeyError("synthetic duplicate event key")

    def create_index(self, keys, **options):
        self.indexes.append((deepcopy(keys), deepcopy(options)))
        self.unique_event_key = True
        for document in self.documents.values():
            self._check_unique(document, excluding=document["_id"])
        return options["name"]

    def update_one(self, query, update, upsert=False):
        self.events.append((self.name, deepcopy(query), deepcopy(update), upsert))
        if self.before_update is not None:
            callback, self.before_update = self.before_update, None
            callback()
        if self.fail_next_update:
            self.fail_next_update = False
            raise PyMongoError("synthetic write failure")
        if set(update) - {"$set", "$setOnInsert", "$inc"}:
            raise AssertionError("Unsupported test update")
        existing = self.find_one(query)
        if existing is None and not upsert:
            return SimpleNamespace(matched_count=0, upserted_id=None)
        if existing is None:
            document = {key: value for key, value in query.items() if not isinstance(value, dict)}
            document.update(deepcopy(update.get("$setOnInsert", {})))
        else:
            document = existing
        document.update(deepcopy(update.get("$set", {})))
        for key, amount in update.get("$inc", {}).items():
            document[key] = document.get(key, 0) + amount
        if existing is not None and document["_id"] != existing["_id"]:
            raise AssertionError("Mongo _id was rewritten")
        self._check_unique(document, excluding=existing["_id"] if existing else None)
        self.documents[document["_id"]] = deepcopy(document)
        return SimpleNamespace(matched_count=int(existing is not None),
                               upserted_id=document["_id"] if existing is None else None)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ("pymongo.MongoClient", "socket.socket.connect", "socket.getaddrinfo"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("Unexpected external I/O")))
        self.events = []
        self.current = MemoryCollection(events=self.events)
        self.history = MemoryCollection(name="history", events=self.events)

    def save(self, record):
        return storage.save_records(self.current, self.history, [record])

    def current_document(self):
        self.assertEqual(len(self.current.documents), 1)
        return deepcopy(next(iter(self.current.documents.values())))

    def test_description_size_updated_and_other_mutable_fields_do_not_change_identity(self):
        original = observation()
        key = storage.event_identity(original)
        for field in ("description", "data_size", "publication_date", "country", "data_contents", "source_url"):
            with self.subTest(field=field):
                self.assertEqual(storage.event_identity({**original, field: "changed"}), key)

    def test_sources_are_distinct_even_for_the_same_company(self):
        first = observation()
        second = observation(_id="gunra_" + "b" * 32, source="gunra")
        self.assertNotEqual(storage.event_identity(first), storage.event_identity(second))
        self.save(first)
        self.save(second)
        self.assertEqual(len(self.current.documents), 2)

    def test_company_url_has_priority_over_company_name_and_legacy_id(self):
        key, basis = storage.event_identity(observation())
        self.assertEqual(basis, "source+company_url")
        self.assertEqual(storage.event_identity(observation(company_name="Different name", _id="different-id"))[0], key)

    def test_identity_canonicalizes_source_scheme_hostname_slash_and_name_whitespace(self):
        key = storage.event_identity(observation())
        self.assertEqual(storage.event_identity(observation(source=" BITLOCK ", company_url=" COMPANY.EXAMPLE/ ")), key)
        self.assertEqual(storage.event_identity(observation(company_url="http://COMPANY.EXAMPLE")), key)
        first = observation(company_url="unknown", company_name="  Example   Company ")
        second = observation(company_url="UNKNOWN", company_name="example company", _id="other-id")
        self.assertEqual(storage.event_identity(first), storage.event_identity(second))

    def test_url_canonicalization_keeps_path_case_www_port_and_query_distinct(self):
        base = storage.event_identity(observation(company_url="company.example/Path"))[0]
        for url in ("company.example/path", "www.company.example/Path", "company.example:8443/Path", "company.example/Path?q=1"):
            self.assertNotEqual(storage.event_identity(observation(company_url=url))[0], base)

    def test_unknown_url_falls_back_to_company_name_then_legacy_id(self):
        by_name = observation(company_url="unknown")
        self.assertEqual(storage.event_identity(by_name)[1], "source+company_name")
        by_id = observation(company_url="unknown", company_name="unknown")
        self.assertEqual(storage.event_identity(by_id)[1], "source+legacy_id")
        self.assertNotEqual(storage.event_identity(by_id), storage.event_identity({**by_id, "_id": "other-id"}))

    def test_fingerprint_excludes_all_bookkeeping_and_identity_fields(self):
        original = observation()
        changed = {**original, "_id": "other-id", "event_key": "ignored", "identity_basis": "ignored",
                   "first_seen": NOW, "last_seen": LATER, "scraped_time": LATER,
                   "observation_count": 50, "content_fingerprint": "ignored", "schema_version": 99}
        self.assertEqual(storage.content_fingerprint(original), storage.content_fingerprint(changed))

    def test_fingerprint_covers_every_declared_metadata_field(self):
        original = observation()
        for field in METADATA_FIELDS:
            value = "https://different.example" if field.endswith("url") else "changed"
            self.assertNotEqual(storage.content_fingerprint(original), storage.content_fingerprint({**original, field: value}), field)

    def test_new_insert_preserves_parser_id_and_initializes_v2(self):
        incoming = observation()
        self.assertEqual(self.save(incoming), 1)
        saved = self.current_document()
        self.assertEqual(saved["_id"], incoming["_id"])
        self.assertEqual((saved["first_seen"], saved["last_seen"], saved["scraped_time"]), (NOW, NOW, NOW))
        self.assertEqual(saved["observation_count"], 1)
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["event_key"], storage.event_identity(incoming)[0])
        self.assertEqual(saved["content_fingerprint"], storage.content_fingerprint(incoming))
        self.assertEqual(len(self.history.documents), 0)

    def test_identical_observation_keeps_one_document_and_no_history(self):
        self.save(observation())
        self.save(observation(scraped_time=LATER))
        saved = self.current_document()
        self.assertEqual(saved["first_seen"], NOW)
        self.assertEqual((saved["last_seen"], saved["scraped_time"]), (LATER, LATER))
        self.assertEqual(saved["observation_count"], 2)
        self.assertEqual(self.history.documents, {})

    def test_different_legacy_id_dedupes_the_same_event(self):
        original = observation()
        self.save(original)
        self.save(observation(_id="bitlock_" + "b" * 32, scraped_time=LATER))
        self.assertEqual(self.current_document()["_id"], original["_id"])
        self.assertEqual(self.current_document()["observation_count"], 2)

    def test_changed_metadata_updates_existing_id_and_writes_exact_changes(self):
        self.save(observation())
        self.save(observation(_id="different-observation-id", data_size="15 GB", scraped_time=LATER))
        saved = self.current_document()
        self.assertEqual(saved["_id"], observation()["_id"])
        self.assertEqual(saved["data_size"], "15 GB")
        self.assertEqual(saved["first_seen"], NOW)
        self.assertEqual(saved["observation_count"], 2)
        self.assertEqual(len(self.history.documents), 1)
        history = next(iter(self.history.documents.values()))
        self.assertEqual(history["changes"], {"data_size": {"before": "10 GB", "after": "15 GB"}})
        self.assertEqual(history["event_key"], saved["event_key"])
        self.assertEqual(history["document_id"], saved["_id"])
        self.assertEqual(history["changed_at"], LATER)
        self.assertEqual(history["schema_version"], 2)
        self.assertRegex(history["_id"], r"^[0-9a-f]{64}$")

    def test_repeating_the_same_change_does_not_duplicate_history(self):
        self.save(observation())
        changed = observation(description="New synthetic overview", scraped_time=LATER)
        self.save(changed)
        self.save(changed)
        self.assertEqual(len(self.history.documents), 1)
        self.assertEqual(self.current_document()["observation_count"], 3)

    def test_history_is_written_first_and_current_failure_retry_is_deduplicated(self):
        self.save(observation())
        before = self.current_document()
        self.events.clear()
        self.current.fail_next_update = True
        changed = observation(data_size="15 GB", scraped_time=LATER)
        with self.assertRaises(PyMongoError):
            self.save(changed)
        self.assertEqual([event[0] for event in self.events], ["history", "current"])
        self.assertEqual(self.current_document(), before)
        self.assertEqual(len(self.history.documents), 1)
        history_before = deepcopy(self.history.documents)
        self.save(changed)
        self.assertEqual(self.history.documents, history_before)
        self.assertEqual(self.current_document()["data_size"], "15 GB")
        self.assertEqual(self.current_document()["observation_count"], 2)

    def test_history_failure_leaves_current_unchanged_until_retry(self):
        self.save(observation())
        before = self.current_document()
        self.history.fail_next_update = True
        changed = observation(data_size="15 GB", scraped_time=LATER)
        with self.assertRaises(PyMongoError):
            self.save(changed)
        self.assertEqual(self.current_document(), before)
        self.assertEqual(self.history.documents, {})
        self.save(changed)
        self.assertEqual(len(self.history.documents), 1)

    def test_unknown_never_overwrites_known_metadata_or_generates_history(self):
        self.save(observation())
        degraded = observation(scraped_time=LATER, **dict.fromkeys(METADATA_FIELDS, " UNKNOWN "))
        self.save(degraded)
        saved = self.current_document()
        for field in METADATA_FIELDS:
            self.assertEqual(saved[field], observation()[field])
        self.assertEqual(self.history.documents, {})

    def test_unknown_to_known_enrichment_is_a_real_change(self):
        self.save(observation(data_size="unknown"))
        self.save(observation(data_size="10 GB", scraped_time=LATER))
        history = next(iter(self.history.documents.values()))
        self.assertEqual(history["changes"], {"data_size": {"before": "unknown", "after": "10 GB"}})

    def test_missing_url_does_not_demote_known_identity_even_with_new_legacy_id(self):
        self.save(observation())
        key = self.current_document()["event_key"]
        self.save(observation(company_url="unknown", _id="different-legacy-id", scraped_time=LATER))
        self.assertEqual(self.current_document()["event_key"], key)
        self.assertEqual(self.current_document()["identity_basis"], "source+company_url")
        self.assertEqual(self.history.documents, {})

    def test_url_enrichment_promotes_fallback_without_rewriting_mongo_id(self):
        self.save(observation(company_url="unknown"))
        old_key = self.current_document()["event_key"]
        self.save(observation(_id="new-observation-id", scraped_time=LATER))
        saved = self.current_document()
        self.assertEqual(saved["_id"], observation()["_id"])
        self.assertNotEqual(saved["event_key"], old_key)
        self.assertEqual(saved["event_key"], storage.event_identity(observation())[0])
        self.assertEqual(saved["identity_basis"], "source+company_url")
        self.assertEqual(len(self.history.documents), 1)
        self.assertEqual(next(iter(self.history.documents.values()))["document_id"], saved["_id"])

    def test_schema_v1_same_id_lazy_upgrade_preserves_first_seen_and_existing_count(self):
        legacy = observation(first_seen=NOW - timedelta(days=10), observation_count=7)
        self.current = MemoryCollection([legacy], events=self.events)
        self.save(observation(scraped_time=LATER))
        saved = self.current_document()
        self.assertEqual(saved["_id"], legacy["_id"])
        self.assertEqual(saved["first_seen"], legacy["first_seen"])
        self.assertEqual(saved["observation_count"], 8)
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(self.history.documents, {})

    def test_v1_without_source_uses_legacy_prefix_and_canonical_url_to_find_old_id(self):
        legacy = observation(company_url="COMPANY.EXAMPLE", scraped_time=NOW.replace(tzinfo=None))
        del legacy["source"]
        self.current = MemoryCollection([legacy], events=self.events)
        self.save(observation(_id="bitlock_" + "b" * 32, scraped_time=LATER))
        saved = self.current_document()
        self.assertEqual(saved["_id"], legacy["_id"])
        self.assertEqual(saved["source"], "bitlock")
        self.assertEqual(saved["first_seen"], NOW)
        self.assertEqual(saved["observation_count"], 2)

    def test_legacy_name_fallback_is_casefolded_and_source_scoped(self):
        legacy = observation(_id="old-id", company_url="unknown", company_name="EXAMPLE   COMPANY")
        self.current = MemoryCollection([legacy], events=self.events)
        self.save(observation(_id="new-id", company_url="unknown", scraped_time=LATER))
        self.assertEqual(self.current_document()["_id"], "old-id")

    def test_known_different_company_urls_are_not_merged_by_matching_names(self):
        self.save(observation())
        self.save(observation(_id="other-id", company_url="https://other-company.example"))
        self.assertEqual(len(self.current.documents), 2)

    def test_ambiguous_legacy_matches_fail_without_creating_or_rewriting_documents(self):
        old = [observation(_id="old-one"), observation(_id="old-two")]
        self.current = MemoryCollection(old, events=self.events)
        before = deepcopy(self.current.documents)
        with self.assertRaises(storage.AmbiguousLegacyMatch):
            self.save(observation(_id="incoming-new-id"))
        self.assertEqual(self.current.documents, before)
        self.assertEqual(self.history.documents, {})

    def test_legacy_id_collision_cannot_merge_different_sources(self):
        self.current = MemoryCollection([observation(source="gunra")], events=self.events)
        with self.assertRaises(storage.IdentityConflict):
            self.save(observation())
        self.assertEqual(self.current_document()["source"], "gunra")

    def test_legacy_object_id_is_preserved(self):
        identity = ObjectId("000000000000000000000001")
        self.current = MemoryCollection([observation(_id=identity)], events=self.events)
        self.save(observation(scraped_time=LATER))
        self.assertEqual(self.current_document()["_id"], identity)

    def test_all_saved_timestamps_are_aware_utc(self):
        local = NOW.astimezone(timezone(timedelta(hours=9)))
        self.save(observation(scraped_time=local))
        self.save(observation(description="Changed", scraped_time=local + timedelta(days=1)))
        for field in ("first_seen", "last_seen", "scraped_time"):
            self.assertIs(self.current_document()[field].tzinfo, timezone.utc)
        self.assertIs(next(iter(self.history.documents.values()))["changed_at"].tzinfo, timezone.utc)
        self.assertEqual(self.current_document()["first_seen"], NOW)

    def test_bad_legacy_time_uses_incoming_time_without_offset_guessing(self):
        self.current = MemoryCollection([observation(scraped_time="bad", first_seen="not-a-date")], events=self.events)
        self.save(observation(scraped_time=LATER))
        self.assertEqual(self.current_document()["first_seen"], LATER)

    def test_naive_incoming_time_is_rejected(self):
        with self.assertRaises(ValueError):
            self.save(observation(scraped_time=NOW.replace(tzinfo=None)))
        self.assertEqual(self.current.documents, {})

    def test_input_dict_and_nested_extras_are_not_mutated(self):
        incoming = observation(extra={"nested": ["synthetic"]})
        before = deepcopy(incoming)
        storage.event_identity(incoming)
        storage.content_fingerprint(incoming)
        self.save(incoming)
        self.assertEqual(incoming, before)

    def test_raw_html_secrets_and_forged_bookkeeping_are_not_stored(self):
        record = observation(raw_html="<html>synthetic raw document</html>", api_key="synthetic-not-a-secret",
                             DB_URI="unused", event_key="forged", first_seen=LATER, observation_count=900)
        self.save(record)
        saved = self.current_document()
        self.assertNotIn("raw_html", saved)
        self.assertNotIn("api_key", saved)
        self.assertNotIn("DB_URI", saved)
        self.assertNotEqual(saved["event_key"], "forged")
        self.assertEqual(saved["observation_count"], 1)
        self.assertEqual(saved["first_seen"], NOW)
        self.save({**record, "description": "Changed"})
        self.assertEqual(set(next(iter(self.history.documents.values()))["changes"]), {"description"})

    def test_url_credentials_are_not_saved(self):
        self.save(observation(company_url="https://fixture-user:fixture-pass@company.example",
                              source_url="https://fixture-user:fixture-pass@source.example"))
        saved = self.current_document()
        self.assertEqual(saved["company_url"], "unknown")
        self.assertEqual(saved["source_url"], "unknown")
        self.assertEqual(saved["identity_basis"], "source+company_name")

    def test_partial_unique_index_does_not_require_v1_rewrite(self):
        old = [observation(_id="old-one", company_url="one.example"), observation(_id="old-two", company_url="two.example")]
        self.current = MemoryCollection(old, events=self.events)
        self.save(observation())
        keys, options = self.current.indexes[0]
        self.assertEqual(keys, [("event_key", 1)])
        self.assertTrue(options["unique"])
        self.assertEqual(options["partialFilterExpression"], {"event_key": {"$type": "string"}})
        for document in old:
            self.assertEqual(self.current.documents[document["_id"]], document)

    def test_concurrent_event_insert_is_read_and_counted_without_duplicate_document(self):
        competing = observation(_id="competing-id")
        key, basis = storage.event_identity(competing)
        other = {**competing, "event_key": key, "identity_basis": basis, "schema_version": 2,
                 "first_seen": NOW, "last_seen": NOW, "observation_count": 1,
                 "content_fingerprint": storage.content_fingerprint(competing)}

        def insert_competing_document():
            self.current.documents[other["_id"]] = deepcopy(other)
            raise DuplicateKeyError("synthetic insert race")

        self.current.before_update = insert_competing_document
        self.save(observation(scraped_time=LATER))
        self.assertEqual(self.current_document()["_id"], "competing-id")
        self.assertEqual(self.current_document()["observation_count"], 2)
        self.assertEqual(self.history.documents, {})

    def test_no_records_means_no_writes_or_index_operations(self):
        self.assertEqual(storage.save_records(self.current, self.history, []), 0)
        self.assertEqual(self.events, [])
        self.assertEqual(self.current.indexes, [])


if __name__ == "__main__":
    unittest.main()
