"""Strict on-demand conversation script built on the existing manager context."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT, project_bitrix_task, project_deal
from openai_api.llm.llm_client import call_structured_output_json, prompt_prefix_before


MAX_FULL_SCRIPT_OUTPUT_TOKENS = 6000
SCRIPT_CONTRACT = "conversation_script_v1"
CALL_SCRIPT_CONTRACT = "conversation_script_v2"
STRATEGIES = ("primary", "alternative", "pattern_break")
SCRIPT_MODES = ("message", "call", "email")


def _script_contract_for_mode(script_mode: str) -> str:
    return CALL_SCRIPT_CONTRACT if script_mode == "call" else SCRIPT_CONTRACT


def full_script_schema(script_mode: str = "message") -> dict[str, Any]:
    short_list = {"type": "array", "maxItems": 3, "items": {"type": "string", "minLength": 1, "maxLength": 500}}
    is_call = script_mode == "call"
    block_properties = {
        "block_id": {"type": "string", "minLength": 1, "maxLength": 80},
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "objective": {"type": "string", "minLength": 1, "maxLength": 420},
        "listen_for": short_list,
        "transition": {"type": "string", "minLength": 1, "maxLength": 420},
        "relevant_objection_ids": {"type": "array", "maxItems": 2, "items": {"type": "string", "minLength": 1, "maxLength": 80}},
    }
    if is_call:
        block_properties["spoken_text"] = {"type": "string", "minLength": 1, "maxLength": 1000}
        block_properties["clarifying_question"] = {"type": "string", "maxLength": 420}
        block_required = ["block_id", "title", "objective", "spoken_text", "clarifying_question", "listen_for", "transition", "relevant_objection_ids"]
    else:
        block_properties["suggested_phrases"] = short_list
        block_required = ["block_id", "title", "objective", "suggested_phrases", "listen_for", "transition", "relevant_objection_ids"]
    block = {
        "type": "object",
        "additionalProperties": False,
        "required": block_required,
        "properties": block_properties,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["script_contract", "selected_strategy", "conversation_goal", "blocks", "closing_agreement", "relevant_tactic_ids"],
        "properties": {
            "script_contract": {"type": "string", "enum": [_script_contract_for_mode(script_mode)]},
            "selected_strategy": {"type": "string", "enum": list(STRATEGIES)},
            "conversation_goal": {"type": "string", "minLength": 1, "maxLength": 420},
            "blocks": {"type": "array", "minItems": 3, "maxItems": 6, "items": block},
            "closing_agreement": {"type": "string", "minLength": 1, "maxLength": 900 if is_call else 500},
            "relevant_tactic_ids": {"type": "array", "maxItems": 3, "items": {"type": "string", "minLength": 1, "maxLength": 80}},
        },
    }


def _normalize_block_objections(item: dict[str, Any], *, allowed_objection_ids: set[str] | None) -> list[str]:
    objection_ids = item.get("relevant_objection_ids")
    if not isinstance(objection_ids, list) or len(objection_ids) > 2 or any(not isinstance(text, str) or not text.strip() for text in objection_ids):
        raise ValueError("Связь блока с возражениями заполнена некорректно")
    normalized_objection_ids = [text.strip()[:80] for text in objection_ids]
    if len(set(normalized_objection_ids)) != len(normalized_objection_ids):
        raise ValueError("Блок содержит повторяющееся возражение")
    if allowed_objection_ids is not None and any(text not in allowed_objection_ids for text in normalized_objection_ids):
        raise ValueError("Блок ссылается на неизвестное возражение")
    return normalized_objection_ids


def validate_full_script(
    value: Any,
    *,
    selected_strategy: str,
    script_mode: str = "message",
    allowed_tactic_ids: set[str] | None = None,
    allowed_objection_ids: set[str] | None = None,
) -> dict[str, Any]:
    if script_mode not in SCRIPT_MODES:
        raise ValueError("Неизвестный режим сценария")
    expected_contract = _script_contract_for_mode(script_mode)
    if not isinstance(value, dict) or value.get("script_contract") != expected_contract:
        raise ValueError("Полный скрипт имеет неподдерживаемый контракт")
    if selected_strategy not in STRATEGIES or value.get("selected_strategy") != selected_strategy:
        raise ValueError("Полный скрипт не соответствует выбранной стратегии")
    is_call = script_mode == "call"
    goal = value.get("conversation_goal")
    closing = value.get("closing_agreement")
    blocks = value.get("blocks")
    tactic_ids = value.get("relevant_tactic_ids")
    closing_limit = 900 if is_call else 500
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
        listen_for = item.get("listen_for")
        if not block_id or block_id in seen_ids or not title or not objective or not transition:
            raise ValueError("Блок полного скрипта заполнен некорректно")
        if not isinstance(listen_for, list) or len(listen_for) > 3:
            raise ValueError("Фразы или ориентиры блока заполнены некорректно")
        if any(not isinstance(text, str) or not text.strip() for text in listen_for):
            raise ValueError("Фразы блока должны быть непустыми строками")
        normalized_objection_ids = _normalize_block_objections(item, allowed_objection_ids=allowed_objection_ids)
        normalized_block = {
            "block_id": block_id[:80], "title": title[:160], "objective": objective[:420],
            "listen_for": [text.strip()[:500] for text in listen_for], "transition": transition[:420],
            "relevant_objection_ids": normalized_objection_ids,
        }
        if is_call:
            spoken_text = item.get("spoken_text")
            clarifying_question = item.get("clarifying_question")
            if not isinstance(spoken_text, str) or not spoken_text.strip():
                raise ValueError("Блок звонка должен содержать одну готовую реплику")
            if clarifying_question is None:
                clarifying_question = ""
            if not isinstance(clarifying_question, str):
                raise ValueError("Уточняющий вопрос блока заполнен некорректно")
            normalized_block["spoken_text"] = spoken_text.strip()[:1000]
            normalized_block["clarifying_question"] = clarifying_question.strip()[:420]
        else:
            phrases = item.get("suggested_phrases")
            if not isinstance(phrases, list) or not 1 <= len(phrases) <= 3:
                raise ValueError("Фразы или ориентиры блока заполнены некорректно")
            if any(not isinstance(text, str) or not text.strip() for text in phrases):
                raise ValueError("Фразы блока должны быть непустыми строками")
            normalized_block["suggested_phrases"] = [text.strip()[:500] for text in phrases]
        seen_ids.add(block_id)
        normalized_blocks.append(normalized_block)
    if not isinstance(tactic_ids, list) or len(tactic_ids) > 3 or any(not isinstance(item, str) or not item.strip() for item in tactic_ids):
        raise ValueError("relevant_tactic_ids заполнен некорректно")
    if allowed_tactic_ids is not None and any(item.strip() not in allowed_tactic_ids for item in tactic_ids):
        raise ValueError("relevant_tactic_ids содержит тактику вне Quick Help")
    return {
        "script_contract": expected_contract,
        "selected_strategy": selected_strategy,
        "conversation_goal": goal.strip()[:420],
        "blocks": normalized_blocks,
        "closing_agreement": closing.strip()[:closing_limit],
        "relevant_tactic_ids": [item.strip()[:80] for item in tactic_ids],
    }


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def build_full_script_prompt(*, analysis_projection: dict[str, Any], situation_projection: dict[str, Any], deal: dict[str, Any], current_bitrix_task: dict[str, Any] | None, checklist: dict[str, Any], communication_pattern_context: dict[str, Any], quick_help: dict[str, Any], selected_strategy: str, relevant_tactics: list[dict[str, Any]], script_mode: str = "message", objection_handling: dict[str, Any] | None = None) -> str:
    if script_mode not in SCRIPT_MODES:
        raise ValueError("Неизвестный режим сценария")
    if script_mode == "call":
        mode_rules = (
            "- Это сценарий именно телефонного звонка. Не предлагай переписку, мессенджер или отправку сообщения как основной блок.\n"
            "- Смысловые блоки и их набор не меняй: не выкидывай работу с возражениями и не добавляй новые искусственные этапы. Меняй только форму живого разговора.\n"
            "- Первый блок — полноценное открытие разговора. В spoken_text первого блока соедини: приветствие, зачем звонок, какую пользу клиент получит от разговора, запрос готовности и только затем первый рабочий вопрос. Не начинай сразу со второго, предметного вопроса.\n"
            "- Каждый вопрос должен сначала заслужить право быть заданным. Перед существенным вопросом дай короткую связку: зачем ты это спрашиваешь и какую пользу клиент получит от ответа. Это может быть не продажа продукта, а польза самого вопроса: не ошибиться с комплектацией, сравнивать варианты по одним критериям, не возвращаться к теме после договора, подготовить расчёт. Связка обязательна, даже если психотип прямой.\n"
            "- Один блок = одна основная реплика. spoken_text — готовый кусок речи, который менеджер реально говорит: ценность/причина + один главный вопрос. Не давай 2–3 равноправных варианта текста и не превращай блок в допрос из нескольких вопросов подряд.\n"
            "- clarifying_question — не второй основной текст. Заполняй его только если ответ клиента может потребовать одно уточнение внутри того же блока; иначе верни пустую строку. Это не отдельный этап разговора.\n"
            "- DISC может менять длину, темп, прямоту, детализацию и порядок аргументов. Он не отменяет человеческую связку. Даже для прямого D-клиента нужна короткая причина вопроса, а не голые вопросы подряд. Жёсткий допрос не подходит никому.\n"
            "- QUICK_HELP — это уже сделанный заход к клиенту. Не дублируй его текст: используй выбранную стратегию как основание звонка и продолжай с текущей точки.\n"
            "- Если ANALYSIS_CONTEXT.client_communication_profile имеет status tentative или supported, адаптируй устную речь под primary_style, secondary_style, profile_confidence и recommended_communication: темп, прямоту, длину фраз, порядок аргументов и способ задавать вопросы. Не объясняй DISC менеджеру и не меняй факты. При insufficient_evidence используй нейтральный деловой стиль.\n"
            "- Перед финальным резюме проверь один главный скрытый стоп-фактор, который реально может остановить ближайший денежный шаг. Не собирай все риски подряд.\n"
            "- OBJECTION_HANDLING содержит готовые возражения из полного анализа. Не придумывай и не переписывай их: укажи в relevant_objection_ids блока не более двух существующих objection_id, только если они уместны в этой точке разговора.\n"
        )
        shape_rules = (
            "- Сделай 3–6 диалоговых блоков по существующим смысловым темам, а не текст для чтения целиком и не анкету.\n"
            "- В каждом блоке укажи цель, одну готовую реплику spoken_text, при необходимости один clarifying_question, что услышать в ответе и переход дальше.\n"
        )
        closing_rule = (
            "- closing_agreement — финальное резюме всего разговора, а не новый допрос и не выдуманные будущие договорённости. "
            "Дай менеджеру готовую механику: коротко перечислить только то, что клиент реально подтвердит в этом звонке; проговорить согласованный следующий шаг из текущего контекста; спросить, всё ли верно зафиксировано. "
            "Не заполняй резюме фактами, суммами, датами или согласиями, которых ещё нет. Верни только JSON по схеме на русском языке."
        )
    else:
        mode_rules = (
            "- Это сценарий продолжения переписки. Давай короткие готовые сообщения и ветки ответа; не превращай его в телефонный разговор.\n"
            "- Если ANALYSIS_CONTEXT.client_communication_profile имеет status tentative или supported, адаптируй длину, прямоту и структуру сообщений под сохранённый профиль. При insufficient_evidence используй нейтральный деловой стиль.\n"
        )
        shape_rules = (
            "- Сделай 3–6 коротких диалоговых блоков, а не текст для чтения целиком и не список из двадцати вопросов.\n"
            "- В каждом блоке укажи цель, 1–3 естественные фразы, что услышать в ответе и переход дальше.\n"
        )
        closing_rule = "- Заверши разговор одной конкретной проверяемой договорённостью. Верни только JSON по схеме на русском языке."
    return "\n\n".join([
        "SYSTEM_RULES:\nТы — прикладной помощник менеджера во время реального разговора по одной сделке. Полный анализ уже выполнен: не анализируй сделку заново.",
        "RULES:\n"
        + mode_rules +
        "- Продолжай ровно выбранный менеджером вариант сообщения. Используй SELECTED_STRATEGY, ASSISTANT_MODE и pressure_lever из QUICK_HELP, если он есть.\n"
        "- Если ASSISTANT_MODE = push, держи экспертный коммерческий тон выбранного дожима и опирайся на выбранный рычаг. Если ASSISTANT_MODE = reanimator, держи мягкое восстановление контакта: приветствие, при необходимости кто мы и на чём остановились, польза ответить, один следующий шаг. Не смешивай эти голоса.\n"
        + shape_rules +
        "- Незакрытые пункты CURRENT_DAILY_CHECKLIST помоги получить естественно, но не создавай новый checklist и не объявляй отметки менеджера фактами клиента.\n"
        "- RELEVANT_TACTICS — допустимые условные приёмы. Не обещай их доступность и не превращай в факт без подтверждения контекстом.\n"
        "- Не выполняй новый анализ возражений и не генерируй новые ответы: UI покажет готовую проекцию полного анализа отдельно.\n"
        "- Не придумывай даты, суммы, наличие оборудования, специалистов, рассрочки, повышение цен, договорённости или слова клиента.\n"
        + closing_rule,
        _section("ANALYSIS_CONTEXT", analysis_projection), _section("SITUATION_CONTEXT", situation_projection),
        _section("DEAL_CONTEXT", project_deal(deal)), _section("CURRENT_BITRIX_TASK", project_bitrix_task(current_bitrix_task)),
        _section("CURRENT_DAILY_CHECKLIST", checklist), _section("COMMUNICATION_PATTERN_CONTEXT", communication_pattern_context),
        _section("SCRIPT_MODE", script_mode), _section("QUICK_HELP", quick_help), _section("SELECTED_STRATEGY", selected_strategy), _section("ASSISTANT_MODE", str(quick_help.get("mode") or "reanimator") if isinstance(quick_help, dict) else "reanimator"), _section("PRESSURE_LEVER", quick_help.get("pressure_lever") if isinstance(quick_help, dict) else None), _section("RELEVANT_TACTICS", relevant_tactics), _section("OBJECTION_HANDLING", objection_handling or {"items": []}),
    ])


def generate_deal_manager_full_script(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_strategy = str(kwargs["selected_strategy"])
    script_mode = str(kwargs.get("script_mode") or "message")
    if script_mode not in SCRIPT_MODES:
        raise ValueError("Неизвестный режим сценария")
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
    prompt = build_full_script_prompt(**kwargs)
    cache_version = "v4" if script_mode == "call" else "v3"
    result, metadata = call_structured_output_json(
        prompt, schema=full_script_schema(script_mode), schema_name="deal_manager_full_script", model=MANAGER_MODEL,
        reasoning_effort=MANAGER_REASONING_EFFORT, max_output_tokens=MAX_FULL_SCRIPT_OUTPUT_TOKENS,
        log_title="deal manager full script prompt", call_type=f"deal_manager_full_script_{script_mode}",
        prompt_cache_key=f"neuro-rop:deal-manager-full-script:{script_mode}:{cache_version}",
        stable_prefix=prompt_prefix_before(prompt, "QUICK_HELP:"),
    )
    return validate_full_script(
        result, selected_strategy=selected_strategy, script_mode=script_mode,
        allowed_tactic_ids=allowed_tactic_ids, allowed_objection_ids=allowed_objection_ids,
    ), metadata
