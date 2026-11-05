"""Командная строка: проверка настроек и разбор отдельного письма."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import build_email, completion, make_config

from processor import cli
from processor.config import AppConfig
from processor.models import InsertedCard

ANALYSIS = {
    "register": True,
    "register_reason": "входящий документ",
    "confidence": 0.88,
    "summary": "Счёт на оплату по договору 45",
    "document_name": "Счёт",
    "sender": "ООО «Ромашка»",
    "doc_number": "45/12",
    "doc_date": "2026-10-18",
    "sheets_count": 1,
    "tags": ["счёт"],
    "category": "work",
}


class FakeJournal:
    def __init__(self) -> None:
        self.cards = []

    def insert_card(self, card, analysis=None):
        self.cards.append(card)
        return InsertedCard(card_id=136, reg_number="новая", storage="db")

    def ping(self) -> str:
        return "тестовая база"

    def close(self) -> None:
        pass


class FakeSource:
    def __init__(self, *_args, **_kwargs) -> None:
        self.closed = False
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def count(self) -> int:
        return 3

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def letter(workdir: Path) -> Path:
    path = workdir / "письмо.eml"
    path.write_bytes(
        build_email(
            subject="Счёт №45",
            attachments=[("счёт.txt", "text/plain", "Счёт на оплату".encode("utf-8"))],
        )
    )
    return path


def _write_config(workdir: Path, stub) -> Path:
    return make_config(
        workdir,
        {"ai": {"base_url": stub.base_url, "model": "test-model"}},
    )


def test_eml_dry_run_prints_card(workdir, letter, stub_ai, monkeypatch, capsys):
    stub = stub_ai([(200, completion(json.dumps(ANALYSIS, ensure_ascii=False)))])
    config_path = _write_config(workdir, stub)
    journal = FakeJournal()
    monkeypatch.setattr(cli, "make_journal", lambda _cfg: journal)

    code = cli.main(["--config", str(config_path), "--eml", str(letter), "--json"])

    assert code == 0
    assert journal.cards == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["card"]["EXTINDEX"] == "45/12"
    assert payload["card"]["KORRESP"] == "ООО «Ромашка»"
    assert payload["card"]["INTDATE"].count(".") == 2
    assert payload["analysis"]["document_name"] == "Счёт"


def test_eml_write_inserts_card(workdir, letter, stub_ai, monkeypatch, capsys):
    stub = stub_ai([(200, completion(json.dumps(ANALYSIS, ensure_ascii=False)))])
    config_path = _write_config(workdir, stub)
    journal = FakeJournal()
    monkeypatch.setattr(cli, "make_journal", lambda _cfg: journal)

    code = cli.main(["--config", str(config_path), "--eml", str(letter), "--write", "--json"])

    assert code == 0
    assert len(journal.cards) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "registered"
    assert payload["inserted"]["card_id"] == 136


def test_eml_human_output(workdir, letter, stub_ai, monkeypatch, capsys):
    stub = stub_ai([(200, completion(json.dumps(ANALYSIS, ensure_ascii=False)))])
    config_path = _write_config(workdir, stub)
    monkeypatch.setattr(cli, "make_journal", lambda _cfg: FakeJournal())

    cli.main(["--config", str(config_path), "--eml", str(letter)])
    out = capsys.readouterr().out
    assert "Запись журнала (таблица CARD)" in out
    assert "SH_CONTENTS" in out
    assert "пробный прогон" in out.lower()


def test_model_failure_returns_error_code(workdir, letter, stub_ai, monkeypatch, capsys):
    stub = stub_ai([(200, completion("это не JSON"))])
    config_path = _write_config(workdir, stub)
    monkeypatch.setattr(cli, "make_journal", lambda _cfg: FakeJournal())

    code = cli.main(["--config", str(config_path), "--eml", str(letter), "--json"])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "JSON" in payload["error"]


def test_missing_letter_file(workdir, stub_ai, monkeypatch, capsys):
    stub = stub_ai([(200, completion("{}"))])
    config_path = _write_config(workdir, stub)
    monkeypatch.setattr(cli, "make_journal", lambda _cfg: FakeJournal())

    code = cli.main(["--config", str(config_path), "--eml", str(workdir / "нет.eml")])

    assert code == 2
    assert "не найден" in capsys.readouterr().err


def test_broken_config_returns_code_2(workdir, capsys):
    path = workdir / "config.ini"
    path.write_text("[mail]\nprotocol = carrier-pigeon\n", encoding="utf-8")

    code = cli.main(["--config", str(path), "--check"])

    assert code == 2
    assert "Ошибка настроек" in capsys.readouterr().err


def test_check_reports_success(workdir, stub_ai, monkeypatch, capsys):
    stub = stub_ai()
    config_path = _write_config(workdir, stub)
    monkeypatch.setattr(cli, "make_journal", lambda _cfg: FakeJournal())
    monkeypatch.setattr(cli, "build_source", lambda _cfg: FakeSource())

    code = cli.main(["--config", str(config_path), "--check"])

    out = capsys.readouterr().out
    assert code == 0
    assert "Модель: OK" in out
    assert "Журнал: OK" in out
    assert "Почта: OK" in out
    assert "всё готово к работе" in out


def test_check_without_mailbox_settings(workdir, stub_ai, monkeypatch, capsys):
    """--check не должен падать на незаполненном ящике: он показывает все узлы."""
    stub = stub_ai()
    config_path = make_config(
        workdir,
        {
            "mail": {"host": "", "username": "", "password": ""},
            "ai": {"base_url": stub.base_url, "model": "test-model"},
        },
    )
    monkeypatch.setattr(cli, "make_journal", lambda _cfg: FakeJournal())

    code = cli.main(["--config", str(config_path), "--check"])

    out = capsys.readouterr().out
    assert code == 1
    assert "Почта: ОШИБКА" in out
    assert "Модель: OK" in out
    assert "есть ошибки" in out


def test_check_reports_failure(workdir, monkeypatch, capsys):
    config_path = make_config(
        workdir, {"ai": {"base_url": "http://127.0.0.1:1/v1", "model": "test-model"}}
    )
    monkeypatch.setattr(cli, "make_journal", lambda _cfg: FakeJournal())
    monkeypatch.setattr(cli, "build_source", lambda _cfg: FakeSource())

    code = cli.main(["--config", str(config_path), "--check"])

    assert code == 1
    assert "Модель: ОШИБКА" in capsys.readouterr().out
