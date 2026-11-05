"""Конвейер обработки письма, учёт состояния и проход по ящику."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import build_email, make_config

from processor.classifiers import ClassifierService, Correspondent, Theme
from processor.config import AppConfig
from processor.mail.sources import FetchedMessage
from processor.models import InsertedCard
from processor.pipeline import (
    STATUS_DUPLICATE,
    STATUS_DRY_RUN,
    STATUS_ERROR,
    STATUS_REGISTERED,
    STATUS_SKIPPED,
    EmailProcessor,
)
from processor.service import MailWorkerService
from processor.state import ProcessedState

ANALYSIS = {
    "register": True,
    "register_reason": "входящий документ",
    "confidence": 0.9,
    "summary": "Счёт на оплату по договору 45",
    "document_name": "Счёт",
    "sender": "ООО «Ромашка»",
    "doc_number": "45/12",
    "doc_date": "2026-10-18",
    "sheets_count": 2,
    "tags": ["счёт"],
    "priority": 4,
    "category": "work",
}


class FakeAiClient:
    """Модель-заглушка: на OCR отвечает текстом, на разбор — заданным JSON."""

    def __init__(self, payload: dict | None = None, ocr_text: str = "ТЕКСТ СКАНА") -> None:
        self.payload = payload if payload is not None else ANALYSIS
        self.ocr_text = ocr_text
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def complete(self, messages, **options):
        self.calls.append({"messages": messages, "options": options})
        if self.error is not None:
            raise self.error
        if options.get("json_mode") is False:
            return self.ocr_text
        return json.dumps(self.payload, ensure_ascii=False)


class FakeJournal:
    """Журнал-заглушка: запоминает записи вместо обращения к Firebird."""

    def __init__(self) -> None:
        self.cards = []
        self.analyses = []
        self.error: Exception | None = None

    def insert_card(self, card, analysis=None):
        if self.error is not None:
            raise self.error
        self.cards.append(card)
        self.analyses.append(analysis)
        return InsertedCard(card_id=136 + len(self.cards), reg_number="новая", storage="db")

    def close(self):
        pass


@pytest.fixture()
def env(workdir: Path):
    cfg = AppConfig.load(make_config(workdir))
    state = ProcessedState(cfg.processing.state_path)
    client = FakeAiClient()
    journal = FakeJournal()
    processor = EmailProcessor(cfg, client, journal, state)
    return cfg, state, client, journal, processor


def test_successful_registration(env, simple_eml):
    cfg, state, client, journal, processor = env

    result = processor.process_raw(simple_eml, uid="1", folder="INBOX")

    assert result.status == STATUS_REGISTERED
    assert len(journal.cards) == 1
    card = journal.cards[0]
    assert card.extindex == "45/12"
    assert card.korresp == "ООО «Ромашка»"
    assert card.sh_contents.startswith("Счёт на оплату")
    assert result.inserted is not None and result.inserted.card_id == 137
    assert state.counts() == {"registered": 1}

    # Исходное письмо сохранено для разбора спорных случаев.
    archived = list(cfg.processing.archive_dir.rglob("*.eml"))
    assert len(archived) == 1


def test_second_run_is_duplicate(env, simple_eml):
    _cfg, _state, _client, journal, processor = env
    processor.process_raw(simple_eml, uid="1", folder="INBOX")
    result = processor.process_raw(simple_eml, uid="1", folder="INBOX")

    assert result.status == STATUS_DUPLICATE
    assert len(journal.cards) == 1


def test_dry_run_writes_nothing(env, simple_eml):
    _cfg, state, _client, journal, processor = env
    result = processor.process_raw(simple_eml, uid="1", folder="INBOX", dry_run=True)

    assert result.status == STATUS_DRY_RUN
    assert result.card is not None
    assert journal.cards == []
    assert state.counts() == {}


def test_model_refuses_registration(env, simple_eml):
    _cfg, state, client, journal, processor = env
    client.payload = dict(ANALYSIS, register=False, register_reason="личная переписка")

    result = processor.process_raw(simple_eml, uid="1", folder="INBOX")

    assert result.status == STATUS_SKIPPED
    assert "личная переписка" in result.reason
    assert journal.cards == []
    assert state.counts() == {"skipped": 1}


def test_register_flag_ignored_when_disabled(env, simple_eml):
    cfg, _state, client, journal, processor = env
    cfg = cfg.__class__(
        **{
            **cfg.__dict__,
            "processing": _replace(cfg.processing, require_register_flag=False),
        }
    )
    client.payload = dict(ANALYSIS, register=False)
    processor = EmailProcessor(cfg, client, journal, ProcessedState(cfg.processing.state_path))

    result = processor.process_raw(simple_eml, uid="1", folder="INBOX")

    assert result.status == STATUS_REGISTERED


def test_filter_all_ignores_model_decision(env, simple_eml):
    cfg, _state, client, journal, processor = env
    cfg = cfg.__class__(**{**cfg.__dict__, "processing": _replace(cfg.processing, filter_mode="all")})
    client.payload = dict(ANALYSIS, register=False)
    processor = EmailProcessor(cfg, client, journal, ProcessedState(cfg.processing.state_path))

    assert processor.process_raw(simple_eml).status == STATUS_REGISTERED


def test_filter_with_attachments(env):
    cfg, _state, client, journal, processor = env
    cfg = cfg.__class__(
        **{**cfg.__dict__, "processing": _replace(cfg.processing, filter_mode="with_attachments")}
    )
    processor = EmailProcessor(cfg, client, journal, ProcessedState(cfg.processing.state_path))

    without = build_email()
    result = processor.process_raw(without, uid="1")
    assert result.status == STATUS_SKIPPED
    assert "вложений" in result.reason

    with_attachment = build_email(
        message_id="with-attachment@test",
        attachments=[("счёт.txt", "text/plain", b"text")],
    )
    assert processor.process_raw(with_attachment, uid="2").status == STATUS_REGISTERED


def test_spam_is_skipped(env, simple_eml):
    _cfg, _state, client, journal, processor = env
    client.payload = dict(ANALYSIS, category="spam")

    result = processor.process_raw(simple_eml, uid="1")

    assert result.status == STATUS_SKIPPED
    assert "спам" in result.reason
    assert journal.cards == []


def test_min_confidence(env, simple_eml):
    cfg, _state, client, journal, processor = env
    cfg = cfg.__class__(
        **{**cfg.__dict__, "processing": _replace(cfg.processing, min_confidence=0.95)}
    )
    client.payload = dict(ANALYSIS, confidence=0.4)
    processor = EmailProcessor(cfg, client, journal, ProcessedState(cfg.processing.state_path))

    result = processor.process_raw(simple_eml, uid="1")

    assert result.status == STATUS_SKIPPED
    assert "уверенность" in result.reason


def test_ai_error_is_recorded_and_retried(env, simple_eml):
    from processor.errors import AiError

    _cfg, state, client, journal, processor = env
    client.error = AiError("модель недоступна")

    first = processor.process_raw(simple_eml, uid="1")
    assert first.status == STATUS_ERROR
    assert "модель недоступна" in first.error
    assert state.counts() == {"error": 1}

    client.error = None
    second = processor.process_raw(simple_eml, uid="1")
    assert second.status == STATUS_REGISTERED


def test_error_is_not_retried_when_disabled(env, simple_eml):
    from processor.errors import AiError

    cfg, state, client, journal, processor = env
    cfg = cfg.__class__(
        **{**cfg.__dict__, "processing": _replace(cfg.processing, retry_errors=False)}
    )
    state = ProcessedState(cfg.processing.state_path)
    processor = EmailProcessor(cfg, client, journal, state)
    client.error = AiError("сбой")

    assert processor.process_raw(simple_eml, uid="1").status == STATUS_ERROR
    client.error = None
    assert processor.process_raw(simple_eml, uid="1").status == STATUS_DUPLICATE


def test_broken_model_answer_is_error(env, simple_eml):
    _cfg, _state, client, journal, processor = env
    client.payload = {"не": "по схеме"}

    result = processor.process_raw(simple_eml, uid="1")

    # Пустой разбор — это письмо без регистрации, а не падение.
    assert result.status == STATUS_SKIPPED
    assert journal.cards == []


def test_bad_json_is_error(env, simple_eml, monkeypatch):
    _cfg, _state, client, journal, processor = env
    monkeypatch.setattr(client, "complete", lambda *a, **k: "это не JSON")

    result = processor.process_raw(simple_eml, uid="1")
    assert result.status == STATUS_ERROR
    assert "JSON" in result.error


def test_journal_error_is_error(env, simple_eml):
    from processor.errors import StorageError

    _cfg, state, _client, journal, processor = env
    journal.error = StorageError("нет связи с базой")

    result = processor.process_raw(simple_eml, uid="1")

    assert result.status == STATUS_ERROR
    assert state.counts() == {"error": 1}


def test_ocr_text_reaches_prompt(env):
    """Скан из вложения должен попасть в промпт для основной модели."""
    _cfg, _state, client, journal, processor = env
    pil = pytest.importorskip("PIL.Image")
    import io

    buffer = io.BytesIO()
    pil.new("RGB", (80, 60), "white").save(buffer, format="PNG")

    raw = build_email(attachments=[("скан.png", "image/png", buffer.getvalue())])
    result = processor.process_raw(raw, uid="1")

    assert result.status == STATUS_REGISTERED
    # Один запрос — OCR (без json_mode), второй — анализ письма.
    assert len(client.calls) == 2
    ocr_call, analysis_call = client.calls
    assert ocr_call["options"].get("json_mode") is False
    prompt = analysis_call["messages"][0]["content"][0]["text"]
    assert "ТЕКСТ СКАНА" in prompt
    assert "--- стр. 1 ---" in prompt


def test_service_run_once_marks_processed(workdir):
    cfg = AppConfig.load(make_config(workdir))
    state = ProcessedState(cfg.processing.state_path)
    client = FakeAiClient()
    journal = FakeJournal()
    processor = EmailProcessor(cfg, client, journal, state)

    messages = {
        "1": build_email(message_id="m1@test"),
        "2": build_email(message_id="m2@test"),
    }
    source = FakeSource(messages)
    service = MailWorkerService(cfg, processor, source_factory=lambda _cfg: source)

    stats = service.run_once()

    assert stats.fetched == 2
    assert stats.registered == 2
    assert source.marked == ["1", "2"]
    assert source.closed is True


def test_service_does_not_mark_failed_messages(workdir):
    from processor.errors import AiError

    cfg = AppConfig.load(make_config(workdir))
    state = ProcessedState(cfg.processing.state_path)
    client = FakeAiClient()
    client.error = AiError("сбой модели")
    journal = FakeJournal()
    processor = EmailProcessor(cfg, client, journal, state)

    source = FakeSource({"1": build_email()})
    service = MailWorkerService(cfg, processor, source_factory=lambda _cfg: source)
    stats = service.run_once()

    assert stats.errors == 1
    assert source.marked == []  # письмо останется для повтора


def test_service_reports_source_error(workdir):
    cfg = AppConfig.load(make_config(workdir))
    processor = EmailProcessor(
        cfg, FakeAiClient(), FakeJournal(), ProcessedState(cfg.processing.state_path)
    )

    class BrokenSource(FakeSource):
        def connect(self):
            from processor.errors import MailSourceError

            raise MailSourceError("сервер недоступен")

    service = MailWorkerService(cfg, processor, source_factory=lambda _cfg: BrokenSource({}))
    stats = service.run_once()

    assert "недоступен" in stats.source_error
    assert "ошибка ящика" in stats.as_text()


class FakeSource:
    """Источник писем для тестов: отдаёт заранее подготовленные сообщения."""

    def __init__(self, messages: dict[str, bytes]) -> None:
        self.messages = messages
        self.marked: list[str] = []
        self.closed = False
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def fetch(self, limit: int = 20) -> list[FetchedMessage]:
        return [FetchedMessage(uid=uid, raw=raw) for uid, raw in self.messages.items()][:limit]

    def mark_processed(self, uid: str) -> None:
        self.marked.append(uid)

    def close(self) -> None:
        self.closed = True


def _replace(dataclass_instance, **changes):
    import dataclasses

    return dataclasses.replace(dataclass_instance, **changes)

# --------------------------------------------------------------- справочники


class FakeDictionaryLoader:
    """Источник справочников для конвейера."""

    def __init__(self) -> None:
        self.correspondents = [Correspondent(id=42, name="ООО «Ромашка»")]
        self.themes = [Theme(id=1, name="Бухгалтерия"), Theme(id=2, name="Кадры")]
        self.created: list[str] = []

    def load_correspondents(self):
        return list(self.correspondents)

    def load_themes(self):
        return list(self.themes)

    def create_correspondent(self, name):
        item = Correspondent(id=99, name=name)
        self.correspondents.append(item)
        self.created.append(name)
        return item

    def create_theme(self, name):
        item = Theme(id=98, name=name)
        self.themes.append(item)
        self.created.append(name)
        return item


def make_clf_env(workdir: Path, **overrides):
    cfg = AppConfig.load(
        make_config(workdir, {"classifiers": {"enabled": "true", **overrides}})
    )
    loader = FakeDictionaryLoader()
    service = ClassifierService(cfg.classifiers, loader)
    client = FakeAiClient()
    journal = FakeJournal()
    state = ProcessedState(cfg.processing.state_path)
    processor = EmailProcessor(
        cfg, client, journal, state, classifiers=service
    )
    return cfg, state, client, journal, processor, loader


def test_classifiers_fill_card(workdir):
    _cfg, _state, client, journal, processor, _loader = make_clf_env(workdir)
    client.payload = dict(ANALYSIS, sender="ООО Ромашка", thema="Бухгалтерия")

    result = processor.process_raw(build_email(), uid="1")

    assert result.status == STATUS_REGISTERED
    card = journal.cards[0]
    assert card.korresp == "ООО «Ромашка»"
    assert card.korrespid == 42
    assert card.thema == 1
    assert card.thema_name == "Бухгалтерия"


def test_theme_list_reaches_prompt(workdir):
    _cfg, _state, client, _journal, processor, _loader = make_clf_env(workdir)
    client.payload = dict(ANALYSIS, sender="ООО Ромашка", thema="Кадры")

    processor.process_raw(build_email(), uid="1")

    prompt = client.calls[-1]["messages"][0]["content"][0]["text"]
    assert "Темы из справочника" in prompt
    assert "- Бухгалтерия" in prompt
    assert "- Кадры" in prompt


def test_unknown_correspondent_skip(workdir):
    _cfg, _state, client, journal, processor, _loader = make_clf_env(
        workdir, unknown_correspondent="skip"
    )
    client.payload = dict(ANALYSIS, sender="Неизвестная фирма", thema=None)

    result = processor.process_raw(build_email(), uid="1")

    assert result.status == STATUS_SKIPPED
    assert "KORRESP" in result.reason
    assert journal.cards == []


def test_unknown_thema_skip(workdir):
    _cfg, _state, client, journal, processor, _loader = make_clf_env(
        workdir, unknown_thema="skip"
    )
    client.payload = dict(ANALYSIS, sender="ООО Ромашка", thema="Неизвестная тема")

    result = processor.process_raw(build_email(), uid="1")

    assert result.status == STATUS_SKIPPED
    assert "THEMA" in result.reason
    assert journal.cards == []


def test_unknown_correspondent_create(workdir):
    _cfg, _state, client, journal, processor, loader = make_clf_env(
        workdir, unknown_correspondent="create"
    )
    client.payload = dict(ANALYSIS, sender="Новая фирма", thema=None)

    result = processor.process_raw(build_email(), uid="1")

    assert result.status == STATUS_REGISTERED
    assert loader.created == ["Новая фирма"]
    assert journal.cards[0].korrespid == 99


def test_dry_run_does_not_create_dictionary_entries(workdir):
    _cfg, _state, client, journal, processor, loader = make_clf_env(
        workdir, unknown_correspondent="create"
    )
    client.payload = dict(ANALYSIS, sender="Новая фирма", thema=None)

    result = processor.process_raw(build_email(), uid="1", dry_run=True)

    assert result.status == STATUS_DRY_RUN
    assert loader.created == []
    assert journal.cards == []

def test_all_mode_ignores_confidence_and_spam_flag(env, simple_eml):
    """В режиме all модель ничего не решает: фильтры ai к письму не применяются."""
    cfg, _state, client, journal, processor = env
    cfg = cfg.__class__(
        **{
            **cfg.__dict__,
            "processing": _replace(
                cfg.processing, filter_mode="all", min_confidence=0.99, skip_spam=False
            ),
        }
    )
    client.payload = dict(ANALYSIS, register=False, confidence=0.1)
    processor = EmailProcessor(cfg, client, journal, ProcessedState(cfg.processing.state_path))

    result = processor.process_raw(simple_eml, uid="1")

    assert result.status == STATUS_REGISTERED
    assert "filter_mode = all" in result.reason


def test_spam_is_skipped_even_in_all_mode(env, simple_eml):
    """skip_spam — отдельный выключатель, он действует в любом режиме."""
    cfg, _state, client, journal, processor = env
    cfg = cfg.__class__(
        **{
            **cfg.__dict__,
            "processing": _replace(cfg.processing, filter_mode="all", skip_spam=True),
        }
    )
    client.payload = dict(ANALYSIS, category="spam")
    processor = EmailProcessor(cfg, client, journal, ProcessedState(cfg.processing.state_path))

    assert processor.process_raw(simple_eml, uid="1").status == STATUS_SKIPPED


def test_forwarded_letter_uses_letterhead_sender_not_from(workdir):
    """В ящике пересылки From — пересылающий; корреспондент берётся из письма."""
    cfg = AppConfig.load(
        make_config(workdir, {"classifiers": {"enabled": "true"}})
    )
    loader = FakeDictionaryLoader()
    service = ClassifierService(cfg.classifiers, loader)
    client = FakeAiClient()
    journal = FakeJournal()
    processor = EmailProcessor(
        cfg,
        client,
        journal,
        ProcessedState(cfg.processing.state_path),
        classifiers=service,
    )
    # From — известный корреспондент из справочника, но письмо пересланное,
    # а в бланке другая организация.
    client.payload = dict(ANALYSIS, sender="Совсем другая организация", thema=None)
    raw = build_email(
        subject="Fwd: Письмо",
        from_name="ООО «Ромашка»",
        from_address="romashka@example.ru",
        body="---------- Пересылаемое сообщение ----------\nТекст",
    )

    result = processor.process_raw(raw, uid="1")

    assert result.status == STATUS_REGISTERED
    card = journal.cards[0]
    assert card.korrespid == 0
    assert card.korresp == "Совсем другая организация"


def test_forwarded_note_reaches_prompt(workdir):
    cfg = AppConfig.load(make_config(workdir))
    client = FakeAiClient()
    processor = EmailProcessor(
        cfg, client, FakeJournal(), ProcessedState(cfg.processing.state_path)
    )
    raw = build_email(subject="Fwd: Письмо", body="---------- Пересылаемое сообщение ----------")

    processor.process_raw(raw, uid="1")

    prompt = client.calls[-1]["messages"][0]["content"][0]["text"]
    assert "Это пересылка" in prompt


def test_attached_eml_is_parsed(workdir):
    """Пересланное письмо вложением .eml: его текст должен дойти до модели."""
    inner = build_email(
        subject="Исходное письмо",
        from_name="АО «Вектор»",
        from_address="vector@example.ru",
        body="Текст исходного письма про договор",
        message_id="inner@test",
    )
    outer = build_email(
        subject="Fwd: Исходное письмо",
        body="Пересылаю.",
        message_id="outer@test",
        attachments=[("Исходное письмо.eml", "message/rfc822", inner)],
    )
    cfg = AppConfig.load(make_config(workdir))
    client = FakeAiClient()
    processor = EmailProcessor(
        cfg, client, FakeJournal(), ProcessedState(cfg.processing.state_path)
    )

    processor.process_raw(outer, uid="1")

    prompt = client.calls[-1]["messages"][0]["content"][0]["text"]
    assert "Пересланное письмо" in prompt
    assert "АО «Вектор»" in prompt
    assert "Текст исходного письма про договор" in prompt

def test_all_mode_does_not_ask_model_for_decision(workdir):
    """Модель не должна считать register/confidence, если решение уже принято."""
    cfg = AppConfig.load(
        make_config(workdir, {"processing": {"filter_mode": "all", "skip_spam": "false"}})
    )
    client = FakeAiClient()
    processor = EmailProcessor(
        cfg, client, FakeJournal(), ProcessedState(cfg.processing.state_path)
    )

    processor.process_raw(build_email(), uid="1")

    prompt = client.calls[-1]["messages"][0]["content"][0]["text"]
    assert "register" not in prompt
    assert "confidence" not in prompt
    assert "category" not in prompt
    assert '"summary"' in prompt


def test_ai_mode_asks_model_for_decision(workdir):
    cfg = AppConfig.load(
        make_config(workdir, {"processing": {"filter_mode": "ai", "skip_spam": "true"}})
    )
    client = FakeAiClient()
    processor = EmailProcessor(
        cfg, client, FakeJournal(), ProcessedState(cfg.processing.state_path)
    )

    processor.process_raw(build_email(), uid="1")

    prompt = client.calls[-1]["messages"][0]["content"][0]["text"]
    assert '"register"' in prompt
    assert '"confidence"' in prompt
    assert '"category"' in prompt
