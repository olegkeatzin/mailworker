"""Word-документы: .docx, .doc (Word 97-2003), .rtf, .odt.

Основной путь — python-docx для .docx. Старый .doc — это OLE2-контейнер,
python-docx его не открывает; текст извлекаем через catdoc (читает stdin) или
antiword (нужен временный файл). RTF и ODT разбираем сами: это текстовый и
zip-формат, внешние программы для них не нужны.
"""

from __future__ import annotations

import html as html_module
import io
import logging
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .docx import docx_parts

logger = logging.getLogger(__name__)

#: Сигнатура OLE2: так начинаются .doc, .xls, .ppt времён Word 97-2003.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: Группы RTF, текст из которых не нужен (шрифты, картинки, колонтитулы).
_SKIP_DESTINATIONS = {
    "fonttbl",
    "colortbl",
    "stylesheet",
    "info",
    "pict",
    "object",
    "header",
    "footer",
    "footnote",
    "annotation",
    "fldinst",
    "listtext",
    "pntext",
}


@dataclass
class WordParts:
    """Текст и картинки Word-документа."""

    text: str = ""
    images: list[bytes] = field(default_factory=list)
    note: str = ""
    format: str = ""


def word_parts(
    content: bytes, *, with_images: bool = True, max_images: int = 10
) -> WordParts:
    """Читает Word-документ любого из поддерживаемых форматов."""
    fmt = detect_format(content)

    if fmt == "docx":
        parts = docx_parts(content, with_images=with_images, max_images=max_images)
        return WordParts(
            text=parts.text, images=parts.images, note=parts.note, format="docx"
        )
    if fmt == "doc":
        text, note = _doc_text(content)
        return WordParts(text=text, note=note, format="doc")
    if fmt == "rtf":
        text = _rtf_text(content)
        return WordParts(
            text=text,
            note="" if text else "в RTF не найден текст",
            format="rtf",
        )
    if fmt == "odt":
        text, note = _odt_text(content)
        return WordParts(text=text, note=note, format="odt")
    if fmt == "html":
        text = _html_text(_decode_text(content))
        return WordParts(text=text, note="", format="html")
    if fmt == "text":
        return WordParts(text=_decode_text(content).strip(), note="", format="text")

    return WordParts(
        note="не удалось определить формат документа (.docx, .doc, .rtf, .odt)",
        format="unknown",
    )


def detect_format(content: bytes) -> str:
    """Определяет формат по содержимому, а не по расширению.

    Отправители регулярно переименовывают файлы: .doc оказывается .docx,
    .docx — старым .doc, а иногда внутри просто HTML или текст.
    """
    if not content:
        return "unknown"

    if content.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            # Пусть python-docx скажет точнее, что файл повреждён.
            return "docx"
        if "word/document.xml" in names:
            return "docx"
        if "content.xml" in names:
            return "odt"
        return "docx"

    if content.startswith(_OLE_MAGIC):
        return "doc"

    stripped = content.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"{\\rtf"):
        return "rtf"

    low = content[:4096].lower()
    if b"<html" in low or b"<!doctype" in low:
        return "html"

    if _looks_like_text(content):
        return "text"
    return "unknown"


def _doc_text(content: bytes) -> tuple[str, str]:
    """Текст старого .doc: catdoc, затем antiword."""
    text, catdoc_error = _catdoc_text(content)
    if text:
        return text, ""

    text, antiword_error = _antiword_text(content)
    if text:
        return text, ""

    details = [item for item in (catdoc_error, antiword_error) if item]
    detail = "; ".join(details) or "текст не найден"
    return "", "не удалось прочитать .doc (Word 97-2003): " + detail


