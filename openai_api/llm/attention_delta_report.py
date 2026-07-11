"""Deterministic preview rendering for isolated shadow artifacts."""

from __future__ import annotations

from typing import Any

from openai_api.llm.deal_attention_playbooks import playbook_preview_lines as deal_playbook_preview_lines
from openai_api.llm.lead_attention_playbooks import playbook_preview_lines as lead_playbook_preview_lines

def render_attention_delta_preview(delta: dict[str, Any]) -> str:
    """Render the compact result without touching the legacy report renderer."""
    action = delta.get("rop_action") if isinstance(delta.get("rop_action"), dict) else {}
    evidence = action.get("evidence_ids") if isinstance(action.get("evidence_ids"), list) else []
    reason = str(delta.get("reason") or "Не указано")
    if not delta.get("attention_required"):
        reason = f"Внимание РОПа не требуется: {reason}"
    lines = [
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
    ]
    lead_review = delta.get("lead_review") if isinstance(delta.get("lead_review"), dict) else None
    if lead_review:
        playbook_lines = lead_playbook_preview_lines(lead_review.get("action_playbook"))
        if playbook_lines:
            lines.extend(["## Применённый регламент обработки", ""])
            lines.extend(f"- {line}" for line in playbook_lines)
            lines.append("")
    deal_review = delta.get("deal_review") if isinstance(delta.get("deal_review"), dict) else None
    if deal_review:
        playbook_lines = deal_playbook_preview_lines(deal_review.get("action_playbook"))
        if playbook_lines:
            lines.extend(["## Applied deal playbook", ""])
            lines.extend(f"- {line}" for line in playbook_lines)
            lines.append("")
    return "\n".join(lines)
