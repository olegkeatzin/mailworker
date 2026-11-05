"""Чтение и пополнение справочников KORRESP и THEMA через fdb (без живой базы)."""

from __future__ import annotations

import sys
import types

import pytest

from processor.config import ClassifiersConfig, DatabaseConfig
from processor.errors import StorageError
from processor.journal.dictionaries import FirebirdDictionaries


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.connection.calls.append((sql, params))

    def fetchone(self):
        return self.connection.one_for(self.last_sql)

    def fetchall(self):
        return self.connection.many_for(self.last_sql)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker
        self.calls = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass

    def many_for(self, sql):
        for marker, rows in self.rows_by_marker.items():
            if marker in sql:
                return list(rows)
        return []

    def one_for(self, sql):
        if "GEN_ID" in sql:
            return (self.rows_by_marker.get("GEN_ID", (777,))[0],)
        if "SELECT FIRST 1" in sql:
            return self.rows_by_marker.get("EXISTING")
        return None


def install_fdb(monkeypatch, connection):
    module = types.ModuleType("fdb")
    module.connect = lambda **params: connection
    monkeypatch.setitem(sys.modules, "fdb", module)
    return module


@pytest.fixture()
def db_cfg() -> DatabaseConfig:
    return DatabaseConfig(host="10.0.0.1", database="kanc.fdb", user="SYSDBA", password="masterkey")


@pytest.fixture()
def clf_cfg() -> ClassifiersConfig:
    return ClassifiersConfig(enabled=True)


def test_load_correspondents(monkeypatch, db_cfg, clf_cfg):
    connection = FakeConnection(
        {"FROM KORRESP": [(42, "ООО «Ромашка» ", "Р", 1), (43, "АО «Вектор»", None, 0)]}
    )
    install_fdb(monkeypatch, connection)
    dictionaries = FirebirdDictionaries(db_cfg, clf_cfg)

    items = dictionaries.load_correspondents()

    assert [item.id for item in items] == [42, 43]
    assert items[0].name == "ООО «Ромашка»"
    assert items[0].numbpart == "Р"
    assert items[0].shows is True
    assert items[1].shows is False  # 0 — скрытая запись
    assert "FROM KORRESP" in connection.calls[0][0]


def test_load_themes(monkeypatch, db_cfg, clf_cfg):
    connection = FakeConnection({"FROM THEMA": [(1, "Бухгалтерия", "", 1)]})
    install_fdb(monkeypatch, connection)

    items = FirebirdDictionaries(db_cfg, clf_cfg).load_themes()

    assert len(items) == 1
    assert items[0].id == 1
    assert items[0].name == "Бухгалтерия"


def test_load_skips_broken_rows(monkeypatch, db_cfg, clf_cfg):
    connection = FakeConnection(
        {"FROM THEMA": [(None, "Без идентификатора", "", 1), ("x", "Кривой id", "", 1), (5, "  ", "", 1), (6, "Кадры", "", 1)]}
    )
    install_fdb(monkeypatch, connection)

    items = FirebirdDictionaries(db_cfg, clf_cfg).load_themes()

    assert [item.id for item in items] == [6]


def test_load_error_is_explained(monkeypatch, db_cfg, clf_cfg):
    class BrokenConnection(FakeConnection):
        def cursor(self):
            raise RuntimeError("Table unknown")

    install_fdb(monkeypatch, BrokenConnection({}))
    dictionaries = FirebirdDictionaries(db_cfg, clf_cfg)

    with pytest.raises(StorageError) as error:
        dictionaries.load_correspondents()
    assert "correspondents_sql" in str(error.value)


def test_create_correspondent_when_absent(monkeypatch, db_cfg, clf_cfg):
    connection = FakeConnection({})
    install_fdb(monkeypatch, connection)
    dictionaries = FirebirdDictionaries(db_cfg, clf_cfg)

    created = dictionaries.create_correspondent("Новая организация")

    assert created.id == 777
    assert created.name == "Новая организация"
    assert connection.committed is True
    insert_sql, params = connection.calls[-1]
    assert "INSERT INTO KORRESP" in insert_sql
    assert params == [777, "Новая организация", "", 0, 1]


def test_create_correspondent_reuses_existing(monkeypatch, db_cfg, clf_cfg):
    connection = FakeConnection({"EXISTING": (42,)})
    install_fdb(monkeypatch, connection)
    dictionaries = FirebirdDictionaries(db_cfg, clf_cfg)

    found = dictionaries.create_correspondent("ооо ромашка")

    assert found.id == 42
    # Вставки не было: только проверка наличия.
    assert all("INSERT" not in sql for sql, _params in connection.calls)


def test_create_theme(monkeypatch, db_cfg, clf_cfg):
    connection = FakeConnection({})
    install_fdb(monkeypatch, connection)
    dictionaries = FirebirdDictionaries(db_cfg, clf_cfg)

    created = dictionaries.create_theme("Новая тема")

    assert created.id == 777
    insert_sql, params = connection.calls[-1]
    assert "INSERT INTO THEMA" in insert_sql
    assert params == [777, "Новая тема", "", 1]


def test_empty_name_is_rejected(db_cfg, clf_cfg):
    dictionaries = FirebirdDictionaries(db_cfg, clf_cfg)
    with pytest.raises(StorageError):
        dictionaries.create_correspondent("   ")


def test_name_is_trimmed_to_limit(monkeypatch, db_cfg, clf_cfg):
    import dataclasses

    connection = FakeConnection({})
    install_fdb(monkeypatch, connection)
    cfg = dataclasses.replace(clf_cfg, max_name_length=10)
    dictionaries = FirebirdDictionaries(db_cfg, cfg)

    created = dictionaries.create_correspondent("Очень длинное название организации")

    assert len(created.name) == 10
