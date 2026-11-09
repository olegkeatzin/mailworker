"""Командная строка службы. Интерфейса нет — только запуск и проверки.

Режимы:
  python main.py                      опрос ящика по расписанию (рабочий режим)
  python main.py --once               один проход и выход (удобно для планировщика)
  python main.py --check              проверка настроек, почты, базы и модели
  python main.py --eml file.eml       разбор файла письма без записи в журнал
  python main.py --eml file.eml --write   разбор с записью в журнал
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import signal
import sys
from pathlib import Path

from . import __version__
from .ai.client import AiClient
from .classifiers import ClassifierService
from .config import AppConfig
from .errors import ConfigError, ProcessorError
from .journal import ApiJournal, FirebirdDictionaries, FirebirdJournal
from .logging_setup import setup_logging
from .mail.parser import parse_eml
from .mail.sources import build_source
from .models import CardData, ProcessResult
from .pipeline import EmailProcessor
from .service import MailWorkerService
from .state import ProcessedState

logger = logging.getLogger(__name__)

PROJECT_NAME = "MailWorker: автоматическая обработка ящика в журнал учёта"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailworker",
        description=PROJECT_NAME,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python main.py --config config.ini --check\n"
            "  python main.py --config config.ini --once\n"
            "  python main.py --config config.ini --eml «письмо.eml»\n"
            "  python main.py --config config.ini --eml «письмо.eml» --write\n"
        ),
    )
    parser.add_argument(
        "--config",
        default="config.ini",
        help="путь к файлу настроек .ini (по умолчанию config.ini рядом со службой)",
    )
    parser.add_argument("--once", action="store_true", help="сделать один проход и выйти")
    parser.add_argument(
        "--check", action="store_true", help="проверить настройки, почту, журнал и модель"
    )
    parser.add_argument(
        "--eml", metavar="ФАЙЛ", help="разобрать отдельный файл письма (.eml)"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="с --eml: действительно записать в журнал (без флага — только показать)",
    )
    parser.add_argument("--json", action="store_true", help="вывести результат в JSON")
    parser.add_argument("--log-level", help="уровень логирования: DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Кириллица в выводе не должна ронять службу на консоли с чужой кодировкой.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)

    try:
        # Разбор файла .eml и проверка настроек не требуют заполненного ящика:
        # --check должен показать все узлы сразу, а не остановиться на первом.
        cfg = AppConfig.load(args.config, require_mail=not (args.eml or args.check))
    except ConfigError as exc:
        print("Ошибка настроек:\n" + str(exc), file=sys.stderr)
        return 2

    setup_logging(cfg.logging, args.log_level)

    try:
        if args.check:
            return run_check(cfg)
        if args.eml:
            return run_eml(cfg, Path(args.eml), write=args.write, as_json=args.json)
        return run_service(cfg, once=args.once)
    except KeyboardInterrupt:
        print("Работа прервана пользователем", file=sys.stderr)
        return 130
    except ProcessorError as exc:
        logger.error("%s", exc)
        print("Ошибка: " + str(exc), file=sys.stderr)
        return 1


# ------------------------------------------------------------------- запуск


def run_service(cfg: AppConfig, *, once: bool) -> int:
    """Рабочий режим: опрос ящика (один проход или бесконечно)."""
    service = MailWorkerService(cfg, build_processor(cfg))
    if once:
        stats = service.run_once()
        print("Проход: " + stats.as_text())
        return 0 if not stats.source_error and not stats.errors else 1

    def handle_signal(signum, _frame) -> None:
        logger.info("Получен сигнал %s — завершаем работу", signum)
        service.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError):  # not main thread / no signal on platform
            pass

    service.run_forever()
    return 0


def build_processor(cfg: AppConfig) -> EmailProcessor:
    """Собирает компоненты службы: модель, журнал, справочники, состояние."""
    return EmailProcessor(
        cfg,
        AiClient(cfg.ai),
        make_journal(cfg),
        ProcessedState(cfg.processing.state_path),
        classifiers=make_classifiers(cfg),
    )


def make_journal(cfg: AppConfig):
    """Реализация записи журнала по настройке [journal] storage."""
    if cfg.journal.storage == "api":
        return ApiJournal(cfg.journal)
    return FirebirdJournal(cfg.database, cfg.journal)


def make_classifiers(cfg: AppConfig) -> ClassifierService:
    """Справочники KORRESP и THEMA из базы журнала."""
    loader = (
        FirebirdDictionaries(cfg.database, cfg.classifiers)
        if cfg.classifiers.enabled
        else None
    )
    return ClassifierService(cfg.classifiers, loader)


# ---------------------------------------------------------------- разбор .eml


def run_eml(cfg: AppConfig, path: Path, *, write: bool, as_json: bool) -> int:
    """Разбор отдельного письма: проверка промпта и полей без опроса ящика."""
    if not path.is_file():
        print("Файл не найден: " + str(path), file=sys.stderr)
        return 2

    raw = path.read_bytes()
    mail = parse_eml(raw, uid=path.name, folder="файл")
    processor = build_processor(cfg)
    result = processor.process_mail(mail, dry_run=not write)

    if as_json:
        print(json.dumps(_result_to_json(result), ensure_ascii=False, indent=2))
    else:
        _print_eml_report(mail, result, cfg=cfg, write=write)

    return 0 if result.status != "error" else 1


def _print_eml_report(
    mail, result: ProcessResult, *, cfg: AppConfig, write: bool
) -> None:
    print("Письмо: «" + mail.subject + "»")
    print("От: " + (mail.from_display or "неизвестен"))
    print("Дата: " + (mail.date_text or "не указана"))
    print("Вложений: " + str(len(mail.attachments)))
    for attachment in mail.attachments:
        print("  - " + attachment.filename + " (" + attachment.mime_type + ", " + str(attachment.size) + " байт)")

    if result.status == "error":
        print()
        print("ОШИБКА: " + result.error)
        return

    analysis = result.analysis
    if analysis is not None:
        print()
        print("Разбор модели:")
        if analysis.register is None and analysis.confidence is None:
            print(
                "  решение о регистрации модель не вычисляет"
                " (filter_mode = " + cfg.processing.filter_mode + ")"
            )
        else:
            print("  регистрировать: " + _yes_no(analysis.register) + _reason(analysis))
            print("  уверенность:    " + _confidence(analysis.confidence))
        print("  вид документа:  " + (analysis.document_name or "—"))
        print("  номер:          " + (analysis.doc_number or "—"))
        print("  дата документа: " + (analysis.doc_date.isoformat() if analysis.doc_date else "—"))
        print("  листов:         " + (str(analysis.sheets_count) if analysis.sheets_count else "—"))
        print("  тема (модель):  " + (analysis.thema_name or "—"))

    if result.status == "skipped":
        print()
        print("Письмо НЕ записано: " + result.reason)
        return

    card = result.card
    if card is not None:
        print()
        print("Запись журнала (таблица CARD):")
        for name, value in _card_fields(card):
            print("  " + name.ljust(12) + " = " + value)

    print()
    if result.status == "registered":
        print("Запись добавлена в журнал, id=" + str(result.inserted.card_id if result.inserted else "?"))
    else:
        print("Запись НЕ добавлена: пробный прогон.")
        if not write:
            print("Чтобы записать в журнал, добавьте --write.")


def _card_fields(card: CardData) -> list[tuple[str, str]]:
    return [
        ("CARD_TYPE", str(card.card_type)),
        ("INTINDEX", card.intindex),
        ("INTDATE", card.intdate.strftime("%d.%m.%Y")),
        ("EXTINDEX", card.extindex or ""),
        ("EXTDATE", card.extdate.strftime("%d.%m.%Y") if card.extdate else ""),
        ("SH_CONTENTS", card.sh_contents),
        ("PAGES_DOC", str(card.pages_doc)),
        ("KORRESP", card.korresp),
        ("KORRESPID", str(card.korrespid)),
        ("THEMA", (str(card.thema) + (" — " + card.thema_name if card.thema_name else ""))),
    ]


def _result_to_json(result: ProcessResult) -> dict:
    payload: dict = {
        "status": result.status,
        "message_id": result.message_id,
        "reason": result.reason,
        "error": result.error,
        "seconds": round(result.seconds, 2),
    }
    if result.analysis is not None:
        analysis = result.analysis
        payload["analysis"] = {
            "register": analysis.register,
            "confidence": analysis.confidence,
            "summary": analysis.summary,
            "document_name": analysis.document_name,
            "sender": analysis.sender,
            "doc_number": analysis.doc_number,
            "doc_date": analysis.doc_date.isoformat() if analysis.doc_date else None,
            "sheets_count": analysis.sheets_count,
            "thema": analysis.thema_name,
            "category": analysis.category,
        }
    if result.card is not None:
        card = result.card
        payload["card"] = {
            "CARD_TYPE": card.card_type,
            "INTINDEX": card.intindex,
            "INTDATE": card.intdate.strftime("%d.%m.%Y"),
            "EXTINDEX": card.extindex,
            "EXTDATE": card.extdate.strftime("%d.%m.%Y") if card.extdate else None,
            "SH_CONTENTS": card.sh_contents,
            "PAGES_DOC": card.pages_doc,
            "KORRESP": card.korresp,
            "KORRESPID": card.korrespid,
            "THEMA": card.thema,
            "THEMA_NAME": card.thema_name,
        }
    if result.inserted is not None:
        payload["inserted"] = {
            "card_id": result.inserted.card_id,
            "reg_number": result.inserted.reg_number,
            "storage": result.inserted.storage,
        }
    return payload


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "не указано"
    return "да" if value else "нет"


def _reason(analysis) -> str:
    return " (" + analysis.register_reason + ")" if analysis.register_reason else ""


def _confidence(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


# -------------------------------------------------------------------- проверки


def run_check(cfg: AppConfig) -> int:
    """Проверяет всё, что нужно для работы, и печатает отчёт."""
    print(PROJECT_NAME)
    print("Файл настроек: " + str(cfg.path))
    print(
        "Запись журнала: "
        + ("Firebird (таблица CARD)" if cfg.journal.storage == "db" else "HTTP API /cards")
    )
    print()

    results = [
        _check_state(cfg),
        _check_ai(cfg),
        _check_journal(cfg),
        _check_classifiers(cfg),
        _check_mail(cfg),
    ]

    print()
    if all(results):
        print("ИТОГ: всё готово к работе.")
        return 0
    print("ИТОГ: есть ошибки — исправьте их и повторите проверку.")
    return 1


def _check_state(cfg: AppConfig) -> bool:
    try:
        state = ProcessedState(cfg.processing.state_path)
        counts = state.counts()
    except Exception as exc:  # noqa: BLE001
        print("Состояние обработки: ОШИБКА — " + str(exc))
        return False
    total = sum(counts.values())
    detail = ", ".join(name + ": " + str(count) for name, count in sorted(counts.items()))
    print(
        "Состояние обработки: OK — "
        + str(cfg.processing.state_path)
        + " (записей: "
        + str(total)
        + (", " + detail if detail else "")
        + ")"
    )
    return True


def _check_ai(cfg: AppConfig) -> bool:
    client = AiClient(cfg.ai)
    try:
        models = client.list_models()
    except ProcessorError as exc:
        print("Модель: ОШИБКА — " + str(exc))
        return False
    print("Модель: OK — " + cfg.ai.base_url + ", моделей на сервере: " + str(len(models)))
    if models and cfg.ai.model not in models:
        print(
            "  ВНИМАНИЕ: модели «"
            + cfg.ai.model
            + "» нет в списке. Доступные: "
            + ", ".join(models[:10])
        )
    if cfg.ocr.model and cfg.ocr.model != cfg.ai.model:
        print("  OCR-модель: " + cfg.ocr.model + " (" + cfg.ocr.base_url + ")")
    return True


def _check_journal(cfg: AppConfig) -> bool:
    journal = make_journal(cfg)
    try:
        target = journal.ping()
    except ProcessorError as exc:
        print("Журнал: ОШИБКА — " + str(exc))
        return False
    finally:
        try:
            journal.close()
        except Exception:  # noqa: BLE001
            pass
    print("Журнал: OK — " + str(target))
    return True


def _check_classifiers(cfg: AppConfig) -> bool:
    """Проверяет, что справочники читаются и не пусты."""
    if not cfg.classifiers.enabled:
        print("Справочники: выключены ([classifiers] enabled = false)")
        return True
    try:
        service = make_classifiers(cfg)
        service.ensure_loaded(force=True)
    except ProcessorError as exc:
        print("Справочники: ОШИБКА — " + str(exc))
        return False
    except Exception as exc:  # noqa: BLE001
        print("Справочники: ОШИБКА — " + str(exc))
        return False

    print(
        "Справочники (KORRESP и THEMA из базы): OK — KORRESP: "
        + str(len(service.correspondents))
        + ", THEMA: "
        + str(len(service.themes))
        + (", тем передаётся модели: " + str(len(service.theme_names_for_prompt())))
    )
    if not service.correspondents:
        print("  ВНИМАНИЕ: справочник корреспондентов пуст — сопоставлять не с чем")
    if not service.themes:
        print("  ВНИМАНИЕ: справочник тем пуст — тема всегда будет пустой")
    return True


def _check_mail(cfg: AppConfig) -> bool:
    if not cfg.mail.host or not cfg.mail.username or not cfg.mail.password:
        print("Почта: ОШИБКА — не заданы [mail] host, username и password")
        return False
    source = build_source(cfg.mail)
    try:
        source.connect()
        count = source.count() if hasattr(source, "count") else None
    except ProcessorError as exc:
        print("Почта: ОШИБКА — " + str(exc))
        return False
    finally:
        source.close()
    if cfg.mail.since_days > 0:
        since = dt.date.today() - dt.timedelta(days=max(0, cfg.mail.since_days - 1))
        window = (
            ", окно с "
            + since.strftime("%d.%m.%Y")
            + " ("
            + str(cfg.mail.since_days)
            + " дн.)"
        )
    else:
        window = ", окно по дате: без ограничения"

    if count is None:
        detail = ""
    elif cfg.mail.protocol == "pop3" and cfg.mail.since_days > 0:
        # POP3 не умеет искать по дате: окно применяется при обработке.
        detail = ", писем в ящике: " + str(count) + " (окно фильтруется при обработке)"
    else:
        detail = ", писем по фильтру: " + str(count)

    print(
        "Почта: OK — "
        + cfg.mail.protocol.upper()
        + " "
        + cfg.mail.host
        + ":"
        + str(cfg.mail.port)
        + ", папка «"
        + cfg.mail.mailbox
        + "»"
        + window
        + detail
    )
    return True