"""Подготовка вложений к отправке в модель.

Устроено как в MailSense (packages/core/src/attachments/index.ts):
  * изображение — уходит картинкой (data-URL);
  * PDF — читается текстовый слой, а страницы рендерятся для OCR;
  * DOCX — текст абзацев и таблиц плюс вложенные картинки;
  * XLSX — листы в текст;
  * текст/HTML — как есть;
  * всё остальное — пометка «неподдерживаемый тип», которая честно попадает
    в промпт: модель должна знать, что часть письма она не видела.

Каждая пометка об обрезке (лимит страниц, лимит символов) идёт в промпт.
Молча терять страницы нельзя: по письму принимают решения и пишут в журнал.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..mail.parser import classify_kind, parse_eml, strip_html
from ..models import Attachment, PreparedAttachment
from .docx import docx_parts
from .images import guess_mime, image_to_data_url
from .pdf import pdf_pages_to_images, pdf_text
from .xlsx import xlsx_text

__all__ = [
    "PrepareLimits",
    "prepare_all",
    "prepare_attachment",
    "decode_text",
    "classify_kind",
]


@dataclass(frozen=True)
class PrepareLimits:
    """Ограничения, пришедшие из настроек."""

    ocr_max_pages: int = 10
    text_max_pages: int = 20
    max_text_chars: int = 60000
    multimodal: bool = True


def prepare_all(
    attachments: list[Attachment],
    limits: PrepareLimits,
    *,
    skip_inline_images: bool = True,
    min_inline_image_bytes: int = 30000,
) -> list[PreparedAttachment]:
    """Готовит все вложения письма.

    Мелкие картинки «в подписи» (логотип, линия) пропускаются: они не документ,
    а OCR каждой из них стоит секунды и портит контекст модели.
    """
    prepared: list[PreparedAttachment] = []
    for attachment in attachments:
        if (
            skip_inline_images
            and attachment.inline
            and attachment.mime_type.lower().startswith("image/")
            and attachment.size < min_inline_image_bytes
        ):
            continue
        prepared.append(prepare_attachment(attachment, limits))
    return prepared


def prepare_attachment(
    attachment: Attachment, limits: PrepareLimits, *, depth: int = 0
) -> PreparedAttachment:
    """Готовит одно вложение. Ошибка обработки не прерывает письмо целиком.

    depth — глубина вложенности для пересылаемых писем: письмо внутри письма
    разбираем, но не глубже MAX_EMAIL_DEPTH, чтобы не зациклиться на «письмо
    в письме в письме».
    """
    kind = classify_kind(attachment.mime_type, attachment.filename)
    prepared = PreparedAttachment(
        filename=attachment.filename,
        mime_type=attachment.mime_type,
        kind=kind,
    )
    try:
        if kind == "image":
            _prepare_image(attachment, limits, prepared)
        elif kind == "pdf":
            _prepare_pdf(attachment, limits, prepared)
        elif kind == "docx":
            _prepare_docx(attachment, limits, prepared)
        elif kind == "xlsx":
            _prepare_xlsx(attachment, limits, prepared)
        elif kind == "text":
            text = decode_text(attachment.content)
            prepared.extracted_text = _trim(text, limits.max_text_chars, prepared)
        elif kind == "html":
            text = strip_html(decode_text(attachment.content))
            prepared.extracted_text = _trim(text, limits.max_text_chars, prepared)
        elif kind == "email":
            _prepare_email(attachment, limits, prepared, depth)
        else:
            prepared.handled = False
            prepared.note = _unsupported_note(attachment)
    except Exception as exc:  # noqa: BLE001 - одно вложение не должно ронять письмо
        prepared.handled = False
        prepared.note = "ошибка обработки: " + str(exc)
    return prepared


# ----------------------------------------------------------------- подготовка


#: Насколько глубоко разбирать «письмо в письме».
MAX_EMAIL_DEPTH = 2


def _prepare_email(
    attachment: Attachment,
    limits: PrepareLimits,
    prepared: PreparedAttachment,
    depth: int,
) -> None:
    """Разбирает пересланное письмо, приложенное файлом .eml.

    Ящик, куда пересылают отобранную корреспонденцию, часто приносит исходное
    письмо вложением. Такому вложению нужны шапка (кто и когда), текст и его
    собственные вложения — иначе модель видит только «Re: …» без содержания,
    а сканы пересланного документа вообще теряются.
    """
    try:
        message = parse_eml(attachment.content, folder="вложенное письмо")
    except Exception as exc:  # noqa: BLE001
        prepared.handled = False
        prepared.note = "не удалось разобрать вложенное письмо: " + str(exc)
        return

    header = [
        "=== Пересланное письмо: " + attachment.filename + " ===",
        "От: " + (message.from_display or "неизвестен"),
        "Дата: " + (message.date_text or "не указана"),
        "Тема: " + message.subject,
        "",
        message.body_text,
    ]
    text = "\n".join(part for part in header if part).strip()

    images: list[str] = []
    notes: list[str] = []
    pages = 0

    if depth < MAX_EMAIL_DEPTH:
        for nested in message.attachments:
            child = prepare_attachment(nested, limits, depth=depth + 1)
            if child.extracted_text:
                text += (
                    "\n\n=== Вложение пересланного письма: "
                    + child.filename
                    + " ===\n"
                    + child.extracted_text
                )
            images.extend(child.images)
            pages += int(child.pages or 0)
            if child.note:
                notes.append(child.filename + ": " + child.note)
    else:
        notes.append("вложенные вложения глубже " + str(MAX_EMAIL_DEPTH) + " уровней не разбирались")

    prepared.extracted_text = _trim(text, limits.max_text_chars, prepared)
    prepared.images = images
    prepared.pages = pages or None
    prepared.handled = bool(prepared.extracted_text or prepared.images)
    if notes:
        prepared.note = (
            prepared.note + "; " + "; ".join(notes) if prepared.note else "; ".join(notes)
        )


def _prepare_image(attachment: Attachment, limits: PrepareLimits, prepared: PreparedAttachment) -> None:
    mime = guess_mime(attachment.filename, attachment.mime_type)
    data_url = (
        image_to_data_url(attachment.content, mime) if limits.multimodal else None
    )
    if data_url:
        prepared.images.append(data_url)
        prepared.pages = 1
        return
    prepared.handled = False
    if not limits.multimodal:
        prepared.note = "изображение не передано: в настройках выключен режим multimodal"
    else:
        prepared.note = "тип изображения не поддержан моделью (" + mime + ")"


def _prepare_pdf(attachment: Attachment, limits: PrepareLimits, prepared: PreparedAttachment) -> None:
    notes: list[str] = []

    text = pdf_text(attachment.content, limits.text_max_pages)
    prepared.pages = text.pages_total or None
    prepared.extracted_text = _trim(text.text, limits.max_text_chars, prepared)
    if text.truncated:
        notes.append(
            "текстовый слой: прочитаны первые "
            + str(text.pages_read)
            + " страниц из "
            + str(text.pages_total)
        )

    if limits.multimodal:
        images = pdf_pages_to_images(attachment.content, limits.ocr_max_pages)
        for image in images:
            data_url = image_to_data_url(image, "image/png")
            if data_url:
                prepared.images.append(data_url)
        rendered = len(prepared.images)
        if text.pages_total and rendered and rendered < text.pages_total:
            notes.append(
                "распознаны первые " + str(rendered) + " страниц из " + str(text.pages_total)
            )
        # «Обработано» — по страницам, а не по наличию текста: скан без текстового
        # слоя прочитает OCR, и считать его необработанным неправильно.
        prepared.handled = bool(text.pages_total) or bool(rendered)
    else:
        prepared.handled = bool(text.pages_total)
        if text.pages_total and not text.text:
            notes.append("текстовый слой пуст, а распознавание изображений выключено")

    if notes:
        prepared.note = "; ".join(notes)


def _prepare_docx(attachment: Attachment, limits: PrepareLimits, prepared: PreparedAttachment) -> None:
    parts = docx_parts(attachment.content, with_images=limits.multimodal)
    prepared.extracted_text = _trim(parts.text, limits.max_text_chars, prepared)
    for image in parts.images:
        data_url = image_to_data_url(image, "image/png")
        if data_url:
            prepared.images.append(data_url)
    if parts.note:
        prepared.note = parts.note
    prepared.handled = bool(parts.text or prepared.images) or not parts.note


def _prepare_xlsx(attachment: Attachment, limits: PrepareLimits, prepared: PreparedAttachment) -> None:
    text = xlsx_text(attachment.content, max_chars=limits.max_text_chars)
    prepared.extracted_text = _trim(text, limits.max_text_chars, prepared)
    prepared.handled = bool(text)
    if not text:
        prepared.note = "таблица пуста или не читается (нужен пакет openpyxl)"


# ---------------------------------------------------------------- вспомогательное


def _trim(text: str, limit: int, prepared: PreparedAttachment) -> str | None:
    """Обрезает текст по лимиту и честно помечает обрезку."""
    value = (text or "").strip()
    if not value:
        return None
    if limit > 0 and len(value) > limit:
        note = "текст обрезан до " + str(limit) + " символов из " + str(len(value))
        prepared.note = (prepared.note + "; " + note) if prepared.note else note
        value = value[:limit].rstrip()
    return value


def _unsupported_note(attachment: Attachment) -> str:
    name = (attachment.filename or "").lower()
    if name.endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
        return "архив не распаковывается — содержимое не проанализировано"
    return "неподдерживаемый тип вложения (" + (attachment.mime_type or "unknown") + ")"


def decode_text(content: bytes) -> str:
    """Декодирует текстовое вложение.

    Порядок: UTF-8 (самый частый), затем CP1251 (документы из 1С и Word
    на русской Windows), затем Latin-1 без потерь.
    """
    for charset in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return content.decode(charset)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace")
