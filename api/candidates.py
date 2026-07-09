"""
Cheap Bitrix candidate scoring for the ROP UI first screen.

No LLM here: only CRM list fields + stage_policy codes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bitrix.client import BitrixReadOnlyClient, get_env_required
from openai_api.bitrix_links import bitrix_entity_url
from openai_api.change_detection.stage_policy import (
    CLOSED_LOST_STAGE_TYPES,
    LOST_STAGE_NAME_PATTERNS,
    SUCCESS_STAGE_IDS,
)
from setup import BASE_DIR, MSK_TZ


DEFAULT_DAYS = 15
DEFAULT_LIMIT = 20
PIPELINE_MAP_PATH = BASE_DIR / "crm_pipeline_map.json"

# Closed-lost priorities for v1 candidate list.
HIGH_CLOSED_REASONS = {
    "wrong_qualification": ("high", "Закрыта как «Неверный квал» — проверить спорное закрытие"),
    "no_response": ("high", "Закрыта как «Нет ответа» — проверить качество дозвона и каналы"),
    "postponed": ("medium", "Отложена — нужна контрольная дата возврата"),
}
MEDIUM_CLOSED_REASONS = {
    "price_lost": ("medium", "Закрыта по цене — проверить сопоставимость КП"),
    "lost_to_competitor": ("medium", "Ушли к конкуренту — разобрать причину"),
    "integration_blocker": ("medium", "Стоп по интеграции — проверить, решаемо ли"),
    "cannot_produce": ("low", "Не можем произвести — проверить аналог/стоп"),
    "not_relevant": ("low", "Не актуально — проверить причину"),
}
SKIP_CLOSED_REASONS = {"duplicate", "won"}


def date_for_bitrix(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def parse_bitrix_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def days_since(value: Any, *, now: datetime | None = None) -> int | None:
    dt = parse_bitrix_dt(value)
    if dt is None:
        return None
    current = now or datetime.now(MSK_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK_TZ)
    return max(0, int((current - dt.astimezone(MSK_TZ)).total_seconds() // 86400))


def load_pipeline_stage_names() -> dict[str, str]:
    if not PIPELINE_MAP_PATH.exists():
        return {}
    try:
        payload = json.loads(PIPELINE_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    names: dict[str, str] = {}

    def add_stage(stage: dict[str, Any]) -> None:
        status_id = str(stage.get("STATUS_ID") or stage.get("status_id") or "")
        name = str(stage.get("NAME") or stage.get("name") or "")
        if status_id and name:
            names[status_id] = name

    # Current map shape: deal_pipelines[].stages[]
    for pipeline in payload.get("deal_pipelines") or []:
        if not isinstance(pipeline, dict):
            continue
        for stage in pipeline.get("stages") or []:
            if isinstance(stage, dict):
                add_stage(stage)

    # Fallback shapes from older dumps / raw block.
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    for category in list(payload.get("deal_categories") or []) + list(raw.get("deal_categories") or []):
        if not isinstance(category, dict):
            continue
        for stage in category.get("stages") or []:
            if isinstance(stage, dict):
                add_stage(stage)
    for stage in payload.get("deal_stages") or []:
        if isinstance(stage, dict):
            add_stage(stage)
    return names


def closed_reason_from_stage(stage_id: str, stage_name: str) -> str | None:
    if stage_id in CLOSED_LOST_STAGE_TYPES:
        return CLOSED_LOST_STAGE_TYPES[stage_id]
    normalized = stage_name.strip().lower()
    for pattern, reason in LOST_STAGE_NAME_PATTERNS:
        if pattern in normalized:
            return reason
    return None


def make_client() -> BitrixReadOnlyClient:
    return BitrixReadOnlyClient(get_env_required("BITRIX_WEBHOOK_URL"))


def load_status_map(client: BitrixReadOnlyClient, entity_id: str) -> dict[str, str]:
    rows = client.list_all("crm.status.list", {"filter": {"ENTITY_ID": entity_id}})
    result: dict[str, str] = {}
    for row in rows:
        status_id = str(row.get("STATUS_ID") or "")
        name = str(row.get("NAME") or status_id)
        if status_id:
            result[status_id] = name
    return result


def fetch_recent_leads(
    client: BitrixReadOnlyClient,
    *,
    created_days: int,
    modified_days: int,
) -> list[dict[str, Any]]:
    """
    Кандидаты-лиды: фильтр по DATE_CREATE и/или DATE_MODIFY.
    0 = без ограничения по этому полю.
    """
    filter_payload: dict[str, Any] = {}
    if created_days > 0:
        start_create = datetime.now(MSK_TZ) - timedelta(days=created_days)
        filter_payload[">=DATE_CREATE"] = date_for_bitrix(start_create)
    if modified_days > 0:
        start_modify = datetime.now(MSK_TZ) - timedelta(days=modified_days)
        filter_payload[">=DATE_MODIFY"] = date_for_bitrix(start_modify)

    return client.list_all(
        "crm.lead.list",
        {
            "order": {"DATE_CREATE": "DESC", "ID": "DESC"},
            "filter": filter_payload,
            "select": [
                "ID",
                "TITLE",
                "NAME",
                "LAST_NAME",
                "STATUS_ID",
                "STATUS_SEMANTIC_ID",
                "SOURCE_ID",
                "ASSIGNED_BY_ID",
                "OPPORTUNITY",
                "CURRENCY_ID",
                "DATE_CREATE",
                "DATE_MODIFY",
            ],
        },
    )


def fetch_recent_deals(
    client: BitrixReadOnlyClient,
    *,
    created_days: int,
    modified_days: int,
) -> list[dict[str, Any]]:
    """
    Кандидаты-сделки: фильтр по DATE_CREATE и/или DATE_MODIFY.
    0 = без ограничения по этому полю.
    """
    filter_payload: dict[str, Any] = {}
    if created_days > 0:
        start_create = datetime.now(MSK_TZ) - timedelta(days=created_days)
        filter_payload[">=DATE_CREATE"] = date_for_bitrix(start_create)
    if modified_days > 0:
        start_modify = datetime.now(MSK_TZ) - timedelta(days=modified_days)
        filter_payload[">=DATE_MODIFY"] = date_for_bitrix(start_modify)

    return client.list_all(
        "crm.deal.list",
        {
            "order": {"DATE_CREATE": "DESC", "ID": "DESC"},
            "filter": filter_payload,
            "select": [
                "ID",
                "TITLE",
                "STAGE_ID",
                "STAGE_SEMANTIC_ID",
                "CATEGORY_ID",
                "CLOSED",
                "OPPORTUNITY",
                "CURRENCY_ID",
                "ASSIGNED_BY_ID",
                "DATE_CREATE",
                "DATE_MODIFY",
                "CLOSEDATE",
            ],
        },
    )


def score_deal(deal: dict[str, Any], stage_names: dict[str, str]) -> dict[str, Any] | None:
    deal_id = str(deal.get("ID") or "")
    if not deal_id:
        return None

    stage_id = str(deal.get("STAGE_ID") or "")
    stage_name = stage_names.get(stage_id) or stage_id or "не указан"
    crm_closed = str(deal.get("CLOSED") or "").upper() == "Y"
    semantic = str(deal.get("STAGE_SEMANTIC_ID") or "").upper()
    is_success = stage_id in SUCCESS_STAGE_IDS or semantic == "S"
    amount = deal.get("OPPORTUNITY")
    try:
        amount_num = float(amount) if amount not in (None, "") else 0.0
    except (TypeError, ValueError):
        amount_num = 0.0
    stale_days = days_since(deal.get("DATE_MODIFY"))
    closed_reason = closed_reason_from_stage(stage_id, stage_name)

    priority = "low"
    score = 0
    reasons: list[str] = []

    if is_success:
        return None
    if closed_reason in SKIP_CLOSED_REASONS:
        return None

    if closed_reason in HIGH_CLOSED_REASONS:
        priority, reason = HIGH_CLOSED_REASONS[closed_reason]
        score += 100 if priority == "high" else 60
        reasons.append(reason)
        if amount_num >= 1_000_000:
            score += 20
            reasons.append(f"Есть потенциал: {amount_num:,.0f} ₽".replace(",", " "))
    elif closed_reason in MEDIUM_CLOSED_REASONS:
        priority, reason = MEDIUM_CLOSED_REASONS[closed_reason]
        score += 55 if priority == "medium" else 25
        reasons.append(reason)
    elif crm_closed or semantic == "F":
        priority = "medium"
        score += 40
        reasons.append("Закрыта без ясной классификации — нужна проверка РОПа")
    else:
        # Open deal stall heuristics.
        if stale_days is not None and stale_days >= 7:
            priority = "high" if stale_days >= 14 else "medium"
            score += 70 if stale_days >= 14 else 45
            reasons.append(f"Нет движения {stale_days} дн. — возможное зависание")
        if amount_num >= 1_000_000 and (stale_days or 0) >= 5:
            score += 25
            priority = "high"
            reasons.append("Крупная сумма при слабом движении")
        if not reasons:
            return None

    if not reasons:
        return None

    amount_label = ""
    if amount not in (None, ""):
        currency = str(deal.get("CURRENCY_ID") or "RUB")
        amount_label = f"{amount} {currency}".strip()

    return {
        "entity_type": "deal",
        "entity_id": deal_id,
        "title": str(deal.get("TITLE") or f"Сделка {deal_id}"),
        "client_name": str(deal.get("TITLE") or ""),
        "status": stage_name,
        "stage_id": stage_id,
        "amount": amount_label,
        "manager_id": str(deal.get("ASSIGNED_BY_ID") or ""),
        "date_modify": str(deal.get("DATE_MODIFY") or ""),
        "date_create": str(deal.get("DATE_CREATE") or ""),
        "stale_days": stale_days,
        "priority": priority,
        "score": score,
        "attention_reason": reasons[0],
        "reasons": reasons,
        "closed_reason_type": closed_reason,
        "bitrix_url": bitrix_entity_url("deal", deal_id),
        "analyzed": False,
    }


def score_lead(lead: dict[str, Any], status_names: dict[str, str]) -> dict[str, Any] | None:
    lead_id = str(lead.get("ID") or "")
    if not lead_id:
        return None

    status_id = str(lead.get("STATUS_ID") or "")
    semantic = str(lead.get("STATUS_SEMANTIC_ID") or "").upper()
    status_name = status_names.get(status_id) or status_id or "не указан"
    stale_days = days_since(lead.get("DATE_MODIFY"))
    age_days = days_since(lead.get("DATE_CREATE"))

    # Converted leads are handled via deal handoff in analyze flow.
    if status_id.upper() == "CONVERTED" or semantic == "S":
        return {
            "entity_type": "lead",
            "entity_id": lead_id,
            "title": str(lead.get("TITLE") or f"Лид {lead_id}"),
            "client_name": " ".join(
                part for part in [str(lead.get("NAME") or ""), str(lead.get("LAST_NAME") or "")] if part
            ).strip()
            or str(lead.get("TITLE") or ""),
            "status": status_name,
            "stage_id": status_id,
            "amount": str(lead.get("OPPORTUNITY") or ""),
            "manager_id": str(lead.get("ASSIGNED_BY_ID") or ""),
            "date_modify": str(lead.get("DATE_MODIFY") or ""),
            "date_create": str(lead.get("DATE_CREATE") or ""),
            "stale_days": stale_days,
            "priority": "medium",
            "score": 50,
            "attention_reason": "Лид сконвертирован — анализ лучше вести по связанной сделке",
            "reasons": ["STATUS_ID=CONVERTED: при запуске анализа будет handoff на сделку"],
            "closed_reason_type": None,
            "bitrix_url": bitrix_entity_url("lead", lead_id),
            "analyzed": False,
            "converted_handoff": True,
        }

    priority = "low"
    score = 0
    reasons: list[str] = []

    if stale_days is not None and stale_days >= 5:
        priority = "high" if stale_days >= 10 else "medium"
        score += 65 if stale_days >= 10 else 40
        reasons.append(f"Лид без движения {stale_days} дн.")
    if age_days is not None and age_days >= 7 and semantic != "F":
        score += 20
        if priority == "low":
            priority = "medium"
        reasons.append(f"В работе уже {age_days} дн. без конвертации")
    if semantic == "F":
        score += 15
        reasons.append("Негативный статус лида — проверить качество обработки vs качество лида")
        if priority == "low":
            priority = "medium"

    if not reasons:
        return None

    client_name = " ".join(
        part for part in [str(lead.get("NAME") or ""), str(lead.get("LAST_NAME") or "")] if part
    ).strip()

    return {
        "entity_type": "lead",
        "entity_id": lead_id,
        "title": str(lead.get("TITLE") or f"Лид {lead_id}"),
        "client_name": client_name or str(lead.get("TITLE") or ""),
        "status": status_name,
        "stage_id": status_id,
        "amount": str(lead.get("OPPORTUNITY") or ""),
        "manager_id": str(lead.get("ASSIGNED_BY_ID") or ""),
        "date_modify": str(lead.get("DATE_MODIFY") or ""),
        "date_create": str(lead.get("DATE_CREATE") or ""),
        "stale_days": stale_days,
        "priority": priority,
        "score": score,
        "attention_reason": reasons[0],
        "reasons": reasons,
        "closed_reason_type": None,
        "bitrix_url": bitrix_entity_url("lead", lead_id),
        "analyzed": False,
    }


def mark_analyzed(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    root = BASE_DIR / "reports" / "rop_assistant"
    for item in candidates:
        entity_type = item["entity_type"]
        entity_id = item["entity_id"]
        analysis = root / ("deals" if entity_type == "deal" else "leads") / f"{entity_type}_{entity_id}" / "analysis" / f"{entity_type}_{entity_id}_analysis.json"
        item["analyzed"] = analysis.exists()
        if analysis.exists():
            item["analysis_path"] = str(analysis)
    return candidates


def search_candidates(
    *,
    entity_type: str = "all",
    created_days: int = DEFAULT_DAYS,
    modified_days: int = DEFAULT_DAYS,
    days: int | None = None,
    limit: int = DEFAULT_LIMIT,
    priority: str | None = None,
) -> dict[str, Any]:
    # Обратная совместимость: старый параметр days задаёт окно CREATE.
    if days is not None:
        created_days = max(0, int(days))
    else:
        created_days = max(0, int(created_days))
    modified_days = max(0, int(modified_days))
    limit = max(1, min(int(limit), 100))
    client = make_client()
    stage_names = load_pipeline_stage_names()
    lead_status_names = load_status_map(client, "STATUS") if entity_type in {"all", "lead"} else {}

    scored: list[dict[str, Any]] = []
    if entity_type in {"all", "deal"}:
        for deal in fetch_recent_deals(
            client,
            created_days=created_days,
            modified_days=modified_days,
        ):
            item = score_deal(deal, stage_names)
            if item:
                scored.append(item)
    if entity_type in {"all", "lead"}:
        for lead in fetch_recent_leads(
            client,
            created_days=created_days,
            modified_days=modified_days,
        ):
            item = score_lead(lead, lead_status_names)
            if item:
                scored.append(item)

    scored = mark_analyzed(scored)
    if priority in {"high", "medium", "low"}:
        scored = [item for item in scored if item.get("priority") == priority]

    scored.sort(
        key=lambda item: (
            {"high": 3, "medium": 2, "low": 1}.get(str(item.get("priority")), 0),
            int(item.get("score") or 0),
            str(item.get("date_create") or ""),
            str(item.get("date_modify") or ""),
        ),
        reverse=True,
    )
    top = scored[:limit]
    summary = {
        "total_scored": len(scored),
        "returned": len(top),
        "high": sum(1 for item in top if item.get("priority") == "high"),
        "medium": sum(1 for item in top if item.get("priority") == "medium"),
        "low": sum(1 for item in top if item.get("priority") == "low"),
        "already_analyzed": sum(1 for item in top if item.get("analyzed")),
    }
    return {
        "created_days": created_days,
        "modified_days": modified_days,
        "days": created_days,  # совместимость со старым UI/клиентом
        "limit": limit,
        "entity_type": entity_type,
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "summary": summary,
        "candidates": top,
    }
