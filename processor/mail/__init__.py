"""Работа с почтой: разбор .eml и получение писем из ящика."""

from .parser import (
    classify_kind,
    extract_external_number,
    is_forwarded,
    normalize_message_id,
    parse_eml,
    strip_html,
)

__all__ = [
    "classify_kind",
    "extract_external_number",
    "is_forwarded",
    "normalize_message_id",
    "parse_eml",
    "strip_html",
]
