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
    "situation_summary",
    "next_action",
    "expected_result",
    "client_messages",
    "recommended_client_tone",
    "call_scripts",
    "recommended_call_tone",
    "crm_checklist",
)

_CLIENT_TONES = ("calm", "confident", "direct")
_CALL_TONES = ("soft", "business", "direct")


def _short_list_schema(max_items: int = 4, max_length: int = 800) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": max_length},
        "maxItems": max_items,
    }


def quick_help_schema() -> dict[str, Any]:
    def tone_variants(tones: tuple[str, ...], max_length: int) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(tones),
            "properties": {
                tone: {"type": "string", "minLength": 1, "maxLength": max_length}
                for tone in tones
            },
        }

    fields = {
        "situation_summary": {"type": "string", "minLength": 1, "maxLength": 420},
        "next_action": {"type": "string", "minLength": 1, "maxLength": 420},
        "expected_result": {"type": "string", "minLength": 1, "maxLength": 320},
        "client_messages": tone_variants(_CLIENT_TONES, 1200),
        "recommended_client_tone": {"type": "string", "enum": list(_CLIENT_TONES)},
        "call_scripts": tone_variants(_CALL_TONES, 1200),
        "recommended_call_tone": {"type": "string", "enum": list(_CALL_TONES)},
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
    text_limits = {"situation_summary": 420, "next_action": 420, "expected_result": 320}
    for field, max_length in text_limits.items():
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError("В quick help есть пустое текстовое поле")
        if "\n" in item:
            raise ValueError(f"{field} должен быть одной короткой фразой без списка")
        normalized[field] = item.strip()[:max_length]
    for field, tones in (("client_messages", _CLIENT_TONES), ("call_scripts", _CALL_TONES)):
        variants = value.get(field)
        if not isinstance(variants, dict) or set(variants) != set(tones):
            raise ValueError(f"{field} должен содержать все допустимые тона")
        if any(not isinstance(variants[tone], str) or not variants[tone].strip() for tone in tones):
            raise ValueError(f"{field} должен содержать только непустые тексты")
        texts = [variants[tone].strip() for tone in tones]
        if len({text.casefold() for text in texts}) != len(tones):
            raise ValueError(f"{field} должен содержать разные варианты")
        normalized[field] = {tone: variants[tone].strip()[:1200] for tone in tones}
    for field, tones in (("recommended_client_tone", _CLIENT_TONES), ("recommended_call_tone", _CALL_TONES)):
        tone = value.get(field)
        if tone not in tones:
            raise ValueError(f"{field} содержит недопустимый тон")
        normalized[field] = tone
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
) -> str:
    question = str(question or "").strip()[:MAX_MANAGER_CONTEXT_CHARS]
    return "\n\n".join(
        [
            "SYSTEM_RULES:\nТы — личный тренер продаж для менеджера по одной текущей сделке.",
            "RULES:\n"
            "- Отвечай на конкретный вопрос менеджера и помогай выполнить ближайшее действие.\n"
            "- Опирайся только на переданные CONTEXT и не придумывай ответ клиента, факты, даты, суммы или договорённости.\n"
            "- Не называй попытку звонка контактом. CRM-задача — поручение, а не доказательство клиентского результата.\n"
            "- Можно прямо сказать менеджеру, что повторяемое действие не работает, если это следует из контекста.\n"
            "- Блок «Понял ситуацию» строится строго как: situation_summary → next_action → expected_result.\n"
            "- Каждый из трёх текстов должен быть одной короткой фразой; вместе они должны помещаться в 2–3 коротких предложения.\n"
            "- next_action — одно максимально конкретное внешнее действие менеджера. Не давай списков, общих советов, внутренних действий вроде «проверь CRM» и не повторяй видимый контекст.\n"
            "- expected_result — краткая цель коммуникации: какой ответ или следующий шаг нужно получить от клиента.\n"
            "- client_messages — три разных готовых сообщения клиенту без плейсхолдеров: calm спокойно, confident уверенно, direct прямо.\n"
            "- call_scripts — три разные короткие фразы, готовые произнести буквально: soft мягко, business делово, direct прямо. Это не подробный сценарий звонка.\n"
            "- Выбери один рекомендуемый тон отдельно для сообщения и звонка, исходя из ситуации.\n"
            "- Если данных недостаточно, не маскируй предположение под факт: сформулируй безопасный вопрос клиенту на уточнение.\n"
            "- crm_checklist — только то, что менеджер должен зафиксировать после фактического действия.\n"
            "- Верни только полный объект по JSON-схеме, спокойно, эмпатично, прямо и по-русски.\n"
            "- Этот запрос независим: не используй и не запрашивай историю прошлых quick help.",
            _section("SITUATION_CONTEXT", situation_projection),
            _section("ANALYSIS_CONTEXT", analysis_projection),
            _section("DEAL_CONTEXT", project_deal(deal)),
            _section("CURRENT_BITRIX_TASK", project_bitrix_task(current_bitrix_task)),
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
    model: str = MANAGER_MODEL,
    reasoning_effort: str = MANAGER_REASONING_EFFORT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = build_quick_help_prompt(
        question=question,
        analysis_projection=analysis_projection,
        deal=deal,
        current_bitrix_task=current_bitrix_task,
        situation_projection=situation_projection,
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
        prompt_cache_key="neuro-rop:deal-manager-quick-help:v1",
        stable_prefix=prompt_prefix_before(prompt, "MANAGER_QUESTION:"),
    )
    return validate_quick_help(result), metadata

