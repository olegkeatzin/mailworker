"""Spool исходных писем для повторной архивации и пересканирования.

Служба сохраняет сырой .eml до записи в журнал. Если SMB/SCANNED или разбор
упали, исходник остаётся на диске, и письмо можно обработать заново, не читая
ящик. После успешной архивации папка письма удаляется.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class Spool:
    """Хранилище .eml по ключу письма (protocol|username|folder|message-id)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.root / digest / "letter.eml"

    def save(self, key: str, raw: bytes) -> Path:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".eml.tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
        return path

    def load(self, key: str) -> bytes | None:
        path = self.path(key)
        try:
            return path.read_bytes()
        except OSError:
            return None

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def delete(self, key: str) -> None:
        path = self.path(key)
        try:
            path.unlink()
        except OSError:
            pass
        try:
            path.parent.rmdir()
        except OSError:
            pass
