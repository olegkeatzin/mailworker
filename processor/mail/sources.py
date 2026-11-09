"""Получение писем из ящика: IMAP и POP3.

Своими руками на imaplib/poplib, без сторонних клиентов: служба должна ставиться
на машину организации минимальным набором зависимостей.

Общие правила:
  * письмо забирается целиком (BODY.PEEK) и НЕ помечается прочитанным до того,
    как обработано — иначе сбой ИИ или базы молча съедал бы письмо;
  * флаг «прочитано» (IMAP) или удаление (POP3) ставится только после того,
    как письмо зарегистрировано или осознанно пропущено;
  * удаление писем выключено по умолчанию: служба пишет в журнал документов,
    терять корреспонденцию из-за ошибки настройки недопустимо.
"""

from __future__ import annotations

import datetime as dt
import email.policy
import email.utils
import imaplib
import logging
import poplib
import ssl
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from typing import Protocol

from ..config import MailConfig
from ..errors import MailSourceError

logger = logging.getLogger(__name__)

#: Класс ошибки imaplib фиксируем при импорте: в тестах модуль imaplib
#: подменяется, и обращение через imaplib.IMAP4.error тогда уже не работает.
_IMAP_ERROR = imaplib.IMAP4.error

#: Сколько писем за один проход забирать максимум (защита от первого запуска
#: на ящике с тысячами писем — служба не должна «перемолоть» весь архив разом).
DEFAULT_BATCH = 20


@dataclass
class FetchedMessage:
    """Сырое письмо из ящика."""

    uid: str
    raw: bytes


class MailSource(Protocol):
    """Интерфейс источника писем — нужен для тестов и для двух протоколов."""

    def connect(self) -> None: ...

    def fetch(self, limit: int = DEFAULT_BATCH) -> list[FetchedMessage]: ...

    def mark_processed(self, uid: str) -> None: ...

    def close(self) -> None: ...


def build_source(cfg: MailConfig) -> MailSource:
    """Создаёт источник по протоколу из настроек."""
    if cfg.protocol == "imap":
        return ImapMailSource(cfg)
    if cfg.protocol == "pop3":
        return Pop3MailSource(cfg)
    raise MailSourceError("Неизвестный протокол: " + cfg.protocol)


def _ssl_context(verify: bool) -> ssl.SSLContext:
    """Контекст TLS; verify_tls=false — для внутренних серверов с самоподписанным."""
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


# ----------------------------------------------------------------------- IMAP


