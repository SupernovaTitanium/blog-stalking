from __future__ import annotations

import unittest
from datetime import datetime, timezone

from feeds import FeedPost, load_feed_configs_from_file
from main import (
    _post_key,
    _split_posts_for_email,
    _truncate_for_translation,
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


class TranslationTruncationTest(unittest.TestCase):
    def test_short_text_passes_through(self) -> None:
        self.assertEqual(_truncate_for_translation("短文", 4000), "短文")

    def test_cap_zero_or_negative_disables_cap(self) -> None:
        long_text = "x" * 9000
        self.assertEqual(_truncate_for_translation(long_text, 0), long_text)
        self.assertEqual(_truncate_for_translation(long_text, -1), long_text)

    def test_long_text_is_cut_at_sentence_boundary(self) -> None:
        sentences = ["這是第 %d 句完整的句子。" % i for i in range(400)]
        text = "".join(sentences)

        truncated = _truncate_for_translation(text, 4000)

        self.assertLessEqual(len(truncated), 4000)
        # never a dangling fragment: always ends with a full sentence
        self.assertTrue(truncated.endswith("。"))
        self.assertNotEqual(truncated, text)


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
