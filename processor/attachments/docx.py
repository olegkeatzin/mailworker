"""DOCX: текст абзацев и таблиц, плюс картинки внутри документа.

Порт docxToParts из MailSense: текст документа идёт в промпт, вложенные
изображения (сканы, вклеенные в Word) — на OCR, потому что подписи и штампы
там часто только картинкой.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field


@dataclass
class DocxParts:
    text: str = ""
    images: list[bytes] = field(default_factory=list)
    note: str = ""


def docx_parts(content: bytes, *, with_images: bool = True, max_images: int = 10) -> DocxParts:
    """Читает .docx; при отсутствии python-docx возвращает пометку."""
    try:
        import docx  # python-docx
    except ImportError:
        return DocxParts(note="не установлен пакет python-docx — текст .docx не прочитан")

    try:
        document = docx.Document(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        # Старый .doc и «псевдо-docx» сюда попадают: тип определён по расширению.
        return DocxParts(note="не удалось прочитать .docx: " + str(exc))

    chunks: list[str] = []
    try:
        for paragraph in document.paragraphs:
            text = (paragraph.text or "").strip()
            if text:
                chunks.append(text)
        for table in document.tables:
            rows: list[str] = []
            for row in table.rows:
                cells = [(cell.text or "").strip() for cell in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                chunks.append("\n".join(rows))
    except Exception as exc:  # noqa: BLE001
        return DocxParts(text="\n".join(chunks), note="часть .docx не прочитана: " + str(exc))

    images: list[bytes] = []
    if with_images:
        images = _embedded_images(content, max_images)

    return DocxParts(text="\n".join(chunks).strip(), images=images)


def _embedded_images(content: bytes, max_images: int) -> list[bytes]:
    """Достаёт картинки из word/media: python-docx их не отдаёт списком."""
    images: list[bytes] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in archive.namelist():
                if not name.startswith("word/media/"):
                    continue
                if not name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff")):
                    continue
                try:
                    data = archive.read(name)
                except (KeyError, OSError):
                    continue
                # Мелкие картинки — рамки, линии, логотип в колонтитуле: OCR не нужен.
                if len(data) < 10_000:
                    continue
                images.append(data)
                if len(images) >= max_images:
                    break
    except (zipfile.BadZipFile, OSError):
        return []
    return images
