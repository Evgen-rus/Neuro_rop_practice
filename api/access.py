"""Authorization and tenancy projections for HTTP API handlers.

All ownership decisions are made from the authenticated user and the local
deal row.  Request fields such as ``source_role`` are never trusted as actor
identity.  The module intentionally uses storage's public functions rather
than opening SQLite itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from api.auth import current_user
from openai_api.change_detection.decision_engine import trigger_label
from storage import rop_db as storage


_LIGHTWEIGHT_DEAL_FIELDS = (
    "deal_id",
    "source",
    "title",
    "manager_id",
    "manager_name",
    "stage_id",
    "stage_name",
    "pipeline_id",
    "pipeline_name",
    "amount",
    "currency_id",
    "created_at_crm",
    "modified_at_crm",
    "is_active",
)


@dataclass(frozen=True)
class DealAccess:
    user: dict[str, Any]
    deal: dict[str, Any]
    ownership: str
    is_own: bool
    read_only: bool
    can_open: bool
    can_edit: bool
    can_run_analysis: bool
    can_run_paid_ai: bool

    @property
    def deal_id(self) -> str:
        return str(self.deal.get("deal_id") or "")


def _deal_rows(*, active_only: bool = False) -> list[dict[str, Any]]:
    function = getattr(storage, "list_deal_control_deals", None)
    if not callable(function):
        return []
    rows = function(storage.DEFAULT_DB_PATH, active_only=active_only)
    return [dict(row) for row in rows if isinstance(row, dict)]


def get_deal(deal_id: str, *, active_only: bool = False) -> dict[str, Any] | None:
    wanted = str(deal_id)
    return next((row for row in _deal_rows(active_only=active_only) if str(row.get("deal_id")) == wanted), None)


def _ownership(user: dict[str, Any], deal: dict[str, Any]) -> tuple[str, bool]:
    manager_id = str(deal.get("manager_id") or "")
    is_own = (
        str(user.get("role")) == "manager"
        and bool(user.get("manager_id"))
        and manager_id == str(user.get("manager_id"))
    )
    if is_own:
        return "own", True
    if not manager_id:
        return "unassigned", False
    return "foreign", False


def _rop_team_manager_ids() -> set[str]:
    function = getattr(storage, "get_deal_control_scope", None)
    if not callable(function):
        return set()
    scope = function(storage.DEFAULT_DB_PATH)
    values = scope.get("manager_ids") if isinstance(scope, dict) else []
    return {str(value) for value in values or [] if str(value).strip()}


def rop_team_manager_ids() -> set[str]:
    """Return the configured manager scope for a ROP read/control session."""

    return _rop_team_manager_ids()


def deals_visible_for_dashboard(deals: list[dict[str, Any]], user: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep only deals the authenticated viewer may see, using the same rules as ``deal_access``."""
    role = str(user.get("role") or "")
    if role == "admin":
        return list(deals)
    if role == "rop":
        team_ids = _rop_team_manager_ids()
        return [row for row in deals if str(row.get("manager_id") or "") in team_ids]
    if role == "manager":
        manager_id = str(user.get("manager_id") or "")
        if not manager_id:
            return []
        return [row for row in deals if str(row.get("manager_id") or "") == manager_id]
    return []


def deal_access(
    user: dict[str, Any],
    deal: dict[str, Any],
    *,
    team_manager_ids: set[str] | None = None,
) -> DealAccess:
    role = str(user.get("role") or "")
    ownership, is_own = _ownership(user, deal)
    if role == "admin":
        can_open = can_edit = can_run_paid_ai = True
    elif role == "rop":
        # РОП и заместитель работают с Дожимом и прочим платным AI по сделкам команды.
        team_ids = team_manager_ids if team_manager_ids is not None else _rop_team_manager_ids()
        in_team = str(deal.get("manager_id") or "") in team_ids
        can_open = can_edit = can_run_paid_ai = in_team
    else:
        can_open = can_edit = is_own
        can_run_paid_ai = is_own
    can_run_analysis = can_open
    return DealAccess(
        user=user,
        deal=deal,
        ownership=ownership,
        is_own=is_own,
        read_only=not can_edit,
        can_open=can_open,
        can_edit=can_edit,
        can_run_analysis=can_run_analysis,
        can_run_paid_ai=can_run_paid_ai,
    )


