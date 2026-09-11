from contextlib import contextmanager
from datetime import timezone

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from .config import ElkError


@contextmanager
def mongo_database(config, uri, *, factory=None):
    client = None
    try:
        client = (factory or MongoClient)(
            uri, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000, socketTimeoutMS=5000, w="majority")
        yield client[config.database]
    except (PyMongoError, ValueError, TypeError):
        raise ElkError("database_error", "MongoDB") from None
    finally:
        if client is not None:
            client.close()
