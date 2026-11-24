"""SMB-загрузка, SCANNED и очередь архива (без живой базы и шары)."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import build_email, make_config

from processor import archive as archive_module
from processor import scanner as scanner_module
from processor.archive import SmbArchive
from processor.config import AppConfig
from processor.mail.parser import parse_eml
from processor.scanner import ArchiveWorker
from processor.spool import Spool
from processor.state import ProcessedState


def make_cfg(workdir):
    return AppConfig.load(
        make_config(
            workdir,
            {
                "archive": {
                    "enabled": "true",
                    "server": "srv-sadko-db",
                    "share": "SadkoAI",
                    "username": "sfs@asup.local",
                    "password": "secret",
                    "domain": "ASUP",
                }
            },
        )
    )


def make_mail():
    raw = build_email(
        attachments=[
            ("Счёт 45.pdf", "application/pdf", b"%PDF"),
            ("акт.docx", "application/octet-stream", b"PK"),
        ]
    )
    return parse_eml(raw)


def test_smb_upload_builds_commands_and_auth(monkeypatch, workdir: Path):
    monkeypatch.setattr(archive_module.tempfile, "tempdir", str(workdir))
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        auth = Path(command[command.index("-A") + 1])
        captured["auth"] = auth.read_text(encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="putting file x as y", stderr="")

    monkeypatch.setattr("processor.archive.subprocess.run", run)
    cfg = make_cfg(workdir)
    archive = SmbArchive(cfg.archive)

    path = archive.upload(b"data", "FILES/178", "S101.pdf")

    assert path == "FILES/178/S101.pdf"
    assert "mkdir" in captured["input"] and 'mkdir "FILES"' in captured["input"]
    assert 'cd "178"' in captured["input"]
    assert 'put ' in captured["input"] and '"S101.pdf"' in captured["input"]
    assert "username = sfs@asup.local" in captured["auth"]
    assert archive.doc_path("FILES/178", "S101.pdf") == "FILES\\178\\S101.pdf"


class FakeArchive:
    def __init__(self, cfg):
        self.cfg = cfg
        self.uploads = []
        self.fail_on = ""

    def upload(self, content, remote_dir, remote_name):
        if self.fail_on and self.fail_on in remote_name:
            raise RuntimeError("NT_STATUS_ACCESS_DENIED")
        self.uploads.append((remote_dir, remote_name, content))
        return remote_dir + "/" + remote_name

    def download(self, remote_path, local_path):
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self.payload)

    def list_remote(self, remote_dir):
        return []

    def doc_path(self, remote_dir, remote_name):
        return remote_dir.replace("/", "\\") + "\\" + remote_name


class FakeStore:
    next_id = 100
    inserted = []
    deleted = []
    committed = 0

    def __init__(self, *_args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def reserve_id(self):
        FakeStore.next_id += 1
        return FakeStore.next_id

    def insert_scan(self, scan_id, card_id, who, doc_path):
        FakeStore.inserted.append((scan_id, card_id, who, doc_path))

    def commit(self):
        FakeStore.committed += 1

    def rollback(self):
        pass

    def delete_scan(self, scan_id):
        FakeStore.deleted.append(scan_id)


@pytest.fixture()
def fake_parts(monkeypatch):
    FakeStore.next_id = 100
    FakeStore.inserted = []
    FakeStore.deleted = []
    FakeStore.committed = 0
    monkeypatch.setattr(scanner_module, "SmbArchive", FakeArchive)
    monkeypatch.setattr(scanner_module, "ScannedStore", FakeStore)


def test_archive_letter_writes_eml_and_attachments(workdir: Path, fake_parts):
    cfg = make_cfg(workdir)
    state = ProcessedState(cfg.processing.state_path)
    worker = ArchiveWorker(cfg, state)
    mail = make_mail()
    key = "imap|inbox|INBOX|id@test"
    state.mark_registered(key, message_id="id@test", uid="1", card_id=178)

    outcome = worker.archive_letter(mail, 178, key)

    assert outcome.ok
    assert outcome.uploaded == 3
    names = [name for _dir, name, _content in worker.archive.uploads]
    assert names[0].endswith(".eml")
    assert names[1].endswith(".pdf")
    assert names[2].endswith(".docx")
    assert all(name.startswith("S") for name in names)
    assert [item[3] for item in FakeStore.inserted][0].startswith("FILES\\178\\S")
    rows = state.files_for(key)
    assert [row["status"] for row in rows] == ["uploaded"] * 3


def test_archive_failure_keeps_row_for_retry(workdir: Path, fake_parts):
    cfg = make_cfg(workdir)
    state = ProcessedState(cfg.processing.state_path)
    worker = ArchiveWorker(cfg, state)
    mail = make_mail()
    key = "imap|inbox|INBOX|id@test"
    state.mark_registered(key, message_id="id@test", uid="1", card_id=178)
    worker.archive.fail_on = ".pdf"

    outcome = worker.archive_letter(mail, 178, key)

    assert outcome.failed == 1
    statuses = [row["status"] for row in state.files_for(key)]
    assert statuses.count("failed") == 1
    assert len(FakeStore.deleted) == 1


def test_retry_archive_uses_spool(workdir: Path, fake_parts):
    cfg = make_cfg(workdir)
    state = ProcessedState(cfg.processing.state_path)
    worker = ArchiveWorker(cfg, state)
    mail = make_mail()
    key = "imap|inbox|INBOX|id@test"
    state.mark_registered(key, message_id="id@test", uid="1", card_id=178)
    worker.spool.save(key, mail.raw)
    worker.archive.fail_on = ".pdf"
    worker.archive_letter(mail, 178, key)
    worker.archive.fail_on = ""

    outcome = worker.retry_files()

    assert outcome.ok
    assert outcome.uploaded == 1
    assert all(row["status"] == "uploaded" for row in state.files_for(key))


def test_spool_roundtrip(workdir: Path):
    spool = Spool(workdir / "spool")
    spool.save("key", b"letter")
    assert spool.load("key") == b"letter"
    assert spool.exists("key")
    spool.delete("key")
    assert not spool.exists("key")

def test_ensure_spool_downloads_eml_from_smb(workdir: Path, fake_parts):
    cfg = make_cfg(workdir)
    state = ProcessedState(cfg.processing.state_path)
    worker = ArchiveWorker(cfg, state)
    mail = make_mail()
    key = "imap|inbox|INBOX|id@test"
    state.mark_registered(key, message_id="id@test", uid="1", card_id=178)
    files = scanner_module.mail_files(mail)
    state.enqueue_files(key, 178, files)
    first = state.files_for(key)[0]
    state.mark_file(
        first["id"], "uploaded", remote_path="FILES/178/S111.eml", doc_path="FILES\\178\\S111.eml"
    )
    worker.archive.payload = mail.raw

    raw = worker.ensure_spool(key, 178)

    assert raw == mail.raw
    assert worker.spool.exists(key)
