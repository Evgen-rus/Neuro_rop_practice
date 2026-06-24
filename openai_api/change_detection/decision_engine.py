"""
Decision engine for deal change detection.

The engine keeps the first MVP intentionally conservative:
- new meaningful CRM events trigger a full LLM analysis;
- no changes and no deterministic risks are skipped;
- no changes with deterministic risks produce a local mini recommendation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from setup import MSK_TZ


FIRST_FULL_ANALYSIS = "FIRST_FULL_ANALYSIS"
SKIPPED_NO_CHANGES = "SKIPPED_NO_CHANGES"
MINI_RECOMMENDATION_NO_LLM = "MINI_RECOMMENDATION_NO_LLM"
FULL_LLM_ANALYSIS = "FULL_LLM_ANALYSIS"
ERROR = "ERROR"

HARD_CHANGE_TYPES = {
    "stage_changed",
    "new_call",
    "new_email",
    "new_message",
    "new_comment",
    "commercial_refs_changed",
    "transcript_changed",
}

SOFT_CHANGE_TYPES = {
    "new_activity",
    "new_task",
    "activity_updated",
    "activity_removed",
    "comment_updated",
    "task_deadline_changed",
    "task_completed_changed",
    "amount_changed",
    "assigned_manager_changed",
    "stage_moved_time_changed",
    "closed_flag_changed",
    "file_refs_changed",
}


@dataclass(frozen=True)
class ProcessingDecision:
    status: str
    reasons: list[str]
    triggers: list[dict[str, Any]]
    diff: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "triggers": self.triggers,
            "diff": self.diff,
        }


def parse_bitrix_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def latest_activity_dt(snapshot: dict[str, Any]) -> datetime | None:
    candidates = []
    for item in snapshot.get("activities", []) or []:
        for key in ("last_updated", "end_time", "start_time", "created", "deadline"):
            dt = parse_bitrix_dt(item.get(key))
            if dt:
                candidates.append(dt)
    for item in snapshot.get("timeline_comments", []) or []:
        dt = parse_bitrix_dt(item.get("created"))
        if dt:
            candidates.append(dt)
    return max(candidates) if candidates else None


def overdue_open_tasks(snapshot: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(MSK_TZ)
    result = []
    for item in snapshot.get("activities", []) or []:
        if item.get("kind") != "task":
            continue
        completed = str(item.get("completed") or "").upper()
        status = str(item.get("status") or "")
        if completed in {"Y", "1", "TRUE"} or status == "2":
            continue
        deadline = parse_bitrix_dt(item.get("deadline"))
        if deadline and deadline < now:
            result.append(
                {
                    "trigger_type": "overdue_open_task",
                    "activity_id": item.get("id"),
                    "deadline": item.get("deadline"),
                }
            )
    return result


def extract_analysis(last_analysis: dict[str, Any] | None) -> dict[str, Any]:
    if not last_analysis:
        return {}
    analysis = last_analysis.get("analysis")
    return analysis if isinstance(analysis, dict) else last_analysis


def analysis_risk_level(last_analysis: dict[str, Any] | None, previous_state: dict[str, Any] | None) -> str:
    if previous_state and previous_state.get("last_risk_level"):
        return str(previous_state.get("last_risk_level"))
    analysis = extract_analysis(last_analysis)
    risk = analysis.get("main_risk", {}) if isinstance(analysis, dict) else {}
    return str(risk.get("risk_level") or "")


def memory_has_next_action(memory: dict[str, Any] | None, last_analysis: dict[str, Any] | None) -> bool:
    source = memory or {}
    if not source:
        analysis = extract_analysis(last_analysis)
        source = analysis.get("memory_update", {}) if isinstance(analysis, dict) else {}

    next_actions = source.get("next_actions_update") if isinstance(source, dict) else None
    if not next_actions:
        return False
    if not isinstance(next_actions, list):
        return bool(str(next_actions).strip())
    return any(str(item).strip() for item in next_actions)


def analysis_mentions_commercial_followup(last_analysis: dict[str, Any] | None) -> bool:
    if not last_analysis:
        return False
    text = json.dumps(last_analysis, ensure_ascii=False).upper()
    return any(token in text for token in ("КП", "ТКП", "КОММЕРЧ", "СЧЕТ", "СЧЁТ", "ДОГОВОР"))


def high_risk_trigger(previous_state: dict[str, Any] | None, last_analysis: dict[str, Any] | None) -> dict[str, Any] | None:
    risk_level = analysis_risk_level(last_analysis, previous_state)
    if risk_level in {"high", "medium_high"}:
        return {"trigger_type": "last_analysis_high_risk", "risk_level": risk_level}
    return None


def no_activity_trigger(snapshot: dict[str, Any], days_threshold: int = 2) -> dict[str, Any] | None:
    latest = latest_activity_dt(snapshot)
    if not latest:
        return {"trigger_type": "no_activity_found"}
    now = datetime.now(latest.tzinfo or MSK_TZ)
    days = (now - latest).total_seconds() / 86400
    if days >= days_threshold:
        return {
            "trigger_type": "stale_activity",
            "last_activity_at": latest.isoformat(),
            "days_without_activity": round(days, 1),
        }
    return None


def mini_triggers(
    *,
    current_snapshot: dict[str, Any],
    previous_state: dict[str, Any] | None,
    last_memory: dict[str, Any] | None,
    last_analysis: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    triggers.extend(overdue_open_tasks(current_snapshot))

    if previous_state and not memory_has_next_action(last_memory, last_analysis):
        triggers.append({"trigger_type": "missing_next_action_in_memory"})

    risk_trigger = high_risk_trigger(previous_state, last_analysis)
    if risk_trigger:
        triggers.append(risk_trigger)

    commercial = current_snapshot.get("commercial", {}) or {}
    if previous_state and analysis_mentions_commercial_followup(last_analysis):
        if commercial.get("file_refs_count", 0) or commercial.get("invoice_refs_count", 0):
            triggers.append({"trigger_type": "commercial_offer_or_invoice_without_new_movement"})

    stale = no_activity_trigger(current_snapshot)
    if previous_state and stale:
        triggers.append(stale)

    seen = set()
    unique = []
    for trigger in triggers:
        identity = json.dumps(trigger, ensure_ascii=False, sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(trigger)
    return unique


def soft_diff_triggers(diff: dict[str, Any]) -> list[dict[str, Any]]:
    triggers = []
    changes = set(diff.get("changes") or [])
    details = diff.get("details") or {}
    mapping = {
        "new_task": "new_task_without_llm",
        "task_deadline_changed": "task_deadline_changed_without_llm",
        "task_completed_changed": "task_completed_changed_without_llm",
        "assigned_manager_changed": "assigned_manager_changed_without_llm",
        "activity_updated": "activity_updated_without_llm",
        "comment_updated": "comment_updated_without_llm",
        "file_refs_changed": "non_commercial_file_refs_changed_without_llm",
    }
    for change, trigger_type in mapping.items():
        if change in changes:
            triggers.append(
                {
                    "trigger_type": trigger_type,
                    "change": change,
                    "details": details.get(change) or details.get(f"{change}_ids"),
                }
            )

    soft_only = sorted((changes & SOFT_CHANGE_TYPES) - set(mapping))
    for change in soft_only:
        triggers.append({"trigger_type": "soft_change_without_llm", "change": change})
    return triggers


def decide_deal_processing(
    *,
    previous_state: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
    fingerprint: str,
    diff: dict[str, Any],
    last_memory: dict[str, Any] | None = None,
) -> ProcessingDecision:
    if not previous_state:
        return ProcessingDecision(
            status=FIRST_FULL_ANALYSIS,
            reasons=["Нет предыдущего состояния сделки в SQLite."],
            triggers=[],
            diff=diff,
        )

    last_analysis = previous_state.get("last_analysis")
    previous_fingerprint = previous_state.get("current_fingerprint")
    changed = previous_fingerprint != fingerprint
    semantic_changes = set(diff.get("changes") or [])
    hard_changes = sorted(semantic_changes & HARD_CHANGE_TYPES)
    if changed and hard_changes:
        return ProcessingDecision(
            status=FULL_LLM_ANALYSIS,
            reasons=[f"Обнаружены hard-изменения: {', '.join(hard_changes)}."],
            triggers=[],
            diff=diff,
        )

    triggers = mini_triggers(
        current_snapshot=current_snapshot,
        previous_state=previous_state,
        last_memory=last_memory,
        last_analysis=last_analysis,
    )
    if changed and semantic_changes:
        triggers = soft_diff_triggers(diff) + triggers

    if triggers:
        return ProcessingDecision(
            status=MINI_RECOMMENDATION_NO_LLM,
            reasons=["Hard-изменений для LLM нет, но есть soft-изменения или контрольные триггеры."],
            triggers=triggers,
            diff=diff,
        )

    if diff.get("only_date_modify_changed"):
        reason = "Изменился только DATE_MODIFY, без изменений активностей, комментариев, стадии, коммерческих ссылок или транскрипта."
    else:
        reason = "Смысловых изменений и контрольных триггеров не найдено."
    return ProcessingDecision(
        status=SKIPPED_NO_CHANGES,
        reasons=[reason],
        triggers=[],
        diff=diff,
    )


def trigger_label(trigger: dict[str, Any]) -> str:
    trigger_type = trigger.get("trigger_type")
    labels = {
        "overdue_open_task": "Просрочена открытая задача",
        "missing_next_action_in_memory": "В памяти сделки нет следующего шага",
        "last_analysis_high_risk": "Последний анализ отметил высокий риск",
        "commercial_offer_or_invoice_without_new_movement": "После КП/счета нет нового движения",
        "stale_activity": "Давно не было активности",
        "no_activity_found": "Активности не найдены",
        "new_task_without_llm": "Добавлена новая задача",
        "task_deadline_changed_without_llm": "Изменился срок задачи",
        "task_completed_changed_without_llm": "Изменился статус задачи",
        "assigned_manager_changed_without_llm": "Изменился ответственный",
        "activity_updated_without_llm": "Обновлена активность без hard-признаков",
        "comment_updated_without_llm": "Обновлен комментарий",
        "non_commercial_file_refs_changed_without_llm": "Изменились файлы/ссылки без признаков КП/счета/договора",
        "soft_change_without_llm": "Soft-изменение без запуска LLM",
    }
    return labels.get(str(trigger_type), str(trigger_type or "триггер"))


def bullet_list(values: list[str]) -> str:
    if not values:
        return "- Нет данных"
    return "\n".join(f"- {value}" for value in values)


def last_primary_text(last_analysis: dict[str, Any] | None) -> dict[str, Any]:
    analysis = extract_analysis(last_analysis)
    manager = analysis.get("manager_action_block", {}) if isinstance(analysis, dict) else {}
    primary = manager.get("primary_text", {}) if isinstance(manager, dict) else {}
    return primary if isinstance(primary, dict) else {}


def render_mini_recommendation(
    *,
    deal_id: str,
    decision: ProcessingDecision,
    previous_state: dict[str, Any] | None,
    current_snapshot: dict[str, Any],
) -> str:
    previous_state = previous_state or {}
    last_analysis = previous_state.get("last_analysis")
    primary = last_primary_text(last_analysis)
    last_report = previous_state.get("last_report_path") or "не указан"
    risk_level = previous_state.get("last_risk_level") or analysis_risk_level(last_analysis, previous_state) or "не указан"

    trigger_lines = []
    for trigger in decision.triggers:
        detail_parts = []
        for key, value in trigger.items():
            if key == "trigger_type":
                continue
            detail_parts.append(f"{key}: {value}")
        suffix = f" ({'; '.join(detail_parts)})" if detail_parts else ""
        trigger_lines.append(f"{trigger_label(trigger)}{suffix}")

    manager_actions = [
        "Проверить открытую задачу и зафиксировать следующий шаг с датой.",
        "Если клиент после КП/счета не двигается, вернуть разговор к сроку решения, ЛПР и следующему шагу.",
        "Если риск высокий, обновить задачу в CRM и уведомить РОПа о статусе.",
    ]
    rop_checks = [
        "Есть ли в CRM открытая задача с актуальным сроком.",
        "Есть ли следующий шаг, срок решения и ответственный за действие.",
        "Нужно ли вмешательство РОПа по последнему полному отчету.",
    ]

    current_stage = (current_snapshot.get("deal") or {}).get("stage_id") or "не указан"
    current_assigned = (current_snapshot.get("deal") or {}).get("assigned_by_id") or "не указан"

    reused_text = "Сохраненного клиентского текста из последнего полного анализа нет."
    if primary.get("text"):
        subject = primary.get("subject") or ""
        reused_text = "\n".join(
            [
                "Ниже текст из последнего полного LLM-анализа. Новый текст без LLM не генерировался.",
                "",
                f"Тип: {primary.get('type') or 'не указан'}",
                f"Тема: {subject}",
                "",
                str(primary.get("text") or ""),
            ]
        )

    return f"""# Мини-рекомендация по сделке {deal_id}

Статус: {decision.status}

## Причины

{bullet_list(decision.reasons)}

## Триггеры

{bullet_list(trigger_lines)}

## Текущее состояние

- Этап Bitrix: {current_stage}
- Ответственный: {current_assigned}
- Последний риск из полного анализа: {risk_level}
- Последний полный отчет: {last_report}

## Что сделать менеджеру

{bullet_list(manager_actions)}

## Что проверить РОПу

{bullet_list(rop_checks)}

## Последний сохраненный текст клиенту

{reused_text}
"""


def save_mini_recommendation_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
