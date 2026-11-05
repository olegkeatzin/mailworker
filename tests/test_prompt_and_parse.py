"""Промпт и разбор ответа модели."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from processor.ai.parse import AnalysisParseError, extract_json, parse_analysis
from processor.ai.prompt import (
    DEFAULT_SYSTEM_PROMPT,
    PromptInput,
    PromptSettings,
    build_prompt,
    build_system_prompt,
    render_template,
)


def test_prompt_contains_letter_and_attachments():
    prompt = build_prompt(
        PromptInput(
            subject="Счёт №45",
            from_display="ООО «Ромашка» <romashka@example.ru>",
            date="2026-10-20 10:00",
            body="Направляем счёт.",
            attachment_texts=["Счёт на оплату 45"],
            attachment_names=["счёт.pdf"],
            attachment_notes=["счёт.pdf: распознаны первые 1 страниц из 3"],
            image_count=1,
            forwarded=False,
            today="2026-11-05",
        )
    )
    assert "ООО «Ромашка»" in prompt
    assert "Счёт №45" in prompt
    assert "Направляем счёт." in prompt
    assert "=== счёт.pdf ===" in prompt
    assert "Счёт на оплату 45" in prompt
    assert "изображения/страницы документов (1 шт.)" in prompt
    assert "распознаны первые 1 страниц из 3" in prompt
    assert "Сегодняшняя дата: 2026-11-05" in prompt
    assert "Пересылка: нет" in prompt


def test_prompt_marks_forwarded_and_empty_body():
    prompt = build_prompt(PromptInput(subject="Тема", body="", forwarded=True))
    assert "Пересылка: да" in prompt
    assert "(пусто)" in prompt  # пустое тело подписано, а не оставлено дырой


def test_forwarding_note_for_forwarded_letter():
    """Модель должна знать, что From — пересылающий, а не корреспондент."""
    prompt = build_prompt(PromptInput(subject="Fwd: Письмо", forwarded=True))
    assert "Это пересылка" in prompt
    assert "From принадлежит пересылающему" in prompt


def test_no_forwarding_note_for_regular_letter():
    prompt = build_prompt(PromptInput(subject="Письмо", forwarded=False))
    assert "Это пересылка" not in prompt


def test_prompt_contains_theme_list():
    prompt = build_prompt(
        PromptInput(subject="Письмо", themes=["Бухгалтерия", "Кадры", "Договоры"])
    )
    assert "Темы из справочника (выбери одну или null):" in prompt
    assert "- Бухгалтерия" in prompt
    assert "- Кадры" in prompt


def test_prompt_without_themes_asks_null():
    prompt = build_prompt(PromptInput(subject="Письмо"))
    assert "Темы из справочника: не переданы" in prompt
    assert 'верни "thema": null' in prompt


def test_custom_template_placeholders():
    settings = PromptSettings(system="СИСТЕМА", user_template="{{subject}}|{{from}}|{{unknown}}")
    prompt = build_prompt(PromptInput(subject="Т", from_display="Ф"), settings)
    assert prompt == "СИСТЕМА\n\nТ|Ф|"


def test_render_template_cyrillic_variable_names():
    assert render_template("{{тема}}!", {"тема": "Письмо"}) == "Письмо!"


def test_summary_is_about_attached_documents():
    """SH_CONTENTS — содержание документов, а не пересказ письма."""
    prompt = build_prompt(PromptInput(subject="Письмо", need_decision=False))

    # Описание поля в схеме.
    assert "ПРИЛОЖЕННЫХ документов" in prompt
    # Правило: про документы, без пересказа сопроводительного текста.
    assert "ПРИЛОЖЕННЫЕ ДОКУМЕНТЫ" in prompt
    assert "Не пересказывай сопроводительный текст письма" in prompt
    # Поведение, когда документов нет.
    assert "Если вложений нет или они не читаются" in prompt


def test_attachments_rule_points_to_documents():
    prompt = build_prompt(PromptInput(subject="Письмо"))
    assert "основа для summary" in prompt


def test_schema_without_decision_fields():
    """В режиме all модель не спрашивают про register и уверенность."""
    prompt = build_prompt(
        PromptInput(subject="Письмо", need_decision=False, need_category=False)
    )
    assert "register" not in prompt
    assert "confidence" not in prompt
    assert "category" not in prompt
    assert '"summary"' in prompt
    assert '"thema"' in prompt


def test_schema_with_decision_fields():
    prompt = build_prompt(
        PromptInput(subject="Письмо", need_decision=True, need_category=True)
    )
    assert '"register"' in prompt
    assert '"confidence"' in prompt
    assert '"category"' in prompt


def test_category_can_be_dropped_alone():
    prompt = build_prompt(
        PromptInput(subject="Письмо", need_decision=True, need_category=False)
    )
    assert '"register"' in prompt
    assert "category" not in prompt


def test_default_prompt_asks_only_journal_fields():
    """Теги, приоритет, срочность и номер дела в журнал не пишутся — и не спрашиваются."""
    assert "register" in DEFAULT_SYSTEM_PROMPT
    assert "confidence" in DEFAULT_SYSTEM_PROMPT
    assert "thema" in DEFAULT_SYSTEM_PROMPT
    for dropped in ('"tags"', '"priority"', '"urgent"', '"event_date"', '"case_number"'):
        assert dropped not in DEFAULT_SYSTEM_PROMPT


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == '{"a": 1}'
    assert extract_json('Вот ответ:\n{"a": 1}') == '{"a": 1}'
    assert extract_json("текст {\"a\": 1} хвост") == '{"a": 1}'
    assert extract_json("") == ""


def test_extract_json_from_code_fence():
    fence = chr(96) * 3
    raw = fence + "json\n{\"a\": 1}\n" + fence
    assert extract_json(raw) == '{"a": 1}'


def _analysis(**overrides) -> dict:
    payload = {
        "register": True,
        "register_reason": "входящий документ",
        "confidence": 0.9,
        "summary": "Счёт на оплату",
        "document_name": "Счёт",
        "sender": "ООО «Ромашка»",
        "doc_number": "45/12",
        "doc_date": "2026-10-20",
        "sheets_count": 2,
        "thema": "Бухгалтерия",
        "category": "work",
    }
    payload.update(overrides)
    return payload


def test_parse_full_answer():
    analysis = parse_analysis(json.dumps(_analysis(), ensure_ascii=False), "model-x")
    assert analysis.register is True
    assert analysis.confidence == 0.9
    assert analysis.summary == "Счёт на оплату"
    assert analysis.document_name == "Счёт"
    assert analysis.sender == "ООО «Ромашка»"
    assert analysis.doc_number == "45/12"
    assert analysis.doc_date == dt.date(2026, 10, 20)
    assert analysis.sheets_count == 2
    assert analysis.thema_name == "Бухгалтерия"
    assert analysis.category == "work"
    assert analysis.model_used == "model-x"


def test_parse_accepts_mailsense_field_names():
    analysis = parse_analysis(
        json.dumps(
            {
                "external_number": "Исх-1/2",
                "send_date": "01.02.2026",
                "documentName": "Письмо",
                "sender_org": "АО «Вектор»",
                "sheetsCount": "3",
            },
            ensure_ascii=False,
        )
    )
    assert analysis.doc_number == "Исх-1/2"
    assert analysis.doc_date == dt.date(2026, 2, 1)
    assert analysis.document_name == "Письмо"
    assert analysis.sender == "АО «Вектор»"
    assert analysis.sheets_count == 3


@pytest.mark.parametrize("key", ["thema", "theme", "topic", "тема"])
def test_theme_aliases(key):
    analysis = parse_analysis(json.dumps({key: "Кадры"}, ensure_ascii=False))
    assert analysis.thema_name == "Кадры"


def test_empty_strings_become_none():
    analysis = parse_analysis(
        json.dumps({"doc_number": "  ", "doc_date": "", "thema": "null", "summary": ""})
    )
    assert analysis.doc_number is None
    assert analysis.doc_date is None
    assert analysis.thema_name is None
    assert analysis.summary == ""


def test_unknown_fields_are_ignored():
    """Старый промпт или чужая модель могут прислать лишние поля — это не ошибка."""
    analysis = parse_analysis(
        json.dumps({"tags": ["счёт"], "priority": 5, "urgent": True, "summary": "Ок"})
    )
    assert analysis.summary == "Ок"


def test_confidence_clamped():
    analysis = parse_analysis(json.dumps({"confidence": 87, "register": "да"}))
    assert analysis.confidence == 0.87
    assert analysis.register is True


def test_register_false_and_yes_no_strings():
    assert parse_analysis(json.dumps({"register": False})).register is False
    assert parse_analysis(json.dumps({"register": "нет"})).register is False
    assert parse_analysis(json.dumps({"register": "true"})).register is True
    assert parse_analysis(json.dumps({})).register is None


def test_russian_date_and_datetime():
    assert parse_analysis(json.dumps({"doc_date": "15 января 2026"})).doc_date == dt.date(2026, 1, 15)
    assert parse_analysis(
        json.dumps({"doc_date": "2026-01-15T00:00:00+03:00"})
    ).doc_date == dt.date(2026, 1, 15)


def test_bad_json_raises_with_snippet():
    with pytest.raises(AnalysisParseError) as error:
        parse_analysis("не JSON вовсе")
    assert "не является JSON" in str(error.value)


def test_empty_answer_raises():
    with pytest.raises(AnalysisParseError):
        parse_analysis("   ")


def test_json_array_raises():
    with pytest.raises(AnalysisParseError):
        parse_analysis("[1, 2, 3]")
