"""No clients, environment loading or external I/O at import time."""
from dataclasses import dataclass
from pathlib import Path
import os
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREFIX = "darkweb-monitor"
PREFIX_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
COLLECTIONS = {"events": "leaked_data", "history": "leak_history", "alerts": "alert_log"}
TIME_FIELDS = {"events": "last_seen", "history": "changed_at", "alerts": "created_at"}
VIEW_NAMES = {"events": "Darkweb Events", "history": "Darkweb Change History",
              "alerts": "Darkweb Alert Delivery"}


class ElkError(Exception):
    """Only fixed error categories, never an external response or credential."""
    def __init__(self, category, service="configuration", status=None):
        self.category, self.service, self.status = category, service, status
        super().__init__(f"{service}: {category}")


def validate_prefix(value):
    if not isinstance(value, str) or PREFIX_PATTERN.fullmatch(value) is None:
        raise ElkError("invalid_ELK_INDEX_PREFIX")
    return value


def validate_database(value):
    # A bounded portable namespace also prevents TOML interpolation ambiguity.
    if (not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", value, re.ASCII) is None
            or value.lower() in {"admin", "local", "config", "monstache"}):
        raise ElkError("invalid_DB_NAME")
    return value


def validate_url(value, setting):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ElkError("invalid_" + setting)
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ElkError("invalid_" + setting)
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path not in {"", "/"}
                or (url.port is not None and not 1 <= url.port <= 65535)):
            raise ValueError
    except ValueError:
        raise ElkError("invalid_" + setting) from None
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    database: str
    prefix: str
    elasticsearch: str
    kibana: str
    monstache: str

    def index(self, kind):
        if kind not in COLLECTIONS:
            raise ElkError("invalid_index_kind")
        return f"{self.prefix}-{kind}"

    def view_id(self, kind):
        return self.index(kind)

    def template_name(self, kind):
        return self.index(kind) + "-template"


def load_config(environ=None):
    if environ is None:
        load_dotenv(ROOT / ".env", override=False)
        environ = os.environ
    return Config(
        validate_database(environ.get("DB_NAME", "")),
        validate_prefix(environ.get("ELK_INDEX_PREFIX", DEFAULT_PREFIX)),
        validate_url(environ.get("ELASTICSEARCH_URL", ""), "ELASTICSEARCH_URL"),
        validate_url(environ.get("KIBANA_URL", ""), "KIBANA_URL"),
        validate_url(environ.get("MONSTACHE_URL", "http://127.0.0.1:8080"), "MONSTACHE_URL"),
    )


def mongo_uri(environ=None):
    values = os.environ if environ is None else environ
    uri = values.get("DB_URI", "")
    if not isinstance(uri, str) or not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise ElkError("invalid_DB_URI")
    return uri
