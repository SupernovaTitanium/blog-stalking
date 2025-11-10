from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone

from feeds import _extract_entry_datetime, _extract_entry_html


class ExtractEntryHtmlTest(unittest.TestCase):
    def test_prefers_content_payload(self) -> None:
        entry = {
            "content": [
                {"value": "<p>primary</p>"},
                {"value": "<p>secondary</p>"},
            ],
            "summary": "<p>ignored</p>",
        }
        self.assertEqual(_extract_entry_html(entry), "<p>primary</p>")

    def test_falls_back_to_summary_detail(self) -> None:
        entry = {
            "summary_detail": {
                "value": "<div>summary detail</div>",
            }
        }
        self.assertEqual(
            _extract_entry_html(entry),
            "<div>summary detail</div>",
        )

    def test_uses_plain_summary_when_html_missing(self) -> None:
        entry = {"summary": "Plain text fallback"}
        self.assertEqual(_extract_entry_html(entry), "Plain text fallback")

    def test_returns_empty_string_when_no_content(self) -> None:
        self.assertEqual(_extract_entry_html({}), "")


class ExtractEntryDatetimeTest(unittest.TestCase):
    def test_prefers_published_timestamp(self) -> None:
        published = time.gmtime(1_700_000_000)
        updated = time.gmtime(1_600_000_000)
        entry = {
            "published_parsed": published,
            "updated_parsed": updated,
        }
        expected = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)
        self.assertEqual(_extract_entry_datetime(entry), expected)

    def test_uses_updated_when_published_missing(self) -> None:
        updated = time.gmtime(1_600_000_000)
        entry = {"updated_parsed": updated}
        expected = datetime.fromtimestamp(1_600_000_000, tz=timezone.utc)
        self.assertEqual(_extract_entry_datetime(entry), expected)

    def test_returns_none_when_no_timestamp_fields(self) -> None:
        self.assertIsNone(_extract_entry_datetime({}))


if __name__ == "__main__":
    unittest.main()
