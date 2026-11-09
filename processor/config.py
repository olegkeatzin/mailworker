"""Настройки службы. Единственный источник — .ini-файл.

Интерфейса у службы нет, поэтому всё, что может понадобиться при развёртывании
(почта, база, ИИ, поля журнала, логи), задаётся в одном файле. Разбор строгий:
опечатка или пустое обязательное поле не должны «тихо» превращаться в работу
с настройками по умолчанию — служба пишет в журнал документов, ошибка стоит дорого.

Относительные пути (файл состояния, архив писем, лог) отсчитываются от каталога,
в котором лежит .ini, а не от текущего каталога процесса: служба запускается и из
планировщика Windows, и из systemd, где рабочий каталог бывает любым.
"""

from __future__ import annotations

import configparser
import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError

# Допустимые значения перечислимых настроек. Проверяем их явно: «чужое» значение
# иначе ведёт себя как «первое попавшееся» и ошибку ищут уже в базе.
PROTOCOLS = ("imap", "pop3")
TLS_MODES = ("none", "ssl", "starttls")
MAIL_MODES = ("unseen", "all", "since_days")
STORAGE_MODES = ("db", "api")
REG_DATE_MODES = ("today", "email_date")
DOC_DATE_FALLBACKS = ("email_date", "reg_date", "today", "none")
DOC_NUMBER_FALLBACKS = ("none", "subject", "regex")
SHEETS_MODES = ("pdf_pages", "attachments", "analysis", "one")
FILTER_MODES = ("ai", "all", "with_attachments")
DATE_FORMATS = ("string", "date")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
UNKNOWN_CORRESPONDENT_MODES = ("model", "none", "skip", "create")
UNKNOWN_THEMA_MODES = ("none", "skip", "create")

#: Запросы к справочникам по умолчанию. Четыре колонки строго по порядку:
#: идентификатор, название, часть номера, признак «показывать».
DEFAULT_CORRESPONDENTS_SQL = (
    "SELECT ID, NAME, NUMBPART, SHOWS FROM KORRESP"
    " WHERE COALESCE(SHOWS, 1) <> 0 ORDER BY NAME"
)
DEFAULT_THEMES_SQL = (
    "SELECT ID, NAME, NUMBPART, SHOWS FROM THEMA"
    " WHERE COALESCE(SHOWS, 1) <> 0 ORDER BY NAME"
)

_TRUE = {"1", "true", "yes", "on", "да"}
_FALSE = {"0", "false", "no", "off", "нет"}


@dataclass(frozen=True)
class MailConfig:
    """Подключение к ящику и режим опроса."""

    protocol: str = "imap"
    host: str = ""
    port: int = 993
    tls: str = "ssl"
    username: str = ""
    password: str = ""
    mailbox: str = "INBOX"
    verify_tls: bool = True
    timeout: int = 60
    mode: str = "unseen"
    #: Окно по дате: N календарных дней, включая сегодня. 0 — без ограничения.
    #: Действует в любом режиме: и для unseen, и для all (включая POP3).
    since_days: int = 0
    mark_seen: bool = True
    delete_after: bool = False
    poll_interval: int = 60
    max_per_cycle: int = 20


@dataclass(frozen=True)
class DatabaseConfig:
    """Подключение к Firebird — те же параметры, что в FastAPI_sender_to_BD/db.py."""

    host: str = ""
    database: str = ""
    user: str = ""
    password: str = ""
    charset: str = "WIN1251"
    fb_client_library: str = ""
    date_format: str = "string"
    sanitize_text: bool = True


@dataclass(frozen=True)
class AiConfig:
    """OpenAI-совместимый эндпоинт (llama.cpp, vLLM, Ollama, OpenRouter и т.п.)."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 120
    temperature: float = 0.2
    max_tokens: int = 8192
    retries: int = 2
    json_mode: bool = True
    disable_thinking: bool = True
    request_extra: dict = field(default_factory=dict)
    multimodal: bool = True
    images_in_analysis: bool = False
    ocr_max_pages: int = 10
    text_max_pages: int = 20
    max_text_chars: int = 60000
    verify_tls: bool = True
    system_prompt_file: Path | None = None
    user_template_file: Path | None = None


@dataclass(frozen=True)
class OcrConfig:
    """Отдельная модель распознавания сканов; пустые поля — берём основную модель."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 120
    concurrency: int = 3


