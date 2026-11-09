"""Источники писем: команды IMAP и POP3 без живого сервера."""

from __future__ import annotations

import datetime as dt
import email.utils
import imaplib
import poplib

import pytest

from processor.config import MailConfig
from processor.errors import MailSourceError
from processor.mail.sources import ImapMailSource, Pop3MailSource

RAW = b"Subject: Test\r\n\r\nbody"

#: Настоящий класс ошибки imaplib: модуль подменяется целиком в фикстуре.
IMAP_ERROR = imaplib.IMAP4.error


class FakeImap:
    """Подмена imaplib.IMAP4_SSL: записывает команды и отдаёт письма."""

    instances: list["FakeImap"] = []
    uids: list[bytes] = [b"1", b"2", b"3"]
    fail_login = False

    def __init__(self, host=None, port=None, ssl_context=None, timeout=None):
        self.host = host
        self.port = port
        self.commands: list[tuple] = []
        self.selected: tuple | None = None
        self.logged: tuple | None = None
        self.closed = False
        FakeImap.instances.append(self)

    def login(self, user, password):
        if FakeImap.fail_login:
            raise IMAP_ERROR("[AUTHENTICATIONFAILED] Invalid credentials")
        self.logged = (user, password)
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.commands.append((command,) + args)
        if command == "SEARCH":
            return "OK", [b" ".join(FakeImap.uids)]
        if command == "FETCH":
            return "OK", [(b"1 (BODY[] {18}", RAW), b")"]
        if command == "STORE":
            return "OK", [b"ok"]
        return "OK", [b""]

    def close(self):
        self.closed = True

    def logout(self):
        return "BYE", [b"bye"]


@pytest.fixture()
def fake_imap(monkeypatch):
    FakeImap.instances = []
    FakeImap.uids = [b"1", b"2", b"3"]
    FakeImap.fail_login = False
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    monkeypatch.setattr(imaplib, "IMAP4", FakeImap)
    return FakeImap


def make_mail_cfg(**overrides) -> MailConfig:
    values = dict(
        protocol="imap",
        host="mail.test",
        port=993,
        tls="ssl",
        username="inbox@test",
        password="secret",
        mailbox="INBOX",
        mode="unseen",
        mark_seen=True,
        max_per_cycle=20,
    )
    values.update(overrides)
    return MailConfig(**values)


def test_imap_connects_and_selects(fake_imap):
    source = ImapMailSource(make_mail_cfg())
    source.connect()

    connection = fake_imap.instances[0]
    assert connection.logged == ("inbox@test", "secret")
    assert connection.selected == ("INBOX", False)
    assert connection.host == "mail.test"


def test_imap_readonly_when_not_marking_seen(fake_imap):
    source = ImapMailSource(make_mail_cfg(mark_seen=False))
    source.connect()
    assert fake_imap.instances[0].selected == ("INBOX", True)


def test_imap_fetch_takes_last_messages(fake_imap):
    source = ImapMailSource(make_mail_cfg())
    source.connect()

    messages = source.fetch(limit=2)

    assert [item.uid for item in messages] == ["2", "3"]
    assert messages[0].raw == RAW
    assert ("SEARCH", None, "UNSEEN") in fake_imap.instances[0].commands


def _imap_day(days_back: int) -> str:
    day = dt.date.today() - dt.timedelta(days=days_back)
    months = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    return "%02d-%s-%s" % (day.day, months[day.month - 1], day.year)


def test_imap_since_criteria_uses_english_months(fake_imap):
    source = ImapMailSource(make_mail_cfg(mode="since_days", since_days=7))
    source.connect()
    source.fetch(limit=1)

    search = [cmd for cmd in fake_imap.instances[0].commands if cmd[0] == "SEARCH"][0]
    assert search[2] == "SINCE"
    # «За 7 дней» — это сегодня и шесть предыдущих, а не семь предыдущих.
    assert search[3] == _imap_day(6)


def test_since_days_one_means_today_only(fake_imap):
    """since_days = 1 — только сегодняшние письма, вчерашние не читаются."""
    source = ImapMailSource(make_mail_cfg(mode="since_days", since_days=1))
    source.connect()
    source.fetch(limit=1)

    search = [cmd for cmd in fake_imap.instances[0].commands if cmd[0] == "SEARCH"][0]
    assert search[3] == _imap_day(0)


@pytest.mark.parametrize("days, days_back", [(1, 0), (2, 1), (3, 2), (7, 6), (30, 29)])
def test_since_window_is_calendar_days_including_today(days, days_back):
    source = ImapMailSource(make_mail_cfg(mode="all", since_days=days))
    assert source.window_date() == dt.date.today() - dt.timedelta(days=days_back)


