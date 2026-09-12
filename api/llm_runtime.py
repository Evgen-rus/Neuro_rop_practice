"""Admin-safe projection of the configured text LLM runtime."""

from __future__ import annotations

import json
import re
from collections import deque
from typing import Any

from openai_api import config as llm_config
from openai_api.llm.usage_trace import usage_trace_path


def _latest_usage_event() -> dict[str, Any] | None:
    path = usage_trace_path()
    try:
        with path.open("r", encoding="utf-8") as source:
            lines = deque(source, maxlen=50)
    except OSError:
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict) and event.get("provider") in {"openai", "openrouter"}:
            return event
    return None


def _safe_fallback_reason(value: Any) -> str | None:
    reason = str(value or "").strip()
    return reason if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*(?:: status=\d{3})?", reason) else None


def _safe_token(value: Any) -> str | None:
    token = str(value or "").strip()
    return token if re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", token) else None


def _error_label(event: dict[str, Any]) -> str | None:
    error_type = _safe_token(event.get("error_type"))
    status_code = event.get("error_status_code")
    if error_type == "PermissionDeniedError" or status_code == 403:
        return "Провайдер отклонил запрос: нет доступа или запрос запрещён политикой"
    if error_type == "AuthenticationError" or status_code == 401:
        return "Провайдер отклонил ключ API"
    if error_type == "RateLimitError" or status_code == 429:
        return "Закончилась квота API" if event.get("error_code") == "insufficient_quota" else "Превышен лимит запросов API"
    if error_type in {"APIConnectionError", "APITimeoutError"}:
        return "Нет соединения с провайдером"
    return f"Ошибка LLM: {error_type}" if error_type else None


def build_llm_runtime_status() -> dict[str, Any]:
    provider = llm_config.LLM_PROVIDER
    fallback_enabled = llm_config.LLM_FALLBACK_TO_OPENAI
    credential_configured = bool(
        llm_config.OPENROUTER_API_KEY if provider == "openrouter" else llm_config.OPENAI_API_KEY
    )
    if credential_configured:
        status = "configured"
        status_label = "Настроен; работа подтверждается фактическими вызовами"
    elif provider == "openrouter" and fallback_enabled and llm_config.OPENAI_API_KEY:
        status = "degraded"
        status_label = "OpenRouter без ключа — production-вызовы перейдут в OpenAI"
    else:
        status = "blocked"
        status_label = f"Ключ {provider} не настроен"

    last = _latest_usage_event()
    return {
        "provider": provider,
        "status": status,
        "status_label": status_label,
        "credential_configured": credential_configured,
        "fallback_enabled": fallback_enabled,
        "roles": {
            "analysis": {"model": llm_config.ANALYSIS_MODEL, "reasoning": llm_config.ANALYSIS_REASONING_EFFORT},
            "repair": {"model": llm_config.ANALYSIS_REPAIR_MODEL, "reasoning": llm_config.ANALYSIS_REPAIR_REASONING_EFFORT},
            "manager": {"model": llm_config.MANAGER_MODEL, "reasoning": llm_config.MANAGER_REASONING_EFFORT},
            "learning_shadow": {
                "model": llm_config.LEARNING_SHADOW_MODEL,
                "reasoning": llm_config.LEARNING_SHADOW_REASONING_EFFORT,
            },
        },
        "transcription": {"provider": "openai", "model": llm_config.TRANSCRIPTION_MODEL},
        "last_request": None if last is None else {
            "requested_at": last.get("requested_at"),
            "provider": last.get("provider"),
            "model": last.get("model"),
            "status": last.get("status"),
            "error_type": _safe_token(last.get("error_type")),
            "error_status_code": (
                last.get("error_status_code")
                if isinstance(last.get("error_status_code"), int) and 100 <= last["error_status_code"] <= 599
                else None
            ),
            "error_code": _safe_token(last.get("error_code")),
            "request_id": _safe_token(last.get("request_id")),
            "error_label": _error_label(last),
            "provider_fallback": bool(last.get("provider_fallback")),
            "provider_fallback_reason": _safe_fallback_reason(last.get("provider_fallback_reason")),
        },
    }
