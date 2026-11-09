"""Архив: имена папок, безопасные имена файлов и раскладка."""

from __future__ import annotations

from pathlib import Path

from processor.archive import folder_name, safe_filename, save_letter
from processor.models import Attachment, CardData, InsertedCard, MailMessage


def make_mail(attachments: list[Attachment] | None = None, raw: bytes = b"raw") -> MailMessage:
    return MailMessage(
        uid="1",
        folder="INBOX",
        message_id="id@test",
        subject="Письмо",
        from_name="",
        from_address="",
        to_display="",
        date=None,
        body_text="",
        body_html="",
        attachments=attachments or [],
        raw=raw,
    )


def make_card(intindex: str = "новая") -> CardData:
    import datetime as dt

    return CardData(
        card_type=1,
        intindex=intindex,
        intdate=dt.date(2026, 11, 9),
        extindex="",
        extdate=None,
        sh_contents="",
        pages_doc=1,
        korresp="",
    )


def test_folder_is_record_id():
    inserted = InsertedCard(card_id=137)
    assert folder_name(inserted, make_card(), make_mail()) == "137"


def test_folder_falls_back_to_registration_number():
    card = make_card("вх12")
    assert folder_name(InsertedCard(card_id=None), card, make_mail()) == "вх12"
    assert folder_name(None, card, make_mail()) == "вх12"


def test_folder_falls_back_to_message_id_and_is_sanitized():
    mail = make_mail()
    mail.message_id = "a/b:c@test"
    assert folder_name(None, make_card(), mail) == "a_b_c@test"


def test_safe_filename_keeps_original_name():
    assert safe_filename("Счёт 45.pdf", "application/pdf", 1) == "Счёт 45.pdf"


def test_safe_filename_strips_paths_and_bad_chars():
    assert safe_filename("../evil?.txt", "text/plain", 1) == "evil_.txt"


def test_safe_filename_adds_extension_from_mime():
    assert safe_filename("скан", "application/pdf", 1) == "скан.pdf"


def test_safe_filename_for_missing_name():
    assert safe_filename(None, "image/png", 3) == "attachment-3.png"


def test_safe_filename_avoids_reserved_names():
    assert safe_filename("CON.txt", "text/plain", 1) == "_CON.txt"


def test_safe_filename_is_limited():
    name = safe_filename("ж" * 200 + ".pdf", "application/pdf", 1)
    assert len(name) <= 100
    assert name.endswith(".pdf")


def test_save_letter_writes_letter_and_attachments(workdir: Path):
    mail = make_mail(
        attachments=[
            Attachment(filename="Счёт 45.pdf", mime_type="application/pdf", content=b"%PDF"),
            Attachment(filename="акт.docx", mime_type="application/octet-stream", content=b"PK"),
        ],
        raw=b"raw-letter",
    )

    folder = save_letter(mail, workdir, "137")

    assert folder == workdir / "137"
    assert (folder / "letter.eml").read_bytes() == b"raw-letter"
    assert (folder / "Счёт 45.pdf").read_bytes() == b"%PDF"
    assert (folder / "акт.docx").read_bytes() == b"PK"


def test_save_letter_does_not_overwrite(workdir: Path):
    folder = workdir / "137"
    folder.mkdir()
    (folder / "letter.eml").write_bytes(b"old")
    (folder / "report.txt").write_bytes(b"old report")

    mail = make_mail(
        attachments=[Attachment(filename="report.txt", mime_type="text/plain", content=b"new")],
        raw=b"new letter",
    )
    save_letter(mail, workdir, "137")

    assert (folder / "letter.eml").read_bytes() == b"old"
    assert (folder / "letter-2.eml").read_bytes() == b"new letter"
    assert (folder / "report.txt").read_bytes() == b"old report"
    assert (folder / "report-2.txt").read_bytes() == b"new"


def test_attachment_named_like_letter_gets_suffix(workdir: Path):
    mail = make_mail(
        attachments=[
            Attachment(filename="letter.eml", mime_type="message/rfc822", content=b"inner")
        ]
    )

    folder = save_letter(mail, workdir, "137")

    assert (folder / "letter.eml").read_bytes() == b"raw"
    assert (folder / "letter-2.eml").read_bytes() == b"inner"
