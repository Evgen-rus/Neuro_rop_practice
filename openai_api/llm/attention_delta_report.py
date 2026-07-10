"""Deterministic preview rendering for isolated shadow artifacts."""

from __future__ import annotations

from typing import Any


def render_attention_delta_preview(delta: dict[str, Any]) -> str:
    """Render the compact result without touching the legacy report renderer."""
    action = delta.get("rop_action") if isinstance(delta.get("rop_action"), dict) else {}
    evidence = action.get("evidence_ids") if isinstance(action.get("evidence_ids"), list) else []
    reason = str(delta.get("reason") or "Не указано")
    if not delta.get("attention_required"):
        reason = f"Внимание РОПа не требуется: {reason}"
    return "\n".join(
        (
            "## Что требует внимания",
            "",
            f"- Почему: {reason}",
            f"- Что проверить РОПу: {action.get('check') or 'Не требуется'}",
            f"- Поручение менеджеру: {action.get('message_to_manager') or 'Не требуется'}",
            f"- Какой факт нужен в CRM: {action.get('expected_crm_fact') or 'Не требуется'}",
            f"- Срок: {action.get('deadline') or 'Не указан'}",
            f"- Критерий: {action.get('success_condition') or 'Не требуется'}",
            f"- Основание: {', '.join(str(item) for item in evidence) if evidence else 'Не указано'}",
            "",
        )
    )
