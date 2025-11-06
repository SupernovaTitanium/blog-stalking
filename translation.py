from __future__ import annotations

from typing import List, Sequence

from loguru import logger
from openai import AzureOpenAI


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

    def _translate_chunk(self, chunk: str) -> str:
        prompt = (
            f"Translate the following content into {self.target_language}. "
            "Do not drop any sentences. Preserve LaTeX/math symbols, URLs, Markdown, and fenced code exactly as-is."
            " Return only the translated text while keeping paragraph structure."
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
            return response.choices[0].message.content.strip()
        except Exception as exc:
            logger.exception("Translation chunk failed")
            return f"[Translation error: {exc}]"

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