class ImapMailSource:
    """Источник писем по IMAP (в том числе с STARTTLS)."""

    def __init__(self, cfg: MailConfig) -> None:
        self.cfg = cfg
        self._conn: imaplib.IMAP4 | None = None

    def connect(self) -> None:
        cfg = self.cfg
        context = _ssl_context(cfg.verify_tls)
        try:
            if cfg.tls == "ssl":
                conn = imaplib.IMAP4_SSL(
                    cfg.host, cfg.port, ssl_context=context, timeout=cfg.timeout
                )
            else:
                conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=cfg.timeout)
                if cfg.tls == "starttls":
                    conn.starttls(ssl_context=context)
            conn.login(cfg.username, cfg.password)
            # readonly, если письма не помечаются прочитанными: так ящик не меняется.
            status, _data = conn.select(cfg.mailbox, readonly=not cfg.mark_seen)
            if status != "OK":
                raise MailSourceError(
                    "Папка «"
                    + cfg.mailbox
                    + "» не открылась на сервере IMAP "
                    + cfg.host
                    + ". Проверьте [mail] mailbox (обычно INBOX)."
                )
        except _IMAP_ERROR as exc:
            raise MailSourceError(_explain_imap_error(exc, cfg)) from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailSourceError(
                "Не удалось соединиться с IMAP-сервером "
                + cfg.host
                + ":"
                + str(cfg.port)
                + ": "
                + str(exc)
            ) from exc
        self._conn = conn
        logger.info(
            "Подключено к IMAP %s:%s, папка «%s»", cfg.host, cfg.port, cfg.mailbox
        )

    def fetch(self, limit: int = DEFAULT_BATCH) -> list[FetchedMessage]:
        conn = self._require_connection()
        criteria = self._search_criteria()
        try:
            status, data = conn.uid("SEARCH", None, *criteria)
        except _IMAP_ERROR as exc:
            raise MailSourceError(
                "IMAP не выполнил поиск писем (" + " ".join(criteria) + "): " + str(exc)
            ) from exc
        if status != "OK":
            raise MailSourceError("IMAP вернул статус " + str(status) + " на поиск писем")

        uids = (data[0] or b"").split() if data else []
        # Берём последние: uid растёт вместе с порядком поступления.
        selected = uids[-limit:]
        window = self.window_date()
        logger.info(
            "В папке «%s» найдено писем: %s, к обработке: %s%s",
            self.cfg.mailbox,
            len(uids),
            len(selected),
            "" if window is None else ", окно с " + window.strftime("%d.%m.%Y"),
        )

        messages: list[FetchedMessage] = []
        for uid in selected:
            messages.append(FetchedMessage(uid=uid.decode("ascii", "replace"), raw=self._fetch_one(uid)))
        return messages

    def _fetch_one(self, uid: bytes) -> bytes:
        conn = self._require_connection()
        try:
            status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        except _IMAP_ERROR as exc:
            raise MailSourceError(
                "IMAP не отдал письмо UID " + uid.decode("ascii", "replace") + ": " + str(exc)
            ) from exc
        if status != "OK" or not data:
            raise MailSourceError(
                "IMAP вернул пустой ответ на письмо UID " + uid.decode("ascii", "replace")
            )
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                return item[1]
        raise MailSourceError(
            "Не удалось прочитать тело письма UID " + uid.decode("ascii", "replace")
        )

    def count(self) -> int:
        """Сколько писем попадает под условие поиска (для --check, без скачивания)."""
        conn = self._require_connection()
        status, data = conn.uid("SEARCH", None, *self._search_criteria())
        if status != "OK" or not data:
            return 0
        return len((data[0] or b"").split())

    def mark_processed(self, uid: str) -> None:
        """Ставит флаг «прочитано» — только после записи в журнал."""
        if not self.cfg.mark_seen or not uid:
            return
        conn = self._require_connection()
        try:
            conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        except _IMAP_ERROR as exc:
            # Не страшно: письмо уже в журнале, состояние дедупликации защищает
            # от повторной записи при следующем опросе.
            logger.warning("Не удалось пометить письмо UID %s прочитанным: %s", uid, exc)

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            try:
                self._conn.close()
            except _IMAP_ERROR:
                pass
            self._conn.logout()
        except Exception:  # noqa: BLE001 - закрытие не должно падать
            pass
        finally:
            self._conn = None

    # ---------------------------------------------------------------- внутреннее

    def _search_criteria(self) -> tuple[str, ...]:
        """Критерии IMAP: флаг по режиму плюс окно по дате, если оно задано.

        since_days действует независимо от режима: без этого «since_days = 1»
        при mode = unseen (или all) молча игнорировался, и служба читала всю
        папку — именно так и происходит с POP3, где поиска по дате нет вовсе.
        """
        criteria: list[str] = []
        if self.cfg.mode == "unseen":
            criteria.append("UNSEEN")
        elif self.cfg.mode == "all":
            criteria.append("ALL")
        # mode = since_days — устаревший алиас «все письма + окно»: флага нет.
        window = self.window_date()
        if window is not None:
            criteria.extend(["SINCE", _imap_date(window)])
        if not criteria:
            criteria.append("ALL")
        return tuple(criteria)

    def window_date(self) -> dt.date | None:
        """Первый день окна по дате; None — ограничения нет.

        IMAP SINCE включает в результат весь указанный день, поэтому «за N дней»
        — это today − (N−1): since_days = 1 значит «только сегодня».
        """
        if self.cfg.since_days <= 0:
            return None
        days_back = max(0, self.cfg.since_days - 1)
        return dt.date.today() - dt.timedelta(days=days_back)

    def _require_connection(self) -> imaplib.IMAP4:
        if self._conn is None:
            raise MailSourceError("Нет соединения с IMAP: сначала вызовите connect()")
        return self._conn


#: Месяцы для IMAP-даты. strftime("%b") берёт названия из локали процесса,
#: а протокол IMAP понимает только английские: на русской Windows запрос
#: SINCE 05-ноя-2026 сервер отклонит.
_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _imap_date(value: dt.date) -> str:
    """Дата в формате IMAP: 05-Nov-2026."""
    return f"{value.day:02d}-{_IMAP_MONTHS[value.month - 1]}-{value.year}"


