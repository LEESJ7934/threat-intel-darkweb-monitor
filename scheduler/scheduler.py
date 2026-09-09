import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

CRAWLER_MODULES = (
    "crawling.gunra_crawler",
    "crawling.Black_Shrantac_crawler",
    "crawling.dragonforce_crawler",
    "crawling.bitlock_crawler",
)

KST = timezone(timedelta(hours=9))


def read_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(
            f"{name}은 정수여야 합니다."
        ) from error

    if value < 1:
        raise ValueError(
            f"{name}은 1 이상이어야 합니다."
        )

    return value


CRAWLER_INTERVAL_MINUTES = read_positive_int(
    "CRAWLER_INTERVAL_MINUTES",
    30,
)
CRAWLER_TIMEOUT_SECONDS = read_positive_int(
    "CRAWLER_TIMEOUT_SECONDS",
    600,
)


def log(message: str) -> None:
    timestamp = datetime.now(KST).strftime(
        "%Y-%m-%d %H:%M:%S KST"
    )
    print(
        f"[{timestamp}] {message}",
        flush=True,
    )


def run_crawler(module_name: str) -> None:
    if module_name not in CRAWLER_MODULES:
        log("등록되지 않은 크롤러 모듈")
        return

    script_path = PROJECT_ROOT.joinpath(*module_name.split(".")).with_suffix(".py")

    if not script_path.is_file():
        log(f"실행 파일 없음: {module_name}")
        return

    log(f"크롤러 실행 시작: {module_name}")

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                module_name,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=CRAWLER_TIMEOUT_SECONDS,
            check=False,
        )

    except subprocess.TimeoutExpired:
        log(
            f"크롤러 제한시간 초과: {module_name} "
            f"({CRAWLER_TIMEOUT_SECONDS}초)"
        )
        return

    except OSError as error:
        log(
            f"크롤러 실행 실패: {module_name} "
            f"({type(error).__name__})"
        )
        return

    if result.stdout.strip():
        print(result.stdout.strip())

    if result.returncode == 0:
        log(
            f"크롤러 실행 완료: {module_name}"
        )
        return

    if result.stderr.strip():
        print(
            result.stderr.strip(),
            file=sys.stderr,
        )

    log(
        f"크롤러 비정상 종료: {module_name} "
        f"(종료 코드 {result.returncode})"
    )


def build_scheduler() -> BlockingScheduler:
    scheduler = BlockingScheduler(
        timezone=KST,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 60,
        },
    )

    first_run = datetime.now(KST)

    for index, module_name in enumerate(
        CRAWLER_MODULES
    ):
        job_id = module_name.rsplit(".", 1)[1]

        scheduler.add_job(
            run_crawler,
            trigger=IntervalTrigger(
                minutes=CRAWLER_INTERVAL_MINUTES,
                timezone=KST,
            ),
            args=[module_name],
            id=job_id,
            name=f"{job_id} crawler",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            replace_existing=True,
            next_run_time=(
                first_run
                + timedelta(seconds=index * 15)
            ),
        )

    return scheduler


def main() -> None:
    scheduler = build_scheduler()

    log(
        "다크웹 크롤링 스케줄러 시작 "
        f"(주기: {CRAWLER_INTERVAL_MINUTES}분)"
    )
    log("종료하려면 Ctrl+C를 누르세요.")

    try:
        scheduler.start()

    except (KeyboardInterrupt, SystemExit):
        log("스케줄러 종료")


if __name__ == "__main__":
    main()
