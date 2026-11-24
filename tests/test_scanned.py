"""SCANNED: резерв ID генератором и INSERT с явным ID."""

from __future__ import annotations

import processor.journal.scanned as scanned_module
from processor.config import ArchiveConfig, DatabaseConfig
from processor.journal.scanned import ScannedStore


class FakeCursor:
    def __init__(self):
        self.sql = ""
        self.params = ()

    def execute(self, sql, params=()):
        self.sql = sql
        self.params = params

    def fetchone(self):
        return (111,)

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursors = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        cursor = FakeCursor()
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def test_reserve_and_insert(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(scanned_module, "connect", lambda _cfg: connection)
    store = ScannedStore(
        DatabaseConfig(host="h", database="d", user="u", password="p"),
        ArchiveConfig(server="srv", share="SadkoAI", username="u"),
    )

    with store:
        scan_id = store.reserve_id()
        store.insert_scan(scan_id, 178, "inbox@test", "FILES\\178\\S111.pdf")
        store.commit()

    assert scan_id == 111
    queries = [cursor.sql for cursor in connection.cursors]
    assert any("GEN_ID(ID_SCANNED, 1)" in query for query in queries)
    insert = [
        cursor for cursor in connection.cursors if "INSERT INTO SCANNED" in cursor.sql
    ][0]
    assert insert.params[0] == 111
    assert insert.params[1] == 178
    assert insert.params[3] == "FILES\\178\\S111.pdf"
    assert connection.commits == 1