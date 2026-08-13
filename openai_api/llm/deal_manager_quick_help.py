"""Strict structured output for the deal-closing manager workspace.

Two voices share one JSON contract and context, but not one prompt:

* ``reanimator`` — current soft pull / restore-contact coach
* ``push`` — expert commercial close using one confirmed pressure lever
"""

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
from openai_api.llm.manager_tactics import load_manager_tactics, manager_tactic_ids


MAX_QUICK_HELP_OUTPUT_TOKENS = 4000
ASSISTANT_MODES = ("push", "reanimator")
_STRATEGIES = ("primary", "alternative", "pattern_break")
_ANSWER_CONTRACT = "strategy_v3"
_LEGACY_CONTRACT = "strategy_v2"
_MESSAGE_LIMIT = 1800
_REQUIRED_FIELDS = (
    "answer_contract",
    "mode",
    "situation_summary",
    "next_action",
    "expected_result",
    "pressure_lever",
    "strategy_labels",
    "client_messages",
    "lifehacks",
    "fallback_action",
)
_V2_REQUIRED_FIELDS = (
    "answer_contract",
    "situation_summary",
    "next_action",
    "expected_result",
    "client_messages",
    "lifehacks",
    "fallback_action",
)


def quick_help_schema(*, tactic_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
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
        "mode": {"type": "string", "enum": list(ASSISTANT_MODES)},
        "situation_summary": {"type": "string", "minLength": 1, "maxLength": 420},
        "next_action": {"type": "string", "minLength": 1, "maxLength": 420},
        "expected_result": {"type": "string", "minLength": 1, "maxLength": 320},
        "pressure_lever": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "rationale"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 80},
                "rationale": {"type": "string", "minLength": 1, "maxLength": 320},
            },
        },
        "strategy_labels": strategy_variants(48),
        "client_messages": strategy_variants(_MESSAGE_LIMIT),
        "lifehacks": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["tactic_id", "title", "action", "why_relevant", "conditions"],
                "properties": {
                    "tactic_id": ({"type": "string", "enum": list(tactic_ids)} if tactic_ids else {"type": "string", "minLength": 1, "maxLength": 80}),
                    "title": {"type": "string", "minLength": 1, "maxLength": 180},
                    "action": {"type": "string", "minLength": 1, "maxLength": 600},
                    "why_relevant": {"type": "string", "minLength": 1, "maxLength": 420},
                    "conditions": {"type": "string", "minLength": 1, "maxLength": 420},
                },
            },
        },
        "fallback_action": {"type": "string", "minLength": 1, "maxLength": 420},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(fields),
        "properties": fields,
    }


