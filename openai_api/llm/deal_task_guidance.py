"""Generate manager guidance for one explicit ROP deal-control task."""

from __future__ import annotations

import json
from typing import Any

from openai_api.config import ANALYSIS_MODEL
from openai_api.llm.llm_client import call_structured_output_json


MAX_GUIDANCE_OUTPUT_TOKENS = 5000


def _short_text_list_schema(max_items: int = 4) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "maxItems": max_items,
    }


def deal_task_guidance_schema() -> dict[str, Any]:
    fields = {
        "task_focus": {"type": "string", "minLength": 1},
        "expected_outcome": {"type": "string", "minLength": 1},
        "known_facts": _short_text_list_schema(),
        "missing_facts": _short_text_list_schema(),
        "contact_goal": {"type": "string", "minLength": 1},
        "contact_questions": _short_text_list_schema(),
        "ready_text": {"type": "string", "minLength": 1},
        "crm_checklist": _short_text_list_schema(),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(fields),
        "properties": fields,
    }


def unwrap_analysis(value: Any) -> dict[str, Any]:
    current = value
    for _ in range(3):
        if not isinstance(current, dict):
            return {}
        nested = current.get("analysis")
        if isinstance(nested, dict):
            current = nested
            continue
        return current
    return current if isinstance(current, dict) else {}


def compact_analysis_context(report_json: Any) -> dict[str, Any]:
    analysis = unwrap_analysis(report_json)
    allowed_fields = (
        "deal_state",
        "deal_control_brief",
        "qualification_assessment",
        "deal_mode",
        "main_risk",
        "payment_blocker",
        "money_path_diagnosis",
        "manager_action_block",
        "new_event",
    )
    return {field: analysis[field] for field in allowed_fields if field in analysis}


def build_deal_task_guidance_prompt(
    *,
    task: dict[str, Any],
    deal: dict[str, Any],
    report_json: Any,
) -> str:
    task_context = {
        "task_id": task.get("id"),
        "task_text": task.get("task_text"),
        "touch_type": task.get("touch_type"),
        "expected_result": task.get("expected_result"),
        "due_at": task.get("due_at"),
    }
    deal_context = {
        "deal_id": deal.get("deal_id"),
        "title": deal.get("title"),
        "stage_name": deal.get("stage_name"),
        "amount": deal.get("amount"),
        "currency_id": deal.get("currency_id"),
        "manager_name": deal.get("manager_name"),
    }
    return f"""
Ты готовишь менеджера к выполнению одной конкретной задачи РОПа по сделке.
Ответ должен помогать выполнить именно задачу, а не пересказывать весь анализ сделки.

Жёсткие правила:
- Не выдумывай факты, людей, даты, договорённости, возражения или ответы клиента.
- Подтверждённые факты бери только из ANALYSIS_CONTEXT и DEAL_CONTEXT.
- Локальная задача РОПа является поручением, но не доказательством контакта или результата клиента.
- Если факта нет, перенеси его в missing_facts или сформулируй вопрос клиенту.
- Не повторяй вопросы, ответы на которые уже подтверждены в анализе.
- contact_goal и expected_outcome должны быть измеримыми и соответствовать задаче РОПа.
- ready_text должен соответствовать touch_type: для звонка это естественный речевой модуль,
  для email/мессенджера — готовый текст сообщения. Не используй плейсхолдеры.
- crm_checklist содержит только то, что менеджер должен зафиксировать в CRM после контакта.
- Не утверждай, что задача выполнена, пока результата клиента нет.
- Пиши компактно: task_focus, expected_outcome и contact_goal — по одному предложению;
  known_facts, missing_facts, contact_questions и crm_checklist — не более 4 коротких пунктов;
  ready_text — до 1200 знаков, без повторения анализа и вводных рассуждений.
- Пиши по-русски, спокойно, конкретно и без канцелярита.

TASK_CONTEXT:
{json.dumps(task_context, ensure_ascii=False, indent=2)}

DEAL_CONTEXT:
{json.dumps(deal_context, ensure_ascii=False, indent=2)}

ANALYSIS_CONTEXT:
{json.dumps(compact_analysis_context(report_json), ensure_ascii=False, indent=2)}

Верни только объект по заданной JSON-схеме.
""".strip()


def generate_deal_task_guidance(
    *,
    task: dict[str, Any],
    deal: dict[str, Any],
    report_json: Any,
    model: str = ANALYSIS_MODEL,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = build_deal_task_guidance_prompt(task=task, deal=deal, report_json=report_json)
    return call_structured_output_json(
        prompt,
        schema=deal_task_guidance_schema(),
        schema_name="deal_task_guidance",
        model=model,
        max_output_tokens=MAX_GUIDANCE_OUTPUT_TOKENS,
        log_title="deal task guidance prompt",
        call_type="deal_task_guidance",
        disable_implicit_cache=True,
    )
