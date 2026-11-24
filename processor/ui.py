"""Простая веб-панель: очередь, ошибки и пересканирование.

Без Flask и JS-сборки: ThreadingHTTPServer, HTML и формы. Панель запускается
вместе со службой, если [ui] enabled = true, и показывает то, что записано в
CARD, плюс очередь файлов SCANNED.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .config import AppConfig
from .state import ProcessedState
from .spool import Spool

logger = logging.getLogger(__name__)

STYLE = """
body{font:14px system-ui,Arial,sans-serif;margin:24px;color:#222}
h1{font-size:20px} table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}
.ok{color:#276749}.degraded{color:#b7791f}.failed{color:#c53030}
pre{background:#f6f6f6;padding:10px;white-space:pre-wrap}
button{padding:4px 10px;margin-right:6px}
.msg{background:#eef;padding:8px;margin:10px 0}
"""


class UiServer:
    """HTTP-панель в отдельном потоке."""

    def __init__(self, cfg: AppConfig, processor_factory: Callable[[], object]) -> None:
        self.cfg = cfg
        self.processor_factory = processor_factory
        self.secret = secrets.token_bytes(32)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        handler = _make_handler(self)
        self.httpd = ThreadingHTTPServer((self.cfg.ui.host, self.cfg.ui.port), handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="mailworker-ui", daemon=True
        )
        self.thread.start()
        logger.info(
            "Веб-панель: http://%s:%s (логин: %s)",
            self.cfg.ui.host,
            self.cfg.ui.port,
            self.cfg.ui.username,
        )

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    # ------------------------------------------------------------------ сессии

    def session_token(self) -> str:
        expires = int(time.time()) + self.cfg.ui.session_hours * 3600
        signature = hmac.new(
            self.secret, str(expires).encode(), hashlib.sha256
        ).hexdigest()[:32]
        return str(expires) + ":" + signature

    def valid_session(self, cookie: str | None) -> bool:
        if not cookie or ":" not in cookie:
            return False
        raw, signature = cookie.rsplit(":", 1)
        try:
            expires = int(raw)
        except ValueError:
            return False
        if expires < int(time.time()):
            return False
        expected = hmac.new(
            self.secret, raw.encode(), hashlib.sha256
        ).hexdigest()[:32]
        return hmac.compare_digest(signature, expected)

    def csrf(self, token: str) -> str:
        return hmac.new(
            self.secret, ("csrf:" + token).encode(), hashlib.sha256
        ).hexdigest()[:32]

    def check_password(self, username: str, password: str) -> bool:
        return hmac.compare_digest(username or "", self.cfg.ui.username) and hmac.compare_digest(
            password or "", self.cfg.ui.password
        )


def _make_handler(server: UiServer):
    cfg = server.cfg
    state = ProcessedState(cfg.processing.state_path)
    spool = Spool(cfg.processing.state_path.parent / "spool")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            token = self._cookie()
            if not server.valid_session(token):
                if parsed.path == "/":
                    return self._login_page()
                return self._redirect("/")
            if parsed.path == "/":
                return self._queue_page(parsed)
            if parsed.path.startswith("/letter/"):
                return self._letter_page(parsed.path.rsplit("/", 1)[-1])
            return self._not_found()

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/login":
                username = (form.get("username") or [""])[0]
                password = (form.get("password") or [""])[0]
                if server.check_password(username, password):
                    token = server.session_token()
                    return self._redirect(
                        "/",
                        cookie=token,
                        headers={"Set-Cookie": "mw_session=" + token + "; HttpOnly; Path=/; SameSite=Lax"},
                    )
                return self._login_page("Неверный логин или пароль")
            token = self._cookie()
            if not server.valid_session(token):
                return self._redirect("/")
            csrf = (form.get("csrf") or [""])[0]
            if not hmac.compare_digest(csrf, server.csrf(token)):
                return self._redirect("/?msg=" + urllib.parse.quote("CSRF-токен неверен"))
            if parsed.path == "/logout":
                return self._redirect("/", headers={"Set-Cookie": "mw_session=; Max-Age=0; Path=/"})
            if parsed.path == "/retry":
                return self._retry()
            if parsed.path.startswith("/rescan/"):
                return self._rescan(parsed.path.rsplit("/", 1)[-1])
            return self._not_found()

        # ------------------------------------------------------------- страницы

        def _login_page(self, error: str = ""):
            body = (
                "<h1>MailWorker</h1>"
                + ("<p class='failed'>" + html.escape(error) + "</p>" if error else "")
                + "<form method='post' action='/login'>"
                "<p>Логин: <input name='username' value='admin'></p>"
                "<p>Пароль: <input type='password' name='password'></p>"
                "<button>Войти</button></form>"
            )
            self._html("MailWorker", body)

        def _queue_page(self, parsed):
            quality = state.quality_counts()
            queue = state.queue_counts()
            letters = state.recent_letters(limit=200)
            message = (urllib.parse.parse_qs(parsed.query).get("msg") or [""])[0]
            body = ["<h1>Очередь MailWorker</h1>"]
            if message:
                body.append("<div class='msg'>" + html.escape(message) + "</div>")
            body.append(
                "<p>Качество: "
                + " ".join(
                    "<b class='" + html.escape(k) + "'>" + html.escape(k) + ": " + str(v) + "</b>"
                    for k, v in sorted(quality.items())
                )
                + " &nbsp; Архив: "
                + ", ".join(html.escape(k) + ": " + str(v) for k, v in sorted(queue.items()))
                + "</p>"
            )
            body.append("<table><tr><th>CARD.ID</th><th>Письмо</th><th>Статус</th><th>Ошибки</th><th></th></tr>")
            for item in letters:
                card_id = html.escape(str(item.get("card_id") or "—"))
                body.append(
                    "<tr><td>" + card_id + "</td><td>" + html.escape(item.get("subject") or "—")
                    + "<br><small>" + html.escape(item.get("from_display") or "") + "</small></td>"
                    + "<td class='" + html.escape(item.get("quality") or "") + "'>"
                    + html.escape(item.get("quality") or "") + "</td>"
                    + "<td>" + html.escape((item.get("warnings") or "")[:200]) + "</td>"
                    + "<td><a href='/letter/" + card_id + "'>Открыть</a></td></tr>"
                )
            body.append("</table>")
            body.append(
                "<form method='post' action='/retry' style='margin-top:12px'>"
                + self._csrf_input()
                + "<button>Повторить архив (failed/pending)</button></form>"
            )
            body.append("<form method='post' action='/logout'><button>Выйти</button></form>")
            self._html("MailWorker — очередь", "".join(body))

        def _letter_page(self, card_id: str):
            record = state.letter_by_card(card_id)
            if record is None:
                return self._html("Не найдено", "<p>Запись не найдена.</p>")
            card = {}
            try:
                card = json.loads(record.get("card_json") or "{}")
            except json.JSONDecodeError:
                pass
            warnings = record.get("warnings") or ""
            files = state.files_for(record["key"])
            body = ["<p><a href='/'>← очередь</a></p>", "<h1>CARD.ID=" + html.escape(card_id) + "</h1>"]
            body.append("<p>Тема: " + html.escape(record.get("subject") or "—") + "</p>")
            body.append("<pre>" + html.escape(json.dumps(card, ensure_ascii=False, indent=2)) + "</pre>")
            if warnings:
                body.append("<h3>Предупреждения</h3><pre>" + html.escape(warnings) + "</pre>")
            body.append("<h3>Файлы в SCANNED</h3>")
            body.append("<table><tr><th>#</th><th>Файл</th><th>SCANNED.ID</th><th>DOC_PATH</th><th>Статус</th><th>Ошибка</th></tr>")
            for item in files:
                body.append(
                    "<tr><td>" + str(item.get("index")) + "</td><td>" + html.escape(item.get("filename") or item.get("kind") or "")
                    + "</td><td>" + html.escape(str(item.get("scan_id") or "")) + "</td><td>"
                    + html.escape(item.get("doc_path") or "") + "</td><td>"
                    + html.escape(item.get("status") or "") + "</td><td>"
                    + html.escape(item.get("last_error") or "") + "</td></tr>"
                )
            body.append("</table>")
            body.append("<p>Spool: " + ("есть" if spool.exists(record["key"]) else "нет") + "</p>")
            body.append(
                "<form method='post' action='/rescan/" + html.escape(card_id) + "'>"
                + self._csrf_input() + "<button>Перепрогнать и перезаписать CARD</button></form>"
            )
            self._html("CARD " + card_id, "".join(body))

        # -------------------------------------------------------------- действия

        def _rescan(self, card_id: str):
            try:
                processor = server.processor_factory()
                processor.rescan_card(int(card_id))
                message = "CARD.ID=" + card_id + " перезаписан"
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка пересканирования CARD.ID=%s", card_id)
                message = "Ошибка: " + str(exc)
            return self._redirect("/?msg=" + urllib.parse.quote(message))

        def _retry(self):
            try:
                processor = server.processor_factory()
                outcome = processor.retry_archive(100)
                message = "Повтор архива: сохранено %s, ошибок %s" % (
                    outcome.uploaded,
                    outcome.failed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка повтора архива")
                message = "Ошибка: " + str(exc)
            return self._redirect("/?msg=" + urllib.parse.quote(message))

        # ---------------------------------------------------------------- helpers

        def _cookie(self) -> str | None:
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "mw_session":
                    return value
            return None

        def _csrf_input(self) -> str:
            token = self._cookie() or ""
            return "<input type='hidden' name='csrf' value='" + server.csrf(token) + "'>"

        def _redirect(self, location: str, *, cookie: str | None = None, headers: dict | None = None):
            self.send_response(303)
            self.send_header("Location", location)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _html(self, title: str, body: str):
            data = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<title>" + html.escape(title) + "</title><style>" + STYLE + "</style></head>"
                "<body>" + body + "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _not_found(self):
            return self._html("Не найдено", "<p>Страница не найдена.</p>")

    return Handler


def start_ui(cfg: AppConfig, processor_factory: Callable[[], object]) -> UiServer | None:
    """Запускает панель, если она включена в конфиге."""
    if not cfg.ui.enabled:
        return None
    server = UiServer(cfg, processor_factory)
    server.start()
    return server