def test_window_is_off_when_zero(fake_imap):
    source = ImapMailSource(make_mail_cfg(mode="unseen", since_days=0))
    assert source.window_date() is None
    source.connect()
    source.fetch(limit=1)
    search = [cmd for cmd in fake_imap.instances[0].commands if cmd[0] == "SEARCH"][0]
    assert search[2:] == ("UNSEEN",)


def test_unseen_mode_with_window(fake_imap):
    """Окно по дате не зависит от режима: unseen + since_days = 1."""
    source = ImapMailSource(make_mail_cfg(mode="unseen", since_days=1))
    source.connect()
    source.fetch(limit=1)

    search = [cmd for cmd in fake_imap.instances[0].commands if cmd[0] == "SEARCH"][0]
    assert search[2] == "UNSEEN"
    assert search[3] == "SINCE"
    assert search[4] == _imap_day(0)


def test_all_mode_with_window(fake_imap):
    source = ImapMailSource(make_mail_cfg(mode="all", since_days=2))
    source.connect()
    source.fetch(limit=1)

    search = [cmd for cmd in fake_imap.instances[0].commands if cmd[0] == "SEARCH"][0]
    assert search[2] == "ALL"
    assert search[3] == "SINCE"
    assert search[4] == _imap_day(1)


def test_imap_marks_seen_only_when_asked(fake_imap):
    source = ImapMailSource(make_mail_cfg())
    source.connect()
    source.mark_processed("7")

    store = [cmd for cmd in fake_imap.instances[0].commands if cmd[0] == "STORE"][0]
    assert store[1] == "7"
    assert "Seen" in store[3]


def test_imap_does_not_mark_when_disabled(fake_imap):
    source = ImapMailSource(make_mail_cfg(mark_seen=False))
    source.connect()
    source.mark_processed("7")

    assert not [cmd for cmd in fake_imap.instances[0].commands if cmd[0] == "STORE"]


def test_imap_login_error_is_explained(fake_imap):
    fake_imap.fail_login = True
    with pytest.raises(MailSourceError) as error:
        ImapMailSource(make_mail_cfg()).connect()
    assert "username" in str(error.value) or "password" in str(error.value)


def test_imap_close_is_safe(fake_imap):
    source = ImapMailSource(make_mail_cfg())
    source.connect()
    source.close()
    source.close()  # повторное закрытие не падает


# ----------------------------------------------------------------------- POP3


def _mail_date(days_ago: int) -> str:
    day = dt.datetime.combine(
        dt.date.today() - dt.timedelta(days=days_ago), dt.time(12, 0)
    )
    return email.utils.format_datetime(day)


class FakePop3:
    instances: list["FakePop3"] = []
    #: Письма с датами: №1 — вчерашнее, №2 — сегодняшнее.
    messages: dict[int, bytes] = {}
    with_uidl = True
    with_top = True

    def __init__(self, host=None, port=None, context=None, timeout=None):
        self.host = host
        self.port = port
        self.deleted: list[int] = []
        self.retrieved: list[int] = []
        self.quit_called = False
        self.close_called = False
        self.logged: tuple | None = None
        FakePop3.instances.append(self)

    def user(self, username):
        self.username = username
        return b"+OK"

    def pass_(self, password):
        self.logged = (getattr(self, "username", ""), password)
        return b"+OK"

    def uidl(self):
        if not FakePop3.with_uidl:
            raise poplib.error_proto("-ERR UIDL not supported")
        return b"+OK", [str(n).encode() + b" UID" + str(n).encode() for n in self.messages], 10

    def list(self):
        return b"+OK", [str(n).encode() + b" 10" for n in self.messages], 10

    def retr(self, number):
        self.retrieved.append(number)
        return b"+OK", self.messages[number].split(b"\r\n"), 10

    def top(self, number, which):
        if not FakePop3.with_top:
            raise poplib.error_proto("-ERR TOP not supported")
        header = self.messages[number].split(b"\r\n\r\n", 1)[0]
        return b"+OK", header.split(b"\r\n"), 10

    def dele(self, number):
        self.deleted.append(number)
        return b"+OK"

    def quit(self):
        self.quit_called = True
        return b"+OK"

    def close(self):
        self.close_called = True


@pytest.fixture()
def fake_pop3(monkeypatch):
    FakePop3.instances = []
    FakePop3.with_uidl = True
    FakePop3.with_top = True
    FakePop3.messages = {
        1: ("Subject: One\r\nDate: " + _mail_date(1) + "\r\n\r\nbody1").encode(),
        2: ("Subject: Two\r\nDate: " + _mail_date(0) + "\r\n\r\nbody2").encode(),
    }
    monkeypatch.setattr(poplib, "POP3_SSL", FakePop3)
    monkeypatch.setattr(poplib, "POP3", FakePop3)
    return FakePop3


