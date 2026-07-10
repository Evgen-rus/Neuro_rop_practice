"""Privacy-preserving composition telemetry for the unchanged legacy prompt."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


APPROX_CHARS_PER_TOKEN = 4
JSON_CONTRACT_MARKER = "Нужная JSON-структура:"
HISTORY_MARKER = "## ИСТОРИЯ"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block(text: str, *, included: bool | None = None) -> dict[str, Any]:
    chars = len(text)
    return {
        "chars": chars,
        "approx_tokens": math.ceil(chars / APPROX_CHARS_PER_TOKEN),
        "sha256": _sha256(text),
        "included": bool(chars) if included is None else included,
    }


def render_okf_sections(okf_sections: list[tuple[Path, str]]) -> str:
    """Return exactly the OKF fragment embedded by the legacy prompt builders."""
    return "\n\n".join(f"### OKF FILE: {path.name}\n\n{text.strip()}" for path, text in okf_sections)


def extract_json_contract(prompt: str) -> str:
    start = prompt.find(JSON_CONTRACT_MARKER)
    if start < 0:
        return ""
    end = prompt.find(HISTORY_MARKER, start)
    return prompt[start:end] if end >= 0 else prompt[start:]


def _remove_once(text: str, fragment: str) -> tuple[str, bool]:
    if not fragment:
        return text, True
    index = text.find(fragment)
    if index < 0:
        return text, False
    return text[:index] + text[index + len(fragment) :], True


def build_prompt_budget(
    *,
    prompt: str,
    model: str,
    history_text: str,
    transcript_text: str,
    diagnostics_text: str,
    okf_sections: list[tuple[Path, str]],
    stage_policy: dict[str, Any] | None = None,
    entity_memory_text: str = "",
    delta_events_text: str = "",
) -> dict[str, Any]:
    """Describe the existing prompt without altering, storing, or logging its text."""
    stage_policy_text = json.dumps(stage_policy, ensure_ascii=False, indent=2) if stage_policy else ""
    json_contract = extract_json_contract(prompt)
    # Legacy builders apply strip() to these dynamic values before embedding
    # them. Telemetry must measure the rendered prompt, not a trailing newline
    # that was present only in the source file.
    blocks_text = {
        "json_contract": json_contract,
        "okf_knowledge": render_okf_sections(okf_sections),
        "history": history_text.strip(),
        "transcript": transcript_text.strip(),
        "diagnostics": diagnostics_text.strip(),
        "crm_stage_policy": stage_policy_text,
        # These are measured now so later phases can populate them without
        # changing the telemetry contract. They are not in legacy prompts.
        "entity_memory": entity_memory_text,
        "delta_events": delta_events_text,
    }

    remaining = prompt
    missing_fragments: list[str] = []
    for name in ("json_contract", "okf_knowledge", "history", "transcript", "diagnostics", "crm_stage_policy"):
        remaining, removed = _remove_once(remaining, blocks_text[name])
        if not removed:
            missing_fragments.append(name)

    blocks = {"instructions": _block(remaining)}
    blocks.update({name: _block(text) for name, text in blocks_text.items()})
    accounted_chars = sum(block["chars"] for block in blocks.values())
    return {
        "version": 1,
        "mode": "legacy_prompt_observability",
        "model": model,
        "approximation": {"method": "ceil(chars / 4)", "chars_per_token": APPROX_CHARS_PER_TOKEN},
        "blocks": blocks,
        "total": {
            **_block(prompt, included=True),
            "accounted_chars": accounted_chars,
            "unaccounted_chars": len(prompt) - accounted_chars,
        },
        "composition_warnings": [f"fragment_not_found:{name}" for name in missing_fragments],
        "actual_usage": None,
        "cost": None,
    }


def attach_response_metadata(budget: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """Add usage and pricing fields only; raw model output is never copied."""
    result = dict(budget)
    usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
    input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    result["actual_usage"] = {
        "input_tokens": usage.get("input_tokens"),
        "cached_input_tokens": input_details.get("cached_tokens", usage.get("cached_input_tokens")),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens", usage.get("reasoning_tokens")),
        "total_tokens": usage.get("total_tokens"),
    }
    result["cost"] = metadata.get("estimated_cost")
    result["model"] = metadata.get("model", result.get("model"))
    return result


def write_prompt_budget(path: Path, budget: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(budget, ensure_ascii=False, indent=2), encoding="utf-8")
