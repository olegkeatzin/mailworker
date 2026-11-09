"""Проверки разбора .ini: значения, умолчания, ошибки."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_config

from processor.config import AppConfig
from processor.errors import ConfigError

EXAMPLE = Path(__file__).resolve().parents[1] / "config.ini.example"


def test_reads_all_sections(workdir):
    cfg = AppConfig.load(make_config(workdir))

    assert cfg.mail.host == "mail.test"
    assert cfg.mail.port == 993
    assert cfg.mail.mode == "all"
    assert cfg.database.charset == "WIN1251"
    assert cfg.ai.model == "test-model"
    assert cfg.journal.intindex == "новая"
    assert cfg.journal.storage == "db"
    assert cfg.processing.filter_mode == "ai"


def test_missing_file_is_explained(workdir):
    with pytest.raises(ConfigError) as error:
        AppConfig.load(workdir / "нет.ini")
    assert "не найден" in str(error.value)


def test_relative_paths_are_relative_to_ini(workdir):
    cfg = AppConfig.load(make_config(workdir))
    assert cfg.processing.state_path == workdir / "data" / "state.sqlite"
    assert cfg.processing.archive_dir == workdir / "data" / "processed"


def test_all_errors_reported_at_once(workdir):
    path = make_config(
        workdir,
        {
            "mail": {"host": "", "username": ""},
            "ai": {"model": ""},
            "journal": {"storage": "интерфейс"},
        },
    )
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    text = str(error.value)
    assert "[mail] host" in text
    assert "[mail] username" in text
    assert "[ai] model" in text
    assert "[journal] storage" in text


def test_percent_and_backslash_are_literal(workdir):
    """Пароль с % не должен трактоваться как подстановка configparser."""
    path = make_config(workdir, {"mail": {"password": "pa%ss\\word"}})
    cfg = AppConfig.load(path)
    assert cfg.mail.password == "pa%ss\\word"


def test_port_inferred_from_protocol(workdir):
    cfg = AppConfig.load(make_config(workdir, {"mail": {"protocol": "pop3", "port": ""}}))
    assert cfg.mail.port == 995


def test_invalid_boolean_and_number(workdir):
    path = make_config(
        workdir, {"mail": {"mark_seen": "может быть"}, "ai": {"timeout": "долго"}}
    )
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    assert "[mail] mark_seen" in str(error.value)
    assert "[ai] timeout" in str(error.value)


def test_imap_delete_after_rejected(workdir):
    path = make_config(workdir, {"mail": {"protocol": "imap", "delete_after": "true"}})
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    assert "delete_after" in str(error.value)


def test_require_mail_false_allows_empty_mailbox(workdir):
    path = make_config(
        workdir, {"mail": {"host": "", "username": "", "password": ""}}
    )
    cfg = AppConfig.load(path, require_mail=False)
    assert cfg.mail.host == ""


def test_storage_api_needs_no_database(workdir):
    path = make_config(
        workdir,
        {
            "journal": {"storage": "api", "api_url": "http://127.0.0.1:8000/"},
            "database": {"host": "", "database": ""},
        },
    )
    cfg = AppConfig.load(path)
    assert cfg.journal.storage == "api"
    assert cfg.journal.api_url == "http://127.0.0.1:8000"


def test_request_extra_json(workdir):
    path = make_config(workdir, {"ai": {"request_extra_json": '{"top_k": 20}'}})
    cfg = AppConfig.load(path)
    assert cfg.ai.request_extra == {"top_k": 20}


def test_broken_request_extra_json(workdir):
    path = make_config(workdir, {"ai": {"request_extra_json": "{oops"}})
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    assert "request_extra_json" in str(error.value)


def test_ocr_falls_back_to_main_model(workdir):
    cfg = AppConfig.load(make_config(workdir))
    assert cfg.ocr.model == "test-model"
    assert cfg.ocr.base_url == cfg.ai.base_url


def test_shipped_example_is_valid():
    """Шаблон config.ini.example должен читаться без ошибок: его копируют."""
    cfg = AppConfig.load(EXAMPLE)

    assert cfg.mail.protocol == "imap"
    assert cfg.database.database.endswith(".fdb")
    assert cfg.journal.storage == "db"
    assert cfg.journal.doc_number_fallback == "none"
    assert cfg.journal.max_sh_contents_length == 512
    assert cfg.classifiers.enabled is True
    # Ящик с пересланной отобранной почтой — регистрируются все письма.
    assert cfg.mail.since_days == 0
    assert cfg.processing.filter_mode == "all"
    assert cfg.processing.skip_spam is False
    assert cfg.processing.require_register_flag is True
    assert cfg.processing.min_confidence == 0.0
    assert cfg.processing.state_path.is_absolute()


def test_ocr_has_own_model(workdir):
    path = make_config(
        workdir, {"ocr": {"base_url": "http://ocr:9000/v1", "model": "ocr-model"}}
    )
    cfg = AppConfig.load(path)
    assert cfg.ocr.model == "ocr-model"
    assert cfg.ocr.base_url == "http://ocr:9000/v1"

# ------------------------------------------------------------- лимиты и справочники


def test_journal_column_limits_from_schema(workdir):
    """Длины по умолчанию совпадают с длинами колонок CARD в схеме базы."""
    cfg = AppConfig.load(make_config(workdir))

    assert cfg.journal.max_intindex_length == 20
    assert cfg.journal.max_extindex_length == 20
    assert cfg.journal.max_korresp_length == 100
    assert cfg.journal.max_sh_contents_length == 512


def test_journal_lengths_are_configurable(workdir):
    path = make_config(
        workdir,
        {"journal": {"max_sh_contents_length": "1000", "max_korresp_length": "150"}},
    )
    cfg = AppConfig.load(path)
    assert cfg.journal.max_sh_contents_length == 1000
    assert cfg.journal.max_korresp_length == 150


def test_classifiers_defaults(workdir):
    path = make_config(workdir)
    text = path.read_text(encoding="utf-8").replace(
        "[classifiers]\nenabled = false\n", ""
    )
    path.write_text(text, encoding="utf-8")
    cfg = AppConfig.load(path)

    assert cfg.classifiers.enabled is True
    assert "FROM KORRESP" in cfg.classifiers.correspondents_sql
    assert "FROM THEMA" in cfg.classifiers.themes_sql
    assert cfg.classifiers.match_threshold == 0.85
    assert cfg.classifiers.unknown_correspondent == "model"
    assert cfg.classifiers.unknown_thema == "none"


def test_classifiers_need_database(workdir):
    path = make_config(
        workdir,
        {
            "classifiers": {"enabled": "true"},
            "journal": {"storage": "api"},
            "database": {"host": "", "database": ""},
        },
    )
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    assert "[classifiers]" in str(error.value)


def test_classifiers_can_be_disabled_without_database(workdir):
    path = make_config(
        workdir,
        {
            "classifiers": {"enabled": "false"},
            "journal": {"storage": "api"},
            "database": {"host": "", "database": ""},
        },
    )
    cfg = AppConfig.load(path)
    assert cfg.classifiers.enabled is False


def test_email_map_path_is_relative_to_ini(workdir):
    path = make_config(
        workdir, {"classifiers": {"email_map_file": "classifiers/emails.csv"}}
    )
    cfg = AppConfig.load(path)
    assert cfg.classifiers.email_map_file == workdir / "classifiers" / "emails.csv"


def test_bad_match_threshold(workdir):
    path = make_config(workdir, {"classifiers": {"match_threshold": "0.2"}})
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    assert "match_threshold" in str(error.value)


def test_unknown_policy_values(workdir):
    path = make_config(workdir, {"classifiers": {"unknown_correspondent": "завести"}})
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    assert "unknown_correspondent" in str(error.value)

def test_admission_defaults_when_keys_absent(workdir):
    """По умолчанию — регистрировать всё: так работает ящик пересылки."""
    path = make_config(workdir)
    ini = path.read_text(encoding="utf-8")
    ini = ini.replace('filter_mode = ai\n', "").replace('skip_spam = true\n', "")
    path.write_text(ini, encoding="utf-8")

    cfg = AppConfig.load(path)

    assert cfg.processing.filter_mode == "all"
    assert cfg.processing.skip_spam is False


def test_admission_settings_are_in_processing(workdir):
    path = make_config(
        workdir,
        {
            "processing": {
                "filter_mode": "ai",
                "require_register_flag": "false",
                "min_confidence": "0.75",
                "skip_spam": "true",
            }
        },
    )
    cfg = AppConfig.load(path)

    assert cfg.processing.require_register_flag is False
    assert cfg.processing.min_confidence == 0.75
    assert cfg.processing.skip_spam is True

def test_mail_window_defaults_off(workdir):
    """По умолчанию окно по дате выключено: обрабатывается всё, что попадает под режим."""
    cfg = AppConfig.load(make_config(workdir))
    assert cfg.mail.since_days == 0


def test_mail_window_with_unseen_mode(workdir):
    """since_days действует в любом режиме, а не только при mode = since_days."""
    path = make_config(workdir, {"mail": {"mode": "unseen", "since_days": "1"}})
    cfg = AppConfig.load(path)
    assert cfg.mail.mode == "unseen"
    assert cfg.mail.since_days == 1


def test_mode_since_days_requires_window(workdir):
    path = make_config(workdir, {"mail": {"mode": "since_days", "since_days": "0"}})
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    assert "since_days" in str(error.value)

def test_intindex_value_from_config(workdir):
    path = make_config(workdir, {"journal": {"intindex": "вх"}})
    cfg = AppConfig.load(path)
    assert cfg.journal.intindex == "вх"


def test_intindex_legacy_key_still_works(workdir):
    """Старый ключ reg_number читается, если intindex не задан."""
    path = make_config(workdir)
    text = path.read_text(encoding="utf-8").replace(
        "intindex = новая", "reg_number = вх2"
    )
    path.write_text(text, encoding="utf-8")

    cfg = AppConfig.load(path)
    assert cfg.journal.intindex == "вх2"


def test_intindex_cannot_be_empty(workdir):
    path = make_config(workdir, {"journal": {"intindex": ""}})
    with pytest.raises(ConfigError) as error:
        AppConfig.load(path)
    assert "intindex" in str(error.value)