from __future__ import annotations

import sys
from types import SimpleNamespace
import types
import unittest
from unittest.mock import MagicMock, patch

# Allow local unit tests to run without installing runtime deps.
fake_loguru = types.ModuleType("loguru")
fake_loguru.logger = SimpleNamespace(
    warning=lambda *args, **kwargs: None,
    exception=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    debug=lambda *args, **kwargs: None,
)
sys.modules.setdefault("loguru", fake_loguru)

fake_openai = types.ModuleType("openai")


class _DummyBadRequestError(Exception):
    pass


class _DummyRateLimitError(Exception):
    status_code = 429


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: None)
        )


fake_openai.OpenAI = _DummyOpenAI
fake_openai.BadRequestError = _DummyBadRequestError

# Force the fake module even when the real openai was already imported by an
# earlier test file (e.g. test_main imports main -> translation -> openai),
# then reload so translation binds to the fake client that records kwargs.
sys.modules["openai"] = fake_openai

import importlib

import translation as _translation_module

_translation_module = importlib.reload(_translation_module)
from translation import OpenAITranslator


class FeedTranslationJsonSchemaTest(unittest.TestCase):
    def _build_translator(self) -> OpenAITranslator:
        return OpenAITranslator(
            api_key="test-key",
            model="gpt-4o-mini",
            target_language="Chinese (Traditional)",
        )

    def test_requests_json_schema_and_parses_summaries(self) -> None:
        translator = self._build_translator()
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content='{"summaries":["第一則摘要","第二則摘要"]}'
                    ),
                )
            ]
        )
        create_mock = MagicMock(return_value=mock_response)
        translator.client.chat.completions.create = create_mock

        result = translator._translate_feed_once("feed://test", ["a", "b"])

        self.assertEqual(result, ["第一則摘要", "第二則摘要"])
        response_format = create_mock.call_args.kwargs.get("response_format")
        self.assertIsInstance(response_format, dict)
        self.assertEqual(response_format.get("type"), "json_schema")
        self.assertEqual(
            response_format.get("json_schema", {}).get("name"),
            "feed_summaries",
        )

    def test_invalid_structured_output_returns_explicit_error(self) -> None:
        translator = self._build_translator()

        result = translator._parse_feed_summaries("not-json-response", expected=2)

        self.assertEqual(
            result,
            [
                OpenAITranslator.INVALID_STRUCTURED_RESPONSE,
                OpenAITranslator.INVALID_STRUCTURED_RESPONSE,
            ],
        )

    def test_translate_batch_uses_structured_translation_json(self) -> None:
        translator = self._build_translator()
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content='{"translation":"完整中文翻譯"}'),
                )
            ]
        )
        create_mock = MagicMock(return_value=mock_response)
        translator.client.chat.completions.create = create_mock

        result = translator.translate_batch(["source text"])

        self.assertEqual(result, ["完整中文翻譯"])
        response_format = create_mock.call_args.kwargs.get("response_format")
        self.assertIsInstance(response_format, dict)
        self.assertEqual(response_format.get("type"), "json_schema")
        self.assertEqual(
            response_format.get("json_schema", {}).get("name"),
            "post_translation",
        )

    def test_translate_batch_invalid_json_returns_marker(self) -> None:
        translator = self._build_translator()
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="invalid-json-output"),
                )
            ]
        )
        translator.client.chat.completions.create = MagicMock(return_value=mock_response)

        result = translator.translate_batch(["source text"])

        self.assertEqual(result, [OpenAITranslator.INVALID_TRANSLATION_RESPONSE])

    def test_parse_translation_text_rejects_long_english_for_chinese_target(self) -> None:
        translator = self._build_translator()

        result = translator._parse_translation_text(
            '{"translation":"This is a long English sentence that should not pass for a Chinese translation output."}'
        )

        self.assertEqual(result, OpenAITranslator.INVALID_TRANSLATION_RESPONSE)

    def test_parse_feed_summaries_rejects_long_english_for_chinese_target(self) -> None:
        translator = self._build_translator()

        result = translator._parse_feed_summaries(
            '{"summaries":["This is a long English summary that should be rejected for Chinese target language."]}',
            expected=1,
        )

        self.assertEqual(result, [OpenAITranslator.INVALID_STRUCTURED_RESPONSE])

    def test_nvidia_provider_uses_openai_compatible_streaming_payload(self) -> None:
        translator = OpenAITranslator(
            api_key="nv-key",
            provider="nvidia",
            model="z-ai/glm-5.2",
            target_language="Chinese (Traditional)",
        )
        self.assertEqual(
            translator.client.kwargs["base_url"],
            "https://integrate.api.nvidia.com/v1",
        )
        translator._nvidia_limiter.wait = MagicMock()
        create_mock = MagicMock(
            return_value=iter(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                finish_reason=None,
                                delta=SimpleNamespace(
                                    content='{"translation":"完整中文翻譯"}'
                                ),
                            )
                        ]
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                finish_reason="stop",
                                delta=SimpleNamespace(content=None),
                            )
                        ]
                    ),
                ]
            )
        )
        translator.client.chat.completions.create = create_mock

        result = translator.translate_batch(["source text"])

        self.assertEqual(result, ["完整中文翻譯"])
        kwargs = create_mock.call_args.kwargs
        self.assertEqual(kwargs["model"], "z-ai/glm-5.2")
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["temperature"], 1)
        self.assertEqual(kwargs["max_tokens"], 16384)
        self.assertEqual(kwargs["top_p"], 0.95)

    def test_nvidia_legacy_chat_completions_url_is_coerced_to_base_url(self) -> None:
        translator = OpenAITranslator(
            api_key="nv-key",
            provider="nvidia",
            model="z-ai/glm-5.2",
            target_language="Chinese (Traditional)",
            nvidia_api_url="https://integrate.api.nvidia.com/v1/chat/completions",
        )

        self.assertEqual(
            translator._coerce_nvidia_base_url(
                nvidia_base_url=None,
                nvidia_api_url="https://integrate.api.nvidia.com/v1/chat/completions",
            ),
            "https://integrate.api.nvidia.com/v1",
        )

    def test_nvidia_rate_limit_retries_before_returning_translation(self) -> None:
        translator = OpenAITranslator(
            api_key="nv-key",
            provider="nvidia",
            model="z-ai/glm-5.2",
            target_language="Chinese (Traditional)",
            rate_limit_retries=1,
            rate_limit_base_sleep=1,
        )
        translator._nvidia_limiter.wait = MagicMock()
        create_mock = MagicMock(
            side_effect=[
                _DummyRateLimitError("Error code: 429 - Too Many Requests"),
                iter(
                    [
                        SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    finish_reason="stop",
                                    delta=SimpleNamespace(
                                        content='{"translation":"完整中文翻譯"}'
                                    ),
                                )
                            ]
                        )
                    ]
                ),
            ]
        )
        translator.client.chat.completions.create = create_mock

        with patch("translation.time.sleep") as sleep_mock:
            result = translator.translate_batch(["source text"])

        self.assertEqual(result, ["完整中文翻譯"])
        self.assertEqual(create_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.0)
        self.assertEqual(
            translator.client.kwargs["base_url"],
            "https://integrate.api.nvidia.com/v1",
        )

    def test_nvidia_rate_limit_exhaustion_opens_skip_circuit(self) -> None:
        translator = OpenAITranslator(
            api_key="nv-key",
            provider="nvidia",
            model="z-ai/glm-5.2",
            target_language="Chinese (Traditional)",
            rate_limit_retries=1,
            rate_limit_base_sleep=1,
        )
        translator._nvidia_limiter.wait = MagicMock()
        create_mock = MagicMock(
            side_effect=[
                _DummyRateLimitError("Error code: 429 - Too Many Requests"),
                _DummyRateLimitError("Error code: 429 - Too Many Requests"),
            ]
        )
        translator.client.chat.completions.create = create_mock

        with patch("translation.time.sleep") as sleep_mock:
            summaries = translator.translate_batch_by_feed(
                ["source text"],
                ["feed://test"],
            )
            translations = translator.translate_batch(["source text"])

        self.assertEqual(summaries, ["[Translation skipped: rate limited]"])
        self.assertEqual(translations, ["[Translation skipped: rate limited]"])
        self.assertEqual(create_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
