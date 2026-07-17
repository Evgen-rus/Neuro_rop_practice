from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_api.llm import analyze_deal
from openai_api.llm.llm_client import ModelJsonParseError
from openai_api.llm.prompt_budget import attach_response_metadata, build_prompt_budget
from openai_api.llm.validation import AnalysisValidationError


class PromptBudgetTests(unittest.TestCase):
    def test_records_hashes_without_sensitive_text(self) -> None:
        history = "Иван Иванов сообщил номер +79990000000"
        transcript = "Транскрипция: Иван Иванов подтвердил встречу"
        diagnostics = "Полнота контекста: полная"
        stage_policy = {"stage_id": "NEW", "is_closed_lost": False}
        okf = [(Path("core.md"), "Правило qualification")]
        contract = 'Нужная JSON-структура:\n{"type":"object"}\n'
        prompt = (
            "Постоянная инструкция\n"
            + contract
            + "## ИСТОРИЯ СДЕЛКИ\n"
            + history
            + "\n## ТРАНСКРИБАЦИЯ\n"
            + transcript
            + "\n## ДИАГНОСТИКА\n"
            + diagnostics
            + "\n## CRM_STAGE_POLICY\n"
            + json.dumps(stage_policy, ensure_ascii=False, indent=2)
            + "\n## OKF\n### OKF FILE: core.md\n\nПравило qualification"
        )

        budget = build_prompt_budget(
            prompt=prompt,
            model="gpt-5.4-mini",
            history_text=history,
            transcript_text=transcript,
            diagnostics_text=diagnostics,
            okf_sections=okf,
            stage_policy=stage_policy,
        )

        self.assertEqual(budget["total"]["chars"], len(prompt))
        self.assertEqual(budget["total"]["unaccounted_chars"], 0)
        self.assertEqual(budget["blocks"]["history"]["chars"], len(history))
        self.assertEqual(budget["blocks"]["entity_memory"]["chars"], 0)
        serialized = json.dumps(budget, ensure_ascii=False)
        self.assertNotIn(history, serialized)
        self.assertNotIn(transcript, serialized)
        self.assertNotIn("Иван Иванов", serialized)
        self.assertNotIn("+79990000000", serialized)

    def test_identical_blocks_have_stable_hashes_and_changed_block_isolated(self) -> None:
        def make_budget(history: str) -> dict:
            prompt = f"Инструкция\nНужная JSON-структура:\n{{}}\n## ИСТОРИЯ\n{history}\n## ТРАНСКРИБАЦИЯ\nЗвонок"
            return build_prompt_budget(
                prompt=prompt,
                model="gpt-5.4-mini",
                history_text=history,
                transcript_text="Звонок",
                diagnostics_text="",
                okf_sections=[],
            )

        first = make_budget("История A")
        same = make_budget("История A")
        changed = make_budget("История B")
        self.assertEqual(first["blocks"]["history"]["sha256"], same["blocks"]["history"]["sha256"])
        self.assertEqual(first["blocks"]["transcript"]["sha256"], changed["blocks"]["transcript"]["sha256"])
        self.assertEqual(first["blocks"]["instructions"]["sha256"], changed["blocks"]["instructions"]["sha256"])
        self.assertNotEqual(first["blocks"]["history"]["sha256"], changed["blocks"]["history"]["sha256"])
        self.assertNotEqual(first["total"]["sha256"], changed["total"]["sha256"])

    def test_records_diagnostics_compression_without_storing_raw_text(self) -> None:
        raw_diagnostics = "private recovery command " * 20
        compact_diagnostics = "## CONTEXT_COMPLETENESS\nstatus: partial"
        prompt = f"Instruction\n{compact_diagnostics}\n## HISTORY\nHistory"
        budget = build_prompt_budget(
            prompt=prompt,
            model="gpt-5.4-mini",
            history_text="History",
            transcript_text="",
            diagnostics_text=compact_diagnostics,
            diagnostics_raw_text=raw_diagnostics,
            okf_sections=[],
        )
        optimization = budget["diagnostics_optimization"]
        self.assertEqual(optimization["diagnostics_raw_chars"], len(raw_diagnostics.strip()))
        self.assertEqual(optimization["context_completeness_chars"], len(compact_diagnostics))
        self.assertGreater(optimization["diagnostics_tokens_saved"], 0)
        self.assertGreater(optimization["reduction_percent"], 0)
        self.assertNotIn(raw_diagnostics, json.dumps(budget, ensure_ascii=False))

    def test_attaches_usage_without_raw_model_output(self) -> None:
        budget = build_prompt_budget(
            prompt="Инструкция\nНужная JSON-структура:\n{}\n## ИСТОРИЯ\nИстория",
            model="gpt-5.4-mini",
            history_text="История",
            transcript_text="",
            diagnostics_text="",
            okf_sections=[],
        )
        result = attach_response_metadata(
            budget,
            {
                "model": "gpt-5.4-mini",
                "usage": {"input_tokens": 100, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 80}},
                "estimated_cost": {"estimated_cost_rub": 1.23},
                "raw_output_text": "sensitive model answer",
            },
        )
        self.assertEqual(result["actual_usage"]["cached_input_tokens"], 80)
        self.assertEqual(result["cost"]["estimated_cost_rub"], 1.23)
        self.assertNotIn("raw_output_text", json.dumps(result, ensure_ascii=False))

    def _deal_args(self, root: Path, knowledge: Path) -> SimpleNamespace:
        return SimpleNamespace(
            deal_id="DEMO",
            allow_direct_llm=True,
            dry_run=False,
            deal_root=str(root),
            transcript="none",
            knowledge_dir=str(knowledge),
            model="gpt-5.4-mini",
        )

    def _prepare_deal_workspace(self, root: Path) -> Path:
        history_dir = root / "deal_DEMO" / "history"
        history_dir.mkdir(parents=True)
        (history_dir / "deal_DEMO_customer_path.md").write_text("История без PII", encoding="utf-8")
        knowledge = root / "knowledge"
        knowledge.mkdir()
        (knowledge / "index.md").write_text("Правило", encoding="utf-8")
        return knowledge

    def _run_deal_main(self, root: Path, knowledge: Path, *, response: object, validator: object) -> Path:
        metadata = {
            "model": "gpt-5.4-mini",
            "usage": {"input_tokens": 101, "output_tokens": 11, "input_tokens_details": {"cached_tokens": 7}},
            "estimated_cost": {"estimated_cost_rub": 0.42},
        }
        call_patch = (
            patch.object(analyze_deal, "call_analysis_json", side_effect=response)
            if isinstance(response, Exception)
            else patch.object(analyze_deal, "call_analysis_json", return_value=response)
        )
        with (
            patch.object(analyze_deal, "parse_args", return_value=self._deal_args(root, knowledge)),
            patch.object(analyze_deal, "load_context_diagnostics_for_analysis", return_value=("Диагностика", None, {})),
            patch.object(analyze_deal, "log_model_file_payload"),
            patch.object(analyze_deal, "log_model_text_payload"),
            call_patch,
            patch.object(analyze_deal, "validate_deal_analysis", side_effect=validator),
            patch.object(analyze_deal, "normalize_analysis_for_validation", return_value=[]),
        ):
            with self.assertRaises(Exception):
                analyze_deal.main()
        budget_path = root / "deal_DEMO" / "analysis" / "deal_DEMO_prompt_budget.json"
        self.assertTrue(budget_path.exists())
        budget = json.loads(budget_path.read_text(encoding="utf-8"))
        self.assertEqual(budget["actual_usage"]["input_tokens"], metadata["usage"]["input_tokens"] * 2)
        self.assertEqual(budget["actual_usage"]["cached_input_tokens"], 14)
        self.assertEqual(budget["cost"]["estimated_cost_rub"], 0.84)
        return budget_path

    def test_usage_is_saved_for_model_json_parse_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge = self._prepare_deal_workspace(root)
            metadata = {
                "model": "gpt-5.4-mini",
                "usage": {"input_tokens": 101, "output_tokens": 11, "input_tokens_details": {"cached_tokens": 7}},
                "estimated_cost": {"estimated_cost_rub": 0.42},
            }
            error = ModelJsonParseError("invalid JSON", "raw response", metadata)
            self._run_deal_main(root, knowledge, response=error, validator=None)

    def test_usage_is_saved_before_business_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            knowledge = self._prepare_deal_workspace(root)
            metadata = {
                "model": "gpt-5.4-mini",
                "usage": {"input_tokens": 101, "output_tokens": 11, "input_tokens_details": {"cached_tokens": 7}},
                "estimated_cost": {"estimated_cost_rub": 0.42},
            }
            self._run_deal_main(
                root,
                knowledge,
                response=({"unexpected": "payload"}, metadata),
                validator=AnalysisValidationError("business validation failed"),
            )


if __name__ == "__main__":
    unittest.main()
