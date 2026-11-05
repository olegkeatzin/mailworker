"""OCR: чтение сканов и картинок отдельной моделью (vision).

Промпт — дословный порт OCR_PROMPT из MailSense
(packages/core/src/ai/ocr.ts): модель должна выдать содержимое документа, а не
пересказ, иначе номер и дата из шапки теряются.

OCR — отдельный этап, потому что модель распознавания часто другая и живёт на
другом сервере (настройки [ocr]); если она не задана, используется основная [ai].
"""

from __future__ import annotations

from ..config import OcrConfig
from .client import AiClient

OCR_PROMPT = "\n".join(
    [
        "Прочитай документ на изображении.",
        "Выведи ТОЛЬКО содержимое документа в формате Markdown, точно сохраняя:",
        "- шапку/бланк отправителя: организация, дата отправки, исходящий/внешний номер;",
        "- таблицы, списки, реквизиты, суммы, даты;",
        "- порядок чтения сверху вниз.",
        "Ничего не пересказывай, не добавляй пояснений — только текст документа.",
    ]
)


def ocr_image(client: AiClient, cfg: OcrConfig, image_data_url: str) -> str:
    """Распознаёт одну страницу/изображение и возвращает текст (Markdown)."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        }
    ]
    return client.complete(
        messages,
        model=cfg.model,
        base_url=cfg.base_url,
        timeout=cfg.timeout,
        temperature=0.0,
        max_tokens=4096,
        # Ответ OCR — обычный текст, а не JSON: response_format здесь только мешает.
        json_mode=False,
    )
