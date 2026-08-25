"""Central model/reasoning capability map for Prompt Lab.

UI labels stay human-readable. Backend always uses real API ids from this
project's runtime/pricing table, never guessed marketing names.
"""

from __future__ import annotations

from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT
from openai_api.pricing import ANALYSIS_MODEL_PRICES_USD_PER_1M


REASONING_OPTIONS: tuple[tuple[str, str], ...] = (
    ("none", "Без рассуждений"),
    ("minimal", "Minimal"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "Very High"),
)
ALLOWED_REASONING = {item[0] for item in REASONING_OPTIONS}

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
    # GPT-5.x in this project already forwards none/low/medium/high/xhigh.
    # Mini keeps the same set so CURRENT runtime values stay selectable.
    del model_id
    return [item[0] for item in REASONING_OPTIONS]


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
