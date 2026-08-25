"""Compact Prompt Lab module specs. Production builders stay source of truth."""

from __future__ import annotations

from typing import Any, Callable

from openai_api.config import (
    COMPANION_MAX_OUTPUT_TOKENS,
    FOLLOWUPS_MAX_OUTPUT_TOKENS,
    QUICK_HELP_MAX_OUTPUT_TOKENS,
)
from openai_api.llm.deal_manager_companion import COMPANION_CONTRACT, companion_static_prompt
from openai_api.llm.deal_manager_email import EMAIL_CONTRACT, MAX_EMAIL_OUTPUT_TOKENS, email_static_prompt
from openai_api.llm.deal_manager_followups import FOLLOWUPS_CONTRACT, followups_static_prompt
from openai_api.llm.deal_manager_full_script import (
    CALL_SCRIPT_CONTRACT,
    MAX_FULL_SCRIPT_OUTPUT_TOKENS,
    SCRIPT_CONTRACT,
    full_script_static_prompt,
)
from openai_api.llm.deal_manager_quick_help import MATERIAL_PROMPT_REVISION, quick_help_static_prompt


MODULE_KEYS = (
    "quick_help.push",
    "quick_help.reanimator",
    "full_script.message",
    "full_script.call",
    "full_script.email",
    "followups",
    "companion",
)


def _quick_help_spec(mode: str) -> dict[str, Any]:
    return {
        "key": f"quick_help.{mode}",
        "label": "Дожим" if mode == "push" else "Реаниматор",
        "family": "quick_help",
        "mode": mode,
        "requires_confirmed_situation": True,
        "requires_upstream_quick_help": False,
        "schema_version": "strategy_v3",
        "material_revision": MATERIAL_PROMPT_REVISION,
        "max_output_tokens": QUICK_HELP_MAX_OUTPUT_TOKENS,
        "call_type": "prompt_lab_quick_help",
        "static_prompt": lambda: quick_help_static_prompt(mode),
        "context_marker": "MANAGER_TACTICS:",
    }


def _script_spec(script_mode: str, label: str, contract: str) -> dict[str, Any]:
    return {
        "key": f"full_script.{script_mode}",
        "label": label,
        "family": "full_script",
        "script_mode": script_mode,
        "requires_confirmed_situation": True,
        "requires_upstream_quick_help": True,
        "schema_version": contract,
        "material_revision": MATERIAL_PROMPT_REVISION,
        "max_output_tokens": MAX_EMAIL_OUTPUT_TOKENS if script_mode == "email" else MAX_FULL_SCRIPT_OUTPUT_TOKENS,
        "call_type": "prompt_lab_full_script",
        "static_prompt": email_static_prompt if script_mode == "email" else lambda: full_script_static_prompt(script_mode),
        "context_marker": "ANALYSIS_CONTEXT:",
    }


SPECS: dict[str, dict[str, Any]] = {
    "quick_help.push": _quick_help_spec("push"),
    "quick_help.reanimator": _quick_help_spec("reanimator"),
    "full_script.message": _script_spec("message", "Message", SCRIPT_CONTRACT),
    "full_script.call": _script_spec("call", "Call", CALL_SCRIPT_CONTRACT),
    "full_script.email": _script_spec("email", "Email", EMAIL_CONTRACT),
    "followups": {
        "key": "followups",
        "label": "Followups",
        "family": "followups",
        "requires_confirmed_situation": True,
        "requires_upstream_quick_help": False,
        "schema_version": FOLLOWUPS_CONTRACT,
        "material_revision": None,
        "max_output_tokens": FOLLOWUPS_MAX_OUTPUT_TOKENS,
        "call_type": "prompt_lab_followups",
        "static_prompt": followups_static_prompt,
        "context_marker": "ANALYSIS_CONTEXT:",
    },
    "companion": {
        "key": "companion",
        "label": "Companion",
        "family": "companion",
        "requires_confirmed_situation": False,
        "requires_upstream_quick_help": False,
        "schema_version": COMPANION_CONTRACT,
        "material_revision": None,
        "max_output_tokens": COMPANION_MAX_OUTPUT_TOKENS,
        "call_type": "prompt_lab_companion",
        "static_prompt": companion_static_prompt,
        "context_marker": "ANALYSIS_CONTEXT:",
    },
}


def get_module(module_key: str) -> dict[str, Any]:
    spec = SPECS.get(str(module_key))
    if spec is None:
        raise ValueError("Неизвестный модуль Prompt Lab")
    return spec


def public_modules() -> list[dict[str, Any]]:
    items = []
    for key in MODULE_KEYS:
        spec = SPECS[key]
        items.append({
            "key": spec["key"],
            "label": spec["label"],
            "family": spec["family"],
            "requires_confirmed_situation": spec["requires_confirmed_situation"],
            "requires_upstream_quick_help": spec["requires_upstream_quick_help"],
            "schema_version": spec["schema_version"],
        })
    return items


def production_prompt_template(module_key: str, **kwargs: Any) -> str:
    spec = get_module(module_key)
    builder: Callable[..., str] = spec["static_prompt"]
    if spec["key"] == "companion":
        return builder(manager_note=kwargs.get("manager_note") or "")
    return builder()
