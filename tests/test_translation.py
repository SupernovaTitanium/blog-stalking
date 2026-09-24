from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from openai import BadRequestError

from translation import PostDigest, Translator, looks_like_target_language

# Dummy auth value for the mocked OpenAI client; never a real credential.
_TEST_AUTH_VALUE = "unit-test-dummy"


def _bad_request(payload: dict) -> BadRequestError:
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    response = httpx.Response(400, request=request, json=payload)
    return BadRequestError("bad request", response=response, body=None)


def _rate_limit_error() -> Exception:
    exc = Exception("Error code: 429 - Too Many Requests")
    exc.status_code = 429
    return exc


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content),
            )
        ]
    )


class LooksLikeTargetLanguageTest(unittest.TestCase):
    def test_chinese_target_accepts_chinese_and_rejects_english(self) -> None:
        self.assertTrue(looks_like_target_language("這是一段中文摘要。", "Chinese (Traditional)"))
        self.assertFalse(
            looks_like_target_language(
                "This is a long English sentence that should not pass.",
                "Chinese (Traditional)",
            )
        )

    def test_short_strings_are_accepted(self) -> None:
        self.assertTrue(looks_like_target_language("f(x) = x²", "Chinese (Traditional)"))

    def test_non_chinese_targets_are_accepted_as_is(self) -> None:
        self.assertTrue(looks_like_target_language("anything", "French"))


