"""Разбор письма в формате .eml стандартными средствами Python.

Логика перенесена из MailSense (packages/core/src/mail/parser.ts):
  * Message-ID нормализуется (снимаются угловые скобки);
  * тема по умолчанию «(без темы)»;
  * вложения — части с именем файла или disposition=attachment;
  * тип вложения классифицируется по MIME и расширению;
  * внешний номер («Исх. № 123/45») вытаскивается регулярными выражениями
    как страховка, если модель его не нашла.

Дополнительно (нужно автоматической службе, где человека нет):
  * если Message-ID отсутствует, строится устойчивый ключ дедупликации по хэшу
    письма — иначе при повторном опросе ящика письмо зарегистрировали бы дважды;
  * определяется пересылка: у пересланного письма From — пересылающий, а не
    корреспондент, и записывать его в графу «От кого получен» нельзя.
"""

from __future__ import annotations

import datetime as dt
import base64
import email
import email.header
import email.policy
import email.utils
import hashlib
import re
from email.message import Message

from ..models import Attachment, MailMessage

#: Тема письма, если её нет: пустая строка в журнале выглядит как ошибка ввода.
EMPTY_SUBJECT = "(без темы)"

#: Маркеры пересылки в теле письма (Thunderbird, Outlook, Mail.ru, Яндекс).
_FORWARD_MARKERS = (
    "---------- пересылаемое сообщение",
    "-------- пересылаемое сообщение",
    "-----original message-----",
    "----------original message----------",
    "forwarded message",
    "пересылаемое сообщение",
)

#: Префиксы темы пересылки.
_FORWARD_SUBJECTS = ("fwd:", "fw:", "fwd :", "пересл", "vs:", "wg:", "отв:")

_IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|webp|tiff?|bmp)$", re.IGNORECASE)
_PDF_EXT = re.compile(r"\.pdf$", re.IGNORECASE)
_DOCX_EXT = re.compile(r"\.(docx?|rtf|odt)$", re.IGNORECASE)
_XLSX_EXT = re.compile(r"\.(xlsx?|ods|csv)$", re.IGNORECASE)


# --------------------------------------------------------------- разбор письма


def parse_eml(raw: bytes, *, uid: str = "", folder: str = "") -> MailMessage:
    """Разбирает байты письма в MailMessage.

    Исключения наружу не выпускаются: письмо может быть кривым (битая кодировка,
    отсутствующие заголовки), но это не повод останавливать службу.
    """
    message = email.message_from_bytes(raw, policy=email.policy.default)
    # Заголовки читаем отдельно в «сыром» виде (compat32): политика default
    # склеивает соседние кодированные слова через пробел, и имя вида
    # «Корлякова ... Олеговн» + «а» превращается в «Олеговн а».
    # decode_header склеивает их правильно, без лишнего пробела (RFC 2047).
    headers = email.message_from_bytes(raw, policy=email.policy.compat32)

    subject = _header_text(headers.get("Subject")) or EMPTY_SUBJECT
    from_name, from_address = _parse_from(headers)
    to_display = _header_text(headers.get("To"))
    date = _parse_date(headers)
    message_id = normalize_message_id(_header_text(headers.get("Message-ID")))

    body_text, body_html, attachments = _walk_parts(message)

    if not body_text.strip() and body_html:
        body_text = strip_html(body_html)

    if not message_id:
        # Устойчивый к повторам ключ: одно и то же письмо, забранное дважды,
        # даёт одинаковый хэш, а разные письма — разный.
        digest = hashlib.sha256(raw).hexdigest()[:32]
        message_id = "sha256:" + digest

    return MailMessage(
        uid=str(uid or ""),
        folder=folder or "",
        message_id=message_id,
        subject=subject,
        from_name=from_name,
        from_address=from_address,
        to_display=to_display,
        date=date,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        raw=raw,
        forwarded=is_forwarded(subject, body_text),
    )


