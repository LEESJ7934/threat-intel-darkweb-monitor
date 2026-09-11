"""Allowlisted JSON audit events, rotating local file with safe console fallback.

No file or handler is opened at import. Hashes are pseudonyms, not anonymization.
Use one process per log file; multi-worker deployment needs a central log sink.
"""
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading

EVENTS = frozenset({"dashboard_access", "event_detail_access", "login_success",
                    "login_failure", "logout", "retention_execution"})
RESULTS = frozenset({"success", "denied", "not_found", "error", "dry_run", "started", "deleted"})
CATEGORIES = frozenset({"dashboard", "event_detail", "authentication", "retention"})
_LOGGER = None
_LOCK = threading.Lock()


def identifier_hash(value, category):
    if value is None:
        return None
    raw = category + ":" + type(value).__name__ + ":" + str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def make_record(event, *, result, category, authenticated=False, user_id=None,
                document_id=None, status=None, collection=None, count=None, now=None):
    if event not in EVENTS or result not in RESULTS or category not in CATEGORIES:
        raise ValueError("invalid_audit_category")
    stamp = datetime.now(timezone.utc) if now is None else now
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("invalid_audit_timestamp")
    record = {"timestamp": stamp.astimezone(timezone.utc).isoformat(), "event": event,
              "category": category, "result": result, "authenticated": authenticated is True}
    for field, value in (("user_hash", identifier_hash(user_id, "user")),
                         ("document_hash", identifier_hash(document_id, "document"))):
        if value is not None:
            record[field] = value
    if type(status) is int and 100 <= status <= 599:
        record["status"] = status
    if collection in {"leaked_data", "leak_history", "alert_log"}:
        record["collection"] = collection
    if type(count) is int and count >= 0:
        record["count"] = count
    return record


class SafeRotatingHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        try:
            Path(self.baseFilename).chmod(0o600)
        except OSError:
            pass  # Windows ACLs and deployment ownership remain operator responsibilities.
        return stream

    def handleError(self, record):
        # logging's default handleError may dump exception/path details.
        try:
            sys.stderr.write("[WARN] audit_file_unavailable; console fallback\n")
            sys.stderr.write(record.getMessage() + "\n")
        except OSError:
            pass


def configure(directory=None, *, stream=None):
    """Explicit runtime configuration; tests supply their own directory/stream."""
    logger = logging.Logger("governance.audit", level=logging.INFO)
    logger.propagate = False
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parents[1] / "logs"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = SafeRotatingHandler(directory / "audit.jsonl", maxBytes=1_048_576,
                                      backupCount=5, encoding="utf-8", delay=False)
    except OSError:
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.warning('{"event":"audit_sink","result":"console_fallback"}')
        return logger
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def emit(event, **fields):
    global _LOGGER
    record = make_record(event, **fields)
    with _LOCK:
        if _LOGGER is None:
            _LOGGER = configure()
        _LOGGER.info(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
    return record
