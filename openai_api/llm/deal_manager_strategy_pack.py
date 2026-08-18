"""One-strategy expansion of Quick Help into email, chat continuation and call script."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.deal_manager_email import email_schema, validate_email
from openai_api.llm.deal_manager_full_script import full_script_schema, validate_full_script
from openai_api.llm.deal_manager_quick_help import project_locked_move, project_quick_help_for_material
from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT, project_bitrix_task, project_deal
from openai_api.llm.llm_client import call_structured_output_json, deal_trace_id, prompt_prefix_before


PACK_CONTRACT = "strategy_pack_v1"
STRATEGIES = ("primary", "alternative", "pattern_break")
MAX_STRATEGY_PACK_OUTPUT_TOKENS = 9000


def strategy_pack_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["pack_contract", "selected_strategy", "email", "message_script", "call_script"],
        "properties": {
            "pack_contract": {"type": "string", "enum": [PACK_CONTRACT]},
            "selected_strategy": {"type": "string", "enum": list(STRATEGIES)},
            "email": email_schema(),
            "message_script": full_script_schema("message"),
            "call_script": full_script_schema("call"),
        },
    }


def validate_strategy_pack(value: Any, *, selected_strategy: str, allowed_tactic_ids: set[str] | None = None, allowed_objection_ids: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("pack_contract") != PACK_CONTRACT:
        raise ValueError("Пакет стратегии имеет неподдерживаемый контракт")
    if selected_strategy not in STRATEGIES or value.get("selected_strategy") != selected_strategy:
        raise ValueError("Пакет стратегии не соответствует выбранному варианту")
    return {
        "pack_contract": PACK_CONTRACT,
        "selected_strategy": selected_strategy,
        "email": validate_email(value.get("email"), selected_strategy=selected_strategy),
        "message_script": validate_full_script(
            value.get("message_script"), selected_strategy=selected_strategy, script_mode="message",
            allowed_tactic_ids=allowed_tactic_ids, allowed_objection_ids=allowed_objection_ids,
        ),
        "call_script": validate_full_script(
            value.get("call_script"), selected_strategy=selected_strategy, script_mode="call",
            allowed_tactic_ids=allowed_tactic_ids, allowed_objection_ids=allowed_objection_ids,
        ),
    }


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def build_strategy_pack_prompt(
    *,
    analysis_projection: dict[str, Any],
    situation_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    checklist: dict[str, Any],
    communication_pattern_context: dict[str, Any],
    quick_help: dict[str, Any],
    selected_strategy: str,
    relevant_tactics: list[dict[str, Any]],
    objection_handling: dict[str, Any] | None = None,
) -> str:
    locked_move = project_locked_move(quick_help, selected_strategy)
    return "\n\n".join([
        "SYSTEM_RULES:\nТы раскрываешь уже выбранный ход Дожима или Реаниматора в три канала одной карточки. Не анализируй сделку заново и не придумывай новую стратегию.",
        "RULES:\n"
        "- LOCKED_MOVE — источник личности. Письмо, переписка и звонок должны звучать как тот же человек: тот же тон, рычаг, аргумент, CTA и ближайшая цель.\n"
        "- Не меняй смысл selected_client_message. Не подмешивай другие варианты Quick Help: их сюда специально не передают.\n"
        "- Если mode = push, держи экспертный коммерческий тон через pressure_lever, без канцелярита и без пустого «посмотрели КП?». Если mode = reanimator, держи мягкое восстановление контакта: польза ответить, низкое усилие, один следующий шаг. Не смешивай эти голоса.\n"
        "- email — тот же заход в формате письма: тема, обращение, контекст, 1–4 связанных вопроса только ради одной цели, аргумент, следующий шаг, завершение. Не анкета.\n"
        "- message_script — продолжение переписки. Первое сообщение — сам selected_client_message или его естественное продолжение в том же голосе. 3–6 коротких блоков, не телефонный разговор.\n"
        "- call_script — устный разговор. Готовое сообщение ещё не сказано по телефону: первый блок — живая устная версия того же захода, не «продолжение с середины» и не дословная переписка. Перед каждым существенным вопросом дай связку «зачем спрашиваю» и сам вопрос в той же реплике spoken_text. Не выноси вопрос в clarifying_question: это поле всегда пустое. Финал — резюме только подтверждённого + следующий шаг + «всё верно зафиксировал?». Не выдумывай будущие договорённости.\n"
        "- Незакрытые пункты CURRENT_DAILY_CHECKLIST помоги получить естественно, но не создавай новый checklist.\n"
        "- RELEVANT_TACTICS — только условные приёмы. OBJECTION_HANDLING не переписывай: в call_script укажи не более двух существующих objection_id, если уместны.\n"
        "- Не придумывай даты, суммы, имена, договорённости или слова клиента. Верни только JSON по схеме на русском языке.",
        _section("ANALYSIS_CONTEXT", analysis_projection),
        _section("SITUATION_CONTEXT", situation_projection),
        _section("DEAL_CONTEXT", project_deal(deal)),
        _section("CURRENT_BITRIX_TASK", project_bitrix_task(current_bitrix_task)),
        _section("CURRENT_DAILY_CHECKLIST", checklist),
        _section("COMMUNICATION_PATTERN_CONTEXT", communication_pattern_context),
        _section("RELEVANT_TACTICS", relevant_tactics),
        _section("OBJECTION_HANDLING", objection_handling or {"items": []}),
        _section("LOCKED_MOVE", locked_move),
        _section("QUICK_HELP", project_quick_help_for_material(quick_help, selected_strategy)),
        _section("SELECTED_STRATEGY", selected_strategy),
        _section("ASSISTANT_MODE", locked_move["mode"]),
        _section("PRESSURE_LEVER", locked_move["pressure_lever"]),
    ])


def generate_strategy_pack(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_strategy = str(kwargs["selected_strategy"])
    relevant_tactics = kwargs.get("relevant_tactics")
    objection_handling = kwargs.get("objection_handling")
    allowed_tactic_ids = {
        str(item.get("tactic_id") or "").strip()
        for item in relevant_tactics if isinstance(item, dict)
    } if isinstance(relevant_tactics, list) else set()
    allowed_objection_ids = {
        str(item.get("objection_id") or "").strip()
        for item in objection_handling.get("items", []) if isinstance(item, dict)
    } if isinstance(objection_handling, dict) else set()
    prompt = build_strategy_pack_prompt(**kwargs)
    result, metadata = call_structured_output_json(
        prompt, schema=strategy_pack_schema(), schema_name="deal_manager_strategy_pack", model=MANAGER_MODEL,
        reasoning_effort=MANAGER_REASONING_EFFORT, max_output_tokens=MAX_STRATEGY_PACK_OUTPUT_TOKENS,
        log_title="deal manager strategy pack prompt", call_type="deal_manager_strategy_pack",
        prompt_cache_key="neuro-rop:deal-manager-strategy-pack:v1",
        stable_prefix=prompt_prefix_before(prompt, "LOCKED_MOVE:"),
        trace_entity_type="deal",
        trace_entity_id=deal_trace_id(kwargs.get("deal")),
    )
    return validate_strategy_pack(
        result, selected_strategy=selected_strategy,
        allowed_tactic_ids=allowed_tactic_ids, allowed_objection_ids=allowed_objection_ids,
    ), metadata
