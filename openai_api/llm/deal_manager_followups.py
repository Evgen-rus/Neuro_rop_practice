"""Strict current-situation follow-up idea generation for a deal manager."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT, project_bitrix_task, project_deal
from openai_api.llm.llm_client import call_structured_output_json, deal_trace_id, prompt_prefix_before


FOLLOWUPS_CONTRACT = "followup_plan_v1"
MAX_FOLLOWUPS_OUTPUT_TOKENS = 3600
_BASIS = ("confirmed", "inferred", "generic")
_FORMATS = ("video", "article", "checklist", "email", "case", "news", "useful_tip", "other")


def followups_schema() -> dict[str, Any]:
    item = {
        "type": "object", "additionalProperties": False,
        "required": ["item_id", "concern_or_scenario", "basis_status", "evidence_summary", "followup_type", "idea", "why_it_may_help", "suggested_channel", "timing", "target_micro_conversion", "caution"],
        "properties": {
            "item_id": {"type": "string", "minLength": 1, "maxLength": 60},
            "concern_or_scenario": {"type": "string", "minLength": 1, "maxLength": 320},
            "basis_status": {"type": "string", "enum": list(_BASIS)},
            "evidence_summary": {"type": "string", "minLength": 1, "maxLength": 420},
            "followup_type": {"type": "string", "enum": list(_FORMATS)},
            "idea": {"type": "string", "minLength": 1, "maxLength": 700},
            "why_it_may_help": {"type": "string", "minLength": 1, "maxLength": 500},
            "suggested_channel": {"type": "string", "minLength": 1, "maxLength": 120},
            "timing": {"type": "string", "minLength": 1, "maxLength": 240},
            "target_micro_conversion": {"type": "string", "minLength": 1, "maxLength": 320},
            "caution": {"type": "string", "minLength": 1, "maxLength": 360},
        },
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["followups_contract", "context_summary", "items"],
        "properties": {
            "followups_contract": {"type": "string", "enum": [FOLLOWUPS_CONTRACT]},
            "context_summary": {"type": "string", "minLength": 1, "maxLength": 500},
            "items": {"type": "array", "minItems": 3, "maxItems": 5, "items": item},
        },
    }


def validate_followups(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("followups_contract") != FOLLOWUPS_CONTRACT:
        raise ValueError("Фоллоуапы имеют неподдерживаемый контракт")
    summary = value.get("context_summary")
    items = value.get("items")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(items, list) or not 3 <= len(items) <= 5:
        raise ValueError("Фоллоуапы должны содержать резюме и от 3 до 5 идей")
    fields = {"item_id": 60, "concern_or_scenario": 320, "evidence_summary": 420, "idea": 700, "why_it_may_help": 500, "suggested_channel": 120, "timing": 240, "target_micro_conversion": 320, "caution": 360}
    normalized: list[dict[str, str]] = []
    used_ids: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict) or raw.get("basis_status") not in _BASIS or raw.get("followup_type") not in _FORMATS:
            raise ValueError("Карточка фоллоуапа заполнена некорректно")
        item: dict[str, str] = {"basis_status": raw["basis_status"], "followup_type": raw["followup_type"]}
        for field, limit in fields.items():
            text = raw.get(field)
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Карточка фоллоуапа не содержит {field}")
            item[field] = text.strip()[:limit]
        if item["item_id"] in used_ids:
            raise ValueError("Фоллоуапы содержат повторяющийся item_id")
        used_ids.add(item["item_id"])
        normalized.append(item)
    return {"followups_contract": FOLLOWUPS_CONTRACT, "context_summary": summary.strip()[:500], "items": normalized}


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def build_followups_prompt(**kwargs: Any) -> str:
    return "\n\n".join([
        "SYSTEM_RULES:\nТы — помощник менеджера по дожиму текущей сделки. Предложи идеи полезных follow-up касаний, но не создавай сами материалы.",
        "RULES:\n"
        "- Дай 3–5 действительно разных вариантов: под подтверждённые сомнения, обоснованные потенциальные барьеры и молчание или потерю динамики, если это применимо.\n"
        "- confirmed разрешён только при прямом evidence в контексте; inferred — осторожная гипотеза; generic — условный сценарий. Явно сохраняй эту границу.\n"
        "- Каждое касание должно добавлять новую пользу: снять конкретную неопределённость, помочь сравнить варианты, упростить согласование или напомнить по содержательному поводу. Не пиши пустое 'напоминаю о себе'.\n"
        "- Предлагай идею видео, статьи, чек-листа, письма, кейса, новости или полезного совета. Не утверждай, что материал уже существует, и не генерируй сам материал или готовое сопроводительное сообщение.\n"
        "- Для каждой идеи задай один проверяемый клиентский результат. Не придумывай сроки; timing описывай относительно известной договорённости или состояния сделки.\n"
        "- Используй client_communication_profile только для рекомендуемой формы и канала: DISC не доказывает страх или факт клиента. При недостаточных данных используй нейтральный стиль.\n"
        "- Не создавай КП, скидки, искусственный дефицит или неподтверждённые обещания. Верни только JSON по схеме на русском языке.",
        _section("ANALYSIS_CONTEXT", kwargs["analysis_projection"]),
        _section("SITUATION_CONTEXT", kwargs["situation_projection"]),
        _section("DEAL_CONTEXT", project_deal(kwargs["deal"])),
        _section("CURRENT_BITRIX_TASK", project_bitrix_task(kwargs.get("current_bitrix_task"))),
        _section("COMMUNICATION_PATTERN_CONTEXT", kwargs["communication_pattern_context"]),
    ])


def generate_deal_manager_followups(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = build_followups_prompt(**kwargs)
    result, metadata = call_structured_output_json(
        prompt, schema=followups_schema(), schema_name="deal_manager_followups", model=MANAGER_MODEL,
        reasoning_effort=MANAGER_REASONING_EFFORT, max_output_tokens=MAX_FOLLOWUPS_OUTPUT_TOKENS,
        log_title="deal manager followups prompt", call_type="deal_manager_followups",
        prompt_cache_key="neuro-rop:deal-manager-followups:v1",
        stable_prefix=prompt_prefix_before(prompt, "COMMUNICATION_PATTERN_CONTEXT:"),
        trace_entity_type="deal",
        trace_entity_id=deal_trace_id(kwargs.get("deal")),
    )
    return validate_followups(result), metadata
