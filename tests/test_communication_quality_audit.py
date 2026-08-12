from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.deal_control import _analysis_coaching
from openai_api.llm.analyze_deal import build_prompt, render_report
from openai_api.llm.validation import AnalysisValidationError, _validate_deal_management_shapes
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


if __name__ == "__main__":
    unittest.main()
