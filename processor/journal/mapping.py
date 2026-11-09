"""Заполнение полей журнала по разбору письма.

Соответствие полей сверено с рабочей схемой базы (schema.sql, таблица CARD):

    CARD_TYPE  = 1                      — входящий документ (KL_CARDTYPE)
    INTINDEX   = «новая»                — номер присваивает система учёта
    INTDATE    = дата регистрации       — формат ДД.ММ.ГГГГ
    EXTINDEX   = номер документа        — от контрагента, как в бумаге, до 20 символов
    EXTDATE    = дата документа         — формат ДД.ММ.ГГГГ
    KORRESP    = название корреспондента из справочника KORRESP, до 100 символов
    KORRESPID  = KORRESP.ID             — 0, если корреспондент не найден
    SH_CONTENTS= краткое содержание     — до 512 символов
    PAGES_DOC  = количество листов
    THEMA      = THEMA.ID               — 0, если тема не определена

Правила заполнения повторяют то, как поля подсказываются в MailSense
(packages/server/src/journalRoutes.ts, эндпоинт /journal/suggest): содержание —
выжимка ИИ, номер — внешний номер, дата — дата отправки документа, листы — сумма
страниц PDF, иначе число вложений.

Отличия, нужные автоматической службе (человека, который бы поправил, нет):
  * корреспондент и тема берутся из справочников, а не придумываются моделью;
  * значения не бывают пустыми в обязательных графах — есть настраиваемые
    резервные источники (дата письма, дата регистрации);
  * тексты обрезаются по длинам колонок CARD: Firebird иначе откажет
    «string right truncation», и письмо не попадёт в журнал;
  * все значения приводятся к одной строке: журнал — таблица, а не текст.
"""

from __future__ import annotations

import datetime as dt

from ..classifiers import ResolvedCorrespondent, ResolvedTheme
from ..config import JournalConfig
from ..mail.parser import extract_external_number
from ..models import Analysis, CardData, MailMessage, PreparedAttachment


def build_card(
    cfg: JournalConfig,
    analysis: Analysis,
    mail: MailMessage,
    prepared: list[PreparedAttachment] | None = None,
    *,
    today: dt.date | None = None,
    correspondent: ResolvedCorrespondent | None = None,
    theme: ResolvedTheme | None = None,
) -> CardData:
    """Собирает запись журнала из письма, разбора модели и справочников.

    prepared можно не передавать: тогда «Кол-во листов» считается без сведений
    о вложениях (минимум — один лист). correspondent и theme — результаты
    сопоставления со справочниками; без них значения берутся из письма.
    """
    prepared = prepared or []
    reg_date = choose_reg_date(cfg, mail, today=today)
    doc_date = choose_doc_date(cfg, analysis, mail, reg_date)

    sender_name = choose_sender(analysis, mail, correspondent)
    korrespid = correspondent.id if correspondent is not None else 0
    thema_id = theme.id if theme is not None else 0
    thema_name = theme.name if theme is not None else (analysis.thema_name or "")

    return CardData(
        card_type=cfg.card_type,
        intindex=_limit(cfg.intindex, cfg.max_intindex_length),
        intdate=reg_date,
        extindex=_limit(choose_doc_number(cfg, analysis, mail), cfg.max_extindex_length),
        extdate=doc_date,
        sh_contents=_limit(choose_summary(analysis, mail), cfg.max_sh_contents_length),
        pages_doc=choose_sheets(cfg, analysis, prepared),
        korresp=_limit(sender_name, cfg.max_korresp_length),
        korrespid=int(korrespid or 0),
        thema=int(thema_id or 0),
        thema_name=_limit(_one_line(thema_name), cfg.max_korresp_length),
    )


def choose_reg_date(
    cfg: JournalConfig, mail: MailMessage, *, today: dt.date | None = None
) -> dt.date:
    """Дата регистрации: сегодняшняя или дата письма."""
    current = today or dt.date.today()
    if cfg.reg_date == "email_date" and mail.date is not None:
        return mail.date.date()
    return current


