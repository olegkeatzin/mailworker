"""Подготовка изображений к отправке в модель (data-URL base64).

Модель получает картинку внутри JSON-запроса, поэтому размер важен: скан на
6000 точек по ширине раздувает запрос в разы и не улучшает распознавание.
При наличии Pillow изображение уменьшается до разумной стороны; без Pillow
картинка уходит как есть, если её тип поддерживается моделью.
"""

from __future__ import annotations

import base64
import io

#: Типы, которые принимает большинство vision-моделей без перекодирования.
SUPPORTED_MIME = ("image/png", "image/jpeg", "image/webp", "image/gif")

_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def guess_mime(filename: str, mime_type: str) -> str:
    """MIME по расширению, если сервер прислал обобщённый application/octet-stream."""
    mime = (mime_type or "").lower()
    if mime and mime != "application/octet-stream":
        return mime
    name = (filename or "").lower()
    for ext, known in _EXT_TO_MIME.items():
        if name.endswith(ext):
            return known
    return mime or "application/octet-stream"


def image_to_data_url(
    content: bytes,
    mime_type: str,
    *,
    max_side: int = 1600,
    max_bytes: int = 4_000_000,
) -> str | None:
    """Готовит картинку к vision-запросу.

    Возвращает data-URL или None, если тип не поддержан и Pillow не установлен.
    """
    mime = (mime_type or "").lower()
    supported_directly = mime in SUPPORTED_MIME and len(content) <= max_bytes
    if supported_directly:
        try:
            from PIL import Image  # noqa: F401

            return _resize_and_encode(content, mime, max_side, max_bytes, Image)
        except ImportError:
            return _to_data_url(content, mime)
    try:
        from PIL import Image
    except ImportError:
        return None
    return _resize_and_encode(content, mime, max_side, max_bytes, Image)


def _resize_and_encode(content: bytes, mime: str, max_side: int, max_bytes: int, Image) -> str | None:
    """Уменьшает изображение и перекодирует в PNG/JPEG.

    BMP/TIFF модель не примет — Pillow переводит их в PNG. GIF оставляем как есть,
    если он небольшой: анимация обычно не нужна, но и портить её незачем.
    """
    try:
        with Image.open(io.BytesIO(content)) as img:
            img.load()
            width, height = img.size
            needs_resize = max(width, height) > max_side
            target_mime = mime if mime in SUPPORTED_MIME else "image/png"
            if target_mime == "image/gif" and not needs_resize and len(content) <= max_bytes:
                return _to_data_url(content, target_mime)

            if needs_resize:
                scale = max_side / float(max(width, height))
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                img = img.resize(new_size, Image.LANCZOS)

            if target_mime in ("image/jpeg", "image/gif") and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buffer = io.BytesIO()
            if target_mime == "image/jpeg":
                img.save(buffer, format="JPEG", quality=85, optimize=True)
            elif target_mime == "image/webp":
                img.save(buffer, format="WEBP", quality=85)
            else:
                target_mime = "image/png"
                img.save(buffer, format="PNG", optimize=True)

            data = buffer.getvalue()
            if len(data) > max_bytes and target_mime == "image/png":
                # PNG после уменьшения всё ещё тяжёлый — JPEG заметно легче.
                buffer = io.BytesIO()
                img.convert("RGB").save(buffer, format="JPEG", quality=80, optimize=True)
                data = buffer.getvalue()
                target_mime = "image/jpeg"
            return _to_data_url(data, target_mime)
    except Exception:  # noqa: BLE001 - битая картинка не должна ронять разбор
        if mime in SUPPORTED_MIME:
            return _to_data_url(content, mime)
        return None


def _to_data_url(data: bytes, mime: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return "data:" + mime + ";base64," + encoded
