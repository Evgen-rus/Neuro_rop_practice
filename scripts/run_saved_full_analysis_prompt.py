"""Run the current Full Deal validator pipeline against one frozen prompt file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a saved Full Deal prompt through the current analyzer")
    parser.add_argument("--deal-id", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--expected-prompt-sha256", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", required=True)
    parser.add_argument("--run-label", choices=("terra", "luna"), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-info", required=True)
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args()


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prompt_sha256(prompt_bytes: bytes) -> str:
    return hashlib.sha256(prompt_bytes).hexdigest()


def technical_metadata(metadata: dict[str, Any], *, validation_passed: bool) -> dict[str, Any]:
    attempts = metadata.get("semantic_attempts")
    attempts = attempts if isinstance(attempts, list) else []
    return {
        "model": metadata.get("model"),
        "reasoning_effort": metadata.get("reasoning_effort"),
        "validation_passed": validation_passed,
        "usage": metadata.get("usage"),
        "estimated_cost": metadata.get("estimated_cost"),
        "estimated_cost_usd": metadata.get("estimated_cost_usd"),
        "estimated_cost_rub": metadata.get("estimated_cost_rub"),
        "semantic_attempt_count": metadata.get("semantic_attempt_count", len(attempts)),
        "semantic_correction_retry": bool(
            metadata.get("semantic_attempt_count", len(attempts)) > 1
            or any(bool(item.get("semantic_correction_retry")) for item in attempts if isinstance(item, dict))
        ),
        "semantic_attempts": attempts,
        "response_id": metadata.get("response_id"),
        "transport_attempt_count": metadata.get("transport_attempt_count"),
        "transport_retry_count": metadata.get("transport_retry_count"),
        "transport_retry": bool(metadata.get("transport_retry")),
        "prompt_cache": metadata.get("prompt_cache"),
        "request_fingerprint": metadata.get("request_fingerprint"),
        "latency_seconds": metadata.get("latency_seconds"),
    }


def update_run_info(
    path: Path,
    *,
    deal_id: str,
    prompt_hash: str,
    run_label: str,
    run_metadata: dict[str, Any],
) -> None:
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("deal_id") != deal_id or value.get("prompt_sha256") != prompt_hash:
            raise ValueError("Existing run_info.json belongs to another deal or prompt")
    else:
        value = {"deal_id": deal_id, "prompt_sha256": prompt_hash}
    value[run_label] = run_metadata
    save_json(path, value)


def transcript_section(prompt: str, start_marker: str, end_marker: str) -> str:
    start = prompt.index(start_marker)
    end = prompt.index(end_marker, start + len(start_marker))
    return prompt[start:end]


def main() -> None:
    args = parse_args()
    prompt_path = Path(args.prompt).resolve()
    prompt_bytes = prompt_path.read_bytes()
    actual_hash = prompt_sha256(prompt_bytes)
    if actual_hash != args.expected_prompt_sha256:
        raise ValueError(f"Frozen prompt hash mismatch: expected {args.expected_prompt_sha256}, got {actual_hash}")
    if args.check_only:
        print(f"Frozen prompt verified: sha256={actual_hash}")
        return

    os.environ["ANALYSIS_REASONING_EFFORT"] = args.reasoning_effort

    from openai_api.llm.analyze_deal import (
        DEAL_PROMPT_CACHE_KEY,
        HISTORY_SECTION_MARKER,
        TRANSCRIPT_SECTION_MARKER,
        deal_prompt_cache_markers,
    )
    from openai_api.llm.llm_client import (
        ValidatedAnalysisFailure,
        call_analysis_json,
        call_validated_analysis_json,
    )
    from openai_api.llm.validation import (
        AnalysisValidationError,
        normalize_analysis_for_validation,
        validate_deal_analysis,
    )

    prompt = prompt_bytes.decode("utf-8")
    transcript_text = transcript_section(prompt, TRANSCRIPT_SECTION_MARKER, HISTORY_SECTION_MARKER)
    output_path = Path(args.output).resolve()
    run_info_path = Path(args.run_info).resolve()
    try:
        analysis, metadata = call_validated_analysis_json(
            prompt,
            validator=validate_deal_analysis,
            normalizer=normalize_analysis_for_validation,
            validation_error_types=(AnalysisValidationError,),
            model=args.model,
            analysis_caller=call_analysis_json,
            call_type="full_deal_analysis_model_compare",
            prompt_cache_key=DEAL_PROMPT_CACHE_KEY,
            prompt_cache_markers=deal_prompt_cache_markers(transcript_text),
            trace_entity_type="deal_model_compare",
            trace_entity_id=str(args.deal_id),
        )
    except ValidatedAnalysisFailure as error:
        metadata = {key: value for key, value in error.metadata.items() if key != "raw_output_text"}
        update_run_info(
            run_info_path,
            deal_id=str(args.deal_id),
            prompt_hash=actual_hash,
            run_label=args.run_label,
            run_metadata=technical_metadata(metadata, validation_passed=False),
        )
        raise

    save_json(output_path, analysis)
    update_run_info(
        run_info_path,
        deal_id=str(args.deal_id),
        prompt_hash=actual_hash,
        run_label=args.run_label,
        run_metadata=technical_metadata(metadata, validation_passed=True),
    )
    print(f"Validated analysis saved: {output_path}")


if __name__ == "__main__":
    main()