def test_pop3_fetch_uses_uidl(fake_pop3):
    source = Pop3MailSource(make_mail_cfg(protocol="pop3", port=995))
    source.connect()

    messages = source.fetch(limit=5)

    assert [item.uid for item in messages] == ["UID1", "UID2"]
    assert messages[0].raw.startswith(b"Subject: One")
    assert source.count() == 2


def test_pop3_keeps_messages_by_default(fake_pop3):
    source = Pop3MailSource(make_mail_cfg(protocol="pop3", port=995))
    source.connect()
    source.mark_processed("UID1")
    source.close()

    connection = fake_pop3.instances[0]
    assert connection.deleted == []
    assert connection.close_called is True


def test_pop3_deletes_when_enabled(fake_pop3):
    source = Pop3MailSource(
        make_mail_cfg(protocol="pop3", port=995, delete_after=True)
    )
    source.connect()
    source.mark_processed("UID2")
    source.close()

    connection = fake_pop3.instances[0]
    assert connection.deleted == [2]
    assert connection.quit_called is True


def test_pop3_without_uidl_uses_numbers(fake_pop3):
    FakePop3.with_uidl = False
    source = Pop3MailSource(make_mail_cfg(protocol="pop3", port=995))
    source.connect()

    messages = source.fetch(limit=1)

    # Номерные идентификаторы тоже устойчивы: берём последнее письмо в ящике.
    assert messages[0].uid == "pop3-2"

def test_pop3_unseen_mode_with_window_skips_old(fake_pop3):
    """Основной случай: mode = unseen и since_days = 1 — старые письма не читаются.

    POP3 не умеет «непрочитанные», поэтому именно окно по дате ограничивает
    выборку. Раньше оно терялось, если mode отличался от since_days.
    """
    source = Pop3MailSource(
        make_mail_cfg(protocol="pop3", port=995, mode="unseen", since_days=1)
    )
    source.connect()

    messages = source.fetch(limit=10)

    assert [item.uid for item in messages] == ["UID2"]
    assert fake_pop3.instances[0].retrieved == [2]


def test_pop3_since_days_skips_old_messages(fake_pop3):
    """Старый способ mode = since_days продолжает работать."""
    source = Pop3MailSource(
        make_mail_cfg(protocol="pop3", port=995, mode="since_days", since_days=1)
    )
    source.connect()

    messages = source.fetch(limit=10)

    assert [item.uid for item in messages] == ["UID2"]
    # Заголовков TOP хватило, чтобы отбросить старое письмо без RETR.
    assert fake_pop3.instances[0].retrieved == [2]


def test_pop3_since_days_two_days_includes_yesterday(fake_pop3):
    source = Pop3MailSource(
        make_mail_cfg(protocol="pop3", port=995, mode="since_days", since_days=2)
    )
    source.connect()

    messages = source.fetch(limit=10)

    assert [item.uid for item in messages] == ["UID1", "UID2"]


def test_pop3_since_days_without_top_still_filters(fake_pop3):
    """Если сервер не умеет TOP, письмо скачивается, но в обработку не идёт."""
    fake_pop3.with_top = False
    source = Pop3MailSource(
        make_mail_cfg(protocol="pop3", port=995, mode="since_days", since_days=1)
    )
    source.connect()

    messages = source.fetch(limit=10)

    assert [item.uid for item in messages] == ["UID2"]
    assert fake_pop3.instances[0].retrieved == [1, 2]


def test_pop3_since_days_keeps_message_without_date(fake_pop3):
    """Письмо без даты считается свежим: лучше обработать, чем потерять."""
    fake_pop3.messages[2] = b"Subject: Two\r\n\r\nbody2"
    source = Pop3MailSource(
        make_mail_cfg(protocol="pop3", port=995, mode="since_days", since_days=1)
    )
    source.connect()

    messages = source.fetch(limit=10)

    assert [item.uid for item in messages] == ["UID2"]


def test_pop3_without_window_reads_all(fake_pop3):
    source = Pop3MailSource(make_mail_cfg(protocol="pop3", port=995, mode="all", since_days=0))
    source.connect()

    messages = source.fetch(limit=10)

    assert [item.uid for item in messages] == ["UID1", "UID2"]


def test_pop3_all_mode_with_window(fake_pop3):
    source = Pop3MailSource(make_mail_cfg(protocol="pop3", port=995, mode="all", since_days=1))
    source.connect()

    messages = source.fetch(limit=10)

    assert [item.uid for item in messages] == ["UID2"]
