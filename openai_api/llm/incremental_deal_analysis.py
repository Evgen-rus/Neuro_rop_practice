from __future__ import annotations

from pathlib import Path
from typing import Any

from openai_api.llm.analyze_deal import build_prompt
from openai_api.llm.full_analysis_repair import build_full_repair_builder
from openai_api.llm.llm_client import call_analysis_json, call_validated_analysis_json
from openai_api.llm.validation import AnalysisValidationError, normalize_analysis_for_validation, validate_deal_analysis


INCREMENTAL_DEAL_PROMPT_VERSION = "neuro-rop:incremental-deal:v1"


def build_incremental_deal_prompt(
    *,
    deal_id: str,
    previous_analysis: dict[str, Any],
    crm_semantic_delta: list[dict[str, Any]],
    evidence_delta: list[dict[str, Any]],
    current_required_crm_facts: dict[str, Any],
    context_diagnostics_text: str,
    okf_sections: list[tuple[Path, str]],
    stage_policy: dict[str, Any],
) -> str:
    return build_prompt(
        str(deal_id),
        "",
        "",
        context_diagnostics_text,
        okf_sections,
        stage_policy,
        incremental_context={
            "PREVIOUS_TRUSTED_COMPLETE_ANALYSIS": previous_analysis,
            "CRM_SEMANTIC_DELTA": crm_semantic_delta,
            "NEW_OR_REVISED_CLIENT_EVIDENCE": evidence_delta,
            "CURRENT_REQUIRED_CRM_FACTS": current_required_crm_facts,
        },
    )


def run_incremental_deal_analysis(
    prompt: str,
    *,
    deal_id: str,
    model: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return call_validated_analysis_json(
        prompt,
        validator=validate_deal_analysis,
        normalizer=normalize_analysis_for_validation,
        validation_error_types=(AnalysisValidationError,),
        model=model,
        targeted_repair_builder=build_full_repair_builder("deal", prompt),
        analysis_caller=call_analysis_json,
        call_type="incremental_deal_analysis",
        prompt_cache_key=INCREMENTAL_DEAL_PROMPT_VERSION,
        trace_entity_type="deal",
        trace_entity_id=str(deal_id),
    )
