"""Human-readable daily OpenAI spend diary. Estimate only, not an invoice."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from setup import LOGS_DIR, MSK_TZ


logger = logging.getLogger(__name__)

DIR_ENV = "SPEND_DIARY_DIR"
BATCH_ENV = "SPEND_DIARY_BATCH_PATH"
RUN_ENV = "SPEND_DIARY_RUN_ID"
JOB_ENV = "SPEND_DIARY_JOB_ID"
DEFAULT_DIR = LOGS_DIR / "daily_spend"
HEADER = "Дневник трат OpenAI. Оценка по тарифу проекта, это не счёт OpenAI.\n"
MAX_ENTITY_ID_CHARS = 40
MINI_LIST_LIMIT = 5

_WRITE_LOCK = threading.Lock()

KIND_LABELS = {
    "full_deal_analysis": "полный анализ",
    "full_lead_analysis": "полный анализ лида",
    "full_analysis": "полный анализ",
    "deal_manager_quick_help_push": "Quick Help",
    "deal_manager_quick_help_reanimator": "Quick Help (дожим)",
    "deal_manager_followups": "фоллоуапы",
    "deal_manager_companion": "сопроводительный текст",
    "deal_manager_situation": "ситуация",
    "deal_manager_email": "письмо",
    "deal_manager_strategy_pack": "стратегия",
    "deal_task_guidance": "подсказка к задаче",
    "transcription": "транскрибация",
    "transcription_voice": "транскрибация голоса",
}


def spend_diary_dir() -> Path:
    configured = os.getenv(DIR_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_DIR


def _now_msk(now: datetime | None = None) -> datetime:
    current = now or datetime.now(MSK_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MSK_TZ)
    return current.astimezone(MSK_TZ)


def _day_stamp(now: datetime | None = None) -> str:
    return _now_msk(now).strftime("%Y-%m-%d")


def human_diary_path(now: datetime | None = None) -> Path:
    return spend_diary_dir() / f"{_day_stamp(now)}.txt"


def events_path(now: datetime | None = None) -> Path:
    return spend_diary_dir() / f"{_day_stamp(now)}.events.jsonl"


def new_cycle_batch_path(now: datetime | None = None) -> Path:
    stamp = _now_msk(now).strftime("%Y%m%d-%H%M%S")
    return spend_diary_dir() / "batches" / f"{stamp}-{uuid.uuid4().hex[:8]}.jsonl"


def format_rub(value: float | None) -> str:
    if value is None:
        return "оценка недоступна"
    if abs(value - round(value)) < 0.005:
        return f"~{int(round(value))} ₽"
    return f"~{value:.2f} ₽"


def kind_label(kind: str | None) -> str:
    raw = str(kind or "").strip()
    if raw in KIND_LABELS:
        return KIND_LABELS[raw]
    if raw.startswith("deal_manager_quick_help_"):
        return "Quick Help"
    if raw.startswith("deal_manager_full_script_"):
        return "полный скрипт"
    if "attention_delta" in raw:
        return "compact"
    return raw.replace("_", " ") or "вызов OpenAI"


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _clean_entity_id(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    token = text.split(" ", 1)[0] if text else ""
    return token[:MAX_ENTITY_ID_CHARS]


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _entity_word(entity_type: str | None) -> str:
    if entity_type == "lead":
        return "лид"
    return "сделка"


def _append_line(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _ensure_header(path: Path) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    _append_line(path, HEADER + "\n")


def _write_event_line(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def load_batch_events(path: str | Path | None) -> list[dict[str, Any]]:
    if not path:
        return []
    return _read_jsonl(Path(path))


def day_total_rub(now: datetime | None = None) -> float:
    total = 0.0
    for event in _read_jsonl(events_path(now)):
        rub = _as_float(event.get("estimated_cost_rub"))
        if rub is not None:
            total += rub
    return total


def events_total_rub(events: list[dict[str, Any]]) -> float:
    total = 0.0
    for event in events:
        rub = _as_float(event.get("estimated_cost_rub"))
        if rub is not None:
            total += rub
    return total


def _is_transcription(kind: str | None) -> bool:
    return str(kind or "").startswith("transcription")


def record_transcription_spend(
    *,
    duration_seconds: float | None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    kind: str = "transcription",
    model: str | None = None,
    attempt: int | None = None,
) -> dict[str, Any] | None:
    """Record one successful transcription HTTP call using the project estimate."""
    try:
        from openai_api.config import TRANSCRIPTION_MODEL, USD_RUB_RATE
        from openai_api.pricing import estimate_transcription_cost

        used_model = (model or TRANSCRIPTION_MODEL).strip() or TRANSCRIPTION_MODEL
        cost = estimate_transcription_cost(used_model, duration_seconds, USD_RUB_RATE)
        return record_paid_call(
            kind=kind,
            estimated_cost_rub=cost.get("estimated_cost_rub"),
            estimated_cost_usd=cost.get("estimated_cost_usd"),
            entity_type=entity_type,
            entity_id=entity_id,
            model=used_model,
            status="success",
            attempt=attempt,
        )
    except Exception as error:  # noqa: BLE001 - diary is best-effort
        logger.warning("Не удалось записать трату транскрибации: %s", type(error).__name__)
        return None


def record_paid_call(
    *,
    kind: str,
    estimated_cost_rub: float | None,
    estimated_cost_usd: float | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    model: str | None = None,
    status: str = "success",
    attempt: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Append one paid OpenAI call. Never raises into the caller."""
    if not str(kind or "").strip():
        return None
    moment = _now_msk(now)
    event = {
        "at": moment.isoformat(timespec="seconds"),
        "kind": _clean_text(kind)[:80],
        "entity_type": _clean_text(entity_type) or None,
        "entity_id": _clean_entity_id(entity_id) or None,
        "estimated_cost_rub": _as_float(estimated_cost_rub),
        "estimated_cost_usd": _as_float(estimated_cost_usd),
        "model": _clean_text(model)[:80] or None,
        "status": _clean_text(status)[:40] or "success",
        "attempt": int(attempt) if isinstance(attempt, int) and attempt > 0 else None,
        "run_id": _clean_text(os.getenv(RUN_ENV))[:80] or None,
        "job_id": _clean_text(os.getenv(JOB_ENV))[:80] or None,
    }
    try:
        with _WRITE_LOCK:
            _write_event_line(events_path(moment), event)
            batch_path = os.getenv(BATCH_ENV, "").strip()
            if batch_path:
                _write_event_line(Path(batch_path), event)
            else:
                _write_adhoc_line(event, moment)
        return event
    except (OSError, TypeError, ValueError) as error:
        logger.warning("Не удалось записать дневник трат: %s", type(error).__name__)
        return None


