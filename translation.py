from __future__ import annotations

from typing import List, Sequence

from loguru import logger
from openai import AzureOpenAI, BadRequestError


class ContentFilterTriggeredError(Exception):
    """Raised when Azure returns a content-filtered response."""


class AzureTranslator:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        deployment: str,
        api_version: str,
        target_language: str,
        max_chars: int = 4000,
        temperature: float | None = None,
    ):
        self.client = AzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=endpoint,
        )
        self.deployment = deployment
        self.target_language = target_language
        self.max_chars = max_chars
        self.temperature = temperature
        self._max_filter_depth = 3

    def translate_batch(self, texts: Sequence[str]) -> List[str]:
        translations: List[str] = []
        for text in texts:
            if not text:
                translations.append("")
                continue

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
            "請將下列技術文章摘要成不超過 200 個中文字，保留核心概念、關鍵步驟與主要結論，"
            "避免加入主觀評論，只呈現最重要的資訊。保持原有的數學符號、LaTeX、URL、Markdown 與程式碼區塊不變。"
        )
        try:
            kwargs = {
                "model": self.deployment,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": chunk},
                ],
            }
            if self.temperature is not None:
                kwargs["temperature"] = self.temperature

            response = self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            content = (choice.message.content or "").strip()
            if choice.finish_reason == "content_filter" or not content:
                raise ContentFilterTriggeredError(
                    f"Azure returned finish_reason={choice.finish_reason!r}"
                )
            return content
        except ContentFilterTriggeredError as exc:
            return self._handle_content_filter(chunk, _depth, str(exc))
        except BadRequestError as exc:
            if self._is_content_filter_error(exc):
                return self._handle_content_filter(chunk, _depth, str(exc))
            logger.exception("Translation chunk failed")
            return f"[Translation error: {exc}]"
        except Exception as exc:
            logger.exception("Translation chunk failed")
            return f"[Translation error: {exc}]"

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
            return "[Translation skipped: blocked by Azure content filter]"

        translations = [
            self._translate_chunk(part, _depth=depth + 1) for part in parts if part
        ]
        combined = "\n\n".join(part for part in translations if part).strip()
        return combined or "[Translation skipped: blocked by Azure content filter]"

    def _chunk_text(self, text: str) -> List[str]:
        if len(text) <= self.max_chars:
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
            if para_len > self.max_chars:
                flush_current()
                chunks.extend(self._split_long_text(para))
                continue

            if current_len == 0:
                current = [para]
                current_len = para_len
                continue

            projected_len = current_len + 2 + para_len  # account for double newline
            if projected_len <= self.max_chars:
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
            end = min(start + self.max_chars, length)
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

    def _summarize_filter_reason(self, reason: str) -> str:
        if not reason:
            return "Azure content filter"
        if "content management policy" in reason or "ResponsibleAIPolicyViolation" in reason:
            return "Azure Responsible AI policy violation"
        if len(reason) > 180:
            return reason[:177] + "..."
        return reason
