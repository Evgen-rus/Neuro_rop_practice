from __future__ import annotations

from unittest.mock import patch
import unittest

from openai_api.llm import prompt_lab_models as models


class PromptLabProviderConfigTests(unittest.TestCase):
    def test_openrouter_uses_only_whitelist_with_gateway_capabilities(self) -> None:
        with (
            patch.object(models, "LLM_PROVIDER", "openrouter"),
            patch.object(models, "OPENROUTER_PROMPT_LAB_MODELS", ("provider/manager", "provider/custom")),
            patch.object(models, "MANAGER_MODEL", "provider/manager"),
            patch.object(models, "MANAGER_REASONING_EFFORT", "max"),
        ):
            items = models.list_lab_models()
            selected = models.validate_model_reasoning("provider/custom", "minimal")

        self.assertEqual([item["id"] for item in items], ["provider/manager", "provider/custom"])
        self.assertEqual(items[0]["reasoning"], ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
        self.assertEqual(selected, ("provider/custom", "minimal"))

    def test_runtime_default_must_be_in_active_whitelist(self) -> None:
        with (
            patch.object(models, "LLM_PROVIDER", "openrouter"),
            patch.object(models, "OPENROUTER_PROMPT_LAB_MODELS", ("provider/custom",)),
            patch.object(models, "MANAGER_MODEL", "provider/runtime"),
            patch.object(models, "MANAGER_REASONING_EFFORT", "low"),
        ):
            with self.assertRaisesRegex(ValueError, "Неизвестная модель"):
                models.resolved_runtime_config()


if __name__ == "__main__":
    unittest.main()
