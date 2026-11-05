"""Справочники журнала: корреспонденты (KORRESP) и темы (THEMA).

Зачем это нужно. В базе журнала корреспондент — это запись справочника KORRESP
(ID и NAME), а тема письма — ссылка THEMA на одноимённый справочник. Если писать
туда текст от модели, в журнале появятся десятки «ООО Ромашка», «ООО «Ромашка»»
и «Ромашка ООО» — и отчёты по корреспондентам перестанут сходиться. Поэтому:

  * корреспондент ищется в KORRESP: сначала по закреплённому почтовому адресу
    (CSV-файл соответствий), затем по названию — точному и похожему;
  * тема выбирается из THEMA: модель получает список названий и возвращает одно
    из них (или null), служба проверяет выбор по справочнику;
  * если ничего не найдено, действует настройка «что делать» — писать как
    определила модель, не писать вовсе или завести новую запись справочника.

Сопоставление по названию терпимо к тому, как название записано: кавычки, «ё»,
ООО/АО/ПАО, лишние пробелы и знаки препинания не мешают. Это важно: в KORRESP
названия заведены годами, и ждать совпадения «символ в символ» от письма нельзя.
"""

from __future__ import annotations

import csv
import logging
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

from .config import ClassifiersConfig
from .models import Analysis, MailMessage

logger = logging.getLogger(__name__)

#: Правовые формы и служебные слова, которые не участвуют в сравнении названий.
#: «ООО «Ромашка»» и «Ромашка» должны считаться одинаковыми.
_LEGAL_FORMS = frozenset(
    {
        "ооо",
        "оао",
        "зао",
        "пао",
        "ао",
        "ип",
        "нко",
        "ано",
        "фгуп",
        "гуп",
        "муп",
        "гбу",
        "мбу",
        "гку",
        "кгбу",
        "фгбоу",
        "во",
        "llc",
        "ltd",
        "inc",
        "jsc",
        "corp",
        "gmbh",
    }
)

#: Слова, которые в названиях организаций ничего не различают.
_STOP_WORDS = frozenset({"им", "имени", "г", "город", "гор", "п", "пос", "с", "село"})

_QUOTES = re.compile("[\"'«»„“”" + chr(96) + "]")
_PARENS = re.compile(r"\([^)]*\)")
_NON_WORD = re.compile(r"[^0-9a-zа-я]+")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Correspondent:
    """Запись справочника KORRESP."""

    id: int
    name: str
    numbpart: str = ""
    shows: bool = True


@dataclass(frozen=True)
class Theme:
    """Запись справочника THEMA."""

    id: int
    name: str
    numbpart: str = ""
    shows: bool = True


@dataclass
class ResolvedCorrespondent:
    """Что писать в KORRESP и KORRESPID."""

    name: str = ""
    id: int = 0
    matched: bool = False
    source: str = "none"  # email | name | model | none | created | skip
    score: float = 0.0
    detail: str = ""

    @property
    def rejected(self) -> bool:
        return self.source == "skip"


@dataclass
class ResolvedTheme:
    """Что писать в CARD.THEMA."""

    id: int = 0
    name: str = ""
    matched: bool = False
    source: str = "none"  # model | name | none | created | skip
    score: float = 0.0

    @property
    def rejected(self) -> bool:
        return self.source == "skip"


class DictionaryLoader(Protocol):
    """Источник справочников. В работе — Firebird, в тестах — список."""

    def load_correspondents(self) -> list[Correspondent]: ...

    def load_themes(self) -> list[Theme]: ...

    def create_correspondent(self, name: str) -> Correspondent: ...

    def create_theme(self, name: str) -> Theme: ...


# ---------------------------------------------------------------- нормализация


