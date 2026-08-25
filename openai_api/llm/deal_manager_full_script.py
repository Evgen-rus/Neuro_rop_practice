"""Strict on-demand conversation script built on the existing manager context."""

from __future__ import annotations

import json
from typing import Any

from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT, project_bitrix_task, project_deal
from openai_api.llm.llm_client import call_structured_output_json, deal_trace_id, prompt_prefix_before
from openai_api.llm.deal_manager_quick_help import (
    project_locked_move,
    project_quick_help_for_material,
)
from openai_api.llm.prompt_parts import assemble_prompt, static_prompt_from_full


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
            "- Первый блок — полноценное открытие разговора. В spoken_text первого блока соедини: приветствие, зачем звонок, какую пользу клиент получит от разговора, запрос готовности и первый рабочий вопрос. Не начинай сразу со второго, предметного вопроса.\n"
            "- Каждый вопрос должен сначала заслужить право быть заданным. Перед существенным вопросом дай короткую связку: зачем ты это спрашиваешь и какую пользу клиент получит от ответа. Это может быть не продажа продукта, а польза самого вопроса: не ошибиться с комплектацией, сравнивать варианты по одним критериям, не возвращаться к теме после договора, подготовить расчёт. Связка обязательна, даже если психотип прямой.\n"
            "- Один блок = одна основная реплика. spoken_text — готовый кусок речи, который менеджер реально говорит: ценность/причина и один главный вопрос в той же реплике. Не выноси вопрос в отдельное поле и не давай 2–3 равноправных варианта текста.\n"
            "- clarifying_question не показывается менеджеру. Всегда верни пустую строку. Не используй это поле как второй пузырь, запасной вопрос или продолжение spoken_text.\n"
            "- DISC может менять длину, темп, прямоту, детализацию и порядок аргументов. Он не отменяет человеческую связку. Даже для прямого D-клиента нужна короткая причина вопроса, а не голые вопросы подряд. Жёсткий допрос не подходит никому.\n"
            "- Готовое сообщение из LOCKED_MOVE ещё не сказано по телефону. Первый блок — устная версия того же захода: тот же смысл, тот же рычаг, та же ближайшая цель. Не начинай «с середины», как будто письменное сообщение уже состоялось, и не подменяй его другим человеком.\n"
            "- Не копируй письменный текст дословно, если он звучит как переписка: адаптируй в живую устную речь, сохранив личность, аргумент и CTA.\n"
            "- Если ANALYSIS_CONTEXT.client_communication_profile имеет status tentative или supported, адаптируй устную речь под primary_style, secondary_style, profile_confidence и recommended_communication: темп, прямоту, длину фраз, порядок аргументов и способ задавать вопросы. Не объясняй DISC менеджеру и не меняй факты. При insufficient_evidence используй нейтральный деловой стиль.\n"
            "- Перед финальным резюме проверь один главный скрытый стоп-фактор, который реально может остановить ближайший денежный шаг. Не собирай все риски подряд.\n"
            "- OBJECTION_HANDLING содержит готовые возражения из полного анализа. Не придумывай и не переписывай их: укажи в relevant_objection_ids блока не более двух существующих objection_id, только если они уместны в этой точке разговора.\n"
        )
        shape_rules = (
            "- Сделай 3–6 диалоговых блоков по существующим смысловым темам, а не текст для чтения целиком и не анкету.\n"
            "- В каждом блоке укажи цель, одну готовую реплику spoken_text, что услышать в ответе и переход дальше.\n"
        )
        closing_rule = (
            "- closing_agreement — финальное резюме всего разговора, а не новый допрос и не выдуманные будущие договорённости. "
            "Дай менеджеру готовую механику: коротко перечислить только то, что клиент реально подтвердит в этом звонке; проговорить согласованный следующий шаг из текущего контекста; спросить, всё ли верно зафиксировано. "
            "Не заполняй резюме фактами, суммами, датами или согласиями, которых ещё нет. Верни только JSON по схеме на русском языке."
        )
    else:
        mode_rules = (
            "- Это сценарий продолжения переписки. Давай короткие готовые сообщения и ветки ответа; не превращай его в телефонный разговор.\n"
            "- Раскрой выбранный LOCKED_MOVE в переписке: первое сообщение — сам SELECTED_CLIENT_MESSAGE или его естественное продолжение в том же голосе. Не начинай новую стратегию.\n"
            "- Если ANALYSIS_CONTEXT.client_communication_profile имеет status tentative или supported, адаптируй длину, прямоту и структуру сообщений под сохранённый профиль. При insufficient_evidence используй нейтральный деловой стиль.\n"
        )
        shape_rules = (
            "- Сделай 3–6 коротких диалоговых блоков, а не текст для чтения целиком и не список из двадцати вопросов.\n"
            "- В каждом блоке укажи цель, 1–3 естественные фразы, что услышать в ответе и переход дальше.\n"
        )
        closing_rule = "- Заверши разговор одной конкретной проверяемой договорённостью. Верни только JSON по схеме на русском языке."
    locked_move = project_locked_move(quick_help, selected_strategy)
    return "\n\n".join([
        "SYSTEM_RULES:\nТы — прикладной помощник менеджера во время реального разговора по одной сделке. Полный анализ уже выполнен: не анализируй сделку заново.",
        "RULES:\n"
        + mode_rules +
        "- LOCKED_MOVE — уже выбранный ход Дожима или Реаниматора. Сценарий должен звучать как тот же человек: тот же тон, тот же рычаг, та же ближайшая цель. Не изобретай новую стратегию и не пересобирай ситуацию заново из сырого анализа, если LOCKED_MOVE её уже зафиксировал.\n"
        "- Опирайся на selected_client_message как на эталон захода. Не смешивай с невыбранными вариантами. Другие client_messages в исходном Quick Help сюда не передаются специально.\n"
        "- Продолжай ровно выбранный менеджером вариант сообщения. Используй LOCKED_MOVE, SELECTED_STRATEGY, ASSISTANT_MODE и pressure_lever.\n"
        "- Если ASSISTANT_MODE = push, держи экспертный коммерческий тон выбранного дожима: уверенно, предметно, через выбранный рычаг, без канцелярита и без пустого «посмотрели КП?». Если ASSISTANT_MODE = reanimator, держи мягкое восстановление контакта: приветствие, при необходимости кто мы и на чём остановились, польза ответить, низкое усилие, один следующий шаг. Не смешивай эти голоса.\n"
        + shape_rules +
        "- Незакрытые пункты CURRENT_DAILY_CHECKLIST помоги получить естественно, но не создавай новый checklist и не объявляй отметки менеджера фактами клиента.\n"
        "- RELEVANT_TACTICS — допустимые условные приёмы. Не обещай их доступность и не превращай в факт без подтверждения контекстом.\n"
        "- Не выполняй новый анализ возражений и не генерируй новые ответы: UI покажет готовую проекцию полного анализа отдельно.\n"
        "- Не придумывай даты, суммы, наличие оборудования, специалистов, рассрочки, повышение цен, договорённости или слова клиента.\n"
        + closing_rule,
        _section("ANALYSIS_CONTEXT", analysis_projection), _section("SITUATION_CONTEXT", situation_projection),
        _section("DEAL_CONTEXT", project_deal(deal)), _section("CURRENT_BITRIX_TASK", project_bitrix_task(current_bitrix_task)),
        _section("CURRENT_DAILY_CHECKLIST", checklist), _section("COMMUNICATION_PATTERN_CONTEXT", communication_pattern_context),
        _section("SCRIPT_MODE", script_mode),
        _section("LOCKED_MOVE", locked_move),
        _section("QUICK_HELP", project_quick_help_for_material(quick_help, selected_strategy)),
        _section("SELECTED_STRATEGY", selected_strategy),
        _section("ASSISTANT_MODE", locked_move["mode"]),
        _section("PRESSURE_LEVER", locked_move["pressure_lever"]),
        _section("RELEVANT_TACTICS", relevant_tactics),
        _section("OBJECTION_HANDLING", objection_handling or {"items": []}),
    ])


