from __future__ import annotations

import json
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


class _DummyOpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: None)
        )


fake_openai.OpenAI = _DummyOpenAI
fake_openai.BadRequestError = _DummyBadRequestError
sys.modules.setdefault("openai", fake_openai)

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

    def test_nvidia_provider_uses_streaming_payload(self) -> None:
        translator = OpenAITranslator(
            api_key="nv-key",
            provider="nvidia",
            model="z-ai/glm-5.2",
            target_language="Chinese (Traditional)",
        )
        translator._nvidia_limiter.wait = MagicMock()

        class FakeResponse:
            def __enter__(self):
                return iter(
                    [
                        (
                            'data: {"choices":[{"delta":{"content":"{\\"translation\\":'
                            '\\"完整中文翻譯\\"}"}}]}\n'
                        ).encode("utf-8"),
                        b"data: [DONE]\n",
                    ]
                )

            def __exit__(self, exc_type, exc, traceback):
                return False

        with patch("translation.urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            result = translator.translate_batch(["source text"])

        self.assertEqual(result, ["完整中文翻譯"])
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "z-ai/glm-5.2")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["temperature"], 1)
        self.assertEqual(payload["max_tokens"], 16384)
        self.assertEqual(payload["top_p"], 0.95)
        self.assertEqual(request.get_header("Authorization"), "Bearer nv-key")
        self.assertEqual(request.get_header("Accept"), "text/event-stream")


if __name__ == "__main__":
    unittest.main()
