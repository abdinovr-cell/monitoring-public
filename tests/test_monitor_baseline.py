import tempfile
import unittest
from pathlib import Path

import monitor


class BaselineReportTests(unittest.TestCase):
    def setUp(self):
        self.original_out = monitor.OUT
        self.temp_dir = tempfile.TemporaryDirectory()
        monitor.OUT = Path(self.temp_dir.name)
        monitor.OUT.mkdir(exist_ok=True)

    def tearDown(self):
        monitor.OUT = self.original_out
        self.temp_dir.cleanup()

    def test_incomplete_initial_baseline_does_not_emit_false_changes(self):
        url = "https://atyrau.edu.kz/ru/Information/Info/195?name=University"
        previous = {url: {"title": "История", "hash": "old"}}
        pages = {url: {"title": "История", "hash": "new", "attachments": []}}

        report = monitor.build_report(
            "2026-11-15T00:00:00+00:00",
            pages,
            [],
            ["https://atyrau.edu.kz/ru/next"],
            {url},
            True,
            previous,
            [],
            False,
        )

        self.assertFalse(report["complete"])
        self.assertFalse(report["baseline_ready"])
        self.assertEqual(report["changes"], [])
        self.assertIn("Исходный снимок ещё формируется", report["note"])

    def test_ready_baseline_reports_content_changes(self):
        url = "https://atyrau.edu.kz/ru"
        previous = {url: {"title": "Главная", "hash": "old"}}
        pages = {url: {"title": "Главная", "hash": "new", "attachments": []}}

        report = monitor.build_report(
            "2026-11-15T00:00:00+00:00",
            pages,
            [],
            [],
            {url},
            False,
            previous,
            [],
            True,
        )

        self.assertTrue(report["complete"])
        self.assertTrue(report["baseline_ready"])
        self.assertEqual([x["type"] for x in report["changes"]], ["changed_page"])



class CheckpointResumeTests(unittest.TestCase):
    def test_empty_completed_checkpoint_starts_fresh_crawl(self):
        saved = {"queue": [], "pages": {"https://atyrau.edu.kz/ru": {"hash": "old"}}, "errors": []}
        self.assertFalse(monitor.should_resume_checkpoint(saved))

    def test_pending_checkpoint_resumes(self):
        saved = {"queue": ["https://atyrau.edu.kz/ru/next"], "pages": {}, "errors": []}
        self.assertTrue(monitor.should_resume_checkpoint(saved))

    def test_failed_urls_resume_even_if_queue_is_empty(self):
        saved = {"queue": [], "pages": {}, "errors": [{"url": "https://atyrau.edu.kz/ru/failed"}]}
        self.assertTrue(monitor.should_resume_checkpoint(saved))


if __name__ == "__main__":
    unittest.main()