def _write_adhoc_line(event: dict[str, Any], moment: datetime) -> None:
    path = human_diary_path(moment)
    _ensure_header(path)
    parts = [moment.strftime("%H:%M"), kind_label(str(event.get("kind") or ""))]
    entity_id = event.get("entity_id")
    if entity_id:
        parts.append(f"{_entity_word(event.get('entity_type'))} {entity_id}")
    parts.append(format_rub(_as_float(event.get("estimated_cost_rub"))))
    today = format_rub(day_total_rub(moment))
    line = f"{parts[0]}  {' · '.join(parts[1:])}  (сегодня {today})\n"
    _append_line(path, line)


def render_cycle_text(
    *,
    started: datetime,
    counts: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
    busy_ids: list[str] | None = None,
    today_rub: float = 0.0,
    status: str | None = None,
    message: str | None = None,
) -> str:
    moment = _now_msk(started)
    header = moment.strftime("%d.%m.%Y %H:%M МСК")
    if status == "skipped_unconfigured":
        lines = [
            header,
            _clean_text(message) or "Цикл пропущен: выборка сделок ещё не настроена.",
            f"За сегодня: {format_rub(today_rub)}",
            "",
        ]
        return "\n".join(lines)

    checked = int(counts.get("checked") or 0)
    full = int(counts.get("full") or 0)
    mini = int(counts.get("mini") or 0)
    skip = int(counts.get("skip") or 0)
    error = int(counts.get("error") or 0)
    full_ids = [_clean_entity_id(item) for item in (counts.get("full_ids") or []) if _clean_entity_id(item)]
    mini_ids = [_clean_entity_id(item) for item in (counts.get("mini_ids") or []) if _clean_entity_id(item)]
    paid_events = list(events or [])
    run_rub = events_total_rub(paid_events)
    by_entity: dict[str, list[dict[str, Any]]] = {}
    for event in paid_events:
        entity_id = _clean_entity_id(event.get("entity_id"))
        if not entity_id:
            by_entity.setdefault("", []).append(event)
            continue
        by_entity.setdefault(entity_id, []).append(event)

    lines = [
        header,
        f"Проверено сделок: {checked}",
        f"FULL: {full} | MINI: {mini} | без изменений: {skip}",
        "",
    ]

    listed_ids: set[str] = set()
    for entity_id in full_ids:
        listed_ids.add(entity_id)
        entity_events = by_entity.get(entity_id) or []
        llm_events = [item for item in entity_events if not _is_transcription(item.get("kind"))]
        transcribe_events = [item for item in entity_events if _is_transcription(item.get("kind"))]
        llm_rub = events_total_rub(llm_events) if llm_events else None
        transcribe_rub = events_total_rub(transcribe_events) if transcribe_events else None
        if transcribe_rub is not None:
            cost_bit = f"{format_rub(llm_rub)}; транскрибация {format_rub(transcribe_rub)}"
        else:
            cost_bit = format_rub(llm_rub)
        lines.append(f"Сделка {entity_id} — полный анализ, {cost_bit}")

    if len(mini_ids) <= MINI_LIST_LIMIT:
        for entity_id in mini_ids:
            listed_ids.add(entity_id)
            lines.append(f"Сделка {entity_id} — мини, без LLM")
    elif mini_ids:
        listed_ids.update(mini_ids)
        lines.append(f"{len(mini_ids)} сделок — мини, без LLM")

    for entity_id, entity_events in by_entity.items():
        if not entity_id or entity_id in listed_ids:
            continue
        label = kind_label(str(entity_events[0].get("kind") or "transcription"))
        lines.append(f"Сделка {entity_id} — {label}, {format_rub(events_total_rub(entity_events))}")

    unknown = by_entity.get("") or []
    if unknown:
        labels = ", ".join(sorted({kind_label(str(item.get("kind") or "")) for item in unknown}))
        lines.append(f"Прочее ({labels}): {format_rub(events_total_rub(unknown))}")

    if skip:
        if skip == checked and full == 0 and mini == 0:
            lines.append(f"{skip} — без изменений, LLM не вызывался")
        else:
            lines.append(f"Остальные {skip} — без изменений, LLM не вызывался")

    busy = [_clean_entity_id(item) for item in (busy_ids or []) if _clean_entity_id(item)]
    if busy:
        lines.append(f"Пропущены, уже анализируются: {len(busy)}")
    if error:
        lines.append(f"Без записанного решения: {error}")

    lines.append("")
    lines.append(f"За этот запуск: {format_rub(run_rub)}")
    lines.append(f"За сегодня: {format_rub(today_rub)}")
    lines.append("")
    return "\n".join(lines)


def write_cycle_block(
    *,
    started: datetime,
    counts: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    busy_ids: list[str] | None = None,
    status: str | None = None,
    message: str | None = None,
    now: datetime | None = None,
) -> None:
    """Append one cycle summary. Never raises into the scheduler."""
    if status == "skipped_locked":
        return
    moment = _now_msk(now or started)
    try:
        with _WRITE_LOCK:
            path = human_diary_path(moment)
            _ensure_header(path)
            text = render_cycle_text(
                started=started,
                counts=counts or {},
                events=events or [],
                busy_ids=busy_ids,
                today_rub=day_total_rub(moment),
                status=status,
                message=message,
            )
            _append_line(path, text)
    except (OSError, TypeError, ValueError) as error:
        logger.warning("Не удалось записать блок дневного цикла: %s", type(error).__name__)
