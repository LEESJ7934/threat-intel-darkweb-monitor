"""Pure risk policy and bounded, plain-text Telegram formatting. No runtime I/O."""
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlsplit

LEVELS = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
MATERIAL_FIELDS = ("company_name", "company_url", "country", "data_contents",
                   "data_size", "description")
METADATA_FIELDS = (*MATERIAL_FIELDS, "publication_date", "source_url")
KST = timezone(timedelta(hours=9))
TIER_1 = ("government", "defense", "국방", "정부", "military")
TIER_2 = ("sk shieldus", "ahnlab", "kdb", "woori", "samsung life", "medical",
          "hospital", "finance", "bank", "kaspersky", "lg cns", "sds", "보안", "금융", "의료")
CRITICAL_WORDS = ("billing", "customer data", "personal document", "patient data",
                  "계좌", "결제", "주민등록", "여권", "ssn")
HIGH_WORDS = ("financial document", "internal document", "employee data",
              "재무", "회계", "인사", "contract", "agreement")
KOREAN_COMPANIES = ("samsung", "lg", "hyundai", "sk", "kt", "cj", "lotte", "posco", "hanwha")


def text_value(value):
    return (" ".join(value.split()) or "unknown") if isinstance(value, str) else "unknown"


def safe_text(value, *, secrets=(), limit=500):
    """Allow plain metadata; redact configured credentials and recognizable URIs/tokens."""
    value = text_value(value)
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    value = re.sub(r"mongodb(?:\+srv)?://[^\s<>]+", "[redacted]", value, flags=re.I)
    value = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b", "[redacted]", value)
    value = re.sub(r"<[^>]*>", "", value)
    return value[:limit] or "unknown"


def url_hostname(value):
    value = text_value(value).replace("🔗", "").strip()
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        return (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def classify_risk(data: dict):
    """Keep the existing five-level heuristic and (level, reason, actor) API."""
    if not isinstance(data, dict):
        return "ERROR", "Invalid data format", "unknown"
    actor = text_value(data.get("source"))
    if actor == "unknown" and isinstance(data.get("_id"), str):
        identity = data["_id"]
        if identity.startswith("black_shrantac"):
            actor = "black_shrantac"
        elif "_" in identity:
            actor = identity.split("_", 1)[0]
    country = text_value(data.get("country")).lower().replace("🗺️", "").replace("location:", "").strip()
    contents = text_value(data.get("data_contents")).lower()
    size = text_value(data.get("data_size")).lower()
    name = text_value(data.get("company_name")).lower()
    description = text_value(data.get("description")).lower()
    host = url_hostname(data.get("company_url"))
    if any(word in contents for word in CRITICAL_WORDS):
        return "CRITICAL", f"민감 정보 유출 의심 ({contents[:30]}...)", actor
    if "korea" in country or host.endswith(".kr"):
        return "CRITICAL", f"한국 관련성 (국가/도메인: {country if 'korea' in country else host})", actor
    if any(word in name for word in TIER_1):
        return "CRITICAL", f"Tier 1(정부/국방) 의심 ({name})", actor
    if any(word in name for word in TIER_2):
        return "CRITICAL", f"Tier 2(주요 기반) 의심 ({name})", actor
    if any(word in contents for word in HIGH_WORDS):
        return "HIGH", f"주요 문서 유출 의심 ({contents[:30]}...)", actor
    if "tb" in size:
        return "HIGH", f"대규모(TB) 유출 ({size})", actor
    if any(word in name for word in KOREAN_COMPANIES):
        return "MEDIUM", f"한국 기업명 의심 ({name})", actor
    if any(word in description for word in KOREAN_COMPANIES):
        return "LOW", "간접 언급", actor
    return "INFO", "한국 관련성 낮음", actor


def meets_threshold(level, minimum="MEDIUM"):
    if minimum not in LEVELS:
        raise ValueError("ALERT_MIN_LEVEL must be CRITICAL/HIGH/MEDIUM/LOW/INFO")
    return level in LEVELS and LEVELS.index(level) >= LEVELS.index(minimum)


def utc_datetime(value):
    if isinstance(value, dict):
        value = value.get("$date")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)  # Legacy naive dates represent UTC.
    return value.astimezone(timezone.utc)


def format_telegram_message(data, level, reason, actor, *, event_type="NEW",
                            changed_fields=(), secrets=()):
    data = data if isinstance(data, dict) else {}
    clean = lambda value, size=300: safe_text(value, secrets=secrets, limit=size)
    stamp = utc_datetime(data.get("first_seen")) or utc_datetime(data.get("scraped_time"))
    formatted = stamp.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST") if stamp else "unknown"
    emoji = {"CRITICAL": "🚨", "HIGH": "🔥", "MEDIUM": "⚠️", "LOW": "ℹ️", "INFO": "ℹ️"}
    kind = "UPDATED" if event_type == "UPDATED" else "NEW"
    lines = [
        f"[{emoji.get(level, '❓')} {clean(level, 16)}][{kind}] {clean(data.get('company_name'), 160)}",
        "■ 이벤트: " + ("기존 유출 게시 정보 변경" if kind == "UPDATED" else "신규 유출 게시 정보"),
    ]
    if kind == "UPDATED":
        fields = [name for name in MATERIAL_FIELDS if name in changed_fields]
        lines.append("■ 변경 필드: " + (", ".join(fields) or "없음"))
    lines.extend((
        f"■ 위험도: {clean(level, 16)} ({clean(reason, 240)})",
        f"■ 공격자/source: {clean(actor, 64)}",
        f"■ 피해 규모: {clean(data.get('data_size'), 80)}",
        f"■ 유출 내용: {clean(data.get('data_contents'))}",
        f"■ 국가: {clean(data.get('country'), 80)}",
        f"■ URL: {clean(data.get('company_url'), 260)}",
        f"■ 최초 관찰 시각: {formatted}",
    ))
    # Bound UTF-16 length as well as character length, including astral emoji.
    return "\n".join(lines).encode("utf-16-le")[:7800].decode("utf-16-le", errors="ignore")
