"""Strict on-demand conversation script built on the existing manager context."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT, project_bitrix_task, project_deal
from openai_api.llm.llm_client import call_structured_output_json, prompt_prefix_before


MAX_FULL_SCRIPT_OUTPUT_TOKENS = 5000
SCRIPT_CONTRACT = "conversation_script_v1"
STRATEGIES = ("primary", "alternative", "pattern_break")
SCRIPT_MODES = ("message", "call")


def full_script_schema() -> dict[str, Any]:
    short_list = {"type": "array", "maxItems": 3, "items": {"type": "string", "minLength": 1, "maxLength": 500}}
    block = {
        "type": "object",
        "additionalProperties": False,
        "required": ["block_id", "title", "objective", "suggested_phrases", "listen_for", "transition"],
        "properties": {
            "block_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "title": {"type": "string", "minLength": 1, "maxLength": 160},
            "objective": {"type": "string", "minLength": 1, "maxLength": 420},
            "suggested_phrases": short_list,
            "listen_for": short_list,
            "transition": {"type": "string", "minLength": 1, "maxLength": 420},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["script_contract", "selected_strategy", "conversation_goal", "blocks", "closing_agreement", "relevant_tactic_ids"],
        "properties": {
            "script_contract": {"type": "string", "enum": [SCRIPT_CONTRACT]},
            "selected_strategy": {"type": "string", "enum": list(STRATEGIES)},
            "conversation_goal": {"type": "string", "minLength": 1, "maxLength": 420},
            "blocks": {"type": "array", "minItems": 3, "maxItems": 6, "items": block},
            "closing_agreement": {"type": "string", "minLength": 1, "maxLength": 500},
            "relevant_tactic_ids": {"type": "array", "maxItems": 3, "items": {"type": "string", "minLength": 1, "maxLength": 80}},
        },
    }


def validate_full_script(value: Any, *, selected_strategy: str, allowed_tactic_ids: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("script_contract") != SCRIPT_CONTRACT:
        raise ValueError("Полный скрипт имеет неподдерживаемый контракт")
    if selected_strategy not in STRATEGIES or value.get("selected_strategy") != selected_strategy:
        raise ValueError("Полный скрипт не соответствует выбранной стратегии")
    goal = value.get("conversation_goal")
    closing = value.get("closing_agreement")
    blocks = value.get("blocks")
    tactic_ids = value.get("relevant_tactic_ids")
    if not isinstance(goal, str) or not goal.strip() or not isinstance(closing, str) or not closing.strip():
        raise ValueError("В полном скрипте отсутствует цель или завершение")
    if not isinstance(blocks, list) or not 3 <= len(blocks) <= 6:
        raise ValueError("Полный скрипт должен содержать от 3 до 6 блоков")
    normalized_blocks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in blocks:
        if not isinstance(item, dict):
            raise ValueError("Блок полного скрипта должен быть объектом")
        block_id = str(item.get("block_id") or "").strip()
        title = str(item.get("title") or "").strip()
        objective = str(item.get("objective") or "").strip()
        transition = str(item.get("transition") or "").strip()
        phrases = item.get("suggested_phrases")
        listen_for = item.get("listen_for")
        if not block_id or block_id in seen_ids or not title or not objective or not transition:
            raise ValueError("Блок полного скрипта заполнен некорректно")
        if not isinstance(phrases, list) or not 1 <= len(phrases) <= 3 or not isinstance(listen_for, list) or len(listen_for) > 3:
            raise ValueError("Фразы или ориентиры блока заполнены некорректно")
        if any(not isinstance(text, str) or not text.strip() for text in [*phrases, *listen_for]):
            raise ValueError("Фразы блока должны быть непустыми строками")
        seen_ids.add(block_id)
        normalized_blocks.append({
            "block_id": block_id[:80], "title": title[:160], "objective": objective[:420],
            "suggested_phrases": [text.strip()[:500] for text in phrases],
            "listen_for": [text.strip()[:500] for text in listen_for], "transition": transition[:420],
        })
    if not isinstance(tactic_ids, list) or len(tactic_ids) > 3 or any(not isinstance(item, str) or not item.strip() for item in tactic_ids):
        raise ValueError("relevant_tactic_ids заполнен некорректно")
    if allowed_tactic_ids is not None and any(item.strip() not in allowed_tactic_ids for item in tactic_ids):
        raise ValueError("relevant_tactic_ids содержит тактику вне Quick Help")
    return {
        "script_contract": SCRIPT_CONTRACT,
        "selected_strategy": selected_strategy,
        "conversation_goal": goal.strip()[:420],
        "blocks": normalized_blocks,
        "closing_agreement": closing.strip()[:500],
        "relevant_tactic_ids": [item.strip()[:80] for item in tactic_ids],
    }


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def build_full_script_prompt(*, analysis_projection: dict[str, Any], situation_projection: dict[str, Any], deal: dict[str, Any], current_bitrix_task: dict[str, Any] | None, checklist: dict[str, Any], communication_pattern_context: dict[str, Any], quick_help: dict[str, Any], selected_strategy: str, relevant_tactics: list[dict[str, Any]], script_mode: str = "message") -> str:
    if script_mode not in SCRIPT_MODES:
        raise ValueError("Неизвестный режим сценария")
    mode_rules = (
        "- Это сценарий именно телефонного звонка. Не предлагай переписку, мессенджер или отправку сообщения как основной блок. "
        "Начни с короткого устного входа, проведи менеджера через вопросы и аргументацию и закончи конкретной договорённостью, двигающей сделку к деньгам.\n"
        "- QUICK_HELP — это уже сделанный заход к клиенту. Не дублируй его текст: используй выбранную стратегию как основание звонка и продолжай с текущей точки.\n"
        "- Если ANALYSIS_CONTEXT.client_communication_profile имеет status tentative или supported, адаптируй устную речь под primary_style, secondary_style, profile_confidence и recommended_communication: темп, прямоту, длину фраз, порядок аргументов и способ задавать вопросы. Не объясняй DISC менеджеру и не меняй факты. При insufficient_evidence используй нейтральный деловой стиль.\n"
    ) if script_mode == "call" else (
        "- Это сценарий продолжения переписки. Давай короткие готовые сообщения и ветки ответа; не превращай его в телефонный разговор.\n"
        "- Если ANALYSIS_CONTEXT.client_communication_profile имеет status tentative или supported, адаптируй длину, прямоту и структуру сообщений под сохранённый профиль. При insufficient_evidence используй нейтральный деловой стиль.\n"
    )
    return "\n\n".join([
        "SYSTEM_RULES:\nТы — прикладной помощник менеджера во время реального разговора по одной сделке. Полный анализ уже выполнен: не анализируй сделку заново.",
        "RULES:\n"
        + mode_rules +
        "- Продолжай ровно выбранный менеджером вариант сообщения. primary соответствует варианту 1, alternative — 2, pattern_break — 3.\n"
        "- Сделай 3–6 коротких диалоговых блоков, а не текст для чтения целиком и не список из двадцати вопросов.\n"
        "- В каждом блоке укажи цель, 1–3 естественные фразы, что услышать в ответе и переход дальше.\n"
        "- Незакрытые пункты CURRENT_DAILY_CHECKLIST помоги получить естественно, но не создавай новый checklist и не объявляй отметки менеджера фактами клиента.\n"
        "- RELEVANT_TACTICS — допустимые условные приёмы. Не обещай их доступность и не превращай в факт без подтверждения контекстом.\n"
        "- Не выполняй новый анализ возражений и не включай objection handling: UI покажет готовую проекцию полного анализа отдельно.\n"
        "- Не придумывай даты, суммы, наличие оборудования, специалистов, рассрочки, повышение цен, договорённости или слова клиента.\n"
        "- Заверши разговор одной конкретной проверяемой договорённостью. Верни только JSON по схеме на русском языке.",
        _section("ANALYSIS_CONTEXT", analysis_projection), _section("SITUATION_CONTEXT", situation_projection),
        _section("DEAL_CONTEXT", project_deal(deal)), _section("CURRENT_BITRIX_TASK", project_bitrix_task(current_bitrix_task)),
        _section("CURRENT_DAILY_CHECKLIST", checklist), _section("COMMUNICATION_PATTERN_CONTEXT", communication_pattern_context),
        _section("SCRIPT_MODE", script_mode), _section("QUICK_HELP", quick_help), _section("SELECTED_STRATEGY", selected_strategy), _section("RELEVANT_TACTICS", relevant_tactics),
    ])


def generate_deal_manager_full_script(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_strategy = str(kwargs["selected_strategy"])
    script_mode = str(kwargs.get("script_mode") or "message")
    if script_mode not in SCRIPT_MODES:
        raise ValueError("Неизвестный режим сценария")
    relevant_tactics = kwargs.get("relevant_tactics")
    allowed_tactic_ids = {
        str(item.get("tactic_id") or "").strip()
        for item in relevant_tactics if isinstance(item, dict)
    } if isinstance(relevant_tactics, list) else set()
    prompt = build_full_script_prompt(**kwargs)
    result, metadata = call_structured_output_json(
        prompt, schema=full_script_schema(), schema_name="deal_manager_full_script", model=MANAGER_MODEL,
        reasoning_effort=MANAGER_REASONING_EFFORT, max_output_tokens=MAX_FULL_SCRIPT_OUTPUT_TOKENS,
        log_title="deal manager full script prompt", call_type=f"deal_manager_full_script_{script_mode}",
        prompt_cache_key=f"neuro-rop:deal-manager-full-script:{script_mode}:v2",
        stable_prefix=prompt_prefix_before(prompt, "QUICK_HELP:"),
    )
    return validate_full_script(result, selected_strategy=selected_strategy, allowed_tactic_ids=allowed_tactic_ids), metadata