class DigestRequestTest(unittest.TestCase):
    def _translator(self, **kwargs) -> Translator:
        return Translator(
            api_key=_TEST_AUTH_VALUE,
            model="glm-5.3",
            target_language="Chinese (Traditional)",
            **kwargs,
        )

    def test_single_request_returns_summary_and_translation(self) -> None:
        translator = self._translator()
        create_mock = MagicMock(
            return_value=_response('{"summary":"中文摘要","translation":"完整中文翻譯"}')
        )
        translator.client.chat.completions.create = create_mock

        digests = translator.digest_texts(["source text"])

        self.assertEqual(digests, [PostDigest(summary="中文摘要", translation="完整中文翻譯")])
        self.assertEqual(create_mock.call_count, 1)
        response_format = create_mock.call_args.kwargs["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "post_digest")

    def test_invalid_structured_output_returns_markers(self) -> None:
        translator = self._translator()
        translator.client.chat.completions.create = MagicMock(
            return_value=_response("not-json-response")
        )

        digests = translator.digest_texts(["source text"])

        self.assertEqual(
            digests,
            [PostDigest(Translator.INVALID_RESPONSE, Translator.INVALID_RESPONSE)],
        )

    def test_english_translation_is_rejected_for_chinese_target(self) -> None:
        translator = self._translator()
        translator.client.chat.completions.create = MagicMock(
            return_value=_response(
                '{"summary":"這是中文摘要","translation":"This is a long English translation '
                'that should not pass for a Chinese translation output."}'
            )
        )

        digests = translator.digest_texts(["source text"])

        self.assertEqual(digests[0].translation, Translator.INVALID_RESPONSE)

    def test_bad_summary_is_salvaged_from_good_translation(self) -> None:
        translation = "這是一段足夠長的中文翻譯，用來作為摘要的替代品，包含技術細節與結論。"
        translator = self._translator()
        translator.client.chat.completions.create = MagicMock(
            return_value=_response(
                '{"summary":"This is an English summary that must be rejected.","translation":"%s"}'
                % translation
            )
        )

        digests = translator.digest_texts(["source text"])

        self.assertIn("中文翻譯", digests[0].summary)
        self.assertEqual(digests[0].translation, translation)

    def test_oversized_summary_is_clamped(self) -> None:
        translator = self._translator()
        translator.client.chat.completions.create = MagicMock(
            return_value=_response('{"summary":"' + "摘" * 400 + '","translation":"翻譯"}')
        )

        digests = translator.digest_texts(["source text"])

        self.assertLessEqual(len(digests[0].summary), Translator.SUMMARY_MAX_CHARS)

    def test_empty_text_is_free(self) -> None:
        translator = self._translator()
        create_mock = MagicMock(return_value=_response("{}"))
        translator.client.chat.completions.create = create_mock

        digests = translator.digest_texts([""])

        self.assertEqual(digests, [PostDigest(summary="", translation="")])
        create_mock.assert_not_called()


class TranslatorOptionsTest(unittest.TestCase):
    def _translator(self, **kwargs) -> Translator:
        return Translator(
            api_key=_TEST_AUTH_VALUE,
            model="glm-5.3",
            target_language="Chinese (Traditional)",
            **kwargs,
        )

    def test_max_tokens_sent_when_positive(self) -> None:
        translator = self._translator(max_tokens=16384)
        create_mock = MagicMock(
            return_value=_response('{"summary":"摘要","translation":"翻譯"}')
        )
        translator.client.chat.completions.create = create_mock

        translator.digest_texts(["text"])

        self.assertEqual(create_mock.call_args.kwargs["max_tokens"], 16384)

    def test_max_tokens_omitted_when_unset(self) -> None:
        translator = self._translator(max_tokens=None)
        create_mock = MagicMock(
            return_value=_response('{"summary":"摘要","translation":"翻譯"}')
        )
        translator.client.chat.completions.create = create_mock

        translator.digest_texts(["text"])

        self.assertNotIn("max_tokens", create_mock.call_args.kwargs)

    def test_base_url_is_forwarded_to_client(self) -> None:
        translator = self._translator(base_url="https://api.z.ai/api/coding/paas/v4")
        self.assertEqual(
            translator.client.base_url, "https://api.z.ai/api/coding/paas/v4/"
        )


class TranslationChunkingTest(unittest.TestCase):
    LONG_TEXT = ("Paragraph with a few sentences. " * 8 + "\n\n") * 40  # ~13k chars

    def _translator(self, **kwargs) -> Translator:
        return Translator(
            api_key=_TEST_AUTH_VALUE,
            model="glm-5.3",
            target_language="Chinese (Traditional)",
            **kwargs,
        )

    def _mock_create(self, translator) -> MagicMock:
        create_mock = MagicMock(
            return_value=_response('{"summary":"中文摘要","translation":"完整中文翻譯"}')
        )
        translator.client.chat.completions.create = create_mock
        return create_mock

    def test_default_chunk_chars_sends_whole_article_in_one_request(self) -> None:
        translator = self._translator()
        create_mock = self._mock_create(translator)

        translator.digest_texts([self.LONG_TEXT])

        self.assertEqual(create_mock.call_count, 1)
        sent = create_mock.call_args.kwargs["messages"][1]["content"]
        self.assertEqual(len(sent), len(self.LONG_TEXT))

    def test_positive_chunk_chars_splits_requests(self) -> None:
        translator = self._translator(chunk_chars=300)
        create_mock = self._mock_create(translator)

        digests = translator.digest_texts([self.LONG_TEXT])

        self.assertGreater(create_mock.call_count, 1)
        for call in create_mock.call_args_list:
            self.assertLessEqual(len(call.kwargs["messages"][1]["content"]), 300)
        # Summary comes from the first chunk; translations are joined.
        self.assertEqual(digests[0].summary, "中文摘要")
        self.assertIn("完整中文翻譯", digests[0].translation)


class RetryPolicyTest(unittest.TestCase):
    def _translator(self, **kwargs) -> Translator:
        return Translator(
            api_key=_TEST_AUTH_VALUE,
            model="glm-5.3",
            target_language="Chinese (Traditional)",
            **kwargs,
        )

    def test_rate_limit_is_retried_then_succeeds(self) -> None:
        translator = self._translator(rate_limit_retries=1, rate_limit_base_sleep=1)
        create_mock = MagicMock(
            side_effect=[
                _rate_limit_error(),
                _response('{"summary":"摘要","translation":"翻譯"}'),
            ]
        )
        translator.client.chat.completions.create = create_mock

        with patch("translation.time.sleep") as sleep_mock:
            digests = translator.digest_texts(["source text"])

        self.assertEqual(digests[0].translation, "翻譯")
        self.assertEqual(create_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.0)

    def test_rate_limit_exhaustion_opens_skip_circuit(self) -> None:
        translator = self._translator(rate_limit_retries=0)
        create_mock = MagicMock(side_effect=_rate_limit_error())
        translator.client.chat.completions.create = create_mock

        with patch("translation.time.sleep"):
            first = translator.digest_texts(["source text"])
            second = translator.digest_texts(["source text"])

        self.assertEqual(first[0].summary, Translator.RATE_LIMITED)
        self.assertEqual(first[0].translation, Translator.RATE_LIMITED)
        # The circuit stays open: no further API calls are made.
        self.assertEqual(create_mock.call_count, 1)
        self.assertEqual(second[0].translation, Translator.RATE_LIMITED)

    def test_transient_server_errors_are_retried(self) -> None:
        translator = self._translator(transient_retries=1, transient_base_sleep=1)
        exc = Exception("boom")
        exc.status_code = 503
        create_mock = MagicMock(
            side_effect=[exc, _response('{"summary":"摘要","translation":"翻譯"}')]
        )
        translator.client.chat.completions.create = create_mock

        with patch("translation.time.sleep"):
            digests = translator.digest_texts(["source text"])

        self.assertEqual(digests[0].translation, "翻譯")
        self.assertEqual(create_mock.call_count, 2)

    def test_unsupported_response_format_falls_back_to_json_object(self) -> None:
        translator = self._translator()
        create_mock = MagicMock(
            side_effect=[
                _bad_request(
                    {
                        "error": {
                            "code": "unsupported",
                            "message": "json_schema not supported",
                        }
                    }
                ),
                _response('{"summary":"摘要","translation":"翻譯"}'),
            ]
        )
        translator.client.chat.completions.create = create_mock

        digests = translator.digest_texts(["source text"])

        self.assertEqual(digests[0].translation, "翻譯")
        self.assertEqual(create_mock.call_count, 2)
        self.assertEqual(
            create_mock.call_args_list[-1].kwargs["response_format"],
            {"type": "json_object"},
        )

    def test_content_filter_marks_unsplittable_chunk(self) -> None:
        translator = self._translator()
        create_mock = MagicMock(
            side_effect=_bad_request(
                {"error": {"code": "content_filter", "message": "ResponsibleAIPolicyViolation"}}
            )
        )
        translator.client.chat.completions.create = create_mock

        digests = translator.digest_texts(["too short to split"])

        self.assertEqual(digests[0].summary, Translator.FILTERED)
        self.assertEqual(digests[0].translation, Translator.FILTERED)


class ParallelDigestTest(unittest.TestCase):
    def test_all_texts_are_processed_in_order(self) -> None:
        translator = Translator(
            api_key=_TEST_AUTH_VALUE,
            model="glm-5.3",
            target_language="Chinese (Traditional)",
            workers=3,
        )
        create_mock = MagicMock(
            side_effect=lambda **kwargs: _response(
                '{"summary":"摘要","translation":"翻譯 %d"}'
                % len(kwargs["messages"][1]["content"])
            )
        )
        translator.client.chat.completions.create = create_mock

        texts = [f"source text {i}" for i in range(5)]
        digests = translator.digest_texts(texts)

        self.assertEqual([d.translation for d in digests], ["翻譯 13"] * 5)


if __name__ == "__main__":
    unittest.main()