def choose_doc_number(cfg: JournalConfig, analysis: Analysis, mail: MailMessage) -> str:
    """Номер документа: сначала ответ модели, затем настраиваемая страховка.

    По умолчанию страховки нет (cfg.doc_number_fallback = none): служба пишет
    в журнал сама, без человека, который поправил бы подсказку. Регэксп по телу
    письма легко цепляет номер из цитаты или примера — пустая графа честнее
    неверного номера. Режимы subject и regex повторяют поведение MailSense.
    """
    if analysis.doc_number:
        return analysis.doc_number
    if cfg.doc_number_fallback == "subject":
        return extract_external_number(mail.subject) or ""
    if cfg.doc_number_fallback == "regex":
        return (
            extract_external_number(mail.subject)
            or extract_external_number(mail.body_text)
            or ""
        )
    return ""


def choose_doc_date(
    cfg: JournalConfig,
    analysis: Analysis,
    mail: MailMessage,
    reg_date: dt.date,
) -> dt.date | None:
    """Дата документа с настраиваемым резервом.

    Firebird не примет пустую строку в поле даты, поэтому «пусто» — это либо
    NULL (если колонка это разрешает), либо осмысленная дата из настроек.
    """
    if analysis.doc_date is not None:
        return analysis.doc_date
    if cfg.doc_date_fallback == "email_date":
        return mail.date.date() if mail.date else None
    if cfg.doc_date_fallback == "reg_date":
        return reg_date
    if cfg.doc_date_fallback == "today":
        return dt.date.today()
    return None


def choose_summary(analysis: Analysis, mail: MailMessage) -> str:
    """«Краткое содержание»: выжимка модели, иначе тема письма."""
    text = analysis.summary.strip()
    if not text:
        text = mail.subject or ""
    return _one_line(text)


def choose_sender(
    analysis: Analysis,
    mail: MailMessage,
    correspondent: ResolvedCorrespondent | None = None,
) -> str:
    """«От кого получен»: название из справочника, иначе имя или адрес отправителя."""
    if correspondent is not None and correspondent.name:
        return _one_line(correspondent.name)
    return _one_line(analysis.sender or mail.from_name or mail.from_address)


def choose_sheets(
    cfg: JournalConfig, analysis: Analysis, prepared: list[PreparedAttachment]
) -> int:
    """«Кол-во листов» по выбранной стратегии."""
    mode = cfg.sheets_mode
    if mode == "one":
        return 1
    if mode == "attachments":
        return max(1, len(prepared))
    if mode == "analysis":
        if analysis.sheets_count:
            return analysis.sheets_count
        return _pdf_pages(prepared)
    # pdf_pages — как в подсказке MailSense: сумма страниц PDF, иначе число вложений.
    return _pdf_pages(prepared)


def _pdf_pages(prepared: list[PreparedAttachment]) -> int:
    pages = [item.pages for item in prepared if item.kind == "pdf" and item.pages]
    if pages:
        return sum(int(value) for value in pages)
    return max(1, len(prepared))


def to_api_payload(card: CardData, analysis: Analysis | None = None) -> dict:
    """Тело запроса к FastAPI-сервису — по api.md.

    Даты — строго ГГГГ-ММ-ДД: сервис разбирает их strptime-ом и упадёт на другом
    формате. docDate в модели обязательный, поэтому при отсутствии берём дату
    регистрации (иначе запрос не пройдёт проверку).
    """
    doc_date = card.extdate or card.intdate
    return {
        "docNumber": card.extindex,
        "docDate": doc_date.isoformat(),
        "sender": card.korresp,
        "summary": card.sh_contents,
        "documentName": (analysis.document_name if analysis is not None else "") or "",
        "sheetsCount": card.pages_doc,
        "regDate": card.intdate.isoformat(),
        "correspondentId": card.korrespid or None,
    }


def describe_card(card: CardData) -> str:
    """Короткая строка записи для журнала работы службы."""
    date_text = card.extdate.strftime("%d.%m.%Y") if card.extdate else "без даты"
    number = card.extindex or "без номера"
    parts = [
        "№ " + number,
        "от " + date_text,
        "корреспондент: " + (card.korresp or "не указан"),
        "KORRESPID=" + str(card.korrespid),
        "листов: " + str(card.pages_doc),
        "содержание: " + _shorten(card.sh_contents, 120),
    ]
    if card.thema:
        parts.insert(4, "тема: " + (card.thema_name or str(card.thema)) + " (" + str(card.thema) + ")")
    return ", ".join(parts)


def _one_line(text: str) -> str:
    """Схлопывает переводы строк и повторные пробелы: журнал — таблица."""
    return " ".join((text or "").split())


def _limit(text: str, max_length: int) -> str:
    """Обрезка по длине колонки (эллипсис влезает в лимит)."""
    if max_length and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