@dataclass(frozen=True)
class JournalConfig:
    """Что и куда писать в журнале."""

    storage: str = "db"
    api_url: str = "http://127.0.0.1:8000"
    api_timeout: int = 30
    card_type: int = 1
    reg_number: str = "новая"
    reg_date: str = "today"
    doc_date_fallback: str = "email_date"
    doc_number_fallback: str = "none"
    sheets_mode: str = "pdf_pages"
    # Длины колонок CARD (VARCHAR). Значения по умолчанию взяты из схемы базы:
    # INTINDEX/EXTINDEX 20, KORRESP 100, SH_CONTENTS 512. Без обрезки Firebird
    # откажет «string right truncation», и письмо не попадёт в журнал.
    max_intindex_length: int = 20
    max_extindex_length: int = 20
    max_korresp_length: int = 100
    max_sh_contents_length: int = 512


@dataclass(frozen=True)
class ClassifiersConfig:
    """Справочники журнала: корреспонденты (KORRESP) и темы (THEMA).

    Оба справочника лежат в той же базе Firebird. Соответствия «почтовый адрес ->
    корреспондент» в базе нет (в KORRESP только название), поэтому адреса
    сопоставляются по необязательному CSV-файлу, а в остальном — по названию.
    """

    enabled: bool = True
    refresh_minutes: int = 10
    correspondents_sql: str = DEFAULT_CORRESPONDENTS_SQL
    themes_sql: str = DEFAULT_THEMES_SQL
    email_map_file: Path | None = None
    match_threshold: float = 0.85
    unknown_correspondent: str = "model"  # model | none | skip | create
    unknown_thema: str = "none"  # none | skip | create
    themes_in_prompt: int = 300
    max_name_length: int = 150


@dataclass(frozen=True)
class ProcessingConfig:
    """Отбор писем: что регистрировать и служебные файлы.

    По умолчанию регистрируются все письма (filter_mode = all): служба рассчитана
    на ящик, куда пересылают уже отобранную корреспонденцию. Решение модели
    (register, уверенность) в этом режиме только пишется в лог.
    """

    filter_mode: str = "all"
    skip_spam: bool = False
    #: Решение модели учитывается только при filter_mode = ai.
    require_register_flag: bool = True
    min_confidence: float = 0.0
    skip_inline_images: bool = True
    min_inline_image_bytes: int = 30000
    retry_errors: bool = True
    state_path: Path = Path("data/state.sqlite")
    archive_dir: Path = Path("data/processed")
    save_eml: bool = True


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = None
    console: bool = True
    max_bytes: int = 5_000_000
    backup_count: int = 5


