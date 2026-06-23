"""
Runtime configuration for local scripts.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from setup import BASE_DIR, get_logger


load_dotenv(BASE_DIR / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip() or "gpt-4o-mini-transcribe"
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"
ANALYSIS_MAX_OUTPUT_TOKENS = int(os.getenv("ANALYSIS_MAX_OUTPUT_TOKENS", "3500") or "3500")
ANALYSIS_INPUT_USD_PER_1M = float(os.getenv("ANALYSIS_INPUT_USD_PER_1M", "0") or "0")
ANALYSIS_OUTPUT_USD_PER_1M = float(os.getenv("ANALYSIS_OUTPUT_USD_PER_1M", "0") or "0")
OPENAI_LOG_PREVIEW_LINES = int(os.getenv("OPENAI_LOG_PREVIEW_LINES", "25") or "25")
OPENAI_LOG_PREVIEW_CHARS = int(os.getenv("OPENAI_LOG_PREVIEW_CHARS", "4000") or "4000")

logger = get_logger("transcription")
