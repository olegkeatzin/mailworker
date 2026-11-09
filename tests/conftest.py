"""Общие приспособления для тестов.

Тесты запускаются из корня репозитория:
    python -m pytest
Пакет processor подхватывается без установки (см. sys.path ниже).
"""

from __future__ import annotations

import email.utils
import json
import re
import shutil
import sys
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processor.config import AppConfig, JournalConfig  # noqa: E402

#: Полный рабочий набор настроек: отдельный тест меняет только нужное поле.
DEFAULT_SECTIONS: dict[str, dict[str, str]] = {
    "mail": {
        "protocol": "imap",
        "host": "mail.test",
        "port": "993",
        "tls": "ssl",
        "username": "inbox@test",
        "password": "secret",
        "mailbox": "INBOX",
        "mode": "all",
        "poll_interval": "60",
        "max_per_cycle": "10",
    },
    "database": {
        "host": "10.0.0.1",
        "database": "test.fdb",
        "user": "SYSDBA",
        "password": "masterkey",
        "charset": "WIN1251",
    },
    "ai": {
        "base_url": "http://127.0.0.1:9/v1",
        "api_key": "test-key",
        "model": "test-model",
        "timeout": "5",
        "retries": "0",
    },
    "ocr": {},
    "journal": {
        "storage": "db",
        "intindex": "новая",
        "sheets_mode": "pdf_pages",
    },
    # По умолчанию в тестах справочники выключены: без живой базы их не прочитать.
    # Тесты справочников создают ClassifierService с подставным источником.
    "classifiers": {
        "enabled": "false",
    },
    "processing": {
        # В тестах отбор явный: по умолчанию в службе регистрируются все письма.
        "filter_mode": "ai",
        "skip_spam": "true",
        "state_path": "data/state.sqlite",
        "archive_dir": "data/processed",
        "save_eml": "true",
    },
    "logging": {
        "level": "INFO",
        "file": "",
        "console": "false",
    },
}


def make_config(tmp_path: Path, overrides: dict[str, dict[str, str]] | None = None) -> Path:
    """Пишет .ini во временный каталог и возвращает путь."""
    sections = {name: dict(values) for name, values in DEFAULT_SECTIONS.items()}
    for section, values in (overrides or {}).items():
        sections.setdefault(section, {}).update(values)

    lines: list[str] = []
    for section, values in sections.items():
        lines.append("[" + section + "]")
        for key, value in values.items():
            lines.append(key + " = " + str(value))
        lines.append("")
    path = tmp_path / "config.ini"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


WORK_ROOT = Path(__file__).resolve().parent / ".work"


@pytest.fixture(scope="session", autouse=True)
def _clean_work_root():
    """Каталог для временных файлов тестов — рядом с тестами, не в %TEMP%.

    Временный каталог системы может быть недоступен (ограниченная песочница,
    политики антивируса), а каталог тестов доступен всегда. После прогона
    каталог удаляется целиком.
    """
    shutil.rmtree(WORK_ROOT, ignore_errors=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(WORK_ROOT, ignore_errors=True)


@pytest.fixture()
def workdir(request) -> Path:
    """Отдельный каталог на каждый тест."""
    safe_name = re.sub(r"[^\w.-]+", "_", request.node.nodeid)[:100]
    path = WORK_ROOT / safe_name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def config(workdir: Path) -> AppConfig:
    """Настройки с путями внутри временного каталога теста."""
    return AppConfig.load(make_config(workdir))


@pytest.fixture()
def journal_cfg() -> JournalConfig:
    """Поля журнала по умолчанию — как в config.ini.example."""
    return JournalConfig()


# ------------------------------------------------------------------- письма


def build_email(
    *,
    subject: str = "Тестовое письмо",
    from_name: str = "Иванов Иван",
    from_address: str = "ivanov@example.ru",
    body: str = "Текст письма.",
    attachments: list[tuple[str, str, bytes]] | None = None,
    html: str | None = None,
    message_id: str | None = "test-message-1@example.ru",
    date: str | None = None,
) -> bytes:
    """Собирает .eml для тестов (как present, так и приложенные документы)."""
    message = EmailMessage()
    message["From"] = email.utils.formataddr((from_name, from_address))
    message["To"] = "inbox@example.ru"
    message["Subject"] = subject
    message["Date"] = date or email.utils.formatdate(localtime=False)
    if message_id:
        message["Message-ID"] = "<" + message_id + ">"

    if html is not None:
        message.set_content(body)
        message.add_alternative(html, subtype="html")
    else:
        message.set_content(body)

    for filename, mime, content in attachments or []:
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(
            content, maintype=maintype, subtype=subtype, filename=filename
        )
    return message.as_bytes()


@pytest.fixture()
def simple_eml() -> bytes:
    return build_email()


# --------------------------------------------------------- заглушка ИИ-сервера


class StubAiServer:
    """Локальный HTTP-сервер, изображающий OpenAI-совместимый эндпоинт."""

    def __init__(self, responses: list[tuple[int, dict]] | None = None) -> None:
        self.responses = list(responses or [(200, _completion("{}"))])
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return "http://" + host + ":" + str(port) + "/v1"

    def __enter__(self) -> "StubAiServer":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - имя задаёт BaseHTTPRequestHandler
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append(body)
                stub.paths.append(self.path)
                status, payload = (
                    stub.responses.pop(0) if len(stub.responses) > 1 else stub.responses[0]
                )
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                stub.paths.append(self.path)
                payload = {"data": [{"id": "test-model"}, {"id": "other-model"}]}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        return Handler


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


@pytest.fixture()
def stub_ai():
    """Фабрика заглушек: StubAiServer([(200, completion(json))])."""
    created: list[StubAiServer] = []

    def factory(responses: list[tuple[int, dict]] | None = None) -> StubAiServer:
        server = StubAiServer(responses)
        server.__enter__()
        created.append(server)
        return server

    yield factory

    for server in created:
        server.__exit__()


def completion(content: str) -> dict:
    """Тело ответа модели с заданным содержимым."""
    return _completion(content)