def _explain_imap_error(exc: Exception, cfg: MailConfig) -> str:
    text = str(exc)
    lowered = text.lower()
    target = "IMAP " + cfg.host + ":" + str(cfg.port) + " (" + cfg.username + ")"
    if "authenticationfailed" in lowered or "auth" in lowered or "login" in lowered:
        return (
            "Сервер " + target + " не принял логин или пароль. Проверьте"
            " [mail] username и password. Ответ: " + text
        )
    if "unknown mailbox" in lowered or "no such mailbox" in lowered or "select" in lowered:
        return (
            "Папка «" + cfg.mailbox + "» не найдена в ящике. Проверьте [mail] mailbox"
            " (например INBOX). Ответ: " + text
        )
    return "Ошибка IMAP " + target + ": " + text


# ----------------------------------------------------------------------- POP3


class Pop3MailSource:
    """Источник писем по POP3 (в том числе POP3S и STLS)."""

    def __init__(self, cfg: MailConfig) -> None:
        self.cfg = cfg
        self._conn: poplib.POP3 | None = None
        self._uid_by_number: dict[int, str] = {}
        self._number_by_uid: dict[str, int] = {}
        self._pending_delete: set[int] = set()
        #: Сервер умеет TOP (заголовки без тела). Проверяется на первом письме.
        self._top_supported = True

    def connect(self) -> None:
        cfg = self.cfg
        context = _ssl_context(cfg.verify_tls)
        try:
            if cfg.tls == "ssl":
                conn = poplib.POP3_SSL(cfg.host, cfg.port, context=context, timeout=cfg.timeout)
            else:
                conn = poplib.POP3(cfg.host, cfg.port, timeout=cfg.timeout)
                if cfg.tls == "starttls":
                    conn.stls(context=context)
            conn.user(cfg.username)
            conn.pass_(cfg.password)
            self._load_uidl(conn)
        except poplib.error_proto as exc:
            raise MailSourceError(_explain_pop3_error(exc, cfg)) from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailSourceError(
                "Не удалось соединиться с POP3-сервером "
                + cfg.host
                + ":"
                + str(cfg.port)
                + ": "
                + str(exc)
            ) from exc
        self._conn = conn
        cutoff = self._since_cutoff()
        if cutoff is not None:
            # POP3 не умеет искать по дате, поэтому окно применяем сами:
            # смотрим заголовок Date и тело старых писем не скачиваем.
            logger.info(
                "POP3: окно — последние %s (с %s), фильтр по заголовку Date.",
                _days_text(cfg.since_days),
                cutoff.strftime("%d.%m.%Y"),
            )
        logger.info(
            "Подключено к POP3 %s:%s, писем в ящике: %s",
            cfg.host,
            cfg.port,
            len(self._uid_by_number),
        )

    def fetch(self, limit: int = DEFAULT_BATCH) -> list[FetchedMessage]:
        """Забирает письма; старые отбрасываются по заголовку Date.

        Окно задаётся параметром since_days и действует в любом режиме.
        Протокол POP3 не умеет искать по дате, поэтому фильтр применяется здесь:
        заголовки письма читаются командой TOP (без тела), и если письмо старше
        окна — оно даже не скачивается. Если сервер TOP не поддерживает, письмо
        скачивается целиком, но в обработку не попадает.
        """
        conn = self._require_connection()
        if not self._uid_by_number:
            self._load_uidl(conn)
        numbers = sorted(self._uid_by_number)[-limit:]
        cutoff = self._since_cutoff()
        logger.info(
            "Проверяем писем: %s из %s%s",
            len(numbers),
            len(self._uid_by_number),
            "" if cutoff is None else ", окно с " + cutoff.strftime("%d.%m.%Y"),
        )

        messages: list[FetchedMessage] = []
        skipped = 0
        for number in numbers:
            uid = self._uid_by_number.get(number, str(number))
            day: dt.date | None = None
            if cutoff is not None:
                day = self._top_day(number)
                if day is not None and day < cutoff:
                    skipped += 1
                    continue

            raw = self._retr(number)

            if cutoff is not None and day is None:
                # TOP не поддержан или в заголовках нет Date — смотрим письмо.
                day = _message_day(raw)
                if day is not None and day < cutoff:
                    skipped += 1
                    continue

            messages.append(FetchedMessage(uid=uid, raw=raw))

        if skipped:
            logger.info(
                "Пропущено писем старше %s (окно since_days = %s): %s",
                cutoff.strftime("%d.%m.%Y") if cutoff else "",
                _days_text(self.cfg.since_days),
                skipped,
            )
        logger.info("К обработке: %s", len(messages))
        return messages

    def _since_cutoff(self) -> dt.date | None:
        """Первый день окна: N календарных дней, включая сегодня; None — окна нет."""
        if self.cfg.since_days <= 0:
            return None
        return dt.date.today() - dt.timedelta(days=max(0, self.cfg.since_days - 1))

    def _top_day(self, number: int) -> dt.date | None:
        """Дата письма из заголовков (TOP), без скачивания тела."""
        if not self._top_supported:
            return None
        conn = self._require_connection()
        try:
            _response, lines, _octets = conn.top(number, 0)
        except poplib.error_proto:
            self._top_supported = False
            logger.debug("POP3 не поддерживает TOP — дату берём из скачанного письма")
            return None
        return _message_day(b"\r\n".join(lines))

    def _retr(self, number: int) -> bytes:
        conn = self._require_connection()
        try:
            _response, lines, _octets = conn.retr(number)
        except poplib.error_proto as exc:
            raise MailSourceError(
                "POP3 не отдал письмо №" + str(number) + ": " + str(exc)
            ) from exc
        return b"\r\n".join(lines)

    def count(self) -> int:
        """Сколько писем в ящике (для --check, без скачивания)."""
        conn = self._require_connection()
        if not self._uid_by_number:
            self._load_uidl(conn)
        return len(self._uid_by_number)

    def mark_processed(self, uid: str) -> None:
        """Удаляет письмо с сервера — только если это включено в настройках."""
        if not self.cfg.delete_after:
            return
        number = self._number_by_uid.get(uid)
        if number is None:
            return
        conn = self._require_connection()
        try:
            conn.dele(number)
            self._pending_delete.add(number)
            logger.info("Письмо №%s помечено к удалению с сервера", number)
        except poplib.error_proto as exc:
            logger.warning("Не удалось удалить письмо №%s: %s", number, exc)

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            # QUIT подтверждает удаления, накопленные DELE; CLOSE — просто рвёт сессию.
            if self._pending_delete:
                self._conn.quit()
            else:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._conn = None

    # ---------------------------------------------------------------- внутреннее

    def _load_uidl(self, conn: poplib.POP3) -> None:
        """Список UIDL: устойчивый идентификатор письма для дедупликации.

        UIDL поддерживают не все серверы; тогда роль идентификатора играет номер
        письма, а от повторной записи защищает Message-ID в ключе состояния.
        """
        self._uid_by_number.clear()
        self._number_by_uid.clear()
        try:
            _response, listing, _octets = conn.uidl()
        except poplib.error_proto:
            logger.debug("Сервер POP3 не поддерживает UIDL — используем номера писем")
            try:
                _response, listing, _octets = conn.list()
            except poplib.error_proto as exc:
                raise MailSourceError("POP3 не вернул список писем: " + str(exc)) from exc
            for line in listing:
                parts = line.decode("ascii", "replace").split()
                if parts:
                    number = int(parts[0])
                    self._uid_by_number[number] = "pop3-" + str(number)
                    self._number_by_uid[self._uid_by_number[number]] = number
            return

        for line in listing:
            parts = line.decode("ascii", "replace").split()
            if len(parts) < 2:
                continue
            number = int(parts[0])
            uid = parts[1]
            self._uid_by_number[number] = uid
            self._number_by_uid[uid] = number

    def _require_connection(self) -> poplib.POP3:
        if self._conn is None:
            raise MailSourceError("Нет соединения с POP3: сначала вызовите connect()")
        return self._conn


