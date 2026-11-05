"""Подготовка вложений: текст документов и картинки для OCR."""

from __future__ import annotations

import io

import pytest
from conftest import build_email

from processor.attachments import PrepareLimits, prepare_all, prepare_attachment
from processor.attachments.images import image_to_data_url
from processor.mail.parser import parse_eml
from processor.models import Attachment

LIMITS = PrepareLimits(ocr_max_pages=3, text_max_pages=3, max_text_chars=10000)


def attachment(name: str, mime: str, content: bytes, *, inline: bool = False) -> Attachment:
    return Attachment(filename=name, mime_type=mime, content=content, inline=inline)


def test_text_attachment_cp1251():
    content = "Договор поставки №45".encode("cp1251")
    prepared = prepare_attachment(attachment("договор.txt", "text/plain", content), LIMITS)
    assert prepared.kind == "text"
    assert prepared.extracted_text == "Договор поставки №45"


def test_html_attachment_is_stripped():
    prepared = prepare_attachment(
        attachment("письмо.html", "text/html", "<p>Привет</p><b>коллеги</b>".encode("utf-8")),
        LIMITS,
    )
    assert prepared.extracted_text == "Привет коллеги"


def test_long_text_is_trimmed_with_note():
    prepared = prepare_attachment(
        attachment("лог.txt", "text/plain", b"a" * 500), PrepareLimits(max_text_chars=100)
    )
    assert prepared.extracted_text is not None
    assert len(prepared.extracted_text) == 100
    assert "обрезан" in (prepared.note or "")


def test_unsupported_archive_has_note():
    prepared = prepare_attachment(
        attachment("архив.zip", "application/zip", b"PK\x03\x04"), LIMITS
    )
    assert prepared.handled is False
    assert "архив" in (prepared.note or "").lower()


def test_unknown_type_has_note():
    prepared = prepare_attachment(attachment("data.bin", "application/octet-stream", b"\x00"), LIMITS)
    assert prepared.handled is False
    assert "неподдерживаемый" in (prepared.note or "")


def test_docx_text_and_tables():
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Договор аренды помещения")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Сумма"
    table.rows[0].cells[1].text = "100 000"
    buffer = io.BytesIO()
    document.save(buffer)

    prepared = prepare_attachment(
        attachment(
            "договор.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            buffer.getvalue(),
        ),
        LIMITS,
    )
    assert prepared.handled is True
    assert "Договор аренды помещения" in (prepared.extracted_text or "")
    assert "100 000" in (prepared.extracted_text or "")


def test_xlsx_text():
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Реестр"
    sheet.append(["Номер", "Сумма"])
    sheet.append(["45", 1000])
    buffer = io.BytesIO()
    book.save(buffer)

    prepared = prepare_attachment(
        attachment(
            "реестр.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            buffer.getvalue(),
        ),
        LIMITS,
    )
    text = prepared.extracted_text or ""
    assert "Реестр" in text
    assert "Номер" in text and "45" in text


def test_png_becomes_data_url():
    pil = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pil.new("RGB", (64, 48), "white").save(buffer, format="PNG")

    prepared = prepare_attachment(
        attachment("скан.png", "image/png", buffer.getvalue()), LIMITS
    )
    assert prepared.handled is True
    assert prepared.images[0].startswith("data:image/png;base64,")
    assert prepared.pages == 1


def test_bmp_is_converted_to_png():
    pil = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pil.new("RGB", (32, 32), "white").save(buffer, format="BMP")

    data_url = image_to_data_url(buffer.getvalue(), "image/bmp")
    assert data_url is not None
    assert data_url.startswith("data:image/png;base64,")


def test_multimodal_off_skips_images():
    pil = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pil.new("RGB", (32, 32), "white").save(buffer, format="PNG")

    prepared = prepare_attachment(
        attachment("скан.png", "image/png", buffer.getvalue()),
        PrepareLimits(multimodal=False),
    )
    assert prepared.images == []
    assert prepared.handled is False
    assert "multimodal" in (prepared.note or "")


