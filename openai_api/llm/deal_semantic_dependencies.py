"""Semantic-domain to deal-analysis section map used by FULL section repair."""

from __future__ import annotations


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