@dataclass(frozen=True)
class AppConfig:
    """Полные настройки службы."""

    path: Path
    base_dir: Path
    mail: MailConfig
    database: DatabaseConfig
    ai: AiConfig
    ocr: OcrConfig
    journal: JournalConfig
    classifiers: ClassifiersConfig
    processing: ProcessingConfig
    logging: LoggingConfig

    # ------------------------------------------------------------------ разбор

    @classmethod
    def load(cls, path: str | Path, *, require_mail: bool = True) -> "AppConfig":
        """Читает .ini и проверяет значения. Бросает ConfigError со списком причин.

        require_mail=False используется режимом разбора отдельного файла .eml,
        где ни ящик, ни его пароль не нужны.
        """
        ini_path = Path(path).expanduser()
        if not ini_path.is_file():
            raise ConfigError(
                f"Файл настроек не найден: {ini_path}\n"
                f"Скопируйте config.ini.example в config.ini и заполните значения."
            )
        base_dir = ini_path.resolve().parent

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str.lower
        try:
            # utf-8-sig: файл правят в «Блокноте», он любит добавлять BOM.
            with ini_path.open("r", encoding="utf-8-sig") as fh:
                parser.read_file(fh)
        except configparser.Error as exc:
            raise ConfigError(f"{ini_path}: не удалось прочитать .ini — {exc}") from exc
        except UnicodeDecodeError as exc:
            raise ConfigError(
                f"{ini_path}: файл должен быть в кодировке UTF-8 (сейчас не читается: {exc})"
            ) from exc

        r = _Reader(parser, base_dir)
        mail = _read_mail(r, require=require_mail)
        database = _read_database(r)
        ai = _read_ai(r)
        ocr = _read_ocr(r, ai)
        journal = _read_journal(r)
        classifiers = _read_classifiers(r)
        processing = _read_processing(r)
        logging_cfg = _read_logging(r)
        _check_cross(r, mail, database, journal, classifiers)

        if r.errors:
            raise ConfigError(
                f"{ini_path}: проверьте настройки —\n  - " + "\n  - ".join(r.errors)
            )

        return cls(
            path=ini_path.resolve(),
            base_dir=base_dir,
            mail=mail,
            database=database,
            ai=ai,
            ocr=ocr,
            journal=journal,
            classifiers=classifiers,
            processing=processing,
            logging=logging_cfg,
        )


# ---------------------------------------------------------------- разбор секций


def _read_mail(r: "_Reader", *, require: bool = True) -> MailConfig:
    protocol = r.choice("mail", "protocol", "imap", PROTOCOLS)
    tls = r.choice("mail", "tls", "ssl", TLS_MODES)
    # Порт по умолчанию зависит от протокола и способа шифрования: не заставляем
    # администратора помнить, что у POP3S это 995, а у IMAP+STARTTLS — 143.
    default_port = {
        ("imap", "ssl"): 993,
        ("imap", "starttls"): 143,
        ("imap", "none"): 143,
        ("pop3", "ssl"): 995,
        ("pop3", "starttls"): 110,
        ("pop3", "none"): 110,
    }.get((protocol, tls), 993)

    if require:
        host = r.required("mail", "host", "адрес почтового сервера")
        username = r.required("mail", "username", "адрес ящика")
        password = r.required("mail", "password", "пароль ящика")
    else:
        # Разбор отдельного файла .eml: ящик не нужен.
        host = r.text("mail", "host")
        username = r.text("mail", "username")
        password = r.text("mail", "password")
    return MailConfig(
        protocol=protocol,
        host=host,
        port=r.integer("mail", "port", default_port, minimum=1, maximum=65535),
        tls=tls,
        username=username,
        password=password,
        mailbox=r.text("mail", "mailbox", "INBOX"),
        verify_tls=r.boolean("mail", "verify_tls", True),
        timeout=r.integer("mail", "timeout", 60, minimum=5, maximum=3600),
        mode=r.choice("mail", "mode", "unseen", MAIL_MODES),
        since_days=r.integer("mail", "since_days", 0, minimum=0, maximum=3650),
        mark_seen=r.boolean("mail", "mark_seen", True),
        delete_after=r.boolean("mail", "delete_after", False),
        poll_interval=r.integer("mail", "poll_interval", 60, minimum=10, maximum=86400),
        max_per_cycle=r.integer("mail", "max_per_cycle", 20, minimum=1, maximum=500),
    )


def _read_database(r: "_Reader") -> DatabaseConfig:
    return DatabaseConfig(
        host=r.text("database", "host"),
        database=r.text("database", "database"),
        user=r.text("database", "user", "SYSDBA"),
        password=r.text("database", "password"),
        charset=r.text("database", "charset", "WIN1251") or "WIN1251",
        fb_client_library=r.text("database", "fb_client_library"),
        date_format=r.choice("database", "date_format", "string", DATE_FORMATS),
        sanitize_text=r.boolean("database", "sanitize_text", True),
    )


