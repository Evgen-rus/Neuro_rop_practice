from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.deal_control import _analysis_coaching
from openai_api.llm.analyze_deal import (
    COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE,
    DEAL_INCREMENTAL_PROMPT_CACHE_KEY,
    DEAL_PROMPT_CACHE_KEY,
    build_prompt,
    render_report,
)
from openai_api.llm.deal_incremental_v2 import (
    build_materialization_contract,
    build_materialization_prompt,
)
from openai_api.llm.validation import (
    AnalysisValidationError,
    _validate_deal_management_shapes,
    normalize_analysis_for_validation,
)
from storage.rop_db import save_ui_report


def audit(*, next_action: int = 0) -> dict:
    reasons = []
    if next_action == 0:
        reasons.append({
            "criterion": "next_action",
            "explanation": "Менеджер оставил инициативу клиенту.",
            "quote": "Как решите, дайте знать.",
        })
    return {
        "status": "assessed",
        "scope_summary": "Учтены звонок с расшифровкой и письмо клиента.",
        "criteria": {
            "next_action": {"score": next_action},
            "value_development": {"score": 1},
            "data_collection": {"score": 1},
        },
        "zero_reasons": reasons,
        "summary_for_rop": "Клиент сравнивает предложения. Менеджеру нужно назначить контрольный звонок.",
        "insufficient_reason": None,
    }