def _manager_name(deal: dict[str, Any]) -> str | None:
    return str(deal.get("manager_name") or "") or None


def deal_flags(access: DealAccess) -> dict[str, Any]:
    return {
        "ownership": access.ownership,
        "is_own": access.is_own,
        "read_only": access.read_only,
        "can_open": access.can_open,
        "can_edit": access.can_edit,
        "can_run_analysis": access.can_run_analysis,
        "can_run_paid_ai": access.can_run_paid_ai,
    }


def project_deal_row(
    deal: dict[str, Any],
    user: dict[str, Any],
    *,
    full: bool = True,
    access: DealAccess | None = None,
) -> dict[str, Any]:
    access = access if access is not None else deal_access(user, deal)
    if not access.can_open:
        row = {key: deal.get(key) for key in _LIGHTWEIGHT_DEAL_FIELDS}
        row["manager_name"] = _manager_name(deal)
    elif full:
        row = dict(deal)
    else:
        row = {key: deal.get(key) for key in _LIGHTWEIGHT_DEAL_FIELDS}
    row.update(deal_flags(access))
    row["manager_name"] = _manager_name(deal)
    return row


def _project_scope(
    scope_value: Any,
    user: dict[str, Any],
    deals: list[dict[str, Any]],
    *,
    team_manager_ids: set[str] | None = None,
) -> dict[str, Any]:
    scope = dict(scope_value) if isinstance(scope_value, dict) else {}
    role = str(user.get("role") or "")
    visible_ids = {str(row.get("deal_id") or "") for row in deals}
    allowed_initial_ids = visible_ids
    if role == "manager":
        # A foreign row may be present as a bounded directory projection, but
        # its CRM identity must not cross the scope metadata boundary.
        allowed_initial_ids = {
            str(row.get("deal_id") or "") for row in deals if bool(row.get("is_own"))
        }
    initial_ids = scope.get("initial_deal_ids") if isinstance(scope.get("initial_deal_ids"), list) else []
    scope["initial_deal_ids"] = [str(value) for value in initial_ids if str(value) in allowed_initial_ids]
    if role == "rop":
        scope["manager_ids"] = sorted(team_manager_ids if team_manager_ids is not None else _rop_team_manager_ids())
    elif role == "manager":
        manager_id = str(user.get("manager_id") or "")
        scope["manager_ids"] = [manager_id] if manager_id else []
    return scope


