"""Промпт для разбора письма, собранный под режим отбора.

Основа взята из MailSense (packages/core/src/ai/prompt.ts): те же правила поиска
внешнего номера (сначала имя файла, потом шапка документа), даты отправки,
краткого содержания и вида документа.

Отличия — потому что эта служба пишет в журнал сама, без человека:

  * схема сокращена до полей, которые нужны CARD. Теги, приоритет, срочность,
    дата события, номер дела и описания вложений в журнал не попадают, поэтому
    модель о них больше не спрашивают;
  * корреспондент и тема берутся из справочников KORRESP и THEMA: модель
    называет организацию из бланка и выбирает тему из переданного списка;
  * в промпт передаётся сегодняшняя дата и признак пересылки;
  * схема зависит от режима отбора: если решение «регистрировать или нет»
    принимает не модель (filter_mode = all или with_attachments), полей
    register, register_reason и confidence в запросе нет. Не нужно заставлять
    модель считать уверенность, которая ни на что не влияет: короткий ответ
    устойчивее и дешевле;
  * так же убирается category, когда отсев спама выключен (skip_spam = false).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

INTRO = [
    "Ты — ассистент, который разбирает входящие деловые письма и заполняет"
    " журнал регистрации входящих документов.",
    "Отвечай ТОЛЬКО валидным JSON-объектом без markdown-разметки и без пояснений.",
    "Схема ответа:",
]


def build_system_prompt(*, need_decision: bool = True, need_category: bool = True) -> str:
    """Собирает системную часть промпта под нужный набор полей.

    need_decision — спрашивать register/register_reason/confidence (нужны только
    при filter_mode = ai); need_category — спрашивать category (нужна только
    при skip_spam = true).
    """
    fields: list[str] = []
    if need_decision:
        fields.extend(
            [
                '"register": true',
                '"register_reason": "почему письмо подлежит регистрации — одним предложением"',
                '"confidence": 0.85',
            ]
        )
    fields.extend(
        [
            '"summary": "краткое содержание ПРИЛОЖЕННЫХ документов: что это за документы и о чём они"',
            '"document_name": "вид документа: Письмо, Счёт, Договор, Акт, Запрос"',
            '"sender": "организация и/или ФИО отправителя из бланка письма"',
            '"doc_number": "номер документа (например «Исх-123/45») или null"',
            '"doc_date": "2026-01-15" или null',
            '"sheets_count": 3',
            '"thema": "точное название темы из списка «Темы из справочника» или null"',
        ]
    )
    if need_category:
        fields.append('"category": "work" | "personal" | "spam"')

    schema = "{\n" + ",\n".join("  " + item for item in fields) + "\n}"

    rules: list[str] = []
    if need_decision:
        rules.extend(
            [
                "- register: true, если письмо — входящий документ, который организация"
                " ставит на учёт: письмо организации или должностного лица, счёт,"
                " договор, акт, претензия, запрос, официальное уведомление.",
                "- register: false для личных писем, рассылок и рекламы, автоматических"
                " уведомлений сервисов (пароли, коды, подтверждения регистрации),"
                " спама, а также переписки без документа, которую в журнал не вносят.",
                "- register_reason: коротко объясни решение по register.",
                "- confidence: число от 0 до 1 — насколько уверенно заполнены номер,"
                " дата и отправитель.",
            ]
        )
    rules.extend(
        [
            "- summary: это графа «Краткое содержание» журнала, и она про ПРИЛОЖЕННЫЕ"
            " ДОКУМЕНТЫ, а не про письмо. Напиши 1–3 предложения о том, что за документы"
            " приложены (вид, номер, дата, о чём, ключевые суммы и стороны), читая их"
            " текст и сканы. Не пересказывай сопроводительный текст письма, приветствия,"
            " просьбы «рассмотреть» и «переслать»; не начинай со слов «В письме…» или"
            " «Направляем…». Если документов несколько и они о разном — по одному"
            " короткому предложению на каждый, но не больше трёх предложений.",
            "- Если вложений нет или они не читаются, кратко опиши содержание самого"
            " письма и укажи, что документов нет.",
            "- document_name: вид документа одним-двумя словами (Письмо, Счёт, Договор,"
            " Акт, Запрос, Претензия); если непонятно — «Письмо».",
            "- sender: организация и/или ФИО из бланка письма, шапки или подписи документа;"
            " если в документе не указано — из заголовка From. Только название организации"
            " и ФИО, без адреса электронной почты. По этому названию служба ищет"
            " корреспондента в справочнике, поэтому не добавляй пояснений вроде"
            " «(бывш. ОАО)».",
            "- thema: выбери РОВНО одно название из списка «Темы из справочника» и скопируй"
            " его посимвольно, ничего не переформулируя. Если ни одна тема не подходит или"
            " список пуст — null. Свою тему не придумывай.",
            "- doc_number: в ПЕРВУЮ очередь ищи номер в ИМЕНИ файла вложения (например"
            " «3175 от 31.08.2026.pdf» → «3175», «Вх. №1575_РТ от 24.07.2026.pdf» → «1575/РТ»)."
            " Если в имени файла номера нет — ищи в тексте документа (шапка). ТОЛЬКО сам"
            " номер (цифры и буквы), БЕЗ даты и БЕЗ слов «исх.», «вх.», «№», «от»."
            " Если номера нет — null.",
            "- doc_date: дата ОТПРАВКИ документа из шапки (обычно после «от» рядом с номером:"
            " «Исх. № 123 от 01.02.2026» → «2026-02-01»). Может быть и в имени файла"
            " («3175 от 31.08.2026.pdf» → «2026-08-31»). Формат ISO-8601 (YYYY-MM-DD)."
            " Это НЕ дата события и НЕ дата получения письма. Если даты нет — null.",
            "- sheets_count: количество листов документа (страницы PDF и сканов; для"
            " DOCX/XLSX — 1). Если определить нельзя — null.",
        ]
    )
    if need_category:
        rules.append("- category: work (рабочее), personal (личное), spam (спам/реклама).")
    rules.extend(
        [
            "- Если письмо переслано (в шапке «Пересылка: да»), отправитель в заголовке"
            " From — пересылающий, а не корреспондент. Организацию, номер и дату"
            " документа бери из пересланного письма: его шапки, текста и вложений.",
            "- Анализируй вложения: изображения и страницы PDF рассматривай как часть"
            " письма; извлечённый текст документов и таблиц — основа для summary,"
            " document_name, sender, doc_number, doc_date и sheets_count.",
            "- Если данных нет — null. Не придумывай номер, дату и организацию.",
        ]
    )

    return "\n".join(INTRO + [schema, "", "Правила:"] + rules)


#: Полный промпт (с решением о регистрации и категорией) — для совместимости и тестов.
DEFAULT_SYSTEM_PROMPT = build_system_prompt()

DEFAULT_USER_TEMPLATE = "\n".join(
    [
        "Сегодняшняя дата: {{today}}",
        "",
        "Метаданные письма:",
        "- Отправитель: {{from}}",
        "- Тема: {{subject}}",
        "- Дата: {{date}}",
        "- Пересылка: {{forwarded}}",
        "{{forward_note}}",
        "",
        "Текст письма:",
        "{{body}}",
        "{{attachment_list}}",
        "{{attachments}}",
        "{{notes}}",
        "",
        "{{themes}}",
        "",
        "Верни результат строго в формате JSON согласно схеме.",
    ]
)


@dataclass
class PromptSettings:
    """Переопределения промпта из файлов настроек.

    system = None означает «встроенный промпт», который собирается под режим
    отбора. Если задан свой файл, он используется как есть: служба не вырезает
    из него поля — за это отвечает автор промпта.
    """

    system: str | None = None
    user_template: str = DEFAULT_USER_TEMPLATE


@dataclass
class PromptInput:
    """Данные письма, подставляемые в шаблон."""

    subject: str = ""
    from_display: str = ""
    date: str = ""
    body: str = ""
    attachment_texts: list[str] = field(default_factory=list)
    attachment_names: list[str] = field(default_factory=list)
    attachment_notes: list[str] = field(default_factory=list)
    image_count: int = 0
    forwarded: bool = False
    today: str = ""
    themes: list[str] = field(default_factory=list)
    #: Спрашивать у модели решение о регистрации (register, confidence).
    need_decision: bool = True
    #: Спрашивать категорию письма (нужна только для отсева спама).
    need_category: bool = True


def load_prompt_settings(
    system_file: Path | None = None, template_file: Path | None = None
) -> PromptSettings:
    """Берёт промпт из файлов настроек, если они заданы (иначе — встроенный)."""
    settings = PromptSettings()
    if system_file is not None and system_file.is_file():
        text = system_file.read_text(encoding="utf-8").strip()
        if text:
            settings.system = text
    if template_file is not None and template_file.is_file():
        settings.user_template = (
            template_file.read_text(encoding="utf-8-sig").strip() or settings.user_template
        )
    return settings


def render_template(template: str, variables: dict[str, str]) -> str:
    """Подставляет {{переменные}}.

    Имена переменных могут быть кириллическими, поэтому код группы «слово»
    (в Python он Unicode-aware) предпочтительнее явного перечня A-Za-z0-9_.
    """
    import re

    def replace(match) -> str:
        key = match.group(1)
        return variables.get(key, "")

    return re.sub(r"\{\{(\w+)\}\}", replace, template)


def build_prompt(input_data: PromptInput, settings: PromptSettings | None = None) -> str:
    """Собирает единый текст промпта: системная часть + письмо (как в MailSense)."""
    opts = settings or PromptSettings()

    attachments_block = "\n\n".join(
        "=== " + name + " ===\n" + text
        for name, text in zip(input_data.attachment_names, input_data.attachment_texts)
        if text and text.strip()
    )

    notes_block = ""
    if input_data.image_count > 0:
        notes_block += (
            "К письму приложены изображения/страницы документов ("
            + str(input_data.image_count)
            + " шт.) — они переданы отдельно."
        )
    if input_data.attachment_notes:
        if notes_block:
            notes_block += "\n"
        notes_block += "Примечания по вложениям:\n" + "\n".join(
            "- " + note for note in input_data.attachment_notes
        )

    attachment_list = ""
    if input_data.attachment_names:
        attachment_list = "Вложения:\n" + "\n".join(
            "- " + name for name in input_data.attachment_names
        )

    variables = {
        "from": input_data.from_display or "(неизвестен)",
        "subject": input_data.subject,
        "date": input_data.date or "(неизвестна)",
        "body": input_data.body or "(пусто)",
        "today": input_data.today,
        "forwarded": "да" if input_data.forwarded else "нет",
        "forward_note": build_forward_note(input_data.forwarded),
        "attachment_list": attachment_list,
        "attachments": attachments_block,
        "notes": notes_block,
        "themes": build_themes_block(input_data.themes),
    }

    system = opts.system or build_system_prompt(
        need_decision=input_data.need_decision,
        need_category=input_data.need_category,
    )
    user_part = render_template(opts.user_template, variables)
    return system + "\n\n" + user_part


def build_forward_note(forwarded: bool) -> str:
    """Подсказка для пересланного письма: откуда брать корреспондента.

    Без неё модель берёт организацию из From, а там стоит тот, кто переслал
    письмо в служебный ящик, — в журнал попал бы не тот корреспондент.
    """
    if not forwarded:
        return ""
    return (
        "- Это пересылка: адрес в заголовке From принадлежит пересылающему."
        " Организацию-отправителя, номер и дату документа ищи в пересланном"
        " письме (шапка, текст, вложения)."
    )


def build_themes_block(themes: list[str]) -> str:
    """Список тем справочника для промпта.

    Пустой список — это не «тему не выбирай вообще», а «справочник не передан»:
    так честнее, чем молча подставить модели её собственную выдумку.
    """
    if not themes:
        return "Темы из справочника: не переданы — верни \"thema\": null."
    lines = "\n".join("- " + name for name in themes)
    return "Темы из справочника (выбери одну или null):\n" + lines
