"""Runtime configuration and Mongo lifecycle; no connection at import."""
from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv

from elk.config import mongo_uri, validate_database
from elk.mongo import mongo_database  # Existing timezone-aware, closing context manager.
from .policy import retention_rules

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Config:
    database: str
    rules: tuple


def load_config(environ=None):
    if environ is None:
        load_dotenv(ROOT / ".env", override=False)
        environ = os.environ
    return Config(validate_database(environ.get("DB_NAME", "")), retention_rules(environ))