def _read_ai(r: "_Reader") -> AiConfig:
    extra = r.json_object("ai", "request_extra_json")
    return AiConfig(
        base_url=r.required("ai", "base_url", "адрес OpenAI-совместимого эндпоинта"),
        api_key=r.text("ai", "api_key", "not-needed"),
        model=r.required("ai", "model", "имя модели"),
        timeout=r.integer("ai", "timeout", 120, minimum=5, maximum=3600),
        temperature=r.real("ai", "temperature", 0.2, minimum=0.0, maximum=2.0),
        max_tokens=r.integer("ai", "max_tokens", 8192, minimum=256, maximum=131072),
        retries=r.integer("ai", "retries", 2, minimum=0, maximum=10),
        json_mode=r.boolean("ai", "json_mode", True),
        disable_thinking=r.boolean("ai", "disable_thinking", True),
        request_extra=extra,
        multimodal=r.boolean("ai", "multimodal", True),
        images_in_analysis=r.boolean("ai", "images_in_analysis", False),
        ocr_max_pages=r.integer("ai", "ocr_max_pages", 10, minimum=1, maximum=200),
        text_max_pages=r.integer("ai", "text_max_pages", 20, minimum=1, maximum=200),
        max_text_chars=r.integer("ai", "max_text_chars", 60000, minimum=1000, maximum=5_000_000),
        verify_tls=r.boolean("ai", "verify_tls", True),
        system_prompt_file=r.optional_path("ai", "system_prompt_file"),
        user_template_file=r.optional_path("ai", "user_template_file"),
    )


def _read_ocr(r: "_Reader", ai: AiConfig) -> OcrConfig:
    base_url = r.text("ocr", "base_url")
    model = r.text("ocr", "model")
    if base_url and not base_url.startswith(("http://", "https://")):
        r.errors.append("[ocr] base_url: ожидается адрес вида http://host:port/v1")
    return OcrConfig(
        base_url=base_url or ai.base_url,
        api_key=r.text("ocr", "api_key") or ai.api_key,
        model=model or ai.model,
        timeout=r.integer("ocr", "timeout", ai.timeout, minimum=5, maximum=3600),
        concurrency=r.integer("ocr", "concurrency", 3, minimum=1, maximum=16),
    )


def _read_journal(r: "_Reader") -> JournalConfig:
    return JournalConfig(
        storage=r.choice("journal", "storage", "db", STORAGE_MODES),
        api_url=r.text("journal", "api_url", "http://127.0.0.1:8000").rstrip("/"),
        api_timeout=r.integer("journal", "api_timeout", 30, minimum=1, maximum=600),
        card_type=r.integer("journal", "card_type", 1, minimum=0),
        reg_number=r.text("journal", "reg_number", "новая"),
        reg_date=r.choice("journal", "reg_date", "today", REG_DATE_MODES),
        doc_date_fallback=r.choice(
            "journal", "doc_date_fallback", "email_date", DOC_DATE_FALLBACKS
        ),
        doc_number_fallback=r.choice(
            "journal", "doc_number_fallback", "none", DOC_NUMBER_FALLBACKS
        ),
        sheets_mode=r.choice("journal", "sheets_mode", "pdf_pages", SHEETS_MODES),
        max_intindex_length=r.integer("journal", "max_intindex_length", 20, minimum=1, maximum=255),
        max_extindex_length=r.integer("journal", "max_extindex_length", 20, minimum=1, maximum=255),
        max_korresp_length=r.integer("journal", "max_korresp_length", 100, minimum=1, maximum=1000),
        max_sh_contents_length=r.integer(
            "journal", "max_sh_contents_length", 512, minimum=1, maximum=4000
        ),
    )


