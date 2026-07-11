"""Static OKF pack selection for compact attention-delta shadow prompts."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any


APPROX_CHARS_PER_TOKEN = 4
ORIGINAL_OKF_FILES = (
    "index.md",
    "qualification.md",
    "technical_data.md",
    "risk_signals.md",
    "call_attempt_rules.md",
    "commercial_offer_followup.md",
    "manager_texts.md",
    "objections.md",
    "funnel.md",
)

PACK_REGISTRY: dict[str, dict[str, Any]] = {
    "core": {
        "filename": "attention_delta_core.md",
        "sources": (
            {"file": "index.md", "sections": ("Общие правила анализа",)},
            {"file": "risk_signals.md", "sections": ("Главный принцип", "Когда нужен контроль РОПа")},
            {"file": "funnel.md", "sections": ("Примеры рабочих следующих шагов",)},
        ),
    },
    "lead": {
        "filename": "attention_delta_lead.md",
        "sources": (
            {"file": "qualification.md", "sections": ("Критерии квалифицированного лида", "Категории приоритета")},
            {"file": "call_attempt_rules.md", "sections": ("Если линия занята", "Если не дозвонились")},
            {"file": "risk_signals.md", "sections": ("Недозвон или автоответчик",)},
            {"file": "funnel.md", "sections": ("Общая логика",)},
        ),
    },
    "deal": {
        "filename": "attention_delta_deal.md",
        "sources": (
            {"file": "commercial_offer_followup.md", "sections": ("Хороший результат после КП", "Что выяснить при сравнении с конкурентами")},
            {"file": "technical_data.md", "sections": ("Общий принцип", "Линия розлива")},
            {"file": "risk_signals.md", "sections": ("Высокий риск зависания", "Когда нужен контроль РОПа")},
            {"file": "funnel.md", "sections": ("После отправки КП", "После получения части технических данных")},
        ),
    },
}


def _pack_metadata(pack_id: str, path: Path, text: str) -> dict[str, Any]:
    config = PACK_REGISTRY[pack_id]
    return {
        "pack_id": pack_id,
        "file": path.name,
        "chars": len(text.strip()),
        "approx_tokens": math.ceil(len(text.strip()) / APPROX_CHARS_PER_TOKEN),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_sections": [
            {"file": source["file"], "sections": list(source["sections"])} for source in config["sources"]
        ],
    }


def select_attention_delta_knowledge(entity_type: str, knowledge_dir: Path) -> dict[str, Any]:
    """Return reproducible static packs selected only by entity type."""
    if entity_type not in {"lead", "deal"}:
        raise ValueError(f"Unsupported attention-delta entity_type: {entity_type!r}")
    pack_ids = ("core", entity_type)
    sections: list[tuple[Path, str]] = []
    packs: list[dict[str, Any]] = []
    for pack_id in pack_ids:
        path = knowledge_dir / str(PACK_REGISTRY[pack_id]["filename"])
        if not path.is_file():
            raise FileNotFoundError(f"Missing attention-delta knowledge pack: {path}")
        text = path.read_text(encoding="utf-8")
        sections.append((path, text))
        packs.append(_pack_metadata(pack_id, path, text))
    return {
        "entity_type": entity_type,
        "selected_pack_ids": list(pack_ids),
        "packs": packs,
        "excluded_original_okf_files": list(ORIGINAL_OKF_FILES),
        "sections": sections,
    }
