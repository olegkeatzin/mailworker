"""Работа с моделью: промпт, запрос, OCR и разбор ответа."""

from .analyze import AnalyzeInput, analyze_email
from .client import AiClient
from .ocr import OCR_PROMPT, ocr_image
from .parse import AnalysisParseError, extract_json, parse_analysis
from .prompt import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_TEMPLATE,
    PromptInput,
    PromptSettings,
    build_prompt,
    load_prompt_settings,
)

__all__ = [
    "AiClient",
    "AnalyzeInput",
    "analyze_email",
    "OCR_PROMPT",
    "ocr_image",
    "AnalysisParseError",
    "extract_json",
    "parse_analysis",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_USER_TEMPLATE",
    "PromptInput",
    "PromptSettings",
    "build_prompt",
    "load_prompt_settings",
]
