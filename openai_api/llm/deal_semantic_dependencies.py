"""Conservative, explicit semantic-domain to deal-analysis dependency map."""

from __future__ import annotations


TRANSIENT_SECTIONS = {
    "new_event", "what_changed", "deal_progress", "recommendation_feedback",
    "memory_update", "communication_quality_audit", "manager_quality",
}

# Sections that always follow genuinely new or revised client evidence, even if
# no semantic domain formally changed. Kept explicit and flat on purpose: this
# is a safety net for the current action/control core, not a generic engine.
ALWAYS_RECOMPUTE_ON_NEW_CLIENT_EVIDENCE = frozenset({
    "new_event",
    "what_changed",
    "deal_progress",
    "manager_quality",
    "communication_quality_audit",
    "call_attempt_recommendation",
    "deal_control_brief",
    "manager_action_block",
    "rop_manager_message_block",
    "rop_action",
    "recommendation_feedback",
    "memory_update",
    "priority_recommendation",
})

CLIENT_EVIDENCE_KINDS = frozenset({"call_transcript", "inbound_email", "inbound_message"})

DEPENDENCIES: dict[str, set[str]] = {
    "current_truth": {"deal_state", "deal_context", "main_risk", "deal_mode", "priority_recommendation", "deal_control_brief"},
    "critical_facts": {"deal_context", "main_risk", "priority_recommendation", "deal_control_brief"},
    "commitments": {"deal_context", "manager_action_block", "rop_manager_message_block", "rop_action"},
    "decision_path": {"deal_context", "main_risk", "deal_mode", "priority_recommendation"},
    "open_questions": {"deal_context", "manager_action_block", "shaker_question"},
    "source_conflicts": {"deal_context", "main_risk", "manager_action_block"},
    "qualification": {"qualification_assessment", "main_risk", "deal_mode", "priority_recommendation", "deal_control_brief", "manager_action_block", "rop_manager_message_block"},
    "commercial_state": {"price_comparability_check", "main_risk", "priority_recommendation", "manager_action_block", "rop_manager_message_block"},
    "payment_state": {"payment_blocker", "money_path_diagnosis", "main_risk", "priority_recommendation", "manager_action_block", "rop_manager_message_block"},
    "competitor_state": {"competitor_defense_checklist", "objection_handling", "price_comparability_check", "main_risk", "manager_action_block"},
    "money_path": {"money_path_diagnosis", "payment_blocker", "main_risk", "priority_recommendation"},
    "risk_state": {"main_risk", "priority_recommendation", "deal_mode", "resource_control", "manager_action_block", "rop_manager_message_block"},
    "turning_points": {"deal_context", "deal_progress", "main_risk"},
    "active_pain_points": {"deal_context", "objection_handling", "manager_action_block", "rop_manager_message_block"},
    "active_pressure_levers": {"deal_context", "priority_recommendation", "manager_action_block", "rop_manager_message_block", "shaker_question"},
    "communication_profile": {"client_communication_profile", "objection_handling", "manager_action_block", "rop_manager_message_block", "call_attempt_recommendation"},
    "continuity_summary": {"deal_state", "deal_mode", "deal_context", "closed_deal_review", "main_risk", "resource_control", "priority_recommendation"},
}


class UnknownSemanticDependency(ValueError):
    pass


def _is_client_evidence(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    kind = str(item.get("kind") or "")
    delta_kind = str(item.get("delta_kind") or "")
    return (
        kind in CLIENT_EVIDENCE_KINDS
        and str(item.get("evidence_id") or "").strip() != ""
        and delta_kind in {"new_evidence", "evidence_revision"}
    )


def resolve_affected_sections(
    changed_domains: list[str],
    evidence_delta: list[dict[str, Any]] | None = None,
) -> list[str]:
    unknown = sorted(set(changed_domains) - set(DEPENDENCIES))
    if unknown:
        raise UnknownSemanticDependency("unmapped semantic domains: " + ", ".join(unknown))
    affected = set(TRANSIENT_SECTIONS)
    for domain in changed_domains:
        affected.update(DEPENDENCIES[domain])
    if any(_is_client_evidence(item) for item in evidence_delta or []):
        affected.update(ALWAYS_RECOMPUTE_ON_NEW_CLIENT_EVIDENCE)
    return sorted(affected)
