"""
Cheap Bitrix candidate scoring for the ROP UI first screen.

No LLM here: only CRM list fields + stage_policy codes.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bitrix.client import BitrixReadOnlyClient, fetch_user_names, get_env_required
from bitrix.customer_history import (
    build_history_sections,
    build_normalized_communications,
    clean_text,
    result_items,
)
from bitrix.usage_trace import bitrix_trace_context
from openai_api.bitrix_links import bitrix_entity_url
from openai_api.config import ANALYSIS_MAX_OUTPUT_TOKENS, ANALYSIS_MODEL, USD_RUB_RATE
from openai_api.change_detection.stage_policy import (
    CLOSED_LOST_STAGE_TYPES,
    LOST_STAGE_NAME_PATTERNS,
    SUCCESS_STAGE_IDS,
)
from openai_api.pricing import estimate_analysis_cost
from setup import BASE_DIR, MSK_TZ
from storage.rop_db import (
    DEFAULT_DB_PATH,
    daily_paid_capacity_used,
    default_analysis_profile,
    get_candidate_review_states,
    get_crm_sync_state,
    get_entity_state,
    get_latest_ui_report,
    put_crm_sync_state,
    reconcile_candidate_cases,
)


DEFAULT_DAYS = 15
DEFAULT_LIMIT = 20
PIPELINE_MAP_PATH = BASE_DIR / "crm_pipeline_map.json"
LEAD_CONTROL_SNAPSHOT_KEY = "lead_control:list:v1"
LEAD_CONTROL_HISTORY_KEY_PREFIX = "lead_control:history:v1:"

LEAD_CONTROL_GROUPS: dict[str, dict[str, dict[str, Any]]] = {
    "active": {
        "UC_W1ZZ1V": {"name": "Идентификация (BANT) (хол.отдел)", "semantics": None},
        "UC_4QFNZN": {"name": "В проработке (B) (хол.отдел)", "semantics": None},
    },
    "closed": {
        "UC_X6NW6I": {"name": "Нет производства/мечты", "semantics": "F"},
        "UC_U1XO6U": {"name": "Срок покупки более 6 месяцев", "semantics": "F"},
    },
}


class LeadControlConfigurationError(ValueError):
    """Lead-control stage mapping differs from the verified CRM contract."""


def resolve_lead_control_group(group: str) -> dict[str, Any]:
    expected = LEAD_CONTROL_GROUPS.get(str(group))
    if expected is None:
        raise LeadControlConfigurationError("Неизвестная группа контроля лидов")
    stages = (load_pipeline_map().get("lead_pipeline") or {}).get("stages") or []
    by_id: dict[str, list[dict[str, Any]]] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        status_id = str(stage.get("status_id") or stage.get("STATUS_ID") or "")
        if status_id:
            by_id.setdefault(status_id, []).append(stage)

    for status_id, contract in expected.items():
        rows = by_id.get(status_id) or []
        if len(rows) != 1:
            raise LeadControlConfigurationError(
                f"Конфигурация контроля лидов не содержит уникальный статус {status_id}; проверьте crm_pipeline_map.json"
            )
        stage = rows[0]
        name = str(stage.get("name") or stage.get("NAME") or "")
        semantics = stage.get("semantics") if "semantics" in stage else (stage.get("raw") or {}).get("SEMANTICS")
        if name != contract["name"] or semantics != contract["semantics"]:
            raise LeadControlConfigurationError(
                f"Конфигурация контроля лидов не совпадает со статусом {status_id}; проверьте crm_pipeline_map.json"
            )
    return {
        "stage_ids": list(expected),
        "required_statuses": expected,
    }


def resolve_auto_lead_ids(ids: list[str]) -> list[str]:
    """Read only IDs in auto mode to enforce the lead batch cap before enqueue."""
    wanted = _normalize_id_list(ids)
    if not wanted:
        return []
    rows = make_client().list_all(
        "crm.lead.list",
        {"filter": {"ID": wanted}, "select": ["ID"]},
    )
    found = {str(row.get("ID") or "") for row in rows if row.get("ID") is not None}
    return [entity_id for entity_id in wanted if entity_id in found]

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
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
LEAD_OWNER_TYPE_ID = 1
DEAL_OWNER_TYPE_ID = 2

SIGNAL_PRIORITY: dict[str, tuple[str, int, str]] = {
    "overdue_task": ("high", 100, "Есть просроченная открытая задача"),
    "questionable_closure": ("high", 95, "Закрытие требует проверки РОПом"),
    "negative_fresh_lead": ("high", 90, "Свежий лид закрыт с негативным статусом"),
    "payment_without_movement": ("high", 85, "Обещание оплаты без зафиксированного движения"),
    "post_proposal_without_control": ("medium", 75, "После КП нет контрольной даты"),
    "control_date_due": ("medium", 70, "Наступила дата контроля РОПа"),
    "postponed_without_date": ("medium", 65, "Отложено без даты возврата"),
    "no_dated_next_step": ("medium", 60, "Нет открытого следующего шага с датой"),
    "call_method_gap": ("medium", 55, "Методика дозвона выполнена не полностью"),
    "review_reason": ("low", 40, "Причину закрытия стоит перепроверить"),
    "meaningful_change_after_review": ("medium", 70, "После решения РОПа изменились существенные поля"),
}


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


def format_candidate_amount(value: Any, currency: Any) -> str:
    if value in (None, ""):
        return ""
    return f"{value} {str(currency or 'RUB').strip()}".strip()


def load_pipeline_map() -> dict[str, Any]:
    if not PIPELINE_MAP_PATH.exists():
        return {}
    try:
        payload = json.loads(PIPELINE_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def list_crm_pipelines() -> dict[str, Any]:
    """Справочник воронок/этапов для UI-фильтров кандидатов."""
    payload = load_pipeline_map()
    deal_pipelines = []
    for pipeline in payload.get("deal_pipelines") or []:
        if not isinstance(pipeline, dict):
            continue
        pipeline_id = str(pipeline.get("id") or "")
        if not pipeline_id:
            continue
        stages = []
        for stage in pipeline.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            status_id = str(stage.get("status_id") or stage.get("STATUS_ID") or "")
            name = str(stage.get("name") or stage.get("NAME") or status_id)
            if status_id:
                stages.append({"id": status_id, "name": name})
        deal_pipelines.append(
            {
                "id": pipeline_id,
                "name": str(pipeline.get("name") or f"Воронка {pipeline_id}"),
                "stages": stages,
            }
        )

    lead_pipeline = payload.get("lead_pipeline") if isinstance(payload.get("lead_pipeline"), dict) else {}
    lead_stages = []
    for stage in lead_pipeline.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        status_id = str(stage.get("status_id") or stage.get("STATUS_ID") or "")
        name = str(stage.get("name") or stage.get("NAME") or status_id)
        if status_id:
            lead_stages.append({"id": status_id, "name": name})

    return {
        "deal_pipelines": deal_pipelines,
        "lead_pipeline": {
            "id": "lead",
            "name": str(lead_pipeline.get("name") or "Лиды"),
            "stages": lead_stages,
        },
    }


def load_pipeline_stage_names() -> dict[str, str]:
    names: dict[str, str] = {}
    catalog = list_crm_pipelines()
    for pipeline in catalog.get("deal_pipelines") or []:
        for stage in pipeline.get("stages") or []:
            status_id = str(stage.get("id") or "")
            name = str(stage.get("name") or "")
            if status_id and name:
                names[status_id] = name
    for stage in (catalog.get("lead_pipeline") or {}).get("stages") or []:
        status_id = str(stage.get("id") or "")
        name = str(stage.get("name") or "")
        if status_id and name:
            names[status_id] = name
    return names


def _normalize_id_list(values: list[Any] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def candidates_filter_ready(
    *,
    entity_type: str,
    pipeline_ids: list[str] | None,
    stage_ids: list[str] | None,
) -> tuple[bool, str]:
    """
    Пока воронка/этапы не выбраны — не ищем.
    Лиды: нужны этапы.
    Сделки: нужны воронка(и) и этапы.
    """
    stages = _normalize_id_list(stage_ids)
    pipelines = _normalize_id_list(pipeline_ids)
    if entity_type == "lead":
        if not stages:
            return False, "Выберите этап(ы) лида — без этого поиск не запускаем"
        return True, ""
    if entity_type == "deal":
        if not pipelines:
            return False, "Выберите воронку(и) сделки"
        if not stages:
            return False, "Выберите этап(ы) в выбранных воронках"
        return True, ""
    return False, "Выберите тип: лиды или сделки"


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
    stage_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Кандидаты-лиды: DATE_CREATE / DATE_MODIFY + обязательные STATUS_ID.
    """
    filter_payload: dict[str, Any] = {}
    if created_days > 0:
        start_create = datetime.now(MSK_TZ) - timedelta(days=created_days)
        filter_payload[">=DATE_CREATE"] = date_for_bitrix(start_create)
    if modified_days > 0:
        start_modify = datetime.now(MSK_TZ) - timedelta(days=modified_days)
        filter_payload[">=DATE_MODIFY"] = date_for_bitrix(start_modify)
    stages = _normalize_id_list(stage_ids)
    if stages:
        filter_payload["STATUS_ID"] = stages

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
    pipeline_ids: list[str] | None = None,
    stage_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Кандидаты-сделки: DATE_CREATE / DATE_MODIFY + CATEGORY_ID + STAGE_ID.
    """
    filter_payload: dict[str, Any] = {}
    if created_days > 0:
        start_create = datetime.now(MSK_TZ) - timedelta(days=created_days)
        filter_payload[">=DATE_CREATE"] = date_for_bitrix(start_create)
    if modified_days > 0:
        start_modify = datetime.now(MSK_TZ) - timedelta(days=modified_days)
        filter_payload[">=DATE_MODIFY"] = date_for_bitrix(start_modify)
    pipelines = _normalize_id_list(pipeline_ids)
    stages = _normalize_id_list(stage_ids)
    if pipelines:
        filter_payload["CATEGORY_ID"] = pipelines
    if stages:
        filter_payload["STAGE_ID"] = stages

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

    amount_label = format_candidate_amount(amount, deal.get("CURRENCY_ID"))

    return {
        "entity_type": "deal",
        "entity_id": deal_id,
        "pipeline_id": str(deal.get("CATEGORY_ID") or ""),
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
            "pipeline_id": "lead",
            "title": str(lead.get("TITLE") or f"Лид {lead_id}"),
            "client_name": " ".join(
                part for part in [str(lead.get("NAME") or ""), str(lead.get("LAST_NAME") or "")] if part
            ).strip()
            or str(lead.get("TITLE") or ""),
            "status": status_name,
            "stage_id": status_id,
            "amount": format_candidate_amount(lead.get("OPPORTUNITY"), lead.get("CURRENCY_ID")),
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
        "pipeline_id": "lead",
        "title": str(lead.get("TITLE") or f"Лид {lead_id}"),
        "client_name": client_name or str(lead.get("TITLE") or ""),
        "status": status_name,
        "stage_id": status_id,
        "amount": format_candidate_amount(lead.get("OPPORTUNITY"), lead.get("CURRENCY_ID")),
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


def attach_saved_lead_qualification(
    candidates: list[dict[str, Any]],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Adds saved validated lead analysis metadata without calling CRM or LLM."""
    from api.jobs import (
        extract_lead_qualification_summary,
        extract_summary_fields,
        list_jobs,
        unwrap_analysis_payload,
    )

    running_ids: set[str] = set()
    for job in list_jobs(limit=10000):
        if str(job.get("status") or "") not in {"queued", "running"}:
            continue
        options = job.get("options") if isinstance(job.get("options"), dict) else {}
        ids = {str(value) for value in options.get("ids") or []}
        if str(options.get("entity_type") or "") == "lead":
            running_ids.update(ids)
        elif str(options.get("entity_type") or "") == "auto":
            progress = job.get("entity_progress") if isinstance(job.get("entity_progress"), dict) else {}
            running_ids.update(entity_id for entity_id in ids if f"lead:{entity_id}" in progress)

    for item in candidates:
        if item.get("entity_type") != "lead":
            continue
        entity_id = str(item.get("entity_id") or "")
        report = get_latest_ui_report(db_path, entity_type="lead", entity_id=entity_id)
        payload = report.get("report_json") if isinstance(report, dict) else None
        analysis = unwrap_analysis_payload(payload) if isinstance(payload, dict) else {}
        has_analysis = isinstance(analysis, dict) and bool(analysis)
        item["latest_report_id"] = int(report["id"]) if report and report.get("id") is not None else None
        item["analyzed_at"] = str(report.get("created_at") or "") or None if report else None
        item["lead_analysis_available"] = has_analysis
        if entity_id in running_ids:
            item["analysis_status"] = "running"
        elif str(item.get("analysis_freshness") or "") == "failed":
            item["analysis_status"] = "error"
        else:
            item["analysis_status"] = "ready" if has_analysis else "not_analyzed"
        if not has_analysis:
            item["lead_category"] = None
            item["lead_qualification"] = None
            item["lead_issue"] = None
            item["lead_next_action"] = None
            item["lead_closure_verdict"] = None
            item["lead_closure_reason"] = None
            continue

        legacy_summary = extract_summary_fields(analysis, "lead")
        item["lead_category"] = legacy_summary.get("lead_category")
        item["lead_issue"] = str(legacy_summary.get("attention_reason") or "").strip()[:320] or None
        item["lead_next_action"] = str(legacy_summary.get("recommended_action") or "").strip()[:320] or None
        closure = analysis.get("closure_review") if isinstance(analysis.get("closure_review"), dict) else {}
        verdict = str(closure.get("verdict") or "")
        item["lead_closure_verdict"] = verdict if verdict in {
            "confirmed_correct", "disputed", "insufficient_evidence", "not_applicable"
        } else None
        item["lead_closure_reason"] = str(closure.get("reason") or "").strip()[:320] or None
        summary = extract_lead_qualification_summary(analysis)
        if summary:
            item["lead_qualification"] = summary
            item["lead_category"] = summary.get("category")
    return candidates


