from __future__ import annotations

import unittest
from unittest.mock import patch

from feeds import FeedConfig, ParsedFeed
from validate_feeds import validate_feed


def _parsed_feed(count: int = 1) -> ParsedFeed:
    from feeds import Entry

    return ParsedFeed(
        title="Example",
        entries=[
            Entry(
                id=f"https://example.com/{i}",
                link=f"https://example.com/{i}",
                title="entry",
                published=None,
                content_html="",
            )
            for i in range(count)
        ],
    )


class ValidateFeedTest(unittest.TestCase):
    def test_ok_when_entries_are_available(self) -> None:
        with patch("validate_feeds.parse_feed", return_value=_parsed_feed(1)):
            status, count, message = validate_feed(
                FeedConfig(
                    name="Example",
                    site="https://example.com",
                    url="https://example.com/feed.xml",
                )
            )

        self.assertEqual(status, "ok")
        self.assertEqual(count, 1)
        self.assertEqual(message, "")

    def test_warns_when_no_entries_are_returned(self) -> None:
        with patch("validate_feeds.parse_feed", return_value=_parsed_feed(0)):
            status, count, message = validate_feed(
                FeedConfig(
                    name="Example",
                    site="https://example.com",
                    url="https://example.com/feed.xml",
                )
            )

        self.assertEqual(status, "warn")
        self.assertEqual(count, 0)
        self.assertEqual(message, "no entries returned")

    def test_errors_when_runtime_parser_fails(self) -> None:
        with patch("validate_feeds.parse_feed", side_effect=RuntimeError("bad feed")):
            status, count, message = validate_feed(
                FeedConfig(
                    name="Example",
                    site="https://example.com",
                    url="https://example.com/feed.xml",
                )
            )

        self.assertEqual(status, "error")
        self.assertEqual(count, 0)
        self.assertIn("bad feed", message)

    def test_custom_parser_is_forwarded(self) -> None:
        with patch("validate_feeds.parse_feed", return_value=_parsed_feed(1)) as parse_mock:
            status, count, _ = validate_feed(
                FeedConfig(
                    name="Minimizing Regret",
                    url="https://www.minregret.com/blog/",
                    parser="jekyll_listing",
                )
            )

        self.assertEqual(status, "ok")
        self.assertEqual(count, 1)
        parse_mock.assert_called_once_with(
            "https://www.minregret.com/blog/",
            site_url=None,
            parser="jekyll_listing",
        )


if __name__ == "__main__":
    unittest.main()