def _walk_parts(message: Message) -> tuple[str, str, list[Attachment]]:
    """Собирает текст письма и вложения из всех частей MIME.

    Обход ручной, а не message.walk(): пересланное письмо (message/rfc822) —
    это вложение, и внутрь него заходить нельзя. Иначе текст чужого письма
    подмешивается в тело внешнего, а само вложение теряется.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []

    # Корень — тоже часть: у простого письма payload это строка, а не список.
    _collect_parts([message], plain_parts, html_parts, attachments)

    body_text = "\n\n".join(plain_parts).strip()
    body_html = "\n\n".join(html_parts).strip()
    return body_text, body_html, attachments


def _collect_parts(payload, plain_parts, html_parts, attachments) -> None:
    """Рекурсивный обход частей письма."""
    if not isinstance(payload, list):
        return
    for part in payload:
        content_type = part.get_content_type()
        filename = _part_filename(part)
        disposition = (part.get_content_disposition() or "").lower()

        if content_type == "message/rfc822":
            # Вложенное письмо: отдаём как вложение, внутрь не заходим.
            content = _part_bytes(part)
            if content:
                attachments.append(
                    Attachment(
                        filename=filename or "пересланное-письмо.eml",
                        mime_type=content_type,
                        content=content,
                        content_id=(part.get("Content-ID") or "").strip("<> ").strip() or None,
                        inline=(disposition == "inline"),
                    )
                )
            continue

        if part.is_multipart():
            _collect_parts(part.get_payload(), plain_parts, html_parts, attachments)
            continue

        if filename or disposition == "attachment":
            content = _part_bytes(part)
            if content is None:
                continue
            attachments.append(
                Attachment(
                    filename=filename or "attachment-" + str(len(attachments) + 1),
                    mime_type=content_type or "application/octet-stream",
                    content=content,
                    content_id=(part.get("Content-ID") or "").strip("<> ").strip() or None,
                    inline=(disposition == "inline"),
                )
            )
            continue

        if content_type == "text/plain":
            text = _part_text(part)
            if text.strip():
                plain_parts.append(text)
        elif content_type == "text/html":
            html = _part_text(part)
            if html.strip():
                html_parts.append(html)


def _part_filename(part: Message) -> str:
    """Имя вложения с раскодированием RFC 2047 (вид =?utf-8?B?...?=)."""
    try:
        raw_name = part.get_filename()
    except (KeyError, UnboundLocalError):
        # policy=default иногда падает на кривом Content-Disposition.
        raw_name = part.get_param("filename")
    if raw_name is None:
        return ""
    return decode_header_value(raw_name).replace("\\", "/").split("/")[-1].strip()


def _part_text(part: Message) -> str:
    """Текст части с запасным путём на случай неизвестной кодировки."""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except (LookupError, UnicodeDecodeError, AssertionError, ValueError):
        pass
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _part_bytes(part: Message) -> bytes | None:
    """Байты вложения после декодирования transfer-encoding."""
    if part.get_content_maintype() == "message":
        # Вложенное письмо (.eml) приходит как message/rfc822. Обычно это
        # готовое вложенное сообщение, но часть клиентов кодирует его base64 —
        # тогда email-пакет разбирает base64 как «письмо» и decode=True пуст.
        raw = b""
        nested = part.get_payload()
        if isinstance(nested, list) and nested:
            raw = nested[0].as_bytes()
        decoded = part.get_payload(decode=True)
        if isinstance(decoded, bytes) and decoded:
            raw = decoded
        cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        if cte == "base64":
            compact = b"".join(raw.split())
            try:
                return base64.b64decode(compact)
            except (ValueError, TypeError):
                return raw or None
        if raw:
            return raw
        return part.as_bytes()
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    raw = part.get_payload()
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace")
    return None


# ------------------------------------------------------------- заголовки и даты


def _header_text(value) -> str:
    """Заголовок в человекочитаемом виде (декодирование RFC 2047)."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return decode_header_value(text)


def decode_header_value(value: str) -> str:
    """Раскодирует заголовок вида =?charset?B?...?=; при сбое возвращает как есть."""
    if not value:
        return ""
    try:
        chunks = email.header.decode_header(value)
    except Exception:  # noqa: BLE001 - кривой заголовок не должен ронять разбор
        return value.strip()
    out: list[str] = []
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _parse_from(message: Message) -> tuple[str, str]:
    """Возвращает (имя, адрес) отправителя."""
    raw = _header_text(message.get("From"))
    if not raw:
        return "", ""
    try:
        name, address = email.utils.parseaddr(raw)
    except Exception:  # noqa: BLE001
        return raw, ""
    return (name or "").strip(), (address or "").strip()


