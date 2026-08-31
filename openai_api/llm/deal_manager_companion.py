"""Short post-call client message from the current deal snapshot."""

from __future__ import annotations

import json
from typing import Any

from openai_api.config import COMPANION_MAX_OUTPUT_TOKENS
from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT, project_bitrix_task, project_deal
from openai_api.llm.llm_client import call_structured_output_json, deal_trace_id, prompt_prefix_before
from openai_api.llm.prompt_parts import assemble_prompt, static_prompt_from_full


COMPANION_CONTRACT = "companion_message_v1"


def companion_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["companion_contract", "understood", "message_text", "insufficient_reason"],
        "properties": {
            "companion_contract": {"type": "string", "enum": [COMPANION_CONTRACT]},
            "understood": {
                "type": "array",
                "minItems": 0,
                "maxItems": 3,
                "items": {"type": "string", "minLength": 1, "maxLength": 180},
            },
            "message_text": {"type": "string", "maxLength": 1200},
            "insufficient_reason": {"type": ["string", "null"], "maxLength": 240},
        },
    }


def validate_companion(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("companion_contract") != COMPANION_CONTRACT:
        raise ValueError("Сопроводительный текст имеет неподдерживаемый контракт")
    understood_raw = value.get("understood")
    if not isinstance(understood_raw, list) or len(understood_raw) > 3:
        raise ValueError("Сопроводительный текст должен содержать до трёх тезисов")
    understood: list[str] = []
    for item in understood_raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Тезис сопроводительного текста пуст")
        understood.append(item.strip()[:180])
    message = value.get("message_text")
    if not isinstance(message, str):
        raise ValueError("Сопроводительный текст должен быть строкой")
    message = message.strip()[:1200]
    reason = value.get("insufficient_reason")
    if reason is not None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Причина недостаточности должна быть строкой или null")
        reason = reason.strip()[:240]
    if reason:
        return {
            "companion_contract": COMPANION_CONTRACT,
            "understood": understood[:3],
            "message_text": "",
            "insufficient_reason": reason,
        }
    if not message:
        raise ValueError("Сопроводительный текст пуст")
    if len([line for line in message.splitlines() if line.strip()]) > 12:
        raise ValueError("Сопроводительный текст слишком длинный")
    return {
        "companion_contract": COMPANION_CONTRACT,
        "understood": understood[:3],
        "message_text": message,
        "insufficient_reason": None,
    }


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def companion_context_sections(**kwargs: Any) -> list[str]:
    manager_note = str(kwargs.get("manager_note") or "").strip()[:4000]
    previous_message = str(kwargs.get("previous_message") or "").strip()[:1200]
    parts = [
        _section("ANALYSIS_CONTEXT", kwargs["analysis_projection"]),
        _section("SITUATION_CONTEXT", kwargs["situation_projection"]),
        _section("DEAL_CONTEXT", project_deal(kwargs["deal"])),
        _section("CURRENT_BITRIX_TASK", project_bitrix_task(kwargs.get("current_bitrix_task"))),
        _section("LAST_CONTACT", kwargs["last_contact"]),
    ]
    if previous_message:
        parts.append(_section("PREVIOUS_MESSAGE", previous_message))
    if manager_note:
        parts.append(_section("MANAGER_NOTE", manager_note))
    return parts


def companion_static_prompt(*, manager_note: str | None = None) -> str:
    return static_prompt_from_full(
        build_companion_prompt(
            analysis_projection={},
            situation_projection={},
            deal={},
            current_bitrix_task=None,
            last_contact={},
            previous_message="",
            manager_note=manager_note or "",
        ),
        "ANALYSIS_CONTEXT:",
    )


def build_companion_prompt(**kwargs: Any) -> str:
    manager_note = str(kwargs.get("manager_note") or "").strip()[:4000]
    rules = (
        "RULES:\n"
        "- Сначала по старому снимку сделки и последней коммуникации пойми, что изменилось, подтвердилось, отменилось и кто владеет следующим шагом.\n"
        "- Затем напиши готовое сообщение в WhatsApp / Max / Telegram: 3–7 коротких смысловых строк, не сплошной абзац.\n"
        "- Формула: короткий вход (спасибо / фиксирую итог) → главное изменение или результат → при уместности акцент на ценности или критерии выбора → обязательства сторон → следующий шаг и когда.\n"
        "- Можно использовать фиксацию договорённостей, value anchoring, reframing, micro-commitment, ownership и specific next step. Нельзя придумывать факты, даты, обещания, конкурента, бюджет или решение клиента.\n"
        "- Рекомендация AI из отчёта не является договорённостью, пока клиент этого не сказал.\n"
        "- DISC влияет только на тон, длину и степень конкретики. DISC не источник фактов.\n"
        "- Если LAST_CONTACT.content_available=false, не выдумывай текст письма или транскрипт. Бери только метаданные касания и факты из ANALYSIS_CONTEXT / SITUATION_CONTEXT.\n"
        "- Если подтверждённого результата разговора нет, верни insufficient_reason и пустой message_text. Не пиши правдоподобное сообщение.\n"
        "- understood — 1–3 коротких тезиса, что система поняла, для контроля менеджера, не для клиента.\n"
        "- Без эмодзи, без канцелярита, без поручений менеджеру. Обращайся к клиенту по имени, если имя есть в контексте. Верни только JSON по схеме на русском языке."
    )
    if manager_note:
        rules += (
            "\n- Есть MANAGER_NOTE: перепиши PREVIOUS_MESSAGE по просьбе менеджера. "
            "Не добавляй факты, даты и обещания, которых нет в контексте. "
            "Если менеджер просит убрать дату, обещание или следующий шаг — убери."
        )
    return "\n\n".join([
        "SYSTEM_RULES:\nТы пишешь короткое сопроводительное сообщение клиенту после уже состоявшегося разговора или переписки. Это не Дожим и не идеи фоллоуапов.",
        rules,
        *companion_context_sections(**kwargs),
    ])


def generate_deal_manager_companion(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_exchange_callback = kwargs.pop("raw_exchange_callback", None)
    prompt_template = kwargs.pop("prompt_template", None)
    model = kwargs.pop("model", None) or MANAGER_MODEL
    reasoning_effort = kwargs.pop("reasoning_effort", None) or MANAGER_REASONING_EFFORT
    call_type = kwargs.pop("call_type", None) or "deal_manager_companion"
    prompt = assemble_prompt(prompt_template, companion_context_sections(**kwargs)) if prompt_template else build_companion_prompt(**kwargs)
    result, metadata = call_structured_output_json(
        prompt, schema=companion_schema(), schema_name="deal_manager_companion", model=model,
        reasoning_effort=reasoning_effort, max_output_tokens=COMPANION_MAX_OUTPUT_TOKENS,
        log_title="deal manager companion prompt", call_type=call_type,
        prompt_cache_key="neuro-rop:deal-manager-companion:v1",
        stable_prefix=prompt_prefix_before(prompt, "LAST_CONTACT:"),
        trace_entity_type="deal",
        trace_entity_id=deal_trace_id(kwargs.get("deal")),
        raw_exchange_callback=raw_exchange_callback,
    )
    return validate_companion(result), metadata
