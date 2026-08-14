from __future__ import annotations

import unittest
from unittest.mock import patch

from openai_api.llm.deal_manager_strategy_pack import (
    PACK_CONTRACT,
    build_strategy_pack_prompt,
    generate_strategy_pack,
    strategy_pack_schema,
    validate_strategy_pack,
)
from tests.test_deal_manager_email_followups import EMAIL
from tests.test_deal_manager_full_script import CALL_SCRIPT, SCRIPT
from tests.test_deal_manager_quick_help import ANSWER, CONTEXT, DEAL


def _pack_for(strategy: str) -> dict:
    return {
        "pack_contract": PACK_CONTRACT,
        "selected_strategy": strategy,
        "email": {**EMAIL, "selected_strategy": strategy},
        "message_script": {**SCRIPT, "selected_strategy": strategy},
        "call_script": {**CALL_SCRIPT, "selected_strategy": strategy},
    }


class DealManagerStrategyPackTests(unittest.TestCase):
    def test_prompt_locks_one_strategy_and_does_not_see_siblings(self) -> None:
        prompt = build_strategy_pack_prompt(
            analysis_projection=CONTEXT["analysis_projection"],
            situation_projection=CONTEXT["situation_projection"],
            deal=DEAL,
            current_bitrix_task=CONTEXT["current_bitrix_task"],
            checklist={"items": []},
            communication_pattern_context={"total_attempts": 2},
            quick_help=ANSWER,
            selected_strategy="alternative",
            relevant_tactics=ANSWER["lifehacks"],
            objection_handling={"items": [{"objection_id": "technical_doubt"}]},
        )
        self.assertIn("LOCKED_MOVE", prompt)
        self.assertIn("раскрываешь уже выбранный ход", prompt)
        self.assertIn("Не выноси вопрос в clarifying_question", prompt)
        self.assertIn(ANSWER["client_messages"]["alternative"], prompt)
        self.assertNotIn(ANSWER["client_messages"]["primary"], prompt)
        self.assertNotIn(ANSWER["client_messages"]["pattern_break"], prompt)
        self.assertLess(prompt.index("ANALYSIS_CONTEXT"), prompt.index("LOCKED_MOVE:"))
        self.assertIn("email", strategy_pack_schema()["properties"])
        self.assertIn("call_script", strategy_pack_schema()["properties"])

    def test_validation_accepts_a_complete_pack(self) -> None:
        pack = _pack_for("alternative")
        self.assertEqual(
            validate_strategy_pack(pack, selected_strategy="alternative", allowed_objection_ids={"technical_doubt"}),
            pack,
        )
        with self.assertRaises(ValueError):
            validate_strategy_pack(pack, selected_strategy="primary")

    def test_generate_caches_stable_prefix_before_locked_move(self) -> None:
        pack = _pack_for("alternative")
        with patch(
            "openai_api.llm.deal_manager_strategy_pack.call_structured_output_json",
            return_value=(pack, {}),
        ) as call:
            generate_strategy_pack(
                analysis_projection=CONTEXT["analysis_projection"],
                situation_projection=CONTEXT["situation_projection"],
                deal=DEAL,
                current_bitrix_task=CONTEXT["current_bitrix_task"],
                checklist={"items": []},
                communication_pattern_context={"total_attempts": 2},
                quick_help=ANSWER,
                selected_strategy="alternative",
                relevant_tactics=ANSWER["lifehacks"],
                objection_handling={"items": [{"objection_id": "technical_doubt"}]},
            )
        self.assertEqual(call.call_args.kwargs["prompt_cache_key"], "neuro-rop:deal-manager-strategy-pack:v1")
        prefix = call.call_args.kwargs["stable_prefix"]
        self.assertIn("ANALYSIS_CONTEXT", prefix)
        self.assertNotIn("LOCKED_MOVE:", prefix)
        self.assertNotIn(ANSWER["client_messages"]["alternative"], prefix)


if __name__ == "__main__":
    unittest.main()
