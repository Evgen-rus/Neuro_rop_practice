"""
Runtime configuration for local scripts.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from setup import BASE_DIR, get_logger


load_dotenv(BASE_DIR / ".env")


def read_bool_env(name: str, default: bool) -> bool:
    """Read an explicit boolean environment flag without surprising truthiness."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

BITRIX_PORTAL_URL = os.getenv("BITRIX_PORTAL_URL", "").strip().rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip() or "gpt-4o-mini-transcribe"
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"
ANALYSIS_MAX_OUTPUT_TOKENS = int(os.getenv("ANALYSIS_MAX_OUTPUT_TOKENS", "3500") or "3500")
USD_RUB_RATE = float(os.getenv("USD_RUB_RATE", "75") or "75")
OPENAI_LOG_PREVIEW_LINES = int(os.getenv("OPENAI_LOG_PREVIEW_LINES", "25") or "25")
OPENAI_LOG_PREVIEW_CHARS = int(os.getenv("OPENAI_LOG_PREVIEW_CHARS", "4000") or "4000")

# Preparation-only flags. They are not consumed by the legacy analysis path yet.
# Defaults deliberately preserve the exact current production behaviour.
CONTEXT_MEMORY_OPTIMIZATION_ENABLED = read_bool_env("CONTEXT_MEMORY_OPTIMIZATION_ENABLED", False)
CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE = read_bool_env("CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE", False)
CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK = read_bool_env(
    "CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK",
    True,
)

logger = get_logger("transcription")
