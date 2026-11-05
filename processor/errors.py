"""Исключения службы.

Все тексты — для человека: они попадают в журнал работы и в консоль, поэтому
формулируются по-русски и по возможности объясняют, что именно случилось.
"""

from __future__ import annotations


class ProcessorError(Exception):
    """Общая ошибка службы."""


class ConfigError(ProcessorError):
    """Файл настроек неполный, отсутствует или содержит недопустимые значения."""


class MailSourceError(ProcessorError):
    """Не удалось подключиться к почтовому ящику или забрать письма."""


class MailParseError(ProcessorError):
    """Письмо не удалось разобрать."""


class AiError(ProcessorError):
    """Модель недоступна или вернула ошибку."""


class AnalysisParseError(ProcessorError):
    """Ответ модели не удалось привести к схеме журнала."""


class StorageError(ProcessorError):
    """Не удалось записать запись в журнал (БД или API)."""
