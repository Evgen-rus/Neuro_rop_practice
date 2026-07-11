from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openai_api.llm.attention_delta_knowledge import ORIGINAL_OKF_FILES, select_attention_delta_knowledge
from openai_api.llm.prompt_budget import build_prompt_budget


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge" / "clients" / "praktikm"


class AttentionDeltaKnowledgeTests(unittest.TestCase):
    def test_selection_depends_only_on_entity_type(self) -> None:
        lead_first = select_attention_delta_knowledge("lead", KNOWLEDGE_DIR)
        lead_second = select_attention_delta_knowledge("lead", KNOWLEDGE_DIR)
        deal = select_attention_delta_knowledge("deal", KNOWLEDGE_DIR)
        self.assertEqual(lead_first["selected_pack_ids"], ["core", "lead"])
        self.assertEqual(deal["selected_pack_ids"], ["core", "deal"])
        self.assertEqual(lead_first["packs"], lead_second["packs"])
        self.assertNotEqual(lead_first["packs"], deal["packs"])

    def test_unknown_entity_type_is_explicit_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported attention-delta entity_type"):
            select_attention_delta_knowledge("contact", KNOWLEDGE_DIR)

    def test_safety_rules_are_covered_without_cross_entity_pack(self) -> None:
        lead = select_attention_delta_knowledge("lead", KNOWLEDGE_DIR)
        deal = select_attention_delta_knowledge("deal", KNOWLEDGE_DIR)
        lead_text = "\n".join(text for _path, text in lead["sections"])
        deal_text = "\n".join(text for _path, text in deal["sections"])
        for fragment in ("meaningful contact", "bad processing", "Qualification", "no-contact", "Move a lead to a deal"):
            self.assertIn(fragment, lead_text)
        for fragment in ("CRM stage is context", "invoice is not payment intent", "disputed closed deal", "internal control deadline", "CRM task"):
            self.assertIn(fragment, deal_text)
        self.assertNotIn("invoice is not payment intent", lead_text)
        self.assertNotIn("meaningful contact", deal_text)
        self.assertEqual(lead["excluded_original_okf_files"], list(ORIGINAL_OKF_FILES))
        self.assertEqual(len(lead["sections"]), 2)
        self.assertEqual(len(deal["sections"]), 2)

    def test_pack_metadata_is_reproducible_and_prompt_budget_has_sources(self) -> None:
        selection = select_attention_delta_knowledge("lead", KNOWLEDGE_DIR)
        prompt = "Instruction\n" + "\n".join(text for _path, text in selection["sections"])
        budget = build_prompt_budget(
            prompt=prompt,
            model="test-model",
            history_text="",
            transcript_text="",
            diagnostics_text="",
            okf_sections=selection["sections"],
            knowledge_selection={key: value for key, value in selection.items() if key != "sections"},
        )
        saved = json.loads(json.dumps(budget, ensure_ascii=False))
        self.assertEqual(saved["knowledge_selection"]["selected_pack_ids"], ["core", "lead"])
        self.assertEqual(len(saved["knowledge_selection"]["packs"]), 2)
        self.assertTrue(all(pack["sha256"] for pack in saved["knowledge_selection"]["packs"]))
        self.assertNotIn("manager_texts.md", [pack["file"] for pack in saved["knowledge_selection"]["packs"]])

    def test_missing_pack_is_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "attention_delta_core.md").write_text("core", encoding="utf-8")
            with self.assertRaisesRegex(FileNotFoundError, "Missing attention-delta knowledge pack"):
                select_attention_delta_knowledge("lead", root)


if __name__ == "__main__":
    unittest.main()
