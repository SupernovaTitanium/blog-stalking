from __future__ import annotations

import json
import re
import time
from typing import Any, List, Sequence

from loguru import logger
from openai import OpenAI, BadRequestError


class ContentFilterTriggeredError(Exception):
    """Raised when the model returns a content-filtered response."""


class RateLimitExhaustedError(Exception):
    """Raised after provider rate-limit retries have been exhausted."""


def looks_like_target_language(text: str | None, target_language: str | None) -> bool:
    """Shared sanity check that LLM output actually matches the target language.

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


class _MinuteRateLimiter:
    def __init__(self, rpm: int | None):
        self.rpm = max(1, int(rpm or 10))
        self._timestamps: list[float] = []

    def wait(self) -> None:
        now = time.monotonic()
        window_start = now - 60
        self._timestamps = [stamp for stamp in self._timestamps if stamp > window_start]
        if len(self._timestamps) >= self.rpm:
            sleep_for = 60 - (now - self._timestamps[0])
            if sleep_for > 0:
                logger.info("NVIDIA rate limit reached; sleeping {:.1f}s.", sleep_for)
                time.sleep(sleep_for)
            now = time.monotonic()
            window_start = now - 60
            self._timestamps = [stamp for stamp in self._timestamps if stamp > window_start]
        self._timestamps.append(time.monotonic())


class OpenAITranslator:
    INVALID_STRUCTURED_RESPONSE = "[Translation error: invalid structured response]"
    INVALID_TRANSLATION_RESPONSE = "[Translation error: invalid structured translation]"
    NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
    NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        model: str,
        target_language: str,
        max_chars: int = 4000,
        max_total_chars: int = 12000,
        chunk_chars: int = 0,
        openai_max_tokens: int | None = None,
        temperature: float | None = None,
        provider: str = "openai",
        nvidia_api_url: str | None = None,
        nvidia_base_url: str | None = None,
        nvidia_rpm: int = 4,
        nvidia_max_tokens: int = 16384,
        nvidia_top_p: float = 0.95,
        rate_limit_retries: int = 4,
        rate_limit_base_sleep: float = 65,
        transient_retries: int = 2,
        transient_base_sleep: float = 5.0,
    ):
        self.provider = (provider or "openai").lower()
        if self.provider not in ("openai", "nvidia"):
            raise ValueError(f"Unsupported AI provider: {provider}")
        base_url = (base_url or "").strip() or None
        api_key = (api_key or "").strip()
        if self.provider == "openai":
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            self.client = OpenAI(
                api_key=api_key,
                base_url=self._coerce_nvidia_base_url(
                    nvidia_base_url=nvidia_base_url,
                    nvidia_api_url=nvidia_api_url,
                ),
            )
        self.api_key = api_key
        self.nvidia_max_tokens = int(nvidia_max_tokens)
        self.nvidia_top_p = float(nvidia_top_p)
        self._nvidia_limiter = _MinuteRateLimiter(nvidia_rpm)
        self.rate_limit_retries = max(0, int(rate_limit_retries))
        self.rate_limit_base_sleep = max(1.0, float(rate_limit_base_sleep))
        self.transient_retries = max(0, int(transient_retries))
        self.transient_base_sleep = max(0.5, float(transient_base_sleep))
        self.model = model
        self.target_language = target_language
        self.max_chars = max_chars
        self.max_total_chars = max_total_chars
        # Translation chunking override: 0 = follow max_chars (legacy),
        # >0 = chunk at this size, <0 = never chunk (one request per article).
        self.chunk_chars = int(chunk_chars)
        # Output cap for the OpenAI path. Aggregators like OpenRouter price
        # requests against the model's maximum when max_tokens is unset and
        # reject (402) when that exceeds the key's credit budget.
        self.openai_max_tokens = (
            int(openai_max_tokens) if openai_max_tokens else None
        )
        self.temperature = temperature
        self._max_filter_depth = 3
        self._rate_limit_exhausted = False

    @property
    def _chunk_limit(self) -> int:
        return self.chunk_chars if self.chunk_chars > 0 else self.max_chars

    def _coerce_nvidia_base_url(
        self,
        *,
        nvidia_base_url: str | None,
        nvidia_api_url: str | None,
    ) -> str:
        if nvidia_base_url:
            return nvidia_base_url.rstrip("/")
        if nvidia_api_url:
            normalized = nvidia_api_url.rstrip("/")
            suffix = "/chat/completions"
            if normalized.endswith(suffix):
                return normalized[: -len(suffix)]
            return normalized
        return self.NVIDIA_BASE_URL

    # ------------------------------------------------------------------
    # Provider calls: retry policy + response-format fallback
    # ------------------------------------------------------------------

    def _chat_completion_content(
        self,
        *,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str, str | None]:
        if self._rate_limit_exhausted:
            raise RateLimitExhaustedError(
                "Provider rate limit was already exhausted; skipping API request."
            )

        if self.provider == "nvidia":
            return self._with_provider_retries(
                lambda: self._nvidia_streaming_chat_completion(
                    messages=messages, response_format=response_format
                )
            )

        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if response_format is not None:
            kwargs["response_format"] = response_format
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.openai_max_tokens:
            kwargs["max_tokens"] = self.openai_max_tokens

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
                        self._rate_limit_exhausted = True
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

    def _nvidia_streaming_chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str, str | None]:
        self._nvidia_limiter.wait()
        parts: list[str] = []
        finish_reason: str | None = None
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 1 if self.temperature is None else self.temperature,
            "max_tokens": self.nvidia_max_tokens,
            "top_p": self.nvidia_top_p,
            "stream": True,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        completion = self.client.chat.completions.create(**kwargs)
        for chunk in completion:
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            choice = choices[0]
            finish_reason = getattr(choice, "finish_reason", None) or finish_reason
            delta = getattr(choice, "delta", None)
            text = getattr(delta, "content", None) if delta is not None else None
            if text:
                parts.append(text)
        return "".join(parts).strip(), finish_reason

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
    # Full-text translation (per chunk)
    # ------------------------------------------------------------------

    def translate_batch(self, texts: Sequence[str]) -> List[str]:
        translations: List[str] = []
        for text in texts:
            if not text:
                translations.append("")
                continue

            if self.chunk_chars < 0:
                chunks: List[str] = [text]
            else:
                chunks = self._chunk_text(text)
            logger.debug(
                f"Translating text with {len(chunks)} chunk(s) (total chars: {len(text)})"
            )
            translated_chunks: List[str] = []
            for chunk in chunks:
                translated_chunks.append(self._translate_chunk(chunk))

            translation = "\n\n".join(part for part in translated_chunks if part).strip()
            translations.append(translation)
        return translations

    def _translate_chunk(self, chunk: str, *, _depth: int = 0) -> str:
        prompt = (
            f"你是專業技術翻譯員。請將使用者提供的內容完整翻譯成 {self.target_language}。"
            "這是全文翻譯，不是摘要，不可省略段落或關鍵資訊。"
            "請保留原始段落結構、數學符號、LaTeX、URL、Markdown 與程式碼區塊。"
            "必須輸出 JSON 物件，格式為 {\"translation\": \"...\"}。"
        )
        try:
            content, finish_reason = self._chat_with_format_fallback(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": chunk},
                ],
                formats=(
                    self._translation_response_format(),
                    {"type": "json_object"},
                    None,
                ),
            )
            if finish_reason == "content_filter" or not content:
                raise ContentFilterTriggeredError(
                    f"Model returned finish_reason={finish_reason!r}"
                )
            translated = self._parse_translation_text(content)
            if translated == self.INVALID_TRANSLATION_RESPONSE:
                logger.warning(
                    "Structured translation response parsing failed for chunk: {}",
                    content[:160],
                )
            return translated
        except ContentFilterTriggeredError as exc:
            return self._handle_content_filter(chunk, _depth, str(exc))
        except RateLimitExhaustedError as exc:
            logger.warning(
                "Translation chunk skipped because provider rate limit was exhausted: {}",
                self._summarize_rate_limit_reason(str(exc)),
            )
            return "[Translation skipped: rate limited]"
        except Exception as exc:
            logger.exception("Translation chunk failed")
            return f"[Translation error: {exc}]"

    # ------------------------------------------------------------------
    # Per-feed summaries
    # ------------------------------------------------------------------

    def translate_batch_by_feed(
        self,
        texts: Sequence[str],
        feed_keys: Sequence[str],
    ) -> List[str]:
        if len(texts) != len(feed_keys):
            raise ValueError("texts and feed_keys must have the same length")
        if not texts:
            return []

        grouped: dict[str, list[tuple[int, str]]] = {}
        for idx, (feed_key, text) in enumerate(zip(feed_keys, texts, strict=False)):
            key = feed_key or "__unknown_feed__"
            grouped.setdefault(key, []).append((idx, text))

        results: List[str] = [""] * len(texts)
        for feed_key, items in grouped.items():
            indices = [idx for idx, _ in items]
            feed_texts = [text for _, text in items]
            summaries = self._translate_feed_once(feed_key, feed_texts)

            if len(summaries) != len(feed_texts):
                logger.warning(
                    "Feed {} returned {} summaries for {} posts; padding/truncating.",
                    feed_key,
                    len(summaries),
                    len(feed_texts),
                )
                if len(summaries) < len(feed_texts):
                    summaries.extend(
                        ["[Translation error: missing summary]"]
                        * (len(feed_texts) - len(summaries))
                    )
                else:
                    summaries = summaries[: len(feed_texts)]

            for idx, summary in zip(indices, summaries, strict=False):
                results[idx] = summary
        return results

    def _translate_feed_once(self, feed_key: str, texts: Sequence[str]) -> List[str]:
        prepared = self._prepare_texts_for_feed(texts)
        if not prepared:
            return []

        prompt = (
            f"請將下列技術文章摘要成不超過 200 個{self.target_language}字詞，保留核心概念、關鍵步驟與主要結論，"
            "避免加入主觀評論，只呈現最重要的資訊。保持原有的數學符號、LaTeX、URL、Markdown 與程式碼區塊不變。"
            "必須輸出 JSON 物件，格式為 {\"summaries\": [\"...\", \"...\"]}，順序需與輸入文章一致。"
        )
        user_parts = [f"【Feed】{feed_key}", "【輸入】"]
        for idx, text in enumerate(prepared, 1):
            user_parts.append(f"【文章 {idx}】\n{text}")
        user_content = "\n\n".join(user_parts)

        try:
            content, finish_reason = self._chat_with_format_fallback(
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content},
                ],
                formats=(
                    self._feed_response_format(),
                    {"type": "json_object"},
                    None,
                ),
            )
            if finish_reason == "content_filter" or not content:
                raise ContentFilterTriggeredError(
                    f"Model returned finish_reason={finish_reason!r}"
                )
            summaries = self._parse_feed_summaries(content, len(prepared))
            return [self._normalize_summary(s) for s in summaries]
        except ContentFilterTriggeredError as exc:
            logger.warning(
                "Content filter blocked feed summary for {}: {}",
                feed_key,
                self._summarize_filter_reason(str(exc)),
            )
            return ["[Translation skipped: blocked by content filter]"] * len(prepared)
        except RateLimitExhaustedError as exc:
            logger.warning(
                "Feed summary skipped for {} because provider rate limit was exhausted: {}",
                feed_key,
                self._summarize_rate_limit_reason(str(exc)),
            )
            return ["[Translation skipped: rate limited]"] * len(prepared)
        except Exception as exc:
            logger.exception("Feed summary failed for {}", feed_key)
            return [f"[Translation error: {exc}]"] * len(prepared)

    def _prepare_texts_for_feed(self, texts: Sequence[str]) -> List[str]:
        if not texts:
            return []
        count = len(texts)
        per_post_cap = min(self.max_chars, max(120, self.max_total_chars // max(1, count)))
        prepared: List[str] = []
        for text in texts:
            cleaned = (text or "").strip()
            if not cleaned:
                cleaned = "（無可用內容）"
            prepared.append(self._truncate_text(cleaned, per_post_cap))
        return prepared

    def _truncate_text(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        head = text[:limit]
        cut = max(head.rfind(" "), head.rfind("\n"))
        if cut >= max(0, limit - 120):
            head = head[:cut]
        return head.strip()

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_feed_summaries(self, raw: str, expected: int) -> List[str]:
        raw = (raw or "").strip()
        if not raw:
            return [self.INVALID_STRUCTURED_RESPONSE] * expected

        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None

        if parsed is None:
            parsed = self._extract_object_json(raw) or self._extract_bracket_json(raw)

        summaries: List[str] = []
        if isinstance(parsed, (dict, list)):
            items = parsed.get("summaries", []) if isinstance(parsed, dict) else parsed
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, str):
                        continue
                    summary = item.strip()
                    if not summary:
                        continue
                    if not looks_like_target_language(summary, self.target_language):
                        summaries.append(self.INVALID_STRUCTURED_RESPONSE)
                        continue
                    summaries.append(summary)

        if not summaries:
            logger.warning("Structured translation response parsing failed: {}", raw[:160])
            return [self.INVALID_STRUCTURED_RESPONSE] * expected

        if len(summaries) < expected:
            summaries.extend(
                [self.INVALID_STRUCTURED_RESPONSE] * (expected - len(summaries))
            )
        return summaries[:expected]

    def _feed_response_format(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "feed_summaries",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "summaries": {
                            "type": "array",
                            "items": {"type": "string"},
                        }
                    },
                    "required": ["summaries"],
                    "additionalProperties": False,
                },
            },
        }

    def _translation_response_format(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "post_translation",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "translation": {"type": "string"},
                    },
                    "required": ["translation"],
                    "additionalProperties": False,
                },
            },
        }

    def _parse_translation_text(self, raw: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            return self.INVALID_TRANSLATION_RESPONSE

        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None

        if parsed is None:
            parsed = self._extract_object_json(raw)

        if isinstance(parsed, dict):
            translation = parsed.get("translation")
            if isinstance(translation, str) and translation.strip():
                value = translation.strip()
                if not looks_like_target_language(value, self.target_language):
                    return self.INVALID_TRANSLATION_RESPONSE
                return value

        return self.INVALID_TRANSLATION_RESPONSE

    def _extract_object_json(self, raw: str):
        match = re.search(r'\{.*\}', raw, flags=re.DOTALL)
        if not match:
            return None
        snippet = match.group(0)
        try:
            return json.loads(snippet)
        except Exception:
            return None

    def _extract_bracket_json(self, raw: str):
        match = re.search(r'\[.*\]', raw, flags=re.DOTALL)
        if not match:
            return None
        snippet = match.group(0)
        try:
            return json.loads(snippet)
        except Exception:
            return None

    def _normalize_summary(self, text: str) -> str:
        summary = (text or "").strip()
        if summary.startswith(("「", "“", "\"")) and summary.endswith(("」", "”", "\"")):
            summary = summary[1:-1].strip()
        if len(summary) > 200:
            summary = summary[:200].rstrip()
        return summary

    # ------------------------------------------------------------------
    # Content-filter recovery
    # ------------------------------------------------------------------

    def _handle_content_filter(self, chunk: str, depth: int, reason: str) -> str:
        can_retry = depth < self._max_filter_depth and len(chunk) > 200
        parts: List[str] = []
        if can_retry:
            parts = self._split_for_filter(chunk)
            can_retry = len(parts) > 1

        log_fn = logger.info if can_retry else logger.warning
        log_fn(
            "Content filter blocked translation (depth={}, chars={}): {}",
            depth,
            len(chunk),
            self._summarize_filter_reason(reason),
        )

        if not can_retry:
            return "[Translation skipped: blocked by content filter]"

        translations = [
            self._translate_chunk(part, _depth=depth + 1) for part in parts if part
        ]
        combined = "\n\n".join(part for part in translations if part).strip()
        return combined or "[Translation skipped: blocked by content filter]"

    def _chunk_text(self, text: str) -> List[str]:
        if len(text) <= self._chunk_limit:
            return [text]

        chunks: List[str] = []
        current: List[str] = []
        current_len = 0

        def flush_current():
            nonlocal current, current_len
            if current:
                chunks.append("\n\n".join(current).strip())
            current = []
            current_len = 0

        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        for paragraph in paragraphs:
            para = paragraph.strip()
            para_len = len(para)
            if para_len > self._chunk_limit:
                flush_current()
                chunks.extend(self._split_long_text(para))
                continue

            if current_len == 0:
                current = [para]
                current_len = para_len
                continue

            projected_len = current_len + 2 + para_len  # account for double newline
            if projected_len <= self._chunk_limit:
                current.append(para)
                current_len = projected_len
            else:
                flush_current()
                current = [para]
                current_len = para_len

        flush_current()
        return chunks

    def _split_for_filter(self, text: str) -> List[str]:
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

    def _split_long_text(self, text: str) -> List[str]:
        pieces: List[str] = []
        start = 0
        length = len(text)
        while start < length:
            end = min(start + self._chunk_limit, length)
            # try to backtrack to nearest space to avoid breaking tokens
            if end < length:
                space = text.rfind(" ", start, end)
                if space > start + 20:
                    end = space
            pieces.append(text[start:end].strip())
            start = end
        return [piece for piece in pieces if piece]

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

    def _summarize_filter_reason(self, reason: str) -> str:
        if not reason:
            return "Content filter"
        if "content management policy" in reason or "ResponsibleAIPolicyViolation" in reason:
            return "Policy violation"
        if len(reason) > 180:
            return reason[:177] + "..."
        return reason

    def _summarize_rate_limit_reason(self, reason: str) -> str:
        if not reason:
            return "Rate limit exhausted"
        if len(reason) > 180:
            return reason[:177] + "..."
        return reason
