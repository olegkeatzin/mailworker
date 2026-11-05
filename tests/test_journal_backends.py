"""Запись в журнал: Firebird (fdb) и HTTP API — без живой базы и сервера."""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from processor.config import DatabaseConfig, JournalConfig
from processor.errors import StorageError
from processor.journal.api_client import ApiJournal
from processor.journal.firebird import INSERT_SQL, FirebirdJournal
from processor.models import Analysis, CardData


def make_card(**overrides) -> CardData:
    values = dict(
        card_type=1,
        intindex="новая",
        intdate=dt.date(2026, 11, 5),
        extindex="45/12",
        extdate=dt.date(2026, 10, 18),
        sh_contents="Счёт на оплату",
        pages_doc=2,
        korresp="ООО «Ромашка»",
    )
    values.update(overrides)
    return CardData(**values)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def execute(self, sql, params=None):
        self.connection.calls.append((sql, params))

    def fetchone(self):
        sql = self.connection.calls[-1][0]
        if "GEN_ID" in sql:
            return (self.connection.generator_value,)
        return None

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, generator_value=136, fail_on_insert=None):
        self.calls = []
        self.generator_value = generator_value
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.fail_on_insert = fail_on_insert

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        if self.fail_on_insert:
            raise self.fail_on_insert
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def install_fake_fdb(monkeypatch, connection, connect_error=None):
    module = types.ModuleType("fdb")
    captured: dict = {}

    def connect(**params):
        captured.update(params)
        if connect_error:
            raise connect_error
        return connection

    module.connect = connect
    module.captured = captured
    monkeypatch.setitem(sys.modules, "fdb", module)
    return module


@pytest.fixture()
def db_cfg() -> DatabaseConfig:
    return DatabaseConfig(
        host="10.0.0.1", database="test.fdb", user="SYSDBA", password="masterkey"
    )


@pytest.fixture()
def journal_cfg() -> JournalConfig:
    return JournalConfig()


def test_insert_card_parameters_and_commit(monkeypatch, db_cfg, journal_cfg):
    connection = FakeConnection()
    module = install_fake_fdb(monkeypatch, connection)
    journal = FirebirdJournal(db_cfg, journal_cfg)

    inserted = journal.insert_card(make_card())

    assert inserted.card_id == 136
    assert inserted.reg_number == "новая"
    assert inserted.storage == "db"
    assert connection.committed is True
    assert "GEN_ID(ID_CARD, 1)" in connection.calls[0][0]

    insert_sql, params = connection.calls[1]
    assert insert_sql == INSERT_SQL
    assert params == [
        136,
        1,
        "новая",
        "05.11.2026",
        "45/12",
        "18.10.2026",
        "Счёт на оплату",
        2,
        "ООО «Ромашка»",
        0,
        0,
    ]
    assert module.captured == {
        "host": "10.0.0.1",
        "database": "test.fdb",
        "user": "SYSDBA",
        "password": "masterkey",
        "charset": "WIN1251",
    }


def test_insert_writes_classifier_ids(monkeypatch, db_cfg, journal_cfg):
    """Ссылки на справочники пишутся в KORRESPID и THEMA."""
    connection = FakeConnection()
    install_fake_fdb(monkeypatch, connection)
    journal = FirebirdJournal(db_cfg, journal_cfg)

    journal.insert_card(make_card(korrespid=42, thema=7))

    _sql, params = connection.calls[1]
    assert params[9] == 42
    assert params[10] == 7
    assert "KORRESPID" in INSERT_SQL and "THEMA" in INSERT_SQL


def test_insert_uses_parameters_not_string_concat(monkeypatch, db_cfg, journal_cfg):
    """Апостроф в тексте письма не должен ломать INSERT."""
    connection = FakeConnection()
    install_fake_fdb(monkeypatch, connection)
    journal = FirebirdJournal(db_cfg, journal_cfg)

    journal.insert_card(make_card(sh_contents="Письмо ООО 'Ромашка'"))

    _sql, params = connection.calls[1]
    assert params[6] == "Письмо ООО 'Ромашка'"
    assert "'" not in INSERT_SQL


def test_date_format_date_passes_date_object(monkeypatch, journal_cfg):
    connection = FakeConnection()
    install_fake_fdb(monkeypatch, connection)
    cfg = DatabaseConfig(
        host="h", database="d", user="u", password="p", date_format="date"
    )
    FirebirdJournal(cfg, journal_cfg).insert_card(make_card())

    _sql, params = connection.calls[1]
    assert params[3] == dt.date(2026, 11, 5)
    assert params[5] == dt.date(2026, 10, 18)


