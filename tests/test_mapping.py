"""Заполнение полей журнала по разбору письма."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from processor.classifiers import ResolvedCorrespondent, ResolvedTheme
from processor.journal.mapping import build_card, describe_card, to_api_payload
from processor.models import Analysis, MailMessage, PreparedAttachment


def make_mail(**overrides) -> MailMessage:
    values = dict(
        uid="1",
        folder="INBOX",
        message_id="msg@test",
        subject="Счёт на оплату",
        from_name="ООО «Ромашка»",
        from_address="romashka@example.ru",
        to_display="inbox@example.ru",
        date=dt.datetime(2026, 10, 20, 10, 0),
        body_text="Направляем счёт.",
        body_html="",
    )
    values.update(overrides)
    return MailMessage(**values)


def make_analysis(**overrides) -> Analysis:
    values = dict(
        summary="Счёт на оплату по договору №45",
        document_name="Счёт",
        sender="ООО «Ромашка», Иванов И.И.",
        doc_number="45/12",
        doc_date=dt.date(2026, 10, 18),
        sheets_count=3,
    )
    values.update(overrides)
    return Analysis(**values)


def test_main_fields(journal_cfg):
    card = build_card(journal_cfg, make_analysis(), make_mail(), [])
    assert card.card_type == 1
    assert card.intindex == "новая"
    assert card.intdate == dt.date.today()
    assert card.extindex == "45/12"
    assert card.extdate == dt.date(2026, 10, 18)
    assert card.sh_contents == "Счёт на оплату по договору №45"
    assert card.korresp == "ООО «Ромашка», Иванов И.И."
    assert card.pages_doc >= 1


def test_intindex_from_config(journal_cfg):
    cfg = dataclasses.replace(journal_cfg, intindex="вх")
    card = build_card(cfg, make_analysis(), make_mail())
    assert card.intindex == "вх"


def test_reg_date_from_letter(journal_cfg):
    cfg = dataclasses.replace(journal_cfg, reg_date="email_date")
    card = build_card(cfg, make_analysis(), make_mail())
    assert card.intdate == dt.date(2026, 10, 20)


def test_doc_date_fallbacks(journal_cfg):
    analysis = make_analysis(doc_date=None)
    mail = make_mail()

    assert build_card(journal_cfg, analysis, mail).extdate == dt.date(2026, 10, 20)

    cfg = dataclasses.replace(journal_cfg, doc_date_fallback="reg_date")
    assert build_card(cfg, analysis, mail).extdate == dt.date.today()

    cfg = dataclasses.replace(journal_cfg, doc_date_fallback="today")
    assert build_card(cfg, analysis, mail).extdate == dt.date.today()

    cfg = dataclasses.replace(journal_cfg, doc_date_fallback="none")
    assert build_card(cfg, analysis, mail).extdate is None


def test_doc_number_fallback_regex(journal_cfg):
    analysis = make_analysis(doc_number=None)
    mail = make_mail(subject="Письмо исх. № 17/3 от 01.09.2026")

    cfg = dataclasses.replace(journal_cfg, doc_number_fallback="regex")
    assert build_card(cfg, analysis, mail).extindex == "17/3"

    cfg = dataclasses.replace(journal_cfg, doc_number_fallback="subject")
    assert build_card(cfg, analysis, mail).extindex == "17/3"

    # По умолчанию номер не придумывается: лучше пусто, чем чужой номер.
    assert build_card(journal_cfg, analysis, mail).extindex == ""


def test_body_only_number_is_not_taken_by_default(journal_cfg):
    """Номер из цитаты в теле письма не должен попасть в журнал сам по себе."""
    analysis = make_analysis(doc_number=None)
    mail = make_mail(subject="Вопрос по договору", body_text="... договору №45 ...")

    assert build_card(journal_cfg, analysis, mail).extindex == ""

    cfg = dataclasses.replace(journal_cfg, doc_number_fallback="regex")
    assert build_card(cfg, analysis, mail).extindex == "45"


def test_summary_falls_back_to_subject(journal_cfg):
    card = build_card(journal_cfg, make_analysis(summary=""), make_mail())
    assert card.sh_contents == "Счёт на оплату"


def test_summary_is_single_line(journal_cfg):
    card = build_card(journal_cfg, make_analysis(summary="Первая строка\n\nВторая   строка"), make_mail())
    assert card.sh_contents == "Первая строка Вторая строка"


@pytest.mark.parametrize(
    "sender, from_name, from_address, expected",
    [
        ("ООО «Ромашка»", "Иванов", "i@x.ru", "ООО «Ромашка»"),
        ("", "Иванов", "i@x.ru", "Иванов"),
        ("", "", "i@x.ru", "i@x.ru"),
        ("", "", "", ""),
    ],
)
def test_sender_chain(journal_cfg, sender, from_name, from_address, expected):
    mail = make_mail(from_name=from_name, from_address=from_address)
    card = build_card(journal_cfg, make_analysis(sender=sender), mail)
    assert card.korresp == expected


def test_summary_is_trimmed_to_column(journal_cfg):
    """SH_CONTENTS в схеме — VARCHAR(512); без обрезки Firebird откажет."""
    cfg = dataclasses.replace(journal_cfg, max_sh_contents_length=512)
    card = build_card(cfg, make_analysis(summary="Ж" * 600), make_mail())
    assert len(card.sh_contents) == 512
    assert card.sh_contents.endswith("…")


def test_doc_number_and_sender_trimmed(journal_cfg):
    cfg = dataclasses.replace(journal_cfg, max_extindex_length=20, max_korresp_length=100)
    card = build_card(
        cfg,
        make_analysis(doc_number="1" * 30, sender="О" * 150),
        make_mail(),
    )
    assert len(card.extindex) == 20
    assert len(card.korresp) == 100


def test_classifier_values_win(journal_cfg):
    """Корреспондент и тема из справочников важнее того, что предложила модель."""
    correspondent = ResolvedCorrespondent(
        name="ООО «Ромашка»", id=42, matched=True, source="email", score=1.0
    )
    theme = ResolvedTheme(id=7, name="Бухгалтерия", matched=True, source="model", score=1.0)

    card = build_card(
        journal_cfg,
        make_analysis(sender="ООО Ромашка (по письму)"),
        make_mail(),
        correspondent=correspondent,
        theme=theme,
    )

    assert card.korresp == "ООО «Ромашка»"
    assert card.korrespid == 42
    assert card.thema == 7
    assert card.thema_name == "Бухгалтерия"


def test_without_classifiers_ids_are_zero(journal_cfg):
    card = build_card(journal_cfg, make_analysis(), make_mail())
    assert card.korrespid == 0
    assert card.thema == 0


def test_sheets_from_pdf_pages(journal_cfg):
    prepared = [
        PreparedAttachment(filename="a.pdf", mime_type="application/pdf", kind="pdf", pages=3),
        PreparedAttachment(filename="b.pdf", mime_type="application/pdf", kind="pdf", pages=2),
        PreparedAttachment(filename="c.txt", mime_type="text/plain", kind="text"),
    ]
    card = build_card(journal_cfg, make_analysis(), make_mail(), prepared)
    assert card.pages_doc == 5


def test_sheets_fallback_to_attachment_count(journal_cfg):
    prepared = [
        PreparedAttachment(filename="a.txt", mime_type="text/plain", kind="text"),
        PreparedAttachment(filename="b.txt", mime_type="text/plain", kind="text"),
    ]
    assert build_card(journal_cfg, make_analysis(), make_mail(), prepared).pages_doc == 2
    assert build_card(journal_cfg, make_analysis(), make_mail(), []).pages_doc == 1


def test_sheets_modes(journal_cfg):
    prepared = [
        PreparedAttachment(filename="a.pdf", mime_type="application/pdf", kind="pdf", pages=4),
    ]
    analysis = make_analysis(sheets_count=9)

    assert build_card(dataclasses.replace(journal_cfg, sheets_mode="analysis"), analysis, make_mail(), prepared).pages_doc == 9
    assert build_card(dataclasses.replace(journal_cfg, sheets_mode="attachments"), analysis, make_mail(), prepared).pages_doc == 1
    assert build_card(dataclasses.replace(journal_cfg, sheets_mode="one"), analysis, make_mail(), prepared).pages_doc == 1


def test_api_payload(journal_cfg):
    card = build_card(journal_cfg, make_analysis(), make_mail())
    payload = to_api_payload(card, make_analysis())
    assert payload["docNumber"] == "45/12"
    assert payload["docDate"] == "2026-10-18"
    assert payload["regDate"] == dt.date.today().isoformat()
    assert payload["sender"] == "ООО «Ромашка», Иванов И.И."
    assert payload["documentName"] == "Счёт"
    assert payload["sheetsCount"] == card.pages_doc
    assert payload["correspondentId"] is None


def test_api_payload_passes_correspondent_id(journal_cfg):
    correspondent = ResolvedCorrespondent(name="ООО «Ромашка»", id=42, matched=True)
    card = build_card(journal_cfg, make_analysis(), make_mail(), correspondent=correspondent)
    payload = to_api_payload(card, make_analysis())
    assert payload["correspondentId"] == 42


def test_api_payload_requires_doc_date(journal_cfg):
    """В модели FastAPI docDate обязательный: подставляем дату регистрации."""
    analysis = make_analysis(doc_date=None)
    cfg = dataclasses.replace(journal_cfg, doc_date_fallback="none")
    card = build_card(cfg, analysis, make_mail())
    payload = to_api_payload(card, analysis)
    assert payload["docDate"] == card.intdate.isoformat()


def test_describe_card(journal_cfg):
    card = build_card(journal_cfg, make_analysis(), make_mail())
    text = describe_card(card)
    assert "45/12" in text
    assert "18.10.2026" in text
    assert "ООО «Ромашка»" in text
    assert "KORRESPID=0" in text