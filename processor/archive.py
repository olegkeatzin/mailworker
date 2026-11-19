"""Архив обработанных писем: локальная папка или SMB-шара.

Раскладка одинаковая в обоих режимах: папка на каждую запись журнала, имя —
только CARD.ID (например «142»), без дополнительных подпапок. В папке лежит
исходное письмо и вложения отдельными файлами; вложения нумеруются по порядку,
чтобы длинные исходные имена не попадали в путь:

    //srv-sadko-db/SadkoAI/142/
        letter.eml
        1.pdf
        2.docx

Локальный режим пишет напрямую в каталог. Режим smb повторяет пример со
smbclient: файлы складываются во временный каталог, затем одной сессией
smbclient создаются подкаталоги (mkdir/cd) и выполняется put. Пароль передаётся
через временный файл учётных данных (-A), а не в командной строке.

Ошибка записи архива не отменяет регистрацию: запись в журнале уже сделана.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from .config import ArchiveConfig
from .errors import ArchiveError
from .models import CardData, InsertedCard, MailMessage

logger = logging.getLogger(__name__)

#: Имя файла с исходным письмом внутри папки записи.
LETTER_FILENAME = "letter.eml"

#: Ограничение длины имени файла: 255 байт — предел большинства файловых систем,
#: а кириллица в UTF-8 занимает два байта на символ. Берём с запасом.
MAX_NAME_LENGTH = 100

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Расширения для типов, у которых в письме не оказалось имени файла.
_EXTENSIONS = {
    "application/pdf": ".pdf",
    "message/rfc822": ".eml",
    "application/zip": ".zip",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    "image/bmp": ".bmp",
    "text/plain": ".txt",
    "text/html": ".html",
}


class Archive(Protocol):
    """Куда именно сохранять письмо: локальный диск или SMB-шара."""

    def describe(self) -> str:
        """Человекочитаемый адрес архива для логов и --check."""

    def save(self, mail: MailMessage, folder: str) -> str:
        """Сохраняет письмо и вложения; возвращает адрес папки."""

    def check(self) -> str:
        """Проверяет доступность архива; возвращает адрес или бросает ArchiveError."""


class LocalArchive:
    """Архив в локальном каталоге."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def describe(self) -> str:
        return str(self.root)

    def save(self, mail: MailMessage, folder: str) -> str:
        try:
            path = save_letter(mail, self.root, folder)
        except OSError as exc:
            raise ArchiveError(
                "Не удалось сохранить письмо в " + str(self.root) + ": " + str(exc)
            ) from exc
        return str(path)

    def check(self) -> str:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / ".mailworker-write-test"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise ArchiveError(
                "Нет доступа на запись в папку архива "
                + str(self.root)
                + ": "
                + str(exc)
            ) from exc
        return str(self.root)


