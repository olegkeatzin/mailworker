"""Клиент модели: запрос, повторы, ошибки, список моделей."""

from __future__ import annotations

import json

import pytest
from conftest import completion

from processor.ai.client import AiClient
from processor.config import AiConfig
from processor.errors import AiError


def make_client(server, **overrides) -> AiClient:
    values = dict(
        base_url=server.base_url,
        api_key="secret-key",
        model="test-model",
        timeout=5,
        retries=0,
    )
    values.update(overrides)
    return AiClient(AiConfig(**values))


def test_complete_returns_content(stub_ai):
    server = stub_ai([(200, completion('{"ok": true}'))])
    client = make_client(server)

    answer = client.complete([{"role": "user", "content": "разбери письмо"}])

    assert answer == '{"ok": true}'
    body = server.requests[0]
    assert body["model"] == "test-model"
    assert body["messages"][0]["content"] == "разбери письмо"
    assert body["response_format"] == {"type": "json_object"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_images_are_sent_as_parts(stub_ai):
    server = stub_ai([(200, completion("{}"))])
    client = make_client(server)

    client.complete(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "смотри"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ]
    )

    parts = server.requests[0]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_retry_after_server_error(stub_ai):
    server = stub_ai([(500, {"error": "перегрузка"}), (200, completion("ok"))])
    client = make_client(server, retries=1)

    assert client.complete([{"role": "user", "content": "x"}]) == "ok"
    assert len(server.requests) == 2


def test_auth_error_is_explained(stub_ai):
    server = stub_ai([(401, {"error": "unauthorized"})])
    client = make_client(server)

    with pytest.raises(AiError) as error:
        client.complete([{"role": "user", "content": "x"}])
    assert "api_key" in str(error.value)


def test_bad_request_is_not_retried(stub_ai):
    server = stub_ai([(400, {"error": "model not found"})])
    client = make_client(server, retries=3)

    with pytest.raises(AiError):
        client.complete([{"role": "user", "content": "x"}])
    assert len(server.requests) == 1


def test_connection_refused():
    client = AiClient(AiConfig(base_url="http://127.0.0.1:1/v1", api_key="k", model="m", retries=0))
    with pytest.raises(AiError) as error:
        client.complete([{"role": "user", "content": "x"}])
    assert "соединиться" in str(error.value)


def test_list_models(stub_ai):
    server = stub_ai()
    client = make_client(server)

    models = client.list_models()

    assert "test-model" in models
    assert server.paths[-1] == "/v1/models"


def test_list_models_non_json(monkeypatch):
    """По адресу может отвечать не модель: проверка обязана объяснить, а не падать."""

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self, *_args):
            return b"<html>Not Found</html>"

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    client = AiClient(AiConfig(base_url="http://x/v1", api_key="k", model="m"))

    with pytest.raises(AiError) as error:
        client.list_models()
    text = str(error.value)
    assert "base_url" in text or "JSON" in text


def test_error_payload_is_raised(stub_ai):
    server = stub_ai([(200, {"error": {"message": "модель не загружена"}})])
    client = make_client(server)

    with pytest.raises(AiError) as error:
        client.complete([{"role": "user", "content": "x"}])
    assert "не загружена" in str(error.value)


def test_content_parts_are_joined(stub_ai):
    server = stub_ai(
        [(200, {"choices": [{"message": {"content": [{"type": "text", "text": "часть"}]}}]})]
    )
    client = make_client(server)
    assert client.complete([{"role": "user", "content": "x"}]) == "часть"


def test_extra_request_fields(stub_ai):
    server = stub_ai([(200, completion("{}"))])
    client = make_client(server, request_extra={"top_k": 20})
    client.complete([{"role": "user", "content": "x"}])
    assert server.requests[0]["top_k"] == 20
