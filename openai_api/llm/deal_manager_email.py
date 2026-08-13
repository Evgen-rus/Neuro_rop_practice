"""Strict on-demand business email built from the current manager context."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT, project_bitrix_task, project_deal
from openai_api.llm.llm_client import call_structured_output_json, prompt_prefix_before


EMAIL_CONTRACT = "manager_email_v1"
STRATEGIES = ("primary", "alternative", "pattern_break")
MAX_EMAIL_OUTPUT_TOKENS = 2600


def email_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["email_contract", "selected_strategy", "subject", "greeting", "context", "questions", "value_point", "call_to_action", "closing"],
        "properties": {
            "email_contract": {"type": "string", "enum": [EMAIL_CONTRACT]},
            "selected_strategy": {"type": "string", "enum": list(STRATEGIES)},
            "subject": {"type": "string", "minLength": 1, "maxLength": 180},
            "greeting": {"type": "string", "minLength": 1, "maxLength": 180},
            "context": {"type": "string", "minLength": 1, "maxLength": 1000},
            "questions": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "minLength": 1, "maxLength": 420}},
            "value_point": {"type": "string", "minLength": 1, "maxLength": 700},
            "call_to_action": {"type": "string", "minLength": 1, "maxLength": 420},
            "closing": {"type": "string", "minLength": 1, "maxLength": 240},
        },
    }


def validate_email(value: Any, *, selected_strategy: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("email_contract") != EMAIL_CONTRACT:
        raise ValueError("Email имеет неподдерживаемый контракт")
    if selected_strategy not in STRATEGIES or value.get("selected_strategy") != selected_strategy:
        raise ValueError("Email не соответствует выбранной стратегии")
    limits = {"subject": 180, "greeting": 180, "context": 1000, "value_point": 700, "call_to_action": 420, "closing": 240}
    result: dict[str, Any] = {"email_contract": EMAIL_CONTRACT, "selected_strategy": selected_strategy}
    for field, limit in limits.items():
        text = value.get(field)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Поле email {field} не заполнено")
        result[field] = text.strip()[:limit]
    questions = value.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4 or any(not isinstance(item, str) or not item.strip() for item in questions):
        raise ValueError("Email должен содержать от 1 до 4 связанных вопросов")
    result["questions"] = [item.strip()[:420] for item in questions]
    return result


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def build_email_prompt(**kwargs: Any) -> str:
    return "\n\n".join([
        "SYSTEM_RULES:\nТы — прикладной помощник менеджера. Подготовь одно деловое email-письмо по текущей сделке; полный анализ уже выполнен.",
        "RULES:\n"
        "- Продолжай выбранную стратегию Quick Help и решай одну ближайшую micro-conversion.\n"
        "- У письма должна быть самостоятельная тема, обращение, краткий контекст, 1–4 связанных вопроса, полезный аргумент, ясный следующий шаг и завершение.\n"
        "- Вопросы допустимы только если нужны для одной общей цели письма; не превращай письмо в анкету.\n"
        "- Используй ANALYSIS_CONTEXT.client_communication_profile для формы подачи: D — прямота и результат, I — живой интерес и образ результата, S — спокойствие и снижение риска, C — структура и точность. Не называй DISC клиенту и не выводи из профиля факты или страхи. При недостаточных данных пиши нейтрально.\n"
        "- Не создавай КП и не обещай вложения или материалы, которых нет в контексте. Не придумывай факты, даты, суммы, имена и договорённости.\n"
        "- Верни только JSON по схеме на русском языке.",
        _section("ANALYSIS_CONTEXT", kwargs["analysis_projection"]),
        _section("SITUATION_CONTEXT", kwargs["situation_projection"]),
        _section("DEAL_CONTEXT", project_deal(kwargs["deal"])),
        _section("CURRENT_BITRIX_TASK", project_bitrix_task(kwargs.get("current_bitrix_task"))),
        _section("COMMUNICATION_PATTERN_CONTEXT", kwargs["communication_pattern_context"]),
        _section("SELECTED_STRATEGY", kwargs["selected_strategy"]),
        _section("QUICK_HELP", kwargs["quick_help"]),
    ])


def generate_deal_manager_email(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_strategy = str(kwargs["selected_strategy"])
    prompt = build_email_prompt(**kwargs)
    result, metadata = call_structured_output_json(
        prompt, schema=email_schema(), schema_name="deal_manager_email", model=MANAGER_MODEL,
        reasoning_effort=MANAGER_REASONING_EFFORT, max_output_tokens=MAX_EMAIL_OUTPUT_TOKENS,
        log_title="deal manager email prompt", call_type="deal_manager_email",
        prompt_cache_key="neuro-rop:deal-manager-email:v1",
        stable_prefix=prompt_prefix_before(prompt, "SELECTED_STRATEGY:"),
    )
    return validate_email(result, selected_strategy=selected_strategy), metadata
