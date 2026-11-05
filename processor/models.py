"""Модели данных: письмо, вложение, ответ модели, запись журнала.

Смысл разделения:
  * MailMessage — то, что пришло из почты (сырой .eml и его разобранные части);
  * PreparedAttachment — то, что из вложения удалось вытащить для модели
    (текст документа, картинки страниц для OCR, пометки об обрезке);
  * Analysis — что ответила модель (черновик полей журнала);
  * CardData — то, что уходит в журнал (поля таблицы CARD).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class Attachment:
    """Вложение письма как есть, без обработки."""

    filename: str
    mime_type: str
    content: bytes
    content_id: str | None = None
    inline: bool = False

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass
class MailMessage:
    """Разобранное письмо."""

    uid: str
    folder: str
    message_id: str
    subject: str
    from_name: str
    from_address: str
    to_display: str
    date: dt.datetime | None
    body_text: str
    body_html: str
    attachments: list[Attachment] = field(default_factory=list)
    raw: bytes = b""
    forwarded: bool = False

    @property
    def from_display(self) -> str:
        """«Имя <адрес>» — так отправитель выглядит в промпте и в журнале."""
        if self.from_name and self.from_address:
            return f"{self.from_name} <{self.from_address}>"
        return self.from_name or self.from_address

    @property
    def date_text(self) -> str:
        return self.date.strftime("%Y-%m-%d %H:%M") if self.date else ""


@dataclass
class PreparedAttachment:
    """Вложение, подготовленное к отправке в модель."""

    filename: str
    mime_type: str
    kind: str  # image | pdf | docx | xlsx | text | html | other
    extracted_text: str | None = None
    images: list[str] = field(default_factory=list)  # data-URL для OCR/vision
    handled: bool = True
    note: str | None = None
    pages: int | None = None  # страниц в документе (для графы «Кол-во листов»)


@dataclass
class Analysis:
    """Разбор письма моделью — только то, что нужно журналу.

    Набор полей сокращён по сравнению с MailSense: теги, приоритет, срочность,
    дата события, номер дела и описания вложений в CARD не пишутся, поэтому
    модель о них не спрашиваем. Корреспондент и тема приходят из справочников
    (KORRESP и THEMA) — модель лишь предлагает, что именно искать.
    """

    summary: str = ""
    document_name: str = ""
    sender: str = ""
    doc_number: str | None = None
    doc_date: dt.date | None = None
    sheets_count: int | None = None
    thema_name: str | None = None
    category: str = "personal"
    register: bool | None = None
    register_reason: str = ""
    confidence: float | None = None
    raw_response: str = ""
    model_used: str = ""


@dataclass
class CardData:
    """Запись журнала — колонки таблицы CARD.

    Набор взят из рабочей схемы базы (schema.sql): те же девять колонок, что
    пишет FastAPI_sender_to_BD, плюс ссылки на справочники — KORRESPID
    (KORRESP.ID) и THEMA (THEMA.ID).
    """

    card_type: int
    intindex: str
    intdate: dt.date
    extindex: str
    extdate: dt.date | None
    sh_contents: str
    pages_doc: int
    korresp: str
    korrespid: int = 0
    thema: int = 0
    thema_name: str = ""


@dataclass
class InsertedCard:
    """Что вернул журнал после записи."""

    card_id: int | str | None = None
    reg_number: str | None = None
    storage: str = "db"


@dataclass
class ProcessResult:
    """Итог обработки одного письма."""

    status: str  # registered | skipped | duplicate | error | dry_run
    message_id: str
    card: CardData | None = None
    analysis: Analysis | None = None
    inserted: InsertedCard | None = None
    reason: str = ""
    error: str = ""
    seconds: float = 0.0