def test_text_is_sanitized_for_win1251(monkeypatch, db_cfg, journal_cfg):
    connection = FakeConnection()
    install_fake_fdb(monkeypatch, connection)
    FirebirdJournal(db_cfg, journal_cfg).insert_card(make_card(sh_contents="Счёт 😀"))

    _sql, params = connection.calls[1]
    assert "😀" not in params[6]
    assert "Счёт" in params[6]


def test_none_doc_date_is_passed_as_null(monkeypatch, db_cfg, journal_cfg):
    connection = FakeConnection()
    install_fake_fdb(monkeypatch, connection)
    FirebirdJournal(db_cfg, journal_cfg).insert_card(make_card(extdate=None))
    _sql, params = connection.calls[1]
    assert params[5] is None


def test_connection_error_mentions_client_library(monkeypatch, db_cfg, journal_cfg):
    install_fake_fdb(
        monkeypatch, FakeConnection(), connect_error=Exception("cannot load library fbclient")
    )
    with pytest.raises(StorageError) as error:
        FirebirdJournal(db_cfg, journal_cfg).insert_card(make_card())
    assert "fb_client_library" in str(error.value)


def test_connection_error_mentions_credentials(monkeypatch, db_cfg, journal_cfg):
    install_fake_fdb(
        monkeypatch, FakeConnection(), connect_error=Exception("Your login is missing")
    )
    with pytest.raises(StorageError) as error:
        FirebirdJournal(db_cfg, journal_cfg).insert_card(make_card())
    assert "user" in str(error.value) or "password" in str(error.value)


def test_missing_fdb_package(monkeypatch, db_cfg, journal_cfg):
    monkeypatch.setitem(sys.modules, "fdb", None)
    with pytest.raises(StorageError) as error:
        FirebirdJournal(db_cfg, journal_cfg).insert_card(make_card())
    assert "fdb" in str(error.value)


def test_insert_error_rolls_back(monkeypatch, db_cfg, journal_cfg):
    connection = FakeConnection()
    connection.fail_on_insert = Exception("arithmetic exception")
    install_fake_fdb(monkeypatch, connection)

    with pytest.raises(StorageError) as error:
        FirebirdJournal(db_cfg, journal_cfg).insert_card(make_card())
    assert "журнал" in str(error.value)
    assert connection.rolled_back is True


# ------------------------------------------------------------------- HTTP API


class ApiServer:
    """Мини-сервер /cards: повторяет контракт из api.md."""

    def __init__(self, status: int = 201, answer: dict | None = None) -> None:
        self.requests: list[dict] = []
        self.raw_requests: list[dict] = []
        self.status = status
        self.answer = answer or {"id": 136, "regNumber": "вх136"}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return "http://" + host + ":" + str(port)

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                server.requests.append(json.loads(raw))
                server.raw_requests.append(dict(self.headers))
                data = json.dumps(server.answer, ensure_ascii=False).encode("utf-8")
                self.send_response(server.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def api_server():
    server = ApiServer()
    yield server
    server.close()


def test_api_insert_sends_fields(api_server, journal_cfg):
    cfg = JournalConfig(storage="api", api_url=api_server.url)
    journal = ApiJournal(cfg)
    inserted = journal.insert_card(make_card(), Analysis(document_name="Счёт"))

    assert inserted.card_id == 136
    assert inserted.reg_number == "вх136"
    assert inserted.storage == "api"
    payload = api_server.requests[0]
    assert payload["docNumber"] == "45/12"
    assert payload["docDate"] == "2026-10-18"
    assert payload["regDate"] == "2026-11-05"
    assert payload["documentName"] == "Счёт"
    assert payload["sender"] == "ООО «Ромашка»"
    assert api_server.raw_requests[0]["Content-Type"].startswith("application/json")


def test_api_error_is_reported(journal_cfg):
    server = ApiServer(status=500)
    try:
        journal = ApiJournal(JournalConfig(storage="api", api_url=server.url))
        with pytest.raises(StorageError) as error:
            journal.insert_card(make_card())
        assert "HTTP 500" in str(error.value)
    finally:
        server.close()


def test_api_unreachable(journal_cfg):
    journal = ApiJournal(JournalConfig(storage="api", api_url="http://127.0.0.1:1"))
    with pytest.raises(StorageError) as error:
        journal.insert_card(make_card())
    assert "api_url" in str(error.value)
