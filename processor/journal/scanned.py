"""Запись файлов в SCANNED и резервирование ID_SCANNED.

Схема повторяет test_save_file2.py:

  1. SELECT GEN_ID(ID_SCANNED, 1)  — резервируем ID;
  2. INSERT INTO SCANNED с явным ID (до фиксации);
  3. загружаем файл на SMB;
  4. commit; при ошибке — rollback и компенсирующий DELETE.

Firebird не откатывает значение генератора: пропуски ID — нормальная ситуация.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

from ..config import ArchiveConfig, DatabaseConfig
from ..errors import StorageError
from .firebird import connect, sanitize_value

logger = logging.getLogger(__name__)

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class ScannedStore:
    """Одно соединение и одна транзакция на файл."""

    def __init__(self, db_cfg: DatabaseConfig, archive: ArchiveConfig) -> None:
        self.db_cfg = db_cfg
        self.archive = archive
        self._connection = None

    # ------------------------------------------------------------------ жизнь

    def __enter__(self) -> "ScannedStore":
        self._connection = connect(self.db_cfg)
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    # ------------------------------------------------------------- операции

    def reserve_id(self) -> int:
        generator = self._object_name(self.archive.scanned_generator)
        cursor = self._conn.cursor()
        try:
            cursor.execute(f"SELECT GEN_ID({generator}, 1) FROM RDB$DATABASE")
            row = cursor.fetchone()
        finally:
            cursor.close()
        if not row or row[0] is None:
            raise StorageError("Firebird не вернул ID из " + generator)
        return int(row[0])

    def insert_scan(self, scan_id: int, card_id: int, who: str, doc_path: str) -> None:
        table = self._object_name(self.archive.scanned_table)
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO " + table + " (ID, MASTERID, WHO, DOC_PATH,"
                " OLD_MASTERID, CARD_TYPE, SAVEDATE)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scan_id,
                    card_id,
                    self._text(who, 25),
                    self._text(doc_path, 300),
                    card_id,
                    int(self.archive.scanned_card_type),
                    dt.datetime.now(),
                ),
            )
        finally:
            cursor.close()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось откатить транзакцию SCANNED", exc_info=True)

    def delete_scan(self, scan_id: int) -> None:
        """Компенсирующий DELETE на случай, если драйвер успел зафиксировать."""
        table = self._object_name(self.archive.scanned_table)
        try:
            cursor = self._conn.cursor()
            try:
                cursor.execute("DELETE FROM " + table + " WHERE ID = ?", (scan_id,))
            finally:
                cursor.close()
            self._conn.commit()
            logger.info("Строка SCANNED ID=%s удалена компенсирующим DELETE", scan_id)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось удалить строку SCANNED ID=%s", scan_id)

    # ---------------------------------------------------------------- внутреннее

    @property
    def _conn(self):
        if self._connection is None:
            raise StorageError("Нет соединения с Firebird: вызовите ScannedStore()")
        return self._connection

    def _text(self, value: str, limit: int) -> str:
        return sanitize_value(self.db_cfg, (value or "")[:limit])

    def _object_name(self, value: str) -> str:
        if not _NAME.match(value or ""):
            raise StorageError("Недопустимое имя таблицы/генератора: " + repr(value))
        return value
