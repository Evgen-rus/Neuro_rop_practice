"""Strict on-demand business email built from the current manager context."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT, project_bitrix_task, project_deal
from openai_api.llm.llm_client import call_structured_output_json, deal_trace_id, prompt_prefix_before
from openai_api.llm.deal_manager_quick_help import project_locked_move, project_quick_help_for_material
from openai_api.llm.prompt_parts import assemble_prompt, static_prompt_from_full


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


def email_context_sections(**kwargs: Any) -> list[str]:
    selected_strategy = str(kwargs["selected_strategy"])
    locked_move = project_locked_move(kwargs.get("quick_help"), selected_strategy)
    return [
        _section("ANALYSIS_CONTEXT", kwargs["analysis_projection"]),
        _section("SITUATION_CONTEXT", kwargs["situation_projection"]),
        _section("DEAL_CONTEXT", project_deal(kwargs["deal"])),
        _section("CURRENT_BITRIX_TASK", project_bitrix_task(kwargs.get("current_bitrix_task"))),
        _section("COMMUNICATION_PATTERN_CONTEXT", kwargs["communication_pattern_context"]),
        _section("LOCKED_MOVE", locked_move),
        _section("SELECTED_STRATEGY", selected_strategy),
        _section("ASSISTANT_MODE", locked_move["mode"]),
        _section("PRESSURE_LEVER", locked_move["pressure_lever"]),
        _section("QUICK_HELP", project_quick_help_for_material(kwargs.get("quick_help"), selected_strategy)),
    ]


def email_static_prompt() -> str:
    return static_prompt_from_full(
        build_email_prompt(
            selected_strategy="primary",
            analysis_projection={},
            situation_projection={},
            deal={},
            current_bitrix_task=None,
            communication_pattern_context={},
            quick_help={"mode": "push", "situation_summary": "x", "next_action": "x", "expected_result": "x", "pressure_lever": {"title": "x", "rationale": "x"}, "strategy_labels": {"primary": "a", "alternative": "b", "pattern_break": "c"}, "client_messages": {"primary": "m", "alternative": "m2", "pattern_break": "m3"}, "lifehacks": [], "fallback_action": "f"},
        ),
        "ANALYSIS_CONTEXT:",
    )


def build_email_prompt(**kwargs: Any) -> str:
    return "\n\n".join([
        "SYSTEM_RULES:\nТы — прикладной помощник менеджера. Подготовь одно деловое email-письмо по текущей сделке; полный анализ уже выполнен.",
        "RULES:\n"
        "- LOCKED_MOVE — уже выбранный ход Дожима или Реаниматора. Письмо должно звучать как тот же человек и решать ту же ближайшую micro-conversion, а не начинать новую стратегию.\n"
        "- Продолжай выбранную стратегию текущего режима помощника. Опирайся на selected_client_message как на эталон захода: тот же тон, рычаг, аргумент и CTA, адаптированные в формат письма.\n"
        "- Если QUICK_HELP.mode = push, пиши экспертно и предметно, опираясь на pressure_lever, без канцелярита и без пустого «посмотрели КП?». Если mode = reanimator, пиши мягче: напомни контекст, дай понятную пользу ответить и один низкий по усилию следующий шаг. Не смешивай эти голоса.\n"
        "- У письма должна быть самостоятельная тема, обращение, краткий контекст, 1–4 связанных вопроса, полезный аргумент, ясный следующий шаг и завершение.\n"
        "- Вопросы допустимы только если нужны для одной общей цели письма; не превращай письмо в анкету.\n"
        "- Используй ANALYSIS_CONTEXT.client_communication_profile для формы подачи: D — прямота и результат, I — живой интерес и образ результата, S — спокойствие и снижение риска, C — структура и точность. Не называй DISC клиенту и не выводи из профиля факты или страхи. При недостаточных данных пиши нейтрально.\n"
        "- Не создавай КП и не обещай вложения или материалы, которых нет в контексте. Не придумывай факты, даты, суммы, имена и договорённости.\n"
        "- Верни только JSON по схеме на русском языке.",
        *email_context_sections(**kwargs),
    ])


def generate_deal_manager_email(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_strategy = str(kwargs["selected_strategy"])
    prompt_template = kwargs.pop("prompt_template", None)
    model = kwargs.pop("model", None) or MANAGER_MODEL
    reasoning_effort = kwargs.pop("reasoning_effort", None) or MANAGER_REASONING_EFFORT
    call_type = kwargs.pop("call_type", None) or "deal_manager_email"
    prompt = assemble_prompt(prompt_template, email_context_sections(**kwargs)) if prompt_template else build_email_prompt(**kwargs)
    result, metadata = call_structured_output_json(
        prompt, schema=email_schema(), schema_name="deal_manager_email", model=model,
        reasoning_effort=reasoning_effort, max_output_tokens=MAX_EMAIL_OUTPUT_TOKENS,
        log_title="deal manager email prompt", call_type=call_type,
        prompt_cache_key="neuro-rop:deal-manager-email:v3",
        stable_prefix=prompt_prefix_before(prompt, "LOCKED_MOVE:"),
        trace_entity_type="deal",
        trace_entity_id=deal_trace_id(kwargs.get("deal")),
    )
    return validate_email(result, selected_strategy=selected_strategy), metadata