def normalize_name(value: str) -> str:
    """Приводит название к сравнимому виду.

    Убирает кавычки, скобочные пояснения, правовые формы и пунктуацию:
    «ООО «Ромашка» (Москва)» и «ромашка» дают одно и то же слово.
    """
    text = (value or "").lower().replace("ё", "е")
    text = _PARENS.sub(" ", text)
    text = _QUOTES.sub(" ", text)
    text = _NON_WORD.sub(" ", text)
    words = [word for word in text.split() if word]
    words = [word for word in words if word not in _LEGAL_FORMS and word not in _STOP_WORDS]
    return " ".join(words)


def similarity(left: str, right: str) -> float:
    """Похожесть двух названий от 0 до 1.

    Правила по убыванию строгости: совпадение после нормализации, вхождение
    одного названия в другое, затем общая похожесть строк. Вхождение считается
    высоким совпадением: «Ромашка» и «Ромашка Торговый Дом» — одна организация.
    """
    first = normalize_name(left)
    second = normalize_name(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    if first in second or second in first:
        shorter = min(len(first), len(second))
        longer = max(len(first), len(second))
        return min(0.99, 0.90 + 0.09 * (shorter / longer))
    return SequenceMatcher(None, first, second).ratio()


# ------------------------------------------------------------ файл адресов


def load_email_map(path: Path | None) -> dict[str, tuple[int, str]]:
    """Читает соответствия «почтовый адрес -> корреспондент».

    В KORRESP нет почтовых адресов, поэтому точное сопоставление частых
    отправителей ведётся по отдельному файлу. Формат — CSV с заголовком:

        email,correspondent_id,correspondent_name
        romashka@example.ru,42,ООО «Ромашка»

    Достаточно одного из двух последних столбцов: по id служба найдёт актуальное
    название в справочнике, по названию — саму запись.
    """
    mapping: dict[str, tuple[int, str]] = {}
    if path is None:
        return mapping
    if not path.is_file():
        logger.warning("Файл соответствий адресов не найден: %s", path)
        return mapping
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for number, row in enumerate(reader, start=2):
                email = (row.get("email") or "").strip().lower()
                if not email:
                    continue
                raw_id = (row.get("correspondent_id") or "").strip()
                name = (row.get("correspondent_name") or "").strip()
                try:
                    correspondent_id = int(raw_id) if raw_id else 0
                except ValueError:
                    logger.warning(
                        "%s, строка %s: correspondent_id «%s» — не число, строка пропущена",
                        path.name,
                        number,
                        raw_id,
                    )
                    continue
                mapping[email] = (correspondent_id, name)
    except OSError as exc:
        logger.warning("Не удалось прочитать файл соответствий %s: %s", path, exc)
    if mapping:
        logger.info("Загружено соответствий адресов: %s (%s)", len(mapping), path)
    return mapping


# ------------------------------------------------------------------- сервис


class ClassifierService:
    """Загружает справочники и сопоставляет с ними письмо."""

    def __init__(self, cfg: ClassifiersConfig, loader: DictionaryLoader | None = None) -> None:
        self.cfg = cfg
        self.loader = loader if cfg.enabled else None
        self._correspondents: list[Correspondent] = []
        self._themes: list[Theme] = []
        self._email_map: dict[str, tuple[int, str]] = {}
        self._loaded_at = 0.0
        self._loaded_once = False
        self._normalized_correspondents: list[tuple[str, Correspondent]] = []
        self._normalized_themes: list[tuple[str, Theme]] = []

    # --------------------------------------------------------------- загрузка

    @property
    def enabled(self) -> bool:
        return self.loader is not None

    def ensure_loaded(self, *, force: bool = False) -> None:
        """Загружает справочники, если истёк срок обновления кэша.

        Ошибка первой загрузки пробрасывается: писать в журнал, не прочитав
        справочники, значит нарушить главное требование — брать значения из них.
        Ошибка обновления уже загруженных справочников только логируется:
        работать со слегка устаревшим списком лучше, чем остановить службу.
        """
        if self.loader is None:
            return
        if not force and self._loaded_once:
            ttl = max(0, self.cfg.refresh_minutes) * 60
            if ttl == 0 or (time.monotonic() - self._loaded_at) < ttl:
                return
        try:
            correspondents = self.loader.load_correspondents()
            themes = self.loader.load_themes()
        except Exception as exc:  # noqa: BLE001
            if not self._loaded_once:
                raise
            logger.warning(
                "Не удалось обновить справочники (%s). Работаем со списком,"
                " загруженным ранее: %s корреспондентов, %s тем.",
                exc,
                len(self._correspondents),
                len(self._themes),
            )
            self._loaded_at = time.monotonic()
            return

        self._correspondents = list(correspondents)
        self._themes = list(themes)
        self._normalized_correspondents = [
            (normalize_name(item.name), item) for item in correspondents
        ]
        self._normalized_themes = [(normalize_name(item.name), item) for item in themes]
        self._email_map = load_email_map(self.cfg.email_map_file)
        self._loaded_at = time.monotonic()
        self._loaded_once = True
        logger.info(
            "Справочники загружены: KORRESP — %s, THEMA — %s, адресов — %s",
            len(self._correspondents),
            len(self._themes),
            len(self._email_map),
        )

    @property
    def correspondents(self) -> list[Correspondent]:
        return self._correspondents

    @property
    def themes(self) -> list[Theme]:
        return self._themes

    def theme_names_for_prompt(self) -> list[str]:
        """Названия тем для промпта: модель выбирает из готового списка."""
        limit = self.cfg.themes_in_prompt
        if not self._themes or limit == 0:
            return []
        return [item.name for item in self._themes if item.name][:limit]


    # ------------------------------------------------------------ сопоставление

    def match_correspondent(
        self, names: list[str], email: str | None = None
    ) -> tuple[Correspondent | None, float, str, str]:
        """Ищет корреспондента по адресу и названиям.

        Возвращает (запись, похожесть, источник, пояснение).
        """
        if not self._loaded_once:
            return None, 0.0, "none", ""

        if email and self._email_map:
            mapped = self._email_map.get(email.strip().lower())
            if mapped:
                mapped_id, mapped_name = mapped
                by_id = self._find_by_id(mapped_id) if mapped_id else None
                if by_id is not None:
                    return by_id, 1.0, "email", "закреплённый адрес"
                if mapped_name:
                    found, score = self._best_by_name(mapped_name)
                    if found is not None:
                        return found, score, "email", "адрес из файла"

        candidates = [name for name in names if name and name.strip()]
        best: tuple[Correspondent | None, float, str] = (None, 0.0, "")
        for candidate in candidates:
            found, score = self._best_by_name(candidate)
            if found is not None and score > best[1]:
                best = (found, score, candidate)
        if best[0] is not None and best[1] >= self.cfg.match_threshold:
            return best[0], best[1], "name", best[2]
        return None, best[1], "none", best[2] if best[0] is not None else ""

    def match_theme(self, name: str | None) -> tuple[Theme | None, float, str]:
        """Ищет тему по названию, предложенному моделью."""
        if not self._loaded_once or not name:
            return None, 0.0, "none"
        normalized = normalize_name(name)
        if not normalized:
            return None, 0.0, "none"
        for theme_normalized, theme in self._normalized_themes:
            if theme_normalized == normalized:
                return theme, 1.0, "model"
        found, score = self._best_theme(normalized)
        if found is not None and score >= self.cfg.match_threshold:
            return found, score, "name"
        return None, score, "none"

    # ---------------------------------------------------------------- решение

    def resolve_correspondent(
        self, analysis: Analysis, mail: MailMessage, *, create: bool = False
    ) -> ResolvedCorrespondent:
        """Определяет, что писать в KORRESP и KORRESPID."""
        policy = self.cfg.unknown_correspondent
        if self.loader is None:
            return ResolvedCorrespondent(
                name=self._fallback_name(analysis, mail), id=0, matched=False, source="model"
            )

        # У пересланного письма заголовок From принадлежит пересылающему,
        # а не корреспонденту: в ящике «пересланных» писем это всегда одно и то
        # же лицо. Поэтому для пересылки From не участвует ни в подборе адреса,
        # ни в сравнении названий — организацию берём из бланка письма.
        if mail.forwarded:
            names = [analysis.sender]
            email = None
        else:
            names = [analysis.sender, mail.from_name]
            email = mail.from_address or None
        found, score, source, detail = self.match_correspondent(names, email=email)
        if found is not None:
            return ResolvedCorrespondent(
                name=found.name,
                id=found.id,
                matched=True,
                source=source,
                score=round(score, 3),
                detail=detail,
            )

        fallback = self._fallback_name(analysis, mail)
        if policy == "skip":
            return ResolvedCorrespondent(source="skip", detail=fallback)
        if policy == "create" and create and fallback:
            try:
                created = self.loader.create_correspondent(fallback)
            except Exception as exc:  # noqa: BLE001
                logger.error("Не удалось завести корреспондента «%s»: %s", fallback, exc)
                return ResolvedCorrespondent(
                    name=fallback, id=0, matched=False, source="model", detail="ошибка создания"
                )
            self._add_correspondent(created)
            return ResolvedCorrespondent(
                name=created.name, id=created.id, matched=True, source="created"
            )
        if policy == "none":
            return ResolvedCorrespondent(source="none", detail=fallback)
        return ResolvedCorrespondent(name=fallback, id=0, matched=False, source="model")

    def resolve_theme(self, analysis: Analysis, *, create: bool = False) -> ResolvedTheme:
        """Определяет, что писать в CARD.THEMA (идентификатор справочника)."""
        policy = self.cfg.unknown_thema
        if self.loader is None:
            return ResolvedTheme(source="none")

        found, score, source = self.match_theme(analysis.thema_name)
        if found is not None:
            return ResolvedTheme(
                id=found.id, name=found.name, matched=True, source=source, score=round(score, 3)
            )

        if policy == "skip":
            return ResolvedTheme(source="skip")
        if policy == "create" and create and analysis.thema_name:
            try:
                created = self.loader.create_theme(analysis.thema_name)
            except Exception as exc:  # noqa: BLE001
                logger.error("Не удалось завести тему «%s»: %s", analysis.thema_name, exc)
                return ResolvedTheme(source="none")
            self._add_theme(created)
            return ResolvedTheme(
                id=created.id, name=created.name, matched=True, source="created"
            )
        return ResolvedTheme(source="none")

    # ---------------------------------------------------------------- служебное

    def _fallback_name(self, analysis: Analysis, mail: MailMessage) -> str:
        return (analysis.sender or mail.from_name or mail.from_address or "").strip()

    def _find_by_id(self, correspondent_id: int) -> Correspondent | None:
        for item in self._correspondents:
            if item.id == correspondent_id:
                return item
        return None

    def _best_by_name(self, name: str) -> tuple[Correspondent | None, float]:
        best: Correspondent | None = None
        best_score = 0.0
        for normalized, item in self._normalized_correspondents:
            score = similarity(normalized, name)
            if score > best_score:
                best, best_score = item, score
        return best, best_score

    def _best_theme(self, normalized: str) -> tuple[Theme | None, float]:
        best: Theme | None = None
        best_score = 0.0
        for theme_normalized, item in self._normalized_themes:
            score = similarity(theme_normalized, normalized)
            if score > best_score:
                best, best_score = item, score
        return best, best_score

    def _add_correspondent(self, item: Correspondent) -> None:
        self._correspondents.append(item)
        self._normalized_correspondents.append((normalize_name(item.name), item))

    def _add_theme(self, item: Theme) -> None:
        self._themes.append(item)
        self._normalized_themes.append((normalize_name(item.name), item))
