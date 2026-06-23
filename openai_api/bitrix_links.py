"""
Helpers for safe public Bitrix entity links.
"""

from __future__ import annotations

from openai_api.config import BITRIX_PORTAL_URL


def bitrix_entity_url(entity_type: str, entity_id: str | int | None) -> str:
    if not entity_id or not BITRIX_PORTAL_URL:
        return ""
    entity_type = entity_type.strip().lower()
    if entity_type not in {"lead", "deal"}:
        return ""
    return f"{BITRIX_PORTAL_URL}/crm/{entity_type}/details/{entity_id}/"

