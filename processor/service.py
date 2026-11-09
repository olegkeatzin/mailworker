"""Служба опроса ящика: проход по письмам и бесконечный цикл.

Один проход — это подключение к ящику, забор пачки писем и обработка каждого
письма по отдельности. Ошибка на одном письме не мешает остальным: служба
работает без человека, и останавливаться из-за одного кривого письма нельзя.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass

from .config import AppConfig, MailConfig
from .errors import MailSourceError
from .mail.sources import MailSource, build_source
from .models import ProcessResult
from .pipeline import (
    STATUS_DUPLICATE,
    STATUS_DRY_RUN,
    STATUS_ERROR,
    STATUS_REGISTERED,
    STATUS_SKIPPED,
    EmailProcessor,
)

logger = logging.getLogger(__name__)


@dataclass
class CycleStats:
    """Итог одного прохода — то, что попадает в лог и в --check."""

    fetched: int = 0
    registered: int = 0
    skipped: int = 0
    duplicates: int = 0
    errors: int = 0
    dry_run: int = 0
    source_error: str = ""

    def add(self, result: ProcessResult) -> None:
        if result.status == STATUS_REGISTERED:
            self.registered += 1
        elif result.status == STATUS_SKIPPED:
            self.skipped += 1
        elif result.status == STATUS_DUPLICATE:
            self.duplicates += 1
        elif result.status == STATUS_ERROR:
            self.errors += 1
        elif result.status == STATUS_DRY_RUN:
            self.dry_run += 1

    def as_text(self) -> str:
        parts = [
            "получено: " + str(self.fetched),
            "записано: " + str(self.registered),
            "пропущено: " + str(self.skipped),
            "уже было: " + str(self.duplicates),
            "ошибок: " + str(self.errors),
        ]
        if self.dry_run:
            parts.append("пробный прогон: " + str(self.dry_run))
        if self.source_error:
            parts.append("ошибка ящика: " + self.source_error)
        return ", ".join(parts)

    @property
    def processed(self) -> int:
        return self.registered + self.skipped + self.duplicates + self.errors + self.dry_run


class MailWorkerService:
    """Опрос ящика по расписанию и обработка писем."""

    def __init__(
        self,
        cfg: AppConfig,
        processor: EmailProcessor,
        source_factory=build_source,
        mail_cfg: MailConfig | None = None,
    ) -> None:
        self.cfg = cfg
        self.processor = processor
        self.source_factory = source_factory
        self.mail_cfg = mail_cfg or cfg.mail
        self._stop = threading.Event()

    # ------------------------------------------------------------------- проход

    def run_once(self) -> CycleStats:
        """Один проход: подключиться, забрать пачку, обработать письма."""
        stats = CycleStats()
        source: MailSource = self.source_factory(self.mail_cfg)
        try:
            source.connect()
            messages = source.fetch(self.mail_cfg.max_per_cycle)
            stats.fetched = len(messages)
            for message in messages:
                result = self.processor.process_raw(
                    message.raw, uid=message.uid, folder=self.mail_cfg.mailbox
                )
                stats.add(result)
                # Помечаем письмо обработанным, только если оно не упало с ошибкой:
                # ошибочное письмо должно попасть в следующий проход.
                if result.status in (STATUS_REGISTERED, STATUS_SKIPPED, STATUS_DUPLICATE):
                    self._mark_processed(source, message.uid)
        except MailSourceError as exc:
            stats.source_error = str(exc)
            logger.error("Почтовый ящик недоступен: %s", exc)
        except Exception as exc:  # noqa: BLE001 - сеть может упасть где угодно
            stats.source_error = str(exc)
            logger.exception("Неожиданная ошибка при опросе ящика")
        finally:
            source.close()

        if stats.fetched or stats.source_error:
            logger.info("Проход завершён: %s", stats.as_text())
        else:
            logger.debug("Новых писем нет")
        return stats

    def _mark_processed(self, source: MailSource, uid: str) -> None:
        try:
            source.mark_processed(uid)
        except MailSourceError as exc:
            # Письмо уже в журнале; повторную запись исключает журнал обработки.
            logger.warning("Не удалось отметить письмо %s обработанным: %s", uid, exc)

    # -------------------------------------------------------------------- цикл

    def run_forever(self) -> None:
        """Бесконечный опрос с паузой между проходами."""
        interval = max(10, self.mail_cfg.poll_interval)
        logger.info(
            "Служба запущена: %s %s:%s, папка «%s», интервал опроса %s с, %s",
            self.mail_cfg.protocol.upper(),
            self.mail_cfg.host,
            self.mail_cfg.port,
            self.mail_cfg.mailbox,
            interval,
            _window_text(self.mail_cfg),
        )
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - цикл не должен умирать
                logger.exception("Проход завершился ошибкой, продолжаем работу")
            # wait вместо sleep: остановка по сигналу не ждёт конца паузы.
            if self._stop.wait(interval):
                break

        logger.info("Служба остановлена")

    def stop(self) -> None:
        """Просит цикл завершиться (вызывается обработчиком сигнала)."""
        self._stop.set()

def _window_text(cfg: MailConfig) -> str:
    """Окно по дате для стартовой строки лога — чтобы видеть, применилось ли оно."""
    if cfg.since_days <= 0:
        return "окно по дате: без ограничения"
    since = dt.date.today() - dt.timedelta(days=max(0, cfg.since_days - 1))
    return (
        "окно по дате на момент запуска: последние "
        + str(cfg.since_days)
        + " дн. (с "
        + since.strftime("%d.%m.%Y")
        + ")"
    )
