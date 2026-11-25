"""Загрузка файлов на SMB-шару через smbclient.

Новая схема (как в test_save_file2.py): каждый файл лежит в
FILES/<CARD.ID>/S<SCANNED.ID><ext>, а строка SCANNED создаётся отдельно.
Модуль отвечает только за передачу байтов; именование и транзакция — в
processor.scanner.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .config import ArchiveConfig
from .errors import ArchiveError

logger = logging.getLogger(__name__)

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


class SmbArchive:
    """Передача файлов на SMB-шару и проверка доступа."""

    def __init__(self, cfg: ArchiveConfig) -> None:
        self.cfg = cfg

    def describe(self) -> str:
        return "//" + self.cfg.server + "/" + self.cfg.share

    def check(self) -> str:
        self._run(["ls"])
        return self.describe()

    def download(self, remote_path: str, local_path: Path) -> None:
        """Скачивает файл с шары (нужно для восстановления spool)."""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        remote = remote_path.replace("\\", "/").strip("/")
        self._run(
            ["get " + _quote(remote) + " " + _quote(str(local_path))]
        )
        if not local_path.is_file():
            raise ArchiveError(
                "smbclient не создал файл " + str(local_path) + " (" + remote + ")"
            )

    def list_remote(self, remote_dir: str) -> list[str]:
        """Список имён файлов в каталоге шары."""
        remote = remote_dir.replace("\\", "/").strip("/")
        output = self._run(["ls " + _quote(remote)])
        names: list[str] = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] in (".", ".."):
                continue
            # Строка smbclient: имя, атрибуты, размер, дата.
            if len(parts) >= 3 and parts[1].isalpha():
                names.append(parts[0])
        return names

    def upload(
        self, content: bytes, remote_dir: str, remote_name: str
    ) -> str:
        """Кладёт файл в share/remote_dir/remote_name, возвращает путь в шаре."""
        stage = _new_stage_dir()
        try:
            local = stage / "upload.bin"
            local.write_bytes(content)
            commands: list[str] = []
            for part in _path_parts(remote_dir):
                commands.append("mkdir " + _quote(part))
                commands.append("cd " + _quote(part))
            commands.append("put " + _quote(str(local)) + " " + _quote(remote_name))
            self._run(commands)
        except OSError as exc:
            raise ArchiveError(
                "Не удалось подготовить файл для " + self.describe() + ": " + str(exc)
            ) from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return remote_dir.strip("/") + "/" + remote_name

    def doc_path(self, remote_dir: str, remote_name: str) -> str:
        return _doc_path(remote_dir, remote_name)

    # ---------------------------------------------------------------- внутреннее

    def _run(self, commands: list[str]) -> str:
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
            return output
        finally:
            shutil.rmtree(auth_dir, ignore_errors=True)

    def _command_line(self, auth_path: Path) -> list[str]:
        parts = shlex.split(self.cfg.smbclient)
        if not parts:
            raise ArchiveError("[archive] smbclient: пустая команда")
        parts.append("//" + self.cfg.server + "/" + self.cfg.share)
        parts.extend(["-A", str(auth_path)])
        if not self.cfg.password:
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


class UncArchive:
    r"""Архив через обычный UNC-путь Windows: \\server\share\FILES\...

    Не требует smbclient: служба работает под учётной записью, у которой есть
    доступ к шаре. На Linux этот режим не используется.
    """

    def __init__(self, cfg: ArchiveConfig, root: Path | None = None) -> None:
        self.cfg = cfg
        self.root = root if root is not None else Path("//" + cfg.server + "/" + cfg.share)

    def describe(self) -> str:
        return chr(92) * 2 + self.cfg.server + chr(92) + self.cfg.share

    def check(self) -> str:
        try:
            folder = self.root / (self.cfg.files_root or "FILES")
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".mailworker-write-test"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise ArchiveError(
                "Нет доступа к " + self.describe() + ": " + str(exc)
            ) from exc
        return self.describe()

    def upload(self, content: bytes, remote_dir: str, remote_name: str) -> str:
        target = self.root / remote_dir.replace("\\", "/") / remote_name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except OSError as exc:
            raise ArchiveError(
                "Не удалось записать " + str(target) + ": " + str(exc)
            ) from exc
        return remote_dir.strip("/") + "/" + remote_name

    def download(self, remote_path: str, local_path: Path) -> None:
        source = self.root / remote_path.replace("\\", "/")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, local_path)
        except OSError as exc:
            raise ArchiveError(
                "Не удалось прочитать " + str(source) + ": " + str(exc)
            ) from exc

    def list_remote(self, remote_dir: str) -> list[str]:
        folder = self.root / remote_dir.replace("\\", "/")
        try:
            return [item.name for item in folder.iterdir() if item.is_file()]
        except OSError as exc:
            raise ArchiveError(
                "Не удалось прочитать каталог " + str(folder) + ": " + str(exc)
            ) from exc

    def doc_path(self, remote_dir: str, remote_name: str) -> str:
        return _doc_path(remote_dir, remote_name)


def build_archive(cfg: ArchiveConfig):
    """Выбирает транспорт: UNC на Windows, smbclient на Linux."""
    if cfg.transport == "unc" or (cfg.transport == "auto" and os.name == "nt"):
        return UncArchive(cfg)
    return SmbArchive(cfg)


def _doc_path(remote_dir: str, remote_name: str) -> str:
    relative = remote_dir.replace("/", "\\").strip("\\")
    relative = (relative + "\\" + remote_name) if relative else remote_name
    return relative


def extension_for(mime_type: str, filename: str | None) -> str:
    """Расширение файла: из имени, иначе по MIME, иначе .bin."""
    extension = os.path.splitext(filename or "")[1].lower()
    if extension:
        return extension
    mime = (mime_type or "").lower()
    if mime in _EXTENSIONS:
        return _EXTENSIONS[mime]
    guessed = mimetypes.guess_extension(mime) if mime else None
    return guessed or ".bin"


def _path_parts(value: str) -> list[str]:
    parts = (value or "").replace("\\", "/").split("/")
    return [part for part in parts if part and part not in (".", "..")]


def _quote(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


def _new_stage_dir() -> Path:
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
