"""Клиент OpenAI-совместимого эндпоинта (llama.cpp, vLLM, Ollama, OpenRouter).

Почему свой клиент, а не пакет openai: служба ставится на машину организации,
и лишняя тяжёлая зависимость — лишний повод для проблем при развёртывании.
Запрос простой (POST JSON), поэтому достаточно стандартной библиотеки.

Повторяет то, что делал MailSense (packages/core/src/ai/analyze.ts):
response_format=json_object, temperature 0.2 и chat_template_kwargs с выключенным
thinking — у reasoning-моделей в llama.cpp иначе ответ обрывается «размышлениями»
вместо JSON.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request

from ..config import AiConfig
from ..errors import AiError

logger = logging.getLogger(__name__)

#: Коды, при которых имеет смысл повторить запрос (перегрузка, таймаут шлюза).
_RETRY_STATUSES = (408, 409, 425, 429, 500, 502, 503, 504)


class AiClient:
    """Тонкий клиент чата с моделью. Один экземпляр на службу."""

    def __init__(self, cfg: AiConfig) -> None:
        self.cfg = cfg
        self._ssl_context = _ssl_context(cfg.verify_tls)

    # ------------------------------------------------------------------ запрос

    def complete(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool | None = None,
        extra: dict | None = None,
    ) -> str:
        """Отправляет сообщения и возвращает текст ответа модели."""
        url = (base_url or self.cfg.base_url).rstrip("/") + "/chat/completions"
        body: dict = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
        }
        use_json_mode = self.cfg.json_mode if json_mode is None else json_mode
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.cfg.disable_thinking:
            # llama.cpp: без этого reasoning-модель тратит токены на размышления
            # и обрезает JSON — тот же приём, что в MailSense.
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if self.cfg.request_extra:
            body.update(self.cfg.request_extra)
        if extra:
            body.update(extra)

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        logger.debug(
            "Запрос к модели %s: %s байт, сообщений: %s",
            body["model"],
            len(payload),
            len(messages),
        )

        answer = self._post(url, payload, timeout or self.cfg.timeout)
        text = _extract_content(answer, body["model"])
        logger.debug("Ответ модели: %s символов", len(text))
        return text

    def list_models(self) -> list[str]:
        """Список моделей (GET /models) — для проверки подключения в --check."""
        url = self.cfg.base_url.rstrip("/") + "/models"
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=self.cfg.timeout, context=self._ssl_context
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                # По адресу может отвечать не модель, а прокси или веб-панель.
                raise AiError(
                    "По адресу "
                    + url
                    + " ответили не JSON-списком моделей. Проверьте [ai] base_url"
                    " (должен заканчиваться на /v1). Начало ответа: "
                    + raw[:200].strip()
                ) from exc
        except urllib.error.HTTPError as exc:
            raise AiError(_http_error_text(exc, url)) from exc
        except urllib.error.URLError as exc:
            raise AiError(
                "Модель недоступна по адресу " + url + ": " + str(exc.reason)
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise AiError("Модель не ответила за " + str(self.cfg.timeout) + " с") from exc

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return sorted(
            str(item.get("id"))
            for item in items
            if isinstance(item, dict) and item.get("id")
        )

    # ---------------------------------------------------------------- внутреннее

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.cfg.api_key:
            headers["Authorization"] = "Bearer " + self.cfg.api_key
        return headers

    def _post(self, url: str, payload: bytes, timeout: int) -> dict:
        attempts = self.cfg.retries + 1
        last_error = ""
        for attempt in range(attempts):
            request = urllib.request.Request(
                url, data=payload, headers=self._headers(), method="POST"
            )
            try:
                started = time.monotonic()
                with urllib.request.urlopen(
                    request, timeout=timeout, context=self._ssl_context
                ) as response:
                    raw = response.read().decode("utf-8", errors="replace")
                logger.debug("Модель ответила за %.1f с", time.monotonic() - started)
                return json.loads(raw)
            except urllib.error.HTTPError as exc:
                message = _http_error_text(exc, url)
                last_error = message
                if exc.code not in _RETRY_STATUSES:
                    raise AiError(message) from exc
            except (socket.timeout, TimeoutError):
                last_error = "модель не ответила за " + str(timeout) + " с"
            except urllib.error.URLError as exc:
                last_error = "не удалось соединиться с моделью: " + str(exc.reason)
                if isinstance(exc.reason, ssl.SSLError):
                    last_error += " (проверьте [ai] verify_tls и сертификат)"
            except json.JSONDecodeError as exc:
                raise AiError("Модель вернула не JSON: " + str(exc)) from exc

            if attempt + 1 < attempts:
                delay = min(2 ** attempt, 10)
                logger.warning(
                    "Модель не ответила (%s). Повтор %s из %s через %s с.",
                    last_error,
                    attempt + 1,
                    self.cfg.retries,
                    delay,
                )
                time.sleep(delay)

        raise AiError("Не удалось получить ответ модели. " + last_error)


def _extract_content(answer: dict, model: str) -> str:
    """Достаёт текст ответа; ошибки сервера превращает в понятное исключение."""
    if not isinstance(answer, dict):
        raise AiError("Неожиданный ответ модели: ожидался JSON-объект")
    if answer.get("error"):
        error = answer["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise AiError("Модель вернула ошибку: " + str(message))

    choices = answer.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AiError("В ответе модели нет choices — проверьте имя модели " + model)
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # Некоторые серверы отдают content массивом частей.
        content = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    if not isinstance(content, str):
        raise AiError("В ответе модели нет текстового содержимого")
    return content


def _http_error_text(exc: urllib.error.HTTPError, url: str) -> str:
    """Текст ошибки HTTP с внятным пояснением по коду и телу ответа."""
    try:
        body = exc.read().decode("utf-8", errors="replace")[:500]
    except Exception:  # noqa: BLE001
        body = ""
    hints = {
        401: "неверный [ai] api_key",
        403: "доступ запрещён — проверьте [ai] api_key",
        404: "адрес не найден — проверьте [ai] base_url (должен заканчиваться /v1)",
        400: "сервер отклонил запрос — возможно, имя модели или лишний параметр",
        422: "сервер не понял параметры запроса",
    }
    hint = hints.get(exc.code, "")
    text = "Модель вернула HTTP " + str(exc.code) + " (" + url + ")"
    if hint:
        text += ": " + hint
    if body:
        text += ". Ответ сервера: " + body.strip()
    return text


def _ssl_context(verify: bool) -> ssl.SSLContext:
    """Контекст TLS для https-эндпоинта; verify_tls=false — для самоподписанных."""
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context
