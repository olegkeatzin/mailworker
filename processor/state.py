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
    subject     TEXT,
    from_display TEXT,
    quality     TEXT NOT NULL DEFAULT 'ok',
    warnings    TEXT,
    card_json   TEXT,
    reason      TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_message ON processed(message_id);
CREATE INDEX IF NOT EXISTS idx_processed_card ON processed(card_id);
CREATE TABLE IF NOT EXISTS archive_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT NOT NULL,
    card_id     INTEGER,
    file_index  INTEGER NOT NULL,
    kind        TEXT,
    ext         TEXT,
    filename    TEXT,
    scan_id     INTEGER,
    remote_path TEXT,
    doc_path    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE(dedup_key, file_index)
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON archive_queue(status);
CREATE TABLE IF NOT EXISTS rescan_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    INTEGER,
    dedup_key  TEXT,
    old_json   TEXT,
    new_json   TEXT,
    created_at TEXT NOT NULL
);
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
            # Старые базы: добавляем новые колонки, не теряя историю.
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(processed)").fetchall()
            }
            for name, definition in (
                ("subject", "TEXT"),
                ("from_display", "TEXT"),
                ("quality", "TEXT NOT NULL DEFAULT 'ok'"),
                ("warnings", "TEXT"),
                ("card_json", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(
                        "ALTER TABLE processed ADD COLUMN " + name + " " + definition
                    )

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
                "SELECT dedup_key, status, card_id, reg_number, subject, from_display,"
                " quality, warnings, card_json, reason, updated_at"
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
            "subject": row[4],
            "from_display": row[5],
            "quality": row[6],
            "warnings": row[7],
            "card_json": row[8],
            "reason": row[9],
            "updated_at": row[10],
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
        subject: str = "",
        from_display: str = "",
        quality: str = "ok",
        warnings: str = "",
        card_json: str = "",
    ) -> None:
        self._upsert(
            key,
            message_id=message_id,
            uid=uid,
            status=STATUS_REGISTERED,
            card_id=None if card_id is None else str(card_id),
            reg_number=reg_number,
            subject=subject,
            from_display=from_display,
            quality=quality,
            warnings=warnings,
            card_json=card_json,
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
        subject: str = "",
        from_display: str = "",
        quality: str = "ok",
        warnings: str = "",
        card_json: str = "",
    ) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._session() as connection:
            connection.execute(
                "INSERT INTO processed"
                " (dedup_key, message_id, uid, status, card_id, reg_number, subject,"
                "  from_display, quality, warnings, card_json, reason, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dedup_key) DO UPDATE SET"
                "   message_id = excluded.message_id,"
                "   uid = excluded.uid,"
                "   status = excluded.status,"
                "   card_id = excluded.card_id,"
                "   reg_number = excluded.reg_number,"
                "   subject = excluded.subject,"
                "   from_display = excluded.from_display,"
                "   quality = excluded.quality,"
                "   warnings = excluded.warnings,"
                "   card_json = excluded.card_json,"
                "   reason = excluded.reason,"
                "   updated_at = excluded.updated_at",
                (
                    key,
                    message_id,
                    uid,
                    status,
                    card_id,
                    reg_number,
                    subject,
                    from_display,
                    quality,
                    warnings,
                    card_json,
                    reason,
                    now,
                ),
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

    # ------------------------------------------------------------- качество
    def set_quality(self, key: str, quality: str, warnings: str = "") -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE processed SET quality = ?, warnings = ?, updated_at = ?"
                " WHERE dedup_key = ?",
                (
                    quality,
                    warnings,
                    dt.datetime.now().isoformat(timespec="seconds"),
                    key,
                ),
            )

    def quality_counts(self) -> dict[str, int]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT quality, COUNT(*) FROM processed GROUP BY quality"
            ).fetchall()
        return {str(quality): int(count) for quality, count in rows}

    # ---------------------------------------------------------- архив/очередь
    def enqueue_files(self, key: str, card_id: object, files: list[dict]) -> None:
        """Ставит файлы письма в очередь; повторный вызов не дублирует строки."""
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._session() as connection:
            for item in files:
                connection.execute(
                    "INSERT INTO archive_queue"
                    " (dedup_key, card_id, file_index, kind, ext, filename,"
                    "  status, attempts, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)"
                    " ON CONFLICT(dedup_key, file_index) DO UPDATE SET"
                    "   card_id = excluded.card_id,"
                    "   kind = excluded.kind,"
                    "   ext = excluded.ext,"
                    "   filename = excluded.filename",
                    (
                        key,
                        int(card_id) if card_id is not None else None,
                        int(item["index"]),
                        item.get("kind", ""),
                        item.get("ext", ""),
                        item.get("filename", ""),
                        now,
                    ),
                )

    def files_for(self, key: str) -> list[dict]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT id, file_index, kind, ext, filename, scan_id, remote_path,"
                " doc_path, status, attempts, last_error"
                " FROM archive_queue WHERE dedup_key = ? ORDER BY file_index",
                (key,),
            ).fetchall()
        return [_queue_row(row) for row in rows]

    def mark_file(
        self,
        file_id: int,
        status: str,
        *,
        scan_id: int | None = None,
        remote_path: str = "",
        doc_path: str = "",
        error: str = "",
    ) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE archive_queue SET status = ?, scan_id = ?, remote_path = ?,"
                " doc_path = ?, last_error = ?, attempts = attempts + 1,"
                " updated_at = ? WHERE id = ?",
                (
                    status,
                    scan_id,
                    remote_path,
                    doc_path,
                    error[:1000],
                    dt.datetime.now().isoformat(timespec="seconds"),
                    file_id,
                ),
            )

    def queue_counts(self) -> dict[str, int]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM archive_queue GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}

    def failed_files(self, limit: int = 100) -> list[dict]:
        with self._session() as connection:
            rows = connection.execute(
                "SELECT q.id, q.dedup_key, q.card_id, q.file_index, q.kind, q.ext,"
                " q.filename, q.scan_id, q.remote_path, q.doc_path, q.status,"
                " q.attempts, q.last_error, p.subject, p.warnings"
                " FROM archive_queue q LEFT JOIN processed p"
                " ON p.dedup_key = q.dedup_key"
                " WHERE q.status IN ('pending', 'failed')"
                " ORDER BY q.updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "dedup_key": row[1],
                "card_id": row[2],
                "index": row[3],
                "kind": row[4],
                "ext": row[5],
                "filename": row[6],
                "scan_id": row[7],
                "remote_path": row[8],
                "doc_path": row[9],
                "status": row[10],
                "attempts": row[11],
                "last_error": row[12],
                "subject": row[13],
                "warnings": row[14],
            }
            for row in rows
        ]

    # --------------------------------------------------------------- выборки
    def letter_by_card(self, card_id: object) -> dict | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT dedup_key, message_id, uid, status, card_id, reg_number,"
                " subject, from_display, quality, warnings, card_json, reason,"
                " updated_at FROM processed WHERE card_id = ?"
                " ORDER BY updated_at DESC LIMIT 1",
                (str(card_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "key": row[0],
            "message_id": row[1],
            "uid": row[2],
            "status": row[3],
            "card_id": row[4],
            "reg_number": row[5],
            "subject": row[6],
            "from_display": row[7],
            "quality": row[8],
            "warnings": row[9],
            "card_json": row[10],
            "reason": row[11],
            "updated_at": row[12],
        }

    def recent_letters(self, limit: int = 100, quality: str | None = None) -> list[dict]:
        query = (
            "SELECT dedup_key, message_id, status, card_id, reg_number, subject,"
            " from_display, quality, warnings, updated_at FROM processed"
        )
        params: tuple = ()
        if quality:
            query += " WHERE quality = ?"
            params = (quality,)
        query += " ORDER BY updated_at DESC LIMIT ?"
        with self._session() as connection:
            rows = connection.execute(query, params + (limit,)).fetchall()
        return [
            {
                "key": row[0],
                "message_id": row[1],
                "status": row[2],
                "card_id": row[3],
                "reg_number": row[4],
                "subject": row[5],
                "from_display": row[6],
                "quality": row[7],
                "warnings": row[8],
                "updated_at": row[9],
            }
            for row in rows
        ]

    def log_rescan(
        self, card_id: object, key: str, old_json: str, new_json: str
    ) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT INTO rescan_log (card_id, dedup_key, old_json, new_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    int(card_id) if card_id is not None else None,
                    key,
                    old_json,
                    new_json,
                    dt.datetime.now().isoformat(timespec="seconds"),
                ),
            )


def _queue_row(row) -> dict:
    return {
        "id": row[0],
        "index": row[1],
        "kind": row[2],
        "ext": row[3],
        "filename": row[4],
        "scan_id": row[5],
        "remote_path": row[6],
        "doc_path": row[7],
        "status": row[8],
        "attempts": row[9] if len(row) > 9 else 0,
        "last_error": row[10] if len(row) > 10 else "",
    }
