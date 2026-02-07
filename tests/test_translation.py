from __future__ import annotations

import sys
from types import SimpleNamespace
import types
import unittest
from unittest.mock import MagicMock

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


if __name__ == "__main__":
    unittest.main()
