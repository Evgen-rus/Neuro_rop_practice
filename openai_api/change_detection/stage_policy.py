"""
Deterministic CRM stage policy for deal analysis.

The CRM pipeline map is generated from Bitrix and can change. This module keeps
the product interpretation we need for ROP reports: whether a deal is closed as
lost and what kind of closed reason should guide the LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


CLOSED_LOST_STAGE_TYPES: dict[str, str] = {
    "C15:LOSE": "duplicate",
    "C15:1": "lost_to_competitor",
    "C15:6": "integration_blocker",
    "C15:2": "price_lost",
    "C15:3": "postponed",
    "C15:4": "wrong_qualification",
    "C15:5": "cannot_produce",
    "C15:UC_BCU6T4": "not_relevant",
    "C15:UC_PZBQIN": "no_response",
}

SUCCESS_STAGE_IDS = {"C15:WON", "WON"}

LOST_STAGE_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("дубль", "duplicate"),
    ("купили у других", "lost_to_competitor"),
    ("чз", "integration_blocker"),
    ("интеграц", "integration_blocker"),
    ("дорого", "price_lost"),
    ("отлож", "postponed"),
    ("неверный квал", "wrong_qualification"),
    ("не можем произвести", "cannot_produce"),
    ("не актуально", "not_relevant"),
    ("нет ответа", "no_response"),
)


CLOSED_REASON_INSTRUCTIONS: dict[str, str] = {
    "duplicate": "Не реанимировать как продажу. РОПу проверить, что это действительно дубль.",
    "lost_to_competitor": "Разобрать причину проигрыша конкуренту и предложить мягкий post-loss follow-up, если уместно.",
    "integration_blocker": "Проверить, является ли интеграция реальным техническим стопом или решаемым барьером.",
    "price_lost": "Проверить, была ли защищена ценность, комплектация, сроки, сервис, лизинг или альтернатива по составу. При существенном ценовом разрыве сначала проверить, сравниваются ли одинаковые комплектации.",
    "postponed": "Не считать окончательной потерей без контрольной даты. Нужна реактивация или прогрев.",
    "wrong_qualification": "Искать спорное закрытие: если есть потребность, деньги, срок или путь к ЛПР, подсветить РОПу управленческую проверку. При существенном ценовом разрыве не рекомендовать возврат без проверки сопоставимости КП, состава решения и реалистичного следующего шага.",
    "cannot_produce": "Не давать клиентский текст без проверки производства, аналога или честного технического стопа.",
    "not_relevant": "Проверить причину неактуальности: срок, бюджет, уже купили, задача снята или нет контакта.",
    "no_response": "Проверить качество дозвона, интервалы попыток и альтернативные каналы до признания потери.",
}


def _dig(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _load_raw_context(deal_dir: Path, deal_id: str) -> dict[str, Any]:
    raw_path = deal_dir / "raw" / f"deal_{deal_id}_context.json"
    if not raw_path.exists():
        return {}
    try:
        return json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _deal_payload(raw_context: dict[str, Any]) -> dict[str, Any]:
    result = _dig(raw_context, "deal", "response", "response", "result")
    return result if isinstance(result, dict) else {}


def _closed_reason_from_stage_name(stage_name: str) -> str | None:
    normalized = stage_name.strip().lower()
    for pattern, reason in LOST_STAGE_NAME_PATTERNS:
        if pattern in normalized:
            return reason
    return None


def build_deal_stage_policy(deal_dir: Path, deal_id: str) -> dict[str, Any]:
    raw_context = _load_raw_context(deal_dir, deal_id)
    deal = _deal_payload(raw_context)
    stage_info = raw_context.get("stage_info") if isinstance(raw_context.get("stage_info"), dict) else {}
    stage = stage_info.get("stage") if isinstance(stage_info.get("stage"), dict) else {}
    pipeline = stage_info.get("pipeline") if isinstance(stage_info.get("pipeline"), dict) else {}

    stage_id = str(stage.get("status_id") or deal.get("STAGE_ID") or "unknown")
    stage_name = str(stage.get("name") or stage.get("NAME") or "unknown")
    category_id = str(pipeline.get("id") or deal.get("CATEGORY_ID") or "unknown")
    pipeline_name = str(pipeline.get("name") or "unknown")
    crm_closed = str(deal.get("CLOSED", "")).upper() == "Y"
    stage_semantic_id = str(deal.get("STAGE_SEMANTIC_ID") or stage.get("semantics") or "unknown")

    is_success = stage_id in SUCCESS_STAGE_IDS or stage_semantic_id.upper() == "S"
    closed_reason_type = CLOSED_LOST_STAGE_TYPES.get(stage_id) or _closed_reason_from_stage_name(stage_name)
    is_closed_lost = bool(crm_closed and not is_success and (closed_reason_type or stage_semantic_id.upper() == "F"))
    if is_success:
        normalized_reason = "won"
    elif closed_reason_type:
        normalized_reason = closed_reason_type
    elif is_closed_lost:
        normalized_reason = "unknown"
    else:
        normalized_reason = "not_applicable"

    return {
        "deal_id": str(deal_id),
        "category_id": category_id,
        "pipeline_name": pipeline_name,
        "stage_id": stage_id,
        "stage_name": stage_name,
        "crm_closed": crm_closed,
        "stage_semantic_id": stage_semantic_id,
        "is_success": is_success,
        "is_closed_lost": is_closed_lost,
        "closed_reason_type": normalized_reason,
        "closed_reason_instruction": CLOSED_REASON_INSTRUCTIONS.get(normalized_reason, "Не применимо."),
    }
