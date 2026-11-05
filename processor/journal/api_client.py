"""Запись в журнал через HTTP API сервиса FastAPI_sender_to_BD.

Нужна, когда служба стоит на другой машине, а запись делает существующий
сервис (POST /cards). Контракт взят из api.md: даты строго ГГГГ-ММ-ДД,
обязателен Content-Type application/json, номер «вх{N}» присваивает сервер.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request

from ..config import JournalConfig
from ..errors import StorageError
from ..models import Analysis, CardData, InsertedCard
from .mapping import to_api_payload

logger = logging.getLogger(__name__)


class ApiJournal:
    """Реализация записи журнала через REST-эндпоинт /cards."""

    storage_name = "api"

    def __init__(self, cfg: JournalConfig) -> None:
        self.cfg = cfg

    def insert_card(self, card: CardData, analysis: Analysis | None = None) -> InsertedCard:
        payload = to_api_payload(card, analysis or Analysis())
        url = self.cfg.api_url + "/cards"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.api_timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001
                pass
            raise StorageError(
                "Сервис журнала вернул HTTP "
                + str(exc.code)
                + " на POST "
                + url
                + (": " + detail.strip() if detail else "")
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise StorageError(
                "Сервис журнала не ответил за " + str(self.cfg.api_timeout) + " с (" + url + ")"
            ) from exc
        except urllib.error.URLError as exc:
            raise StorageError(
                "Не удалось обратиться к сервису журнала "
                + url
                + ": "
                + str(exc.reason)
                + ". Проверьте [journal] api_url."
            ) from exc

        try:
            answer = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            answer = {}
        card_id = answer.get("id") if isinstance(answer, dict) else None
        reg_number = answer.get("regNumber") if isinstance(answer, dict) else None
        logger.info(
            "Сервис журнала принял запись: id=%s, regNumber=%s", card_id, reg_number
        )
        return InsertedCard(card_id=card_id, reg_number=reg_number, storage="api")

    def ping(self) -> str:
        """Проверка доступности сервиса (для --check)."""
        url = self.cfg.api_url + "/openapi.json"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.api_timeout) as response:
                response.read(256)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                # Сервис отвечает, просто другой маршрут — этого достаточно.
                return self.cfg.api_url
            raise StorageError(
                "Сервис журнала ответил HTTP " + str(exc.code) + " (" + url + ")"
            ) from exc
        except urllib.error.URLError as exc:
            raise StorageError(
                "Сервис журнала недоступен по адресу "
                + self.cfg.api_url
                + ": "
                + str(exc.reason)
            ) from exc
        return self.cfg.api_url

    def close(self) -> None:
        """Соединения не удерживаются."""