def _read_classifiers(r: "_Reader") -> ClassifiersConfig:
    email_map = r.text("classifiers", "email_map_file")
    return ClassifiersConfig(
        enabled=r.boolean("classifiers", "enabled", True),
        refresh_minutes=r.integer("classifiers", "refresh_minutes", 10, minimum=0, maximum=1440),
        correspondents_sql=r.text(
            "classifiers", "correspondents_sql", DEFAULT_CORRESPONDENTS_SQL
        )
        or DEFAULT_CORRESPONDENTS_SQL,
        themes_sql=r.text("classifiers", "themes_sql", DEFAULT_THEMES_SQL)
        or DEFAULT_THEMES_SQL,
        email_map_file=r.path("classifiers", "email_map_file", email_map) if email_map else None,
        match_threshold=r.real("classifiers", "match_threshold", 0.85, minimum=0.5, maximum=1.0),
        unknown_correspondent=r.choice(
            "classifiers", "unknown_correspondent", "model", UNKNOWN_CORRESPONDENT_MODES
        ),
        unknown_thema=r.choice("classifiers", "unknown_thema", "none", UNKNOWN_THEMA_MODES),
        themes_in_prompt=r.integer("classifiers", "themes_in_prompt", 300, minimum=0, maximum=5000),
        max_name_length=r.integer("classifiers", "max_name_length", 150, minimum=10, maximum=1000),
    )


def _read_processing(r: "_Reader") -> ProcessingConfig:
    return ProcessingConfig(
        filter_mode=r.choice("processing", "filter_mode", "all", FILTER_MODES),
        skip_spam=r.boolean("processing", "skip_spam", False),
        require_register_flag=r.boolean("processing", "require_register_flag", True),
        min_confidence=r.real("processing", "min_confidence", 0.0, minimum=0.0, maximum=1.0),
        skip_inline_images=r.boolean("processing", "skip_inline_images", True),
        min_inline_image_bytes=r.integer(
            "processing", "min_inline_image_bytes", 30000, minimum=0
        ),
        retry_errors=r.boolean("processing", "retry_errors", True),
        state_path=r.path("processing", "state_path", "data/state.sqlite"),
        archive_dir=r.path("processing", "archive_dir", "data/processed"),
        save_eml=r.boolean("processing", "save_eml", True),
    )


def _read_logging(r: "_Reader") -> LoggingConfig:
    file = r.text("logging", "file")
    return LoggingConfig(
        level=r.choice("logging", "level", "INFO", LOG_LEVELS, upper=True),
        file=r.path("logging", "file", "data/mailworker.log") if file else None,
        console=r.boolean("logging", "console", True),
        max_bytes=r.integer("logging", "max_bytes", 5_000_000, minimum=0),
        backup_count=r.integer("logging", "backup_count", 5, minimum=0, maximum=50),
    )


def _check_cross(
    r: "_Reader",
    mail: MailConfig,
    database: DatabaseConfig,
    journal: JournalConfig,
    classifiers: ClassifiersConfig,
) -> None:
    """Проверки, которым нужны значения из разных секций."""
    if journal.storage == "db":
        if not database.host:
            r.errors.append("[database] host: не задано — нужен для записи в журнал (storage = db)")
        if not database.database:
            r.errors.append(
                "[database] database: не задано — путь к файлу базы Firebird (storage = db)"
            )
        if not database.user:
            r.errors.append("[database] user: не задано — пользователь Firebird")
    else:
        if not journal.api_url.startswith(("http://", "https://")):
            r.errors.append(
                "[journal] api_url: ожидается адрес вида http://host:8000 (storage = api)"
            )
    if mail.protocol == "imap" and mail.delete_after:
        r.errors.append(
            "[mail] delete_after: удаление писем поддерживается только для POP3 "
            "(для IMAP используйте mark_seen)"
        )
    if mail.mode == "since_days" and mail.since_days < 1:
        r.errors.append(
            "[mail] mode = since_days требует since_days ≥ 1. Окно задаётся "
            "параметром since_days в любом режиме: укажите mode = unseen или all "
            "и нужное значение since_days."
        )
    if classifiers.enabled:
        # Справочники KORRESP и THEMA лежат в той же базе, что и журнал:
        # без подключения к Firebird их не прочитать.
        if not database.host or not database.database:
            r.errors.append(
                "[classifiers] enabled = true: справочники читаются из Firebird —"
                " задайте [database] host и database или выключите [classifiers] enabled"
            )
        if not classifiers.correspondents_sql or not classifiers.themes_sql:
            r.errors.append(
                "[classifiers] correspondents_sql и themes_sql не могут быть пустыми"
            )