class CommunicationQualityAuditTests(unittest.TestCase):
    def test_prompt_includes_single_current_audit_contract_when_enabled(self) -> None:
        with patch("openai_api.llm.analyze_deal.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            prompt = build_prompt("7", "История", "Транскрипт", "Диагностика", [], {})
        self.assertIn("communication_quality_audit", prompt)
        self.assertIn("один текущий аудит", prompt)
        self.assertIn("Недозвон", prompt)
        self.assertIn("JSON number 0 или 1", prompt)
        self.assertNotIn('"score": "0|1|null"', prompt)

    def test_prompt_omits_audit_contract_when_disabled(self) -> None:
        with patch("openai_api.llm.analyze_deal.COMMUNICATION_QUALITY_AUDIT_ENABLED", False):
            prompt = build_prompt("7", "История", "Транскрипт", "Диагностика", [], {})
        self.assertNotIn("communication_quality_audit", prompt)

    def test_validator_requires_reason_exactly_for_zero_scores(self) -> None:
        errors: list[str] = []
        with patch("openai_api.llm.validation.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            _validate_deal_management_shapes({
                "communication_quality_audit": audit(),
                "client_communication_profile": {},
            }, errors)
        self.assertFalse(any("communication_quality_audit" in error for error in errors), errors)

        invalid = audit()
        invalid["zero_reasons"] = []
        errors = []
        with patch("openai_api.llm.validation.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            _validate_deal_management_shapes({
                "communication_quality_audit": invalid,
                "client_communication_profile": {},
            }, errors)
        self.assertTrue(any("cover exactly" in error for error in errors))

    def test_insufficient_evidence_has_null_scores_and_no_false_failures(self) -> None:
        value = {
            "status": "insufficient_evidence",
            "scope_summary": "Есть только попытка дозвона без разговора.",
            "criteria": {
                "next_action": {"score": None},
                "value_development": {"score": None},
                "data_collection": {"score": None},
            },
            "zero_reasons": [],
            "summary_for_rop": None,
            "insufficient_reason": "Нет содержательной коммуникации с клиентом.",
        }
        errors: list[str] = []
        with patch("openai_api.llm.validation.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            _validate_deal_management_shapes({
                "communication_quality_audit": value,
                "client_communication_profile": {},
            }, errors)
        self.assertFalse(any("communication_quality_audit" in error for error in errors), errors)

    def test_safe_normalization_converts_numeric_strings_and_orders_unique_reasons(self) -> None:
        value = audit()
        value["criteria"]["next_action"]["score"] = "0"
        value["criteria"]["value_development"]["score"] = "1"
        value["criteria"]["data_collection"]["score"] = "0"
        value["zero_reasons"] = [
            {
                "criterion": "data_collection",
                "explanation": "Не уточнил данные.",
                "quote": "Данные пришлёте потом.",
            },
            {
                "criterion": "next_action",
                "explanation": "Оставил инициативу клиенту.",
                "quote": "Как решите, дайте знать.",
            },
            {
                "criterion": "data_collection",
                "explanation": "Повтор той же причины.",
                "quote": "Данные пришлёте потом.",
            },
        ]
        analysis = {"communication_quality_audit": value}

        changes = normalize_analysis_for_validation(analysis)

        self.assertEqual(value["criteria"]["next_action"]["score"], 0)
        self.assertEqual(value["criteria"]["value_development"]["score"], 1)
        self.assertEqual(value["criteria"]["data_collection"]["score"], 0)
        self.assertEqual(
            [item["criterion"] for item in value["zero_reasons"]],
            ["next_action", "data_collection"],
        )
        self.assertTrue(any(change.get("action") == "deduplicated_and_ordered" for change in changes))
        errors: list[str] = []
        with patch("openai_api.llm.validation.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            _validate_deal_management_shapes(
                {"communication_quality_audit": value, "client_communication_profile": {}},
                errors,
            )
        self.assertFalse(any("communication_quality_audit" in error for error in errors), errors)

    def test_safe_normalization_does_not_infer_missing_assessed_score_or_reason(self) -> None:
        value = audit()
        value["criteria"]["next_action"]["score"] = None
        value["zero_reasons"] = []

        normalize_analysis_for_validation({"communication_quality_audit": value})

        self.assertIsNone(value["criteria"]["next_action"]["score"])
        self.assertEqual(value["zero_reasons"], [])
        errors: list[str] = []
        with patch("openai_api.llm.validation.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            _validate_deal_management_shapes(
                {"communication_quality_audit": value, "client_communication_profile": {}},
                errors,
            )
        self.assertTrue(any("score must be 0 or 1" in error for error in errors))

    def test_insufficient_evidence_normalization_resets_only_dependent_fields(self) -> None:
        value = audit()
        value["status"] = "insufficient_evidence"
        value["insufficient_reason"] = "Нет содержательной коммуникации с клиентом."

        normalize_analysis_for_validation({"communication_quality_audit": value})

        self.assertTrue(all(item["score"] is None for item in value["criteria"].values()))
        self.assertEqual(value["zero_reasons"], [])
        self.assertIsNone(value["summary_for_rop"])
        self.assertEqual(value["insufficient_reason"], "Нет содержательной коммуникации с клиентом.")
        errors: list[str] = []
        with patch("openai_api.llm.validation.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            _validate_deal_management_shapes(
                {"communication_quality_audit": value, "client_communication_profile": {}},
                errors,
            )
        self.assertFalse(any("communication_quality_audit" in error for error in errors), errors)

    def test_audit_is_projected_to_rop_screen_and_markdown(self) -> None:
        value = audit()
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_ui_report(
                db_path,
                entity_type="deal",
                entity_id="101",
                report_json={"communication_quality_audit": value},
            )
            with patch("api.deal_control.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
                coaching = _analysis_coaching(db_path, "101")
        self.assertEqual(coaching["communication_quality_audit"], value)

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite"
            save_ui_report(db_path, entity_type="deal", entity_id="101", report_json={"communication_quality_audit": value})
            with patch("api.deal_control.COMMUNICATION_QUALITY_AUDIT_ENABLED", False):
                coaching = _analysis_coaching(db_path, "101")
        self.assertIsNone(coaching["communication_quality_audit"])

        report = render_report({
            "deal_id": "101",
            "communication_quality_audit": value,
        })
        self.assertIn("## Контроль качества ведения сделки", report)
        self.assertIn("Критерий 1 — Next Action: 0", report)
        self.assertIn("Как решите, дайте знать.", report)


NEXT_ACTION_SCORE_ONE_EXAMPLES = (
    "Завтра наберу",
    "Во вторник наберу",
    "На следующей неделе во вторник созвонимся",
    "Через пару дней дам ответ",
    "В течение двух рабочих дней подготовим",
    "К концу недели вернусь с ответом",
    "В первой половине следующей недели дадим результат",
    "Завтра постараюсь уточнить и вам отзвонюсь",
    "Завтра днём информация уже должна быть",
)

NEXT_ACTION_SCORE_ZERO_EXAMPLES = (
    "На следующей неделе созвонимся",
    "В ближайшее время",
    "На днях",
    "Попозже",
    "Скоро",
    "Будет время, посмотрю",
    "Чуть-чуть подождите",
    "Я сейчас передам информацию на склад",
)

VALUE_DEVELOPMENT_RULE = (
    "value_development=1 только если касание имело конкретный информационный повод, "
    "добавляло ценность или уточняло производство, сроки либо бюджет; пустое «не надумали?» — 0."
)
DATA_COLLECTION_RULE = (
    "data_collection=1 только если менеджер, опираясь на историю, собрал или актуализировал "
    "необходимые технические, логистические, реквизитные данные либо сведения о ЛПР; "
    "игнорирование существенного пробела — 0."
)


class NextActionRubricTests(unittest.TestCase):
    def test_shared_rule_accepts_concrete_time_anchors_without_exact_clock(self) -> None:
        rule = COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE
        self.assertIn("Точное время НЕ обязательно", rule)
        self.assertIn("не проверяй, является ли действие шагом к договору", rule)
        self.assertNotIn("точной датой и временем", rule)
        for phrase in NEXT_ACTION_SCORE_ONE_EXAMPLES:
            with self.subTest(score=1, phrase=phrase):
                self.assertIn(phrase, rule)

    def test_shared_rule_rejects_vague_or_missing_time_anchors(self) -> None:
        rule = COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE
        self.assertIn("без дня или более узкого периода", rule)
        self.assertIn("без срока результата", rule)
        for phrase in NEXT_ACTION_SCORE_ZERO_EXAMPLES:
            with self.subTest(score=0, phrase=phrase):
                self.assertIn(phrase, rule)

    def test_full_prompt_uses_shared_next_action_rule(self) -> None:
        with patch("openai_api.llm.analyze_deal.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            prompt = build_prompt("7", "История", "Транскрипт", "Диагностика", [], {})
        self.assertIn(COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE, prompt)
        self.assertIn(VALUE_DEVELOPMENT_RULE, prompt)
        self.assertIn(DATA_COLLECTION_RULE, prompt)
        self.assertNotIn("точной датой и временем", prompt)

    def test_incremental_v1_prompt_uses_shared_next_action_rule(self) -> None:
        with patch("openai_api.llm.analyze_deal.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            prompt = build_prompt(
                "7",
                "История не должна попасть",
                "",
                "Диагностика",
                [],
                {},
                incremental_context={
                    "previous_analysis": {"deal_id": "7"},
                    "new_events": [],
                    "crm_delta": {},
                },
            )
        self.assertIn(COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE, prompt)
        self.assertIn(VALUE_DEVELOPMENT_RULE, prompt)
        self.assertIn(DATA_COLLECTION_RULE, prompt)
        self.assertNotIn("точной датой и временем", prompt)

    def test_incremental_v2_materialization_uses_the_same_next_action_rule(self) -> None:
        _, _, constraints = build_materialization_contract(
            {"communication_quality_audit": {}},
            ["communication_quality_audit"],
        )
        self.assertEqual(
            constraints["communication_quality_audit"]["next_action"],
            COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE,
        )
        prompt = build_materialization_prompt(
            previous_analysis={"communication_quality_audit": {}},
            semantic_state={"schema_version": "deal-semantic-state-v1"},
            evidence_delta=[{"evidence_id": "call:9"}],
            affected_sections=["communication_quality_audit"],
            stage_policy={},
            prior_recommendation=None,
            daily_checklist=None,
            compact_policy_text="POLICY",
        )
        self.assertIn(COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE, prompt)
        for phrase in NEXT_ACTION_SCORE_ONE_EXAMPLES + NEXT_ACTION_SCORE_ZERO_EXAMPLES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_cache_keys_and_other_audit_criteria_stay_unchanged(self) -> None:
        self.assertEqual(DEAL_PROMPT_CACHE_KEY, "neuro-rop:full-deal:v2")
        self.assertEqual(DEAL_INCREMENTAL_PROMPT_CACHE_KEY, "neuro-rop:incremental-deal:v1")
        with patch("openai_api.llm.analyze_deal.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            prompt = build_prompt("7", "История", "Транскрипт", "Диагностика", [], {})
        self.assertIn(VALUE_DEVELOPMENT_RULE, prompt)
        self.assertIn(DATA_COLLECTION_RULE, prompt)


if __name__ == "__main__":
    unittest.main()
