"""Independent personal-sales-coach jobs for the manager deal screen.

Canonical storage expected by this module:

``save_deal_manager_quick_help(db_path, *, deal_id, source_report_id,
situation_review_id, question, answer_json, model_meta=None)`` -> dict

``list_deal_manager_quick_help(db_path, *, deal_id, limit=20,
before_id=None)`` -> list[dict]

The API never reads or writes a previous quick-help answer into a new prompt.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bitrix.customer_history import build_normalized_communications
from bitrix.workspace import DEFAULT_RAW_DIR, deal_workspace_dir
from api.deal_manager_situation import (
    DEFAULT_DB_PATH,
    StorageContractUnavailable,
    _safe_model_meta,
    load_manager_screen_context,
    _situation_id,
    _situation_status,
    _storage_call,
    public_disc_profile,
)
from openai_api.llm.deal_manager_quick_help import ASSISTANT_MODES, generate_deal_manager_quick_help
from openai_api.llm.llm_client import ModelJsonParseError, ModelResponseIncompleteError
from setup import MSK_TZ

logger = logging.getLogger(__name__)

INCOMPLETE_QUICK_HELP_ERROR = "Ответ модели оборвался. Нажмите «Повторить»."
FORMAT_QUICK_HELP_ERROR = "Модель вернула ответ в неверном формате. Можно повторить."
VALIDATION_QUICK_HELP_ERROR = "Ответ модели не прошёл проверку. Нажмите «Повторить»."
UNAVAILABLE_QUICK_HELP_ERROR = "Сервис ответа сейчас недоступен. Попробуйте ещё раз."
GENERIC_QUICK_HELP_ERROR = "Не удалось подготовить дожим. Попробуйте ещё раз."

_KEEP_QUICK_HELP_ERRORS = (
    "Вопрос должен содержать от 1 до 4000 знаков",
    "Сначала подтвердите текущую ситуацию сделки",
    "Сначала проведите полный анализ сделки",
    "У полного анализа сделки отсутствует идентификатор",
    "У текущей ситуации отсутствует идентификатор",
    "Сделка не найдена в локальном контуре контроля",
)


MAX_QUESTION_CHARS = 4000
COMMUNICATION_WINDOW_DAYS = 30
COMMUNICATION_HISTORY_DAYS = 60
MAX_COMMUNICATION_EVENTS = 10
AUTO_QUESTIONS = {
    "push": "Сформируй текущий дожим сделки",
    "reanimator": "Сформируй текущую рекомендацию по восстановлению коммуникации",
}


def public_quick_help_error(error: BaseException) -> str:
    """Human job/HTTP error without class names, traces, prompts or cost."""
    if isinstance(error, ModelResponseIncompleteError):
        return INCOMPLETE_QUICK_HELP_ERROR
    if isinstance(error, ModelJsonParseError):
        return FORMAT_QUICK_HELP_ERROR
    text = str(error or "").strip()
    lowered = text.casefold()
    for known in _KEEP_QUICK_HELP_ERRORS:
        if known.casefold() in lowered:
            return known
    if "платный" in lowered or "confirm_paid" in lowered:
        return GENERIC_QUICK_HELP_ERROR
    if "incomplete" in lowered:
        return INCOMPLETE_QUICK_HELP_ERROR
    if "invalid json" in lowered or "json-объект" in lowered:
        return FORMAT_QUICK_HELP_ERROR
    if any(marker in lowered for marker in ("quick help", "lifehacks", "pressure_lever", "tactic_id", "answer_contract")):
        return VALIDATION_QUICK_HELP_ERROR
    if any(marker in lowered for marker in ("timeout", "connection", "api key", "openai")):
        return UNAVAILABLE_QUICK_HELP_ERROR
    return GENERIC_QUICK_HELP_ERROR


def _now() -> str:
    return datetime.now(MSK_TZ).isoformat(timespec="seconds")


@dataclass
class DealManagerQuickHelpJob:
    job_id: str
    deal_id: str
    question: str
    situation_id: int | None
    mode: str | None = None
    origin: str = "manager"
    turn_id: str | None = None
    status: str = "queued"
    stage: str = "queued"
    detail: str = "Подготавливаем ответ тренера"
    percent: int = 5
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    quick_help_id: int | None = None
    saved_by_mode: dict[str, int] = field(default_factory=dict)
    reused: bool = False
    error: str | None = None


_QUICK_HELP_JOBS: dict[str, DealManagerQuickHelpJob] = {}
_QUICK_HELP_LOCK = threading.Lock()


def _touch(job: DealManagerQuickHelpJob, *, stage: str, detail: str, percent: int) -> None:
    with _QUICK_HELP_LOCK:
        job.stage = stage
        job.detail = detail
        job.percent = percent
        job.updated_at = _now()


def get_quick_help_job(job_id: str) -> dict[str, Any] | None:
    with _QUICK_HELP_LOCK:
        job = _QUICK_HELP_JOBS.get(str(job_id))
        return asdict(job) if job else None


def _current_for_mode(db_path: str | Path, context: dict[str, Any], mode: str) -> dict[str, Any] | None:
    situation_id = context.get("situation_id")
    if situation_id is None:
        return None
    saved = _storage_call(
        "get_current_deal_manager_quick_help",
        db_path,
        deal_id=str(context["deal"]["deal_id"]),
        source_report_id=int(context["source_report_id"]),
        situation_review_id=int(situation_id),
        mode=mode,
    )
    return saved if isinstance(saved, dict) else None


def _save_mode_answer(
    *,
    db_path: str | Path,
    job: DealManagerQuickHelpJob,
    context: dict[str, Any],
    situation_id: int,
    mode: str,
    question: str,
    origin: str,
    communication_pattern_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    answer, metadata = generate_deal_manager_quick_help(
        question=question,
        analysis_projection=context["analysis_projection"],
        deal=context["deal"],
        current_bitrix_task=context["current_bitrix_task"],
        situation_projection=context["situation_projection"],
        communication_pattern_context=communication_pattern_context,
        mode=mode,
    )
    saved = _storage_call(
        "save_deal_manager_quick_help",
        db_path,
        deal_id=job.deal_id,
        source_report_id=context["source_report_id"],
        situation_review_id=situation_id,
        question=question,
        answer_json=answer,
        model_meta=_safe_model_meta(metadata),
        mode=mode,
        origin=origin,
        turn_id=job.turn_id,
    )
    saved_id = int(saved["id"]) if isinstance(saved, dict) and saved.get("id") is not None else None
    with _QUICK_HELP_LOCK:
        if saved_id is not None:
            job.saved_by_mode[mode] = saved_id
            job.quick_help_id = saved_id
    return (saved if isinstance(saved, dict) else {}), answer


def _run_quick_help_job(job_id: str, db_path: str | Path) -> None:
    with _QUICK_HELP_LOCK:
        job = _QUICK_HELP_JOBS[job_id]
        job.status = "running"
    try:
        _touch(job, stage="context", detail="Проверяем подтверждённую ситуацию сделки", percent=20)
        context = load_manager_screen_context(
            db_path,
            job.deal_id,
            require_confirmed_situation=True,
        )
        situation_id = context["situation_id"] or job.situation_id
        if situation_id is None:
            raise ValueError("У текущей ситуации отсутствует идентификатор")
        communication_pattern_context = build_communication_pattern_context(
            _load_local_communications(job.deal_id)
        )
        if job.origin == "auto" and not job.question.strip():
            for item_mode in ASSISTANT_MODES:
                current = _current_for_mode(db_path, context, item_mode)
                sibling_turn = str((current or {}).get("turn_id") or "").strip()
                if sibling_turn:
                    job.turn_id = sibling_turn
                    break
        if not job.turn_id:
            job.turn_id = uuid.uuid4().hex
        modes = list(ASSISTANT_MODES)
        generated = 0
        for index, mode in enumerate(modes):
            existing = _current_for_mode(db_path, context, mode) if job.origin == "auto" and not job.question.strip() else None
            if isinstance(existing, dict) and existing.get("id") is not None:
                with _QUICK_HELP_LOCK:
                    job.saved_by_mode[mode] = int(existing["id"])
                    job.quick_help_id = int(existing["id"])
                continue
            label = "дожим" if mode == "push" else "реаниматор"
            brain_percent = 28 + index * 36
            _touch(job, stage="llm", detail=f"AI готовит {label}", percent=brain_percent)
            question = job.question.strip() or AUTO_QUESTIONS[mode]
            saved, _answer = _save_mode_answer(
                db_path=db_path,
                job=job,
                context=context,
                situation_id=int(situation_id),
                mode=mode,
                question=question,
                origin=job.origin,
                communication_pattern_context=communication_pattern_context,
            )
            generated += 1
            saved_id = int(saved["id"]) if saved.get("id") is not None else None
            if saved_id is not None:
                _touch(job, stage="llm", detail=f"Совет «{label}» готов", percent=min(88, brain_percent + 18))
        with _QUICK_HELP_LOCK:
            job.reused = generated == 0
            job.status = "done"
        _touch(job, stage="done", detail="Пакет рекомендации готов", percent=100)
    except Exception as error:  # noqa: BLE001 - never return model content in a job error
        logger.exception("Quick help job %s failed for deal %s", job_id, job.deal_id)
        with _QUICK_HELP_LOCK:
            job.status = "error"
            job.error = public_quick_help_error(error)
        _touch(job, stage="error", detail="Не удалось подготовить ответ", percent=100)


def start_quick_help_job(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    question: str | None = None,
    confirm_paid: bool,
    mode: str | None = None,
) -> dict[str, Any]:
    normalized_question = str(question or "").strip()
    if normalized_question and not 1 <= len(normalized_question) <= MAX_QUESTION_CHARS:
        raise ValueError("Вопрос должен содержать от 1 до 4000 знаков")
    if mode is not None and mode not in ASSISTANT_MODES:
        raise ValueError("mode должен быть push или reanimator")
    origin = "manager" if normalized_question else "auto"
    if origin == "manager" and mode is None:
        mode = "reanimator"
    # Уточнение в чате всегда платное. Автогенерация может переиспользовать
    # уже сохранённые режимы без LLM, поэтому confirm_paid проверяем после контекста.
    if origin == "manager" and not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для quick help")
    context = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    if origin == "auto":
        saved_by_mode: dict[str, int] = {}
        missing: list[str] = []
        for item_mode in ASSISTANT_MODES:
            current = _current_for_mode(db_path, context, item_mode)
            if isinstance(current, dict) and current.get("id") is not None:
                saved_by_mode[item_mode] = int(current["id"])
            else:
                missing.append(item_mode)
        if not missing:
            job = DealManagerQuickHelpJob(
                job_id=uuid.uuid4().hex,
                deal_id=str(deal_id),
                question="",
                situation_id=context["situation_id"],
                mode=mode,
                origin="auto",
                status="done",
                stage="done",
                detail="Открываем сохранённую актуальную рекомендацию",
                percent=100,
                quick_help_id=saved_by_mode.get("push") or next(iter(saved_by_mode.values()), None),
                saved_by_mode=saved_by_mode,
                reused=True,
            )
            with _QUICK_HELP_LOCK:
                _QUICK_HELP_JOBS[job.job_id] = job
            return asdict(job)
    if not confirm_paid:
        raise ValueError("Подтвердите платный AI-вызов для quick help")
    with _QUICK_HELP_LOCK:
        existing = next(
            (
                item
                for item in _QUICK_HELP_JOBS.values()
                if item.deal_id == str(deal_id) and item.status in {"queued", "running"}
            ),
            None,
        )
        if existing is not None:
            return asdict(existing)
        job_id = uuid.uuid4().hex
        job = DealManagerQuickHelpJob(
            job_id=job_id,
            deal_id=str(deal_id),
            question=normalized_question,
            situation_id=context["situation_id"],
            mode=mode,
            origin=origin,
            turn_id=uuid.uuid4().hex,
        )
        _QUICK_HELP_JOBS[job_id] = job
    thread = threading.Thread(target=_run_quick_help_job, args=(job_id, db_path), daemon=True)
    thread.start()
    return asdict(job)


def list_quick_help_history(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    limit: int = 20,
    before_id: int | None = None,
) -> dict[str, Any]:
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit должен быть от 1 до 100")
    if before_id is not None and int(before_id) < 1:
        raise ValueError("before_id должен быть положительным")
    context = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    items = _storage_call(
        "list_deal_manager_quick_help",
        db_path,
        deal_id=str(deal_id),
        limit=int(limit),
        before_id=int(before_id) if before_id is not None else None,
    )
    return {"items": items if isinstance(items, list) else []}


def load_local_communication_bundle(deal_id: str) -> dict[str, Any]:
    paths = (
        deal_workspace_dir(str(deal_id)) / "raw" / f"deal_{deal_id}_customer_history_bundle.json",
        DEFAULT_RAW_DIR / f"deal_{deal_id}_customer_history_bundle.json",
    )
    bundle: dict[str, Any] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            bundle = value
            break
    return bundle


def _load_local_communications(deal_id: str) -> list[dict[str, Any]]:
    bundle = load_local_communication_bundle(str(deal_id))
    if not bundle:
        return []
    events = bundle.get("normalized_communications")
    if not isinstance(events, list):
        events = build_normalized_communications(bundle)
    return [
        item for item in events
        if isinstance(item, dict)
        and str(item.get("contact_class") or "") != "internal_information"
    ]


def _communication_datetime(item: dict[str, Any]) -> datetime | None:
    raw_value = str(item.get("occurred_at") or "").strip()
    if not raw_value:
        return None
    try:
        value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=MSK_TZ)
    return value.astimezone(MSK_TZ)


def _project_communication(item: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {
        "occurred_at": str(item.get("occurred_at") or ""),
        "channel": str(item.get("channel") or "unknown"),
        "direction": str(item.get("direction") or "unknown"),
        "contact_class": str(item.get("contact_class") or "unknown"),
    }
    duration = item.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        projected["duration_seconds"] = int(duration)
    return projected


def build_communication_pattern_context(
    events: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a bounded fact-only context without message bodies or transcripts."""
    current = now or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MSK_TZ)
    current = current.astimezone(MSK_TZ)
    dated = [
        (occurred_at, item)
        for item in events
        if isinstance(item, dict)
        and str(item.get("contact_class") or "") != "internal_information"
        and (occurred_at := _communication_datetime(item)) is not None
        and occurred_at <= current
    ]
    dated.sort(key=lambda pair: pair[0], reverse=True)
    recent_cutoff = current - timedelta(days=COMMUNICATION_WINDOW_DAYS)
    history_cutoff = current - timedelta(days=COMMUNICATION_HISTORY_DAYS)
    recent = [(occurred_at, item) for occurred_at, item in dated if occurred_at >= recent_cutoff]
    attempts = [
        item for _, item in recent
        if str(item.get("direction") or "") == "outgoing"
        and str(item.get("contact_class") or "") == "attempt"
    ]
    confirmed = [
        item for _, item in recent
        if str(item.get("contact_class") or "") == "confirmed_contact"
    ]
    attempts_by_channel: dict[str, int] = {}
    for item in attempts:
        channel = str(item.get("channel") or "unknown")
        attempts_by_channel[channel] = attempts_by_channel.get(channel, 0) + 1
    attempts_since_contact = 0
    for _, item in recent:
        if str(item.get("contact_class") or "") == "confirmed_contact":
            break
        if (
            str(item.get("direction") or "") == "outgoing"
            and str(item.get("contact_class") or "") == "attempt"
        ):
            attempts_since_contact += 1
    last_confirmed = next(
        (
            item for occurred_at, item in dated
            if occurred_at >= history_cutoff
            and str(item.get("contact_class") or "") == "confirmed_contact"
        ),
        None,
    )
    return {
        "window_days": COMMUNICATION_WINDOW_DAYS,
        "max_recent_events": MAX_COMMUNICATION_EVENTS,
        "total_attempts": len(attempts),
        "confirmed_contacts": len(confirmed),
        "attempts_by_channel": dict(sorted(attempts_by_channel.items())),
        "consecutive_attempts_without_contact": attempts_since_contact,
        "last_confirmed_contact": _project_communication(last_confirmed) if last_confirmed else None,
        "recent_events": [
            _project_communication(item)
            for _, item in recent[:MAX_COMMUNICATION_EVENTS]
        ],
    }


