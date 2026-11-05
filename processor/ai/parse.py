"""Разбор ответа модели в Analysis.

Порт parseAnalysis из MailSense (packages/core/src/ai/parse.ts), но без zod:
схема проверяется вручную, зато с «мягким» приведением типов и синонимами полей.

Почему мягко: модель иногда отвечает "sheets": "3", "doc_date": "", "register":
"да", заворачивает JSON в блок кода или добавляет пояснение перед ним.
Служба пишет в журнал документов, поэтому нужны и терпимость к формату, и
строгость к смыслу: пустая строка превращается в null, а не в значение.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from ..errors import AnalysisParseError
from ..models import Analysis

__all__ = ["AnalysisParseError", "extract_json", "parse_analysis"]

# Блок кода в ответе модели выделяется тремя обратными кавычками; собираем
# шаблон из символа, чтобы строка не ломала чтение файла и подсветку.
_BACKTICK = chr(96)
_FENCE = re.compile(
    _BACKTICK * 3 + r"(?:json)?\s*([\s\S]*?)" + _BACKTICK * 3, re.IGNORECASE
)

_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d/%m/%Y", "%Y%m%d")

#: Синонимы полей: разные промпты и разные модели называют одно и то же по-своему.
_ALIASES: dict[str, tuple[str, ...]] = {
    "register": ("register", "shouldregister", "isregister", "зарегистрировать"),
    "register_reason": ("registerreason", "reason", "why"),
    "confidence": ("confidence", "уверенность", "certainty"),
    "summary": ("summary", "краткоесодержание", "содержание", "annotation"),
    "document_name": ("documentname", "docname", "documenttype", "наименованиедокумента"),
    "sender": ("sender", "correspondent", "senderorg", "отправитель", "откого"),
    "doc_number": ("docnumber", "externalnumber", "extnumber", "number", "номер"),
    "doc_date": ("docdate", "senddate", "externaldate", "документдата"),
    "sheets_count": ("sheetscount", "sheets", "pages", "листов"),
    "thema": ("thema", "theme", "topic", "тема"),
    "category": ("category", "категория"),
}


def extract_json(text: str) -> str:
    """Достаёт JSON из ответа модели (порт extractJson из MailSense).

    Порядок: чистый объект -> блок кода с пометкой json -> от первой { до
    последней }. Так разбираются ответы с пояснением перед JSON.
    """
    if not text:
        return ""
    trimmed = text.strip()
    if trimmed.startswith("{") and trimmed.endswith("}"):
        return trimmed
    fenced = _FENCE.search(trimmed)
    if fenced:
        return fenced.group(1).strip()
    first = trimmed.find("{")
    last = trimmed.rfind("}")
    if first >= 0 and last > first:
        return trimmed[first : last + 1]
    return trimmed


def parse_analysis(raw: str, model_used: str = "") -> Analysis:
    """Превращает сырой ответ модели в Analysis.

    Бросает AnalysisParseError с фрагментом ответа: по нему видно, что именно
    вернула модель, без повторного запроса.
    """
    payload = extract_json(raw)
    if not payload:
        raise AnalysisParseError("Модель вернула пустой ответ")

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        snippet = payload[:300].replace("\n", " ")
        raise AnalysisParseError(
            "Ответ модели не является JSON (" + str(exc) + "). Начало ответа: " + snippet
        ) from exc

    if not isinstance(parsed, dict):
        raise AnalysisParseError("Ответ модели должен быть JSON-объектом")

    fields = _normalize_keys(parsed)

    return Analysis(
        summary=_as_text(_pick(fields, "summary")),
        document_name=_as_text(_pick(fields, "document_name")),
        sender=_as_text(_pick(fields, "sender")),
        doc_number=_as_optional_text(_pick(fields, "doc_number")),
        doc_date=_as_date(_pick(fields, "doc_date")),
        sheets_count=_as_positive_int(_pick(fields, "sheets_count")),
        thema_name=_as_optional_text(_pick(fields, "thema")),
        category=_as_category(_pick(fields, "category")),
        register=_as_flag(_pick(fields, "register")),
        register_reason=_as_text(_pick(fields, "register_reason")),
        confidence=_as_confidence(_pick(fields, "confidence")),
        raw_response=raw,
        model_used=model_used,
    )


# ----------------------------------------------------------------- приведение


def _normalize_keys(obj: dict) -> dict[str, Any]:
    """Ключи без регистра, пробелов, подчёркиваний и дефисов."""
    normalized: dict[str, Any] = {}
    for key, value in obj.items():
        if not isinstance(key, str):
            continue
        normalized[re.sub(r"[\s_\-]", "", key).lower()] = value
    return normalized


def _pick(fields: dict[str, Any], name: str) -> Any:
    """Ищет значение по всем синонимам поля."""
    for alias in _ALIASES.get(name, (name,)):
        key = re.sub(r"[\s_\-]", "", alias).lower()
        if key in fields:
            return fields[key]
    return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _as_optional_text(value: Any) -> str | None:
    """Пустая строка — это отсутствие значения, а не пустое поле журнала."""
    text = _as_text(value)
    if not text:
        return None
    # Модель любит писать «null» строкой.
    if text.lower() in ("null", "none", "нет", "не указан", "n/a", "-"):
        return None
    return text


def _as_flag(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "да", "y", "нужно", "требуется"):
            return True
        if lowered in ("false", "0", "no", "нет", "n", "не нужно"):
            return False
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return None
    return None


def _as_positive_int(value: Any) -> int | None:
    number = _as_int(value)
    if number is None or number <= 0:
        return None
    return number


def _as_confidence(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        number = float(text.replace(",", ".").rstrip("%"))
    except (TypeError, ValueError):
        return None
    if text.endswith("%") or number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _as_date(value: Any) -> dt.date | None:
    """Дата в ISO, «ДД.ММ.ГГГГ», ISO с временем или «15 января 2026»."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "нет"):
        return None
    try:
        if "T" in text or " " in text:
            return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return _parse_russian_date(text)


_RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def _parse_russian_date(text: str) -> dt.date | None:
    match = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", text.lower())
    if not match:
        return None
    day, month_name, year = match.groups()
    month = _RU_MONTHS.get(month_name)
    if not month:
        return None
    try:
        return dt.date(int(year), month, int(day))
    except ValueError:
        return None


def _as_category(value: Any) -> str:
    text = _as_text(value).lower()
    if text in ("work", "рабочее", "рабочий", "business"):
        return "work"
    if text in ("spam", "спам", "реклама"):
        return "spam"
    return "personal"