# ---------------------------------------------------------------------- чтение


class _Reader:
    """Обёртка над configparser: значения + накопленные ошибки.

    Ошибки копим, а не падаем на первой: администратор должен увидеть весь список
    сразу, а не чинить файл по одной строке за запуск.
    """

    def __init__(self, parser: configparser.ConfigParser, base_dir: Path) -> None:
        self.parser = parser
        self.base_dir = base_dir
        self.errors: list[str] = []

    # ---------------------------------------------------------------- базовое

    def raw(self, section: str, key: str) -> str | None:
        if not self.parser.has_section(section):
            return None
        if not self.parser.has_option(section, key):
            return None
        return self.parser.get(section, key).strip()

    def text(self, section: str, key: str, default: str = "") -> str:
        value = self.raw(section, key)
        return default if value is None else value

    def required(self, section: str, key: str, what: str) -> str:
        value = self.raw(section, key)
        if not value:
            self.errors.append(f"[{section}] {key}: не задано — {what}")
            return ""
        return value

    def integer(
        self,
        section: str,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = self.raw(section, key)
        if value is None or value == "":
            return default
        try:
            number = int(value)
        except ValueError:
            self.errors.append(f"[{section}] {key}: «{value}» — ожидается целое число")
            return default
        if minimum is not None and number < minimum:
            self.errors.append(f"[{section}] {key}: {number} — минимум {minimum}")
            return default
        if maximum is not None and number > maximum:
            self.errors.append(f"[{section}] {key}: {number} — максимум {maximum}")
            return default
        return number

    def real(
        self,
        section: str,
        key: str,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        value = self.raw(section, key)
        if value is None or value == "":
            return default
        try:
            # В русской локали десятичный разделитель в файлах иногда ставят запятой.
            number = float(value.replace(",", "."))
        except ValueError:
            self.errors.append(f"[{section}] {key}: «{value}» — ожидается число")
            return default
        if minimum is not None and number < minimum:
            self.errors.append(f"[{section}] {key}: {number} — минимум {minimum}")
            return default
        if maximum is not None and number > maximum:
            self.errors.append(f"[{section}] {key}: {number} — максимум {maximum}")
            return default
        return number

    def boolean(self, section: str, key: str, default: bool) -> bool:
        value = self.raw(section, key)
        if value is None or value == "":
            return default
        lowered = value.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        self.errors.append(
            f"[{section}] {key}: «{value}» — ожидается true/false (можно да/нет)"
        )
        return default

    def choice(
        self,
        section: str,
        key: str,
        default: str,
        allowed: tuple[str, ...],
        *,
        upper: bool = False,
    ) -> str:
        value = self.raw(section, key)
        if value is None or value == "":
            return default
        candidate = value.upper() if upper else value.lower()
        if candidate not in allowed:
            self.errors.append(
                f"[{section}] {key}: «{value}» — допустимо: " + ", ".join(allowed)
            )
            return default
        return candidate

    def json_object(self, section: str, key: str) -> dict:
        value = self.raw(section, key)
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            self.errors.append(f"[{section}] {key}: некорректный JSON — {exc}")
            return {}
        if not isinstance(parsed, dict):
            self.errors.append(f"[{section}] {key}: ожидается JSON-объект {{...}}")
            return {}
        return parsed

    def path(self, section: str, key: str, default: str) -> Path:
        """Путь из настройки; относительный считается от каталога .ini."""
        value = self.raw(section, key) or default
        return self._resolve(value)

    def optional_path(self, section: str, key: str) -> Path | None:
        """Необязательный путь: пустое значение — настройки нет."""
        value = self.raw(section, key)
        return self._resolve(value) if value else None

    def _resolve(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.base_dir / candidate
        return candidate