def _communication_text(item: dict[str, Any]) -> str:
    channel = str(item.get("channel") or "").lower()
    direction = str(item.get("direction") or "").lower()
    subject = str(item.get("subject") or "").strip()
    preview = str(item.get("preview") or "").strip()
    labels = {
        "call": "Звонок",
        "email": "Email",
        "message": "Сообщение",
        "whatsapp": "WhatsApp",
        "telegram": "Telegram",
        "max": "Max",
    }
    label = labels.get(channel, str(item.get("source_label") or "Коммуникация"))
    direction_label = "клиента" if direction == "incoming" else "менеджера" if direction == "outgoing" else ""
    detail = subject or preview
    prefix = " ".join(value for value in (label, direction_label) if value)
    return f"{prefix}: {detail}" if detail else prefix


def _main_risk(analysis: dict[str, Any], situation: dict[str, Any]) -> str:
    risk = analysis.get("main_risk")
    if isinstance(risk, dict):
        for key in ("description", "summary", "risk", "title"):
            value = str(risk.get(key) or "").strip()
            if value:
                return value
    elif isinstance(risk, str) and risk.strip():
        return risk.strip()
    return str(situation.get("what_blocks_progress") or "").strip()


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _public_deal_card(
    deal: dict[str, Any],
    analysis: dict[str, Any],
    raw_card: Any,
) -> dict[str, Any]:
    """Собрать карточку сделки: CRM-поля надёжнее, модель дополняет пробелы."""
    card = _dict(raw_card)
    deal_state = _dict(analysis.get("deal_state"))
    assessment = _dict(analysis.get("qualification_assessment"))
    solution = _dict(assessment.get("solution_fit"))
    commercial = _dict(assessment.get("commercial_fit"))
    equipment = str(card.get("equipment") or "").strip()
    if not equipment:
        equipment_type = str(solution.get("equipment_type") or "").strip()
        equipment = equipment_type if equipment_type and equipment_type != "unknown" else ""
    amount = deal.get("amount")
    if amount in (None, ""):
        amount = card.get("amount")
    if amount in (None, ""):
        amount = deal_state.get("amount")
    if amount in (None, "") and commercial.get("confirmed_budget_rub") is not None:
        amount = commercial.get("confirmed_budget_rub")
    return {
        "title": str(deal.get("title") or "").strip(),
        "company": str(card.get("company") or "").strip(),
        "equipment": equipment,
        "manufacturing_days": card.get("manufacturing_days"),
        "amount": amount,
        "currency_id": deal.get("currency_id"),
        "responsible": str(deal.get("manager_name") or card.get("responsible") or "").strip(),
        "stage": str(deal.get("stage_name") or deal_state.get("stage") or "").strip(),
    }


