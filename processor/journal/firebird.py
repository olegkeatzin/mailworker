"""Запись в журнал напрямую в Firebird (таблица CARD).

Повторяет то, что делает FastAPI_sender_to_BD/db.py, и дополнен по рабочей
схеме базы (schema.sql):
  * номер строки берётся из генератора ID_CARD;
  * вставляются те же колонки, что пишет сервис FastAPI, плюс ссылки на
    справочники: KORRESPID (KORRESP.ID) и THEMA (THEMA.ID);
  * даты передаются строкой ДД.ММ.ГГГГ, как в рабочем сервисе.

Отличие одно: значения передаются ПАРАМЕТРАМИ запроса, а не склейкой строки.
Это не смена контракта, а защита: краткое содержание письма с апострофом или
двойными кавычками ломало бы склеенный INSERT, а текст с «'» — обычное дело.

Подключение открывается на запись и закрывается сразу: служба обрабатывает
письма по одному, а разрыв соединения с сервером Firebird не должен требовать
перезапуска всей службы.
"""

from __future__ import annotations

import datetime as dt
import logging

from ..config import DatabaseConfig, JournalConfig
from ..errors import StorageError
from ..models import Analysis, CardData, InsertedCard

logger = logging.getLogger(__name__)

#: Колонки записи: как в FastAPI_sender_to_BD/db.py плюс ссылки на справочники
#: KORRESPID и THEMA. Состав сверен с определением таблицы CARD в схеме базы.
CARD_COLUMNS = (
    "ID",
    "CARD_TYPE",
    "INTINDEX",
    "INTDATE",
    "EXTINDEX",
    "EXTDATE",
    "SH_CONTENTS",
    "PAGES_DOC",
    "KORRESP",
    "KORRESPID",
    "THEMA",
)

INSERT_SQL = (
    "INSERT INTO CARD ("
    + ", ".join(CARD_COLUMNS)
    + ") VALUES ("
    + ", ".join(["?"] * len(CARD_COLUMNS))
    + ")"
)

#: Имена кодировок Firebird и соответствующие кодеки Python.
_CODEC_ALIASES = {
    "UTF8": "utf-8",
    "UNICODE_FSS": "utf-8",
    "WIN1250": "cp1250",
    "WIN1251": "cp1251",
    "WIN1252": "cp1252",
    "WIN1253": "cp1253",
    "WIN1254": "cp1254",
    "WIN1257": "cp1257",
    "ISO8859_5": "iso8859-5",
    "CYRL": "cp1251",
}


def connect(cfg: DatabaseConfig):
    """Открывает соединение с Firebird.

    Вынесено из класса: справочники KORRESP/THEMA читаются тем же подключением,
    и дублировать разбор настроек и понятные сообщения об ошибках не нужно.
    """
    try:
        import fdb
    except ImportError as exc:
        raise StorageError(
            "Не установлен пакет fdb — без него нет доступа к Firebird.\n"
            "Установите зависимости: pip install -r requirements.txt"
        ) from exc

    params = {
        "host": cfg.host,
        "database": cfg.database,
        "user": cfg.user,
        "password": cfg.password,
        "charset": cfg.charset,
    }
    if cfg.fb_client_library:
        # Клиентская библиотека Firebird лежит рядом с программой (Windows)
        # или в /usr/lib (Linux): путь задаётся настройкой, если не в PATH.
        params["fb_library_name"] = cfg.fb_client_library
    try:
        return fdb.connect(**params)
    except Exception as exc:  # noqa: BLE001
        raise StorageError(_explain_connect_error(exc, cfg)) from exc


def sanitize_value(cfg: DatabaseConfig, value: str) -> str:
    """Приводит строку к кодировке базы.

    В WIN1251 нет эмодзи и части европейских символов: без замены драйвер
    упал бы на первом же таком символе, и письмо осталось бы незаписанным.
    """
    text = value or ""
    if not cfg.sanitize_text:
        return text
    codec = _codec(cfg.charset)
    if codec is None:
        return text
    try:
        text.encode(codec)
        return text
    except UnicodeEncodeError:
        sanitized = text.encode(codec, errors="replace").decode(codec, errors="replace")
        logger.warning(
            "В тексте были символы, отсутствующие в %s — заменены на «?»", cfg.charset
        )
        return sanitized


