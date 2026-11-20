import time
import unittest
from unittest.mock import patch

import extended_sources as monitor


class DocumentCycleTests(unittest.TestCase):
    def setUp(self):
        self.cycle = {
            "known": [],
            "pending": [],
            "staging": {},
            "attempts": {},
            "errors": [],
        }

    def test_document_queue_resumes_between_batches(self):
        with patch.object(monitor, "MAX_DOCUMENTS", 2), \
             patch.object(monitor, "fetch", return_value=(b"document", "application/pdf")), \
             patch.object(monitor, "extract_document", return_value="text"):
            first, processed_first = monitor.scan_document_batch(
                ["https://atyrau.edu.kz/a.pdf", "https://atyrau.edu.kz/b.pdf",
                 "https://atyrau.edu.kz/c.pdf"],
                self.cycle, time.monotonic() + 10
            )
            self.assertEqual(processed_first, 2)
            self.assertEqual(len(first["staging"]), 2)
            self.assertEqual(len(first["pending"]), 1)

            second, processed_second = monitor.scan_document_batch(
                ["https://atyrau.edu.kz/a.pdf", "https://atyrau.edu.kz/b.pdf",
                 "https://atyrau.edu.kz/c.pdf"],
                first, time.monotonic() + 10
            )
            self.assertEqual(processed_second, 1)
            self.assertEqual(len(second["staging"]), 3)
            self.assertEqual(len(second["pending"]), 0)

    def test_persistently_unavailable_document_is_recorded(self):
        with patch.object(monitor, "MAX_DOCUMENTS", 5), \
             patch.object(monitor, "fetch", side_effect=OSError("unavailable")):
            cycle, processed = monitor.scan_document_batch(
                ["https://atyrau.edu.kz/missing.pdf"],
                self.cycle, time.monotonic() + 10
            )
        self.assertEqual(processed, 5)
        self.assertEqual(cycle["pending"], [])
        self.assertEqual(cycle["errors"], [])
        self.assertIn("https://atyrau.edu.kz/missing.pdf", cycle["unavailable"])


if __name__ == "__main__":
    unittest.main()
