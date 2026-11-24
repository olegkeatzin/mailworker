"""Журнал учёта: поля записи, справочники и запись в Firebird."""

from .dictionaries import FirebirdDictionaries
from .firebird import FirebirdJournal
from .mapping import build_card, describe_card

__all__ = [
    "FirebirdDictionaries",
    "FirebirdJournal",
    "build_card",
    "describe_card",
]