def _validate_text_fields(value: dict[str, Any], limits: dict[str, int]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for field, max_length in limits.items():
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise ValueError("В quick help есть пустое текстовое поле")
        if "\n" in item:
            raise ValueError(f"{field} должен быть одной короткой фразой без списка")
        normalized[field] = item.strip()[:max_length]
    return normalized


def _validate_strategy_texts(variants: Any, *, field: str, max_length: int) -> dict[str, str]:
    if not isinstance(variants, dict) or set(variants) != set(_STRATEGIES):
        raise ValueError(f"{field} должен содержать все допустимые стратегии")
    if any(not isinstance(variants[strategy], str) or not variants[strategy].strip() for strategy in _STRATEGIES):
        raise ValueError(f"{field} должен содержать только непустые тексты")
    texts = [variants[strategy].strip() for strategy in _STRATEGIES]
    if len({text.casefold() for text in texts}) != len(_STRATEGIES):
        raise ValueError(f"{field} должен содержать разные варианты")
    return {strategy: variants[strategy].strip()[:max_length] for strategy in _STRATEGIES}


def _validate_lifehacks(lifehacks: Any, *, allowed_tactic_ids: tuple[str, ...] | None) -> list[dict[str, str]]:
    if not isinstance(lifehacks, list) or len(lifehacks) > 3:
        raise ValueError("lifehacks должен содержать не более 3 рекомендаций")
    normalized_lifehacks: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    limits = {"tactic_id": 80, "title": 180, "action": 600, "why_relevant": 420, "conditions": 420}
    for item in lifehacks:
        if not isinstance(item, dict) or set(item) != set(limits):
            raise ValueError("Элемент lifehacks имеет неверный контракт")
        current: dict[str, str] = {}
        for field, limit in limits.items():
            text = item.get(field)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Элемент lifehacks содержит пустое поле")
            current[field] = text.strip()[:limit]
        if current["tactic_id"] in seen_ids:
            raise ValueError("lifehacks содержит повторяющийся tactic_id")
        if allowed_tactic_ids is not None and current["tactic_id"] not in allowed_tactic_ids:
            raise ValueError("lifehacks содержит неизвестный tactic_id")
        seen_ids.add(current["tactic_id"])
        normalized_lifehacks.append(current)
    return normalized_lifehacks


def _validate_pressure_lever(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("pressure_lever должен быть объектом")
    title = value.get("title")
    rationale = value.get("rationale")
    if not isinstance(title, str) or not title.strip() or not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("pressure_lever должен содержать title и rationale")
    if "\n" in title:
        raise ValueError("title рычага должен быть одной короткой фразой")
    return {"title": title.strip()[:80], "rationale": rationale.strip()[:320]}


def validate_quick_help(
    value: Any,
    *,
    allowed_tactic_ids: tuple[str, ...] | None = None,
    expected_mode: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Quick help должен быть JSON-объектом")
    contract = value.get("answer_contract")
    if contract == _LEGACY_CONTRACT:
        return _validate_strategy_v2(value, allowed_tactic_ids=allowed_tactic_ids)
    if any(field not in value for field in _REQUIRED_FIELDS):
        raise ValueError("В quick help отсутствует обязательное поле")
    if contract != _ANSWER_CONTRACT:
        raise ValueError("answer_contract содержит неподдерживаемую версию")
    mode = str(value.get("mode") or "").strip()
    if mode not in ASSISTANT_MODES:
        raise ValueError("mode должен быть push или reanimator")
    if expected_mode is not None and mode != expected_mode:
        raise ValueError("mode не соответствует запрошенному режиму")
    normalized: dict[str, Any] = {
        "answer_contract": _ANSWER_CONTRACT,
        "mode": mode,
        **_validate_text_fields(
            value,
            {
                "situation_summary": 420,
                "next_action": 420,
                "expected_result": 320,
                "fallback_action": 420,
            },
        ),
        "pressure_lever": _validate_pressure_lever(value.get("pressure_lever")),
        "strategy_labels": _validate_strategy_texts(value.get("strategy_labels"), field="strategy_labels", max_length=48),
        "client_messages": _validate_strategy_texts(value.get("client_messages"), field="client_messages", max_length=_MESSAGE_LIMIT),
        "lifehacks": _validate_lifehacks(value.get("lifehacks"), allowed_tactic_ids=allowed_tactic_ids),
    }
    return normalized


def _validate_strategy_v2(value: dict[str, Any], *, allowed_tactic_ids: tuple[str, ...] | None) -> dict[str, Any]:
    if any(field not in value for field in _V2_REQUIRED_FIELDS):
        raise ValueError("В quick help отсутствует обязательное поле")
    return {
        "answer_contract": _LEGACY_CONTRACT,
        **_validate_text_fields(
            value,
            {
                "situation_summary": 420,
                "next_action": 420,
                "expected_result": 320,
                "fallback_action": 420,
            },
        ),
        "client_messages": _validate_strategy_texts(value.get("client_messages"), field="client_messages", max_length=1200),
        "lifehacks": _validate_lifehacks(value.get("lifehacks"), allowed_tactic_ids=allowed_tactic_ids),
    }


def _section(name: str, value: Any) -> str:
    return f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=2)}"


def _shared_context_sections(
    *,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    situation_projection: dict[str, Any],
    communication_pattern_context: dict[str, Any],
    manager_tactics: str,
    question: str,
) -> list[str]:
    return [
        _section("SITUATION_CONTEXT", situation_projection),
        _section("ANALYSIS_CONTEXT", analysis_projection),
        _section("DEAL_CONTEXT", project_deal(deal)),
        _section("CURRENT_BITRIX_TASK", project_bitrix_task(current_bitrix_task)),
        _section("COMMUNICATION_PATTERN_CONTEXT", communication_pattern_context),
        f"MANAGER_TACTICS:\n{manager_tactics}",
        _section("MANAGER_QUESTION", question),
    ]


def build_reanimator_prompt(
    *,
    question: str,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    situation_projection: dict[str, Any],
    communication_pattern_context: dict[str, Any],
    manager_tactics: str | None = None,
) -> str:
    question = str(question or "").strip()[:MAX_MANAGER_CONTEXT_CHARS]
    manager_tactics = manager_tactics if manager_tactics is not None else load_manager_tactics()
    return "\n\n".join(
        [
            "SYSTEM_RULES:\nТы — прикладной sales-assistant менеджера по одной текущей сделке в режиме Реаниматор (pull). Твоя цель — помочь получить ближайший подтверждаемый шаг клиента, который возвращает его в коммуникацию и продвигает сделку к деньгам.",
            "RULES:\n"
            "- Это мягкий режим восстановления контакта, не жёсткий дожим. Не дави, не торопи искусственным дефицитом и не превращай ответ в push.\n"
            "- Если вопрос менеджера не пустой, отвечай на конкретный вопрос менеджера в рамках этого мягкого режима. Иначе сформируй первую актуальную рекомендацию по текущему CONTEXT. Внутренне определи стадию сделки, подтверждённые клиентом факты, blocker и нужную micro-conversion; внутреннее рассуждение не показывай.\n"
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
            "- Готовое сообщение клиенту строй как продолжение предыдущей коммуникации: приветствие; если давно не было контакта — кто мы и на чём остановились; понятная польза или причина ответить; низкое усилие клиента; один следующий шаг. Не превращай это в презентацию продукта.\n"
            "- Для каждого рекомендуемого касания внутренне определи, зачем клиенту ответить или выполнить следующий шаг. Если контекст подтверждает конкретную ценность, кратко отрази её в формулировке. Не придумывай выгоду. Если ценность нельзя обосновать контекстом, используй нейтральное объяснение цели или короткий вопрос с низким усилием ответа.\n"
            "- pressure_lever — один подтверждаемый рычаг, через который сейчас лучше всего вернуть клиента в коммуникацию. Короткий title и rationale только из фактов CONTEXT. Это не жёсткое давление: точка, через которую проще восстановить контакт. Если сильного рычага нет, выбери честный нейтральный ход, а не выдуманный дефицит, бронь, скидку или дедлайн.\n"
            "- client_messages содержит три реально разные стратегии одной ближайшей цели: primary — лучший ход сейчас и всегда вариант №1; alternative — другой аргумент, вопрос, CTA или акцент; pattern_break — смена неработающей механики. Это не перефразировки.\n"
            "- strategy_labels — короткие смысловые названия тех же трёх стратегий, по которым менеджер поймёт различие до клика. Не пиши «1», «2», «3» и не делай длинных заголовков.\n"
            "- client_messages — готовые сообщения без плейсхолдеров. Не создавай здесь речевой модуль или полный сценарий разговора.\n"
            "- lifehacks — от 0 до 3 реально применимых тактик только из MANAGER_TACTICS. Копируй стабильный tactic_id. Если подтверждающих условий нет, не выбирай тактику. Условную возможность не подавай как доступный факт. Лайфхак не должен дублировать next_action, situation_summary или текст сообщения клиенту: это дополнительный ход.\n"
            "- fallback_action — одно конкретное действие с одной micro-conversion. По возможности сохраняй ближайшую цель, но меняй способ её достижения и не делай fallback сложнее основного касания. Меняй цель только если переданный контекст показывает, что она неактуальна, недостижима или перед ней есть более ранний blocker. Не придумывай срок или число попыток. При явном отказе или просьбе не связываться fallback может быть внутренним действием.\n"
            "- Если ANALYSIS_CONTEXT содержит client_communication_profile со status tentative или supported, используй primary_style, secondary_style, profile_confidence и recommended_communication, чтобы адаптировать длину, прямоту, порядок аргументов, детализацию, акценты и CTA каждой стратегии. Не объясняй DISC менеджеру и не меняй факты, цель или следующий шаг. При insufficient_evidence используй нейтральный деловой стиль.\n"
            "- Если данных недостаточно, не маскируй предположение под факт: сформулируй безопасный вопрос клиенту на уточнение.\n"
            "- Не придумывай скидки, дедлайны, суммы, обещания, имена, решения клиента, договорённости или искусственный дефицит.\n"
            "- mode всегда reanimator. Верни только полный объект по JSON-схеме, спокойно, эмпатично, прямо и по-русски.\n"
            "- Этот запрос независим: не используй и не запрашивай историю прошлых ответов.",
            *_shared_context_sections(
                analysis_projection=analysis_projection,
                deal=deal,
                current_bitrix_task=current_bitrix_task,
                situation_projection=situation_projection,
                communication_pattern_context=communication_pattern_context,
                manager_tactics=manager_tactics,
                question=question,
            ),
        ]
    )


def build_push_prompt(
    *,
    question: str,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    situation_projection: dict[str, Any],
    communication_pattern_context: dict[str, Any],
    manager_tactics: str | None = None,
) -> str:
    question = str(question or "").strip()[:MAX_MANAGER_CONTEXT_CHARS]
    manager_tactics = manager_tactics if manager_tactics is not None else load_manager_tactics()
    return "\n\n".join(
        [
            "SYSTEM_RULES:\nТы — экспертный коммерческий помощник менеджера по одной текущей сделке в режиме Дожим (push). Твоя цель — продвинуть сделку к ближайшему коммерчески значимому решению через одну подтверждаемую точку давления, ценности или риска.",
            "RULES:\n"
            "- Это PUSH-режим: уверенный, экспертный, предметный, коммерческий. При необходимости будь технически подробным. Не канцелярит, не пустое «посмотрели КП?», не хамство, не манипуляции и не выдуманный дефицит. «Жёсткий» здесь означает сильную экспертную позицию, а не агрессию.\n"
            "- Если вопрос менеджера не пустой, пересобери дожим в рамках этого режима с учётом правки менеджера. Иначе сформируй первую актуальную рекомендацию по текущему CONTEXT.\n"
            "- Внутренне определи: текущий blocker; ближайшее коммерчески значимое движение; один подтверждаемый рычаг; конкретное внешнее next_action. Внутреннее рассуждение не показывай.\n"
            "- Опирайся только на переданные CONTEXT. Не придумывай ответ клиента, факты, даты, суммы, скидки, бонусы, бронь, дедлайны, конкурентные предложения, технические характеристики, экономический эффект, обещания производства, решения клиента, полномочия или договорённости.\n"
            "- Не называй попытку звонка контактом. CRM-задача — поручение, а не доказательство клиентского результата.\n"
            "- COMMUNICATION_PATTERN_CONTEXT содержит только детерминированный срез коммуникаций без текстов и транскриптов. По нему можно утверждать повторение канала или попыток без подтверждённого контакта, но нельзя утверждать повторение одинакового CTA.\n"
            "- Если один канал или способ контакта несколько раз не дал подтверждённого результата, не рекомендуй просто повторить его: измени канал, повод, CTA, аргумент или допустимого участника сделки. Не придумывай новых лиц.\n"
            "- Не откатывай сделку к общей квалификации, если она уже дошла до КП, договора, счёта или оплаты и нет конкретной причины возвращаться назад.\n"
            "- Блок «Понял ситуацию» строится строго как: situation_summary → next_action → expected_result. Он должен сказать, что сейчас происходит, что конкретно сделать и какой подтверждаемый результат нужен. Это не большой аналитический отчёт.\n"
            "- next_action — одно максимально конкретное внешнее действие менеджера. Не давай списков, общих советов и внутренних действий вроде «проверь CRM».\n"
            "- expected_result называет одну ближайшую micro-conversion клиента, а не действие менеджера.\n"
            "- pressure_lever — ровно один приоритетный рычаг на этот ответ, не три и не девять комбинаций. Сначала выбери линзу только из подтверждаемых фактов CONTEXT: отстройка по надёжности или узлу относительно конкурента; срок или окно решения клиента; выход на ЛПР; тест, образец или видео по реальной таре или узлу; договор, юристы, лизинг или согласование; снятие конкретного возражения. Title называет выбранную линзу своими словами по фактам сделки, а не копирует этот список. Rationale объясняет, почему именно этот факт сейчас двигает сделку. Если под линзу нет подтверждённого факта — не используй её. Если сильного рычага нет, выбери нейтральный честно обоснованный ход, а не галлюцинируй давление.\n"
            "- Рычаг должен влиять на «Понял ситуацию», next_action, три варианта коммуникации и готовые сообщения.\n"
            "- client_messages содержит три реально разные стратегии одной ближайшей цели, все через выбранный рычаг, но с разным заходом: primary — лучший ход; alternative — другой аргумент, вопрос, CTA или акцент; pattern_break — смена механики. Это не перефразировки.\n"
            "- strategy_labels — короткие смысловые названия тех же трёх стратегий. Не пиши «1», «2», «3».\n"
            "- Готовое сообщение клиенту строй по формуле: приветствие; на чём остановились — что уже отправили или о чём договорились; один экспертный аргумент через выбранный рычаг; один закрывающий шаг или вопрос. Не начинай с «посмотрели КП?». Не превращай текст в презентацию всей линии. Не требуй искусственной краткости: объём может быть больше, если это усиливает экспертность и снимает blocker. Не раздувай канцеляритом.\n"
            "- Если ANALYSIS_CONTEXT содержит client_communication_profile со status tentative или supported, адаптируй длину, прямоту, структуру, детализацию, акцент и CTA под профиль. Не объясняй DISC менеджеру и не меняй факты, цель, рычаг или следующий шаг. При insufficient_evidence используй нейтральный деловой экспертный стиль.\n"
            "- lifehacks — от 0 до 3 реально применимых тактик только из MANAGER_TACTICS. Копируй стабильный tactic_id. Лайфхак не должен дублировать next_action, situation_summary или сообщение клиенту. Если дополнительной тактики нет, верни пустой массив.\n"
            "- fallback_action — одно конкретное действие с одной micro-conversion. Не повторяй основной ход, не усложняй его без причины, меняй механику, не придумывай новые факты.\n"
            "- Если данных недостаточно, не маскируй предположение под факт: сформулируй безопасный вопрос клиенту на уточнение.\n"
            "- mode всегда push. Верни только полный объект по JSON-схеме по-русски.\n"
            "- Этот запрос независим: не используй и не запрашивай историю прошлых ответов.",
            *_shared_context_sections(
                analysis_projection=analysis_projection,
                deal=deal,
                current_bitrix_task=current_bitrix_task,
                situation_projection=situation_projection,
                communication_pattern_context=communication_pattern_context,
                manager_tactics=manager_tactics,
                question=question,
            ),
        ]
    )


def build_quick_help_prompt(
    *,
    question: str,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    situation_projection: dict[str, Any],
    communication_pattern_context: dict[str, Any],
    manager_tactics: str | None = None,
    mode: str = "reanimator",
) -> str:
    if mode not in ASSISTANT_MODES:
        raise ValueError("mode должен быть push или reanimator")
    builder = build_push_prompt if mode == "push" else build_reanimator_prompt
    return builder(
        question=question,
        analysis_projection=analysis_projection,
        deal=deal,
        current_bitrix_task=current_bitrix_task,
        situation_projection=situation_projection,
        communication_pattern_context=communication_pattern_context,
        manager_tactics=manager_tactics,
    )


def generate_deal_manager_quick_help(
    *,
    question: str,
    analysis_projection: dict[str, Any],
    deal: dict[str, Any],
    current_bitrix_task: dict[str, Any] | None,
    situation_projection: dict[str, Any],
    communication_pattern_context: dict[str, Any],
    manager_tactics: str | None = None,
    mode: str = "reanimator",
    model: str = MANAGER_MODEL,
    reasoning_effort: str = MANAGER_REASONING_EFFORT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if mode not in ASSISTANT_MODES:
        raise ValueError("mode должен быть push или reanimator")
    tactics = manager_tactics if manager_tactics is not None else load_manager_tactics()
    tactic_ids = manager_tactic_ids(tactics)
    if not tactic_ids:
        raise ValueError("В базе практических тактик не найдены стабильные ID")
    prompt = build_quick_help_prompt(
        question=question,
        analysis_projection=analysis_projection,
        deal=deal,
        current_bitrix_task=current_bitrix_task,
        situation_projection=situation_projection,
        communication_pattern_context=communication_pattern_context,
        manager_tactics=tactics,
        mode=mode,
    )
    cache_key = (
        "neuro-rop:deal-manager-push:v2"
        if mode == "push"
        else "neuro-rop:deal-manager-quick-help:v5"
    )
    result, metadata = call_structured_output_json(
        prompt,
        schema=quick_help_schema(tactic_ids=tactic_ids),
        schema_name="deal_manager_quick_help",
        model=model,
        reasoning_effort=reasoning_effort,
        max_output_tokens=MAX_QUICK_HELP_OUTPUT_TOKENS,
        log_title=f"deal manager {mode} prompt",
        call_type=f"deal_manager_quick_help_{mode}",
        prompt_cache_key=cache_key,
        stable_prefix=prompt_prefix_before(prompt, "MANAGER_QUESTION:"),
    )
    return validate_quick_help(result, allowed_tactic_ids=tactic_ids, expected_mode=mode), metadata
