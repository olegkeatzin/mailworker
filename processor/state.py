"""Учёт обработанных писем в локальной базе SQLite.

Зачем это нужно, если у писем есть Message-ID:

  * опрос ящика повторяется, и одно письмо легко попадёт в обработку дважды —
    например, если после записи не удалось поставить флаг «прочитано»;
  * IMAP и POP3 не дают «транзакции» на всю обработку, поэтому нужен свой
    устойчивый след: что уже записано в журнал, что пропущено и почему;
  * ошибки ИИ и базы не должны терять письмо — при следующем опросе оно
    обрабатывается снова (настройка [processing] retry_errors).

Ключ — протокол, ящик, папка и Message-ID. Хранится в файле рядом со службой,
поэтому переживает перезапуск.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    dedup_key   TEXT PRIMARY KEY,
    message_id  TEXT,
    uid         TEXT,
    status      TEXT NOT NULL,
    card_id     TEXT,
    reg_number  TEXT,
    reason      TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_message ON processed(message_id);
"""

STATUS_REGISTERED = "registered"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"


class ProcessedState:
    """Журнал обработки писем (не путать с журналом учёта документов)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ схема

    def _init_schema(self) -> None:
        with self._session() as connection:
            connection.executescript(_SCHEMA)

    @contextmanager
    def _session(self):
        """Соединение на одну операцию с гарантированным закрытием.

        Контекстный менеджер sqlite3.Connection делает COMMIT, но НЕ закрывает
        соединение: в долгоживущей службе это утечка файловых дескрипторов.
        """
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------------ чтение

    def get(self, key: str) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT dedup_key, status, card_id, reg_number, reason, updated_at"
                " FROM processed WHERE dedup_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "key": row[0],
            "status": row[1],
            "card_id": row[2],
            "reg_number": row[3],
            "reason": row[4],
            "updated_at": row[5],
        }

    def should_skip(self, key: str, *, retry_errors: bool = True) -> bool:
        """Обработано ли письмо раньше.

        Ошибки можно повторять: письмо осталось не записанным, и терять его
        из-за временной недоступности модели нельзя.
        """
        record = self.get(key)
        if record is None:
            return False
        if record["status"] == STATUS_ERROR:
            return not retry_errors
        return True

    # ------------------------------------------------------------------ запись

    def mark_registered(
        self,
        key: str,
        *,
        message_id: str,
        uid: str,
        card_id: object = None,
        reg_number: str | None = None,
    ) -> None:
        self._upsert(
            key,
            message_id=message_id,
            uid=uid,
            status=STATUS_REGISTERED,
            card_id=None if card_id is None else str(card_id),
            reg_number=reg_number,
            reason=None,
        )

    def mark_skipped(self, key: str, *, message_id: str, uid: str, reason: str) -> None:
        self._upsert(
            key,
            message_id=message_id,
            uid=uid,
            status=STATUS_SKIPPED,
            card_id=None,
            reg_number=None,
            reason=reason,
        )

    def mark_error(self, key: str, *, message_id: str, uid: str, error: str) -> None:
        self._upsert(
            key,
            message_id=message_id,
            uid=uid,
            status=STATUS_ERROR,
            card_id=None,
            reg_number=None,
            reason=error[:1000],
        )

    def _upsert(
        self,
        key: str,
        *,
        message_id: str,
        uid: str,
        status: str,
        card_id: str | None,
        reg_number: str | None,
        reason: str | None,
    ) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._session() as connection:
            connection.execute(
                "INSERT INTO processed"
                " (dedup_key, message_id, uid, status, card_id, reg_number, reason, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dedup_key) DO UPDATE SET"
                "   message_id = excluded.message_id,"
                "   uid = excluded.uid,"
                "   status = excluded.status,"
                "   card_id = excluded.card_id,"
                "   reg_number = excluded.reg_number,"
                "   reason = excluded.reason,"
                "   updated_at = excluded.updated_at",
                (key, message_id, uid, status, card_id, reg_number, reason, now),
            )

    # --------------------------------------------------------------- статистика

    def counts(self) -> dict[str, int]:
        """Сколько писем в каком состоянии — для итога прохода и --check."""
        with self._session() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM processed GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def last_errors(self, limit: int = 10) -> list[dict]:
        """Последние ошибки — чтобы увидеть причину, не читая весь лог."""
        with self._session() as connection:
            rows = connection.execute(
                "SELECT message_id, reason, updated_at FROM processed"
                " WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (STATUS_ERROR, limit),
            ).fetchall()
        return [
            {"message_id": row[0], "reason": row[1], "updated_at": row[2]} for row in rows
        ]