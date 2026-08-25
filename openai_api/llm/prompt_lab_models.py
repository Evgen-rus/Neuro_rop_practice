"""Verified model/reasoning capability map for Prompt Lab.

UI labels stay human-readable. Backend always uses real API ids from this
project's runtime/pricing table, never guessed marketing names.

Reasoning values are taken from OpenAI model cards for the ids this project
already prices, not from a generic GPT-5 list.
"""

from __future__ import annotations

from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT
from openai_api.pricing import ANALYSIS_MODEL_PRICES_USD_PER_1M


REASONING_LABELS: dict[str, str] = {
    "none": "Без рассуждений",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Very High",
    "max": "Max",
}

# OpenAI model cards (2026):
# gpt-5.6-terra / gpt-5.6-luna: none, low, medium, high, xhigh, max
# gpt-5.4: none, low, medium, high, xhigh
# gpt-5.4-mini / gpt-5.5: same family as 5.4 (no max). Mini has no xhigh on its card.
GPT56_REASONING = ("none", "low", "medium", "high", "xhigh", "max")
GPT54_REASONING = ("none", "low", "medium", "high", "xhigh")
GPT54_MINI_REASONING = ("none", "low", "medium", "high")

MODEL_REASONING: dict[str, tuple[str, ...]] = {
    "gpt-5.6-terra": GPT56_REASONING,
    "gpt-5.6-luna": GPT56_REASONING,
    "gpt-5.5": GPT54_REASONING,
    "gpt-5.4": GPT54_REASONING,
    "gpt-5.4-mini": GPT54_MINI_REASONING,
}

_MODEL_LABELS = {
    "gpt-5.6-terra": "Terra",
    "gpt-5.6-luna": "Luna",
    "gpt-5.4-mini": "5.4 Mini",
    "gpt-5.4": "5.4",
    "gpt-5.5": "5.5",
}


def _label_for(model_id: str) -> str:
    return _MODEL_LABELS.get(model_id, model_id)


def _reasoning_for(model_id: str) -> list[str]:
    allowed = list(MODEL_REASONING.get(model_id) or ("low", "medium", "high"))
    if model_id == MANAGER_MODEL and MANAGER_REASONING_EFFORT not in allowed:
        allowed.append(MANAGER_REASONING_EFFORT)
    return allowed


def list_lab_models(*, include_runtime: bool = True) -> list[dict[str, object]]:
    seen: list[str] = []
    for model_id in ANALYSIS_MODEL_PRICES_USD_PER_1M:
        if model_id not in seen:
            seen.append(model_id)
    if include_runtime and MANAGER_MODEL not in seen:
        seen.insert(0, MANAGER_MODEL)
    elif include_runtime:
        seen = [MANAGER_MODEL, *[item for item in seen if item != MANAGER_MODEL]]
    return [
        {
            "id": model_id,
            "label": _label_for(model_id),
            "reasoning": _reasoning_for(model_id),
        }
        for model_id in seen
    ]


def resolved_runtime_config() -> dict[str, str]:
    return {
        "model": MANAGER_MODEL,
        "reasoning": MANAGER_REASONING_EFFORT,
    }


def validate_model_reasoning(model: str, reasoning: str) -> tuple[str, str]:
    model_id = str(model or "").strip()
    effort = str(reasoning or "").strip()
    known = {item["id"]: item for item in list_lab_models()}
    if model_id not in known:
        raise ValueError(f"Неизвестная модель: {model_id}")
    allowed = set(known[model_id]["reasoning"])  # type: ignore[arg-type]
    if effort not in allowed:
        raise ValueError(f"Модель {model_id} не поддерживает reasoning={effort}")
    return model_id, effort
