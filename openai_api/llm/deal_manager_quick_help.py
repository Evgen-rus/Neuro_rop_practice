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
from openai_api.llm.llm_client import call_structured_output_json, prompt_prefix_before


MAX_QUICK_HELP_OUTPUT_TOKENS = 3000
_LIST_FIELDS = ("crm_checklist",)
_REQUIRED_FIELDS = (
    "answer_contract",
    "situation_summary",
    "next_action",
    "expected_result",
    "client_messages",
    "call_scripts",
    "recommended_strategy",
    "recommended_channel",
    "fallback_action",
    "crm_checklist",
)

_STRATEGIES = ("primary", "alternative", "pattern_break")
_RECOMMENDED_CHANNELS = ("message", "call")
_ANSWER_CONTRACT = "strategy_v1"


def _short_list_schema(max_items: int = 4, max_length: int = 800) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": max_length},
        "maxItems": max_items,
    }


def quick_help_schema() -> dict[str, Any]:
    def strategy_variants(max_length: int) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(_STRATEGIES),
            "properties": {
                strategy: {"type": "string", "minLength": 1, "maxLength": max_length}
                for strategy in _STRATEGIES
            },
        }

    fields = {
        "answer_contract": {"type": "string", "enum": [_ANSWER_CONTRACT]},
        "situation_summary": {"type": "string", "minLength": 1, "maxLength": 420},
        "next_action": {"type": "string", "minLength": 1, "maxLength": 420},
        "expected_result": {"type": "string", "minLength": 1, "maxLength": 320},
        "client_messages": strategy_variants(1200),
        "call_scripts": strategy_variants(1200),
        "recommended_strategy": {"type": "string", "enum": list(_STRATEGIES)},
        "recommended_channel": {"type": "string", "enum": list(_RECOMMENDED_CHANNELS)},
        "fallback_action": {"type": "string", "minLength": 1, "maxLength": 420},
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
    if value.get("answer_contract") != _ANSWER_CONTRACT:
        raise ValueError("answer_contract содержит неподдерживаемую версию")
    normalized["answer_contract"] = _ANSWER_CONTRACT
    text_limits = {
        "situation_summary": 420,
        "next_action": 420,
        "expected_result": 320,
        "fallback_action": 420,
    }
    for field, max_length in text_limits.items():
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError("В quick help есть пустое текстовое поле")
        if "\n" in item:
            raise ValueError(f"{field} должен быть одной короткой фразой без списка")
        normalized[field] = item.strip()[:max_length]
    for field in ("client_messages", "call_scripts"):
        variants = value.get(field)
        if not isinstance(variants, dict) or set(variants) != set(_STRATEGIES):
            raise ValueError(f"{field} должен содержать все допустимые стратегии")
        if any(not isinstance(variants[strategy], str) or not variants[strategy].strip() for strategy in _STRATEGIES):
            raise ValueError(f"{field} должен содержать только непустые тексты")
        texts = [variants[strategy].strip() for strategy in _STRATEGIES]
        if len({text.casefold() for text in texts}) != len(_STRATEGIES):
            raise ValueError(f"{field} должен содержать разные варианты")
        normalized[field] = {
            strategy: variants[strategy].strip()[:1200]
            for strategy in _STRATEGIES
        }
    strategy = value.get("recommended_strategy")
    if strategy not in _STRATEGIES:
        raise ValueError("recommended_strategy содержит недопустимую стратегию")
    normalized["recommended_strategy"] = strategy
    channel = value.get("recommended_channel")
    if channel not in _RECOMMENDED_CHANNELS:
        raise ValueError("recommended_channel содержит недопустимый канал")
    normalized["recommended_channel"] = channel
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
    communication_pattern_context: dict[str, Any],
) -> str:
    question = str(question or "").strip()[:MAX_MANAGER_CONTEXT_CHARS]
    return "\n\n".join(
        [
            "SYSTEM_RULES:\nТы — прикладной sales-assistant менеджера по одной текущей сделке. Твоя цель — помочь получить ближайший подтверждаемый шаг клиента, который продвигает сделку к деньгам.",
            "RULES:\n"
            "- Отвечай на конкретный вопрос менеджера. Внутренне определи стадию сделки, подтверждённые клиентом факты, blocker и нужную micro-conversion; внутреннее рассуждение не показывай.\n"
            "- Опирайся только на переданные CONTEXT и не придумывай ответ клиента, факты, даты, суммы или договорённости.\n"
            "- Не называй попытку звонка контактом. CRM-задача — поручение, а не доказательство клиентского результата.\n"
            "- COMMUNICATION_PATTERN_CONTEXT содержит только детерминированный срез коммуникаций без текстов и транскриптов. По нему можно утверждать повторение канала или попыток без подтверждённого контакта, но нельзя утверждать повторение одинакового CTA.\n"
            "- Если один канал или способ контакта несколько раз не дал подтверждённого результата, не рекомендуй просто повторить его: измени хотя бы канал, повод, CTA, требуемое усилие клиента, аргумент или допустимого участника сделки. Не придумывай новых лиц.\n"
            "- Не откатывай сделку к общей квалификации, если она уже дошла до КП, договора, счёта или оплаты и нет конкретной причины возвращаться назад. Убирай текущий blocker.\n"
            "- Блок «Понял ситуацию» строится строго как: situation_summary → next_action → expected_result.\n"
            "- next_action — одно максимально конкретное внешнее действие менеджера. Не давай списков, общих советов, внутренних действий вроде «проверь CRM» и не повторяй видимый контекст.\n"
            "- У next_action и каждого предлагаемого касания должна быть одна главная ближайшая micro-conversion — один подтверждаемый результат клиента, который реально открывает следующий шаг сделки. expected_result называет именно этот результат, а не действие менеджера.\n"
            "- Не объединяй в одном касании несколько независимых решений или обязательств клиента. Несколько связанных вопросов допустимы, только если они нужны для одной общей micro-conversion, например для получения исходных данных технического расчёта или подтверждения статуса согласования.\n"
            "- Соразмеряй усилие клиента с ситуацией: при молчании, низкой вовлечённости или повторных безрезультатных касаниях снижай сложность ответа; при активном техническом обсуждении допускай подробную коммуникацию, если она снимает текущий blocker.\n"
            "- Для каждого рекомендуемого касания внутренне определи, зачем клиенту отвечать или выполнять следующий шаг. Если контекст подтверждает конкретную ценность, кратко отрази её в формулировке: снижение риска или неопределённости, предотвращение переделки, получение корректного расчёта, упрощение согласования или продвижение уже согласованного процесса. Не придумывай выгоду и не превращай каждое касание в презентацию продукта. Если ценность нельзя обосновать контекстом, используй нейтральное объяснение цели или короткий вопрос с низким усилием ответа.\n"
            "- client_messages и call_scripts содержат три реально разные стратегии: primary — лучший ход сейчас; alternative — другой аргумент, вопрос, CTA или акцент к той же цели; pattern_break — смена неработающей механики, а при отсутствии паттерна более решительный недублирующий способ получить обязательство.\n"
            "- client_messages — готовые сообщения без плейсхолдеров. call_scripts — короткие готовые фразы, которые можно произнести буквально, а не подробные сценарии.\n"
            "- recommended_strategy выбери один раз для recommended_channel; next_action должен им соответствовать. Другой канал остаётся запасным и не считается одновременно рекомендованным.\n"
            "- fallback_action — одно конкретное действие с одной micro-conversion. По возможности сохраняй ближайшую цель, но меняй способ её достижения и не делай fallback сложнее основного касания. Меняй цель только если переданный контекст показывает, что она неактуальна, недостижима или перед ней есть более ранний blocker. Не придумывай срок или число попыток. При явном отказе или просьбе не связываться fallback может быть внутренним действием.\n"
            "- Если ANALYSIS_CONTEXT содержит client_communication_profile со status tentative или supported, используй primary_style, secondary_style, profile_confidence и recommended_communication, чтобы адаптировать длину, прямоту, порядок аргументов, детализацию, акценты и CTA каждой стратегии. Не объясняй DISC менеджеру и не меняй факты, цель или следующий шаг. При insufficient_evidence используй нейтральный деловой стиль.\n"
            "- Если данных недостаточно, не маскируй предположение под факт: сформулируй безопасный вопрос клиенту на уточнение.\n"
            "- Не придумывай скидки, дедлайны, суммы, обещания, имена, решения клиента, договорённости или искусственный дефицит.\n"
            "- crm_checklist — только то, что менеджер должен зафиксировать после фактического действия.\n"
            "- Верни только полный объект по JSON-схеме, спокойно, эмпатично, прямо и по-русски.\n"
            "- Этот запрос независим: не используй и не запрашивай историю прошлых quick help.",
            _section("SITUATION_CONTEXT", situation_projection),
            _section("ANALYSIS_CONTEXT", analysis_projection),
            _section("DEAL_CONTEXT", project_deal(deal)),
            _section("CURRENT_BITRIX_TASK", project_bitrix_task(current_bitrix_task)),
            _section("COMMUNICATION_PATTERN_CONTEXT", communication_pattern_context),
            _section("MANAGER_QUESTION", question),
        ]
    )


def generate_deal_manager_quick_help(
    *,
    question: str,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    situation_projection: dict[str, Any],
    communication_pattern_context: dict[str, Any],
    model: str = MANAGER_MODEL,
    reasoning_effort: str = MANAGER_REASONING_EFFORT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = build_quick_help_prompt(
        question=question,
        analysis_projection=analysis_projection,
        deal=deal,
        current_bitrix_task=current_bitrix_task,
        situation_projection=situation_projection,
        communication_pattern_context=communication_pattern_context,
    )
    result, metadata = call_structured_output_json(
        prompt,
        schema=quick_help_schema(),
        schema_name="deal_manager_quick_help",
        model=model,
        reasoning_effort=reasoning_effort,
        max_output_tokens=MAX_QUICK_HELP_OUTPUT_TOKENS,
        log_title="deal manager quick help prompt",
        call_type="deal_manager_quick_help",
        prompt_cache_key="neuro-rop:deal-manager-quick-help:v3",
        stable_prefix=prompt_prefix_before(prompt, "MANAGER_QUESTION:"),
    )
    return validate_quick_help(result), metadata