def _fallback_deal_context(
    analysis: dict[str, Any],
    situation: dict[str, Any],
    deal: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    """Build a safe display-only context for reports created before deal_context."""
    deal_state = analysis.get("deal_state") if isinstance(analysis.get("deal_state"), dict) else {}
    brief = analysis.get("deal_control_brief") if isinstance(analysis.get("deal_control_brief"), dict) else {}
    money = analysis.get("money_path_diagnosis") if isinstance(analysis.get("money_path_diagnosis"), dict) else {}
    assessment = analysis.get("qualification_assessment") if isinstance(analysis.get("qualification_assessment"), dict) else {}
    bant = assessment.get("bant") if isinstance(assessment.get("bant"), dict) else {}
    timeframe = bant.get("timeframe") if isinstance(bant.get("timeframe"), dict) else {}
    commercial = assessment.get("commercial_fit") if isinstance(assessment.get("commercial_fit"), dict) else {}
    risk = _main_risk(analysis, situation)

    critical_facts: list[dict[str, Any]] = []
    levers: list[dict[str, Any]] = []
    launch_timing = str(timeframe.get("need_or_launch_timing") or "").strip()
    timing_evidence = [str(item).strip() for item in timeframe.get("evidence", []) if str(item).strip()][:5]
    if launch_timing:
        timing_confirmed = timeframe.get("need_or_launch_timing_status") == "confirmed"
        critical_facts.append({
            "fact_id": "launch_timing",
            "category": "deadline",
            "fact": launch_timing,
            "status": "confirmed" if timing_confirmed else "needs_confirmation",
            "importance": "high",
            "observed_at": None,
            "source_type": "model_inference",
            "evidence": timing_evidence or [launch_timing],
        })
        levers.append({
            "lever_id": "launch_timing",
            "type": "deadline",
            "title": "Срок потребности или запуска",
            "fact": launch_timing,
            "why_important": "Срок ограничивает окно для решения и выполнения обязательств.",
            "business_consequence": "Актуальность срока и реалистичность исполнения необходимо подтвердить.",
            "basis_status": "confirmed" if timing_confirmed else "needs_confirmation",
            "status": "active",
            "ai_priority": 1,
            "evidence": timing_evidence or [launch_timing],
        })
    budget = commercial.get("confirmed_budget_rub")
    if budget is not None:
        budget_text = f"Подтверждённый бюджет: {budget} ₽"
        budget_evidence = [str(item).strip() for item in commercial.get("evidence", []) if str(item).strip()][:5]
        critical_facts.append({
            "fact_id": "confirmed_budget",
            "category": "budget",
            "fact": budget_text,
            "status": "confirmed",
            "importance": "high",
            "observed_at": None,
            "source_type": "model_inference",
            "evidence": budget_evidence or [budget_text],
        })
        levers.append({
            "lever_id": "confirmed_budget",
            "type": "budget",
            "title": "Бюджетное ограничение",
            "fact": budget_text,
            "why_important": "Бюджет определяет реалистичный состав решения.",
            "business_consequence": "Несопоставимый состав предложения может остановить решение.",
            "basis_status": "confirmed",
            "status": "active",
            "ai_priority": 2 if levers else 1,
            "evidence": budget_evidence or [budget_text],
        })
    pain_points = []
    if risk:
        pain_points.append({
            "pain_id": "main_risk",
            "title": "Главный риск",
            "description": risk,
            "status": "active",
            "impact": str(money.get("why_money_is_at_risk") or risk),
            "evidence": [str(item).strip() for item in money.get("evidence", []) if str(item).strip()][:5] or [risk],
        })
    changes = [str(item).strip() for item in analysis.get("what_changed", []) if str(item).strip()]
    turning_points = [{
        "turning_point_id": f"change_{index}",
        "occurred_at": None,
        "title": f"Изменение {index}",
        "what_happened": text,
        "impact": text,
        "status": "active",
        "evidence": [text],
    } for index, text in enumerate(changes[:8], start=1)]
    missing = [str(item).strip() for item in brief.get("missing_facts", []) if str(item).strip()]
    missing.extend(str(item).strip() for item in bant.get("missing_facts", []) if str(item).strip())
    open_questions = list(dict.fromkeys(missing))[:7]
    return {
        "current_truth": {
            "client_profile": str(deal_state.get("client") or deal.get("title") or "Не определён"),
            "current_need": str((bant.get("need") or {}).get("evidence", [""])[0] if isinstance(bant.get("need"), dict) and (bant.get("need") or {}).get("evidence") else deal_state.get("summary") or "Не определена"),
            "desired_outcome": str(situation.get("target_result") or brief.get("contact_goal") or "Требует уточнения"),
            "current_status": str(situation.get("current_situation") or brief.get("current_situation") or deal_state.get("summary") or "Не определён"),
            "current_task": str(task.get("subject") or task.get("description") or "Нет открытой задачи"),
            "next_checkpoint": task.get("deadline"),
            "next_step_owner": str(money.get("current_owner_of_next_step") or "unknown"),
        },
        "critical_facts": critical_facts,
        "turning_points": turning_points,
        "pain_points": pain_points,
        "pressure_levers": levers,
        "open_questions": open_questions,
        "source_conflicts": [],
        "deal_card": {
            "company": "Не указана",
            "equipment": "Не указано",
            "manufacturing_days": None,
            "amount": deal.get("amount") or deal_state.get("amount"),
            "responsible": str(deal.get("manager_name") or "Не указан"),
        },
        "decision_path": {
            "decision_maker": "Не подтверждён",
            "influencers": [],
            "approval_path": "Недостаточно данных для маршрута согласования",
            "current_step_owner": str(money.get("current_owner_of_next_step") or "unknown"),
            "basis_status": "needs_confirmation",
            "evidence": ["В старом отчёте нет отдельного маршрута решения."],
        },
        "commitments": [],
        "journey": [],
    }


def _public_deal_context(
    analysis: dict[str, Any],
    situation: dict[str, Any],
    deal: dict[str, Any],
    task: dict[str, Any],
    priority_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = analysis.get("deal_context")
    context = dict(raw) if isinstance(raw, dict) and raw else _fallback_deal_context(analysis, situation, deal, task)
    manual = {str(item.get("lever_id") or ""): item.get("priority") for item in priority_rows}
    levers = []
    for raw_lever in context.get("pressure_levers", []):
        if not isinstance(raw_lever, dict):
            continue
        lever = dict(raw_lever)
        lever_id = str(lever.get("lever_id") or "")
        lever["manual_priority"] = manual.get(lever_id)
        levers.append(lever)
    context["pressure_levers"] = levers
    context["deal_card"] = _public_deal_card(deal, analysis, context.get("deal_card"))
    context.setdefault("decision_path", None)
    context.setdefault("commitments", [])
    context.setdefault("journey", [])
    # BANT, путь к деньгам и конкурент уже посчитаны в полном анализе — не просим модель дублировать их.
    assessment = _dict(analysis.get("qualification_assessment"))
    context["bant"] = _dict(assessment.get("bant")) or None
    context["solution_fit"] = _dict(assessment.get("solution_fit")) or None
    context["commercial_fit"] = _dict(assessment.get("commercial_fit")) or None
    context["money_path"] = _dict(analysis.get("money_path_diagnosis")) or None
    payment = _dict(analysis.get("payment_blocker"))
    context["payment_blocker"] = payment or None
    context["competitor"] = _dict(analysis.get("competitor_defense_checklist")) or None
    return context


def get_manager_assistant_workspace(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
) -> dict[str, Any]:
    """Project the existing quick-help rows and local CRM history for one deal."""
    context = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    items = _storage_call(
        "list_deal_manager_quick_help",
        db_path,
        deal_id=str(deal_id),
        limit=100,
        before_id=None,
    )
    entries = items if isinstance(items, list) else []
    assistant_events = _storage_call(
        "list_deal_manager_assistant_events",
        db_path,
        deal_id=str(deal_id),
        limit=100,
    )
    local_events = assistant_events if isinstance(assistant_events, list) else []
    communications = _load_local_communications(str(deal_id))
    timeline: list[dict[str, Any]] = []
    for entry in entries:
        origin = str(entry.get("origin") or "manager")
        mode = str(entry.get("mode") or "reanimator")
        mode_label = "Дожим" if mode == "push" else "Реаниматор"
        if origin == "auto":
            text = f"Система сформировала рекомендацию: {mode_label}"
        else:
            text = f"Менеджер уточнил {mode_label}: {str(entry.get('question') or '').strip()}"
        timeline.append({
            "id": f"assistant:{entry.get('id')}",
            "kind": "assistant_request",
            "occurred_at": entry.get("created_at"),
            "text": text,
        })
    for event in local_events:
        timeline.append({
            "id": f"assistant-event:{event.get('id')}",
            "kind": str(event.get("event_type") or "assistant_event"),
            "occurred_at": event.get("created_at"),
            "text": "Менеджер отметил коммуникацию выполненной.",
        })
    for item in communications:
        text = _communication_text(item)
        if text:
            timeline.append({
                "id": str(item.get("event_id") or f"communication:{len(timeline)}"),
                "kind": "communication",
                "occurred_at": item.get("occurred_at"),
                "text": text,
                "channel": item.get("channel"),
                "contact_class": item.get("contact_class"),
            })
    timeline.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
    latest_communication = max(
        communications,
        key=lambda item: str(item.get("occurred_at") or ""),
        default=None,
    )
    task = context.get("current_bitrix_task") if isinstance(context.get("current_bitrix_task"), dict) else {}
    deal = context.get("deal") if isinstance(context.get("deal"), dict) else {}
    analysis = context.get("analysis_projection") if isinstance(context.get("analysis_projection"), dict) else {}
    situation = context.get("situation_projection") if isinstance(context.get("situation_projection"), dict) else {}
    current_by_mode: dict[str, Any] = {"push": None, "reanimator": None}
    source_report_id = context.get("source_report_id")
    situation_id = context.get("situation_id")
    for entry in entries:
        mode = str(entry.get("mode") or "reanimator")
        if mode not in current_by_mode:
            continue
        if current_by_mode[mode] is not None:
            continue
        if source_report_id is not None and int(entry.get("source_report_id") or 0) != int(source_report_id):
            continue
        if situation_id is not None and int(entry.get("situation_review_id") or 0) != int(situation_id):
            continue
        current_by_mode[mode] = entry
    priority_rows = (
        _storage_call(
            "list_deal_context_lever_priorities",
            db_path,
            deal_id=str(deal_id),
            source_report_id=int(source_report_id),
        )
        if source_report_id is not None
        else []
    )
    report = context.get("report") if isinstance(context.get("report"), dict) else {}
    report_path = Path(str(report.get("report_path") or "")) if report.get("report_path") else None
    return {
        "started": bool(entries),
        "entries": entries,
        "current_by_mode": current_by_mode,
        "source_report_id": source_report_id,
        "situation_review_id": situation_id,
        "timeline": timeline[:50],
        "disc_profile": public_disc_profile(analysis),
        "context": {
            "stage": str(deal.get("stage_name") or ""),
            "current_task": str(task.get("subject") or task.get("description") or ""),
            "last_communication": {
                "event_id": latest_communication.get("event_id"),
                "channel": latest_communication.get("channel"),
                "occurred_at": latest_communication.get("occurred_at"),
                "text": _communication_text(latest_communication),
            } if latest_communication else None,
            "main_risk": _main_risk(analysis, situation),
            "deal_context": _public_deal_context(
                analysis,
                situation,
                deal,
                task,
                priority_rows if isinstance(priority_rows, list) else [],
            ),
            "report": {
                "report_id": source_report_id,
                "markdown_available": bool(report_path and report_path.is_file()),
            },
        },
    }


def set_deal_context_lever_priority(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    lever_id: str,
    priority: int | None,
    actor_role: str,
) -> dict[str, Any]:
    context = load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    source_report_id = context.get("source_report_id")
    if source_report_id is None:
        raise ValueError("Для сделки ещё нет полного отчёта")
    analysis = context.get("analysis_projection") if isinstance(context.get("analysis_projection"), dict) else {}
    situation = context.get("situation_projection") if isinstance(context.get("situation_projection"), dict) else {}
    deal = context.get("deal") if isinstance(context.get("deal"), dict) else {}
    task = context.get("current_bitrix_task") if isinstance(context.get("current_bitrix_task"), dict) else {}
    public_context = _public_deal_context(analysis, situation, deal, task, [])
    available_ids = {
        str(item.get("lever_id") or "")
        for item in public_context.get("pressure_levers", [])
        if isinstance(item, dict)
    }
    normalized_lever_id = str(lever_id).strip()
    if normalized_lever_id not in available_ids:
        raise ValueError("Выбранный рычаг отсутствует в текущем отчёте")
    event = _storage_call(
        "save_deal_context_lever_priority",
        db_path,
        deal_id=str(deal_id),
        source_report_id=int(source_report_id),
        lever_id=normalized_lever_id,
        priority=priority,
        actor_role=actor_role,
    )
    rows = _storage_call(
        "list_deal_context_lever_priorities",
        db_path,
        deal_id=str(deal_id),
        source_report_id=int(source_report_id),
    )
    return {"ok": True, "event": event, "priorities": rows if isinstance(rows, list) else []}


def record_manager_communication_completed(
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    deal_id: str,
    quick_help_id: int,
) -> dict[str, Any]:
    load_manager_screen_context(
        db_path,
        str(deal_id),
        require_confirmed_situation=True,
    )
    return _storage_call(
        "record_deal_manager_assistant_event",
        db_path,
        deal_id=str(deal_id),
        event_type="communication_completed",
        quick_help_id=int(quick_help_id),
        payload=None,
    )
