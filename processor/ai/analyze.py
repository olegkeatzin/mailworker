"""Запрос анализа письма: промпт + текст вложений + изображения.

Порт analyzeEmailRaw из MailSense: одно сообщение пользователя, где первая часть —
текст промпта, дальше по частям — изображения (data-URL). Так модель видит и текст
письма, и сканы вложений в одном запросе.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ..config import AiConfig
from .client import AiClient
from .prompt import PromptInput, PromptSettings, build_prompt


@dataclass
class AnalyzeInput:
    """Всё, что известно о письме к моменту запроса к модели."""

    subject: str = ""
    from_display: str = ""
    date: str = ""
    body: str = ""
    attachment_texts: list[str] = field(default_factory=list)
    attachment_names: list[str] = field(default_factory=list)
    attachment_notes: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    forwarded: bool = False
    #: Названия тем из справочника THEMA: модель выбирает одну из них.
    themes: list[str] = field(default_factory=list)
    #: Спрашивать решение о регистрации: нужно только при filter_mode = ai.
    need_decision: bool = True
    #: Спрашивать категорию: нужна только при skip_spam = true.
    need_category: bool = True


def analyze_email(
    client: AiClient,
    cfg: AiConfig,
    input_data: AnalyzeInput,
    settings: PromptSettings,
) -> str:
    """Отправляет письмо модели и возвращает сырой текст ответа (обычно JSON)."""
    body = _truncate_body(input_data.body, cfg.max_text_chars)

    prompt_input = PromptInput(
        subject=input_data.subject,
        from_display=input_data.from_display,
        date=input_data.date,
        body=body,
        attachment_texts=input_data.attachment_texts,
        attachment_names=input_data.attachment_names,
        attachment_notes=input_data.attachment_notes,
        image_count=len(input_data.images),
        forwarded=input_data.forwarded,
        today=dt.date.today().strftime("%Y-%m-%d"),
        themes=input_data.themes,
        need_decision=input_data.need_decision,
        need_category=input_data.need_category,
    )
    prompt = build_prompt(prompt_input, settings)

    content: list[dict] = [{"type": "text", "text": prompt}]
    for image in input_data.images:
        content.append({"type": "image_url", "image_url": {"url": image}})

    messages = [{"role": "user", "content": content}]
    return client.complete(messages)


def _truncate_body(body: str, limit: int) -> str:
    """Ограничивает тело письма: рассылки бывают на сотни тысяч символов.

    Обрезка помечается в тексте, чтобы модель не считала письмо законченным.
    """
    text = body or ""
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip() + "\n... (текст письма обрезан по размеру)"
    return text