def lead_qualification_matches(item: dict[str, Any], *, categories: set[str], bant_filter: str) -> bool:
    if item.get("entity_type") != "lead":
        return not categories and not bant_filter
    summary = item.get("lead_qualification") if isinstance(item.get("lead_qualification"), dict) else None
    category = str(item.get("lead_category") or (summary or {}).get("category") or "unknown")
    if categories and category not in categories:
        return False
    if not summary:
        return not bant_filter
    if not bant_filter:
        return True
    statuses = summary.get("statuses") if isinstance(summary.get("statuses"), dict) else {}
    values = [str(statuses.get(key) or "unknown") for key in ("budget", "authority", "need", "timeframe")]
    if bant_filter == "complete":
        return all(value == "confirmed" for value in values)
    if bant_filter == "incomplete":
        return any(value != "confirmed" for value in values)
    if bant_filter in {"budget", "authority", "need", "timeframe"}:
        return str(statuses.get(bant_filter) or "unknown") != "confirmed"
    if bant_filter == "negative":
        return "negative" in values
    if bant_filter == "unknown":
        return "unknown" in values
    return True


def apply_candidate_review_states(
    candidates: list[dict[str, Any]],
    *,
    entity_type: str,
    view: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Исключает только проверенные РОПом сущности, не целые этапы CRM."""
    reviews = get_candidate_review_states(
        DEFAULT_DB_PATH,
        entity_type=entity_type,
        entity_ids=[str(item.get("entity_id") or "") for item in candidates],
    )
    today = datetime.now(MSK_TZ).date().isoformat()
    result: list[dict[str, Any]] = []
    summary = {"reviewed_hidden": 0, "reviewed_visible": 0, "changed_after_review": 0, "crm_updated_after_review": 0}

    for item in candidates:
        review = reviews.get(str(item.get("entity_id") or ""))
        if not review or review.get("state") == "active":
            result.append(item)
            continue

        stage_changed = bool(
            review.get("reviewed_stage_id")
            and str(review.get("reviewed_stage_id")) != str(item.get("stage_id") or "")
        )
        pipeline_changed = bool(
            review.get("reviewed_pipeline_id")
            and str(review.get("reviewed_pipeline_id")) != str(item.get("pipeline_id") or "")
        )
        amount_changed = bool(
            review.get("reviewed_amount")
            and str(review.get("reviewed_amount")) != str(item.get("amount") or "")
        )
        control_due = bool(review.get("state") == "snoozed" and str(review.get("next_control_date") or "") <= today)
        changed_reasons = []
        if stage_changed or pipeline_changed:
            changed_reasons.append("изменилась стадия")
        if amount_changed:
            changed_reasons.append("изменилась сумма")
        if control_due:
            changed_reasons.append("наступила дата контроля")
        if changed_reasons:
            item["review_state"] = "changed"
            item["review_change_reason"] = ", ".join(changed_reasons)
            summary["changed_after_review"] += 1
            if view != "reviewed":
                result.append(item)
            continue

        reviewed_modify = str(review.get("reviewed_date_modify") or "")
        if reviewed_modify and str(item.get("date_modify") or "") > reviewed_modify:
            item["crm_updated_after_review"] = True
            summary["crm_updated_after_review"] += 1

        item["review_state"] = str(review.get("state") or "reviewed")
        item["review_decision"] = str(review.get("decision") or "Проверено РОПом")
        item["reviewed_at"] = str(review.get("updated_at") or "")
        if view in {"reviewed", "all"}:
            summary["reviewed_visible"] += 1
            result.append(item)
        else:
            summary["reviewed_hidden"] += 1

    return result, summary


def profile_period_bounds(
    preset: str,
    *,
    now: datetime | None = None,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, str]:
    """Возвращает полуоткрытое календарное окно [from, to) в timezone профиля."""
    timezone = ZoneInfo(timezone_name)
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    current = current.astimezone(timezone)
    today_start = datetime.combine(current.date(), time.min, timezone)
    included_dates: list[date] = []
    if preset == "today":
        period_from, period_to = today_start, today_start + timedelta(days=1)
    elif preset in {"previous_workday", "today_and_previous_workday"}:
        previous_date = current.date() - timedelta(days=1)
        while previous_date.weekday() >= 5:
            previous_date -= timedelta(days=1)
        if preset == "previous_workday" or current.date().weekday() >= 5:
            included_dates = [previous_date]
        else:
            included_dates = [previous_date, current.date()]
        period_from = datetime.combine(included_dates[0], time.min, timezone)
        period_to = datetime.combine(included_dates[-1] + timedelta(days=1), time.min, timezone)
    else:
        raise ValueError(f"Неизвестный период профиля: {preset}")
    return {
        "preset": preset,
        "timezone": timezone_name,
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "as_of": current.isoformat(timespec="seconds"),
        **({"included_dates": ",".join(item.isoformat() for item in included_dates)} if included_dates else {}),
    }


def custom_period_bounds(
    date_from: date,
    date_to: date,
    *,
    now: datetime | None = None,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, str]:
    """Возвращает включительный пользовательский диапазон дат как окно [from, to)."""
    if date_from > date_to:
        raise ValueError("Начало произвольного периода не может быть позже окончания")
    timezone = ZoneInfo(timezone_name)
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    current = current.astimezone(timezone)
    return {
        "preset": "custom",
        "timezone": timezone_name,
        "period_from": datetime.combine(date_from, time.min, timezone).isoformat(),
        "period_to": datetime.combine(date_to + timedelta(days=1), time.min, timezone).isoformat(),
        "as_of": current.isoformat(timespec="seconds"),
    }


def _in_period(value: Any, period: dict[str, str]) -> bool:
    parsed = parse_bitrix_dt(value)
    if parsed is None:
        return False
    timezone = ZoneInfo(period["timezone"])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    start = datetime.fromisoformat(period["period_from"])
    end = datetime.fromisoformat(period["period_to"])
    localized = parsed.astimezone(timezone)
    if not (start <= localized < end):
        return False
    included_dates = {item for item in str(period.get("included_dates") or "").split(",") if item}
    return not included_dates or localized.date().isoformat() in included_dates


def resolve_excluded_lead_sources(
    client: BitrixReadOnlyClient,
    lead_profile: dict[str, Any],
) -> tuple[set[str], list[dict[str, str]]]:
    """Разрешает DMP/DMP1 по live справочнику; сохранённые ID остаются fallback."""
    excluded = {str(item) for item in lead_profile.get("excluded_source_ids") or [] if str(item)}
    requested = {str(item).strip().upper() for item in lead_profile.get("excluded_source_codes") or [] if str(item)}
    resolved: list[dict[str, str]] = []
    if requested:
        for row in client.list_all("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}}):
            source_id = str(row.get("STATUS_ID") or "").strip()
            source_name = str(row.get("NAME") or "").strip()
            normalized_id = source_id.upper()
            normalized_name = source_name.upper().strip()
            matched = next(
                (
                    code for code in requested
                    if code == normalized_id
                    or normalized_name == code
                    or normalized_name.startswith(f"{code} ")
                    or normalized_name.startswith(f"{code}-")
                ),
                None,
            )
            if matched and source_id:
                excluded.add(source_id)
                resolved.append({"code": matched, "id": source_id, "name": source_name})
    return excluded, resolved


def resolve_excluded_lead_statuses(
    client: BitrixReadOnlyClient,
    lead_profile: dict[str, Any],
    *,
    required_statuses: dict[str, dict[str, Any]] | None = None,
) -> tuple[set[str], list[dict[str, str]]]:
    excluded = {str(item) for item in lead_profile.get("excluded_status_ids") or [] if str(item)}
    requested = {str(item).strip().lower() for item in lead_profile.get("excluded_status_names") or [] if str(item)}
    resolved: list[dict[str, str]] = []
    status_rows = client.list_all("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
    if required_statuses:
        by_id = {
            str(row.get("STATUS_ID") or "").strip(): row
            for row in status_rows
            if str(row.get("STATUS_ID") or "").strip()
        }
        for status_id, contract in required_statuses.items():
            row = by_id.get(status_id)
            if (
                row is None
                or str(row.get("NAME") or "").strip() != str(contract.get("name") or "")
                or "SEMANTICS" not in row
                or row.get("SEMANTICS") != contract.get("semantics")
            ):
                raise LeadControlConfigurationError(
                    f"Справочник Bitrix не совпадает со статусом {status_id} группы контроля лидов; поиск остановлен"
                )
    for row in status_rows:
        status_id = str(row.get("STATUS_ID") or "").strip()
        status_name = str(row.get("NAME") or "").strip()
        normalized = " ".join(status_name.lower().split())
        matched = next((name for name in requested if normalized == name), None)
        if matched and status_id:
            excluded.add(status_id)
            resolved.append({"name": matched, "id": status_id, "crm_name": status_name})
    return excluded, resolved


def fetch_profile_leads(
    client: BitrixReadOnlyClient,
    *,
    profile: dict[str, Any],
    period: dict[str, str],
    required_statuses: dict[str, dict[str, Any]] | None = None,
    manager_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lead_profile = profile.get("lead") if isinstance(profile.get("lead"), dict) else {}
    if not lead_profile.get("enabled", True):
        return [], {"excluded_sources": [], "raw": 0, "selected": 0}
    control_scope = bool(required_statuses)
    excluded_sources, resolved = (
        (set(), []) if control_scope else resolve_excluded_lead_sources(client, lead_profile)
    )
    excluded_statuses, resolved_statuses = resolve_excluded_lead_statuses(
        client,
        lead_profile,
        required_statuses=required_statuses,
    )
    filter_payload: dict[str, Any] = {
        ">=DATE_CREATE": period["period_from"],
        "<DATE_CREATE": period["period_to"],
    }
    stages = _normalize_id_list(lead_profile.get("stage_ids"))
    if stages and not lead_profile.get("all_stages", False):
        filter_payload["STATUS_ID"] = stages
    if manager_id:
        filter_payload["ASSIGNED_BY_ID"] = str(manager_id)
    rows = client.list_all(
        "crm.lead.list",
        {
            "order": {"DATE_CREATE": "DESC", "ID": "DESC"},
            "filter": filter_payload,
            "select": [
                "ID", "TITLE", "NAME", "LAST_NAME", "STATUS_ID", "STATUS_SEMANTIC_ID",
                "SOURCE_ID", "ASSIGNED_BY_ID", "OPPORTUNITY", "CURRENCY_ID", "DATE_CREATE",
                "DATE_MODIFY",
            ],
        },
    )
    selected = [
        row for row in rows
        if (control_scope or str(row.get("SOURCE_ID") or "") not in excluded_sources)
        and str(row.get("STATUS_ID") or "") not in excluded_statuses
        and (not control_scope or str(row.get("STATUS_ID") or "") in required_statuses)
        and _in_period(row.get("DATE_CREATE"), period)
    ]
    return selected, {
        "raw": len(rows),
        "selected": len(selected),
        "excluded_by_source": sum(1 for row in rows if str(row.get("SOURCE_ID") or "") in excluded_sources),
        "excluded_by_status": sum(1 for row in rows if str(row.get("STATUS_ID") or "") in excluded_statuses),
        "excluded_sources": sorted(excluded_sources),
        "resolved_sources": resolved,
        "resolved_statuses": resolved_statuses,
    }


def fetch_profile_deals(
    client: BitrixReadOnlyClient,
    *,
    profile: dict[str, Any],
    period: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    deal_profile = profile.get("deal") if isinstance(profile.get("deal"), dict) else {}
    if not deal_profile.get("enabled", True):
        return [], {"fresh": 0, "portfolio": 0, "selected": 0}
    pipelines = _normalize_id_list(deal_profile.get("pipeline_ids"))
    stages = _normalize_id_list(deal_profile.get("stage_ids"))
    select = [
        "ID", "TITLE", "LEAD_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CATEGORY_ID", "CLOSED",
        "OPPORTUNITY", "CURRENCY_ID", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY", "CLOSEDATE",
    ]
    common_filter: dict[str, Any] = {}
    if pipelines:
        common_filter["CATEGORY_ID"] = pipelines
    if stages:
        common_filter["STAGE_ID"] = stages
    fresh: list[dict[str, Any]] = []
    if deal_profile.get("include_fresh_deals", True):
        fresh_filter = {
            **common_filter,
            ">=DATE_CREATE": period["period_from"],
            "<DATE_CREATE": period["period_to"],
        }
        fresh = client.list_all(
            "crm.deal.list",
            {"order": {"DATE_CREATE": "DESC", "ID": "DESC"}, "filter": fresh_filter, "select": select},
        )
        fresh = [row for row in fresh if _in_period(row.get("DATE_CREATE"), period)]
    portfolio: list[dict[str, Any]] = []
    if deal_profile.get("include_portfolio", True) and deal_profile.get("include_all_active", True):
        portfolio_filter = {**common_filter, "CLOSED": "N"}
        portfolio = client.list_all(
            "crm.deal.list",
            {"order": {"DATE_MODIFY": "DESC", "ID": "DESC"}, "filter": portfolio_filter, "select": select},
        )
    by_id: dict[str, dict[str, Any]] = {}
    for row in [*fresh, *portfolio]:
        entity_id = str(row.get("ID") or "")
        if entity_id:
            by_id[entity_id] = row
    return list(by_id.values()), {"fresh": len(fresh), "portfolio": len(portfolio), "selected": len(by_id)}


def fetch_lead_handoffs(
    client: BitrixReadOnlyClient,
    leads: list[dict[str, Any]],
    *,
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lead_ids = [str(item.get("ID") or "") for item in leads if str(item.get("ID") or "")]
    if not lead_ids:
        return [], []
    deal_profile = profile.get("deal") if isinstance(profile.get("deal"), dict) else {}
    selected_pipelines = set(_normalize_id_list(deal_profile.get("pipeline_ids")))
    selected_stages = set(_normalize_id_list(deal_profile.get("stage_ids")))
    rows = client.list_all(
        "crm.deal.list",
        {
            "order": {"DATE_CREATE": "DESC", "ID": "DESC"},
            "filter": {"LEAD_ID": lead_ids},
            "select": [
                "ID", "TITLE", "LEAD_ID", "STAGE_ID", "STAGE_SEMANTIC_ID", "CATEGORY_ID", "CLOSED",
                "OPPORTUNITY", "CURRENCY_ID", "ASSIGNED_BY_ID", "DATE_CREATE", "DATE_MODIFY", "CLOSEDATE",
            ],
        },
    )
    in_scope, outside = [], []
    for row in rows:
        pipeline_ok = not selected_pipelines or str(row.get("CATEGORY_ID") or "") in selected_pipelines
        stage_ok = not selected_stages or str(row.get("STAGE_ID") or "") in selected_stages
        (in_scope if pipeline_ok and stage_ok else outside).append(row)
    return in_scope, outside


def fetch_candidate_activities(
    client: BitrixReadOnlyClient,
    entity_type: str,
    entity_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    owner_type_id = LEAD_OWNER_TYPE_ID if entity_type == "lead" else DEAL_OWNER_TYPE_ID
    response = client.safe_list_all(
        "crm.activity.list",
        {
            "order": {"START_TIME": "ASC", "DEADLINE": "ASC", "ID": "ASC"},
            "filter": {"OWNER_TYPE_ID": owner_type_id, "OWNER_ID": str(entity_id)},
            "select": [
                "ID", "OWNER_ID", "TYPE_ID", "PROVIDER_ID", "PROVIDER_TYPE_ID", "SUBJECT", "DESCRIPTION",
                "ASSOCIATED_ENTITY_ID", "RESPONSIBLE_ID",
                "DIRECTION", "COMPLETED", "START_TIME", "END_TIME", "DEADLINE", "CREATED", "LAST_UPDATED",
                "FILES", "RESULT_STATUS", "RESULT_MARK", "SETTINGS", "PROVIDER_PARAMS", "PROVIDER_DATA",
                "COMMUNICATIONS",
            ],
        },
    )
    return list(response.get("items") or []), None if response.get("ok") else str(response.get("error") or "activity unavailable")


def fetch_candidate_activities_bulk(
    client: BitrixReadOnlyClient,
    entities: list[tuple[str, dict[str, Any]]],
    *,
    additional_filter: dict[str, Any] | None = None,
) -> dict[tuple[str, str], tuple[list[dict[str, Any]], str | None]]:
    """Два paginated read-only запроса вместо N запросов; fallback остаётся точечным."""
    result: dict[tuple[str, str], tuple[list[dict[str, Any]], str | None]] = {}
    select = [
        "ID", "OWNER_ID", "TYPE_ID", "PROVIDER_ID", "PROVIDER_TYPE_ID", "SUBJECT", "DESCRIPTION",
        "ASSOCIATED_ENTITY_ID", "RESPONSIBLE_ID",
        "DIRECTION", "COMPLETED", "START_TIME", "END_TIME", "DEADLINE", "CREATED", "LAST_UPDATED",
        "FILES", "RESULT_STATUS", "RESULT_MARK", "SETTINGS", "PROVIDER_PARAMS", "PROVIDER_DATA",
        "COMMUNICATIONS",
    ]
    for entity_type, owner_type_id in (("lead", LEAD_OWNER_TYPE_ID), ("deal", DEAL_OWNER_TYPE_ID)):
        ids = [str(row.get("ID") or "") for kind, row in entities if kind == entity_type and str(row.get("ID") or "")]
        if not ids:
            continue
        response = client.safe_list_all(
            "crm.activity.list",
            {
                "order": {"OWNER_ID": "ASC", "START_TIME": "ASC", "ID": "ASC"},
                "filter": {
                    **dict(additional_filter or {}),
                    "OWNER_TYPE_ID": owner_type_id,
                    "OWNER_ID": ids,
                },
                "select": select,
            },
        )
        if not response.get("ok"):
            error = str(response.get("error") or "activity unavailable")
            for entity_id in ids:
                result[(entity_type, entity_id)] = ([], error)
            continue
        grouped: dict[str, list[dict[str, Any]]] = {entity_id: [] for entity_id in ids}
        for activity in response.get("items") or []:
            grouped.setdefault(str(activity.get("OWNER_ID") or ""), []).append(activity)
        for entity_id in ids:
            result[(entity_type, entity_id)] = (grouped.get(entity_id, []), None)
    return result


def _activity_dt(activity: dict[str, Any]) -> datetime | None:
    for key in ("DEADLINE", "START_TIME", "END_TIME", "CREATED", "LAST_UPDATED"):
        value = parse_bitrix_dt(activity.get(key))
        if value is not None:
            return value if value.tzinfo else value.replace(tzinfo=MOSCOW_TZ)
    return None


def _is_completed(activity: dict[str, Any]) -> bool:
    return str(activity.get("COMPLETED") or "").upper() in {"Y", "1", "TRUE"}


def _is_call(activity: dict[str, Any]) -> bool:
    haystack = " ".join(str(activity.get(key) or "") for key in ("TYPE_ID", "PROVIDER_ID", "PROVIDER_TYPE_ID", "SUBJECT")).upper()
    return str(activity.get("TYPE_ID") or "") == "2" or "CALL" in haystack or "ЗВОН" in haystack


def evaluate_call_method(
    activities: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(MOSCOW_TZ)
    calls = [item for item in activities if _is_call(item)]
    incoming = [item for item in calls if str(item.get("DIRECTION") or "") == "1"]
    outgoing = [item for item in calls if str(item.get("DIRECTION") or "") == "2"]
    completed = [item for item in calls if _is_completed(item)]
    unsuccessful_tokens = ("НЕ ОТВЕТ", "НЕДОЗВОН", "ЗАНЯТ", "BUSY", "NO ANSWER", "INVALID", "НЕВЕРН")
    unsuccessful = [
        item for item in calls
        if any(token in " ".join(str(item.get(key) or "") for key in ("SUBJECT", "DESCRIPTION", "PROVIDER_TYPE_ID")).upper() for token in unsuccessful_tokens)
    ]
    last_dt = max((_activity_dt(item) for item in calls if _activity_dt(item)), default=None)
    gap_hours = None if last_dt is None else max(0.0, (current - last_dt.astimezone(current.tzinfo or MOSCOW_TZ)).total_seconds() / 3600)
    # Это мягкий операционный сигнал. Без транскрипта «содержательный контакт» не утверждаем.
    method_gap = bool(
        calls
        and (unsuccessful or not completed)
        and (len(outgoing) < 3 or (gap_hours is not None and gap_hours >= 4))
    )
    return {
        "attempts": len(calls),
        "incoming": len(incoming),
        "outgoing": len(outgoing),
        "completed_activities": len(completed),
        "unsuccessful_attempts": len(unsuccessful),
        "meaningful_contact": None,
        "last_attempt_at": last_dt.isoformat() if last_dt else None,
        "gap_hours": None if gap_hours is None else round(gap_hours, 1),
        "method_gap": method_gap,
    }


def detect_candidate_signals(
    entity: dict[str, Any],
    *,
    entity_type: str,
    stage_name: str,
    activities: list[dict[str, Any]],
    period: dict[str, str],
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    current = now or datetime.fromisoformat(period["as_of"])
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW_TZ)
    open_activities = [item for item in activities if not _is_completed(item)]
    future = [item for item in open_activities if (_activity_dt(item) or current) >= current]
    overdue = [item for item in open_activities if _activity_dt(item) and _activity_dt(item) < current]
    signals: list[dict[str, Any]] = []

    def add(code: str, detail: str | None = None) -> None:
        priority, score, label = SIGNAL_PRIORITY[code]
        signals.append({"reason_code": code, "priority": priority, "score": score, "label": label, "detail": detail})

    if overdue:
        add("overdue_task", f"Просрочено активностей: {len(overdue)}")
    if not future:
        add("no_dated_next_step")

    semantic = str(entity.get("STATUS_SEMANTIC_ID") if entity_type == "lead" else entity.get("STAGE_SEMANTIC_ID") or "").upper()
    if entity_type == "lead" and semantic == "F" and _in_period(entity.get("DATE_CREATE"), period):
        add("negative_fresh_lead")
    if entity_type == "deal":
        stage_id = str(entity.get("STAGE_ID") or "")
        reason = closed_reason_from_stage(stage_id, stage_name)
        normalized_stage = stage_name.upper()
        if "КП" in normalized_stage and not future:
            add("post_proposal_without_control")
        payment_mentions = [
            item for item in activities
            if "ОПЛАТ" in " ".join(str(item.get(key) or "") for key in ("SUBJECT", "DESCRIPTION")).upper()
            and any(token in " ".join(str(item.get(key) or "") for key in ("SUBJECT", "DESCRIPTION")).upper() for token in ("ОБЕЩ", "ОЖИД", "ПЛАН"))
        ]
        if payment_mentions and "ОПЛАТА ПОЛУЧЕНА" not in normalized_stage and not future:
            add("payment_without_movement")
        if reason in HIGH_CLOSED_REASONS:
            add("questionable_closure", HIGH_CLOSED_REASONS[reason][1])
        elif reason == "postponed" and not future:
            add("postponed_without_date")
        elif reason in MEDIUM_CLOSED_REASONS:
            add("review_reason", MEDIUM_CLOSED_REASONS[reason][1])

    call_method = evaluate_call_method(activities, now=current)
    if call_method["method_gap"]:
        add("call_method_gap", f"Исходящих попыток: {call_method['outgoing']}; gap: {call_method['gap_hours']} ч")
    return signals, call_method


def candidate_freshness(
    entity_type: str,
    entity: dict[str, Any],
    *,
    activities: list[dict[str, Any]] | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> str:
    entity_id = str(entity.get("ID") or "")
    state = get_entity_state(db_path, entity_type, entity_id) if entity_id else None
    if not state:
        return "missing"
    if str(state.get("last_analysis_status") or "").lower() in {"error", "failed"}:
        return "failed"
    if not get_latest_ui_report(db_path, entity_type=entity_type, entity_id=entity_id):
        return "missing"
    previous = state.get("snapshot") if isinstance(state.get("snapshot"), dict) else {}
    previous_entity = previous.get(entity_type) if isinstance(previous.get(entity_type), dict) else {}
    pairs = (
        ("status_id" if entity_type == "lead" else "stage_id", entity.get("STATUS_ID") if entity_type == "lead" else entity.get("STAGE_ID")),
        ("category_id", None if entity_type == "lead" else entity.get("CATEGORY_ID")),
        ("opportunity", entity.get("OPPORTUNITY")),
    )
    meaningful = False
    for key, value in pairs:
        if value is None:
            continue
        old = previous_entity.get(key)
        if old is not None and str(old) != str(value or ""):
            meaningful = True
            break
    if meaningful:
        return "changed"
    if activities is not None and isinstance(previous.get("activities"), list):
        previous_activity_state = {
            str(item.get("id") or ""): (
                str(item.get("completed") or ""),
                str(item.get("deadline") or ""),
                str(item.get("start_time") or ""),
                str(item.get("last_updated") or ""),
                str(item.get("direction") or ""),
            )
            for item in previous["activities"]
            if isinstance(item, dict) and str(item.get("source") or entity_type) == entity_type
        }
        current_activity_state = {
            str(item.get("ID") or ""): (
                str(item.get("COMPLETED") or ""),
                str(item.get("DEADLINE") or ""),
                str(item.get("START_TIME") or ""),
                str(item.get("LAST_UPDATED") or ""),
                str(item.get("DIRECTION") or ""),
            )
            for item in activities
            if str(item.get("ID") or "")
        }
        if previous_activity_state != current_activity_state:
            return "changed"
    metadata = previous.get("metadata") if isinstance(previous.get("metadata"), dict) else {}
    previous_modify = metadata.get("date_modify")
    if previous_modify and str(previous_modify) != str(entity.get("DATE_MODIFY") or ""):
        return "date_modified_only"
    return "fresh" if state.get("last_analysis_at") or state.get("last_analysis") else "missing"


def build_profile_candidate(
    entity: dict[str, Any],
    *,
    entity_type: str,
    stage_names: dict[str, str],
    status_names: dict[str, str],
    activities: list[dict[str, Any]],
    activity_error: str | None,
    period: dict[str, str],
    db_path: str | Path = DEFAULT_DB_PATH,
    include_without_signals: bool = False,
    include_local_analysis_state: bool = True,
    include_activity_signals: bool = True,
) -> dict[str, Any] | None:
    entity_id = str(entity.get("ID") or "")
    if not entity_id:
        return None
    stage_id = str(entity.get("STATUS_ID") if entity_type == "lead" else entity.get("STAGE_ID") or "")
    stage_name = (status_names if entity_type == "lead" else stage_names).get(stage_id) or stage_id or "не указан"
    if include_activity_signals:
        signals, call_method = detect_candidate_signals(
            entity,
            entity_type=entity_type,
            stage_name=stage_name,
            activities=activities,
            period=period,
        )
    else:
        signals, call_method = [], {"method_gap": False}
    if not signals and not include_without_signals:
        return None
    signals.sort(key=lambda item: int(item["score"]), reverse=True)
    top = signals[0] if signals else {}
    freshness = (
        candidate_freshness(entity_type, entity, activities=activities, db_path=db_path)
        if include_local_analysis_state
        else "missing"
    )
    lead_id = str(entity.get("LEAD_ID") or "") if entity_type == "deal" else entity_id
    journey_key = f"lead:{lead_id}" if lead_id else f"deal:{entity_id}"
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "origin_lead_id": lead_id if lead_id else None,
        "journey_key": journey_key,
        "pipeline_id": "lead" if entity_type == "lead" else str(entity.get("CATEGORY_ID") or ""),
        "title": str(entity.get("TITLE") or f"{'Лид' if entity_type == 'lead' else 'Сделка'} {entity_id}"),
        "client_name": " ".join(str(entity.get(key) or "") for key in ("NAME", "LAST_NAME")).strip() or str(entity.get("TITLE") or ""),
        "status": stage_name,
        "stage_id": stage_id,
        "amount": format_candidate_amount(entity.get("OPPORTUNITY"), entity.get("CURRENCY_ID")),
        "manager_id": str(entity.get("ASSIGNED_BY_ID") or ""),
        "date_create": str(entity.get("DATE_CREATE") or ""),
        "date_modify": str(entity.get("DATE_MODIFY") or ""),
        "priority": top.get("priority", "low"),
        "score": sum(int(item["score"]) for item in signals),
        "attention_reason": str(top.get("label") or ""),
        "reasons": [str(item["label"]) for item in signals],
        "reason_codes": [str(item["reason_code"]) for item in signals],
        "signals": signals,
        "call_method": call_method,
        "activity_error": activity_error,
        "analysis_freshness": freshness,
        "bitrix_url": bitrix_entity_url(entity_type, entity_id),
        "analyzed": freshness in {"fresh", "date_modified_only"},
    }


def deduplicate_journeys(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сделка становится current entity для пути из лида; direct deals сохраняются отдельно."""
    result: dict[str, dict[str, Any]] = {}
    for item in sorted(candidates, key=lambda value: value.get("entity_type") == "deal"):
        key = str(item.get("journey_key") or f"{item.get('entity_type')}:{item.get('entity_id')}")
        previous = result.get(key)
        if previous and item.get("entity_type") == "deal":
            item["origin_lead"] = {
                "entity_id": previous.get("entity_id"),
                "title": previous.get("title"),
                "bitrix_url": previous.get("bitrix_url"),
            }
            item["signals"] = sorted(
                [*(previous.get("signals") or []), *(item.get("signals") or [])],
                key=lambda signal: int(signal.get("score") or 0),
                reverse=True,
            )
            item["reason_codes"] = list(dict.fromkeys([*(previous.get("reason_codes") or []), *(item.get("reason_codes") or [])]))
            item["reasons"] = list(dict.fromkeys([*(previous.get("reasons") or []), *(item.get("reasons") or [])]))
            item["score"] = sum(int(signal.get("score") or 0) for signal in item["signals"])
        if previous is None or item.get("entity_type") == "deal":
            result[key] = item
    return list(result.values())


def select_workset(candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
    workset_limit = max(1, int(limits.get("workset") or 15))
    new_limit = max(0, int(limits.get("new_slots") or 10))
    backlog_limit = max(0, int(limits.get("backlog_slots") or 5))
    selected: set[str] = set()
    new_count = backlog_count = 0
    for item in candidates:
        lifecycle = str(item.get("lifecycle") or "new")
        take = False
        if lifecycle == "new" and new_count < new_limit:
            new_count += 1
            take = True
        elif lifecycle != "new" and backlog_count < backlog_limit:
            backlog_count += 1
            take = True
        if take and len(selected) < workset_limit:
            selected.add(str(item.get("journey_key")))
    # Если одна корзина пуста, свободные места отдаются общему рейтингу.
    for item in candidates:
        if len(selected) >= workset_limit:
            break
        selected.add(str(item.get("journey_key")))
    for item in candidates:
        item["workset_selected"] = str(item.get("journey_key")) in selected
        if not item["workset_selected"]:
            item["capacity_state"] = "waiting_for_capacity"
    return candidates


def profile_candidates_preview(
    profile_record: dict[str, Any],
    *,
    client: BitrixReadOnlyClient | None = None,
    now: datetime | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    period_override: dict[str, str] | None = None,
    required_lead_statuses: dict[str, dict[str, Any]] | None = None,
    lead_manager_id: str | None = None,
) -> dict[str, Any]:
    """Live read-only discovery профиля. Не вызывает OpenAI и ничего не пишет в Bitrix."""
    profile = profile_record.get("profile") if isinstance(profile_record.get("profile"), dict) else profile_record
    timezone_name = str(profile.get("timezone") or "Europe/Moscow")
    period = period_override or profile_period_bounds(
        str(profile.get("period_preset") or "today_and_previous_workday"),
        now=now,
        timezone_name=timezone_name,
    )
    crm = client or make_client()
    leads, lead_scope = fetch_profile_leads(
        crm,
        profile=profile,
        period=period,
        required_statuses=required_lead_statuses,
        manager_id=lead_manager_id,
    )
    control_scope = bool(required_lead_statuses)
    manager_names: dict[str, str] = {}
    if control_scope:
        manager_ids = {str(item.get("ASSIGNED_BY_ID") or "").strip() for item in leads}
        manager_ids.discard("")
        if manager_ids:
            try:
                with bitrix_trace_context(component="lead_control_managers"):
                    manager_names = fetch_user_names(crm, manager_ids)
            except Exception:  # noqa: BLE001 - owner names are optional; retain prior snapshot values on refresh
                manager_names = {}
    if control_scope:
        deals, deal_scope = [], {"fresh": 0, "portfolio": 0, "selected": 0}
    else:
        deals, deal_scope = fetch_profile_deals(crm, profile=profile, period=period)
    handoff_deals, outside_handoffs = ([], []) if control_scope else fetch_lead_handoffs(crm, leads, profile=profile)
    deal_by_id = {str(item.get("ID") or ""): item for item in [*deals, *handoff_deals] if str(item.get("ID") or "")}
    deals = list(deal_by_id.values())
    outside_lead_ids = {str(item.get("LEAD_ID") or "") for item in outside_handoffs}
    if outside_lead_ids:
        leads = [item for item in leads if str(item.get("ID") or "") not in outside_lead_ids]
    deal_scope["lead_handoffs_in_scope"] = len(handoff_deals)
    deal_scope["selected"] = len(deals)
    stage_names = {} if control_scope else load_pipeline_stage_names()
    status_names = (
        {status_id: str(contract.get("name") or status_id) for status_id, contract in required_lead_statuses.items()}
        if control_scope
        else load_status_map(crm, "STATUS") if leads else {}
    )
    catalog = {} if control_scope else list_crm_pipelines()
    known_pipelines = {str(item.get("id") or "") for item in catalog.get("deal_pipelines") or []}
    known_stages = set(stage_names)
    deal_profile = profile.get("deal") if isinstance(profile.get("deal"), dict) else {}
    missing_pipelines = sorted(set(_normalize_id_list(deal_profile.get("pipeline_ids"))) - known_pipelines)
    missing_stages = sorted(set(_normalize_id_list(deal_profile.get("stage_ids"))) - known_stages)
    entities = [(entity_type, row) for entity_type, rows in (("lead", leads), ("deal", deals)) for row in rows]

    if control_scope:
        activity_contexts = [(entity_type, row, [], None) for entity_type, row in entities]
    else:
        activity_lookup = fetch_candidate_activities_bulk(crm, entities)

        def load_activity_context(pair: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any], list[dict[str, Any]], str | None]:
            entity_type, row = pair
            entity_id = str(row.get("ID") or "")
            activities, error = activity_lookup.get((entity_type, entity_id), ([], "activity bulk response missing"))
            # Некоторые порталы не принимают OWNER_ID[]=...; только тогда делаем bounded fallback.
            if error:
                activities, error = fetch_candidate_activities(crm, entity_type, entity_id)
            return entity_type, row, activities, error

        failed_bulk = any(error for _, error in activity_lookup.values())
        if failed_bulk:
            with ThreadPoolExecutor(max_workers=min(6, max(1, len(entities)))) as executor:
                activity_contexts = list(executor.map(load_activity_context, entities))
        else:
            activity_contexts = [load_activity_context(pair) for pair in entities]

    cards: list[dict[str, Any]] = []
    activity_errors = 0
    for entity_type, row, activities, error in activity_contexts:
        activity_errors += int(bool(error))
        card = build_profile_candidate(
            row,
            entity_type=entity_type,
            stage_names=stage_names,
            status_names=status_names,
            activities=activities,
            activity_error=error,
            period=period,
            db_path=db_path,
            include_without_signals=control_scope,
            include_local_analysis_state=not control_scope,
            include_activity_signals=not control_scope,
        )
        if card:
            if control_scope:
                manager_name = manager_names.get(str(row.get("ASSIGNED_BY_ID") or ""))
                if manager_name:
                    card["manager_name"] = manager_name
            cards.append(card)
    if not control_scope:
        cards = deduplicate_journeys(cards)
        attach_saved_lead_qualification(cards, db_path=db_path)
        cards, review_summary = apply_profile_review_states(cards, view=str(profile.get("review_view") or "active"), db_path=db_path)
    else:
        review_summary = {"reviewed_hidden": 0, "reviewed_visible": 0, "reactivated": 0}
    for item in cards:
        stable_signal = {
            "entity_type": item.get("entity_type"),
            "entity_id": item.get("entity_id"),
            "stage_id": item.get("stage_id"),
            "pipeline_id": item.get("pipeline_id"),
            "reason_codes": sorted(item.get("reason_codes") or []),
        }
        item["signal_hash"] = hashlib.sha256(
            json.dumps(stable_signal, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
    if not control_scope:
        reconcile_candidate_cases(db_path, cards, as_of=period["as_of"])
    cards.sort(
        key=lambda item: (
            {"high": 3, "medium": 2, "low": 1}.get(str(item.get("priority")), 0),
            int(item.get("score") or 0),
            str(item.get("date_create") or ""),
        ),
        reverse=True,
    )
    if control_scope:
        return {
            "profile": {"id": profile_record.get("id"), "name": profile_record.get("name"), "version": profile_record.get("version")},
            "period": period,
            "scope": {
                "lead": lead_scope,
                "deal": deal_scope,
                "activity_errors": 0,
                "handoff_warning": {"outside_profile_count": 0, "lead_ids": [], "message": ""},
                "profile_drift": {"has_drift": False, "missing_pipeline_ids": [], "missing_stage_ids": [], "message": ""},
            },
            "summary": {
                "total": len(cards),
                "workset": len(cards),
                "reviewed_hidden": review_summary["reviewed_hidden"],
                "reviewed_visible": review_summary["reviewed_visible"],
                "reactivated": review_summary["reactivated"],
                "estimated_paid_entities": 0,
                "estimated_cost_rub": 0,
            },
            "candidates": cards,
            "manager_names": manager_names,
            "generated_at": period["as_of"],
            "llm_called": False,
        }
    select_workset(cards, profile.get("limits") if isinstance(profile.get("limits"), dict) else {})
    selected = [item for item in cards if item.get("workset_selected")]
    paid_run_limit = max(0, int((profile.get("limits") or {}).get("paid_per_run") or 5))
    paid_day_limit = max(0, int((profile.get("limits") or {}).get("paid_per_day") or 5))
    paid_used_today = daily_paid_capacity_used(db_path, day_prefix=period["as_of"][:10])
    paid_limit = min(paid_run_limit, max(0, paid_day_limit - paid_used_today))
    llm_required = [item for item in selected if item.get("analysis_freshness") in {"missing", "changed", "failed"}]
    estimated_entities = min(len(llm_required), paid_limit)
    per_entity_estimate = estimate_analysis_cost(
        ANALYSIS_MODEL,
        {"input_tokens": 25_000, "output_tokens": ANALYSIS_MAX_OUTPUT_TOKENS},
        USD_RUB_RATE,
    )
    per_entity_rub = per_entity_estimate.get("estimated_cost_rub")
    total_estimated_rub = None if per_entity_rub is None else round(float(per_entity_rub) * estimated_entities, 2)
    return {
        "profile": {
            "id": profile_record.get("id"),
            "name": profile_record.get("name"),
            "version": profile_record.get("version"),
        },
        "period": period,
        "scope": {
            "lead": lead_scope,
            "deal": deal_scope,
            "activity_errors": activity_errors,
            "handoff_warning": {
                "outside_profile_count": len(outside_handoffs),
                "lead_ids": sorted(outside_lead_ids)[:20],
                "message": "Свежие лиды перешли в сделки вне выбранной воронки/этапов и не включены в основные карточки."
                if outside_handoffs else "",
            },
            "profile_drift": {
                "has_drift": bool(missing_pipelines or missing_stages),
                "missing_pipeline_ids": missing_pipelines,
                "missing_stage_ids": missing_stages,
                "message": "Часть сохранённых ID отсутствует в локальной CRM-карте. Выборка не менялась автоматически; обновите карту и проверьте профиль."
                if missing_pipelines or missing_stages else "",
            },
        },
        "summary": {
            "total": len(cards),
            "workset": len(selected),
            "reserve": len(cards) - len(selected),
            "high": sum(1 for item in cards if item.get("priority") == "high"),
            "llm_required": len(llm_required),
            "llm_allowed_this_run": min(len(llm_required), paid_limit),
            "waiting_for_paid_capacity": max(0, len(llm_required) - paid_limit),
            **review_summary,
        },
        "cost_preview": {
            "paid_entity_limit": paid_limit,
            "paid_per_run_limit": paid_run_limit,
            "paid_per_day_limit": paid_day_limit,
            "paid_used_today": paid_used_today,
            "entities_requiring_llm": len(llm_required),
            "exact_cost_known": False,
            "model": ANALYSIS_MODEL,
            "assumed_input_tokens_per_entity": 25_000,
            "assumed_output_tokens_per_entity": ANALYSIS_MAX_OUTPUT_TOKENS,
            "estimated_cost_rub_per_entity": per_entity_rub,
            "estimated_cost_rub_total": total_estimated_rub,
            "message": "Оценка LLM консервативная и не включает возможную транскрибацию; точная стоимость зависит от подготовленного контекста. Платный запуск требует отдельного подтверждения.",
        },
        "candidates": cards,
        "generated_at": period["as_of"],
        "llm_called": False,
    }


def apply_profile_review_states(
    candidates: list[dict[str, Any]],
    *,
    view: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    summary = {"reviewed_hidden": 0, "reviewed_visible": 0, "reactivated": 0}
    result: list[dict[str, Any]] = []
    by_type: dict[str, list[dict[str, Any]]] = {"lead": [], "deal": []}
    for item in candidates:
        by_type.setdefault(str(item.get("entity_type")), []).append(item)
    for entity_type, rows in by_type.items():
        reviews = get_candidate_review_states(
            db_path,
            entity_type=entity_type,
            entity_ids=[str(item.get("entity_id") or "") for item in rows],
        )
        today = datetime.now(MOSCOW_TZ).date().isoformat()
        for item in rows:
            review = reviews.get(str(item.get("entity_id") or ""))
            if not review or review.get("state") == "active":
                item["lifecycle"] = "new"
                result.append(item)
                continue
            due = review.get("state") == "snoozed" and str(review.get("next_control_date") or "") <= today
            changed = item.get("analysis_freshness") == "changed"
            if due or changed:
                item["review_state"] = "changed"
                item["analysis_freshness"] = "changed"
                item["lifecycle"] = "reactivation"
                item.setdefault("reason_codes", []).append("meaningful_change_after_review")
                summary["reactivated"] += 1
                if view != "reviewed":
                    result.append(item)
                continue
            item["review_state"] = str(review.get("state") or "reviewed")
            item["analysis_freshness"] = "snoozed" if item["review_state"] == "snoozed" else "reviewed"
            item["lifecycle"] = "backlog"
            if view in {"reviewed", "all"}:
                summary["reviewed_visible"] += 1
                result.append(item)
            else:
                summary["reviewed_hidden"] += 1
    return result, summary


def _lead_control_period(*, now: datetime | None = None) -> dict[str, str]:
    current = now or datetime.now(MOSCOW_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MOSCOW_TZ)
    current = current.astimezone(MOSCOW_TZ)
    return {
        "timezone": "Europe/Moscow",
        "period_from": (current - timedelta(days=30)).isoformat(),
        "period_to": current.isoformat(),
        "as_of": current.isoformat(timespec="seconds"),
    }


def _lead_control_snapshot_cards(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Store Bitrix-derived rows only; current reports, jobs, and reviews are reattached on reads.
    mutable_fields = {
        "analysis_freshness", "analysis_status", "latest_report_id", "analyzed_at",
        "lead_analysis_available", "lead_category", "lead_qualification", "lead_issue",
        "lead_next_action", "lead_closure_verdict", "lead_closure_reason", "analyzed",
        "analysis_path", "review_state", "lifecycle", "workset_selected", "capacity_state",
    }
    return [
        {key: value for key, value in item.items() if key not in mutable_fields}
        for item in candidates
        if isinstance(item, dict) and item.get("entity_type") == "lead" and str(item.get("entity_id") or "")
    ]


def _lead_control_snapshot_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict) or not isinstance(state.get("payload"), dict):
        return None
    payload = state["payload"]
    candidates = payload.get("candidates")
    if payload.get("schema_version") != 1 or not isinstance(candidates, list):
        return None
    manager_names = {
        str(user_id): str(name).strip()
        for user_id, name in (payload.get("manager_names") or {}).items()
        if str(user_id).strip() and str(name).strip()
    } if isinstance(payload.get("manager_names"), dict) else {}
    normalized_candidates = [dict(item) for item in candidates if isinstance(item, dict)]
    for item in normalized_candidates:
        manager_id = str(item.get("manager_id") or "").strip()
        manager_name = str(item.get("manager_name") or "").strip()
        if manager_id and manager_name:
            manager_names.setdefault(manager_id, manager_name)
    return {
        "revision": int(state.get("revision") or 0),
        "crm_updated_at": str(payload.get("crm_updated_at") or "") or None,
        "candidates": normalized_candidates,
        "manager_names": manager_names,
    }


def lead_control_snapshot_candidate(lead_id: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    state = _lead_control_snapshot_state(get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY))
    return next(
        (item for item in (state or {}).get("candidates", []) if str(item.get("entity_id") or "") == str(lead_id)),
        None,
    )


def _lead_control_history_key(lead_id: str) -> str:
    return f"{LEAD_CONTROL_HISTORY_KEY_PREFIX}{lead_id}"


def _lead_control_history_state(state: dict[str, Any] | None, *, lead_id: str) -> dict[str, Any] | None:
    if not isinstance(state, dict) or not isinstance(state.get("payload"), dict):
        return None
    payload = state["payload"]
    if payload.get("schema_version") != 1 or str(payload.get("lead_id") or "") != lead_id:
        return None
    comments = payload.get("comments")
    communications = payload.get("communications")
    if not isinstance(comments, list) or not isinstance(communications, list):
        return None
    task = payload.get("current_task")
    return {
        "lead_id": lead_id,
        "available": bool(payload.get("available")),
        "comments": [dict(item) for item in comments if isinstance(item, dict)],
        "communications": [dict(item) for item in communications if isinstance(item, dict)],
        "current_task": dict(task) if isinstance(task, dict) else None,
        "task_checked": bool(payload.get("task_checked")),
        "limitations": [str(item) for item in payload.get("limitations") or [] if str(item).strip()],
        "fetched_at": str(payload.get("fetched_at") or "") or None,
        "revision": int(state.get("revision") or 0),
    }


def _lead_control_current_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    def raw_of(item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("raw")
        return raw if isinstance(raw, dict) else {}

    def completed(item: dict[str, Any]) -> bool:
        raw = raw_of(item)
        value = str(item.get("completed") or raw.get("COMPLETED") or "").strip().upper()
        return value in {"Y", "1", "TRUE", "COMPLETED"}

    def task_time(item: dict[str, Any]) -> tuple[bool, str, str]:
        raw = raw_of(item)
        deadline = str(raw.get("DEADLINE") or "").strip()
        fallback = str(item.get("when") or raw.get("START_TIME") or "").strip()
        return (not bool(deadline), deadline or fallback, str(item.get("id") or ""))

    open_tasks = [item for item in tasks if not completed(item)]
    task = min(open_tasks, key=task_time, default=None)
    if not isinstance(task, dict):
        return None
    raw = raw_of(task)
    return {
        "subject": str(task.get("subject") or raw.get("SUBJECT") or "").strip(),
        "deadline": str(raw.get("DEADLINE") or "") or None,
        "status": "open",
    }


def _project_lead_control_history(
    lead_id: str,
    activity_response: dict[str, Any],
    comment_response: dict[str, Any],
) -> dict[str, Any]:
    activity_ok = bool(activity_response.get("ok"))
    comments_ok = bool(comment_response.get("ok"))
    history = {
        "activities_by_entity": {
            f"lead:{lead_id}": {
                "entity_type": "lead",
                "entity_id": lead_id,
                "activities": activity_response,
                "timeline_comments": [comment_response],
            }
        }
    }
    sections = build_history_sections(history)
    bundle = {**sections, "lead": {}}
    events = build_normalized_communications(bundle)
    activity_text_by_id = {
        str(item.get("ID") or ""): clean_text(item.get("DESCRIPTION"))
        for item in result_items(activity_response)
        if str(item.get("ID") or "")
    }
    comment_text_by_id = {
        str(item.get("ID") or ""): clean_text(item.get("COMMENT") or item.get("TEXT") or item.get("DESCRIPTION"))
        for item in result_items(comment_response)
        if str(item.get("ID") or "")
    }
    communications: list[dict[str, Any]] = []
    missing_transcript = False
    truncated_text = False
    for event in events:
        channel = str(event.get("channel") or "unknown")
        contact_class = str(event.get("contact_class") or "unknown")
        transcript_available: bool | None = None
        kind = (
            "contact" if contact_class == "confirmed_contact"
            else "internal" if contact_class == "internal_information"
            else "attempt" if contact_class == "attempt"
            else "unknown"
        )
        source_ids = event.get("source_ids") if isinstance(event.get("source_ids"), list) else []
        source_id = str(source_ids[0] or "") if source_ids else ""
        source_text = (
            activity_text_by_id.get(source_id, "")
            if event.get("source_type") == "crm_activity"
            else comment_text_by_id.get(source_id, "")
        )
        text = source_text or str(event.get("content") or event.get("subject") or "")
        if channel == "call":
            transcript_available = False
            call_id = str(event.get("call_id") or "")
            if call_id:
                try:
                    from api.jobs import _call_transcript, _transcript_confirms_contact

                    transcript = _call_transcript(lead_id, call_id)
                    transcript_available = bool(transcript)
                    if transcript:
                        text = transcript
                        if _transcript_confirms_contact(
                            transcript,
                            event.get("duration_seconds") if isinstance(event.get("duration_seconds"), (int, float)) else None,
                        ):
                            kind = "contact"
                except Exception:  # noqa: BLE001 - local transcripts are optional history evidence
                    transcript_available = False
            missing_transcript = missing_transcript or not transcript_available
        if len(text) > 20_000:
            text = text[:20_000]
            truncated_text = True
        projected = {
            "id": str(event.get("event_id") or ""),
            "occurred_at": str(event.get("occurred_at") or "") or None,
            "channel": channel,
            "kind": kind,
            "text": text,
        }
        if transcript_available is not None:
            projected["transcript_available"] = transcript_available
        communications.append(projected)
    communications.sort(key=lambda item: (str(item.get("occurred_at") or ""), str(item.get("id") or "")), reverse=True)

    comments = []
    for item in sections.get("internal_context") or []:
        if not isinstance(item, dict):
            continue
        comment_id = str(item.get("id") or "")
        text = comment_text_by_id.get(comment_id) or str(item.get("text") or "")
        if len(text) > 20_000:
            text = text[:20_000]
            truncated_text = True
        comments.append({
            "id": comment_id,
            "created_at": str(item.get("when") or "") or None,
            "text": text,
            "author_id": str(item.get("author_id") or "") or None,
        })
    comments.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    limitations = []
    if not activity_ok:
        limitations.append("Не удалось загрузить активности лида из Bitrix.")
    if not comments_ok:
        limitations.append("Не удалось загрузить комментарии лида из Bitrix.")
    if missing_transcript:
        limitations.append("Для части звонков нет доступной локальной расшифровки.")
    if truncated_text:
        limitations.append("Текст отдельных записей сокращён до 20 000 символов.")
    return {
        "comments": comments,
        "communications": communications,
        "current_task": _lead_control_current_task(sections.get("tasks_and_control") or []) if activity_ok else None,
        "task_checked": activity_ok,
        "activity_ok": activity_ok,
        "comments_ok": comments_ok,
        "limitations": limitations,
    }


def load_lead_control_history(
    lead_id: str,
    *,
    refresh_crm: bool = False,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    key = _lead_control_history_key(lead_id)
    before = get_crm_sync_state(db_path, key)
    cached = _lead_control_history_state(before, lead_id=lead_id)
    if cached and not refresh_crm:
        return {field: value for field, value in cached.items() if field not in {"revision", "task_checked"}}

    try:
        client = make_client()
    except Exception:  # noqa: BLE001 - report source unavailability without exposing configuration details
        limitations = ["Не удалось подключиться к Bitrix для чтения истории лида."]
        if cached:
            return {
                field: value
                for field, value in {
                    **cached,
                    "limitations": list(dict.fromkeys([*cached["limitations"], *limitations])),
                }.items()
                if field not in {"revision", "task_checked"}
            }
        return {
            "lead_id": lead_id,
            "available": False,
            "comments": [],
            "communications": [],
            "current_task": None,
            "limitations": limitations,
            "fetched_at": None,
        }
    activity_payload = {
        "order": {"START_TIME": "DESC", "DEADLINE": "DESC", "ID": "DESC"},
        "filter": {"OWNER_TYPE_ID": 1, "OWNER_ID": lead_id},
        "select": [
            "ID", "TYPE_ID", "PROVIDER_ID", "PROVIDER_TYPE_ID", "PROVIDER_GROUP_ID", "SUBJECT",
            "DESCRIPTION", "DIRECTION", "COMPLETED", "START_TIME", "CREATED", "DEADLINE", "END_TIME",
        ],
    }
    comment_payload = {
        "order": {"CREATED": "DESC", "ID": "DESC"},
        "filter": {"ENTITY_TYPE": "lead", "ENTITY_ID": lead_id},
        "select": ["ID", "CREATED", "ENTITY_ID", "ENTITY_TYPE", "AUTHOR_ID", "COMMENT"],
    }

    def read_source(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = client.safe_list_all(method, payload)
            return response if isinstance(response, dict) else {"ok": False, "items": []}
        except Exception:  # noqa: BLE001 - keep source failures explicit and independent
            return {"ok": False, "items": []}

    with bitrix_trace_context(component="lead_control_history"):
        activities = read_source("crm.activity.list", activity_payload)
        comments = read_source("crm.timeline.comment.list", comment_payload)
    projected = _project_lead_control_history(lead_id, activities, comments)
    # Preserve last known source data when an individual Bitrix read fails.
    if cached:
        if not projected["activity_ok"]:
            projected["communications"] = cached["communications"]
            projected["current_task"] = cached["current_task"]
            projected["task_checked"] = cached["task_checked"]
        if not projected["comments_ok"]:
            projected["comments"] = cached["comments"]
    has_fresh_source = projected["activity_ok"] or projected["comments_ok"]
    fetched_at = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds") if has_fresh_source else (cached or {}).get("fetched_at")
    payload = {
        "schema_version": 1,
        "lead_id": lead_id,
        "available": bool(has_fresh_source or (cached or {}).get("available")),
        "comments": projected["comments"],
        "communications": projected["communications"],
        "current_task": projected["current_task"],
        "task_checked": projected["task_checked"],
        "limitations": projected["limitations"],
        "fetched_at": fetched_at,
    }
    if has_fresh_source:
        expected_revision = int(before.get("revision") or 0) if before else 0
        try:
            put_crm_sync_state(db_path, key, payload, expected_revision=expected_revision)
        except RuntimeError:
            newer = _lead_control_history_state(get_crm_sync_state(db_path, key), lead_id=lead_id)
            if newer:
                return {field: value for field, value in newer.items() if field not in {"revision", "task_checked"}}
            raise
    return {
        "lead_id": lead_id,
        "available": payload["available"],
        "comments": payload["comments"],
        "communications": payload["communications"],
        "current_task": payload["current_task"],
        "limitations": payload["limitations"],
        "fetched_at": payload["fetched_at"],
    }


def _search_lead_control_candidates(
    *,
    group: str,
    manager_id: str | None,
    limit: int,
    review_view: str,
    refresh_crm: bool,
    required_statuses: dict[str, dict[str, Any]],
    db_path: str | Path,
) -> dict[str, Any]:
    period = _lead_control_period()
    before_refresh = get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY)
    state = _lead_control_snapshot_state(before_refresh)
    if refresh_crm:
        expected_statuses = {
            status_id: contract
            for group_statuses in LEAD_CONTROL_GROUPS.values()
            for status_id, contract in group_statuses.items()
        }
        if required_statuses != expected_statuses:
            raise LeadControlConfigurationError("Перед обновлением Bitrix требуется проверить все четыре статуса контроля лидов")
        profile = default_analysis_profile()
        profile["review_view"] = "all"
        profile["lead"]["enabled"] = True
        profile["lead"]["all_stages"] = False
        profile["lead"]["stage_ids"] = list(required_statuses)
        profile["deal"]["enabled"] = False
        preview = profile_candidates_preview(
            {"name": "Lead control snapshot", "version": 1, "profile": profile},
            period_override=period,
            required_lead_statuses=required_statuses,
            db_path=db_path,
        )
        refreshed_names = {
            str(user_id).strip(): str(name).strip()
            for user_id, name in (preview.get("manager_names") or {}).items()
            if str(user_id).strip() and str(name).strip()
        } if isinstance(preview.get("manager_names"), dict) else {}
        manager_names = {**(state or {}).get("manager_names", {}), **refreshed_names}
        snapshot_cards = _lead_control_snapshot_cards(preview.get("candidates") or [])
        snapshot_manager_names: dict[str, str] = {}
        for item in snapshot_cards:
            owner_id = str(item.get("manager_id") or "").strip()
            manager_name = refreshed_names.get(owner_id) or manager_names.get(owner_id) or str(item.get("manager_name") or "").strip()
            if owner_id and manager_name:
                item["manager_name"] = manager_name
                snapshot_manager_names[owner_id] = manager_name
            else:
                item.pop("manager_name", None)
        payload = {
            "schema_version": 1,
            "crm_updated_at": period["as_of"],
            "candidates": snapshot_cards,
            "manager_names": snapshot_manager_names,
        }
        expected_revision = int(before_refresh.get("revision") or 0) if before_refresh else 0
        try:
            put_crm_sync_state(
                db_path,
                LEAD_CONTROL_SNAPSHOT_KEY,
                payload,
                expected_revision=expected_revision,
            )
        except RuntimeError as error:
            newer = get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY)
            if newer is not None and int(newer.get("revision") or 0) != expected_revision:
                raise RuntimeError("Lead-control refresh conflicted with a newer snapshot; retry the refresh") from error
            raise
        state = _lead_control_snapshot_state(get_crm_sync_state(db_path, LEAD_CONTROL_SNAPSHOT_KEY))

    crm_updated_at = state.get("crm_updated_at") if state else None
    cached_cards = state.get("candidates") if state else []
    manager_names = state.get("manager_names") if state else {}
    wanted_statuses = set(LEAD_CONTROL_GROUPS[group])
    matching = [
        item
        for item in cached_cards
        if item.get("entity_type") == "lead"
        and str(item.get("stage_id") or "") in wanted_statuses
        and (not manager_id or str(item.get("manager_id") or "") == manager_id)
        and _in_period(item.get("date_create"), period)
    ]
    matching.sort(
        key=lambda item: (
            {"high": 3, "medium": 2, "low": 1}.get(str(item.get("priority")), 0),
            int(item.get("score") or 0),
            str(item.get("date_create") or ""),
        ),
        reverse=True,
    )
    total_scored = len(matching)
    top = [dict(item) for item in matching[:limit]]
    for item in top:
        manager_name = manager_names.get(str(item.get("manager_id") or ""))
        if manager_name:
            item["manager_name"] = manager_name
        history_state = _lead_control_history_state(
            get_crm_sync_state(db_path, _lead_control_history_key(str(item.get("entity_id") or ""))),
            lead_id=str(item.get("entity_id") or ""),
        )
        item["crm_task"] = history_state.get("current_task") if history_state else None
        item["crm_task_checked"] = bool(history_state and history_state.get("task_checked"))
        item["analysis_freshness"] = candidate_freshness(
            "lead",
            {
                "ID": str(item.get("entity_id") or ""),
                "STATUS_ID": item.get("stage_id"),
                "DATE_MODIFY": item.get("date_modify"),
            },
            db_path=db_path,
        )
    attach_saved_lead_qualification(top, db_path=db_path)
    for item in top:
        item["analyzed"] = bool(item.get("lead_analysis_available"))
    top, review_summary = apply_profile_review_states(top, view=review_view, db_path=db_path)
    summary = {
        "total_scored": total_scored,
        "returned": len(top),
        "high": sum(1 for item in top if item.get("priority") == "high"),
        "medium": sum(1 for item in top if item.get("priority") == "medium"),
        "low": sum(1 for item in top if item.get("priority") == "low"),
        "already_analyzed": sum(1 for item in top if item.get("analyzed")),
        "reviewed_hidden": review_summary["reviewed_hidden"],
        "reviewed_visible": review_summary["reviewed_visible"],
        "changed_after_review": review_summary["reactivated"],
        "crm_updated_after_review": 0,
    }
    return {
        "created_days": 30,
        "modified_days": 0,
        "days": 30,
        "limit": limit,
        "entity_type": "lead",
        "lead_control_group": group,
        "manager_id": manager_id,
        "pipeline_ids": [],
        "stage_ids": list(LEAD_CONTROL_GROUPS[group]),
        "review_view": review_view,
        "lead_categories": [],
        "bant_filter": "",
        "ready": True,
        "ready_message": "" if state else "Нажмите «Обновить Bitrix», чтобы загрузить список лидов.",
        "generated_at": period["as_of"],
        "crm_snapshot_available": state is not None,
        "crm_updated_at": crm_updated_at,
        "manager_names": manager_names,
        "summary": summary,
        "candidates": top,
    }


def search_candidates(
    *,
    entity_type: str = "lead",
    created_days: int = DEFAULT_DAYS,
    modified_days: int = DEFAULT_DAYS,
    days: int | None = None,
    limit: int = DEFAULT_LIMIT,
    priority: str | None = None,
    pipeline_ids: list[str] | None = None,
    stage_ids: list[str] | None = None,
    review_view: str = "active",
    lead_categories: list[str] | None = None,
    bant_filter: str = "",
    lead_control_group: str | None = None,
    lead_manager_id: str | None = None,
    required_lead_statuses: dict[str, dict[str, Any]] | None = None,
    refresh_crm: bool = False,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    # Обратная совместимость: старый параметр days задаёт окно CREATE.
    if days is not None:
        created_days = max(0, int(days))
    else:
        created_days = max(0, int(created_days))
    modified_days = max(0, int(modified_days))
    limit = max(1, min(int(limit), 100))
    pipelines = _normalize_id_list(pipeline_ids)
    stages = _normalize_id_list(stage_ids)
    manager_id = str(lead_manager_id or "").strip() or None
    if manager_id and (entity_type != "lead" or not lead_control_group):
        raise LeadControlConfigurationError("Фильтр менеджера доступен только в контроле лидов")
    if lead_control_group:
        if entity_type != "lead" or str(lead_control_group) not in LEAD_CONTROL_GROUPS:
            raise LeadControlConfigurationError("Неизвестная группа контроля лидов")
        return _search_lead_control_candidates(
            group=str(lead_control_group),
            manager_id=manager_id,
            limit=limit,
            review_view=review_view,
            refresh_crm=refresh_crm,
            required_statuses=required_lead_statuses or {},
            db_path=db_path,
        )
    review_view = review_view if review_view in {"active", "reviewed", "all"} else "active"
    categories = (
        set()
        if lead_control_group
        else {str(value) for value in (lead_categories or []) if str(value) in {"A", "B", "C", "D", "E", "unknown"}}
    )
    bant_filter = "" if lead_control_group else (
        bant_filter
        if bant_filter in {"", "complete", "incomplete", "budget", "authority", "need", "timeframe", "negative", "unknown"}
        else ""
    )

    ready, ready_message = candidates_filter_ready(
        entity_type=entity_type,
        pipeline_ids=pipelines,
        stage_ids=stages,
    )
    empty_summary = {
        "total_scored": 0,
        "returned": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "already_analyzed": 0,
        "reviewed_hidden": 0,
        "reviewed_visible": 0,
        "changed_after_review": 0,
        "crm_updated_after_review": 0,
    }
    base_response = {
        "created_days": created_days,
        "modified_days": modified_days,
        "days": created_days,
        "limit": limit,
        "entity_type": entity_type,
        "lead_control_group": lead_control_group,
        "manager_id": manager_id,
        "pipeline_ids": pipelines,
        "stage_ids": stages,
        "review_view": review_view,
        "lead_categories": sorted(categories),
        "bant_filter": bant_filter,
        "ready": ready,
        "ready_message": ready_message,
        "generated_at": datetime.now(MSK_TZ).isoformat(timespec="seconds"),
        "summary": empty_summary,
        "candidates": [],
    }
    if not ready:
        return base_response

    # Совместимый endpoint использует тот же live engine, что и ежедневная сводка.
    # Старые score_* оставлены только как чистые backward-compatible helpers.
    profile = default_analysis_profile()
    profile["review_view"] = review_view
    profile["limits"].update({"workset": limit, "new_slots": limit, "backlog_slots": 0})
    profile["lead"]["enabled"] = entity_type == "lead"
    profile["deal"]["enabled"] = entity_type == "deal"
    if entity_type == "lead":
        profile["lead"]["all_stages"] = False
        profile["lead"]["stage_ids"] = stages
    else:
        profile["deal"]["pipeline_ids"] = pipelines
        profile["deal"]["stage_ids"] = stages
    current = datetime.now(MOSCOW_TZ)
    period_from = current - timedelta(days=created_days) if created_days > 0 else datetime.combine(current.date(), time.min, MOSCOW_TZ)
    period_override = {
        "preset": "legacy_days_window",
        "timezone": "Europe/Moscow",
        "period_from": period_from.isoformat(),
        "period_to": current.isoformat(),
        "as_of": current.isoformat(timespec="seconds"),
    }
    preview = profile_candidates_preview(
        {"name": "Совместимый фильтр кандидатов", "version": 1, "profile": profile},
        period_override=period_override,
        required_lead_statuses=required_lead_statuses,
        lead_manager_id=manager_id,
        db_path=db_path,
    )
    scored = [item for item in preview["candidates"] if item.get("entity_type") == entity_type]
    if entity_type == "lead" and (categories or bant_filter):
        scored = [
            item
            for item in scored
            if lead_qualification_matches(item, categories=categories, bant_filter=bant_filter)
        ]
    if not lead_control_group and priority in {"high", "medium", "low"}:
        scored = [item for item in scored if item.get("priority") == priority]
    review_summary = {
        "reviewed_hidden": int(preview["summary"].get("reviewed_hidden") or 0),
        "reviewed_visible": int(preview["summary"].get("reviewed_visible") or 0),
        "changed_after_review": int(preview["summary"].get("reactivated") or 0),
        "crm_updated_after_review": 0,
    }
    top = scored[:limit]
    base_response["summary"] = {
        "total_scored": len(scored),
        "returned": len(top),
        "high": sum(1 for item in top if item.get("priority") == "high"),
        "medium": sum(1 for item in top if item.get("priority") == "medium"),
        "low": sum(1 for item in top if item.get("priority") == "low"),
        "already_analyzed": sum(1 for item in top if item.get("analyzed")),
        **review_summary,
    }
    base_response["candidates"] = top
    return base_response
