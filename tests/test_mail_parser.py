"""Разбор писем: заголовки, тело, вложения, эвристики."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import build_email

from processor.mail.parser import (
    classify_kind,
    extract_external_number,
    is_forwarded,
    normalize_message_id,
    parse_eml,
    strip_html,
)

# Письмо из исходных материалов проекта; в публичный репозиторий не входит,
# поэтому тест пропускается, если файла нет.
REAL_EML = Path(__file__).resolve().parents[1] / "reference" / (
    "Re_Эндпоинт_регистрации_письма_в_журнале_Корлякова_Мария_Олеговна.eml"
)


def test_subject_and_sender_are_decoded():
    raw = build_email(
        subject="Счёт на оплату",
        from_name="Корлякова Мария Олеговна",
        from_address="korlyakova@kniitmu.ru",
    )
    mail = parse_eml(raw)
    assert mail.subject == "Счёт на оплату"
    assert mail.from_name == "Корлякова Мария Олеговна"
    assert mail.from_address == "korlyakova@kniitmu.ru"
    assert mail.from_display == "Корлякова Мария Олеговна <korlyakova@kniitmu.ru>"


def test_message_id_is_normalized():
    mail = parse_eml(build_email(message_id="abc@host"))
    assert mail.message_id == "abc@host"


def test_missing_message_id_gets_stable_key():
    raw = build_email(message_id=None)
    first = parse_eml(raw).message_id
    second = parse_eml(raw).message_id
    assert first.startswith("sha256:")
    assert first == second


def test_missing_subject_has_placeholder():
    raw = build_email(subject="")
    assert parse_eml(raw).subject == "(без темы)"


def test_attachments_are_collected():
    raw = build_email(
        attachments=[("счёт.pdf", "application/pdf", b"%PDF-1.4 test"), ("акт.txt", "text/plain", b"text")]
    )
    mail = parse_eml(raw)
    names = [item.filename for item in mail.attachments]
    assert names == ["счёт.pdf", "акт.txt"]
    assert mail.attachments[0].content.startswith(b"%PDF")


def test_html_only_body_becomes_text():
    raw = build_email(body="", html="<p>Уважаемые коллеги!</p><p>&laquo;Договор&raquo;</p>")
    mail = parse_eml(raw)
    assert "Уважаемые коллеги" in mail.body_text
    assert "<p>" not in mail.body_text
    assert "«Договор»" in mail.body_text


def test_forwarded_detected_by_subject():
    raw = build_email(subject="Fwd: Договор №17", body="текст")
    assert parse_eml(raw).forwarded is True


def test_forwarded_detected_by_body_marker():
    raw = build_email(subject="Договор", body="---------- Пересылаемое сообщение ----------")
    assert parse_eml(raw).forwarded is True


def test_not_forwarded():
    raw = build_email(subject="Договор", body="Обычное письмо")
    assert parse_eml(raw).forwarded is False


def test_date_is_parsed():
    mail = parse_eml(build_email(date="Fri, 23 Oct 2026 18:09:21 +0300"))
    assert mail.date is not None
    assert mail.date.year == 2026
    assert mail.date_text.startswith("2026-10-23")


def test_broken_email_does_not_raise():
    # Битые заголовки не должны ронять разбор: служба продолжает работу.
    raw = b"Subject: =?unknown-charset?B?0J/RgNC40LLQtdGC?=\r\n\r\nBody"
    mail = parse_eml(raw)
    assert isinstance(mail.subject, str)


@pytest.mark.parametrize(
    "mime, name, expected",
    [
        ("image/png", "logo.png", "image"),
        ("application/octet-stream", "scan.PDF", "pdf"),
        ("application/pdf", "file.bin", "pdf"),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx", "docx"),
        ("application/octet-stream", "письмо.doc", "docx"),
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "a.xlsx", "xlsx"),
        ("message/rfc822", "пересланное.eml", "email"),
        ("application/octet-stream", "письмо.EML", "email"),
        ("text/plain", "a.txt", "text"),
        ("text/html", "a.html", "html"),
        ("application/zip", "a.zip", "other"),
    ],
)
def test_classify_kind(mime, name, expected):
    assert classify_kind(mime, name) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Исх. № 123/45 от 01.02.2026", "123/45"),
        ("Вх. 456/78", "456/78"),
        ("письмо №789", "789"),
        ("без номера вообще", None),
        ("исх. № 12–34", "12-34"),
    ],
)
def test_extract_external_number(text, expected):
    assert extract_external_number(text) == expected


def test_normalize_message_id():
    assert normalize_message_id("<a@b>") == "a@b"
    assert normalize_message_id("  <a@b>  ") == "a@b"
    assert normalize_message_id(None) == ""


def test_strip_html():
    assert strip_html("<b>Жирный</b> &amp; <i>курсив</i>") == "Жирный & курсив"


@pytest.mark.skipif(not REAL_EML.is_file(), reason="нет файла письма из задания")
def test_real_letter_from_assignment():
    mail = parse_eml(REAL_EML.read_bytes(), uid="7", folder="INBOX")
    assert mail.subject == "Re: Эндпоинт регистрации письма в журнале"
    assert mail.from_name == "Корлякова Мария Олеговна"
    assert mail.from_address == "korlyakova_mo@kniitmu.ru"
    assert mail.message_id == "2c8fcf16-3ea3-437c-8609-a875f13f1611@kniitmu.ru"
    assert [item.filename for item in mail.attachments] == ["FastAPI_sender_to_BD.zip"]
    assert "Сервис отправки разбора в БД fbd" in mail.body_text
