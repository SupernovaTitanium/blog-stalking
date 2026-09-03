from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone

from feeds import (
    _extract_entry_datetime,
    _trim_boilerplate_lines,
    _extract_entry_html,
    _parse_feed,
    _sanitize_feed_payload,
    fetch_recent_posts,
)
from feedparser import FeedParserDict
from unittest.mock import patch


class FetchRecentPostsTitleTest(unittest.TestCase):
    def _feed_with_title(self, title: str) -> bytes:
        escaped = title.replace("&", "&amp;")
        return (
            '<rss version="2.0"><channel><title>T</title><link>https://example.com/</link>'
            f"<item><title>{escaped}</title>"
            "<link>https://example.com/post</link>"
            "<pubDate>Wed, 02 Sep 2026 06:00:00 +0000</pubDate>"
            "<description>body text</description></item>"
            "</channel></rss>"
        ).encode("utf-8")

    def test_oversized_titles_are_truncated(self) -> None:
        # Mastodon-style feeds put the whole post text into the title.
        huge_title = "字" * 300

        with patch(
            "feeds._fetch_feed_bytes",
            return_value=self._feed_with_title(huge_title),
        ):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertEqual(len(posts), 1)
        self.assertLessEqual(len(posts[0].title), 141)
        self.assertTrue(posts[0].title.endswith("…"))
        # The full text is still available as content.
        self.assertIn("body text", posts[0].content_text)

    def test_normal_titles_are_untouched(self) -> None:
        with patch(
            "feeds._fetch_feed_bytes",
            return_value=self._feed_with_title("A perfectly normal title"),
        ):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertEqual(posts[0].title, "A perfectly normal title")