class SmbArchive:
    """Архив на SMB-шаре через smbclient (как в примере со скриптом)."""

    def __init__(self, cfg: ArchiveConfig) -> None:
        self.cfg = cfg

    def describe(self) -> str:
        base = "//" + self.cfg.server + "/" + self.cfg.share
        if self.cfg.path:
            base += "/" + self.cfg.path
        return base

    def save(self, mail: MailMessage, folder: str) -> str:
        stage = _new_stage_dir()
        try:
            staged = _stage_files(mail, stage)
            self._run(self._commands(folder, staged))
        except OSError as exc:
            raise ArchiveError(
                "Не удалось подготовить файлы для отправки на "
                + self.describe()
                + ": "
                + str(exc)
            ) from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return self.describe() + "/" + folder

    def check(self) -> str:
        commands: list[str] = []
        parts = _path_parts(self.cfg.path)
        for part in parts:
            commands.append("mkdir " + _quote(part))
            commands.append("cd " + _quote(part))
        commands.append("ls")
        self._run(commands)
        return self.describe()

    # ---------------------------------------------------------------- внутреннее

    def _commands(
        self, folder: str, staged: list[tuple[Path, str]]
    ) -> list[str]:
        """Команды smbclient: создать подкаталоги и загрузить файлы."""
        commands: list[str] = []
        for part in _path_parts(self.cfg.path):
            commands.append("mkdir " + _quote(part))
            commands.append("cd " + _quote(part))
        commands.append("mkdir " + _quote(folder))
        commands.append("cd " + _quote(folder))
        for local_path, remote_name in staged:
            commands.append("put " + _quote(str(local_path)) + " " + _quote(remote_name))
        return commands

    def _run(self, commands: list[str]) -> None:
        auth_dir = _new_stage_dir()
        auth_path = self._write_auth_file(auth_dir)
        try:
            command = self._command_line(auth_path)
            try:
                process = subprocess.run(
                    command,
                    input="\n".join(commands) + "\n",
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.cfg.timeout,
                )
            except FileNotFoundError as exc:
                raise ArchiveError(
                    "smbclient не найден («"
                    + self.cfg.smbclient
                    + "»). Установите пакет smbclient или укажите путь в [archive] smbclient"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise ArchiveError(
                    "smbclient не ответил за "
                    + str(self.cfg.timeout)
                    + " с при обращении к "
                    + self.describe()
                ) from exc

            output = ((process.stdout or "") + "\n" + (process.stderr or "")).strip()
            for line in output.splitlines():
                # mkdir существующего каталога — штатная ситуация при повторной
                # записи; остальные NT_STATUS означают отказ сервера.
                if "NT_STATUS" in line and "OBJECT_NAME_COLLISION" not in line:
                    raise ArchiveError(
                        "SMB-сервер отказал (" + self.describe() + "): " + line.strip()
                    )
            if process.returncode != 0:
                raise ArchiveError(
                    "smbclient завершился с кодом "
                    + str(process.returncode)
                    + ": "
                    + (output[-500:] if output else "вывод пуст")
                )
            logger.debug("smbclient: %s", output[-1000:] if output else "без вывода")
        finally:
            shutil.rmtree(auth_dir, ignore_errors=True)

    def _command_line(self, auth_path: Path) -> list[str]:
        parts = shlex.split(self.cfg.smbclient)
        if not parts:
            raise ArchiveError("[archive] smbclient: пустая команда")
        parts.append("//" + self.cfg.server + "/" + self.cfg.share)
        parts.extend(["-A", str(auth_path)])
        if not self.cfg.password:
            # Без пароля smbclient иначе может спросить его интерактивно.
            parts.append("-N")
        return parts

    def _write_auth_file(self, folder: Path) -> Path:
        lines = [
            "username = " + self.cfg.username,
            "password = " + self.cfg.password,
        ]
        if self.cfg.domain:
            lines.append("domain = " + self.cfg.domain)
        path = folder / "smb.creds"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path


def build_archive(cfg: ArchiveConfig) -> Archive:
    """Выбирает архив по настройке [archive] mode."""
    if cfg.mode == "smb":
        return SmbArchive(cfg)
    return LocalArchive(cfg.dir)


def folder_name(
    inserted: InsertedCard | None, card: CardData | None, mail: MailMessage
) -> str:
    """Имя папки записи: только CARD.ID.

    Например, запись с ID 142 даёт папку «142». INTINDEX и имена вложений в
    путь не попадают: по номеру записи в журнале всегда можно найти архив.

    Если идентификатора записи нет (сервис журнала не вернул id), остаётся
    Message-ID письма — другого стабильного имени нет.
    """
    if inserted is not None and inserted.card_id not in (None, ""):
        return _safe_component(str(inserted.card_id))
    return _safe_component(mail.message_id or mail.uid or "mail")


def save_letter(mail: MailMessage, base_dir: Path, folder: str) -> Path:
    """Сохраняет письмо и вложения в base_dir/folder и возвращает папку.

    Существующие файлы не затираются: совпадение имени даёт суффикс -2, -3 и т. д.
    """
    target = base_dir / folder
    target.mkdir(parents=True, exist_ok=True)

    letter_path = _unique_path(target, LETTER_FILENAME)
    letter_path.write_bytes(mail.raw)

    used = {letter_path.name.lower()}
    for index, attachment in enumerate(mail.attachments, start=1):
        name = numbered_filename(index, attachment.mime_type, attachment.filename)
        path = _unique_path(target, name, used)
        path.write_bytes(attachment.content)
        used.add(path.name.lower())

    return target


def numbered_filename(index: int, mime_type: str, filename: str | None) -> str:
    """Имя вложения в архиве: порядковый номер и расширение исходного файла.

    Полное исходное имя не используется намеренно: длинные имена с пробелами и
    кириллицей создают проблемы на SMB-шарах и в путях Windows. Расширение
    сохраняется, поэтому формат файла остаётся понятным.
    """
    extension = os.path.splitext(filename or "")[1].lower()
    if not extension:
        extension = _extension_for(mime_type, filename)
    return str(index) + extension


def _stage_files(mail: MailMessage, stage: Path) -> list[tuple[Path, str]]:
    """Готовит файлы во временном каталоге: письмо и вложения с безопасными именами."""
    used: set[str] = set()
    letter = stage / LETTER_FILENAME
    letter.write_bytes(mail.raw)
    used.add(letter.name.lower())
    staged: list[tuple[Path, str]] = [(letter, letter.name)]

    for index, attachment in enumerate(mail.attachments, start=1):
        name = numbered_filename(index, attachment.mime_type, attachment.filename)
        path = stage / name
        path.write_bytes(attachment.content)
        used.add(name.lower())
        staged.append((path, name))
    return staged


def _path_parts(value: str) -> list[str]:
    """Разбивает подпапку внутри шары на уровни, отбрасывая «.» и «..»."""
    parts = (value or "").replace("\\", "/").split("/")
    return [part for part in parts if part and part not in (".", "..")]


def _quote(value: str) -> str:
    """Кавычки для команды smbclient: имена с пробелами и кириллицей."""
    return '"' + value.replace('"', "") + '"'


def _new_stage_dir() -> Path:
    """Временный каталог для подготовки файлов и учётных данных.

    Не используем tempfile.mkdtemp: он создаёт каталог с правами 0700, и в
    ограниченных средах (Windows-служба, песочница) запись внутрь может быть
    запрещена. Обычный mkdir наследует права родителя и работает предсказуемо.
    """
    base = Path(tempfile.gettempdir())
    for _ in range(10):
        candidate = base / ("mailworker-" + uuid.uuid4().hex)
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise ArchiveError(
                "Не удалось создать временный каталог в " + str(base) + ": " + str(exc)
            ) from exc
        return candidate
    raise ArchiveError("Не удалось создать временный каталог в " + str(base))


def _extension_for(mime_type: str, filename: str | None) -> str:
    """Расширение по MIME, если в имени файла его нет."""
    mime = (mime_type or "").lower()
    if mime in _EXTENSIONS:
        return _EXTENSIONS[mime]
    guessed = mimetypes.guess_extension(mime) if mime else None
    if guessed:
        return guessed
    _, original = os.path.splitext(filename or "")
    return original if original else ".bin"


def _safe_component(value: str) -> str:
    """Имя папки без разделителей пути и запрещённых символов."""
    cleaned = _INVALID_CHARS.sub("_", value or "").strip(" .")
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    return cleaned[:MAX_NAME_LENGTH] or "mail"


def _unique_path(folder: Path, name: str, used: set[str] | None = None) -> Path:
    """Свободный путь: при совпадении добавляет -2, -3 и т. д."""
    taken = used or set()
    candidate = folder / name
    if not candidate.exists() and name.lower() not in taken:
        return candidate

    stem, extension = os.path.splitext(name)
    counter = 2
    while True:
        candidate = folder / (stem + "-" + str(counter) + extension)
        if not candidate.exists() and candidate.name.lower() not in taken:
            return candidate
        counter += 1
