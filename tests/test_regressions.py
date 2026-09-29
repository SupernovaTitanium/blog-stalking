"""Regression tests for the bugs listed in claude_modify.md."""

from __future__ import annotations

import argparse
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import main as main_module
from construct_email import _render_text_with_math
from feeds import FeedConfig, FeedPost
from translation import PostDigest, Translator

# Dummy auth value for the mocked OpenAI client; never a real credential.
_TEST_AUTH_VALUE = "unit-test-dummy"
_GOOD = '{"summary":"摘要","translation":"翻譯"}'


def _response(content: str, finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ]
    )


def _translator(**kwargs) -> Translator:
    return Translator(
        api_key=_TEST_AUTH_VALUE,
        model="glm-5.3",
        target_language="Chinese (Traditional)",
        **kwargs,
    )


# --- Bug 1: "429" substring false positive ---------------------------------


class RateLimitDetectionTest(unittest.TestCase):
    def test_token_count_containing_429_is_not_a_rate_limit(self) -> None:
        exc = Exception(
            "This model's maximum context length is 128000 tokens. "
            "However, you requested 130429 tokens"
        )
        self.assertFalse(_translator()._is_rate_limit_error(exc))

    def test_real_rate_limits_are_still_detected(self) -> None:
        by_status = Exception("boom")
        by_status.status_code = 429
        by_text = Exception("Too Many Requests")
        self.assertTrue(_translator()._is_rate_limit_error(by_status))
        self.assertTrue(_translator()._is_rate_limit_error(by_text))


# --- Bug 2: failed digests must be detectable ------------------------------


class PostDigestFailedTest(unittest.TestCase):
    def test_error_and_rate_limited_markers_are_failures(self) -> None:
        self.assertTrue(PostDigest("x", Translator.INVALID_RESPONSE).failed)
        self.assertTrue(PostDigest("x", "[Translation error: boom]").failed)
        self.assertTrue(PostDigest("x", Translator.RATE_LIMITED).failed)

    def test_success_and_permanent_skips_are_not_failures(self) -> None:
        self.assertFalse(PostDigest("摘要", "完整翻譯").failed)
        self.assertFalse(PostDigest("", "").failed)
        self.assertFalse(PostDigest(Translator.FILTERED, Translator.FILTERED).failed)
        self.assertFalse(PostDigest(Translator.TRUNCATED, Translator.TRUNCATED).failed)


# --- Bug 3: finish_reason == "length" --------------------------------------


class TruncatedOutputTest(unittest.TestCase):
    def test_truncated_response_is_split_and_retried(self) -> None:
        translator = _translator()
        paragraphs = ["第一段內容 " * 30, "第二段內容 " * 30]
        text = "\n\n".join(paragraphs)
        create_mock = MagicMock(
            side_effect=[
                _response('{"summary":"摘', finish_reason="length"),
                _response(_GOOD),
                _response(_GOOD),
            ]
        )
        translator.client.chat.completions.create = create_mock

        digest = translator.digest_texts([text])[0]

        self.assertEqual(create_mock.call_count, 3)
        self.assertEqual(digest.translation, "翻譯\n\n翻譯")
        self.assertFalse(digest.failed)

    def test_unsplittable_truncation_gets_permanent_marker(self) -> None:
        translator = _translator()
        create_mock = MagicMock(return_value=_response("{", finish_reason="length"))
        translator.client.chat.completions.create = create_mock

        digest = translator.digest_texts(["short text"])[0]

        self.assertEqual(digest.translation, Translator.TRUNCATED)
        self.assertFalse(digest.failed)


# --- Minor: "$5 ... $10" is not math ---------------------------------------


class CurrencyIsNotMathTest(unittest.TestCase):
    def test_prices_stay_plain_text(self) -> None:
        rendered = _render_text_with_math("售價 $5，另一款售價 $10，差距很小。")
        self.assertNotIn("math-inline", rendered)
        self.assertIn("$5", rendered)
        self.assertIn("$10", rendered)

    def test_real_inline_math_still_renders(self) -> None:
        for source in ("結論是 $E=mc^2$。", "令 $x$ 與 $y$ 為實數", "見 $n$-th 項"):
            self.assertIn("math-inline", _render_text_with_math(source), source)

    def test_math_starting_with_a_digit_still_renders(self) -> None:
        self.assertIn("math-inline", _render_text_with_math("令 $2x+1$ 為奇數"))


# --- Bugs 2/4 + minor: run-state handling in main() ------------------------


def _post(url: str, feed_url: str = "https://a.example.com/feed") -> FeedPost:
    return FeedPost(
        id=url,
        url=url,
        title=url,
        published=datetime.now(timezone.utc) - timedelta(hours=1),
        content_html="<p>body</p>",
        content_text="body text",
        source="Example",
        feed_url=feed_url,
    )


