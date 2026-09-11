"""Pure metadata and retention policies; these are project defaults, not law."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from types import MappingProxyType

from crawling.models import METADATA_FIELDS, UNKNOWN, metadata_text, normalized_metadata

# Reuse the crawler allowlist; limits do not add a second collection field list.
TEXT_LIMITS = MappingProxyType(dict(zip(METADATA_FIELDS, (512, 2048, 128, 2000, 128, 128, 8000, 2048))))
SOURCE_LIMIT = 128
PROHIBITED_FIELDS = frozenset({
    "raw_html", "html", "password", "passwd", "credential", "credentials",
    "cookie", "cookies", "session", "session_id", "telegram_token", "db_uri",
    "raw_file", "attachments", "captcha_token", "private_key",
})
PRIVATE_ANALYTICS_FIELDS = frozenset({"message", "claim_token", "resume_token"})
COLLECTIONS = ("leaked_data", "leak_history", "alert_log")
RETENTION_SETTINGS = MappingProxyType({
    "leaked_data": ("GOV_EVENT_RETENTION_DAYS", 365, "last_seen", "scraped_time"),
    "leak_history": ("GOV_HISTORY_RETENTION_DAYS", 365, "changed_at", None),
    "alert_log": ("GOV_ALERT_RETENTION_DAYS", 180, "updated_at", "created_at"),
})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class PolicyError(ValueError):
    """Messages contain fixed setting/field names only."""


def redact_credentials(value, *, secrets=()):
    """Conservative credential redaction, not a complete PII/DLP detector."""
    if not isinstance(value, str):
        return UNKNOWN
    for secret in sorted((str(item) for item in secrets if item), key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    # Remove an entire key block, or from an unmatched header to end-of-text.
    value = re.sub(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----.*?"
                   r"(?:-----END (?:[A-Z0-9]+ )?PRIVATE KEY-----|$)",
                   "[redacted]", value, flags=re.I | re.S)
    value = re.sub(r"mongodb(?:\+srv)?://[^\s<>\"']+", "[redacted]", value, flags=re.I)
    value = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", "[redacted]", value)
    value = re.sub(r"\b[a-z][a-z0-9+.-]*://[^\s/@<>]+@[^\s<>\"']+",
                   "[redacted]", value, flags=re.I)
    # Includes quoted multiword values and JSON-like assignments. Do not match
    # bare mentions such as "token-based service" or "password policy".
    value = re.sub(
        r"\b(?:password|passwd|token|api[_-]?key|secret)\b[\"']?\s*[:=]\s*"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;<>]+)", "[redacted]", value, flags=re.I)
    return value


def bounded_metadata(record):
    """Storage-only limits, after the parser has already produced its legacy ID.

    Reject overlong identity text instead of truncating two companies to the
    same identity. Other metadata is redacted then capped. No input mutation.
    """
    values = normalized_metadata(record)
    result = {}
    for name in METADATA_FIELDS:
        value = metadata_text(redact_credentials(values[name]))
        if name in {"company_name", "company_url"} and len(value) > TEXT_LIMITS[name]:
            raise PolicyError("identity_metadata_too_long:" + name)
        # A credential-bearing company name is not a usable company identity.
        if name == "company_name" and value != values[name]:
            value = UNKNOWN
        result[name] = value[:TEXT_LIMITS[name]].strip() or UNKNOWN
    return result


def bounded_source(value):
    value = metadata_text(value).casefold()
    if len(value) > SOURCE_LIMIT or redact_credentials(value) != value:
        raise PolicyError("invalid_source_metadata")
    return value


def prohibited_fields(collection, *, analytics=False):
    if collection not in COLLECTIONS:
        raise PolicyError("unsupported_collection")
    # Delivery snapshots/leases belong only to the internal alert collection.
    internal = frozenset({"message", "claim_token"}) if collection == "alert_log" and not analytics else frozenset()
    return PROHIBITED_FIELDS | (PRIVATE_ANALYTICS_FIELDS - internal)


def positive_days(value, setting):
    if (not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,7}", value.strip())
            or int(value) <= 0):
        raise PolicyError(setting + " must be a positive integer (up to 7 digits)")
    return int(value)


def boolean(value, setting):
    if not isinstance(value, str) or value.strip().casefold() not in TRUE_VALUES | FALSE_VALUES:
        raise PolicyError(setting + " must be a boolean")
    return value.strip().casefold() in TRUE_VALUES


def dashboard_config_errors(environ):
    errors = []
    flags = {}
    for name, default in (("DJANGO_DEBUG", "True"), ("DASHBOARD_REQUIRE_AUTH", "True"),
                          ("DJANGO_SECURE_COOKIES", "False")):
        try:
            flags[name] = boolean(environ.get(name, default), name)
        except PolicyError as error:
            errors.append(str(error))
    if flags.get("DJANGO_DEBUG") is False:
        secret = environ.get("DJANGO_SECRET_KEY", "").strip()
        if len(secret) < 50 or secret.startswith(("dev-only-", "replace_")):
            errors.append("DJANGO_SECRET_KEY must be at least 50 characters and non-placeholder in production")
        hosts = [host.strip() for host in environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if host.strip()]
        if not hosts or any("*" in host for host in hosts):
            errors.append("DJANGO_ALLOWED_HOSTS must be explicit; wildcards are forbidden in production")
        if flags.get("DASHBOARD_REQUIRE_AUTH") is not True:
            errors.append("DASHBOARD_REQUIRE_AUTH must be True in production")
    return errors


@dataclass(frozen=True)
class RetentionRule:
    collection: str
    days: int
    primary: str
    fallback: str | None


def retention_rules(environ):
    return tuple(RetentionRule(name, positive_days(environ.get(setting, str(default)), setting), primary, fallback)
                 for name, (setting, default, primary, fallback) in RETENTION_SETTINGS.items())


def utc_now(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PolicyError("current_time_must_be_aware_UTC")
    return value.astimezone(timezone.utc)


def cutoff(rule, now):
    now = utc_now(now)
    try:
        return now - timedelta(days=rule.days)
    except OverflowError:
        # A very long configured retention is safe: no representable older dates.
        return datetime.min.replace(tzinfo=timezone.utc)