def _parse_date(message: Message) -> dt.datetime | None:
    """Дата письма; при отсутствии/ошибке — None."""
    raw = message.get("Date")
    if raw is None:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed


def normalize_message_id(value: str | None) -> str:
    """Снимает угловые скобки: <id@host> -> id@host (как в MailSense)."""
    return (value or "").strip().strip("<>").strip()


# ------------------------------------------------------------------- эвристики


def is_forwarded(subject: str, body: str) -> bool:
    """Похоже ли письмо на пересылку.

    У пересланного письма From — тот, кто переслал, а корреспондент указан
    внутри. Автоматическая запись такого адреса в графу «От кого получен»
    была бы ошибкой, поэтому признак выставляется и попадает в промпт.
    """
    low = (subject or "").strip().lower()
    if any(low.startswith(prefix) for prefix in _FORWARD_SUBJECTS):
        return True
    low_body = (body or "")[:4000].lower()
    return any(marker in low_body for marker in _FORWARD_MARKERS)


def classify_kind(mime_type: str, filename: str) -> str:
    """Тип вложения: image | pdf | docx | xlsx | email | text | html | other.

    Порт classifyKind из MailSense: смотрим и MIME, и расширение — отправители
    регулярно присылают .pdf с MIME application/octet-stream.

    Отдельный тип email — для пересланного письма вложением (.eml): его нужно
    разобрать как письмо (шапка, текст, вложения), а не читать как текст.
    """
    mime = (mime_type or "").lower()
    name = (filename or "").lower()
    if mime.startswith("image/") or _IMAGE_EXT.search(name):
        return "image"
    if mime == "application/pdf" or _PDF_EXT.search(name):
        return "pdf"
    if "wordprocessingml" in mime or mime == "application/msword" or _DOCX_EXT.search(name):
        return "docx"
    if "spreadsheetml" in mime or mime == "application/vnd.ms-excel" or _XLSX_EXT.search(name):
        return "xlsx"
    if mime == "message/rfc822" or name.endswith(".eml"):
        return "email"
    if mime.startswith("text/html") or name.endswith((".html", ".htm")):
        return "html"
    if mime.startswith("text/plain") or name.endswith((".txt", ".log")):
        return "text"
    return "other"


_NUMBER_BODY = r"[0-9][0-9/\-–—]*"


def extract_external_number(text: str) -> str | None:
    """Ищет внешний номер по шаблонам «Исх. № 123», «Вх. 456/78», «№ 789».

    Порт extractExternalNumber из MailSense. Это страховка: основной источник
    номера — ответ модели (она смотрит имена файлов и шапку документа).
    """
    if not text:
        return None
    labeled = re.search(
        r"(?:исх\.?|вх\.?|исходящий|входящий|номер|number|ref|reference)"
        r"\s*(?:№|N|#)?\s*[:.\s-]*\s*(" + _NUMBER_BODY + r")",
        text,
        re.IGNORECASE,
    )
    if labeled:
        return _clean_number(labeled.group(1))
    bare = re.search(r"(?:№|N|#)\s*[:.\s-]*\s*(" + _NUMBER_BODY + r")", text, re.IGNORECASE)
    if bare:
        return _clean_number(bare.group(1))
    return None


def _clean_number(value: str) -> str:
    return value.replace("–", "-").replace("—", "-").strip()


_HTML_SCRIPT = re.compile(r"<script[\s\S]*?</script>", re.IGNORECASE)
_HTML_STYLE = re.compile(r"<style[\s\S]*?</style>", re.IGNORECASE)
_HTML_TAGS = re.compile(r"<[^>]+>")
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&laquo;": "«",
    "&raquo;": "»",
    "&mdash;": "—",
    "&ndash;": "–",
}


def strip_html(html: str) -> str:
    """Грубое превращение HTML в текст — порт stripHtml из MailSense."""
    if not html:
        return ""
    text = _HTML_SCRIPT.sub(" ", html)
    text = _HTML_STYLE.sub(" ", text)
    text = _HTML_TAGS.sub(" ", text)
    for entity, replacement in _HTML_ENTITIES.items():
        text = text.replace(entity, replacement)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()