def full_script_static_prompt(script_mode: str = "message") -> str:
    dummy_help = {
        "mode": "push",
        "situation_summary": "x",
        "next_action": "x",
        "expected_result": "x",
        "pressure_lever": {"title": "x", "rationale": "x"},
        "strategy_labels": {"primary": "a", "alternative": "b", "pattern_break": "c"},
        "client_messages": {"primary": "m", "alternative": "m2", "pattern_break": "m3"},
        "lifehacks": [],
        "fallback_action": "f",
    }
    full = build_full_script_prompt(
        analysis_projection={},
        situation_projection={},
        deal={},
        current_bitrix_task=None,
        checklist={},
        communication_pattern_context={},
        quick_help=dummy_help,
        selected_strategy="primary",
        relevant_tactics=[],
        script_mode=script_mode,
        objection_handling={"items": []},
    )
    return static_prompt_from_full(full, "ANALYSIS_CONTEXT:")


def assemble_full_script_prompt(*, prompt_template: str, **kwargs: Any) -> str:
    full = build_full_script_prompt(**kwargs)
    return assemble_prompt(prompt_template, [full[len(static_prompt_from_full(full, "ANALYSIS_CONTEXT:")):].lstrip()])


def generate_deal_manager_full_script(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_strategy = str(kwargs["selected_strategy"])
    script_mode = str(kwargs.get("script_mode") or "message")
    if script_mode not in SCRIPT_MODES:
        raise ValueError("Неизвестный режим сценария")
    prompt_template = kwargs.pop("prompt_template", None)
    model = kwargs.pop("model", None) or MANAGER_MODEL
    reasoning_effort = kwargs.pop("reasoning_effort", None) or MANAGER_REASONING_EFFORT
    call_type = kwargs.pop("call_type", None) or f"deal_manager_full_script_{script_mode}"
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
    prompt = assemble_full_script_prompt(prompt_template=prompt_template, **kwargs) if prompt_template else build_full_script_prompt(**kwargs)
    cache_version = "v5" if script_mode == "call" else "v4"
    result, metadata = call_structured_output_json(
        prompt, schema=full_script_schema(script_mode), schema_name="deal_manager_full_script", model=model,
        reasoning_effort=reasoning_effort, max_output_tokens=MAX_FULL_SCRIPT_OUTPUT_TOKENS,
        log_title="deal manager full script prompt", call_type=call_type,
        prompt_cache_key=f"neuro-rop:deal-manager-full-script:{script_mode}:{cache_version}",
        stable_prefix=prompt_prefix_before(prompt, "LOCKED_MOVE:"),
        trace_entity_type="deal",
        trace_entity_id=deal_trace_id(kwargs.get("deal")),
    )
    return validate_full_script(
        result, selected_strategy=selected_strategy, script_mode=script_mode,
        allowed_tactic_ids=allowed_tactic_ids, allowed_objection_ids=allowed_objection_ids,
    ), metadata
