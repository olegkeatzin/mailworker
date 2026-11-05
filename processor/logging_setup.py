"""Логирование: консоль и файл с ротацией.

Служба работает без интерфейса, поэтому журнал работы — единственное место, где
видно, что происходило: какое письмо пришло, что ответила модель, какая запись
ушла в журнал и почему письмо пропущено.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig

_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(cfg: LoggingConfig, level_override: str | None = None) -> None:
    """Настраивает корневой логгер один раз (повторный вызов безопасен)."""
    root = logging.getLogger()
    level = getattr(logging, (level_override or cfg.level).upper(), logging.INFO)
    root.setLevel(level)

    # Повторная настройка (тесты, повторный запуск) не должна плодить дубли строк.
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    if cfg.console:
        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)

    if cfg.file is not None:
        try:
            cfg.file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                cfg.file,
                maxBytes=cfg.max_bytes or 0,
                backupCount=cfg.backup_count,
                encoding="utf-8",
            )
        except OSError as exc:
            # Нет прав на файл — не повод не работать: пишем предупреждение в консоль.
            logging.getLogger(__name__).warning(
                "Не удалось открыть файл журнала %s: %s. Работаем только с консолью.",
                cfg.file,
                exc,
            )
        else:
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

    # urllib и imaplib на уровне DEBUG пишут слишком много (в том числе тело запроса).
    logging.getLogger("urllib3").setLevel(max(level, logging.INFO))


def log_path(cfg: LoggingConfig) -> Path | None:
    """Путь к файлу журнала — нужен для сообщения «куда смотреть» при старте."""
    return cfg.file
