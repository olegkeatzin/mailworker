"""Учёт обработанных писем: защита от повторной записи в журнал."""

from __future__ import annotations

import pytest

from processor.state import ProcessedState


@pytest.fixture()
def state(workdir):
    return ProcessedState(workdir / "state.sqlite")


def test_new_message_is_not_skipped(state):
    assert state.should_skip("key") is False
    assert state.counts() == {}


def test_registered_message_is_skipped(state):
    state.mark_registered("key", message_id="m1", uid="1", card_id=136, reg_number="новая")

    assert state.should_skip("key") is True
    record = state.get("key")
    assert record["card_id"] == "136"
    assert record["status"] == "registered"


def test_skipped_message_is_not_analyzed_again(state):
    state.mark_skipped("key", message_id="m1", uid="1", reason="спам")
    assert state.should_skip("key") is True


def test_error_is_retried_by_default(state):
    state.mark_error("key", message_id="m1", uid="1", error="сбой модели")

    assert state.should_skip("key", retry_errors=True) is False
    assert state.should_skip("key", retry_errors=False) is True


def test_state_survives_restart(workdir):
    path = workdir / "state.sqlite"
    ProcessedState(path).mark_registered("key", message_id="m1", uid="1", card_id=1)

    reopened = ProcessedState(path)
    assert reopened.should_skip("key") is True


def test_counts_and_last_errors(state):
    state.mark_registered("a", message_id="m1", uid="1", card_id=1)
    state.mark_skipped("b", message_id="m2", uid="2", reason="рассылка")
    state.mark_error("c", message_id="m3", uid="3", error="модель недоступна")

    assert state.counts() == {"registered": 1, "skipped": 1, "error": 1}
    errors = state.last_errors()
    assert errors[0]["message_id"] == "m3"
    assert "недоступна" in errors[0]["reason"]


def test_repeated_mark_overwrites(state):
    state.mark_error("key", message_id="m1", uid="1", error="сбой")
    state.mark_registered("key", message_id="m1", uid="1", card_id=5)
    assert state.get("key")["status"] == "registered"
