r"""Сохранение письма и вложений в FILES/<CARD.ID>/ + запись в SCANNED.

Порядок на каждый файл повторяет test_save_file2.py:

  1. зарезервировать SCANNED.ID из генератора;
  2. вставить строку SCANNED (MASTERID = CARD.ID, DOC_PATH = FILES\<ID>\S<ID><ext>);
  3. загрузить файл на SMB;
  4. commit; при ошибке — rollback и компенсирующий DELETE.

Ошибка одного файла не мешает остальным: письмо уже зарегистрировано, а строка
очереди остаётся failed для --retry-archive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .archive import build_archive, extension_for
from .config import AppConfig
from .errors import ArchiveError, StorageError
from .journal.scanned import ScannedStore
from .mail.parser import parse_eml
from .models import MailMessage
from .state import ProcessedState
from .spool import Spool

logger = logging.getLogger(__name__)


@dataclass
class ArchiveOutcome:
    uploaded: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


def mail_files(mail: MailMessage) -> list[dict]:
    """Файлы письма в порядке очереди: сначала .eml, затем вложения."""
    files: list[dict] = [
        {
            "index": 0,
            "kind": "email",
            "ext": ".eml",
            "filename": "letter.eml",
            "content": mail.raw,
        }
    ]
    for index, attachment in enumerate(mail.attachments, start=1):
        files.append(
            {
                "index": index,
                "kind": "attachment",
                "ext": extension_for(attachment.mime_type, attachment.filename),
                "filename": attachment.filename or "",
                "content": attachment.content,
            }
        )
    return files


class ArchiveWorker:
    """Очередь файлов + SMB + SCANNED для одного письма."""

    def __init__(self, cfg: AppConfig, state: ProcessedState, archive=None) -> None:
        self.cfg = cfg
        self.state = state
        self.archive = archive or build_archive(cfg.archive)
        self.spool = Spool(cfg.processing.state_path.parent / "spool")

    def archive_letter(self, mail: MailMessage, card_id: int, key: str) -> ArchiveOutcome:
        files = mail_files(mail)
        self.state.enqueue_files(key, card_id, files)
        queue = {item["index"]: item for item in self.state.files_for(key)}
        outcome = ArchiveOutcome()

        for item in files:
            row = queue.get(item["index"])
            if row is None or row["status"] == "uploaded":
                outcome.uploaded += 1 if row and row["status"] == "uploaded" else 0
                continue
            try:
                self._upload_one(item, row, mail, card_id, key)
                outcome.uploaded += 1
            except Exception as exc:  # noqa: BLE001 - один файл не должен рвать письмо
                outcome.failed += 1
                outcome.errors.append((item.get("filename") or item["kind"]) + ": " + str(exc))
                self.state.mark_file(row["id"], "failed", error=str(exc))
                logger.warning(
                    "Не удалось сохранить файл %s (CARD.ID=%s): %s",
                    item.get("filename") or item["kind"],
                    card_id,
                    exc,
                )
        return outcome

    def _upload_one(
        self, item: dict, row: dict, mail: MailMessage, card_id: int, key: str
    ) -> None:
        remote_dir = self.cfg.archive.files_root + "/" + str(card_id)
        who = self.cfg.mail.username or "mailworker"

        with ScannedStore(self.cfg.database, self.cfg.archive) as store:
            scan_id = store.reserve_id()
            remote_name = "S" + str(scan_id) + item["ext"]
            doc_path = self.archive.doc_path(remote_dir, remote_name)
            store.insert_scan(scan_id, int(card_id), who, doc_path)
            try:
                remote_path = self.archive.upload(
                    item["content"], remote_dir, remote_name
                )
            except Exception:
                store.rollback()
                store.delete_scan(scan_id)
                raise
            store.commit()

        self.state.mark_file(
            row["id"],
            "uploaded",
            scan_id=scan_id,
            remote_path=remote_path,
            doc_path=doc_path,
        )
        logger.debug(
            "Файл сохранён: %s -> %s (SCANNED.ID=%s)",
            item.get("filename") or item["kind"],
            doc_path,
            scan_id,
        )

    # ------------------------------------------------------------- повтор/поиск

    def ensure_spool(self, key: str, card_id: int | None) -> bytes | None:
        """Если spool потерян, скачивает .eml с шары по очереди/каталогу CARD."""
        raw = self.spool.load(key)
        if raw is not None:
            return raw
        if not self.cfg.archive.enabled or card_id in (None, ""):
            return None
        remote = self._remote_email(key, int(card_id))
        if remote is None:
            return None
        local = self.spool.path(key).with_suffix(".download")
        try:
            self.archive.download(remote, local)
            raw = local.read_bytes()
            parse_eml(raw)
            self.spool.save(key, raw)
            logger.debug("Spool восстановлен с SMB: %s", remote)
            return raw
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось скачать .eml с SMB: %s", remote)
            return None
        finally:
            try:
                local.unlink()
            except OSError:
                pass

    def _remote_email(self, key: str, card_id: int) -> str | None:
        rows = self.state.files_for(key)
        for row in rows:
            if row.get("index") == 0 and row.get("remote_path"):
                return str(row["remote_path"])
        remote_dir = self.cfg.archive.files_root + "/" + str(card_id)
        try:
            names = self.archive.list_remote(remote_dir)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось получить список %s", remote_dir)
            return None
        for name in names:
            if name.lower().endswith(".eml"):
                return remote_dir + "/" + name
        return None

    def retry_files(self, limit: int = 100) -> ArchiveOutcome:
        """Повторяет pending/failed файлы из spool (CARD не меняется)."""
        outcome = ArchiveOutcome()
        for row in self.state.failed_files(limit):
            key = row["dedup_key"]
            mail = self._load_mail(key, row.get("card_id"))
            if mail is None:
                outcome.failed += 1
                outcome.errors.append(
                    "нет spool для " + str(row.get("card_id") or key)
                )
                self.state.mark_file(row["id"], "failed", error="нет spool с исходником")
                continue
            files = {item["index"]: item for item in mail_files(mail)}
            item = files.get(row["index"])
            if item is None:
                outcome.failed += 1
                self.state.mark_file(row["id"], "failed", error="файл отсутствует в письме")
                continue
            if row.get("card_id") in (None, ""):
                outcome.failed += 1
                self.state.mark_file(row["id"], "failed", error="нет CARD.ID")
                continue
            try:
                self._upload_one(item, row, mail, int(row["card_id"]), key)
                outcome.uploaded += 1
            except Exception as exc:  # noqa: BLE001
                outcome.failed += 1
                outcome.errors.append(str(exc))
                self.state.mark_file(row["id"], "failed", error=str(exc))
        return outcome

    def _load_mail(self, key: str, card_id=None) -> MailMessage | None:
        raw = self.spool.load(key)
        if raw is None:
            raw = self.ensure_spool(key, card_id)
        if raw is None:
            return None
        try:
            return parse_eml(raw)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось разобрать письмо из spool: %s", key)
            return None