def _days_text(days: int) -> str:
    """«1 день», «2 дня», «5 дней» — чтобы в логе было по-русски."""
    value = abs(int(days))
    tail = value % 100
    if 11 <= tail <= 14:
        return str(value) + " дней"
    last = value % 10
    if last == 1:
        return str(value) + " день"
    if 2 <= last <= 4:
        return str(value) + " дня"
    return str(value) + " дней"


def _message_day(raw: bytes) -> dt.date | None:
    """Дата письма из заголовка Date, приведённая к местному дню.

    None — даты нет или она не разбирается. Такое письмо считается свежим:
    лучше обработать лишнее, чем молча пропустить корреспонденцию.
    """
    try:
        message = BytesHeaderParser(policy=email.policy.default).parsebytes(raw or b"")
        value = message.get("Date")
        if value is None:
            return None
        parsed = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.date()


def _explain_pop3_error(exc: Exception, cfg: MailConfig) -> str:
    text = str(exc)
    lowered = text.lower()
    target = "POP3 " + cfg.host + ":" + str(cfg.port) + " (" + cfg.username + ")"
    if "auth" in lowered or "login" in lowered or "invalid" in lowered:
        return (
            "Сервер " + target + " не принял логин или пароль."
            " Проверьте [mail] username и password. Ответ: " + text
        )
    return "Ошибка POP3 " + target + ": " + text