class FirebirdJournal:
    """Реализация записи журнала через пакет fdb."""

    storage_name = "db"

    def __init__(self, cfg: DatabaseConfig, journal_cfg: JournalConfig) -> None:
        self.cfg = cfg
        self.journal_cfg = journal_cfg

    # ------------------------------------------------------------------- запись

    def insert_card(self, card: CardData, analysis: Analysis | None = None) -> InsertedCard:
        """Вставляет запись и возвращает её идентификатор."""
        connection = self._connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT GEN_ID(ID_CARD, 1) FROM RDB$DATABASE")
            row = cursor.fetchone()
            if not row or row[0] is None:
                raise StorageError("Firebird не вернул новый ID из генератора ID_CARD")
            new_id = int(row[0])

            values = [new_id] + self._params(card)
            logger.debug("INSERT INTO CARD: %s", values)
            cursor.execute(INSERT_SQL, values)
            connection.commit()
        except StorageError:
            connection.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - ошибку драйвера переводим в свою
            connection.rollback()
            raise StorageError(_explain_db_error(exc)) from exc
        finally:
            try:
                cursor.close()
            finally:
                connection.close()

        logger.info(
            "Запись в журнал добавлена: ID=%s, INTINDEX=%s, EXTINDEX=%s, KORRESPID=%s, THEMA=%s",
            new_id,
            card.intindex,
            card.extindex or "(без номера)",
            card.korrespid,
            card.thema,
        )
        return InsertedCard(card_id=new_id, reg_number=card.intindex, storage="db")

    # -------------------------------------------------------------- диагностика

    def ping(self) -> str:
        """Проверка подключения к базе (для --check)."""
        connection = self._connect()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT 1 FROM RDB$DATABASE")
            cursor.fetchone()
            cursor.close()
            return self.cfg.host + ":" + self.cfg.database
        finally:
            connection.close()

    def close(self) -> None:
        """Соединение не удерживается — закрывать нечего (метод для единообразия)."""

    # ---------------------------------------------------------------- внутреннее

    def _connect(self):
        return connect(self.cfg)

    def _params(self, card: CardData) -> list:
        return [
            int(card.card_type),
            self._text(card.intindex),
            self._date(card.intdate),
            self._text(card.extindex),
            self._date(card.extdate),
            self._text(card.sh_contents),
            int(card.pages_doc),
            self._text(card.korresp),
            int(card.korrespid or 0),
            int(card.thema or 0),
        ]

    def _text(self, value: str) -> str:
        return sanitize_value(self.cfg, value)

    def _date(self, value: dt.date | None):
        if value is None:
            return None
        if self.cfg.date_format == "date":
            return value
        return value.strftime("%d.%m.%Y")


def _codec(charset: str) -> str | None:
    """Кодек Python для имени кодировки Firebird."""
    name = (charset or "").strip().upper()
    if name in _CODEC_ALIASES:
        return _CODEC_ALIASES[name]
    lowered = (charset or "").strip().lower()
    try:
        "".encode(lowered)
    except LookupError:
        logging.getLogger(__name__).warning(
            "Неизвестная кодировка «%s»: текст будет передан как есть", charset
        )
        return None
    return lowered


def _explain_connect_error(exc: Exception, cfg: DatabaseConfig) -> str:
    """Понятное объяснение ошибки подключения к Firebird."""
    text = str(exc)
    lowered = text.lower()
    base = "Не удалось подключиться к базе Firebird " + cfg.host + ":" + cfg.database
    if "library" in lowered or "fbclient" in lowered or "cannot load" in lowered:
        return (
            base
            + ". Не найдена клиентская библиотека Firebird (fbclient.dll / libfbclient.so)."
            " Укажите путь в [database] fb_client_library."
        )
    if "password" in lowered or "login" in lowered or "user name" in lowered:
        return base + ". Проверьте [database] user и password. Ответ: " + text
    if "unavailable" in lowered or "connection" in lowered or "connection refused" in lowered:
        return base + ". Сервер недоступен — проверьте host, порт 3050 и сеть. Ответ: " + text
    if "no such file" in lowered or "i/o error" in lowered:
        return base + ". Файл базы не найден — проверьте [database] database. Ответ: " + text
    return base + ". Ответ сервера: " + text


def _explain_db_error(exc: Exception) -> str:
    """Понятное объяснение ошибки записи."""
    text = str(exc)
    lowered = text.lower()
    base = "Ошибка записи в журнал: "
    if "column unknown" in lowered or "unknown column" in lowered:
        return (
            base
            + "в таблице CARD нет ожидаемой колонки — схема базы отличается от той,"
            " под которую писан сервис FastAPI. Ответ: " + text
        )
    if "not null" in lowered:
        return (
            base
            + "база требует значение в колонке, которое не заполнено"
            " (например дата документа). Задайте [journal] doc_date_fallback. Ответ: " + text
        )
    if "string right truncation" in lowered or "arithmetic exception" in lowered:
        return (
            base
            + "значение не помещается в поле таблицы (слишком длинный текст)."
            " Уменьшите [journal] max_sh_contents_length / max_korresp_length /"
            " max_extindex_length. Ответ: " + text
        )
    if "conversion error" in lowered:
        return (
            base
            + "не удалось привести значение к типу колонки (скорее всего дата)."
            " Попробуйте [database] date_format = date. Ответ: " + text
        )
    return base + text
