"""Compact, privacy-safe provenance for persisted change-aware analysis runs."""

from __future__ import annotations

from typing import Any


CHANGE_DETECTION_LOGIC_VERSION = "change-aware-v1"


def analysis_run_provenance(
    payload: dict[str, Any],
    *,
    fingerprint: str,
    decision_reason: dict[str, Any],
    prompt_version: str,
    model_override: str | None = None,
) -> dict[str, Any]:
    metadata = payload.get("model_metadata") if isinstance(payload.get("model_metadata"), dict) else {}
    request_fingerprint = (
        metadata.get("request_fingerprint")
        if isinstance(metadata.get("request_fingerprint"), dict)
        else {}
    )
    prompt_fingerprint = (
        request_fingerprint.get("prompt")
        if isinstance(request_fingerprint.get("prompt"), dict)
        else None
    )
    safe_model_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in {"raw_output_text", "request_fingerprint"}
    }
    provenance = {
        "trigger": decision_reason.get("status"),
        "reasons": decision_reason.get("reasons") or [],
        "diff": decision_reason.get("diff") or {},
        "snapshot_fingerprint": fingerprint,
        "model_metadata": safe_model_metadata,
    }
    if prompt_fingerprint:
        provenance["prompt_fingerprint"] = prompt_fingerprint
    return {
        "model": str(metadata.get("model") or model_override or "").strip() or None,
        "prompt_version": prompt_version,
        "logic_version": CHANGE_DETECTION_LOGIC_VERSION,
        "provenance": provenance,
    }
