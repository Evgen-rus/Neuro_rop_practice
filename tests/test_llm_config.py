from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PAIRS = (
    ("OPENAI_ANALYSIS_MODEL", "ANALYSIS_MODEL"),
    ("OPENAI_ANALYSIS_REASONING_EFFORT", "ANALYSIS_REASONING_EFFORT"),
    ("OPENAI_REPAIR_MODEL", "ANALYSIS_REPAIR_MODEL"),
    ("OPENAI_REPAIR_REASONING_EFFORT", "ANALYSIS_REPAIR_REASONING_EFFORT"),
    ("OPENAI_MANAGER_MODEL", "DEAL_MANAGER_MODEL"),
    ("OPENAI_MANAGER_REASONING_EFFORT", "DEAL_MANAGER_REASONING_EFFORT"),
)
CONFIG_NAMES = {
    *(name for pair in PROFILE_PAIRS for name in pair),
    "LLM_PROVIDER",
    "LLM_FALLBACK_TO_OPENAI",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_ANALYSIS_MODEL",
    "OPENROUTER_ANALYSIS_REASONING_EFFORT",
    "OPENROUTER_REPAIR_MODEL",
    "OPENROUTER_REPAIR_REASONING_EFFORT",
    "OPENROUTER_MANAGER_MODEL",
    "OPENROUTER_MANAGER_REASONING_EFFORT",
    "OPENROUTER_LEARNING_SHADOW_MODEL",
    "OPENROUTER_LEARNING_SHADOW_REASONING_EFFORT",
    "OPENROUTER_PROMPT_LAB_MODELS",
    "OPENAI_LEARNING_SHADOW_MODEL",
    "OPENAI_LEARNING_SHADOW_REASONING_EFFORT",
    "OPENAI_PROMPT_LAB_MODELS",
    "ANALYSIS_MAX_OUTPUT_TOKENS",
    "ANALYSIS_REPAIR_MAX_OUTPUT_TOKENS",
    "MANAGER_SITUATION_MAX_OUTPUT_TOKENS",
    "QUICK_HELP_MAX_OUTPUT_TOKENS",
    "FOLLOWUPS_MAX_OUTPUT_TOKENS",
    "COMPANION_MAX_OUTPUT_TOKENS",
    "FULL_SCRIPT_MAX_OUTPUT_TOKENS",
    "EMAIL_MAX_OUTPUT_TOKENS",
    "STRATEGY_PACK_MAX_OUTPUT_TOKENS",
    "TASK_GUIDANCE_MAX_OUTPUT_TOKENS",
    "LEARNING_SHADOW_MAX_OUTPUT_TOKENS",
}


