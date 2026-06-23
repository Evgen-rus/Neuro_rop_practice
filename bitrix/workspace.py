"""
Helpers for per-entity working folders used in the semi-manual ROP assistant flow.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from setup import BASE_DIR, MSK_TZ


DEFAULT_LEAD_WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "leads"
DEFAULT_DEAL_WORKSPACE_ROOT = BASE_DIR / "reports" / "rop_assistant" / "deals"
DEFAULT_WORKSPACE_ROOT = DEFAULT_LEAD_WORKSPACE_ROOT
DEFAULT_HISTORY_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "markdown"
DEFAULT_RAW_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "raw"
DEFAULT_AUDIO_MANIFEST_DIR = BASE_DIR / "reports" / "bitrix_customer_path" / "audio"
DEFAULT_LEAD_HISTORY_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "markdown"
DEFAULT_LEAD_RAW_DIR = BASE_DIR / "reports" / "bitrix_lead_path" / "raw"


def safe_slug(value: str, max_length: int = 90) -> str:
    cleaned = re.sub(r"[^\wа-яА-ЯёЁ.-]+", "_", value, flags=re.U).strip("._")
    if not cleaned:
        return "item"
    return cleaned[:max_length].strip("._") or "item"


def normalize_dt_for_filename(value: str | None) -> str:
    if not value:
        return datetime.now(MSK_TZ).strftime("%Y-%m-%d_%H-%M-%S")

    cleaned = value.strip()
    for old, new in (("T", "_"), (":", "-"), ("+", "_plus_")):
        cleaned = cleaned.replace(old, new)
    cleaned = re.sub(r"[^0-9A-Za-zа-яА-ЯёЁ_.-]+", "_", cleaned, flags=re.U)
    return cleaned.strip("._") or datetime.now(MSK_TZ).strftime("%Y-%m-%d_%H-%M-%S")


def entity_workspace_dir(
    entity_id: str,
    entity_type: str = "lead",
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
) -> Path:
    entity_type = safe_slug(entity_type.lower())
    return workspace_root / f"{entity_type}_{safe_slug(str(entity_id))}"


def ensure_entity_workspace(
    entity_id: str,
    entity_type: str = "lead",
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT,
    history_dir: Path = DEFAULT_HISTORY_DIR,
    raw_dir: Path = DEFAULT_RAW_DIR,
    audio_manifest_dir: Path = DEFAULT_AUDIO_MANIFEST_DIR,
) -> Path:
    entity_type = safe_slug(entity_type.lower())
    entity_dir = entity_workspace_dir(entity_id, entity_type=entity_type, workspace_root=workspace_root)
    for child in ("history", "raw", "audio", "transcripts", "analysis"):
        (entity_dir / child).mkdir(parents=True, exist_ok=True)

    copy_if_exists(
        history_dir / f"deal_{entity_id}_customer_path.md",
        entity_dir / "history" / f"{entity_type}_{entity_id}_customer_path.md",
    )
    copy_if_exists(
        raw_dir / f"deal_{entity_id}_context.json",
        entity_dir / "raw" / f"{entity_type}_{entity_id}_context.json",
    )
    copy_if_exists(
        audio_manifest_dir / f"deal_{entity_id}_call_audio_manifest.json",
        entity_dir / "audio" / f"{entity_type}_{entity_id}_call_audio_manifest.json",
    )
    write_workspace_index(entity_id, entity_dir, entity_type=entity_type)
    return entity_dir


def deal_workspace_dir(deal_id: str, workspace_root: Path = DEFAULT_DEAL_WORKSPACE_ROOT) -> Path:
    return entity_workspace_dir(deal_id, entity_type="deal", workspace_root=workspace_root)


def ensure_deal_workspace(
    deal_id: str,
    workspace_root: Path = DEFAULT_DEAL_WORKSPACE_ROOT,
    history_dir: Path = DEFAULT_HISTORY_DIR,
    raw_dir: Path = DEFAULT_RAW_DIR,
    audio_manifest_dir: Path = DEFAULT_AUDIO_MANIFEST_DIR,
) -> Path:
    return ensure_entity_workspace(
        deal_id,
        entity_type="deal",
        workspace_root=workspace_root,
        history_dir=history_dir,
        raw_dir=raw_dir,
        audio_manifest_dir=audio_manifest_dir,
    )


def ensure_lead_workspace(
    lead_id: str,
    workspace_root: Path = DEFAULT_LEAD_WORKSPACE_ROOT,
    history_dir: Path = DEFAULT_LEAD_HISTORY_DIR,
    raw_dir: Path = DEFAULT_LEAD_RAW_DIR,
) -> Path:
    lead_dir = entity_workspace_dir(lead_id, entity_type="lead", workspace_root=workspace_root)
    for child in ("history", "raw", "audio", "transcripts", "analysis"):
        (lead_dir / child).mkdir(parents=True, exist_ok=True)

    copy_if_exists(
        history_dir / f"lead_{lead_id}_customer_path.md",
        lead_dir / "history" / f"lead_{lead_id}_customer_path.md",
    )
    copy_if_exists(
        raw_dir / f"lead_{lead_id}_context.json",
        lead_dir / "raw" / f"lead_{lead_id}_context.json",
    )
    write_workspace_index(lead_id, lead_dir, entity_type="lead")
    return lead_dir


def copy_if_exists(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def write_workspace_index(
    entity_id: str,
    entity_dir: Path,
    entity_type: str = "lead",
    extra: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        f"{entity_type}_id": str(entity_id),
        "workspace_dir": str(entity_dir),
        "history_dir": str(entity_dir / "history"),
        "raw_dir": str(entity_dir / "raw"),
        "audio_dir": str(entity_dir / "audio"),
        "transcripts_dir": str(entity_dir / "transcripts"),
        "analysis_dir": str(entity_dir / "analysis"),
        "updated_at": datetime.now(MSK_TZ).isoformat(),
    }
    if extra:
        payload.update(extra)

    index_path = entity_dir / "index.json"
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return index_path


def copy_audio_to_workspace(
    source_audio_path: Path,
    deal_dir: Path,
    activity_id: str | None = None,
    call_start: str | None = None,
) -> Path:
    timestamp = normalize_dt_for_filename(call_start)
    prefix = f"call_{safe_slug(activity_id)}" if activity_id else "manual_call"
    destination_name = f"{prefix}_{timestamp}_{safe_slug(source_audio_path.stem)}{source_audio_path.suffix.lower()}"
    destination = deal_dir / "audio" / destination_name
    destination.parent.mkdir(parents=True, exist_ok=True)

    counter = 2
    while destination.exists():
        destination = deal_dir / "audio" / f"{destination.stem}_{counter}{destination.suffix}"
        counter += 1

    shutil.copy2(source_audio_path, destination)
    return destination
