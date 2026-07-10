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
    ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
    ANALYSIS_MODEL,
    CONTEXT_MEMORY_OPTIMIZATION_ENABLED,
    CONTEXT_MEMORY_OPTIMIZATION_FORCE_FULL_FALLBACK,
    CONTEXT_MEMORY_OPTIMIZATION_SHADOW_MODE,
    USD_RUB_RATE,
)
from openai_api.llm.attention_delta import (
    build_deal_attention_delta_prompt,
    build_lead_attention_delta_prompt,
    deal_attention_delta_schema,
    lead_attention_delta_schema,
    materialize_lead_attention_delta,
    validate_deal_attention_delta,
    validate_lead_attention_delta,
)
from openai_api.llm.attention_delta_report import render_attention_delta_preview
from openai_api.llm.llm_client import (
    ModelJsonParseError,
    ModelResponseIncompleteError,
    call_structured_output_json,
)
from openai_api.llm.prompt_budget import attach_response_metadata, build_prompt_budget, write_prompt_budget
from openai_api.pricing import estimate_analysis_cost


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run compact attention-delta analysis only in isolated shadow mode")
    parser.add_argument("--manifest", required=True, help="Local benchmark manifest, normally benchmarks/local/cases.json")
    parser.add_argument("--output-dir", default=None, help="Ignored shadow output directory")
    parser.add_argument("--case-id", default=None, help="Run only one neutral benchmark case ID")
    parser.add_argument("--model", default=ANALYSIS_MODEL, help="Explicit model override for the compact call")
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help="Required API safety cap: selected case count must not exceed this value.",
    )
    parser.add_argument(
        "--max-estimated-cost-rub",
        type=float,
        default=None,
        help="Required API safety cap for the combined no-cache upper cost estimate in RUB.",
    )
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
    if isinstance(paths, str):
        paths = [paths]
    elif isinstance(paths, dict):
        paths = [paths[key] for key in sorted(paths)]
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
    baseline_gaps: list[str] = []
    if entity_type == "deal" and stage_policy is None:
        baseline_gaps.append("no crm_stage_policy in the legacy baseline")
    if transcript_path is None:
        baseline_gaps.append("no real transcript input in the legacy baseline")
    if baseline_gaps:
        raise ValueError(f"Case {case.get('case_id')!r} is not ready: {'; '.join(baseline_gaps)}")
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


def prepare_shadow_case(case: dict[str, Any], *, output_root: Path, model: str) -> dict[str, Any]:
    """Build local inputs and a no-cache upper cost estimate without calling OpenAI."""
    inputs = load_shadow_inputs(case)
    prompt, schema, schema_name, validator = build_shadow_request(inputs)
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
    schema_chars = len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    approx_input_tokens = budget["total"]["approx_tokens"]
    upper_estimated_cost = estimate_analysis_cost(
        model,
        {"input_tokens": approx_input_tokens, "output_tokens": ATTENTION_DELTA_MAX_OUTPUT_TOKENS},
        USD_RUB_RATE,
    )
    transcript_is_fallback = inputs["transcript_path"] is None
    not_ready_reasons = ["legacy baseline has no real transcript input"] if transcript_is_fallback else []
    prompt_metrics = {
        "history_chars": len(inputs["history_text"].strip()),
        "transcript_chars": len(inputs["transcript_text"].strip()),
        "okf_chars": budget["blocks"]["okf_knowledge"]["chars"],
        "instructions_chars": budget["blocks"]["instructions"]["chars"],
        "schema_chars": schema_chars,
        "total_chars": budget["total"]["chars"],
        "approx_input_tokens": approx_input_tokens,
        "uses_real_transcript": not transcript_is_fallback,
    }
    return {
        "case": case,
        "inputs": inputs,
        "prompt": prompt,
        "schema": schema,
        "schema_name": schema_name,
        "validator": validator,
        "budget": budget,
        "output_dir": _case_output_dir(output_root, case.get("case_id")),
        "prompt_metrics": prompt_metrics,
        "max_output_tokens": ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
        "upper_estimated_cost": upper_estimated_cost,
        "ready_for_api": not not_ready_reasons,
        "not_ready_reasons": not_ready_reasons,
    }