def scoped_dashboard(dashboard: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """Apply row-level projection to a deal-control dashboard."""

    source_deals = [row for row in dashboard.get("deals") or [] if isinstance(row, dict)]
    role = str(user.get("role") or "")
    team_ids = _rop_team_manager_ids() if role == "rop" else None
    if role == "admin":
        deals = [project_deal_row(row, user, full=True) for row in source_deals]
    elif role == "rop":
        deals = []
        for row in source_deals:
            access_row = deal_access(user, row, team_manager_ids=team_ids)
            if not access_row.can_open:
                continue
            deals.append(project_deal_row(row, user, full=True, access=access_row))
        dashboard = {**dashboard, "summary": _team_summary(dashboard.get("summary"), deals)}
        dashboard["outcome_metrics"] = team_metrics(manager_ids=team_ids)
    elif role == "manager":
        owned_source = [row for row in source_deals if deal_access(user, row).is_own]
        owned = [project_deal_row(row, user, full=True) for row in owned_source]
        deals = owned
        summary = dict(dashboard.get("summary") or {})
        summary["active_deals"] = len(owned)
        summary["portfolio_amount"] = sum(
            float(str(row.get("amount") or "0").replace(",", ".") or 0)
            for row in owned
            if str(row.get("amount") or "").replace(",", ".").replace(".", "", 1).isdigit()
        )
        primary_tasks = [row.get("primary_bitrix_task") for row in owned if row.get("primary_bitrix_task")]
        buckets = [str(task.get("time_bucket") or "unscheduled") for task in primary_tasks]
        summary["tasks_total"] = len(primary_tasks)
        summary["tasks_today"] = buckets.count("today")
        summary["tasks_tomorrow"] = buckets.count("tomorrow")
        summary["tasks_future"] = buckets.count("future")
        summary["tasks_overdue"] = buckets.count("overdue")
        summary["tasks_completed_today"] = sum(
            str(task.get("completion_state") or "open") in {"local", "bitrix"}
            and str(task.get("time_bucket") or "") in {"overdue", "today"}
            for task in primary_tasks
        )
        summary["tasks_missing"] = sum(row.get("primary_bitrix_task") is None for row in owned)
        summary["tasks_plan_today"] = summary["tasks_missing"] + summary["tasks_overdue"] + summary["tasks_today"]
        probabilities = [int(row["probability"]) for row in owned if row.get("probability") is not None]
        summary["average_probability"] = round(sum(probabilities) / len(probabilities)) if probabilities else None
        metrics = getattr(storage, "get_deal_control_metrics", None)
        manager_id = str(user.get("manager_id") or "")
        if callable(metrics) and manager_id:
            summary_metrics = metrics(storage.DEFAULT_DB_PATH, manager_id=manager_id)
        else:
            summary_metrics = {}
        dashboard = {**dashboard, "summary": summary, "outcome_metrics": summary_metrics}
    else:
        raise HTTPException(status_code=403, detail="Forbidden")
    dashboard["scope"] = _project_scope(
        dashboard.get("scope"),
        user,
        deals,
        team_manager_ids=team_ids,
    )
    dashboard["deals"] = deals
    return dashboard


def _team_summary(summary_value: Any, deals: list[dict[str, Any]]) -> dict[str, Any]:
    summary = dict(summary_value) if isinstance(summary_value, dict) else {}
    summary["active_deals"] = len(deals)
    summary["portfolio_amount"] = sum(
        float(str(row.get("amount") or "0").replace(",", ".") or 0)
        for row in deals
        if str(row.get("amount") or "").replace(",", ".").replace(".", "", 1).isdigit()
    )
    primary_tasks = [row.get("primary_bitrix_task") for row in deals if row.get("primary_bitrix_task")]
    buckets = [str(task.get("time_bucket") or "unscheduled") for task in primary_tasks]
    summary["tasks_total"] = len(primary_tasks)
    summary["tasks_today"] = buckets.count("today")
    summary["tasks_tomorrow"] = buckets.count("tomorrow")
    summary["tasks_future"] = buckets.count("future")
    summary["tasks_overdue"] = buckets.count("overdue")
    summary["tasks_completed_today"] = sum(
        str(task.get("completion_state") or "open") in {"local", "bitrix"}
        and str(task.get("time_bucket") or "") in {"overdue", "today"}
        for task in primary_tasks
    )
    summary["tasks_missing"] = sum(row.get("primary_bitrix_task") is None for row in deals)
    summary["tasks_plan_today"] = summary["tasks_missing"] + summary["tasks_overdue"] + summary["tasks_today"]
    probabilities = [int(row["probability"]) for row in deals if row.get("probability") is not None]
    summary["average_probability"] = round(sum(probabilities) / len(probabilities)) if probabilities else None
    return summary


def team_metrics(*, manager_ids: set[str] | None = None) -> dict[str, Any]:
    function = getattr(storage, "get_deal_control_metrics", None)
    if not callable(function):
        return {}
    resolved_ids = manager_ids if manager_ids is not None else _rop_team_manager_ids()
    if not resolved_ids:
        return {}
    collected = [
        function(storage.DEFAULT_DB_PATH, manager_id=manager_id)
        for manager_id in sorted(resolved_ids)
    ]
    result: dict[str, Any] = {}
    for key in ("overall", "with_guidance", "without_guidance"):
        result[key] = {}
        fields = {
            field
            for value in collected
            if isinstance(value, dict) and isinstance(value.get(key), dict)
            for field in value[key]
        }
        for field in fields:
            result[key][field] = sum(
                int(value.get(key, {}).get(field) or 0)
                for value in collected
                if isinstance(value, dict) and isinstance(value.get(key), dict)
            )
    result["cancelled_tasks"] = sum(int(value.get("cancelled_tasks") or 0) for value in collected if isinstance(value, dict))
    result["note"] = "Сравнение показывает связь с подготовленной AI-подсказкой, но не доказывает причинность."
    return result


def scoped_deal_metrics(user: dict[str, Any], manager_id: str | None = None) -> dict[str, Any]:
    """Return metrics constrained to the actor's deal scope."""

    role = str(user.get("role") or "")
    function = getattr(storage, "get_deal_control_metrics", None)
    if not callable(function):
        return {}
    if role == "admin":
        return function(storage.DEFAULT_DB_PATH, manager_id=manager_id)
    if role == "manager":
        own_manager_id = str(user.get("manager_id") or "")
        if not own_manager_id:
            raise HTTPException(status_code=403, detail="Manager scope is not configured")
        return function(storage.DEFAULT_DB_PATH, manager_id=own_manager_id)
    if role == "rop":
        team_ids = _rop_team_manager_ids()
        if manager_id is not None:
            if str(manager_id) not in team_ids:
                raise HTTPException(status_code=403, detail="Manager is outside the ROP team scope")
            return function(storage.DEFAULT_DB_PATH, manager_id=str(manager_id))
        return team_metrics(manager_ids=team_ids)
    raise HTTPException(status_code=403, detail="Forbidden")


def require_deal(
    deal_id: str,
    *,
    user: dict[str, Any] | None = None,
    action: str = "open",
    allow_projection: bool = False,
) -> DealAccess:
    actor = user or current_user()
    deal = get_deal(str(deal_id), active_only=False)
    if deal is None:
        raise HTTPException(status_code=404, detail="Deal not found")
    access = deal_access(actor, deal)
    if action == "view_projection" and allow_projection:
        return access
    if action == "paid_ai" and not access.can_run_paid_ai:
        raise HTTPException(status_code=403, detail="Paid AI is not available for this role or deal")
    if action in {"open", "read", "edit", "control"} and not access.can_open:
        raise HTTPException(status_code=403, detail="Deal access forbidden")
    if action == "edit" and not access.can_edit:
        raise HTTPException(status_code=403, detail="Deal is read-only for this user")
    return access


def require_task(task_id: int, *, user: dict[str, Any] | None = None, action: str = "open") -> tuple[DealAccess, dict[str, Any]]:
    function = getattr(storage, "get_deal_control_task", None)
    task = function(storage.DEFAULT_DB_PATH, task_id=int(task_id)) if callable(function) else None
    if not isinstance(task, dict):
        raise HTTPException(status_code=404, detail="Task not found")
    return require_deal(str(task.get("deal_id") or ""), user=user, action=action), task


def require_report(report_id: int, *, user: dict[str, Any] | None = None, action: str = "read") -> dict[str, Any]:
    function = getattr(storage, "get_ui_report", None)
    report = function(storage.DEFAULT_DB_PATH, int(report_id)) if callable(function) else None
    if not isinstance(report, dict):
        raise HTTPException(status_code=404, detail="Report not found")
    actor = user or current_user()
    if str(report.get("entity_type") or "") == "deal":
        require_deal(str(report.get("entity_id") or ""), user=actor, action="edit" if action == "edit" else "open")
    elif str(actor.get("role")) == "manager":
        raise HTTPException(status_code=403, detail="This report is outside your deal scope")
    return report


def actor_source_role(user: dict[str, Any]) -> str:
    return "manager" if str(user.get("role")) == "manager" else "rop"


def require_paid_deal(deal_id: str, *, user: dict[str, Any] | None = None) -> DealAccess:
    return require_deal(deal_id, user=user, action="paid_ai")


def job_entity_ids(job: dict[str, Any]) -> list[tuple[str, str]]:
    options = job.get("options") if isinstance(job.get("options"), dict) else {}
    entity_type = str(options.get("entity_type") or "")
    ids = options.get("ids") if isinstance(options.get("ids"), list) else []
    if entity_type not in {"deal", "lead"}:
        return []
    return [(entity_type, str(item)) for item in ids if str(item)]


def can_view_job(job: dict[str, Any], user: dict[str, Any]) -> bool:
    role = str(user.get("role") or "")
    if role == "admin":
        return True
    entities = job_entity_ids(job)
    if not entities or any(entity_type != "deal" for entity_type, _ in entities):
        return False
    for _entity_type, entity_id in entities:
        deal = get_deal(entity_id)
        if deal is None or not deal_access(user, deal).can_open:
            return False
    return True


_ACTIVE_AUTOMATIC_ANALYSIS_STAGES = frozenset(
    {
        "crm_context",
        "audio_download",
        "transcription",
        "llm_analysis",
        "validation",
        "report",
    }
)


def scoped_automatic_analysis_items(
    items: list[dict[str, Any]],
    user: dict[str, Any],
) -> list[dict[str, Any]]:
    role = str(user.get("role") or "")
    if role == "admin":
        return list(items)
    visible: list[dict[str, Any]] = []
    for item in items:
        deal = get_deal(str(item.get("entity_id") or ""))
        if deal is None:
            continue
        if deal_access(user, deal).can_open:
            visible.append(item)
    return visible


def _current_automatic_analysis_payload(
    run: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if str(run.get("status") or "") != "running":
        return None
    active = [
        item
        for item in items
        if str(item.get("stage") or "") in _ACTIVE_AUTOMATIC_ANALYSIS_STAGES
    ]
    if not active:
        return None
    chosen = max(active, key=lambda item: str(item.get("updated_at") or ""))
    entity_id = str(chosen.get("entity_id") or "").strip()
    if not entity_id:
        return None
    deal = get_deal(entity_id)
    title = str((deal or {}).get("title") or "").strip() or f"Сделка {entity_id}"
    stage = str(chosen.get("stage") or "").strip() or None
    return {"title": title, "stage": stage}


def _updated_automatic_analysis_deal_ids(items: list[dict[str, Any]]) -> list[str]:
    """IDs of scoped FULL published and MINI deals the UI can refresh one-by-one."""
    ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        decision = str(item.get("decision_status") or "")
        publication = str(item.get("publication_status") or "")
        entity_id = str(item.get("entity_id") or "").strip()
        if not entity_id or entity_id in seen:
            continue
        ready = decision == "mini" or (decision == "full" and publication == "published")
        if not ready:
            continue
        seen.add(entity_id)
        ids.append(entity_id)
    return ids


def automatic_analysis_latest_payload(
    run: dict[str, Any] | None,
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if run is None:
        return None
    full = mini = skip = errors = processed = published = 0
    for item in items:
        decision = str(item.get("decision_status") or "")
        publication = str(item.get("publication_status") or "")
        if decision in {"full", "mini", "skip", "error"}:
            processed += 1
        if decision == "full":
            full += 1
        elif decision == "mini":
            mini += 1
        elif decision == "skip":
            skip += 1
        if decision == "error" or (not decision and publication == "error"):
            errors += 1
        if publication == "published" and decision == "full":
            published += 1
    return {
        "business_date": run.get("business_date"),
        "status": run.get("status"),
        "processed": processed,
        "total": len(items),
        "succeeded": full + mini,
        "errors": errors,
        "skipped": skip,
        "full": full,
        "mini": mini,
        "reports_published": published,
        "updated_deal_ids": _updated_automatic_analysis_deal_ids(items),
        "current_stage": run.get("current_stage"),
        "current": _current_automatic_analysis_payload(run, items),
        "started_at": run.get("started_at"),
        "updated_at": run.get("updated_at"),
        "finished_at": run.get("finished_at"),
    }


_AUTOMATIC_CHANGE_LABELS = {
    "commercial_refs_changed": "Изменились ссылки на КП, счёт или договор",
    "transcript_changed": "Изменилась расшифровка разговора",
    "new_inbound_customer_message": "Появилось новое входящее сообщение клиента",
    "deal_closed": "Сделка закрыта",
    "significant_amount_changed": "Существенно изменилась сумма сделки",
    "new_activity": "Добавлена CRM-активность",
    "activity_removed": "Удалена CRM-активность",
    "amount_changed": "Изменилась сумма сделки",
    "stage_moved_time_changed": "Изменилось время перехода по стадии",
    "closed_flag_changed": "Изменился признак закрытия сделки",
}

_AUTOMATIC_FALLBACK_LABELS = {
    "optimization_disabled": "Инкрементальный анализ отключён в настройках",
    "shadow_mode": "Инкрементальный анализ работает в теневом режиме",
    "force_full_fallback": "В настройках включён переход к полному анализу",
    "incremental_analyzer_failed": "Инкрементальный анализ завершился ошибкой",
    "previous_analysis_missing": "Нет предыдущего анализа для инкрементального обновления",
    "previous_analysis_not_object": "Предыдущий анализ имеет неподходящий формат",
    "previous_analysis_invalid": "Предыдущий анализ не прошёл проверку",
    "new_transcript_unavailable": "Новая расшифровка недоступна для инкрементального обновления",
    "transcript_changed_in_place": "Изменён текст уже существующей расшифровки",
    "transcript_evidence_identity_missing": "Не удалось установить источник расшифровки",
    "no_materialized_new_events": "Не удалось подготовить новые события для инкрементального анализа",
}


def _automatic_decision_reasons(value: Any) -> list[str]:
    """Project known decision reasons; never return diff payloads or exception text."""
    decision = value if isinstance(value, dict) else {}
    raw_reasons = decision.get("reasons") if isinstance(value, dict) else value
    reasons: list[str] = []
    for reason in raw_reasons if isinstance(raw_reasons, list) else []:
        if not isinstance(reason, str):
            continue
        if reason == "Нет предыдущего состояния сделки в SQLite.":
            reasons.append("Первый анализ: предыдущее состояние сделки отсутствует")
        elif reason == "Ручной принудительный запуск: --force-llm.":
            reasons.append("Запрошен принудительный полный анализ")
        elif reason.startswith(("Обнаружены hard-изменения для прямого FULL: ",
                                "Обнаружены новые evidence для incremental-анализа: ")):
            for code in reason.split(": ", 1)[1].rstrip(".").split(", "):
                reasons.append(_AUTOMATIC_CHANGE_LABELS.get(code, "Сохранённый тип изменения не распознан"))
        elif reason.startswith("Безопасный FULL fallback: "):
            code = reason.removeprefix("Безопасный FULL fallback: ").removesuffix(".")
            fallback = _AUTOMATIC_FALLBACK_LABELS.get(code)
            if not fallback and code.startswith("v2:"):
                fallback = "Инкрементальный анализ V2 не завершён или не прошёл проверку"
            reasons.append(f"Переход к FULL: {fallback or 'сохранённая причина перехода не распознана'}")
        elif reason == "Hard-изменений для LLM нет, но есть soft-изменения или контрольные триггеры.":
            reasons.append("Изменения или контрольные триггеры не требуют LLM")
        elif reason.startswith("V2 evidence identity подтвердил, что выбранное представление транскрипта уже покрыто предыдущим анализом."):
            reasons.append("Эта расшифровка уже учтена в предыдущем анализе")
        elif reason.startswith("После отсечения ложного transcript change остались soft-изменения или контрольные триггеры:"):
            reasons.append("После исключения повторной расшифровки остались контрольные причины для MINI")
        else:
            reasons.append("Сохранённая причина не распознана")
    triggers = decision.get("triggers")
    for trigger in triggers if isinstance(triggers, list) else []:
        if not isinstance(trigger, dict) or not isinstance(trigger.get("trigger_type"), str):
            continue
        label = trigger_label({"trigger_type": trigger["trigger_type"]})
        if label == trigger["trigger_type"]:
            label = "Сохранённый контрольный триггер не распознан"
        elif trigger["trigger_type"] == "soft_change_without_llm":
            change = trigger.get("change")
            label = _AUTOMATIC_CHANGE_LABELS.get(change, label) if isinstance(change, str) else label
        reasons.append(label)
    return list(dict.fromkeys(reasons)) or ["Причина для этого запуска не сохранена"]


def automatic_analysis_details_payload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Admin projection. The endpoint must check the server session before reading it."""
    deals = {str(row.get("deal_id")): row for row in _deal_rows()} if items else {}
    return [
        {
            "deal_id": str(item["entity_id"]),
            "title": str((deals.get(str(item["entity_id"])) or {}).get("title") or f"Сделка {item['entity_id']}"),
            "decision": item["decision_status"],
            "incremental": item.get("engine_status") == "INCREMENTAL_LLM_ANALYSIS",
            "reasons": _automatic_decision_reasons(item.get("decision_reason")),
        }
        for item in items
        if item.get("decision_status") in {"full", "mini"}
    ]
