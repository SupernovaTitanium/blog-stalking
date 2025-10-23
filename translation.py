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
    ):
        self.client = AzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=endpoint,
        )
        self.deployment = deployment
        self.target_language = target_language
        self.max_chars = max_chars

    def translate_batch(self, texts: Sequence[str]) -> List[str]:
        translations: List[str] = []
        for text in texts:
            if not text:
                translations.append("")
                continue

            prompt = (
                f"Translate the following Mastodon status into {self.target_language}. "
                "Preserve math symbols, URLs, and fenced code exactly. "
                "Return only the translation; keep paragraph breaks."
            )
            try:
                response = self.client.chat.completions.create(
                    model=self.deployment,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text[: self.max_chars]},
                    ],
                    temperature=0.2,
                )
                translated = response.choices[0].message.content.strip()
            except Exception as exc:
                logger.exception("Translation failed")
                translated = f"[Translation error: {exc}]"

            translations.append(translated)
        return translations
