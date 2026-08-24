"""Run one canonical FULL deal analysis without publishing or overwriting runtime output.

The runner reads the prepared local deal workspace and production knowledge, but
writes only an ignored benchmark artifact under ``analysis/full_shadow``. It does
not update SQLite, entity state, UI reports, recommendations, or checklists.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai_api.config import ANALYSIS_MODEL
from openai_api.llm.analyze_deal import (
    DEAL_PROMPT_CACHE_KEY,
    DEFAULT_KNOWLEDGE_DIR,
    build_deal_stage_policy,
    build_prompt,
    deal_prompt_cache_markers,
    knowledge_files,
    load_context_diagnostics_for_analysis,
    read_text,
    resolve_history_path,
    transcript_text_for_prompt,
)
from openai_api.llm.llm_client import call_analysis_json, call_validated_analysis_json
from openai_api.llm.prompt_budget import attach_response_metadata, build_prompt_budget
from openai_api.llm.validation import (
    AnalysisValidationError,
    normalize_analysis_for_validation,
    validate_deal_analysis,
)
from storage.rop_db import (
    DEFAULT_DB_PATH,
    get_deal_daily_checklist_analysis_projection,
    get_latest_neuro_rop_recommendation_projection,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deal-id", required=True)
    parser.add_argument("--deal-root", default="reports/rop_assistant/deals")
    parser.add_argument("--knowledge-dir", default=str(DEFAULT_KNOWLEDGE_DIR))
    parser.add_argument("--model", default=ANALYSIS_MODEL)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    deal_id = str(args.deal_id)
    deal_dir = Path(args.deal_root) / f"deal_{deal_id}"
    history_path = resolve_history_path(deal_dir, deal_id)
    transcript_path = deal_dir / "transcripts" / f"deal_{deal_id}_all_calls_transcript.md"
    knowledge_dir = Path(args.knowledge_dir)
    if not history_path.exists():
        raise FileNotFoundError(f"history not found: {history_path}")
    if not transcript_path.exists():
        raise FileNotFoundError(f"aggregate transcript not found: {transcript_path}")
    if not knowledge_dir.exists():
        raise FileNotFoundError(f"knowledge directory not found: {knowledge_dir}")

    diagnostics_text, _diagnostics_payload, diagnostics_paths = load_context_diagnostics_for_analysis(
        entity_type="deal",
        entity_id=deal_id,
        workspace_root=Path(args.deal_root),
    )
    history_text = read_text(history_path)
    transcript_text = transcript_text_for_prompt(
        transcript_path,
        read_text(transcript_path),
        deal_id=deal_id,
    )
    okf_sections = [(path, read_text(path)) for path in knowledge_files(knowledge_dir, entity_type="deal")]
    stage_policy = build_deal_stage_policy(deal_dir, deal_id)
    prior_recommendation = get_latest_neuro_rop_recommendation_projection(DEFAULT_DB_PATH, deal_id)
    daily_checklist = get_deal_daily_checklist_analysis_projection(DEFAULT_DB_PATH, deal_id)
    prompt = build_prompt(
        deal_id,
        history_text,
        transcript_text,
        diagnostics_text,
        okf_sections,
        stage_policy,
        prior_recommendation,
        daily_checklist,
        None,
    )
    prompt_budget = build_prompt_budget(
        prompt=prompt,
        model=args.model,
        history_text=history_text,
        transcript_text=transcript_text,
        diagnostics_text=diagnostics_text,
        okf_sections=okf_sections,
        stage_policy=stage_policy,
    )
    analysis, metadata = call_validated_analysis_json(
        prompt,
        validator=validate_deal_analysis,
        normalizer=normalize_analysis_for_validation,
        validation_error_types=(AnalysisValidationError,),
        model=args.model,
        analysis_caller=call_analysis_json,
        call_type="full_deal_analysis_shadow",
        prompt_cache_key=DEAL_PROMPT_CACHE_KEY,
        prompt_cache_markers=deal_prompt_cache_markers(transcript_text),
        trace_entity_type="deal",
        trace_entity_id=deal_id,
        preview_prompt=False,
        preview_response_errors=False,
    )

    output_dir = deal_dir / "analysis" / "full_shadow"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    artifact = output_dir / f"benchmark_full_{stamp}.json"
    safe_metadata = {key: value for key, value in metadata.items() if key != "raw_output_text"}
    artifact.write_text(
        json.dumps(
            {
                "mode": "full_shadow_benchmark",
                "deal_id": deal_id,
                "input_files": {
                    "history": str(history_path),
                    "transcript": str(transcript_path),
                    "context_diagnostics": diagnostics_paths,
                    "knowledge": [str(path) for path, _text in okf_sections],
                },
                "model_metadata": safe_metadata,
                "prompt_budget": attach_response_metadata(prompt_budget, metadata),
                "analysis": analysis,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    usage = safe_metadata.get("usage") or {}
    print(json.dumps({
        "status": "passed",
        "prompt_chars": len(prompt),
        "usage": usage,
        "estimated_cost_rub": safe_metadata.get("estimated_cost_rub"),
        "latency_seconds": safe_metadata.get("latency_seconds"),
        "api_attempts": safe_metadata.get("semantic_attempt_count"),
        "artifact": str(artifact),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
