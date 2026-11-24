"""Веб-панель: включает пароль и показывает очередь."""

from __future__ import annotations

import http.cookiejar
import socket
import urllib.parse
import urllib.request

from conftest import make_config

from processor.config import AppConfig
from processor.ui import start_ui


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_ui_disabled_returns_none(workdir):
    cfg = AppConfig.load(make_config(workdir, {"ui": {"enabled": "false"}}))
    assert start_ui(cfg, lambda: None) is None


def test_ui_login_and_queue(workdir):
    port = free_port()
    cfg = AppConfig.load(
        make_config(
            workdir,
            {
                "ui": {
                    "enabled": "true",
                    "host": "127.0.0.1",
                    "port": str(port),
                    "username": "admin",
                    "password": "secret",
                }
            },
        )
    )
    server = start_ui(cfg, lambda: None)
    assert server is not None
    try:
        base = "http://127.0.0.1:" + str(port)
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        with opener.open(base + "/") as response:
            assert "Войти" in response.read().decode("utf-8")
        data = urllib.parse.urlencode(
            {"username": "admin", "password": "secret"}
        ).encode()
        with opener.open(base + "/login", data=data) as response:
            assert "Очередь MailWorker" in response.read().decode("utf-8")
    finally:
        server.stop()
