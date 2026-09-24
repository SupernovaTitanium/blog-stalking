"""LLM digest generation over any OpenAI-compatible chat-completions API.

One request per article returns both the quick summary and the full
translation as structured JSON, so a run with N posts costs N requests.
Articles larger than ``chunk_chars`` are split; the summary is taken from
the first chunk. Requests run in a small thread pool.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Sequence

from loguru import logger
from openai import BadRequestError, OpenAI


class ContentFilterTriggeredError(Exception):
    """Raised when the model returns a content-filtered response."""


class RateLimitExhaustedError(Exception):
    """Raised after provider rate-limit retries have been exhausted."""


def looks_like_target_language(text: str | None, target_language: str | None) -> bool:
    """Sanity check that LLM output actually matches the target language.

    Only Chinese targets get a heuristic (counting CJK vs. Latin characters);
    every other target language is accepted as-is. Short strings are accepted
    because they may legitimately be mostly symbols or math.
    """
    if not text:
        return False
    target = (target_language or "").lower()
    if "chinese" not in target and "中文" not in target:
        return True
    stripped = text.strip()
    if len(stripped) <= 24:
        return True
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", stripped))
    latin_count = len(re.findall(r"[A-Za-z]", stripped))
    return cjk_count >= 4 or (cjk_count >= 2 and latin_count <= cjk_count * 4)


@dataclass
class PostDigest:
    summary: str
    translation: str


class Translator:
    INVALID_RESPONSE = "[Translation error: invalid structured response]"
    RATE_LIMITED = "[Translation skipped: rate limited]"
    FILTERED = "[Translation skipped: blocked by content filter]"
    SUMMARY_MAX_CHARS = 200

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        target_language: str,
        base_url: str | None = None,
        max_tokens: int | None = None,
        chunk_chars: int = -1,
        workers: int = 4,
        rate_limit_retries: int = 4,
        rate_limit_base_sleep: float = 65.0,
        transient_retries: int = 2,
        transient_base_sleep: float = 5.0,
    ):
        self.client = OpenAI(
            api_key=(api_key or "").strip(),
            base_url=(base_url or "").strip() or None,
        )
        self.model = model
        self.target_language = target_language
        # Output cap for the request. Aggregators like OpenRouter price
        # requests against the model's maximum when max_tokens is unset and
        # reject (402) when that exceeds the key's credit budget.
        self.max_tokens = int(max_tokens) if max_tokens else None
        # >0: split articles at this size; <=0: one request per article.
        self.chunk_chars = int(chunk_chars)
        self.workers = max(1, int(workers))
        self.rate_limit_retries = max(0, int(rate_limit_retries))
        self.rate_limit_base_sleep = max(1.0, float(rate_limit_base_sleep))
        self.transient_retries = max(0, int(transient_retries))
        self.transient_base_sleep = max(0.5, float(transient_base_sleep))
        self._max_filter_depth = 3
        self._rate_limit_exhausted = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def digest_texts(self, texts: Sequence[str]) -> list[PostDigest]:
        """Summarize + translate every text, in parallel."""
        if not texts:
            return []
        workers = min(self.workers, len(texts))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self._digest_text, texts))

    # ------------------------------------------------------------------
    # Per-article pipeline
    # ------------------------------------------------------------------

    def _digest_text(self, text: str) -> PostDigest:
        if not text:
            return PostDigest(summary="", translation="")
        if self._rate_limit_exhausted:
            return PostDigest(summary=self.RATE_LIMITED, translation=self.RATE_LIMITED)

        chunks = self._chunk_text(text) if self.chunk_chars > 0 else [text]
        summary = ""
        parts: list[str] = []
        for index, chunk in enumerate(chunks):
            chunk_summary, chunk_translation = self._digest_chunk(chunk)
            if index == 0:
                summary = chunk_summary
            parts.append(chunk_translation)

        translation = "\n\n".join(part for part in parts if part).strip()
        if not looks_like_target_language(summary, self.target_language):
            # Salvage a summary from a good translation instead of shipping
            # an error marker next to readable text.
            salvaged = self._first_chars(translation, self.SUMMARY_MAX_CHARS)
            summary = salvaged or summary
        return PostDigest(summary=summary, translation=translation)

    def _digest_chunk(self, chunk: str, *, _depth: int = 0) -> tuple[str, str]:
        prompt = (
            f"你是專業技術編輯。請對使用者提供的內容做兩件事：\n"
            f"1. translation：將內容完整翻譯成 {self.target_language}。"
            "這是全文翻譯，不是摘要，不可省略段落或關鍵資訊；"
            "請保留原始段落結構、數學符號、LaTeX、URL、Markdown 與程式碼區塊。\n"
            f"2. summary：以 {self.target_language} 寫出不超過 "
            f"{self.SUMMARY_MAX_CHARS} 字的摘要，保留核心概念、關鍵步驟與主要結論，"
            "不加主觀評論，維持數學符號、LaTeX、URL、程式碼區塊原樣。\n"
            '必須輸出 JSON 物件，格式為 {"summary": "...", "translation": "..."}。'
        )
        try:
            content, finish_reason = self._chat_with_format_fallback(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": chunk},
                ],
                formats=(self._response_format(), {"type": "json_object"}, None),
            )
            if finish_reason == "content_filter" or not content:
                raise ContentFilterTriggeredError(
                    f"Model returned finish_reason={finish_reason!r}"
                )
            return self._parse_digest(content)
        except ContentFilterTriggeredError as exc:
            return self._handle_content_filter(chunk, _depth, str(exc))
        except RateLimitExhaustedError:
            logger.warning(
                "Digest skipped because provider rate limit was exhausted; "
                "further requests this run will be skipped too."
            )
            self._rate_limit_exhausted = True
            return self.RATE_LIMITED, self.RATE_LIMITED
        except Exception as exc:
            logger.exception("Digest request failed")
            marker = f"[Translation error: {exc}]"
            return marker, marker

    # ------------------------------------------------------------------
    # Provider calls: retry policy + response-format fallback
    # ------------------------------------------------------------------

    def _chat_completion_content(
        self,
        *,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str, str | None]:
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if response_format is not None:
            kwargs["response_format"] = response_format
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens

        def request_once() -> tuple[str, str | None]:
            response = self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            return (choice.message.content or "").strip(), choice.finish_reason

        return self._with_provider_retries(request_once)

    def _with_provider_retries(self, operation):
        rate_attempts = 0
        transient_attempts = 0
        while True:
            try:
                return operation()
            except Exception as exc:
                is_rate_limit = self._is_rate_limit_error(exc)
                is_transient = self._is_transient_error(exc)
                if not is_rate_limit and not is_transient:
                    raise
                if is_rate_limit:
                    if rate_attempts >= self.rate_limit_retries:
                        raise RateLimitExhaustedError(
                            "Provider rate limit remained active after "
                            f"{rate_attempts + 1} request attempt(s): {exc}"
                        ) from exc
                    sleep_for = self._retry_after_seconds(exc, rate_attempts)
                    rate_attempts += 1
                else:
                    if transient_attempts >= self.transient_retries:
                        raise
                    sleep_for = self.transient_base_sleep * (2**transient_attempts)
                    transient_attempts += 1
                logger.warning(
                    "Provider call failed ({}), retrying in {:.1f}s.",
                    "rate limited" if is_rate_limit else "transient error",
                    sleep_for,
                )
                time.sleep(sleep_for)

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return True
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) == 429:
            return True
        text = str(exc).lower()
        return "429" in text or "too many requests" in text or "rate limit" in text

    def _is_transient_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int) and 500 <= status_code < 600:
            return True
        response = getattr(exc, "response", None)
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int) and 500 <= response_status < 600:
            return True
        text = str(exc).lower()
        return any(
            token in text
            for token in (
                "connection error",
                "timed out",
                "timeout",
                "temporarily unavailable",
                "server error",
                "bad gateway",
                "service unavailable",
                "overloaded",
            )
        )

    def _retry_after_seconds(self, exc: Exception, attempt: int) -> float:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        retry_after = None
        if headers is not None:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass
        return self.rate_limit_base_sleep * (attempt + 1)

    def _is_content_filter_error(self, exc: Exception) -> bool:
        if not isinstance(exc, BadRequestError):
            return False
        try:
            data = exc.response.json() if exc.response else None
        except Exception:
            data = None

        if isinstance(data, dict):
            error = data.get("error") or {}
            code = (error.get("code") or "").lower()
            inner = error.get("innererror") or {}
            inner_code = (inner.get("code") or "").lower()
            if "content_filter" in code or "responsibleaipolicyviolation" in inner_code:
                return True
        return "content_filter" in str(exc).lower()

    def _is_response_format_error(self, exc: Exception) -> bool:
        if not isinstance(exc, BadRequestError):
            return False
        try:
            data = exc.response.json() if exc.response else None
        except Exception:
            data = None

        texts: list[str] = [str(exc)]
        if isinstance(data, dict):
            error = data.get("error") or {}
            texts.append(str(error.get("code") or ""))
            texts.append(str(error.get("message") or ""))
            texts.append(str(error.get("type") or ""))

        haystack = " ".join(texts).lower()
        return (
            "response_format" in haystack
            or "json_schema" in haystack
            or "unsupported" in haystack
        )

    def _chat_with_format_fallback(
        self,
        *,
        messages: list[dict[str, str]],
        formats: tuple[dict[str, Any] | None, ...],
    ):
        """Call the model, degrading the response format on unsupported errors.

        Content-filter rejections are converted to ContentFilterTriggeredError
        so callers apply their chunk-splitting recovery.
        """
        last_exc: Exception | None = None
        for index, response_format in enumerate(formats):
            try:
                return self._chat_completion_content(
                    messages=messages, response_format=response_format
                )
            except BadRequestError as exc:
                if self._is_content_filter_error(exc):
                    raise ContentFilterTriggeredError(str(exc)) from exc
                if self._is_response_format_error(exc) and index + 1 < len(formats):
                    logger.warning(
                        "Model {} rejected response_format {!r}; falling back.",
                        self.model,
                        response_format,
                    )
                    last_exc = exc
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_digest(self, raw: str) -> tuple[str, str]:
        raw = (raw or "").strip()
        if not raw:
            return self.INVALID_RESPONSE, self.INVALID_RESPONSE

        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = self._extract_object_json(raw)

        summary = translation = ""
        if isinstance(parsed, dict):
            summary = parsed.get("summary") or ""
            translation = parsed.get("translation") or ""
            if not isinstance(summary, str):
                summary = ""
            if not isinstance(translation, str):
                translation = ""

        if not summary and not translation:
            logger.warning("Structured digest response parsing failed: {}", raw[:160])
            return self.INVALID_RESPONSE, self.INVALID_RESPONSE

        if summary and not looks_like_target_language(summary, self.target_language):
            summary = self.INVALID_RESPONSE
        if translation and not looks_like_target_language(
            translation, self.target_language
        ):
            translation = self.INVALID_RESPONSE
        summary = self._clamp_summary(summary)
        return summary, translation

    def _clamp_summary(self, summary: str) -> str:
        summary = (summary or "").strip()
        if len(summary) > self.SUMMARY_MAX_CHARS:
            summary = summary[: self.SUMMARY_MAX_CHARS].rstrip()
        return summary

    def _extract_object_json(self, raw: str):
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None

    def _first_chars(self, text: str, limit: int) -> str:
        text = (text or "").strip()
        if not text or text.startswith("[Translation"):
            return ""
        flattened = " ".join(
            line.strip() for line in text.splitlines() if line.strip()
        ).strip()
        if not flattened or not looks_like_target_language(
            flattened, self.target_language
        ):
            return ""
        return self._clamp_summary(
            flattened[:limit].rstrip() + ("..." if len(flattened) > limit else "")
        )

    def _response_format(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "post_digest",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "translation": {"type": "string"},
                    },
                    "required": ["summary", "translation"],
                    "additionalProperties": False,
                },
            },
        }

    # ------------------------------------------------------------------
    # Chunking + content-filter recovery
    # ------------------------------------------------------------------

    def _chunk_text(self, text: str) -> list[str]:
        if len(text) <= self.chunk_chars:
            return [text]

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        def flush_current():
            nonlocal current, current_len
            if current:
                chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0

        for paragraph in text.split("\n\n"):
            para = paragraph.strip()
            if not para:
                continue
            para_len = len(para)
            if para_len > self.chunk_chars:
                flush_current()
                chunks.extend(self._split_long_text(para))
                continue

            if current_len == 0:
                current = [para]
                current_len = para_len
                continue

            projected_len = current_len + 2 + para_len  # account for double newline
            if projected_len <= self.chunk_chars:
                current.append(para)
                current_len = projected_len
            else:
                flush_current()
                current = [para]
                current_len = para_len

        flush_current()
        return chunks

    def _split_long_text(self, text: str) -> list[str]:
        pieces: list[str] = []
        start = 0
        length = len(text)
        while start < length:
            end = min(start + self.chunk_chars, length)
            if end < length:
                # Backtrack to the nearest space to avoid breaking tokens.
                space = text.rfind(" ", start, end)
                if space > start + 20:
                    end = space
            pieces.append(text[start:end].strip())
            start = end
        return [piece for piece in pieces if piece]

    def _handle_content_filter(
        self, chunk: str, depth: int, reason: str
    ) -> tuple[str, str]:
        can_retry = depth < self._max_filter_depth and len(chunk) > 200
        parts: list[str] = []
        if can_retry:
            parts = self._split_for_filter(chunk)
            can_retry = len(parts) > 1

        log_fn = logger.info if can_retry else logger.warning
        log_fn(
            "Content filter blocked digest (depth={}, chars={}): {}",
            depth,
            len(chunk),
            reason[:180],
        )

        if not can_retry:
            return self.FILTERED, self.FILTERED

        summary = translation = ""
        for index, part in enumerate(parts):
            part_summary, part_translation = self._digest_chunk(part, _depth=depth + 1)
            if index == 0:
                summary = part_summary
            translation = (
                f"{translation}\n\n{part_translation}".strip()
                if translation or part_translation
                else part_translation
            )
        return summary or self.FILTERED, translation or self.FILTERED

    def _split_for_filter(self, text: str) -> list[str]:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        if len(paragraphs) > 1:
            mid = len(paragraphs) // 2
            first = "\n\n".join(paragraphs[:mid]).strip()
            second = "\n\n".join(paragraphs[mid:]).strip()
            return [part for part in (first, second) if part]

        midpoint = max(len(text) // 2, 1)
        split_at = text.rfind(" ", 0, midpoint)
        if split_at <= 0:
            split_at = midpoint
        first = text[:split_at].strip()
        second = text[split_at:].strip()
        return [part for part in (first, second) if part]
