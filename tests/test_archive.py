"""Архив: имена папок, локальная запись и SMB через smbclient."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from processor import archive as archive_module
from processor.archive import (
    ArchiveError,
    LocalArchive,
    SmbArchive,
    build_archive,
    folder_name,
    safe_filename,
    save_letter,
)
from processor.config import ArchiveConfig
from processor.models import Attachment, CardData, InsertedCard, MailMessage


def make_mail(attachments: list[Attachment] | None = None, raw: bytes = b"raw") -> MailMessage:
    return MailMessage(
        uid="1",
        folder="INBOX",
        message_id="id@test",
        subject="Письмо",
        from_name="",
        from_address="",
        to_display="",
        date=None,
        body_text="",
        body_html="",
        attachments=attachments or [],
        raw=raw,
    )


def make_card(intindex: str = "вх") -> CardData:
    import datetime as dt

    return CardData(
        card_type=1,
        intindex=intindex,
        intdate=dt.date(2026, 11, 9),
        extindex="",
        extdate=None,
        sh_contents="",
        pages_doc=1,
        korresp="",
    )


def test_folder_is_intindex_plus_record_id():
    inserted = InsertedCard(card_id=137)
    assert folder_name(inserted, make_card(), make_mail()) == "вх137"


def test_folder_without_id_uses_intindex():
    card = make_card("вх")
    assert folder_name(InsertedCard(card_id=None), card, make_mail()) == "вх"
    assert folder_name(None, card, make_mail()) == "вх"


def test_folder_falls_back_to_message_id_and_is_sanitized():
    mail = make_mail()
    mail.message_id = "a/b:c@test"
    assert folder_name(None, make_card(intindex=""), mail) == "a_b_c@test"


def test_safe_filename_keeps_original_name():
    assert safe_filename("Счёт 45.pdf", "application/pdf", 1) == "Счёт 45.pdf"


def test_safe_filename_strips_paths_and_bad_chars():
    assert safe_filename("../evil?.txt", "text/plain", 1) == "evil_.txt"


def test_safe_filename_adds_extension_from_mime():
    assert safe_filename("скан", "application/pdf", 1) == "скан.pdf"


def test_safe_filename_for_missing_name():
    assert safe_filename(None, "image/png", 3) == "attachment-3.png"


def test_safe_filename_avoids_reserved_names():
    assert safe_filename("CON.txt", "text/plain", 1) == "_CON.txt"


def test_safe_filename_is_limited():
    name = safe_filename("ж" * 200 + ".pdf", "application/pdf", 1)
    assert len(name) <= 100
    assert name.endswith(".pdf")


def test_save_letter_writes_letter_and_attachments(workdir: Path):
    mail = make_mail(
        attachments=[
            Attachment(filename="Счёт 45.pdf", mime_type="application/pdf", content=b"%PDF"),
            Attachment(filename="акт.docx", mime_type="application/octet-stream", content=b"PK"),
        ],
        raw=b"raw-letter",
    )

    folder = save_letter(mail, workdir, "137")

    assert folder == workdir / "137"
    assert (folder / "letter.eml").read_bytes() == b"raw-letter"
    assert (folder / "Счёт 45.pdf").read_bytes() == b"%PDF"
    assert (folder / "акт.docx").read_bytes() == b"PK"


def test_save_letter_does_not_overwrite(workdir: Path):
    folder = workdir / "137"
    folder.mkdir()
    (folder / "letter.eml").write_bytes(b"old")
    (folder / "report.txt").write_bytes(b"old report")

    mail = make_mail(
        attachments=[Attachment(filename="report.txt", mime_type="text/plain", content=b"new")],
        raw=b"new letter",
    )
    save_letter(mail, workdir, "137")

    assert (folder / "letter.eml").read_bytes() == b"old"
    assert (folder / "letter-2.eml").read_bytes() == b"new letter"
    assert (folder / "report.txt").read_bytes() == b"old report"
    assert (folder / "report-2.txt").read_bytes() == b"new"


def test_attachment_named_like_letter_gets_suffix(workdir: Path):
    mail = make_mail(
        attachments=[
            Attachment(filename="letter.eml", mime_type="message/rfc822", content=b"inner")
        ]
    )

    folder = save_letter(mail, workdir, "137")

    assert (folder / "letter.eml").read_bytes() == b"raw"
    assert (folder / "letter-2.eml").read_bytes() == b"inner"


# ------------------------------------------------------------------ local / smb


def test_local_archive_saves_and_checks(workdir: Path):
    archive = LocalArchive(workdir / "share")
    mail = make_mail(
        attachments=[Attachment(filename="акт.pdf", mime_type="application/pdf", content=b"%PDF")],
        raw=b"letter",
    )

    where = archive.save(mail, "вх142")
    assert where == str(workdir / "share" / "вх142")
    assert (workdir / "share" / "вх142" / "letter.eml").read_bytes() == b"letter"
    assert (workdir / "share" / "вх142" / "акт.pdf").read_bytes() == b"%PDF"
    assert archive.check() == str(workdir / "share")


def test_build_archive_chooses_backend():
    assert isinstance(build_archive(ArchiveConfig()), LocalArchive)
    assert isinstance(build_archive(ArchiveConfig(mode="smb")), SmbArchive)


def _fake_smbclient(monkeypatch, *, returncode=0, stdout="", stderr=""):
    """Подменяет subprocess.run и запоминает команды, учётные данные и файлы."""
    captured: dict = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        auth_path = Path(command[command.index("-A") + 1])
        captured["auth"] = auth_path.read_text(encoding="utf-8")
        captured["files"] = {}
        for line in str(kwargs["input"]).splitlines():
            if line.startswith("put "):
                quoted = re.findall(r'"([^"]*)"', line)
                captured["files"][quoted[1]] = Path(quoted[0]).read_bytes()
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(archive_module.subprocess, "run", run)
    return captured


def test_smb_path_drops_parent_segments():
    assert archive_module._path_parts("../letters/./2026") == ["letters", "2026"]


def _smb_config(**overrides) -> ArchiveConfig:
    values = dict(
        mode="smb",
        server="srv-sadko-db",
        share="SadkoAI",
        path="letters/2026",
        username="sfs@asup.local",
        password="secret",
        domain="ASUP",
    )
    values.update(overrides)
    return ArchiveConfig(**values)


def test_smb_archive_uploads_letter_and_attachments(monkeypatch, workdir: Path):
    monkeypatch.setattr(archive_module.tempfile, "tempdir", str(workdir))
    captured = _fake_smbclient(monkeypatch)
    archive = SmbArchive(_smb_config())
    mail = make_mail(
        attachments=[
            Attachment(filename="Счёт 45.pdf", mime_type="application/pdf", content=b"%PDF"),
            Attachment(filename="Счёт 45.pdf", mime_type="application/pdf", content=b"dup"),
        ],
        raw=b"raw-letter",
    )

    where = archive.save(mail, "вх142")

    assert where == "//srv-sadko-db/SadkoAI/letters/2026/вх142"
    assert captured["command"][0] == "smbclient"
    assert "//srv-sadko-db/SadkoAI" in captured["command"]
    assert "-A" in captured["command"]
    assert "-N" not in captured["command"]
    assert "username = sfs@asup.local" in captured["auth"]
    assert "password = secret" in captured["auth"]
    assert "domain = ASUP" in captured["auth"]

    commands = captured["input"]
    assert 'mkdir "letters"' in commands
    assert 'cd "2026"' in commands
    assert 'mkdir "вх142"' in commands
    assert 'cd "вх142"' in commands
    assert captured["files"]["letter.eml"] == b"raw-letter"
    assert captured["files"]["Счёт 45.pdf"] == b"%PDF"
    assert captured["files"]["Счёт 45-2.pdf"] == b"dup"


def test_smb_archive_without_password_uses_no_password_flag(monkeypatch, workdir: Path):
    monkeypatch.setattr(archive_module.tempfile, "tempdir", str(workdir))
    captured = _fake_smbclient(monkeypatch)
    archive = SmbArchive(_smb_config(password=""))

    assert archive.check() == "//srv-sadko-db/SadkoAI/letters/2026"
    assert "-N" in captured["command"]
    assert captured["input"].strip().endswith("ls")


def test_smb_archive_reports_server_error(monkeypatch, workdir: Path):
    monkeypatch.setattr(archive_module.tempfile, "tempdir", str(workdir))
    _fake_smbclient(monkeypatch, stdout="NT_STATUS_ACCESS_DENIED listing /")
    archive = SmbArchive(_smb_config())

    with pytest.raises(ArchiveError) as error:
        archive.save(make_mail(), "вх1")

    assert "NT_STATUS_ACCESS_DENIED" in str(error.value)


def test_smb_archive_ignores_existing_directory(monkeypatch, workdir: Path):
    monkeypatch.setattr(archive_module.tempfile, "tempdir", str(workdir))
    _fake_smbclient(
        monkeypatch, stdout="NT_STATUS_OBJECT_NAME_COLLISION making remote directory"
    )
    SmbArchive(_smb_config()).save(make_mail(), "вх1")


def test_smb_archive_reports_missing_smbclient(monkeypatch, workdir: Path):
    monkeypatch.setattr(archive_module.tempfile, "tempdir", str(workdir))

    def run(*_args, **_kwargs):
        raise FileNotFoundError("smbclient")

    monkeypatch.setattr(archive_module.subprocess, "run", run)

    with pytest.raises(ArchiveError) as error:
        SmbArchive(_smb_config()).save(make_mail(), "вх1")

    assert "smbclient" in str(error.value)