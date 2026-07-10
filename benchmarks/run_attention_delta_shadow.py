"""Run isolated compact attention-delta shadow benchmarks.

By default this command only verifies local inputs and writes prompt telemetry.
OpenAI can be called only when the operator explicitly passes ``--allow-api``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openai_api.config import (
    ANALYSIS_MODEL,
    CONTEXT_MEMORY_OPTIMIZATION_ENABLED,
    CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK,
    CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE,
)
from openai_api.llm.attention_delta import (
    build_deal_attention_delta_prompt,
    build_lead_attention_delta_prompt,
    deal_attention_delta_schema,
    lead_attention_delta_schema,
    validate_deal_attention_delta,
    validate_lead_attention_delta,
)
from openai_api.llm.attention_delta_report import render_attention_delta_preview
from openai_api.llm.llm_client import ModelJsonParseError, call_structured_output_json
from openai_api.llm.prompt_budget import attach_response_metadata, build_prompt_budget, write_prompt_budget


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run compact attention-delta analysis only in isolated shadow mode")
    parser.add_argument("--manifest", required=True, help="Local benchmark manifest, normally benchmarks/local/cases.json")
    parser.add_argument("--output-dir", default=None, help="Ignored shadow output directory")
    parser.add_argument("--case-id", default=None, help="Run only one neutral benchmark case ID")
    parser.add_argument("--model", default=ANALYSIS_MODEL, help="Explicit model override for the compact call")
    parser.add_argument(
        "--allow-api",
        action="store_true",
        help="Required acknowledgement before this benchmark can call OpenAI.",
    )
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_required_text(path_value: Any, label: str) -> tuple[Path, str]:
    path = Path(str(path_value or ""))
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Missing {label}: {path}")
    return path, path.read_text(encoding="utf-8")


def _read_diagnostics(paths: Any) -> tuple[list[str], str]:
    if paths is None:
        return [], "Diagnostics were not saved with the legacy baseline."
    if not isinstance(paths, list):
        raise ValueError("Legacy input_files.context_diagnostics must be a list")
    used_paths: list[str] = []
    fragments: list[str] = []
    for value in paths:
        path, text = _read_required_text(value, "context diagnostics")
        used_paths.append(str(path))
        fragments.append(f"### {path.name}\n{text.strip()}")
    return used_paths, "\n\n".join(fragments) or "Diagnostics were not saved with the legacy baseline."


def load_shadow_inputs(case: dict[str, Any]) -> dict[str, Any]:
    """Load exactly the input artifacts recorded in an existing legacy baseline."""
    baseline = case.get("baseline") if isinstance(case.get("baseline"), dict) else {}
    analysis_path = Path(str(baseline.get("analysis_json") or ""))
    payload = read_json(analysis_path)
    inputs = payload.get("input_files") if isinstance(payload.get("input_files"), dict) else {}
    entity_type = case.get("entity_type")
    if entity_type not in {"deal", "lead"}:
        raise ValueError(f"Unsupported entity_type in case {case.get('case_id')!r}")
    entity_id = str(payload.get(f"{entity_type}_id") or case.get("entity_id") or "")
    if not entity_id:
        raise ValueError(f"Case {case.get('case_id')!r} needs entity_id or a legacy {entity_type}_id")
    history_path, history_text = _read_required_text(inputs.get("history"), "history input")
    transcript_value = inputs.get("transcript")
    if transcript_value:
        transcript_path, transcript_text = _read_required_text(transcript_value, "transcript input")
    else:
        transcript_path = None
        transcript_text = "Транскрибация не предоставлена. Анализируй только доступную CRM-историю и ограничения контекста."
    diagnostics_paths, diagnostics_text = _read_diagnostics(inputs.get("context_diagnostics"))
    knowledge_paths = inputs.get("knowledge")
    if not isinstance(knowledge_paths, list) or not knowledge_paths:
        raise ValueError(f"Case {case.get('case_id')!r} has no legacy knowledge input paths")
    okf_sections = [_read_required_text(value, "OKF knowledge input") for value in knowledge_paths]
    stage_policy = payload.get("crm_stage_policy") if isinstance(payload.get("crm_stage_policy"), dict) else None
    if entity_type == "deal" and stage_policy is None:
        raise ValueError(f"Case {case.get('case_id')!r} has no crm_stage_policy in the legacy baseline")
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "history_path": str(history_path),
        "history_text": history_text,
        "transcript_path": str(transcript_path) if transcript_path else None,
        "transcript_text": transcript_text,
        "diagnostics_paths": diagnostics_paths,
        "diagnostics_text": diagnostics_text,
        "okf_sections": okf_sections,
        "knowledge_paths": [str(path) for path, _text in okf_sections],
        "stage_policy": stage_policy,
        "legacy_analysis_path": str(analysis_path),
    }


def build_shadow_request(inputs: dict[str, Any]) -> tuple[str, dict[str, Any], str, Any]:
    entity_type = inputs["entity_type"]
    if entity_type == "deal":
        prompt = build_deal_attention_delta_prompt(
            inputs["entity_id"],
            inputs["history_text"],
            inputs["transcript_text"],
            inputs["diagnostics_text"],
            inputs["okf_sections"],
            inputs["stage_policy"],
        )
        return prompt, deal_attention_delta_schema(), "deal_attention_delta", validate_deal_attention_delta
    prompt = build_lead_attention_delta_prompt(
        inputs["entity_id"],
        inputs["history_text"],
        inputs["transcript_text"],
        inputs["diagnostics_text"],
        inputs["okf_sections"],
    )
    return prompt, lead_attention_delta_schema(), "lead_attention_delta", validate_lead_attention_delta


def _case_output_dir(output_root: Path, case_id: Any) -> Path:
    value = str(case_id or "")
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError("case_id must be a neutral single path component")
    return output_root / value


def run_shadow_case(case: dict[str, Any], *, output_root: Path, allow_api: bool, model: str) -> dict[str, Any]:
    """Run one case without writing any path named by the legacy baseline."""
    inputs = load_shadow_inputs(case)
    prompt, schema, schema_name, validator = build_shadow_request(inputs)
    output_dir = _case_output_dir(output_root, case.get("case_id"))
    output_dir.mkdir(parents=True, exist_ok=True)
    budget = build_prompt_budget(
        prompt=prompt,
        model=model,
        history_text=inputs["history_text"],
        transcript_text=inputs["transcript_text"],
        diagnostics_text=inputs["diagnostics_text"],
        okf_sections=inputs["okf_sections"],
        stage_policy=inputs["stage_policy"],
    )
    budget["mode"] = "attention_delta_shadow"
    budget_path = output_dir / "attention_delta_prompt_budget.json"
    write_prompt_budget(budget_path, budget)
    metadata_path = output_dir / "attention_delta_metadata.json"
    common_metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "case_id": case.get("case_id"),
        "entity_type": inputs["entity_type"],
        "entity_id": inputs["entity_id"],
        "model": model,
        "allow_api": allow_api,
        "feature_flags": {
            "context_memory_optimization_enabled": CONTEXT_MEMORY_OPTIMIZATION_ENABLED,
            "context_memory_optimization_shadow_mode": CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE,
            "context_memory_optimization_force_full_fallback": CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK,
        },
        "legacy_analysis_path": inputs["legacy_analysis_path"],
        "input_files": {
            "history": inputs["history_path"],
            "transcript": inputs["transcript_path"],
            "context_diagnostics": inputs["diagnostics_paths"],
            "knowledge": inputs["knowledge_paths"],
        },
    }
    if not allow_api:
        metadata = {**common_metadata, "status": "inputs_ready_no_api_call", "usage": None, "estimated_cost": None}
        write_json(metadata_path, metadata)
        return metadata

    try:
        delta, response_metadata = call_structured_output_json(
            prompt,
            schema=schema,
            schema_name=schema_name,
            model=model,
        )
    except ModelJsonParseError as error:
        write_prompt_budget(budget_path, attach_response_metadata(budget, error.metadata))
        (output_dir / "attention_delta_raw_model_output.txt").write_text(error.raw_output_text, encoding="utf-8")
        metadata = {**common_metadata, "status": "invalid_json", "error": str(error), "model_metadata": error.metadata}
        write_json(metadata_path, metadata)
        raise

    # Usage is persisted before the local business validation, so a rejected
    # structured response remains measurable without becoming a legacy result.
    write_prompt_budget(budget_path, attach_response_metadata(budget, response_metadata))
    try:
        validator(delta)
    except ValueError as error:
        (output_dir / "attention_delta_raw_model_output.txt").write_text(
            response_metadata.get("raw_output_text", ""), encoding="utf-8"
        )
        metadata = {
            **common_metadata,
            "status": "validation_failed",
            "error": str(error),
            "model_metadata": {key: value for key, value in response_metadata.items() if key != "raw_output_text"},
        }
        write_json(metadata_path, metadata)
        write_json(output_dir / "attention_delta_error.json", {"error": str(error), "attention_delta": delta})
        raise

    payload = {
        "generated_at": common_metadata["generated_at"],
        "entity_type": inputs["entity_type"],
        "entity_id": inputs["entity_id"],
        "attention_delta": delta,
        "model_metadata": {key: value for key, value in response_metadata.items() if key != "raw_output_text"},
    }
    write_json(output_dir / "attention_delta.json", payload)
    (output_dir / "attention_delta_preview.md").write_text(render_attention_delta_preview(delta), encoding="utf-8")
    (output_dir / "attention_delta_raw_model_output.txt").write_text(
        response_metadata.get("raw_output_text", ""), encoding="utf-8"
    )
    metadata = {**common_metadata, "status": "completed", "model_metadata": payload["model_metadata"]}
    write_json(metadata_path, metadata)
    return metadata


def main() -> None:
    args = parse_args()
    manifest_path = Path(args.manifest)
    manifest = read_json(manifest_path)
    cases = manifest.get("cases") if isinstance(manifest.get("cases"), list) else []
    if args.case_id:
        cases = [case for case in cases if isinstance(case, dict) and case.get("case_id") == args.case_id]
    if not cases:
        raise SystemExit("Manifest has no matching cases.")
    output_root = Path(args.output_dir) if args.output_dir else PROJECT_ROOT / "benchmarks" / "results" / "attention_delta"
    completed = [
        run_shadow_case(case, output_root=output_root, allow_api=args.allow_api, model=args.model)
        for case in cases
        if isinstance(case, dict)
    ]
    print(f"Attention-delta shadow artifacts saved: {output_root} ({len(completed)} case(s))")


if __name__ == "__main__":
    main()
