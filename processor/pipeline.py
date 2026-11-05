"""Конвейер обработки одного письма.

Порядок этапов повторяет MailSense (services/analysisService.ts):
  1) вложения готовятся: из PDF/DOCX/XLSX вынимается текст, страницы рендерятся;
  2) распознаются картинки и сканы (OCR отдельной моделью, по страницам,
     параллельно);
  3) весь текст уходит основной модели с промптом разбора журнала;
  4) корреспондент и тема ищутся в справочниках KORRESP и THEMA;
  5) запись приводится к полям журнала и пишется в журнал учёта.

Решение «регистрировать или нет» задаётся настройками [processing]. По умолчанию
регистрируются все письма (filter_mode = all): служба рассчитана на ящик, куда
пересылают уже отобранную корреспонденцию. На обычном входящем ящике включается
filter_mode = ai — тогда решение принимает модель (поле register и уверенность).
Ошибка на любом этапе не рвёт службу: письмо помечается как ошибочное и
повторяется при следующем опросе, если включено [processing] retry_errors.

Корреспондент и тема не берутся из ответа модели «как есть»: модель только
подсказывает название организации из бланка и выбирает тему из переданного
списка, а служба находит запись справочника и пишет в CARD её идентификатор.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .ai.analyze import AnalyzeInput, analyze_email
from .ai.client import AiClient
from .ai.ocr import ocr_image
from .ai.parse import parse_analysis
from .ai.prompt import PromptSettings, load_prompt_settings
from .attachments import PrepareLimits, prepare_all
from .classifiers import ClassifierService, ResolvedCorrespondent, ResolvedTheme
from .config import AppConfig
from .errors import AiError, AnalysisParseError, MailParseError, StorageError
from .journal.mapping import build_card, describe_card
from .mail.parser import parse_eml
from .models import (
    Analysis,
    MailMessage,
    PreparedAttachment,
    ProcessResult,
)
from .state import ProcessedState

logger = logging.getLogger(__name__)

STATUS_REGISTERED = "registered"
STATUS_SKIPPED = "skipped"
STATUS_DUPLICATE = "duplicate"
STATUS_ERROR = "error"
STATUS_DRY_RUN = "dry_run"


class EmailProcessor:
    """Обрабатывает письма по одному: разбор, ИИ, запись в журнал."""

    def __init__(
        self,
        cfg: AppConfig,
        client: AiClient,
        journal,
        state: ProcessedState,
        prompt: PromptSettings | None = None,
        classifiers: ClassifierService | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.journal = journal
        self.state = state
        self.prompt = prompt or load_prompt_settings(
            cfg.ai.system_prompt_file, cfg.ai.user_template_file
        )
        # Без источника справочников сервис работает в режиме «модель как есть»:
        # так удобно тестам, но в рабочем запуске источник подставляет cli.
        self.classifiers = classifiers or ClassifierService(cfg.classifiers)

    # ------------------------------------------------------------------ публичное

    def process_raw(
        self, raw: bytes, *, uid: str = "", folder: str = "", dry_run: bool = False
    ) -> ProcessResult:
        """Обрабатывает письмо в виде байтов .eml (из ящика или файла)."""
        try:
            mail = parse_eml(raw, uid=uid, folder=folder)
        except Exception as exc:  # noqa: BLE001 - кривое письмо не должно рвать проход
            logger.exception("Не удалось разобрать письмо")
            return ProcessResult(
                status=STATUS_ERROR,
                message_id="",
                error="Не удалось разобрать письмо: " + str(exc),
            )
        return self.process_mail(mail, dry_run=dry_run)

    def process_mail(self, mail: MailMessage, *, dry_run: bool = False) -> ProcessResult:
        started = time.monotonic()
        key = self.dedup_key(mail)
        logger.info(
            "Письмо %s «%s» от %s",
            mail.message_id,
            mail.subject,
            mail.from_display or "неизвестного отправителя",
        )

        if not dry_run and self.state.should_skip(
            key, retry_errors=self.cfg.processing.retry_errors
        ):
            record = self.state.get(key) or {}
            logger.info(
                "Письмо уже обработано (%s), пропущено. Карточка: %s",
                record.get("status"),
                record.get("card_id") or "—",
            )
            return ProcessResult(
                status=STATUS_DUPLICATE,
                message_id=mail.message_id,
                reason="уже обработано ранее: " + str(record.get("status")),
                seconds=time.monotonic() - started,
            )

        try:
            return self._process(mail, key=key, dry_run=dry_run, started=started)
        except (AiError, AnalysisParseError, StorageError, MailParseError) as exc:
            logger.error("Письмо %s не обработано: %s", mail.message_id, exc)
            self._mark_error(key, mail, str(exc), dry_run)
            return ProcessResult(
                status=STATUS_ERROR,
                message_id=mail.message_id,
                error=str(exc),
                seconds=time.monotonic() - started,
            )
        except Exception as exc:  # noqa: BLE001 - служба обязана продолжать работу
            logger.exception("Неожиданная ошибка при обработке письма %s", mail.message_id)
            self._mark_error(key, mail, "неожиданная ошибка: " + str(exc), dry_run)
            return ProcessResult(
                status=STATUS_ERROR,
                message_id=mail.message_id,
                error="Неожиданная ошибка: " + str(exc),
                seconds=time.monotonic() - started,
            )

    def dedup_key(self, mail: MailMessage) -> str:
        """Ключ дедупликации: протокол, ящик, папка, Message-ID."""
        return "|".join(
            [
                self.cfg.mail.protocol,
                self.cfg.mail.username.lower(),
                mail.folder or self.cfg.mail.mailbox,
                mail.message_id,
            ]
        )

    # --------------------------------------------------------------- внутреннее

    def _process(
        self, mail: MailMessage, *, key: str, dry_run: bool, started: float
    ) -> ProcessResult:
        # Справочники нужны до обращения к модели: если база недоступна, лучше
        # отметить письмо ошибкой и повторить позже, чем записать «мимо справочника».
        self.classifiers.ensure_loaded()

        prepared = prepare_all(
            mail.attachments,
            PrepareLimits(
                ocr_max_pages=self.cfg.ai.ocr_max_pages,
                text_max_pages=self.cfg.ai.text_max_pages,
                max_text_chars=self.cfg.ai.max_text_chars,
                multimodal=self.cfg.ai.multimodal and self.cfg.ai.model != "",
            ),
            skip_inline_images=self.cfg.processing.skip_inline_images,
            min_inline_image_bytes=self.cfg.processing.min_inline_image_bytes,
        )
        logger.debug("Подготовлено вложений: %s", len(prepared))

        ocr_texts = self._ocr_stage(mail, prepared)

        input_data = self._build_input(mail, prepared, ocr_texts)
        raw_response = analyze_email(self.client, self.cfg.ai, input_data, self.prompt)
        analysis = parse_analysis(raw_response, model_used=self.cfg.ai.model)
        fields = (
            "номер=%s, дата=%s, листов=%s, отправитель=%s, тема=%s",
            analysis.doc_number or "—",
            analysis.doc_date.isoformat() if analysis.doc_date else "—",
            "—" if analysis.sheets_count is None else analysis.sheets_count,
            analysis.sender or "—",
            analysis.thema_name or "—",
        )
        if analysis.register is None and analysis.confidence is None:
            # В режимах all/with_attachments решение модели не спрашивается вовсе.
            logger.info(
                "Разбор ИИ (решение модели не спрашивается, filter_mode = %s): "
                + fields[0],
                self.cfg.processing.filter_mode,
                *fields[1:],
            )
        else:
            logger.info(
                "Разбор ИИ: регистрировать=%s (уверенность %s), " + fields[0],
                _yes_no(analysis.register),
                "—" if analysis.confidence is None else analysis.confidence,
                *fields[1:],
            )

        register, reason = self._decide(analysis, mail, prepared)
        if not register:
            return self._skip(mail, key, analysis, reason, dry_run, started)

        # Сопоставление со справочниками. Заводить новые записи в справочнике
        # можно только при реальной записи: пробный прогон ничего не меняет.
        correspondent = self.classifiers.resolve_correspondent(
            analysis, mail, create=not dry_run
        )
        theme = self.classifiers.resolve_theme(analysis, create=not dry_run)
        self._log_classifiers(correspondent, theme)

        if correspondent.rejected:
            return self._skip(
                mail,
                key,
                analysis,
                "корреспондент «"
                + (correspondent.detail or analysis.sender or "не определён")
                + "» не найден в справочнике KORRESP"
                " (classifiers.unknown_correspondent = skip)",
                dry_run,
                started,
            )
        if theme.rejected:
            return self._skip(
                mail,
                key,
                analysis,
                "тема «"
                + (analysis.thema_name or "не определена")
                + "» не найдена в справочнике THEMA"
                " (classifiers.unknown_thema = skip)",
                dry_run,
                started,
            )

        card = build_card(
            self.cfg.journal,
            analysis,
            mail,
            prepared,
            correspondent=correspondent,
            theme=theme,
        )
        if dry_run:
            logger.info("Пробный прогон, запись не делается: %s", describe_card(card))
            return ProcessResult(
                status=STATUS_DRY_RUN,
                message_id=mail.message_id,
                card=card,
                analysis=analysis,
                reason=reason,
                seconds=time.monotonic() - started,
            )

        inserted = self.journal.insert_card(card, analysis)
        self.state.mark_registered(
            key,
            message_id=mail.message_id,
            uid=mail.uid,
            card_id=inserted.card_id,
            reg_number=inserted.reg_number,
        )
        self._archive(mail)
        logger.info(
            "Записано в журнал (id=%s): %s", inserted.card_id, describe_card(card)
        )
        return ProcessResult(
            status=STATUS_REGISTERED,
            message_id=mail.message_id,
            card=card,
            analysis=analysis,
            inserted=inserted,
            reason=reason,
            seconds=time.monotonic() - started,
        )

    def _skip(
        self,
        mail: MailMessage,
        key: str,
        analysis: Analysis,
        reason: str,
        dry_run: bool,
        started: float,
    ) -> ProcessResult:
        """Письмо осознанно не регистрируется — с причиной в журнале работы."""
        logger.info("Письмо %s пропущено: %s", mail.message_id, reason)
        if not dry_run:
            self.state.mark_skipped(
                key, message_id=mail.message_id, uid=mail.uid, reason=reason
            )
        return ProcessResult(
            status=STATUS_SKIPPED,
            message_id=mail.message_id,
            analysis=analysis,
            reason=reason,
            seconds=time.monotonic() - started,
        )

    def _log_classifiers(
        self, correspondent: ResolvedCorrespondent, theme: ResolvedTheme
    ) -> None:
        """Пишет в лог, что нашлось в справочниках: это главный след проверки."""
        if correspondent.matched:
            logger.info(
                "Корреспондент: «%s» → KORRESP ID=%s (%s%s)",
                correspondent.name,
                correspondent.id,
                correspondent.source,
                ", похожесть " + str(correspondent.score) if correspondent.score else "",
            )
        else:
            logger.info(
                "Корреспондент: в справочнике не найден, в журнал пойдёт «%s»",
                correspondent.name or "пусто",
            )
        if theme.matched:
            logger.info(
                "Тема: «%s» → THEMA ID=%s (%s%s)",
                theme.name,
                theme.id,
                theme.source,
                ", похожесть " + str(theme.score) if theme.score else "",
            )
        elif theme.name:
            logger.info("Тема: «%s» → THEMA ID=%s", theme.name, theme.id or "не определена")

    def _build_input(
        self,
        mail: MailMessage,
        prepared: list[PreparedAttachment],
        ocr_texts: list[str],
    ) -> AnalyzeInput:
        """Собирает текст вложений и пометки для промпта (как в MailSense)."""
        texts: list[str] = []
        names: list[str] = []
        notes: list[str] = []
        images: list[str] = []

        for index, attachment in enumerate(prepared):
            parts: list[str] = []
            if attachment.extracted_text:
                parts.append(attachment.extracted_text)
            if index < len(ocr_texts) and ocr_texts[index]:
                parts.append(ocr_texts[index])
            texts.append("\n\n".join(parts))
            names.append(attachment.filename)

            if attachment.note or not attachment.handled:
                notes.append(
                    attachment.filename + ": " + (attachment.note or "не обработано")
                )
            if self.cfg.ai.images_in_analysis:
                images.extend(attachment.images)

        return AnalyzeInput(
            subject=mail.subject,
            from_display=mail.from_display,
            date=mail.date_text,
            body=mail.body_text,
            attachment_texts=texts,
            attachment_names=names,
            attachment_notes=notes,
            images=images,
            forwarded=mail.forwarded,
            themes=self.classifiers.theme_names_for_prompt(),
            # Поля «регистрировать» и «уверенность» просим только там, где они
            # влияют на решение. В режимах all и with_attachments они не нужны —
            # модель их не считает, ответ короче и устойчивее.
            need_decision=self.cfg.processing.filter_mode == "ai",
            need_category=self.cfg.processing.skip_spam,
        )

    def _ocr_stage(self, mail: MailMessage, prepared: list[PreparedAttachment]) -> list[str]:
        """Распознаёт все картинки письма; сохраняет маркеры страниц."""
        jobs: list[tuple[int, int, str]] = []
        for index, attachment in enumerate(prepared):
            for page, image in enumerate(attachment.images):
                jobs.append((index, page, image))
        if not jobs:
            return ["" for _ in prepared]

        logger.info(
            "Распознавание изображений: %s страниц(ы) в %s вложениях",
            len(jobs),
            sum(1 for item in prepared if item.images),
        )
        results: dict[tuple[int, int], str] = {}
        workers = max(1, min(self.cfg.ocr.concurrency, len(jobs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._ocr_one,
                    prepared[index].filename,
                    image,
                    page,
                ): (index, page)
                for index, page, image in jobs
            }
            for future in as_completed(futures):
                index, page = futures[future]
                try:
                    results[(index, page)] = future.result()
                except AiError as exc:
                    logger.warning(
                        "Страница %s вложения «%s» не распознана: %s",
                        page + 1,
                        prepared[index].filename,
                        exc,
                    )
                    note = "OCR страницы " + str(page + 1) + " не удался"
                    prepared[index].note = (
                        prepared[index].note + "; " + note
                        if prepared[index].note
                        else note
                    )

        texts: list[str] = []
        for index, attachment in enumerate(prepared):
            pages: list[str] = []
            for page in range(len(attachment.images)):
                text = (results.get((index, page)) or "").strip()
                if text:
                    pages.append("--- стр. " + str(page + 1) + " ---\n" + text)
            texts.append("\n\n".join(pages))
        return texts

    def _ocr_one(self, filename: str, image: str, page: int) -> str:
        logger.debug("OCR: «%s», страница %s", filename, page + 1)
        return ocr_image(self.client, self.cfg.ocr, image)

    def _decide(
        self, analysis: Analysis, mail: MailMessage, prepared: list[PreparedAttachment]
    ) -> tuple[bool, str]:
        """Решает, писать ли письмо в журнал. Все правила — из [processing].

        Режимы:
          all              — регистрируются все письма (ящик с отобранной почтой);
          ai               — решает модель: register, затем порог уверенности;
          with_attachments — только письма с вложениями.

        Нежелательные письма (спам) отсекаются отдельно, флагом skip_spam,
        независимо от режима.
        """
        cfg = self.cfg.processing

        if cfg.skip_spam and analysis.category == "spam":
            return False, "модель отнесла письмо к спаму (skip_spam = true)"

        if cfg.filter_mode == "with_attachments" and not mail.attachments:
            return False, "у письма нет вложений (filter_mode = with_attachments)"

        if cfg.filter_mode == "all":
            prepared_count = sum(1 for item in prepared if item.handled)
            return True, (
                "регистрируются все письма (filter_mode = all), вложений обработано: "
                + str(prepared_count)
            )

        # filter_mode = ai: решает модель.
        if cfg.require_register_flag:
            if analysis.register is None:
                return False, "модель не указала, подлежит ли письмо регистрации"
            if analysis.register is False:
                reason = analysis.register_reason or "письмо не является входящим документом"
                return False, "модель считает, что регистрировать не нужно: " + reason

        if (
            analysis.confidence is not None
            and cfg.min_confidence > 0
            and analysis.confidence < cfg.min_confidence
        ):
            return False, (
                "уверенность модели "
                + str(analysis.confidence)
                + " ниже порога "
                + str(cfg.min_confidence)
            )

        prepared_count = sum(1 for item in prepared if item.handled)
        return True, "к регистрации, вложений обработано: " + str(prepared_count)

    def _mark_error(
        self, key: str, mail: MailMessage, error: str, dry_run: bool
    ) -> None:
        if dry_run:
            return
        try:
            self.state.mark_error(key, message_id=mail.message_id, uid=mail.uid, error=error)
        except Exception:  # noqa: BLE001 - журнал обработки не должен ронять службу
            logger.exception("Не удалось записать ошибку в журнал обработки")

    def _archive(self, mail: MailMessage) -> None:
        """Сохраняет исходное письмо: журнал учёта без исходника спорить не с чем."""
        if not self.cfg.processing.save_eml:
            return
        try:
            folder = self.cfg.processing.archive_dir / dt.date.today().isoformat()
            folder.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^\w.@-]+", "_", mail.message_id)[:80] or mail.uid or "mail"
            path = folder / (name + ".eml")
            path.write_bytes(mail.raw)
            logger.debug("Исходное письмо сохранено: %s", path)
        except OSError as exc:
            # Нет места на диске или прав — запись в журнал уже сделана, это важнее.
            logger.warning("Не удалось сохранить исходное письмо: %s", exc)


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "не указано"
    return "да" if value else "нет"
