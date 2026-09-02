from __future__ import annotations

import unittest
from datetime import datetime, timezone

from feeds import FeedPost
from main import (
    _post_key,
    _split_posts_for_email,
    _summary_fallback_from_translation,
    _summary_is_usable,
    load_feed_configs_from_file,
)


def _post(
    url: str,
    *,
    title: str = "Post",
    content_html: str = "<p>body</p>",
    translation: str | None = None,
    source: str = "Example",
) -> FeedPost:
    return FeedPost(
        id=url,
        url=url,
        title=title,
        published=datetime(2026, 9, 2, tzinfo=timezone.utc),
        content_html=content_html,
        content_text="body",
        source=source,
        feed_url="https://example.com/feed",
        translation=translation,
    )


class PostKeyTest(unittest.TestCase):
    def test_same_article_in_two_feeds_collapses_to_one_key(self) -> None:
        blog = _post("https://blog.example.com/2026/09/post/")
        mastodon = _post("https://blog.example.com/2026/09/post", source="Mastodon")

        self.assertEqual(_post_key(blog), _post_key(mastodon))

    def test_tracking_parameters_are_stripped(self) -> None:
        tracked = _post(
            "https://example.com/p?utm_source=rss&fbclid=abc&keep=1"
        )
        clean = _post("https://example.com/p?keep=1")

        self.assertEqual(_post_key(tracked), _post_key(clean))

    def test_www_and_scheme_case_are_normalized(self) -> None:
        a = _post("https://WWW.Example.com/p")
        b = _post("https://example.com/p")

        self.assertEqual(_post_key(a), _post_key(b))

    def test_falls_back_to_source_and_id_without_url(self) -> None:
        post = _post("")
        post.id = "guid-1"

        self.assertEqual(_post_key(post), f"{post.source}:guid-1")


class SplitPostsForEmailTest(unittest.TestCase):
    def test_splits_by_max_posts(self) -> None:
        posts = [_post(f"https://example.com/{i}") for i in range(7)]

        batches = _split_posts_for_email(posts, max_posts=3, max_bytes=10**9)

        self.assertEqual([len(b) for b in batches], [3, 3, 1])

    def test_splits_by_estimated_size(self) -> None:
        posts = [
            _post("https://example.com/1", content_html="<p>" + "x" * 4000 + "</p>"),
            _post("https://example.com/2", content_html="<p>" + "y" * 4000 + "</p>"),
            _post("https://example.com/3", content_html="<p>" + "z" * 4000 + "</p>"),
        ]

        # Each post estimates to ~6KB, so two fit under 12500 bytes; the
        # third starts a new email.
        batches = _split_posts_for_email(posts, max_posts=10, max_bytes=12500)

        self.assertEqual([len(b) for b in batches], [2, 1])

    def test_oversized_single_post_still_delivered_alone(self) -> None:
        huge = _post("https://example.com/huge", content_html="x" * 200_000)
        small = _post("https://example.com/small")

        batches = _split_posts_for_email([huge, small], max_posts=10, max_bytes=1000)

        self.assertEqual([len(b) for b in batches], [1, 1])
        self.assertIs(batches[0][0], huge)

    def test_empty_posts_yield_single_batch_for_send_empty(self) -> None:
        self.assertEqual(_split_posts_for_email([], max_posts=5, max_bytes=1000), [[]])


class SummaryFallbackTest(unittest.TestCase):
    def test_uses_translation_prefix_when_summary_unusable(self) -> None:
        fallback = _summary_fallback_from_translation(
            "這是一段足夠長的中文翻譯，用來作為摘要的替代品，包含技術細節與結論。",
            "Chinese (Traditional)",
        )

        self.assertIsNotNone(fallback)
        self.assertTrue(_summary_is_usable(fallback, "Chinese (Traditional)"))

    def test_rejects_error_markers_and_english(self) -> None:
        self.assertIsNone(
            _summary_fallback_from_translation("[Translation error: boom]", "Chinese (Traditional)")
        )
        self.assertIsNone(
            _summary_fallback_from_translation(
                "This is a long English translation that should not become a summary.",
                "Chinese (Traditional)",
            )
        )

    def test_truncates_long_fallback(self) -> None:
        fallback = _summary_fallback_from_translation(
            "這是摘要" * 100, "Chinese (Traditional)"
        )

        self.assertLessEqual(len(fallback), 203)
        self.assertTrue(fallback.endswith("..."))

    def test_summary_is_usable_rejects_error_markers(self) -> None:
        self.assertFalse(
            _summary_is_usable("[Translation skipped: rate limited]", "Chinese (Traditional)")
        )
        self.assertTrue(_summary_is_usable("這是一段可用的中文摘要內容。", "Chinese (Traditional)"))


class PinnedFeedConfigTest(unittest.TestCase):
    def test_loads_pinned_flag_from_catalog(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.json"
            path.write_text(
                json.dumps(
                    [
                        {"feed": "https://a.example.com/feed"},
                        {"feed": "https://b.example.com/feed", "pinned": True},
                    ]
                ),
                encoding="utf-8",
            )
            configs = load_feed_configs_from_file(str(path))

        self.assertFalse(configs[0].pinned)
        self.assertTrue(configs[1].pinned)


if __name__ == "__main__":
    unittest.main()
