"""XLSX: таблицы в текст для промпта.

Порт xlsxToText из MailSense. Таблица реестра или счёта часто и есть документ,
поэтому её содержимое важно целиком, но с ограничениями: лист на 100 000 строк
не должен превращаться в мегабайт промпта.
"""

from __future__ import annotations

import io

MAX_ROWS_PER_SHEET = 200
MAX_COLUMNS = 30
MAX_CHARS = 40000


def xlsx_text(content: bytes, *, max_rows: int = MAX_ROWS_PER_SHEET, max_chars: int = MAX_CHARS) -> str:
    """Возвращает текстовое представление книги.

    read_only=True — чтобы большие книги не занимали память целиком;
    data_only=True — чтобы вместо формул приходили значения.
    """
    try:
        import openpyxl
    except ImportError:
        return ""

    try:
        book = openpyxl.load_workbook(
            io.BytesIO(content), read_only=True, data_only=True
        )
    except Exception:  # noqa: BLE001 - битый файл: не мешаем обработать письмо
        return ""

    chunks: list[str] = []
    try:
        for sheet in book.worksheets:
            rows: list[str] = []
            for index, row in enumerate(sheet.iter_rows(max_col=MAX_COLUMNS)):
                if index >= max_rows:
                    rows.append("... (строки после " + str(max_rows) + " не показаны)")
                    break
                # В read_only-режиме приходят объекты ячеек, а не значения.
                cells = []
                for cell in row:
                    value = getattr(cell, "value", None)
                    cells.append("" if value is None else str(value))
                while cells and not cells[-1].strip():
                    cells.pop()
                if any(cell.strip() for cell in cells):
                    rows.append(" | ".join(cells))
            if rows:
                chunks.append("=== Лист: " + sheet.title + " ===")
                chunks.extend(rows)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass

    text = "\n".join(chunks).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n... (таблица обрезана по размеру)"
    return text
