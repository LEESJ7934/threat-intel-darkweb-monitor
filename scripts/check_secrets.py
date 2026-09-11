import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRECTORIES = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "mongo-data",
    "node_modules",
    "venv",
}

SKIP_FILES = {
    ".env.example",
}

MAX_FILE_SIZE = 1_000_000

RULES = {
    "telegram_bot_token": re.compile(
        r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"
    ),
    "mongodb_uri_with_credentials": re.compile(
        r"mongodb(?:\+srv)?://[^\s/:@]+:[^\s/@]+@",
        re.IGNORECASE,
    ),
    "aws_access_key": re.compile(
        r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"
    ),
    "private_key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "personal_windows_path": re.compile(
        r"[A-Za-z]:\\Users\\[^\\\r\n]+",
        re.IGNORECASE,
    ),
    "hardcoded_django_secret": re.compile(
        r"\b(?:DJANGO_)?SECRET_KEY\s*=\s*['\"]"
        r"(?!dev-only-|replace_|REMOVED_)[^'\"\r\n]{50,}['\"]"
    ),
}


def is_local_env_file(path):
    if path.name == ".env":
        return True

    return (
        path.name.startswith(".env.")
        and path.name != ".env.example"
    )


def iter_text_files():
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue

        if any(
            part in SKIP_DIRECTORIES
            for part in path.parts
        ):
            continue

        if path.name in SKIP_FILES:
            continue

        if is_local_env_file(path):
            continue

        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                continue

            raw_data = path.read_bytes()
        except OSError:
            continue

        if b"\x00" in raw_data:
            continue

        yield path, raw_data.decode(
            "utf-8",
            errors="ignore",
        )


def find_tracked_env_files():
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    tracked_files = []

    for file_name in result.stdout.splitlines():
        if is_local_env_file(Path(file_name)):
            tracked_files.append(file_name)

    return tracked_files


def main():
    findings = []

    for path, file_text in iter_text_files():
        relative_path = path.relative_to(
            PROJECT_ROOT
        ).as_posix()

        for rule_name, pattern in RULES.items():
            if pattern.search(file_text):
                findings.append(
                    (relative_path, rule_name)
                )

    for file_name in find_tracked_env_files():
        findings.append(
            (file_name, "tracked_environment_file")
        )

    if findings:
        print(
            "[FAIL] Potential secret exposure detected. "
            "Matched values are hidden."
        )

        for file_name, rule_name in sorted(set(findings)):
            print(f"- {file_name}: {rule_name}")

        return 1

    print(
        "[PASS] No known secret pattern was found "
        "in project files."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
