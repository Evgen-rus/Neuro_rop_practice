"""Strict structured output for the manager's independent quick-help coach."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.deal_manager_situation import (
    MANAGER_MODEL,
    MANAGER_REASONING_EFFORT,
    MAX_MANAGER_CONTEXT_CHARS,
    project_bitrix_task,
    project_deal,
)
from openai_api.llm.llm_client import call_structured_output_json


MAX_QUICK_HELP_OUTPUT_TOKENS = 3000
_LIST_FIELDS = ("action_steps", "facts_to_clarify", "crm_checklist")
_REQUIRED_FIELDS = (
    "problem_summary",
    "diagnosis",
    "recommended_action",
    "action_steps",
    "client_message",
    "call_script",
    "facts_to_clarify",
    "crm_checklist",
)


def _short_list_schema(max_items: int = 4, max_length: int = 800) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": max_length},
        "maxItems": max_items,
    }


def quick_help_schema() -> dict[str, Any]:
    fields = {
        "problem_summary": {"type": "string", "minLength": 1, "maxLength": 1600},
        # This is the visible "Что сейчас мешает" block in the manager UI.
        "diagnosis": {"type": "string", "minLength": 1, "maxLength": 2000},
        "recommended_action": {"type": "string", "minLength": 1, "maxLength": 1600},
        "action_steps": _short_list_schema(max_items=4),
        "client_message": {"type": "string", "minLength": 1, "maxLength": 1600},
        "call_script": {"type": "string", "minLength": 1, "maxLength": 2000},
        "facts_to_clarify": _short_list_schema(max_items=4),
        "crm_checklist": _short_list_schema(max_items=4),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(fields),
        "properties": fields,
    }


def validate_quick_help(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Quick help должен быть JSON-объектом")
    if any(field not in value for field in _REQUIRED_FIELDS):
        raise ValueError("В quick help отсутствует обязательное поле")
    normalized: dict[str, Any] = {}
    for field in ("problem_summary", "diagnosis", "recommended_action", "client_message", "call_script"):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError("В quick help есть пустое текстовое поле")
        normalized[field] = item.strip()[:2000]
    for field in _LIST_FIELDS:
        items = value.get(field)
        if not isinstance(items, list) or len(items) > 4:
            raise ValueError(f"{field} должен содержать не более 4 пунктов")
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"{field} должен содержать только непустые строки")
        normalized[field] = [item.strip()[:800] for item in items]
    return normalized


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def build_quick_help_prompt(
    *,
    question: str,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    situation_projection: dict[str, Any],
) -> str:
    question = str(question or "").strip()[:MAX_MANAGER_CONTEXT_CHARS]
    return "\n\n".join(
        [
            "SYSTEM_RULES:\nТы — личный тренер продаж для менеджера по одной текущей сделке.",
            "RULES:\n"
            "- Отвечай на конкретный вопрос менеджера и помогай выполнить ближайшее действие.\n"
            "- Опирайся только на переданные CONTEXT и не придумывай ответ клиента, факты, даты, суммы или договорённости.\n"
            "- Не называй попытку звонка контактом. CRM-задача — поручение, а не доказательство клиентского результата.\n"
            "- Можно прямо сказать менеджеру, что повторяемое действие не работает, если это следует из контекста.\n"
            "- Диагноз должен быть видимым блоком «Что сейчас мешает».\n"
            "- Сформулируй выполнимую рекомендацию; критерий качества — менеджер понял и может сделать задачу.\n"
            "- Если данных не хватает, перечисли их в facts_to_clarify и не маскируй предположение под факт.\n"
            "- client_message — готовый короткий текст клиенту без плейсхолдеров; call_script — естественный сценарий звонка.\n"
            "- crm_checklist — только то, что менеджер должен зафиксировать после фактического действия.\n"
            "- Верни только полный объект по JSON-схеме, спокойно, эмпатично, прямо и по-русски.\n"
            "- Этот запрос независим: не используй и не запрашивай историю прошлых quick help.",
            _section("MANAGER_QUESTION", question),
            _section("SITUATION_CONTEXT", situation_projection),
            _section("ANALYSIS_CONTEXT", analysis_projection),
            _section("DEAL_CONTEXT", project_deal(deal)),
            _section("CURRENT_BITRIX_TASK", project_bitrix_task(current_bitrix_task)),
        ]
    )


def generate_deal_manager_quick_help(
    *,
    question: str,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    situation_projection: dict[str, Any],
    model: str = MANAGER_MODEL,
    reasoning_effort: str = MANAGER_REASONING_EFFORT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = build_quick_help_prompt(
        question=question,
        analysis_projection=analysis_projection,
        deal=deal,
        current_bitrix_task=current_bitrix_task,
        situation_projection=situation_projection,
    )
    result, metadata = call_structured_output_json(
        prompt,
        schema=quick_help_schema(),
        schema_name="deal_manager_quick_help",
        model=model,
        reasoning_effort=reasoning_effort,
        max_output_tokens=MAX_QUICK_HELP_OUTPUT_TOKENS,
        log_title="deal manager quick help prompt",
    )
    return validate_quick_help(result), metadata

