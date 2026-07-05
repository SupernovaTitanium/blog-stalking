from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone

from feeds import (
    _extract_entry_datetime,
    _extract_entry_html,
    _parse_feed,
    _sanitize_feed_payload,
)
from feedparser import FeedParserDict
from unittest.mock import patch


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


class SanitizeFeedPayloadTest(unittest.TestCase):
    def test_preserves_escaped_html_inside_xml_text(self) -> None:
        payload = (
            b"<rss><channel><item>"
            b"<description>&lt;p&gt;escaped&lt;/p&gt;</description>"
            b"</item></channel></rss>"
        )

        self.assertEqual(_sanitize_feed_payload(payload), payload)

    def test_escapes_bare_ampersands(self) -> None:
        payload = b"<rss><channel><title>A & B</title></channel></rss>"

        self.assertIn(b"A &amp; B", _sanitize_feed_payload(payload))


class ParseFeedRecoveryTest(unittest.TestCase):
    def test_recovers_from_html_and_discovers_rss(self) -> None:
        html_payload = (
            b"<html><head>"
            b'<link rel="alternate" type="application/rss+xml" href="/rss.xml"/>'
            b"</head><body>fallback</body></html>"
        )

        def fake_parse(input_data, **kwargs):
            if isinstance(input_data, (bytes, bytearray)):
                return FeedParserDict(
                    bozo=True,
                    bozo_exception=Exception("bad xml"),
                    entries=[],
                    feed={},
                )
            if input_data == "https://example.com/rss.xml":
                return FeedParserDict(
                    bozo=False,
                    entries=[{"title": "ok"}],
                    feed={"title": "Example"},
                )
            return FeedParserDict(
                bozo=True,
                bozo_exception=Exception("bad xml"),
                entries=[],
                feed={},
            )

        with patch("feeds.feedparser.parse", side_effect=fake_parse):
            with patch("feeds._fetch_feed_bytes", return_value=html_payload):
                parsed = _parse_feed(
                    "https://example.com/feed",
                    site_url="https://example.com",
                )

        self.assertEqual(len(parsed.entries), 1)

    def test_prefers_sanitized_parse_for_recoverable_bozo_feed(self) -> None:
        clean_payload = b"<rss><channel><item><title>ok</title></item></channel></rss>"

        def fake_parse(input_data, **kwargs):
            if isinstance(input_data, (bytes, bytearray)):
                return FeedParserDict(
                    bozo=False,
                    entries=[{"title": "clean"}],
                    feed={"title": "Example"},
                )
            return FeedParserDict(
                bozo=True,
                bozo_exception=Exception("bad xml"),
                entries=[{"title": "dirty"}],
                feed={"title": "Example"},
            )

        with patch("feeds.feedparser.parse", side_effect=fake_parse):
            with patch("feeds._fetch_feed_bytes", return_value=clean_payload):
                parsed = _parse_feed("https://example.com/feed")

        self.assertFalse(parsed.bozo)
        self.assertEqual(parsed.entries[0]["title"], "clean")


if __name__ == "__main__":
    unittest.main()