def run_config(expression: str, **values: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({name: "" for name in CONFIG_NAMES})
    env.update(values)
    return subprocess.run(
        [sys.executable, "-c", f"import json; import openai_api.config as c; print(json.dumps({expression}))"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class LlmConfigTests(unittest.TestCase):
    def test_defaults_preserve_current_runtime_values(self) -> None:
        names = (
            "ANALYSIS_MODEL", "ANALYSIS_REASONING_EFFORT", "ANALYSIS_REPAIR_MODEL",
            "ANALYSIS_REPAIR_REASONING_EFFORT", "OPENAI_MANAGER_MODEL",
            "OPENAI_MANAGER_REASONING_EFFORT", "OPENAI_LEARNING_SHADOW_MODEL",
            "OPENAI_LEARNING_SHADOW_REASONING_EFFORT", "ANALYSIS_MAX_OUTPUT_TOKENS",
            "ANALYSIS_REPAIR_MAX_OUTPUT_TOKENS", "MANAGER_SITUATION_MAX_OUTPUT_TOKENS",
            "QUICK_HELP_MAX_OUTPUT_TOKENS", "FOLLOWUPS_MAX_OUTPUT_TOKENS",
            "COMPANION_MAX_OUTPUT_TOKENS", "FULL_SCRIPT_MAX_OUTPUT_TOKENS",
            "EMAIL_MAX_OUTPUT_TOKENS", "STRATEGY_PACK_MAX_OUTPUT_TOKENS",
            "TASK_GUIDANCE_MAX_OUTPUT_TOKENS", "LEARNING_SHADOW_MAX_OUTPUT_TOKENS",
        )
        result = run_config("{name: getattr(c, name) for name in " + repr(names) + "}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "ANALYSIS_MODEL": "gpt-5.6-terra",
            "ANALYSIS_REASONING_EFFORT": "low",
            "ANALYSIS_REPAIR_MODEL": "gpt-5.6-luna",
            "ANALYSIS_REPAIR_REASONING_EFFORT": "xhigh",
            "OPENAI_MANAGER_MODEL": "gpt-5.6-terra",
            "OPENAI_MANAGER_REASONING_EFFORT": "low",
            "OPENAI_LEARNING_SHADOW_MODEL": "gpt-5.6-luna",
            "OPENAI_LEARNING_SHADOW_REASONING_EFFORT": "xhigh",
            "ANALYSIS_MAX_OUTPUT_TOKENS": 3500,
            "ANALYSIS_REPAIR_MAX_OUTPUT_TOKENS": 8000,
            "MANAGER_SITUATION_MAX_OUTPUT_TOKENS": 2400,
            "QUICK_HELP_MAX_OUTPUT_TOKENS": 4000,
            "FOLLOWUPS_MAX_OUTPUT_TOKENS": 3600,
            "COMPANION_MAX_OUTPUT_TOKENS": 1800,
            "FULL_SCRIPT_MAX_OUTPUT_TOKENS": 6000,
            "EMAIL_MAX_OUTPUT_TOKENS": 2600,
            "STRATEGY_PACK_MAX_OUTPUT_TOKENS": 9000,
            "TASK_GUIDANCE_MAX_OUTPUT_TOKENS": 5000,
            "LEARNING_SHADOW_MAX_OUTPUT_TOKENS": 12000,
        })

    def test_new_profile_names_and_manager_inheritance(self) -> None:
        result = run_config(
            "[c.ANALYSIS_MODEL, c.ANALYSIS_REASONING_EFFORT, c.ANALYSIS_REPAIR_MODEL, "
            "c.ANALYSIS_REPAIR_REASONING_EFFORT, c.OPENAI_MANAGER_MODEL, "
            "c.OPENAI_MANAGER_REASONING_EFFORT, c.OPENAI_LEARNING_SHADOW_MODEL, "
            "c.OPENAI_LEARNING_SHADOW_REASONING_EFFORT]",
            OPENAI_ANALYSIS_MODEL="analysis-new",
            OPENAI_ANALYSIS_REASONING_EFFORT="medium",
            OPENAI_REPAIR_MODEL="repair-new",
            OPENAI_REPAIR_REASONING_EFFORT="high",
            OPENAI_LEARNING_SHADOW_MODEL="shadow-new",
            OPENAI_LEARNING_SHADOW_REASONING_EFFORT="max",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            "analysis-new", "medium", "repair-new", "high",
            "analysis-new", "medium", "shadow-new", "max",
        ])

    def test_new_name_wins_when_legacy_name_is_still_present(self) -> None:
        result = run_config(
            "c.ANALYSIS_MODEL",
            OPENAI_ANALYSIS_MODEL="new-value",
            ANALYSIS_MODEL="ignored-legacy-value",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), "new-value")

    def test_manager_profile_uses_explicit_new_override(self) -> None:
        result = run_config(
            "[c.OPENAI_MANAGER_MODEL, c.OPENAI_MANAGER_REASONING_EFFORT]",
            OPENAI_ANALYSIS_MODEL="analysis-model",
            OPENAI_ANALYSIS_REASONING_EFFORT="low",
            OPENAI_MANAGER_MODEL="manager-model",
            OPENAI_MANAGER_REASONING_EFFORT="high",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["manager-model", "high"])

    def test_legacy_profile_names_fail_fast(self) -> None:
        for new_name, legacy_name in PROFILE_PAIRS:
            with self.subTest(legacy_name=legacy_name):
                result = run_config("None", **{legacy_name: "legacy-value"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Rename {legacy_name} to {new_name}", result.stderr)
                self.assertNotIn("legacy-value", result.stderr)

    def test_prompt_lab_whitelist_is_configurable_and_deduplicated(self) -> None:
        result = run_config(
            "c.OPENAI_PROMPT_LAB_MODELS",
            OPENAI_PROMPT_LAB_MODELS="gpt-5.6-luna,gpt-5.4,gpt-5.6-luna",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["gpt-5.6-luna", "gpt-5.4"])

    def test_openrouter_selects_active_role_profiles(self) -> None:
        result = run_config(
            "[c.LLM_PROVIDER, c.LLM_FALLBACK_TO_OPENAI, c.ACTIVE_BASE_URL, "
            "c.ANALYSIS_MODEL, c.ANALYSIS_REASONING_EFFORT, c.ANALYSIS_REPAIR_MODEL, "
            "c.ANALYSIS_REPAIR_REASONING_EFFORT, c.MANAGER_MODEL, c.MANAGER_REASONING_EFFORT, "
            "c.LEARNING_SHADOW_MODEL, c.LEARNING_SHADOW_REASONING_EFFORT, "
            "c.OPENAI_ANALYSIS_MODEL, c.OPENROUTER_PROMPT_LAB_MODELS, c.TRANSCRIPTION_MODEL]",
            LLM_PROVIDER="openrouter",
            LLM_FALLBACK_TO_OPENAI="true",
            OPENROUTER_BASE_URL="https://router.example/v1/",
            OPENROUTER_ANALYSIS_MODEL="provider/analysis",
            OPENROUTER_ANALYSIS_REASONING_EFFORT="medium",
            OPENROUTER_REPAIR_MODEL="provider/repair",
            OPENROUTER_REPAIR_REASONING_EFFORT="high",
            OPENROUTER_MANAGER_MODEL="provider/manager",
            OPENROUTER_MANAGER_REASONING_EFFORT="xhigh",
            OPENROUTER_LEARNING_SHADOW_MODEL="provider/shadow",
            OPENROUTER_LEARNING_SHADOW_REASONING_EFFORT="max",
            OPENROUTER_PROMPT_LAB_MODELS="provider/manager,provider/analysis",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            "openrouter", True, "https://router.example/v1", "provider/analysis", "medium",
            "provider/repair", "high", "provider/manager", "xhigh", "provider/shadow", "max",
            "gpt-5.6-terra", ["provider/manager", "provider/analysis"], "gpt-4o-mini-transcribe",
        ])

    def test_invalid_provider_fails_fast(self) -> None:
        result = run_config("None", LLM_PROVIDER="other")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LLM_PROVIDER must be one of: openai, openrouter", result.stderr)

    def test_openrouter_requires_all_role_models(self) -> None:
        result = run_config("None", LLM_PROVIDER="openrouter")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OPENROUTER_ANALYSIS_MODEL", result.stderr)
        self.assertIn("OPENROUTER_LEARNING_SHADOW_MODEL", result.stderr)


if __name__ == "__main__":
    unittest.main()