def verify_api_limits(
    prepared_cases: list[dict[str, Any]],
    *,
    max_cases: int | None,
    max_estimated_cost_rub: float | None,
) -> float:
    """Fail before any API call when an explicit local budget is not respected."""
    if max_cases is None or max_cases <= 0:
        raise ValueError("--allow-api requires a positive --max-cases safety cap")
    if max_estimated_cost_rub is None or max_estimated_cost_rub <= 0:
        raise ValueError("--allow-api requires a positive --max-estimated-cost-rub safety cap")
    if len(prepared_cases) > max_cases:
        raise ValueError(f"Selected {len(prepared_cases)} case(s), exceeding --max-cases={max_cases}")
    not_ready = [
        f"{prepared['case'].get('case_id')}: {', '.join(prepared['not_ready_reasons'])}"
        for prepared in prepared_cases
        if not prepared["ready_for_api"]
    ]
    if not_ready:
        raise ValueError("Refusing API call for incomplete benchmark inputs: " + "; ".join(not_ready))
    costs = [prepared["upper_estimated_cost"].get("estimated_cost_rub") for prepared in prepared_cases]
    if any(cost is None for cost in costs):
        raise ValueError("Cannot establish API safety cap: model is absent from the local pricing table")
    total_cost_rub = round(sum(float(cost) for cost in costs), 2)
    if total_cost_rub > max_estimated_cost_rub:
        raise ValueError(
            f"Expected upper cost {total_cost_rub:.2f} RUB exceeds --max-estimated-cost-rub={max_estimated_cost_rub:.2f}"
        )
    return total_cost_rub


def print_api_confirmation(prepared_cases: list[dict[str, Any]], total_cost_rub: float) -> None:
    """Print a deterministic, non-interactive acknowledgement before requests."""
    for prepared in prepared_cases:
        cost = prepared["upper_estimated_cost"].get("estimated_cost_rub")
        print(
            "API shadow preflight: "
            f"model={prepared['upper_estimated_cost'].get('model')} "
            f"case_id={prepared['case'].get('case_id')} "
            f"approx_input_tokens={prepared['prompt_metrics']['approx_input_tokens']} "
            f"max_output_tokens={prepared['max_output_tokens']} "
            f"max_estimated_cost_rub={float(cost):.2f} "
            f"output_dir={prepared['output_dir']}"
        )
    print(f"API shadow preflight total upper cost: {total_cost_rub:.2f} RUB")


def response_metrics(metadata: dict[str, Any], *, max_output_tokens: int) -> dict[str, Any]:
    """Store response-limit telemetry separately from the attention delta."""
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    output_tokens = usage.get("output_tokens")
    reasoning_tokens = output_details.get("reasoning_tokens", usage.get("reasoning_tokens"))
    output_limit_usage_ratio = None
    if isinstance(output_tokens, (int, float)) and max_output_tokens > 0:
        output_limit_usage_ratio = round(float(output_tokens) / max_output_tokens, 4)
    return {
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "max_output_tokens": max_output_tokens,
        "output_limit_usage_ratio": output_limit_usage_ratio,
        "response_status": metadata.get("response_status"),
        "incomplete_reason": metadata.get("incomplete_reason"),
    }


