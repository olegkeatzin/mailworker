"""Справочники: нормализация названий, сопоставление и политики."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from processor.classifiers import (
    ClassifierService,
    Correspondent,
    ResolvedCorrespondent,
    Theme,
    load_email_map,
    normalize_name,
    similarity,
)
from processor.config import ClassifiersConfig
from processor.models import Analysis, MailMessage


def make_mail(**overrides) -> MailMessage:
    values = dict(
        uid="1",
        folder="INBOX",
        message_id="m@test",
        subject="Письмо",
        from_name="ООО «Ромашка»",
        from_address="romashka@example.ru",
        to_display="inbox@test",
        date=dt.datetime(2026, 10, 20, 10, 0),
        body_text="",
        body_html="",
    )
    values.update(overrides)
    return MailMessage(**values)


class FakeLoader:
    """Подставной источник справочников: списки в памяти."""

    def __init__(self, correspondents=None, themes=None) -> None:
        self.correspondents = list(correspondents or [])
        self.themes = list(themes or [])
        self.loads = 0
        self.fail_after: int | None = None
        self.created_correspondents: list[str] = []
        self.created_themes: list[str] = []
        self.next_id = 500

    def load_correspondents(self):
        self.loads += 1
        if self.fail_after is not None and self.loads > self.fail_after:
            raise RuntimeError("база недоступна")
        return list(self.correspondents)

    def load_themes(self):
        if self.fail_after is not None and self.loads > self.fail_after:
            raise RuntimeError("база недоступна")
        return list(self.themes)

    def create_correspondent(self, name):
        self.created_correspondents.append(name)
        self.next_id += 1
        item = Correspondent(id=self.next_id, name=name)
        self.correspondents.append(item)
        return item

    def create_theme(self, name):
        self.created_themes.append(name)
        self.next_id += 1
        item = Theme(id=self.next_id, name=name)
        self.themes.append(item)
        return item


def make_service(loader, **overrides) -> ClassifierService:
    values = dict(enabled=True, refresh_minutes=10, match_threshold=0.85)
    values.update(overrides)
    return ClassifierService(ClassifiersConfig(**values), loader)


@pytest.fixture()
def loader() -> FakeLoader:
    return FakeLoader(
        correspondents=[
            Correspondent(id=42, name="ООО «Ромашка»"),
            Correspondent(id=43, name="АО «Вектор»"),
            Correspondent(id=44, name="ПАО «Ромашка Торговый Дом»"),
        ],
        themes=[
            Theme(id=1, name="Бухгалтерия"),
            Theme(id=2, name="Кадры"),
            Theme(id=3, name="Договоры"),
        ],
    )


# --------------------------------------------------------------- нормализация


@pytest.mark.parametrize(
    "value, expected",
    [
        ("ООО «Ромашка»", "ромашка"),
        ("ООО Ромашка (Москва)", "ромашка"),
        ('АО "Вектор"', "вектор"),
        ("  Ёлки   и Палки  ", "елки и палки"),
        ("ПАО «Ромашка Торговый Дом»", "ромашка торговый дом"),
    ],
)
def test_normalize_name(value, expected):
    assert normalize_name(value) == expected


def test_similarity_cases():
    assert similarity("ООО «Ромашка»", "ООО Ромашка") == 1.0
    assert similarity("Ромашка", "Ромашка Торговый Дом") > 0.9
    assert similarity("Ромашка", "Вектор") < 0.5
    assert similarity("", "Вектор") == 0.0


# --------------------------------------------------------------- файл адресов


def test_email_map_is_loaded(workdir: Path):
    path = workdir / "emails.csv"
    path.write_text(
        "email,correspondent_id,correspondent_name\n"
        "Romashka@Example.ru,42,\n"
        "ivanov@example.ru,,ООО «Ромашка»\n"
        "плохая строка,not-a-number,\n",
        encoding="utf-8",
    )
    mapping = load_email_map(path)

    assert mapping["romashka@example.ru"] == (42, "")
    assert mapping["ivanov@example.ru"] == (0, "ООО «Ромашка»")
    assert "плохая строка" not in mapping


def test_missing_email_map_is_not_an_error(workdir: Path):
    assert load_email_map(workdir / "нет.csv") == {}
    assert load_email_map(None) == {}


# --------------------------------------------------------------- сопоставление


def test_match_by_locked_email(loader, workdir: Path):
    path = workdir / "emails.csv"
    path.write_text("email,correspondent_id,correspondent_name\nromashka@example.ru,42,\n", encoding="utf-8")
    service = make_service(loader, email_map_file=path)
    service.ensure_loaded()

    found, score, source, _detail = service.match_correspondent(
        ["Совсем другое название"], email="romashka@example.ru"
    )

    assert found is not None and found.id == 42
    assert score == 1.0
    assert source == "email"


def test_match_by_name_above_threshold(loader):
    service = make_service(loader)
    service.ensure_loaded()

    found, score, source, _detail = service.match_correspondent(["ООО Ромашка"], email=None)

    # Точное совпадение после нормализации важнее «похожего» длинного названия.
    assert found is not None
    assert found.id == 42
    assert source == "name"
    assert score == 1.0


def test_match_below_threshold(loader):
    """При высоком пороге похожее, но не совпадающее название не принимается."""
    service = make_service(loader, match_threshold=1.0)
    service.ensure_loaded()

    found, score, source, _detail = service.match_correspondent(
        ["Ромашка и партнёры"], email=None
    )

    assert found is None
    assert source == "none"
    assert score < 1.0


def test_no_match_returns_none(loader):
    service = make_service(loader)
    service.ensure_loaded()

    found, _score, source, _detail = service.match_correspondent(["Совсем другая фирма"], email=None)

    assert found is None
    assert source == "none"


def test_theme_exact_and_fuzzy(loader):
    service = make_service(loader)
    service.ensure_loaded()

    exact, _score, source = service.match_theme("Бухгалтерия")
    assert exact is not None and exact.id == 1 and source == "model"

    fuzzy, _score, source = service.match_theme("бухгалтерия и налоги")
    assert fuzzy is not None and fuzzy.id == 1 and source == "name"


def test_theme_not_found(loader):
    service = make_service(loader)
    service.ensure_loaded()
    found, _score, source = service.match_theme("Совсем не тема")
    assert found is None and source == "none"


def test_theme_names_for_prompt_are_limited(loader):
    service = make_service(loader, themes_in_prompt=2)
    service.ensure_loaded()
    assert service.theme_names_for_prompt() == ["Бухгалтерия", "Кадры"]

    service = make_service(loader, themes_in_prompt=0)
    service.ensure_loaded()
    assert service.theme_names_for_prompt() == []


# -------------------------------------------------------------------- политики


def test_resolve_correspondent_from_catalog(loader):
    service = make_service(loader)
    service.ensure_loaded()

    resolved = service.resolve_correspondent(Analysis(sender="ООО Ромашка"), make_mail())

    assert resolved.matched is True
    assert resolved.id in (42, 44)
    assert resolved.source == "name"


def test_forwarded_letter_ignores_from(loader):
    """У пересылки From — пересылающий; его нельзя брать за корреспондента."""
    service = make_service(loader)
    service.ensure_loaded()
    mail = make_mail(
        forwarded=True, from_name="ООО «Ромашка»", from_address="romashka@example.ru"
    )

    resolved = service.resolve_correspondent(Analysis(sender="Другая организация"), mail)

    assert resolved.id == 0
    assert resolved.name == "Другая организация"


def test_regular_letter_uses_from_as_fallback(loader):
    service = make_service(loader)
    service.ensure_loaded()

    resolved = service.resolve_correspondent(Analysis(sender=""), make_mail())

    assert resolved.id == 42
    assert resolved.matched is True


def test_resolve_correspondent_fallback_model(loader):
    service = make_service(loader, unknown_correspondent="model")
    service.ensure_loaded()

    # Имя в заголовке From тоже участвует в поиске, поэтому оно тоже «чужое».
    resolved = service.resolve_correspondent(
        Analysis(sender="Неизвестная фирма"), make_mail(from_name="Неизвестная фирма")
    )

    assert resolved.matched is False
    assert resolved.name == "Неизвестная фирма"
    assert resolved.id == 0


def test_resolve_correspondent_policy_none(loader):
    service = make_service(loader, unknown_correspondent="none")
    service.ensure_loaded()

    resolved = service.resolve_correspondent(
        Analysis(sender="Неизвестная фирма"), make_mail(from_name="Неизвестная фирма")
    )

    assert resolved.name == ""
    assert resolved.id == 0


def test_resolve_correspondent_policy_skip(loader):
    service = make_service(loader, unknown_correspondent="skip")
    service.ensure_loaded()

    resolved = service.resolve_correspondent(
        Analysis(sender="Неизвестная фирма"), make_mail(from_name="Неизвестная фирма")
    )

    assert resolved.rejected is True


def test_resolve_correspondent_policy_create(loader):
    service = make_service(loader, unknown_correspondent="create")
    service.ensure_loaded()

    resolved = service.resolve_correspondent(
        Analysis(sender="Новая организация"), make_mail(from_name="Новая организация"), create=True
    )

    assert resolved.matched is True
    assert resolved.source == "created"
    assert resolved.id > 0
    assert loader.created_correspondents == ["Новая организация"]


def test_create_is_not_done_on_dry_run(loader):
    service = make_service(loader, unknown_correspondent="create")
    service.ensure_loaded()

    resolved = service.resolve_correspondent(
        Analysis(sender="Новая организация"), make_mail(from_name="Новая организация"), create=False
    )

    assert resolved.source == "model"
    assert loader.created_correspondents == []


def test_resolve_theme_policies(loader):
    service = make_service(loader, unknown_thema="skip")
    service.ensure_loaded()
    assert service.resolve_theme(Analysis(thema_name="Не тема")).rejected is True

    service = make_service(loader, unknown_thema="create")
    service.ensure_loaded()
    created = service.resolve_theme(Analysis(thema_name="Новая тема"), create=True)
    assert created.matched is True and created.id > 0
    assert loader.created_themes == ["Новая тема"]

    service = make_service(loader, unknown_thema="none")
    service.ensure_loaded()
    empty = service.resolve_theme(Analysis(thema_name="Другая тема"))
    assert empty.id == 0 and empty.source == "none"


# ---------------------------------------------------------------------- кэш


def test_dictionaries_are_cached(loader):
    service = make_service(loader, refresh_minutes=10)
    service.ensure_loaded()
    service.ensure_loaded()
    assert loader.loads == 1


def test_refresh_failure_keeps_old_list(loader):
    service = make_service(loader, refresh_minutes=0)
    service.ensure_loaded()
    loader.fail_after = 1
    service.ensure_loaded(force=True)  # ошибка обновления не должна ронять службу

    assert len(service.correspondents) == 3


def test_first_load_failure_is_raised(loader):
    loader.fail_after = 0
    service = make_service(loader)
    with pytest.raises(RuntimeError):
        service.ensure_loaded()


def test_disabled_service_uses_model_values(loader):
    service = ClassifierService(ClassifiersConfig(enabled=False), loader)
    resolved = service.resolve_correspondent(Analysis(sender="ООО Письмо"), make_mail())
    assert resolved.source == "model"
    assert resolved.name == "ООО Письмо"
    assert service.theme_names_for_prompt() == []
