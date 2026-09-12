"""
Runtime configuration for local scripts.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from setup import BASE_DIR, get_logger


load_dotenv(BASE_DIR / ".env")
logger = get_logger("openai")


def read_bool_env(name: str, default: bool) -> bool:
    """Read an explicit boolean environment flag without surprising truthiness."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_openai_profile_env(name: str, legacy_name: str, default: str) -> str:
    value = os.getenv(name)
    legacy_value = os.getenv(legacy_name)
    if (value is None or not value.strip()) and legacy_value and legacy_value.strip():
        raise RuntimeError(f"Rename {legacy_name} to {name} in the environment")
    return value.strip() if value and value.strip() else default


def _read_csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    items = tuple(dict.fromkeys(item.strip() for item in (value or "").split(",") if item.strip()))
    return items or default


LLM_PROVIDER = (os.getenv("LLM_PROVIDER", "openai").strip().lower() or "openai")
if LLM_PROVIDER not in {"openai", "openrouter"}:
    raise RuntimeError("LLM_PROVIDER must be one of: openai, openrouter")
LLM_FALLBACK_TO_OPENAI = read_bool_env("LLM_FALLBACK_TO_OPENAI", True)

BITRIX_PORTAL_URL = os.getenv("BITRIX_PORTAL_URL", "").strip().rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_REQUEST_TIMEOUT_SECONDS = max(
    30.0,
    float(os.getenv("OPENAI_REQUEST_TIMEOUT_SECONDS", "600") or "600"),
)
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip() or "gpt-4o-mini-transcribe"
OPENAI_ANALYSIS_MODEL = _read_openai_profile_env("OPENAI_ANALYSIS_MODEL", "ANALYSIS_MODEL", "gpt-5.6-terra")
OPENAI_ANALYSIS_REASONING_EFFORT = _read_openai_profile_env(
    "OPENAI_ANALYSIS_REASONING_EFFORT", "ANALYSIS_REASONING_EFFORT", "low",
)
OPENAI_REPAIR_MODEL = _read_openai_profile_env("OPENAI_REPAIR_MODEL", "ANALYSIS_REPAIR_MODEL", "gpt-5.6-luna")
OPENAI_REPAIR_REASONING_EFFORT = _read_openai_profile_env(
    "OPENAI_REPAIR_REASONING_EFFORT", "ANALYSIS_REPAIR_REASONING_EFFORT", "xhigh",
)
OPENAI_MANAGER_MODEL = _read_openai_profile_env("OPENAI_MANAGER_MODEL", "DEAL_MANAGER_MODEL", OPENAI_ANALYSIS_MODEL)
OPENAI_MANAGER_REASONING_EFFORT = _read_openai_profile_env(
    "OPENAI_MANAGER_REASONING_EFFORT", "DEAL_MANAGER_REASONING_EFFORT", OPENAI_ANALYSIS_REASONING_EFFORT,
)
OPENAI_LEARNING_SHADOW_MODEL = os.getenv("OPENAI_LEARNING_SHADOW_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
OPENAI_LEARNING_SHADOW_REASONING_EFFORT = (
    os.getenv("OPENAI_LEARNING_SHADOW_REASONING_EFFORT", "xhigh").strip() or "xhigh"
)
OPENAI_PROMPT_LAB_MODELS = _read_csv_env(
    "OPENAI_PROMPT_LAB_MODELS",
    ("gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"),
)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = (
    os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")
    or "https://openrouter.ai/api/v1"
)
OPENROUTER_ANALYSIS_MODEL = os.getenv("OPENROUTER_ANALYSIS_MODEL", "").strip()
OPENROUTER_ANALYSIS_REASONING_EFFORT = (
    os.getenv("OPENROUTER_ANALYSIS_REASONING_EFFORT", OPENAI_ANALYSIS_REASONING_EFFORT).strip()
    or OPENAI_ANALYSIS_REASONING_EFFORT
)
OPENROUTER_REPAIR_MODEL = os.getenv("OPENROUTER_REPAIR_MODEL", "").strip()
OPENROUTER_REPAIR_REASONING_EFFORT = (
    os.getenv("OPENROUTER_REPAIR_REASONING_EFFORT", OPENAI_REPAIR_REASONING_EFFORT).strip()
    or OPENAI_REPAIR_REASONING_EFFORT
)
OPENROUTER_MANAGER_MODEL = os.getenv("OPENROUTER_MANAGER_MODEL", "").strip()
OPENROUTER_MANAGER_REASONING_EFFORT = (
    os.getenv("OPENROUTER_MANAGER_REASONING_EFFORT", OPENAI_MANAGER_REASONING_EFFORT).strip()
    or OPENAI_MANAGER_REASONING_EFFORT
)
OPENROUTER_LEARNING_SHADOW_MODEL = os.getenv("OPENROUTER_LEARNING_SHADOW_MODEL", "").strip()
OPENROUTER_LEARNING_SHADOW_REASONING_EFFORT = (
    os.getenv("OPENROUTER_LEARNING_SHADOW_REASONING_EFFORT", OPENAI_LEARNING_SHADOW_REASONING_EFFORT).strip()
    or OPENAI_LEARNING_SHADOW_REASONING_EFFORT
)
OPENROUTER_PROMPT_LAB_MODELS = _read_csv_env("OPENROUTER_PROMPT_LAB_MODELS", ())

if LLM_PROVIDER == "openrouter":
    _missing_openrouter_models = tuple(
        name
        for name, value in (
            ("OPENROUTER_ANALYSIS_MODEL", OPENROUTER_ANALYSIS_MODEL),
            ("OPENROUTER_REPAIR_MODEL", OPENROUTER_REPAIR_MODEL),
            ("OPENROUTER_MANAGER_MODEL", OPENROUTER_MANAGER_MODEL),
            ("OPENROUTER_LEARNING_SHADOW_MODEL", OPENROUTER_LEARNING_SHADOW_MODEL),
        )
        if not value
    )
    if _missing_openrouter_models:
        raise RuntimeError(
            "LLM_PROVIDER=openrouter requires non-empty role models: "
            + ", ".join(_missing_openrouter_models)
        )

# Active transport/profile aliases keep callers provider-agnostic.
ACTIVE_PROVIDER = LLM_PROVIDER
ACTIVE_API_KEY = OPENROUTER_API_KEY if LLM_PROVIDER == "openrouter" else OPENAI_API_KEY
ACTIVE_BASE_URL = OPENROUTER_BASE_URL if LLM_PROVIDER == "openrouter" else None
ANALYSIS_MODEL = OPENROUTER_ANALYSIS_MODEL if LLM_PROVIDER == "openrouter" else OPENAI_ANALYSIS_MODEL
ANALYSIS_REASONING_EFFORT = (
    OPENROUTER_ANALYSIS_REASONING_EFFORT
    if LLM_PROVIDER == "openrouter"
    else OPENAI_ANALYSIS_REASONING_EFFORT
)
ANALYSIS_REPAIR_MODEL = OPENROUTER_REPAIR_MODEL if LLM_PROVIDER == "openrouter" else OPENAI_REPAIR_MODEL
ANALYSIS_REPAIR_REASONING_EFFORT = (
    OPENROUTER_REPAIR_REASONING_EFFORT
    if LLM_PROVIDER == "openrouter"
    else OPENAI_REPAIR_REASONING_EFFORT
)
MANAGER_MODEL = OPENROUTER_MANAGER_MODEL if LLM_PROVIDER == "openrouter" else OPENAI_MANAGER_MODEL
MANAGER_REASONING_EFFORT = (
    OPENROUTER_MANAGER_REASONING_EFFORT
    if LLM_PROVIDER == "openrouter"
    else OPENAI_MANAGER_REASONING_EFFORT
)
LEARNING_SHADOW_MODEL = (
    OPENROUTER_LEARNING_SHADOW_MODEL
    if LLM_PROVIDER == "openrouter"
    else OPENAI_LEARNING_SHADOW_MODEL
)
LEARNING_SHADOW_REASONING_EFFORT = (
    OPENROUTER_LEARNING_SHADOW_REASONING_EFFORT
    if LLM_PROVIDER == "openrouter"
    else OPENAI_LEARNING_SHADOW_REASONING_EFFORT
)
ANALYSIS_REPAIR_MAX_OUTPUT_TOKENS = int(os.getenv("ANALYSIS_REPAIR_MAX_OUTPUT_TOKENS", "8000") or "8000")
ANALYSIS_MAX_OUTPUT_TOKENS = int(os.getenv("ANALYSIS_MAX_OUTPUT_TOKENS", "3500") or "3500")
# Quick Help / «Дожим» JSON plus reasoning tokens. 4000 was too tight with
# high reasoning: the model billed a full window and the answer never saved.
QUICK_HELP_MAX_OUTPUT_TOKENS = int(os.getenv("QUICK_HELP_MAX_OUTPUT_TOKENS", "4000") or "4000")
# Follow-up ideas JSON plus reasoning tokens. Default stays the previous
# hardcoded 3600; raise via env if high reasoning truncates the answer.
FOLLOWUPS_MAX_OUTPUT_TOKENS = int(os.getenv("FOLLOWUPS_MAX_OUTPUT_TOKENS", "3600") or "3600")
# Short post-call client message. Keep below follow-ups: the answer is a few lines.
COMPANION_MAX_OUTPUT_TOKENS = int(os.getenv("COMPANION_MAX_OUTPUT_TOKENS", "1800") or "1800")
MANAGER_SITUATION_MAX_OUTPUT_TOKENS = int(os.getenv("MANAGER_SITUATION_MAX_OUTPUT_TOKENS", "2400") or "2400")
FULL_SCRIPT_MAX_OUTPUT_TOKENS = int(os.getenv("FULL_SCRIPT_MAX_OUTPUT_TOKENS", "6000") or "6000")
EMAIL_MAX_OUTPUT_TOKENS = int(os.getenv("EMAIL_MAX_OUTPUT_TOKENS", "2600") or "2600")
STRATEGY_PACK_MAX_OUTPUT_TOKENS = int(os.getenv("STRATEGY_PACK_MAX_OUTPUT_TOKENS", "9000") or "9000")
TASK_GUIDANCE_MAX_OUTPUT_TOKENS = int(os.getenv("TASK_GUIDANCE_MAX_OUTPUT_TOKENS", "5000") or "5000")
LEARNING_SHADOW_MAX_OUTPUT_TOKENS = int(os.getenv("LEARNING_SHADOW_MAX_OUTPUT_TOKENS", "12000") or "12000")
COMMUNICATION_QUALITY_AUDIT_ENABLED = read_bool_env("COMMUNICATION_QUALITY_AUDIT_ENABLED", True)
DEAL_INCREMENTAL_ANALYSIS_ENABLED = read_bool_env("DEAL_INCREMENTAL_ANALYSIS_ENABLED", False)
USD_RUB_RATE = float(os.getenv("USD_RUB_RATE", "75") or "75")
OPENAI_LOG_PREVIEW_LINES = int(os.getenv("OPENAI_LOG_PREVIEW_LINES", "25") or "25")
OPENAI_LOG_PREVIEW_CHARS = int(os.getenv("OPENAI_LOG_PREVIEW_CHARS", "4000") or "4000")