class MainRunStateTest(unittest.TestCase):
    """Drives main.main() end to end with the network/LLM/SMTP stubbed out."""

    def _run(
        self,
        *,
        fetch,
        digest_side_effect=None,
        send_side_effect=None,
        extra_args=(),
        feeds=("https://a.example.com/feed", "https://b.example.com/feed"),
    ) -> tuple[dict, Exception | None, MagicMock]:
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            feed_list = Path(tmp) / "feeds.json"
            feed_list.write_text(json.dumps([{"feed": url} for url in feeds]))

            def fake_digest(self_, texts):
                if digest_side_effect is not None:
                    return digest_side_effect(texts)
                return [PostDigest("摘要", "翻譯") for _ in texts]

            argv = [
                "main.py",
                "--feed_list", str(feed_list),
                "--state_file", str(state_path),
                "--email_html_dir", "none",
                "--openai_api_key", _TEST_AUTH_VALUE,
                "--openai_model", "m",
                "--smtp_server", "smtp.example.com",
                "--sender", "a@example.com",
                "--sender_password", _TEST_AUTH_VALUE,
                "--receiver", "b@example.com",
                *extra_args,
            ]
            send_mock = MagicMock(side_effect=send_side_effect)
            error: Exception | None = None
            # Fresh parser per call: main.py builds a module-level parser.
            with patch.object(main_module, "parser", argparse.ArgumentParser()), \
                 patch("sys.argv", argv), \
                 patch.object(main_module, "fetch_recent_posts", side_effect=fetch), \
                 patch.object(Translator, "digest_texts", fake_digest), \
                 patch.object(main_module, "send_email", send_mock):
                try:
                    main_module.main()
                except Exception as exc:  # noqa: BLE001 - asserted by callers
                    error = exc
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            return state, error, send_mock

    def test_failed_translation_is_not_marked_delivered_and_run_fails(self) -> None:
        good, bad = _post("https://a.example.com/good"), _post("https://a.example.com/bad")

        def fetch(url, *args, **kwargs):
            return [good, bad] if "a.example" in url else []

        def digest(texts):
            return [
                PostDigest("摘要", "翻譯"),
                PostDigest("[Translation error: boom]", "[Translation error: boom]"),
            ]

        state, error, send_mock = self._run(fetch=fetch, digest_side_effect=digest)

        self.assertIsNotNone(error)
        self.assertIn("held back for retry", str(error))
        self.assertEqual(send_mock.call_count, 1)  # the good post still ships
        self.assertEqual(state["seen_posts"], [main_module._post_key(good)])

    def test_failed_feed_holds_the_window_back(self) -> None:
        def fetch(url, *args, **kwargs):
            if "b.example" in url:
                raise RuntimeError("Failed to parse feed")
            return [_post("https://a.example.com/p")]

        before = datetime.now(timezone.utc)
        state, error, _ = self._run(fetch=fetch)

        self.assertIsNone(error)
        window_end = datetime.fromisoformat(state["window_end"])
        # Held at (run cutoff + grace), i.e. ~24h ago, not "now".
        self.assertLess(window_end, before - timedelta(hours=23))

    def test_healthy_run_advances_the_window(self) -> None:
        before = datetime.now(timezone.utc)
        state, error, _ = self._run(
            fetch=lambda url, *a, **k: [_post("https://a.example.com/p")]
            if "a.example" in url
            else []
        )

        self.assertIsNone(error)
        self.assertGreaterEqual(datetime.fromisoformat(state["window_end"]), before)

    def test_partial_email_failure_records_only_sent_posts(self) -> None:
        posts = [_post(f"https://a.example.com/{i}") for i in range(4)]

        def fetch(url, *args, **kwargs):
            return posts if "a.example" in url else []

        state, error, send_mock = self._run(
            fetch=fetch,
            extra_args=("--email_max_posts", "2"),
            send_side_effect=[None, RuntimeError("smtp down")],
        )

        self.assertIsNotNone(error)
        self.assertEqual(send_mock.call_count, 2)
        self.assertEqual(
            state["seen_posts"],
            [main_module._post_key(p) for p in posts[:2]],
        )

    def test_max_post_num_cut_holds_the_window_back(self) -> None:
        posts = [_post(f"https://a.example.com/{i}") for i in range(3)]

        def fetch(url, *args, **kwargs):
            return posts if "a.example" in url else []

        before = datetime.now(timezone.utc)
        state, error, _ = self._run(fetch=fetch, extra_args=("--max_post_num", "1"))

        self.assertIsNone(error)
        self.assertLess(datetime.fromisoformat(state["window_end"]), before - timedelta(hours=23))
        self.assertEqual(len(state["seen_posts"]), 1)


if __name__ == "__main__":
    unittest.main()
