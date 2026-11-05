"""Справочники корреспондентов (KORRESP) и тем (THEMA) из Firebird.

Запросы к справочникам задаются настройками [classifiers], а не зашиты в код:
колонки в разных базах КАНЦ отличаются, и администратор должен иметь возможность
поправить запрос без правки программы. Но результат приводится к одному виду —
четыре значения по порядку: идентификатор, название, часть номера, показывать.

Пополнение справочника (режимы unknown_correspondent = create и
unknown_thema = create) — отдельные настройки, по умолчанию выключены:
заводить записи в справочнике документов автоматически опасно, мусорные
названия потом расходятся по всему журналу.
"""

from __future__ import annotations

import logging

from ..classifiers import Correspondent, Theme
from ..config import ClassifiersConfig, DatabaseConfig
from ..errors import StorageError
from .firebird import connect, sanitize_value

logger = logging.getLogger(__name__)

#: Генераторы идентификаторов справочников — из схемы базы.
CORRESPONDENT_GENERATOR = "ID_KORRESP"
THEME_GENERATOR = "ID_THEMA"


class FirebirdDictionaries:
    """Источник справочников для ClassifierService."""

    def __init__(self, db_cfg: DatabaseConfig, clf_cfg: ClassifiersConfig) -> None:
        self.db_cfg = db_cfg
        self.clf_cfg = clf_cfg

    # ------------------------------------------------------------------ чтение

    def load_correspondents(self) -> list[Correspondent]:
        return self._load(self.clf_cfg.correspondents_sql, Correspondent, "KORRESP")

    def load_themes(self) -> list[Theme]:
        return self._load(self.clf_cfg.themes_sql, Theme, "THEMA")

    def _load(self, sql: str, factory, title: str) -> list:
        connection = connect(self.db_cfg)
        rows: list = []
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                rows = cursor.fetchall() or []
            finally:
                cursor.close()
        except Exception as exc:  # noqa: BLE001
            raise StorageError(
                "Не удалось прочитать справочник "
                + title
                + ": "
                + str(exc)
                + ". Проверьте [classifiers] "
                + ("correspondents_sql" if title == "KORRESP" else "themes_sql")
                + " — запрос должен вернуть колонки ID, NAME, NUMBPART, SHOWS."
            ) from exc
        finally:
            connection.close()

        items = []
        for number, row in enumerate(rows, start=1):
            if row is None or len(row) < 2:
                continue
            raw_id, raw_name = row[0], row[1]
            if raw_id is None or raw_name is None:
                continue
            name = str(raw_name).strip()
            if not name:
                continue
            numbpart = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
            shows = True if len(row) < 4 else _truthy(row[3])
            try:
                item_id = int(raw_id)
            except (TypeError, ValueError):
                logger.warning(
                    "Справочник %s, строка %s: идентификатор «%s» не число — пропущено",
                    title,
                    number,
                    raw_id,
                )
                continue
            items.append(factory(id=item_id, name=name, numbpart=numbpart, shows=shows))
        return items

    # ---------------------------------------------------------------- пополнение

    def create_correspondent(self, name: str) -> Correspondent:
        """Заводит корреспондента. Возвращает существующего, если такой уже есть."""
        text = self._prepare_name(name, "корреспондента")
        existing = self._existing_id("KORRESP", text)
        if existing is not None:
            return Correspondent(id=existing, name=text)
        new_id = self._insert(
            "KORRESP",
            CORRESPONDENT_GENERATOR,
            "INSERT INTO KORRESP (ID, NAME, NUMBPART, CARD_TYPE, SHOWS) VALUES (?, ?, ?, ?, ?)",
            (text, "", 0, 1),
        )
        logger.info("В справочник KORRESP добавлена запись ID=%s «%s»", new_id, text)
        return Correspondent(id=new_id, name=text)

    def create_theme(self, name: str) -> Theme:
        """Заводит тему. Возвращает существующую, если такая уже есть."""
        text = self._prepare_name(name, "темы")
        existing = self._existing_id("THEMA", text)
        if existing is not None:
            return Theme(id=existing, name=text)
        new_id = self._insert(
            "THEMA",
            THEME_GENERATOR,
            "INSERT INTO THEMA (ID, NAME, NUMBPART, SHOWS) VALUES (?, ?, ?, ?)",
            (text, "", 1),
        )
        logger.info("В справочник THEMA добавлена запись ID=%s «%s»", new_id, text)
        return Theme(id=new_id, name=text)

    # ---------------------------------------------------------------- служебное

    def _prepare_name(self, name: str, what: str) -> str:
        text = sanitize_value(self.db_cfg, (name or "").strip())
        limit = self.clf_cfg.max_name_length
        if limit and len(text) > limit:
            text = text[:limit].rstrip()
        if not text:
            raise StorageError("Нельзя завести запись справочника: пустое название " + what)
        return text

    def _existing_id(self, table: str, name: str) -> int | None:
        connection = connect(self.db_cfg)
        cursor = connection.cursor()
        try:
            cursor.execute(
                "SELECT FIRST 1 ID FROM " + table + " WHERE UPPER(NAME) = UPPER(?)",
                [name],
            )
            row = cursor.fetchone()
        except Exception as exc:  # noqa: BLE001
            raise StorageError(
                "Не удалось проверить наличие «" + name + "» в " + table + ": " + str(exc)
            ) from exc
        finally:
            try:
                cursor.close()
            finally:
                connection.close()
        if not row or row[0] is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def _insert(self, table: str, generator: str, sql: str, values: tuple) -> int:
        connection = connect(self.db_cfg)
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT GEN_ID(" + generator + ", 1) FROM RDB$DATABASE")
            row = cursor.fetchone()
            if not row or row[0] is None:
                raise StorageError("Firebird не вернул ID из генератора " + generator)
            new_id = int(row[0])
            cursor.execute(sql, [new_id, *values])
            connection.commit()
        except StorageError:
            connection.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            connection.rollback()
            raise StorageError(
                "Не удалось добавить запись в справочник " + table + ": " + str(exc)
            ) from exc
        finally:
            try:
                cursor.close()
            finally:
                connection.close()
        return new_id


def _truthy(value) -> bool:
    """BOOL в базе — INTEGER, где 1 и -1 это «да», 0 — «нет»."""
    if value is None:
        return True
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return True
