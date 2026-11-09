"""Архив обработанных писем.

Раскладка: по папке на каждую запись журнала, имя — номер записи из базы
(CARD.ID). В папке лежит исходное письмо и каждое вложение отдельным файлом
в исходном формате:

    data/137/
        letter.eml
        Счёт 45.pdf
        акт.docx

Зачем так: по записи журнала всегда видно, из какого письма она появилась,
а вложения можно открыть и проверить, не разбирая .eml заново.

Имена файлов очищаются от символов, недопустимых в путях (в том числе в Windows),
и от попыток выйти из папки через «..». Совпадающие имена получают суффикс -2, -3.
Ошибка записи архива не отменяет регистрацию: запись в журнале уже сделана.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from pathlib import Path

from .models import CardData, InsertedCard, MailMessage

logger = logging.getLogger(__name__)

#: Имя файла с исходным письмом внутри папки записи.
LETTER_FILENAME = "letter.eml"

#: Ограничение длины имени файла: 255 байт — предел большинства файловых систем,
#: а кириллица в UTF-8 занимает два байта на символ. Берём с запасом.
MAX_NAME_LENGTH = 100

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM" + str(number) for number in range(1, 10)]
    + ["LPT" + str(number) for number in range(1, 10)]
)

#: Расширения для типов, у которых в письме не оказалось имени файла.
_EXTENSIONS = {
    "application/pdf": ".pdf",
    "message/rfc822": ".eml",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "text/plain": ".txt",
    "text/html": ".html",
}


def folder_name(
    inserted: InsertedCard | None, card: CardData | None, mail: MailMessage
) -> str:
    """Имя папки записи: номер из базы, иначе регистрационный номер или письмо.

    Основной источник — идентификатор записи CARD.ID: после регистрации он есть
    всегда и не меняется. Регистрационный номер (INTINDEX) до присвоения номера
    системой учёта равен «новая» и для имени папки не годится.
    """
    if inserted is not None and inserted.card_id not in (None, ""):
        return _safe_component(str(inserted.card_id))
    if card is not None and card.intindex and card.intindex.strip().lower() != "новая":
        return _safe_component(card.intindex)
    return _safe_component(mail.message_id or mail.uid or "mail")


def save_letter(mail: MailMessage, base_dir: Path, folder: str) -> Path:
    """Сохраняет письмо и вложения в base_dir/folder и возвращает папку.

    Существующие файлы не затираются: совпадение имени даёт суффикс -2, -3 и т. д.
    """
    target = base_dir / folder
    target.mkdir(parents=True, exist_ok=True)

    letter_path = _unique_path(target, LETTER_FILENAME)
    letter_path.write_bytes(mail.raw)

    used = {letter_path.name.lower()}
    for index, attachment in enumerate(mail.attachments, start=1):
        name = safe_filename(attachment.filename, attachment.mime_type, index)
        path = _unique_path(target, name, used)
        path.write_bytes(attachment.content)
        used.add(path.name.lower())

    return target


def safe_filename(filename: str | None, mime_type: str, index: int) -> str:
    """Имя файла вложения: исходное, но безопасное для файловой системы."""
    name = (filename or "").replace("\\", "/").split("/")[-1]
    name = _INVALID_CHARS.sub("_", name).strip(" .")

    if not name:
        name = "attachment-" + str(index) + _extension_for(mime_type, filename)

    stem, extension = os.path.splitext(name)
    if not extension:
        extension = _extension_for(mime_type, filename)
        name = stem + extension
        stem = stem or "attachment-" + str(index)

    if stem.upper() in _RESERVED_NAMES:
        stem = "_" + stem
        name = stem + extension

    if len(name) > MAX_NAME_LENGTH:
        name = stem[: MAX_NAME_LENGTH - len(extension)] + extension

    return name


def _extension_for(mime_type: str, filename: str | None) -> str:
    """Расширение по MIME, если в имени файла его нет."""
    mime = (mime_type or "").lower()
    if mime in _EXTENSIONS:
        return _EXTENSIONS[mime]
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return guessed
    _, original = os.path.splitext(filename or "")
    return original if original else ".bin"


def _safe_component(value: str) -> str:
    """Имя папки без разделителей пути и запрещённых символов."""
    cleaned = _INVALID_CHARS.sub("_", value or "").strip(" .")
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    return cleaned[:MAX_NAME_LENGTH] or "mail"


def _unique_path(folder: Path, name: str, used: set[str] | None = None) -> Path:
    """Свободный путь: при совпадении добавляет -2, -3 и т. д."""
    taken = used or set()
    candidate = folder / name
    if not candidate.exists() and name.lower() not in taken:
        return candidate

    stem, extension = os.path.splitext(name)
    counter = 2
    while True:
        candidate = folder / (stem + "-" + str(counter) + extension)
        if not candidate.exists() and candidate.name.lower() not in taken:
            return candidate
        counter += 1
