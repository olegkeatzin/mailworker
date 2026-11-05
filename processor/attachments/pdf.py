"""PDF: текстовый слой, число страниц и рендер страниц для OCR.

Повторяет подход MailSense (packages/core/src/attachments/pdf.ts):
  * сначала читается ТЕКСТОВЫЙ СЛОЙ — это почти бесплатно и даёт точный текст;
  * страницы рендерятся в картинки только для OCR (сканы, где слоя нет);
  * обрезка по лимиту страниц помечается честно: «распознаны первые N из M»,
    иначе модель считает, что видела документ целиком.

Библиотеки необязательные: pypdf (текст) и PyMuPDF (рендер). Если не установлена
ни одна, вложение помечается как необработанное с понятной причиной.
"""

from __future__ import annotations

import io
from dataclasses import dataclass


@dataclass
class PdfText:
    """Результат чтения текстового слоя."""

    text: str = ""
    pages_total: int = 0
    pages_read: int = 0
    truncated: bool = False


def pdf_text(content: bytes, max_pages: int) -> PdfText:
    """Читает текстовый слой первых max_pages страниц."""
    reader = _open_pypdf(content)
    if reader is not None:
        return _text_with_pypdf(reader, max_pages)
    doc = _open_pymupdf(content)
    if doc is not None:
        return _text_with_pymupdf(doc, max_pages)
    return PdfText()


def pdf_page_count(content: bytes) -> int:
    """Всего страниц в PDF (для графы «Кол-во листов»)."""
    reader = _open_pypdf(content)
    if reader is not None:
        try:
            return len(reader.pages)
        except Exception:  # noqa: BLE001
            return 0
    doc = _open_pymupdf(content)
    if doc is not None:
        try:
            return doc.page_count
        finally:
            doc.close()
    return 0


def pdf_pages_to_images(
    content: bytes, max_pages: int, *, dpi: int = 150, max_side: int = 1600
) -> list[bytes]:
    """Рендерит первые max_pages страниц в PNG для OCR."""
    doc = _open_pymupdf(content)
    if doc is None:
        return []
    images: list[bytes] = []
    try:
        for index in range(min(max_pages, doc.page_count)):
            page = doc.load_page(index)
            pixmap = page.get_pixmap(dpi=dpi)
            if max(pixmap.width, pixmap.height) > max_side:
                scale = max_side / float(max(pixmap.width, pixmap.height))
                pixmap = page.get_pixmap(dpi=max(72, int(dpi * scale)))
            images.append(pixmap.tobytes("png"))
    except Exception:  # noqa: BLE001 - битая страница не отменяет остальные
        pass
    finally:
        doc.close()
    return images


# ------------------------------------------------------------------ внутреннее


def _open_pypdf(content: bytes):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return PdfReader(io.BytesIO(content))
    except Exception:  # noqa: BLE001
        return None


def _open_pymupdf(content: bytes):
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # старое имя пакета
        except ImportError:
            return None
    try:
        return pymupdf.open(stream=content, filetype="pdf")
    except Exception:  # noqa: BLE001
        return None


def _text_with_pypdf(reader, max_pages: int) -> PdfText:
    try:
        total = len(reader.pages)
    except Exception:  # noqa: BLE001
        return PdfText()
    limit = min(max_pages, total)
    chunks: list[str] = []
    for index in range(limit):
        try:
            page_text = reader.pages[index].extract_text() or ""
        except Exception:  # noqa: BLE001
            page_text = ""
        if page_text.strip():
            # Маркер страницы нужен модели, чтобы указать источник номера документа.
            chunks.append("--- стр. " + str(index + 1) + " ---\n" + page_text.strip())
    return PdfText(
        text="\n\n".join(chunks).strip(),
        pages_total=total,
        pages_read=limit,
        truncated=total > limit,
    )


def _text_with_pymupdf(doc, max_pages: int) -> PdfText:
    total = doc.page_count
    limit = min(max_pages, total)
    chunks: list[str] = []
    try:
        for index in range(limit):
            try:
                page_text = doc.load_page(index).get_text("text") or ""
            except Exception:  # noqa: BLE001
                page_text = ""
            if page_text.strip():
                chunks.append("--- стр. " + str(index + 1) + " ---\n" + page_text.strip())
    finally:
        doc.close()
    return PdfText(
        text="\n\n".join(chunks).strip(),
        pages_total=total,
        pages_read=limit,
        truncated=total > limit,
    )
