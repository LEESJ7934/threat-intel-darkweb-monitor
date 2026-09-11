"""Read-only counts and explicitly confirmed document retention. No TTL/cascade."""
from . import audit
from .policy import (COLLECTIONS, RETENTION_SETTINGS, PolicyError, RetentionRule,
                     cutoff, prohibited_fields, utc_now)

TIMEOUT_MS = 5000


def validate_rules(rules):
    rows = tuple(rules)
    if (len(rows) != 3 or any(not isinstance(row, RetentionRule) for row in rows)
            or {row.collection for row in rows} != set(COLLECTIONS)):
        raise PolicyError("retention_collection_allowlist_required")
    for row in rows:
        expected = RETENTION_SETTINGS[row.collection]
        if (not isinstance(row, RetentionRule) or type(row.days) is not int or row.days <= 0
                or (row.primary, row.fallback) != expected[2:]):
            raise PolicyError("invalid_retention_rule")
    return rows


def timestamp_expression(rule):
    primary = "$" + rule.primary
    if not rule.fallback:
        return primary
    # Fall back only if absent/null, never when the preferred value is invalid.
    return {"$cond": [{"$in": [{"$type": primary}, ["missing", "null"]]},
                      "$" + rule.fallback, primary]}


def queries(rule, now):
    stamp = timestamp_expression(rule)
    is_date = {"$eq": [{"$type": stamp}, "date"]}
    missing = {"$in": [{"$type": stamp}, ["missing", "null"]]}
    fields = ["$" + name for name in (rule.primary, rule.fallback) if name]
    valid_types = {"$and": [{"$in": [{"$type": value}, ["date", "missing", "null"]]} for value in fields]}
    future = {"$or": [{"$and": [{"$eq": [{"$type": value}, "date"]},
                                {"$gt": [value, utc_now(now)]}]} for value in fields]}
    # Aggregation $type tests a scalar; the query $type operator also matches
    # date elements inside arrays and would be unsafe for document deletion.
    return {
        "expired": {"$expr": {"$and": [is_date, valid_types, {"$not": [future]},
                                        {"$lt": [stamp, cutoff(rule, now)]}]}},
        "future": {"$expr": future},
        "missing": {"$expr": missing},
        "invalid": {"$expr": {"$not": [valid_types]}},
    }


def forbidden_count(collection, collection_name):
    # Read field names only on the server; never return whole documents to the CLI.
    names = {"$map": {"input": {"$objectToArray": "$$ROOT"}, "as": "entry",
                       "in": {"$toLower": "$$entry.k"}}}
    expression = {"$gt": [{"$size": {"$setIntersection": [
        names, sorted(prohibited_fields(collection_name))]}}, 0]}
    return collection.count_documents({"$expr": expression}, maxTimeMS=TIMEOUT_MS)


def inspect_retention(database, rules, *, now=None, check_fields=False):
    rows, now = validate_rules(rules), utc_now(now)
    report = {}
    for rule in rows:
        collection = database[rule.collection]
        counts = {name: collection.count_documents(query, maxTimeMS=TIMEOUT_MS)
                  for name, query in queries(rule, now).items()}
        if check_fields:
            counts["prohibited"] = forbidden_count(collection, rule.collection)
        report[rule.collection] = counts
    return report


def confirm_apply(configured_name, database_name, confirmation):
    # Require explicit exact values; no trimming, inferred name or --force.
    if not configured_name or not database_name or database_name != configured_name or confirmation != configured_name:
        raise PolicyError("database_confirmation_required_or_mismatched")
    if configured_name.casefold() in {"admin", "local", "config", "monstache"}:
        raise PolicyError("protected_database")


def execute_retention(database, rules, *, apply=False, configured_name=None,
                      database_name=None, confirmation=None, now=None):
    rows, now = validate_rules(rules), utc_now(now)
    if apply:
        confirm_apply(configured_name, database_name, confirmation)
        if database.name != configured_name:
            raise PolicyError("connected_database_mismatch")
    audit.emit("retention_execution", category="retention", result="started" if apply else "dry_run")
    try:
        # All counts succeed before the first write. Every delete re-evaluates
        # its date predicate on MongoDB, so a recent concurrent update survives.
        report = inspect_retention(database, rows, now=now)
        if apply:
            for rule in rows:
                outcome = database[rule.collection].delete_many(queries(rule, now)["expired"])
                report[rule.collection]["deleted"] = outcome.deleted_count
                audit.emit("retention_execution", category="retention", result="deleted",
                           collection=rule.collection, count=outcome.deleted_count)
        audit.emit("retention_execution", category="retention", result="success" if apply else "dry_run")
        return report
    except Exception:
        audit.emit("retention_execution", category="retention", result="error")
        raise  # Entry points print only fixed categories, not this exception.
