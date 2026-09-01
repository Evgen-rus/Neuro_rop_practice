"""Strict, evidence-first LLM contract for Learning Shadow deal episodes."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.llm_client import call_structured_output_json


LEARNING_SHADOW_MODEL = "gpt-5.6-luna"
LEARNING_SHADOW_REASONING_EFFORT = "xhigh"
LEARNING_SHADOW_MAX_OUTPUT_TOKENS = 12000


def learning_shadow_schema() -> dict[str, Any]:
    correlation = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "recommendation_id", "application", "manager_actions",
            "evidence_event_ids", "explanation", "confidence",
        ],
        "properties": {
            "recommendation_id": {"type": "string"},
            "application": {
                "type": "string",
                "enum": ["confirmed", "probable", "possible", "no_evidence", "contradicted"],
            },
            "manager_actions": {"type": "array", "items": {"type": "string"}},
            "evidence_event_ids": {"type": "array", "items": {"type": "string"}},
            "explanation": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }
    reaction = {
        "type": "object",
        "additionalProperties": False,
        "required": ["related_action_event_ids", "reaction", "summary", "evidence_event_ids"],
        "properties": {
            "related_action_event_ids": {"type": "array", "items": {"type": "string"}},
            "reaction": {
                "type": "string",
                "enum": ["positive", "neutral", "negative", "no_response", "unknown"],
            },
            "summary": {"type": "string"},
            "evidence_event_ids": {"type": "array", "items": {"type": "string"}},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "case_summary", "recommendation_correlations", "client_reactions",
            "business_result", "business_result_summary", "overall_summary",
        ],
        "properties": {
            "case_summary": {"type": "string"},
            "recommendation_correlations": {"type": "array", "items": correlation},
            "client_reactions": {"type": "array", "items": reaction},
            "business_result": {
                "type": "string",
                "enum": [
                    "progressed", "next_step_created", "contact_only", "stalled",
                    "rejected", "unknown",
                ],
            },
            "business_result_summary": {"type": "string"},
            "overall_summary": {"type": "string"},
        },
    }


def build_learning_shadow_prompt(case: dict[str, Any]) -> str:
    compact = {
        "deal_id": case.get("deal_id"),
        "manager_id": case.get("manager_id"),
        "observation_window": {
            "from": case.get("first_view_at"),
            "to": case.get("period_end_at"),
        },
        "recommendations": case.get("recommendations") or [],
        "timeline": case.get("timeline") or [],
    }
    return (
        "Ты анализируешь исследовательский Learning Shadow case одной сделки. "
        "Найди все существенные смысловые корреляции между просмотренными рекомендациями, "
        "действиями менеджера, реакциями клиента и бизнес-результатом. "
        "Просмотр рекомендации не доказывает применение. Последующее действие не доказывает "
        "причинность. Учитывай содержание, канал, время и конкурирующие рекомендации. "
        "Если связь не подтверждается evidence, используй possible или no_evidence; не завышай "
        "confidence. Ссылайся только на event_id из timeline. Не придумывай факты.\n\n"
        "CASE_JSON:\n" + json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def analyze_learning_shadow_case(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    return call_structured_output_json(
        build_learning_shadow_prompt(case),
        schema=learning_shadow_schema(),
        schema_name="learning_shadow_case",
        model=LEARNING_SHADOW_MODEL,
        reasoning_effort=LEARNING_SHADOW_REASONING_EFFORT,
        max_output_tokens=LEARNING_SHADOW_MAX_OUTPUT_TOKENS,
        log_title="learning shadow case prompt",
        call_type="learning_shadow_case",
        trace_entity_type="deal",
        trace_entity_id=str(case.get("deal_id") or ""),
    )
