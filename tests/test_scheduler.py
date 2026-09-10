import os
import subprocess
import sys
from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scheduler import scheduler as scheduler_module
from scheduler.scheduler import (
    CRAWLER_MODULES,
    build_scheduler,
    read_positive_int,
)


class SchedulerConfigurationTests(
    unittest.TestCase
):
    @patch.dict(
        os.environ,
        {"TEST_INTERVAL": "15"},
    )
    def test_positive_integer_is_accepted(self):
        self.assertEqual(
            read_positive_int(
                "TEST_INTERVAL",
                30,
            ),
            15,
        )

    @patch.dict(
        os.environ,
        {"TEST_INTERVAL": "invalid"},
    )
    def test_non_integer_is_rejected(self):
        with self.assertRaises(ValueError):
            read_positive_int(
                "TEST_INTERVAL",
                30,
            )

    @patch.dict(
        os.environ,
        {"TEST_INTERVAL": "0"},
    )
    def test_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            read_positive_int(
                "TEST_INTERVAL",
                30,
            )

    def test_only_bitlock_is_active_and_registered_once(self):
        scheduler = build_scheduler()
        jobs = scheduler.get_jobs()

        expected_ids = {
            module_name.rsplit(".", 1)[1]
            for module_name in CRAWLER_MODULES
        }
        actual_ids = {
            job.id
            for job in jobs
        }

        self.assertEqual(
            len(jobs),
            len(CRAWLER_MODULES),
        )
        self.assertEqual(
            actual_ids,
            expected_ids,
        )
        self.assertEqual(CRAWLER_MODULES, ("crawling.bitlock_crawler",))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(actual_ids, {"bitlock_crawler"})

        for job in jobs:
            self.assertEqual(
                job.max_instances,
                1,
            )
            self.assertTrue(job.coalesce)
            self.assertEqual(job.misfire_grace_time, 60)
            self.assertEqual(job.trigger.interval, timedelta(minutes=scheduler_module.CRAWLER_INTERVAL_MINUTES))
            self.assertEqual(tuple(job.args), ("crawling." + job.id,))

        self.assertEqual(jobs[0].next_run_time.tzinfo, scheduler_module.KST)

    @patch("scheduler.scheduler.subprocess.run")
    @patch("scheduler.scheduler.log")
    def test_each_crawler_runs_as_module_with_existing_timeout_and_cwd(self, log, run):
        run.return_value = SimpleNamespace(stdout="", stderr="", returncode=0)
        for module_name in CRAWLER_MODULES:
            scheduler_module.run_crawler(module_name)
            run.assert_called_with(
                [sys.executable, "-m", module_name], cwd=scheduler_module.PROJECT_ROOT,
                capture_output=True, text=True, timeout=scheduler_module.CRAWLER_TIMEOUT_SECONDS,
                check=False,
            )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], [sys.executable, "-m", "crawling.bitlock_crawler"])

    @patch("scheduler.scheduler.log")
    @patch("scheduler.scheduler.subprocess.run", side_effect=subprocess.TimeoutExpired("crawler", 600))
    def test_subprocess_timeout_is_logged_without_stopping_scheduler(self, run, log):
        scheduler_module.run_crawler(CRAWLER_MODULES[0])
        self.assertIn("제한시간 초과", log.call_args.args[0])

    @patch("scheduler.scheduler.log")
    @patch("scheduler.scheduler.subprocess.run", side_effect=OSError("synthetic failure"))
    def test_process_start_failure_is_logged(self, run, log):
        scheduler_module.run_crawler(CRAWLER_MODULES[0])
        self.assertIn("실행 실패", log.call_args.args[0])

    @patch("scheduler.scheduler.log")
    @patch("scheduler.scheduler.subprocess.run")
    def test_nonzero_crawler_exit_is_reported(self, run, log):
        run.return_value = SimpleNamespace(stdout="", stderr="", returncode=1)
        scheduler_module.run_crawler(CRAWLER_MODULES[0])
        self.assertIn("종료 코드 1", log.call_args.args[0])

    @patch("scheduler.scheduler.log")
    @patch("scheduler.scheduler.subprocess.run")
    def test_unregistered_module_is_not_executed(self, run, log):
        for module in ("unregistered.module", "crawling.gunra_crawler",
                       "crawling.Black_Shrantac_crawler", "crawling.dragonforce_crawler"):
            scheduler_module.run_crawler(module)
        run.assert_not_called()

    @patch("scheduler.scheduler.log")
    @patch("scheduler.scheduler.subprocess.run")
    def test_failed_run_does_not_prevent_the_next_bitlock_run(self, run, log):
        run.side_effect = [subprocess.TimeoutExpired("crawler", 600),
                           SimpleNamespace(stdout="", stderr="", returncode=0)]
        scheduler_module.run_crawler(CRAWLER_MODULES[0])
        scheduler_module.run_crawler(CRAWLER_MODULES[0])
        self.assertEqual(run.call_count, 2)
        self.assertIn("실행 완료", log.call_args.args[0])

    @patch("scheduler.scheduler.log")
    @patch("scheduler.scheduler.subprocess.run")
    @patch("scheduler.scheduler.Path.is_file", return_value=False)
    def test_missing_crawler_file_is_not_executed(self, exists, run, log):
        scheduler_module.run_crawler(CRAWLER_MODULES[0])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