def _catdoc_text(content: bytes) -> tuple[str, str]:
    exe = shutil.which("catdoc")
    if not exe:
        return "", "catdoc не установлен"
    try:
        process = subprocess.run(
            [exe, "-d", "utf-8"],
            input=content,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", "catdoc: " + str(exc)

    text = process.stdout.decode("utf-8", errors="replace").strip()
    if process.returncode == 0 and text:
        return text, ""
    error = process.stderr.decode("utf-8", errors="replace").strip()
    return "", "catdoc: " + (error or "текст не извлечён")


def _antiword_text(content: bytes) -> tuple[str, str]:
    exe = shutil.which("antiword")
    if not exe:
        return "", "antiword не установлен"

    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as handle:
            handle.write(content)
            path = Path(handle.name)
        process = subprocess.run(
            [exe, "-m", "UTF-8.txt", str(path)],
            capture_output=True,
            timeout=60,
        )
        text = process.stdout.decode("utf-8", errors="replace").strip()
        if process.returncode == 0 and text:
            return text, ""
        error = process.stderr.decode("utf-8", errors="replace").strip()
        return "", "antiword: " + (error or "текст не извлечён")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", "antiword: " + str(exc)
    finally:
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass


def _rtf_text(content: bytes) -> str:
    """Достаёт текст из RTF: без внешних программ и без потери кириллицы."""
    raw = content.decode("cp1251", errors="replace")
    out: list[str] = []
    stack: list[int] = []
    skip = 0
    index = 0
    length = len(raw)

    while index < length:
        char = raw[index]

        if char == "{":
            stack.append(skip)
            index += 1
            if index < length and raw[index] == "\\":
                j = index + 1
                if j < length and raw[j] == "*":
                    j += 1
                start = j
                while j < length and raw[j].isalpha():
                    j += 1
                if raw[start:j].lower() in _SKIP_DESTINATIONS:
                    skip += 1
            continue

        if char == "}":
            if stack:
                skip = stack.pop()
            index += 1
            continue

        if char == "\\":
            nxt = raw[index + 1] if index + 1 < length else ""
            if nxt in "\\{}":
                if not skip:
                    out.append(nxt)
                index += 2
                continue

            if nxt == "'":
                try:
                    byte = int(raw[index + 2 : index + 4], 16)
                except ValueError:
                    index += 2
                    continue
                if not skip:
                    out.append(bytes([byte]).decode("cp1251", errors="replace"))
                index += 4
                continue

            j = index + 1
            start = j
            while j < length and raw[j].isalpha():
                j += 1
            word = raw[start:j].lower()

            number_start = j
            if j < length and raw[j] == "-":
                j += 1
            while j < length and raw[j].isdigit():
                j += 1
            argument = raw[number_start:j]

            if j < length and raw[j] == " ":
                j += 1

            if not skip:
                if word in ("par", "line", "sect"):
                    out.append("\n")
                elif word == "tab":
                    out.append("\t")
                elif word == "u" and argument:
                    try:
                        code = int(argument)
                    except ValueError:
                        code = 0
                    if code < 0:
                        code += 65536
                    if code:
                        out.append(chr(code))

            # После \uN в RTF идёт символ-заменитель (обычно «?») — пропускаем.
            if word == "u" and j < length and raw[j] == "?":
                j += 1

            index = j
            continue

        if char in "\r\n":
            index += 1
            continue

        if not skip:
            out.append(char)
        index += 1

    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _odt_text(content: bytes) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            xml = archive.read("content.xml")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        return "", "не удалось открыть .odt: " + str(exc)

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return "", "не удалось разобрать .odt: " + str(exc)

    lines: list[str] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in ("p", "h"):
            text = "".join(element.itertext()).strip()
            if text:
                lines.append(text)
    return "\n".join(lines), ""


def _html_text(content: str) -> str:
    content = re.sub(r"(?is)<(script|style).*?</\1>", " ", content)
    content = re.sub(r"(?s)<[^>]+>", " ", content)
    content = html_module.unescape(content)
    return re.sub(r"\s+", " ", content).strip()


def _looks_like_text(content: bytes) -> bool:
    sample = content[:4096]
    if b"\x00" in sample:
        return False
    for encoding in ("utf-8", "cp1251"):
        try:
            sample.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")