def test_small_inline_images_are_skipped():
    pil = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pil.new("RGB", (8, 8), "white").save(buffer, format="PNG")
    attachments = [
        attachment("logo.png", "image/png", buffer.getvalue(), inline=True),
        attachment("скан.png", "image/png", buffer.getvalue(), inline=False),
    ]
    prepared = prepare_all(
        attachments, LIMITS, skip_inline_images=True, min_inline_image_bytes=10_000
    )
    assert [item.filename for item in prepared] == ["скан.png"]


def test_pdf_text_pages_and_render():
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    for number in (1, 2):
        page = document.new_page()
        page.insert_text((72, 72), "Dogovor 45 page " + str(number))
    pdf_bytes = document.tobytes()
    document.close()

    prepared = prepare_attachment(attachment("договор.pdf", "application/pdf", pdf_bytes), LIMITS)
    assert prepared.handled is True
    assert prepared.pages == 2
    text = prepared.extracted_text or ""
    assert "--- стр. 1 ---" in text
    assert "Dogovor 45 page 1" in text
    assert len(prepared.images) == 2
    assert prepared.images[0].startswith("data:image/png;base64,")


def test_pdf_page_limit_is_reported():
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    for _ in range(4):
        document.new_page()
    pdf_bytes = document.tobytes()
    document.close()

    prepared = prepare_attachment(
        attachment("длинный.pdf", "application/pdf", pdf_bytes),
        PrepareLimits(ocr_max_pages=2, text_max_pages=2),
    )
    assert prepared.pages == 4
    assert "первые 2 страниц из 4" in (prepared.note or "")


def test_full_pipeline_prepares_attachments_from_letter():
    raw = build_email(
        attachments=[("счёт.txt", "text/plain", "Счёт на оплату".encode("utf-8"))]
    )
    mail = parse_eml(raw)
    prepared = prepare_all(mail.attachments, LIMITS)
    assert prepared[0].extracted_text == "Счёт на оплату"

def test_eml_attachment_is_parsed_as_letter():
    """Пересланное письмо вложением: шапка, текст и вложения внутри него."""
    inner = build_email(
        subject="Исходное письмо",
        from_name="АО «Вектор»",
        from_address="vector@example.ru",
        body="Текст исходного письма про договор",
        message_id="inner@test",
        attachments=[("акт.txt", "text/plain", "Акт приёма работ".encode("utf-8"))],
    )
    prepared = prepare_attachment(
        attachment("Пересланное.eml", "message/rfc822", inner), LIMITS
    )

    assert prepared.kind == "email"
    assert prepared.handled is True
    text = prepared.extracted_text or ""
    assert "Пересланное письмо" in text
    assert "АО «Вектор»" in text
    assert "Текст исходного письма про договор" in text
    assert "Акт приёма работ" in text


def test_eml_attachment_pages_are_counted():
    """Страницы PDF внутри пересланного письма идут в «Кол-во листов»."""
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    for _ in range(3):
        document.new_page()
    pdf_bytes = document.tobytes()
    document.close()

    inner = build_email(
        subject="С актом",
        body="Направляю акт.",
        message_id="inner-pdf@test",
        attachments=[("акт.pdf", "application/pdf", pdf_bytes)],
    )
    prepared = prepare_attachment(
        attachment("Пересланное.eml", "message/rfc822", inner), LIMITS
    )

    assert prepared.pages == 3
    assert prepared.images, "страницы PDF должны уйти на OCR"


def test_eml_attachment_without_headers_is_still_read():
    """Файл .eml без заголовков не роняет разбор: текст остаётся доступен."""
    prepared = prepare_attachment(
        attachment("битое.eml", "message/rfc822", b"not a message at all"), LIMITS
    )
    assert prepared.kind == "email"
    assert prepared.handled is True
    assert "not a message at all" in (prepared.extracted_text or "")
