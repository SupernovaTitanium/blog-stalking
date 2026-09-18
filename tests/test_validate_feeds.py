from __future__ import annotations

import unittest
from unittest.mock import patch

from feedparser import FeedParserDict

from main import FeedConfig
from validate_feeds import validate_feed


class ValidateFeedTest(unittest.TestCase):
    def test_tolerates_parse_warning_when_entries_are_available(self) -> None:
        parsed = FeedParserDict(
            bozo=True,
            bozo_exception=Exception("non-fatal warning"),
            entries=[{"title": "entry"}],
            feed={"title": "Example"},
        )

        with patch("validate_feeds.parse_feed", return_value=parsed):
            status, count, message = validate_feed(
                FeedConfig(
                    name="Example",
                    site="https://example.com",
                    url="https://example.com/feed.xml",
                )
            )

        self.assertEqual(status, "ok")
        self.assertEqual(count, 1)
        self.assertIn("parse warning tolerated", message)

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
        parsed = FeedParserDict(
            bozo=False,
            entries=[{"title": "entry"}],
            feed={"title": "Minimizing Regret"},
        )

        with patch("validate_feeds.parse_feed", return_value=parsed) as parse_mock:
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
