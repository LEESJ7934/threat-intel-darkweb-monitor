"""CLI entry point. Importing classify_risk never loads .env or starts a client."""
import asyncio
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import sys

# Preserve direct script execution as well as python -m alert.alert.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alert.risk import classify_risk, format_telegram_message
from alert.service import AlertPolicy, TelegramSender, watch_forever

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AlertConfig:
    db_uri: str = field(repr=False)
    db_name: str = field(repr=False)
    token: str = field(repr=False)
    chat_id: int = field(repr=False)
    policy: AlertPolicy


def load_config(environ=None):
    if environ is None:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        environ = os.environ
    values = {}
    for name in ("DB_URI", "DB_NAME", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        value = environ.get(name)
        if not isinstance(value, str) or not value.strip() or value.strip() == "replace_locally":
            raise ValueError(f"{name} must be configured")
        values[name] = value.strip()
    if not values["DB_URI"].startswith(("mongodb://", "mongodb+srv://")):
        raise ValueError("DB_URI must use a MongoDB URI scheme")
    try:
        chat_id = int(values["TELEGRAM_CHAT_ID"])
    except ValueError:
        raise ValueError("TELEGRAM_CHAT_ID must be an integer") from None
    return AlertConfig(values["DB_URI"], values["DB_NAME"], values["TELEGRAM_TOKEN"],
                       chat_id, AlertPolicy.from_mapping(environ))


async def run(config):
    async with TelegramSender(config.token, config.chat_id) as sender:
        await watch_forever(config.db_uri, config.db_name, sender, config.policy,
                            secrets=(config.token, config.db_uri, str(config.chat_id)))


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # HTTP request URLs from third-party loggers can contain a Bot token.
    for name in ("httpx", "httpcore", "telegram", "pymongo"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        config = load_config()
    except ValueError as exc:
        print(f"[FAIL] {exc}")
        return 2
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("[FAIL] Alert service startup or shutdown failed; check local configuration.")
        return 1
    return 0


def main_watcher():
    return main()


if __name__ == "__main__":
    sys.exit(main())
