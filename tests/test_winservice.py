"""Обёртка службы Windows: импорт и понятные ошибки без pywin32."""

from __future__ import annotations

import pytest

from processor import winservice
from processor.errors import ConfigError


def test_winservice_module_imports():
    assert hasattr(winservice, "install_service")
    assert hasattr(winservice, "run_service")


def test_install_without_pywin32(monkeypatch):
    monkeypatch.setattr(winservice, "_HAS_PYWIN32", False)
    with pytest.raises(ConfigError) as error:
        winservice.install_service("config.ini")
    assert "pywin32" in str(error.value)


def test_service_without_pywin32(monkeypatch):
    monkeypatch.setattr(winservice, "_HAS_PYWIN32", False)
    with pytest.raises(ConfigError) as error:
        winservice.run_service("config.ini")
    assert "pywin32" in str(error.value)