class ArticleContentFallbackTest(unittest.TestCase):
    FEED_XML = (
        '<rss version="2.0"><channel><title>T</title>'
        "<item><title>Excerpt post</title>"
        "<link>https://example.com/post</link>"
        "<pubDate>Wed, 02 Sep 2026 06:00:00 +0000</pubDate>"
        "<description>Climate</description></item>"
        "</channel></rss>"
    ).encode("utf-8")
    ARTICLE_HTML = (
        "<html><body><nav>menu</nav>"
        "<main><p>" + "Full article body text. " * 30 + "</p></main>"
        "</body></html>"
    ).encode("utf-8")

    def test_short_feed_text_is_enriched_from_article_page(self) -> None:
        calls: list[str] = []

        def fake_fetch(url, *args, **kwargs):
            calls.append(url)
            return self.FEED_XML if len(calls) == 1 else self.ARTICLE_HTML

        with patch("feeds._fetch_feed_bytes", side_effect=fake_fetch):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertEqual(len(posts), 1)
        self.assertIn("Full article body text.", posts[0].content_text)
        self.assertGreater(len(posts[0].content_text), 200)
        self.assertEqual(calls[1], "https://example.com/post")

    def test_article_fetch_failure_keeps_feed_text(self) -> None:
        calls: list[str] = []

        def fake_fetch(url, *args, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return self.FEED_XML
            raise RuntimeError("page down")

        with patch("feeds._fetch_feed_bytes", side_effect=fake_fetch):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertEqual(posts[0].content_text, "Climate")

    def test_long_feed_text_never_triggers_article_fetch(self) -> None:
        feed_xml = (
            '<rss version="2.0"><channel><title>T</title>'
            "<item><title>Full post</title>"
            "<link>https://example.com/post</link>"
            "<pubDate>Wed, 02 Sep 2026 06:00:00 +0000</pubDate>"
            "<description>" + "Rich syndicated content. " * 30 + "</description>"
            "</item></channel></rss>"
        ).encode("utf-8")

        def fail_second_fetch(url, *args, **kwargs):
            if fail_second_fetch.calls:
                raise AssertionError("article fetch should not happen")
            fail_second_fetch.calls.append(url)
            return feed_xml

        fail_second_fetch.calls = []
        with patch("feeds._fetch_feed_bytes", side_effect=fail_second_fetch):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertIn("Rich syndicated content.", posts[0].content_text)

    def test_link_post_page_without_extra_content_keeps_feed_text(self) -> None:
        # Schneier-style: the feed one-liner is clean, and the article page
        # only adds the title plus boilerplate — the page must NOT replace it.
        feed_xml = (
            '<rss version="2.0"><channel><title>T</title>'
            "<item><title>Researching Employment Scams</title>"
            "<link>https://example.com/post</link>"
            "<pubDate>Wed, 02 Sep 2026 06:00:00 +0000</pubDate>"
            "<description>&lt;p&gt;Researchers built a fake company to study "
            "fake employee scams. Read the &lt;a href='https://x.example/'&gt;"
            "full analysis&lt;/a&gt; covering recruitment funnels and payouts "
            "across three months of operation.&lt;/p&gt;</description>"
            "</item></channel></rss>"
        ).encode("utf-8")
        page_html = (
            "<html><body><main>"
            "<article><h1>Researching Employment Scams</h1>"
            "<p>Researchers built a fake company to study fake employee scams.</p>"
            "</article>"
            '<div class="comments">Leave a comment Cancel reply</div>'
            "<div>Powered by WordPress</div>"
            "</main></body></html>"
        ).encode("utf-8")

        calls: list[str] = []

        def fake_fetch(url, *args, **kwargs):
            calls.append(url)
            return feed_xml if len(calls) == 1 else page_html

        with patch("feeds._fetch_feed_bytes", side_effect=fake_fetch):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("fake employee scams", posts[0].content_text)
        self.assertNotIn("Leave a comment", posts[0].content_text)
        self.assertNotIn("Powered by WordPress", posts[0].content_text)

    def test_page_with_real_content_replaces_excerpt(self) -> None:
        feed_xml = (
            '<rss version="2.0"><channel><title>T</title>'
            "<item><title>Deep dive</title>"
            "<link>https://example.com/post</link>"
            "<pubDate>Wed, 02 Sep 2026 06:00:00 +0000</pubDate>"
            "<description>Climate</description></item>"
            "</channel></rss>"
        ).encode("utf-8")
        body = "<p>" + "Substantial article content. " * 40 + "</p>"
        page_html = (
            "<html><body><main>"
            f"<article><h1>Deep dive</h1>{body}</article>"
            '<div class="sidebar">Related posts</div>'
            '<nav>Pagination</nav>'
            "</main></body></html>"
        ).encode("utf-8")

        calls: list[str] = []

        def fake_fetch(url, *args, **kwargs):
            calls.append(url)
            return feed_xml if len(calls) == 1 else page_html

        with patch("feeds._fetch_feed_bytes", side_effect=fake_fetch):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertIn("Substantial article content.", posts[0].content_text)
        self.assertNotIn("Related posts", posts[0].content_text)
        self.assertNotIn("Pagination", posts[0].content_text)

    def test_inline_tag_line_breaks_are_rejoined(self) -> None:
        feed_xml = (
            '<rss version="2.0"><channel><title>T</title>'
            "<item><title>Routers</title>"
            "<link>https://example.com/post</link>"
            "<pubDate>Wed, 02 Sep 2026 06:00:00 +0000</pubDate>"
            "<description>&lt;p&gt;Comcast has &lt;em&gt;added&lt;/em&gt; "
            "motion detection.&lt;/p&gt;&lt;p&gt;Second paragraph "
            "here.&lt;/p&gt;</description>"
            "</item></channel></rss>"
        ).encode("utf-8")

        with patch("feeds._fetch_feed_bytes", return_value=feed_xml):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertIn("Comcast has added motion detection.", posts[0].content_text)
        self.assertIn("Second paragraph here.", posts[0].content_text)


class BoilerplateTrimTest(unittest.TestCase):
    def test_trailing_wordpress_footer_is_removed(self) -> None:
        text = (
            "Real article content sentence.\n\n"
            "Tags: security, scam\n"
            "Posted on August 31, 2026 at 7:03 AM\n"
            "Blog moderation policy.\n"
        )
        trimmed = _trim_boilerplate_lines(text)
        self.assertIn("Real article content sentence.", trimmed)
        self.assertNotIn("Tags:", trimmed)
        self.assertNotIn("Posted on", trimmed)
        self.assertNotIn("moderation", trimmed)

    def test_related_posts_heading_truncates_rest(self) -> None:
        text = "Intro paragraph.\n\nRelated posts\nSome other article\nAnother article"
        trimmed = _trim_boilerplate_lines(text)
        self.assertIn("Intro paragraph.", trimmed)
        self.assertNotIn("Some other article", trimmed)

    def test_sentences_containing_posted_on_survive(self) -> None:
        text = (
            "At one point Intel 471 finds Express posted on Breachforums "
            "for months in 2025 using fake stores."
        )
        trimmed = _trim_boilerplate_lines(text)
        self.assertIn("posted on Breachforums", trimmed)

    def test_leading_nav_words_removed(self) -> None:
        text = "Home\nBlog\n\nFirst real paragraph here."
        trimmed = _trim_boilerplate_lines(text)
        self.assertTrue(trimmed.startswith("First real paragraph"))


class ArticleHostSwapTest(unittest.TestCase):
    FEED_XML = (
        '<rss version="2.0"><channel><title>V</title>'
        "<item><title>Obfuscation III</title>"
        "<link>https://dead.example/general/2026/08/21/post.html</link>"
        "<pubDate>Wed, 02 Sep 2026 06:00:00 +0000</pubDate>"
        "<description></description></item>"
        "</channel></rss>"
    ).encode("utf-8")
    PAGE_HTML = (
        "<html><body><p>" + "Recovered article body. " * 40 + "</p></body></html>"
    ).encode("utf-8")

    def test_dead_entry_domain_retried_on_feed_host(self) -> None:
        calls: list[str] = []

        def fake_fetch(url, *args, **kwargs):
            calls.append(url)
            if "dead.example" in url:
                raise RuntimeError("DNS fail")
            if len(calls) == 1:
                return self.FEED_XML
            return self.PAGE_HTML

        with patch("feeds._fetch_feed_bytes", side_effect=fake_fetch):
            posts = fetch_recent_posts(
                "https://live.example/feed.xml",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertIn("Recovered article body.", posts[0].content_text)
        self.assertTrue(any("live.example/general/2026/08/21/post.html" in c for c in calls))


class WholePageExtractionTest(unittest.TestCase):
    def test_containerless_page_falls_back_to_whole_soup(self) -> None:
        feed_xml = (
            '<rss version="2.0"><channel><title>T</title>'
            "<item><title>Containerless</title>"
            "<link>https://example.com/post</link>"
            "<pubDate>Wed, 02 Sep 2026 06:00:00 +0000</pubDate>"
            "<description>Excerpt</description></item>"
            "</channel></rss>"
        ).encode("utf-8")
        page_html = (
            "<html><div><p>" + "Bare page body content. " * 60 + "</p></div></html>"
        ).encode("utf-8")
        calls: list[str] = []

        def fake_fetch(url, *args, **kwargs):
            calls.append(url)
            return feed_xml if len(calls) == 1 else page_html

        with patch("feeds._fetch_feed_bytes", side_effect=fake_fetch):
            posts = fetch_recent_posts(
                "https://example.com/feed",
                window_hours=24,
                cutoff=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )

        self.assertIn("Bare page body content.", posts[0].content_text)


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

    def test_handles_pre_epoch_timestamp_without_crashing(self) -> None:
        # datetime.fromtimestamp rejects negative values on Windows with
        # OSError [Errno 22]; such entries must be skipped (None) or produce
        # their pre-1970 datetime (Linux) — but never crash the feed.
        pre_epoch = time.struct_time((1969, 12, 30, 0, 0, 0, 1, 363, 0))
        entry = {"published_parsed": pre_epoch}
        parsed = _extract_entry_datetime(entry)
        if parsed is not None:
            self.assertLess(parsed.year, 1970)


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
        rss_payload = (
            b'<rss version="2.0"><channel><title>Example</title>'
            b"<item><title>ok</title></item>"
            b"</channel></rss>"
        )

        def fake_fetch(url, *args, **kwargs):
            if url == "https://example.com/rss.xml":
                return rss_payload
            return html_payload

        with patch("feeds._fetch_feed_bytes", side_effect=fake_fetch):
            parsed = _parse_feed(
                "https://example.com/feed",
                site_url="https://example.com",
            )

        self.assertEqual(len(parsed.entries), 1)
        self.assertEqual(parsed.entries[0]["title"], "ok")

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


class ParseFeedBudgetTest(unittest.TestCase):
    def test_stops_after_max_candidate_urls(self) -> None:
        # HTML page with no alternate links forces the suffix-candidate path;
        # the candidate list is far longer than the budget.
        html_payload = b"<html><head></head><body>no links here</body></html>"

        with patch("feeds._fetch_feed_bytes", return_value=html_payload):
            with self.assertRaises(RuntimeError) as ctx:
                _parse_feed("https://example.com/feed", max_candidates=4)

        self.assertIn("exceeded 4 candidate URLs", str(ctx.exception))

    def test_stops_when_deadline_is_exhausted(self) -> None:
        html_payload = b"<html><head></head><body>no links here</body></html>"

        with patch("feeds._fetch_feed_bytes", return_value=html_payload):
            with self.assertRaises(RuntimeError) as ctx:
                _parse_feed(
                    "https://example.com/feed",
                    deadline=time.monotonic() - 1,
                )

        self.assertIn("time budget exhausted", str(ctx.exception))

    def test_returns_empty_valid_feed_without_recovery(self) -> None:
        empty_payload = b'<rss version="2.0"><channel><title>Empty</title></channel></rss>'
        fetch_calls: list[str] = []

        def allow_single_fetch(url):
            fetch_calls.append(url)
            if len(fetch_calls) > 1:
                raise AssertionError("recovery should not fetch anything")
            return empty_payload

        with patch("feeds._fetch_feed_bytes", side_effect=allow_single_fetch):
            parsed = _parse_feed("https://example.com/feed")

        self.assertFalse(parsed.bozo)
        self.assertEqual(len(parsed.entries), 0)


if __name__ == "__main__":
    unittest.main()