def run_shadow_case(case: dict[str, Any], *, output_root: Path, allow_api: bool, model: str) -> dict[str, Any]:
    """Run one case without writing any path named by the legacy baseline."""
    try:
        prepared = prepare_shadow_case(case, output_root=output_root, model=model)
    except (FileNotFoundError, ValueError) as error:
        if allow_api:
            raise
        output_dir = _case_output_dir(output_root, case.get("case_id"))
        output_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "case_id": case.get("case_id"),
            "entity_type": case.get("entity_type"),
            "model": model,
            "allow_api": False,
            "status": "inputs_not_ready_no_api_call",
            "ready_for_api": False,
            "not_ready_reasons": [str(error)],
            "usage": None,
            "estimated_cost": None,
        }
        write_json(output_dir / "attention_delta_metadata.json", metadata)
        return metadata
    inputs = prepared["inputs"]
    prompt = prepared["prompt"]
    schema = prepared["schema"]
    schema_name = prepared["schema_name"]
    validator = prepared["validator"]
    output_dir = prepared["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    budget = prepared["budget"]
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
        "prompt_metrics": prepared["prompt_metrics"],
        "max_output_tokens": prepared["max_output_tokens"],
        "upper_estimated_cost": prepared["upper_estimated_cost"],
        "ready_for_api": prepared["ready_for_api"],
        "not_ready_reasons": prepared["not_ready_reasons"],
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
        status = "inputs_ready_no_api_call" if prepared["ready_for_api"] else "inputs_not_ready_no_api_call"
        metadata = {**common_metadata, "status": status, "usage": None, "estimated_cost": None}
        write_json(metadata_path, metadata)
        return metadata

    if not prepared["ready_for_api"]:
        raise ValueError("Refusing API call for incomplete benchmark inputs: " + "; ".join(prepared["not_ready_reasons"]))

    try:
        delta, response_metadata = call_structured_output_json(
            prompt,
            schema=schema,
            schema_name=schema_name,
            model=model,
            max_output_tokens=ATTENTION_DELTA_MAX_OUTPUT_TOKENS,
        )
    except ModelResponseIncompleteError as error:
        write_prompt_budget(budget_path, attach_response_metadata(budget, error.metadata))
        (output_dir / "attention_delta_raw_model_output.txt").write_text(error.raw_output_text, encoding="utf-8")
        metadata = {
            **common_metadata,
            "status": "output_limit_exceeded"
            if error.metadata.get("incomplete_reason") == "max_output_tokens"
            else "response_incomplete",
            "error": str(error),
            "response_metrics": response_metrics(error.metadata, max_output_tokens=ATTENTION_DELTA_MAX_OUTPUT_TOKENS),
            "model_metadata": {key: value for key, value in error.metadata.items() if key != "raw_output_text"},
        }
        write_json(metadata_path, metadata)
        return metadata
    except ModelJsonParseError as error:
        write_prompt_budget(budget_path, attach_response_metadata(budget, error.metadata))
        (output_dir / "attention_delta_raw_model_output.txt").write_text(error.raw_output_text, encoding="utf-8")
        metadata = {
            **common_metadata,
            "status": "invalid_json",
            "error": str(error),
            "response_metrics": response_metrics(error.metadata, max_output_tokens=ATTENTION_DELTA_MAX_OUTPUT_TOKENS),
            "model_metadata": error.metadata,
        }
        write_json(metadata_path, metadata)
        raise

    # Usage is persisted before the local business validation, so a rejected
    # structured response remains measurable without becoming a legacy result.
    write_prompt_budget(budget_path, attach_response_metadata(budget, response_metadata))
    if inputs["entity_type"] == "lead":
        delta = materialize_lead_attention_delta(delta)
        response_metadata["deterministic_playbook_applied"] = (
            delta.get("lead_review", {}).get("action_playbook") if isinstance(delta.get("lead_review"), dict) else None
        )
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
            "response_metrics": response_metrics(response_metadata, max_output_tokens=ATTENTION_DELTA_MAX_OUTPUT_TOKENS),
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
        "response_metrics": response_metrics(response_metadata, max_output_tokens=ATTENTION_DELTA_MAX_OUTPUT_TOKENS),
    }
    write_json(output_dir / "attention_delta.json", payload)
    (output_dir / "attention_delta_preview.md").write_text(render_attention_delta_preview(delta), encoding="utf-8")
    (output_dir / "attention_delta_raw_model_output.txt").write_text(
        response_metadata.get("raw_output_text", ""), encoding="utf-8"
    )
    metadata = {
        **common_metadata,
        "status": "completed",
        "model_metadata": payload["model_metadata"],
        "response_metrics": payload["response_metrics"],
    }
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
    if args.allow_api:
        try:
            prepared_cases = [prepare_shadow_case(case, output_root=output_root, model=args.model) for case in cases if isinstance(case, dict)]
            total_cost_rub = verify_api_limits(
                prepared_cases,
                max_cases=args.max_cases,
                max_estimated_cost_rub=args.max_estimated_cost_rub,
            )
        except ValueError as error:
            raise SystemExit(str(error)) from error
        print_api_confirmation(prepared_cases, total_cost_rub)
    completed = [
        run_shadow_case(case, output_root=output_root, allow_api=args.allow_api, model=args.model)
        for case in cases
        if isinstance(case, dict)
    ]
    print(f"Attention-delta shadow artifacts saved: {output_root} ({len(completed)} case(s))")


if __name__ == "__main__":
    main()
