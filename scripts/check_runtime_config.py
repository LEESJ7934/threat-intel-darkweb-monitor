import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

REQUIRED_SETTINGS = (
    "DB_URI",
    "DB_NAME",
    "ELASTICSEARCH_URL",
    "KIBANA_URL",
    "TELEGRAM_TOKEN",
    "TELEGRAM_CHAT_ID",
)

PLACEHOLDER_WORDS = (
    "replace_locally",
    "username",
    "password",
    "example",
)

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(word in lowered for word in PLACEHOLDER_WORDS)


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
    )


def validate_config(environ=None) -> list[str]:
    errors = []

    if environ is None:
        if not ENV_FILE.is_file():
            return ["프로젝트 최상단에 .env 파일이 없습니다."]

        load_dotenv(ENV_FILE, override=False)
        environ = os.environ

    for name in REQUIRED_SETTINGS:
        value = environ.get(name, "").strip()

        if not value:
            errors.append(
                f"{name} 환경변수가 비어 있습니다."
            )
        elif is_placeholder(value):
            errors.append(
                f"{name}에 예시값이 아닌 실제 로컬 설정값을 입력하세요."
            )

    db_uri = environ.get("DB_URI", "").strip()

    if (
        db_uri
        and not db_uri.startswith(
            ("mongodb://", "mongodb+srv://")
        )
    ):
        errors.append(
            "DB_URI는 mongodb:// 또는 "
            "mongodb+srv://로 시작해야 합니다."
        )

    chat_id = environ.get(
        "TELEGRAM_CHAT_ID",
        "",
    ).strip()

    if chat_id:
        try:
            int(chat_id)
        except ValueError:
            errors.append(
                "TELEGRAM_CHAT_ID는 정수 형식이어야 합니다."
            )

    for name in (
        "ELASTICSEARCH_URL",
        "KIBANA_URL",
    ):
        value = environ.get(name, "").strip()

        if value and not is_http_url(value):
            errors.append(
                f"{name}은 http:// 또는 "
                "https:// URL이어야 합니다."
            )

    debug_value = environ.get(
        "DJANGO_DEBUG",
        "True",
    ).strip().lower()

    if debug_value not in TRUE_VALUES | FALSE_VALUES:
        errors.append(
            "DJANGO_DEBUG는 True/False, 1/0, "
            "yes/no 또는 on/off 중 하나여야 합니다."
        )

    secret_key = environ.get(
        "DJANGO_SECRET_KEY",
        "",
    ).strip()

    if (
        debug_value in FALSE_VALUES
        and len(secret_key) < 50
    ):
        errors.append(
            "운영 모드에서는 50자 이상의 "
            "DJANGO_SECRET_KEY가 필요합니다."
        )

    allowed_hosts = environ.get(
        "DJANGO_ALLOWED_HOSTS",
        "",
    ).strip()

    if (
        debug_value in FALSE_VALUES
        and not allowed_hosts
    ):
        errors.append(
            "운영 모드에서는 "
            "DJANGO_ALLOWED_HOSTS를 설정해야 합니다."
        )

    minimum = environ.get("ALERT_MIN_LEVEL", "MEDIUM").strip()
    if minimum not in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"}:
        errors.append("ALERT_MIN_LEVEL는 CRITICAL/HIGH/MEDIUM/LOW/INFO 중 하나여야 합니다.")
    for name, default in (("ALERT_MAX_ATTEMPTS", "5"), ("ALERT_RETRY_SECONDS", "60"),
                          ("ALERT_LEASE_SECONDS", "120")):
        value = environ.get(name, default).strip()
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            errors.append(f"{name}는 양의 정수여야 합니다.")
    # Missing setting keeps the documented default; an explicit empty value is invalid.
    prefix = environ.get("ELK_INDEX_PREFIX", "darkweb-monitor")
    if not isinstance(prefix, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", prefix, re.ASCII) is None:
        errors.append("ELK_INDEX_PREFIX는 소문자/숫자로 시작하는 1~64자의 소문자, 숫자, -, _만 허용합니다.")
    return errors


def main() -> int:
    errors = validate_config()

    if errors:
        print(
            "[FAIL] 실행 환경 설정에서 "
            "문제가 발견되었습니다."
        )

        for error in errors:
            print(f"- {error}")

        print(
            "민감한 환경변수 값은 "
            "출력하지 않았습니다."
        )
        return 1

    print(
        "[OK] 필수 환경변수의 존재 여부와 "
        "형식이 정상입니다."
    )
    print(
        "민감한 환경변수 값은 "
        "출력하지 않았습니다."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
