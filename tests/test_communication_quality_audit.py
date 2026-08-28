from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from api.deal_control import _analysis_coaching, _today_communications, _fetch_deal_timeline_comments
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
from setup import MSK_TZ
from openai_api.llm.deal_daily_quality import (
    DAILY_QUALITY_RULE, build_daily_quality_context, quality_event_signature, stamp_daily_quality_scope,
)


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


class DailyAuditContextTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 18, 16, tzinfo=MSK_TZ)
        self.events = [
            {"event_id": "crm_activity:1", "channel": "email", "direction": "outgoing",
             "occurred_at": "2026-08-18T10:00:00+03:00", "content": "Отправляю условия поставки к пятнице."},
            {"event_id": "timeline_comment:2", "channel": "max", "direction": "incoming",
             "occurred_at": "2026-08-18T13:00:00+03:00", "content": "Готовы согласовать объём и доставку."},
        ]

    def context(self, events=None):
        return build_daily_quality_context({"normalized_communications": events or self.events}, now=self.now)

    def test_day_context_contains_all_today_messages_but_not_old_future_or_internal(self):
        extras = [
            {**self.events[0], "event_id": "old", "occurred_at": "2026-08-17T10:00:00+03:00"},
            {**self.events[0], "event_id": "future", "occurred_at": "2026-08-18T17:00:00+03:00"},
            {**self.events[0], "event_id": "internal", "channel": "internal_comment"},
        ]
        context = self.context(self.events + extras)
        self.assertEqual([item["event_id"] for item in context["events"]], [item["event_id"] for item in self.events])
        self.assertEqual(context["business_date"], "2026-08-18")
        self.assertTrue(context["content_complete"])

    def test_full_v1_and_v2_get_same_daily_context_and_rule(self):
        context = self.context()
        with patch("openai_api.llm.analyze_deal.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            for incremental in (None, {"previous_analysis": {"communication_quality_audit": audit()}, "new_events": []}):
                prompt = build_prompt("7", "История", "Текст", "Диагностика", [], {}, incremental_context=incremental,
                                      daily_quality_context=context)
                self.assertIn(DAILY_QUALITY_RULE, prompt)
                for event in self.events:
                    self.assertIn(event["content"], prompt)
        prompt = build_materialization_prompt(
            previous_analysis={"communication_quality_audit": audit()}, semantic_state={}, evidence_delta=[],
            affected_sections=["communication_quality_audit"], stage_policy={}, prior_recommendation=None,
            compact_policy_text="", daily_quality_context=context,
        )
        self.assertIn(DAILY_QUALITY_RULE, prompt)
        for event in self.events:
            self.assertIn(event["content"], prompt)

    def test_provenance_is_stamped_from_input_not_model_or_publication_date(self):
        analysis = {"communication_quality_audit": audit()}
        analysis["communication_quality_audit"]["daily_scope"] = {"business_date": "2099-01-01"}
        stamp_daily_quality_scope(analysis, self.context())
        scope = analysis["communication_quality_audit"]["daily_scope"]
        self.assertEqual(scope["business_date"], "2026-08-18")
        self.assertEqual(scope["event_signatures"]["crm_activity:1"], quality_event_signature(self.events[0]))
        stamp_daily_quality_scope(analysis, None)
        self.assertNotIn("daily_scope", analysis["communication_quality_audit"])

    def test_missing_body_cannot_stamp_complete_ai_coverage(self):
        context = self.context([{**self.events[0], "content": ""}])
        self.assertFalse(context["content_complete"])
        analysis = {"communication_quality_audit": audit()}
        stamp_daily_quality_scope(analysis, context)
        self.assertNotIn("daily_scope", analysis["communication_quality_audit"])

    def test_sync_and_prompt_use_same_full_message_and_call_signatures(self):
        from tests.test_deal_current_situation import _touchpoint
        message = "Подтверждаем поставку оборудования к пятнице. " * 50
        call_text = "Менеджер согласовал с клиентом срок и следующий звонок завтра. " * 3
        points = [
            _touchpoint(when="2026-08-18T10:00:00+03:00", event_id="1", event_type="email", direction="2", text=message),
            _touchpoint(when="2026-08-18T11:00:00+03:00", event_id="2", event_type="call", direction="2"),
        ]
        activities = [{**point["raw"], "TYPE_ID": "4" if index == 0 else "2", "FILES": ["1"] if index else []} for index, point in enumerate(points)]
        # Sync can precede transcription; the ensuing AI result must match immediately.
        with patch("api.deal_control.find_call_transcript", return_value=None):
            communications = _today_communications(activities, self.now, deal_id="1")
        with patch("openai_api.llm.deal_current_situation._transcripts_by_activity", return_value={"2": {"text": call_text}}):
            context = build_daily_quality_context({"client_touchpoints": points}, now=self.now, deal_id="1")
        self.assertEqual(len(context["events"]), 2)
        self.assertEqual(context["events"][0]["text"], message.strip())
        self.assertEqual(
            {item["event_id"]: item["source_signature"] for item in context["events"]},
            {item["event_id"]: item["quality_source_signature"] for item in communications["items"]},
        )
        with patch("openai_api.llm.deal_current_situation._transcripts_by_activity", return_value={"2": {"text": call_text + " Уточнение."}}):
            revised = build_daily_quality_context({"client_touchpoints": points}, now=self.now, deal_id="1")
        self.assertEqual(context["events"][1]["source_signature"], revised["events"][1]["source_signature"])
        self.assertNotEqual(context["events"][1]["evidence_signature"], revised["events"][1]["evidence_signature"])

    def test_related_other_deal_and_uncompleted_activity_cannot_give_daily_points(self):
        from tests.test_deal_current_situation import _touchpoint
        pending = _touchpoint(when="2026-08-18T10:00:00+03:00", event_id="4", event_type="email", direction="2", text="Черновик", completed="N")
        bundle = {"normalized_communications": [
            {**self.events[0], "entity_type": "deal", "entity_id": "1"},
            {**self.events[1], "entity_type": "deal", "entity_id": "2"},
            {**self.events[0], "event_id": "crm_activity:4", "source_ids": ["4"]},
        ], "client_touchpoints": [pending]}
        context = build_daily_quality_context(bundle, now=self.now, deal_id="1")
        self.assertEqual([item["event_id"] for item in context["events"]], ["crm_activity:1"])

    def test_comment_fetch_failure_marks_quality_sources_unavailable(self):
        unavailable = set()
        with patch("api.deal_control._list_many", side_effect=RuntimeError("offline")):
            result = _fetch_deal_timeline_comments(object(), ["1"], self.now, unavailable_ids=unavailable)
        self.assertEqual(result, {"1": []})
        self.assertEqual(unavailable, {"1"})


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
            compact_policy_text="POLICY",
        )
        self.assertIn(COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE, prompt)
        for phrase in NEXT_ACTION_SCORE_ONE_EXAMPLES + NEXT_ACTION_SCORE_ZERO_EXAMPLES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)

    def test_cache_keys_and_other_audit_criteria_stay_unchanged(self) -> None:
        self.assertEqual(DEAL_PROMPT_CACHE_KEY, "neuro-rop:full-deal:v3")
        self.assertEqual(DEAL_INCREMENTAL_PROMPT_CACHE_KEY, "neuro-rop:incremental-deal:v1")
        with patch("openai_api.llm.analyze_deal.COMMUNICATION_QUALITY_AUDIT_ENABLED", True):
            prompt = build_prompt("7", "История", "Транскрипт", "Диагностика", [], {})
        self.assertIn(VALUE_DEVELOPMENT_RULE, prompt)
        self.assertIn(DATA_COLLECTION_RULE, prompt)


if __name__ == "__main__":
    unittest.main()
