"""Regression/integration tests for the Incremental Deal Analysis V2 corrective pass.

Covers: trusted checkpoint lineage across MINI/SKIP, conservative recompute of
the action/control core on new client evidence, honest bootstrap evidence
coverage from recorded provenance, purge of V2 tables without FK errors,
compact policy context in V2 prompts, and MINI preservation after duplicate
transcript representations.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openai_api.llm.deal_evidence import (
    collect_deal_evidence,
    coverage_from_evidence,
    evidence_delta,
    evidence_ids_included_from_context,
    transcript_evidence_ids_for_input,
)
from openai_api.llm.deal_semantic_dependencies import (
    ALWAYS_RECOMPUTE_ON_NEW_CLIENT_EVIDENCE,
    resolve_affected_sections,
)
from openai_api.llm.analyze_deal import COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE
from openai_api.llm.deal_incremental_v2 import (
    IncrementalV2Result,
    _estimated_cost_summary,
    _materialization_normalizer,
    _materialization_repair_prompt_builder,
    _materialization_validator,
    _semantic_envelope_validator,
    _usage_summary,
    build_materialization_contract,
    build_v2_compact_diagnostics,
    build_materialization_prompt,
    build_semantic_update_prompt,
    build_v2_compact_policy,
    render_v2_compact_diagnostics,
    render_v2_compact_policy,
)
from openai_api.llm.validation import (
    AnalysisValidationError,
    DEAL_RECOMMENDED_CHANNELS,
    RECOMMENDATION_FEEDBACK_STATUSES,
)
from openai_api.llm.deal_semantic_state import SCHEMA_VERSION, bootstrap_semantic_state
from storage.rop_db import (
    connect,
    deal_analysis_purge_counts,
    get_analysis_run_evidence_ids,
    get_latest_deal_semantic_checkpoint,
    init_db,
    purge_local_deal_analysis_state,
    save_analysis_run,
    save_deal_incremental_v2_run,
    save_deal_semantic_checkpoint,
)


def _bootstrap_state(coverage: dict | None = None) -> dict:
    return bootstrap_semantic_state(
        {"deal_context": {}, "main_risk": {"risk_level": "low"}},
        deal_id="7",
        source_analysis_run_id=1,
        source_fingerprint="fp",
        evidence_coverage=coverage or {},
    )


def _transcript(directory: Path, activity_id: str, text: str, name: str) -> None:
    (directory / name).write_text(
        json.dumps(
            {
                "metadata": {"activity_id": activity_id, "call_start": "2026-08-19T10:00:00+03:00"},
                "text": text,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class TrustedCheckpointLineageTests(unittest.TestCase):
    """P1: MINI/SKIP between a checkpoint and new evidence keep the baseline trusted."""

    def _checkpoint(self, db_path: Path) -> tuple[int, int]:
        source_run_id = save_analysis_run(
            db_path,
            entity_type="deal",
            entity_id="7",
            status="full_llm_analysis",
        )
        state = _bootstrap_state({"call:1": {"content_hash": "a", "revision": 1}})
        checkpoint_id = save_deal_semantic_checkpoint(
            db_path,
            entity_id="7",
            schema_version=SCHEMA_VERSION,
            source_analysis_run_id=source_run_id,
            source_fingerprint="fp1",
            semantic_state=state,
            mode="on",
            baseline_snapshot={"entity_type": "deal", "deal": {"id": "7"}},
        )
        return checkpoint_id, source_run_id

    def test_checkpoint_survives_mini_and_skip_fingerprint_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            init_db(db_path)
            _checkpoint_id, source_run_id = self._checkpoint(db_path)

            # MINI/SKIP persist an analysis run and move entity_state forward
            # without any LLM analysis; the source analysis stays the latest one.
            save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="7",
                status="SKIPPED_NO_CHANGES",
                fingerprint="fp2",
            )
            from storage.rop_db import upsert_entity_state

            upsert_entity_state(
                db_path,
                entity_type="deal",
                entity_id="7",
                fingerprint="fp2",
                snapshot={"entity_type": "deal"},
                last_analysis_status="full_llm_analysis",
                last_analysis={
                    "analysis": {"main_risk": {"risk_level": "low"}},
                    "analysis_run_id": source_run_id,
                },
            )
            from storage.rop_db import get_entity_state

            previous_state = get_entity_state(db_path, "deal", "7")

            from openai_api.llm.analyze_deal_if_changed import _trusted_v2_checkpoint

            checkpoint = _trusted_v2_checkpoint(db_path, "7", previous_state)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["source_analysis_run_id"], source_run_id)
        self.assertEqual(checkpoint["baseline_snapshot"]["deal"]["id"], "7")

    def test_new_source_analysis_invalidates_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            init_db(db_path)
            self._checkpoint(db_path)
            newer_run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="7",
                status="full_llm_analysis",
            )
            from storage.rop_db import upsert_entity_state, get_entity_state

            upsert_entity_state(
                db_path,
                entity_type="deal",
                entity_id="7",
                fingerprint="fp3",
                snapshot={},
                last_analysis_status="full_llm_analysis",
                last_analysis={
                    "analysis": {"main_risk": {}},
                    "analysis_run_id": newer_run_id,
                },
            )
            previous_state = get_entity_state(db_path, "deal", "7")

            from openai_api.llm.analyze_deal_if_changed import _trusted_v2_checkpoint

            self.assertIsNone(_trusted_v2_checkpoint(db_path, "7", previous_state))

    def test_legacy_checkpoint_without_baseline_is_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            init_db(db_path)
            source_run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="7",
                status="full_llm_analysis",
            )
            state = _bootstrap_state()
            save_deal_semantic_checkpoint(
                db_path,
                entity_id="7",
                schema_version=SCHEMA_VERSION,
                source_analysis_run_id=source_run_id,
                source_fingerprint="fp1",
                semantic_state=state,
                mode="shadow",
            )
            from storage.rop_db import upsert_entity_state, get_entity_state

            upsert_entity_state(
                db_path,
                entity_type="deal",
                entity_id="7",
                fingerprint="fp1",
                snapshot={},
                last_analysis_status="full_llm_analysis",
                last_analysis={"analysis": {}, "analysis_run_id": source_run_id},
            )
            previous_state = get_entity_state(db_path, "deal", "7")

            from openai_api.llm.analyze_deal_if_changed import _trusted_v2_checkpoint

            self.assertIsNone(_trusted_v2_checkpoint(db_path, "7", previous_state))


class DependencyCoreTests(unittest.TestCase):
    """P2: conservative always-recompute core for genuinely new client evidence."""

    def test_new_call_evidence_recomputes_call_attempt_core(self) -> None:
        affected = resolve_affected_sections(
            [],
            [{"evidence_id": "call:9", "kind": "call_transcript", "delta_kind": "new_evidence"}],
        )
        for section in (
            "call_attempt_recommendation",
            "manager_quality",
            "communication_quality_audit",
            "deal_control_brief",
            "manager_action_block",
            "rop_manager_message_block",
            "rop_action",
            "recommendation_feedback",
            "daily_checklist_update",
            "memory_update",
            "priority_recommendation",
            "new_event",
            "what_changed",
            "deal_progress",
        ):
            self.assertIn(section, affected)

    def test_non_client_domain_does_not_pull_expensive_unrelated_sections(self) -> None:
        affected = resolve_affected_sections(["competitor_state"], [])
        self.assertIn("competitor_defense_checklist", affected)
        self.assertNotIn("payment_blocker", affected)
        self.assertNotIn("qualification_assessment", affected)
        self.assertFalse(ALWAYS_RECOMPUTE_ON_NEW_CLIENT_EVIDENCE & set(affected) - set(ALWAYS_RECOMPUTE_ON_NEW_CLIENT_EVIDENCE))

    def test_crm_only_delta_kind_does_not_trigger_always_core(self) -> None:
        affected = resolve_affected_sections(
            [],
            [{"evidence_id": "task:3", "kind": "crm_task", "delta_kind": "new_evidence"}],
        )
        self.assertNotIn("call_attempt_recommendation", affected)


class HonestCoverageTests(unittest.TestCase):
    """P4: bootstrap coverage uses only provably included evidence."""

    def test_bootstrap_coverage_excludes_unseen_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.sqlite"
            init_db(db_path)
            source_run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="18633",
                status="full_llm_analysis",
                evidence_ids_included=["call:648405"],
            )
            transcripts = root / "transcripts"
            transcripts.mkdir()
            raw_bundle = {"deal_id": "18633"}
            _transcript(transcripts, "648405", "Первый звонок текст", "first.json")
            _transcript(transcripts, "648406", "Второй звонок текст", "second.json")

            from openai_api.llm.analyze_deal_if_changed import _save_v2_checkpoint_from_analysis

            payload = {
                "analysis_run_id": source_run_id,
                "analysis": {"deal_context": {}, "main_risk": {"risk_level": "low"}},
            }
            _save_v2_checkpoint_from_analysis(
                db_path=db_path,
                deal_id="18633",
                payload=payload,
                fingerprint="fp",
                raw_bundle=raw_bundle,
                transcripts_dir=transcripts,
                mode="shadow",
                snapshot={"entity_type": "deal"},
            )
            checkpoint = get_latest_deal_semantic_checkpoint(db_path, "18633")
            coverage = checkpoint["semantic_state"]["evidence_coverage"]
            current = collect_deal_evidence(raw_bundle, transcripts)
            delta, next_coverage = evidence_delta(current, coverage)
        self.assertEqual(list(coverage), ["call:648405"])
        self.assertEqual([item["evidence_id"] for item in delta], ["call:648406"])
        self.assertIn("call:648406", next_coverage)

    def test_missing_provenance_creates_no_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "test.sqlite"
            init_db(db_path)
            source_run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="18633",
                status="full_llm_analysis",
            )
            transcripts = root / "transcripts"
            transcripts.mkdir()
            _transcript(transcripts, "648405", "Текст", "first.json")

            from openai_api.llm.analyze_deal_if_changed import _save_v2_checkpoint_from_analysis

            result = _save_v2_checkpoint_from_analysis(
                db_path=db_path,
                deal_id="18633",
                payload={"analysis_run_id": source_run_id, "analysis": {"deal_context": {}}},
                fingerprint="fp",
                raw_bundle={"deal_id": "18633"},
                transcripts_dir=transcripts,
                mode="shadow",
            )
            checkpoint = get_latest_deal_semantic_checkpoint(db_path, "18633")
        self.assertEqual(result, 0)
        self.assertIsNone(checkpoint)

    def test_analysis_run_persists_evidence_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            init_db(db_path)
            run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="7",
                status="FULL_LLM_ANALYSIS",
                evidence_ids_included=["call:648405", "email:123"],
            )
            legacy_run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="8",
                status="FULL_LLM_ANALYSIS",
            )
            included = get_analysis_run_evidence_ids(db_path, run_id)
            legacy = get_analysis_run_evidence_ids(db_path, legacy_run_id)
        self.assertEqual(included, ["call:648405", "email:123"])
        self.assertIsNone(legacy)


class PurgeV2TablesTests(unittest.TestCase):
    """P5: deal purge removes V2 rows before analysis_runs without FK errors."""

    def test_purge_removes_v2_rows_without_foreign_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "test.sqlite"
            init_db(db_path)
            run_id = save_analysis_run(
                db_path,
                entity_type="deal",
                entity_id="100",
                status="full_llm_analysis",
            )
            state = _bootstrap_state()
            checkpoint_id = save_deal_semantic_checkpoint(
                db_path,
                entity_id="100",
                schema_version=SCHEMA_VERSION,
                source_analysis_run_id=run_id,
                source_fingerprint="fp",
                semantic_state=state,
                mode="shadow",
                baseline_snapshot={"entity_type": "deal"},
            )
            v2_run_id = save_deal_incremental_v2_run(
                db_path,
                entity_id="100",
                mode="shadow",
                source_analysis_run_id=run_id,
            )
            preview = deal_analysis_purge_counts(db_path)
            self.assertEqual(preview["deal_semantic_checkpoints"], 1)
            self.assertEqual(preview["deal_incremental_v2_runs"], 1)

            deleted = purge_local_deal_analysis_state(db_path)

            self.assertEqual(deleted["deal_semantic_checkpoints"], 1)
            self.assertEqual(deleted["deal_incremental_v2_runs"], 1)
            with connect(db_path) as conn:
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM deal_semantic_checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
                )
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM deal_incremental_v2_runs WHERE id = ?", (v2_run_id,)).fetchone()
                )
                self.assertIsNone(
                    conn.execute("SELECT 1 FROM analysis_runs WHERE id = ?", (run_id,)).fetchone()
                )


class CompactPolicyTests(unittest.TestCase):
    """P3: both V2 prompts receive the compact policy subset with budget blocks."""

    @staticmethod
    def _write_knowledge_files(knowledge_dir: Path) -> None:
        knowledge_dir.mkdir(parents=True)
        contents = {
            "technical_data.md": "## Общий принцип\nЕсли для подбора оборудования не хватает технических данных, запросить их.\n",
            "risk_signals.md": "## Главный принцип\nКаждый контакт должен либо продвинуть клиента, либо уточнить риск.\n",
            "call_attempt_rules.md": "## Первые 3 дня\nСогласовать следующую попытку связи.\n",
            "commercial_offer_followup.md": "## Хороший результат после КП\nЗафиксировать следующий шаг.\n",
            "objections.md": "## Возражения\nПроверить причину сомнений клиента.\n",
            "funnel.md": "## Общая логика\nСледующий шаг должен быть конкретным.\n",
        }
        for filename, content in contents.items():
            (knowledge_dir / filename).write_text(content, encoding="utf-8")

    def test_policy_builder_uses_existing_knowledge_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            knowledge_dir = Path(temp_dir) / "knowledge"
            self._write_knowledge_files(knowledge_dir)
            with patch("openai_api.llm.analyze_deal.DEFAULT_KNOWLEDGE_DIR", knowledge_dir):
                policy = build_v2_compact_policy(deal_dir=Path(temp_dir) / "reports" / "any", deal_id="7")

            expected = {
                "technical_data.md", "risk_signals.md", "call_attempt_rules.md",
                "commercial_offer_followup.md", "objections.md", "funnel.md",
            }
            self.assertEqual(set(policy["knowledge_files"]), expected)
            rendered = render_v2_compact_policy(policy)
            self.assertIn("Если для подбора оборудования не хватает технических данных", rendered)
            self.assertIn("Каждый контакт должен либо продвинуть клиента", rendered)
            self.assertNotIn("CRM_STAGE_POLICY", rendered)

    def test_both_prompts_include_compact_policy_text(self) -> None:
        state = _bootstrap_state()
        semantic_prompt = build_semantic_update_prompt(
            previous_state=state,
            evidence_delta=[{"evidence_id": "call:9", "text": "x"}],
            crm_delta={},
            compact_policy_text="POLICY_MARKER",
        )
        materialization_prompt = build_materialization_prompt(
            previous_analysis={},
            semantic_state=state,
            evidence_delta=[{"evidence_id": "call:9", "text": "x"}],
            affected_sections=["new_event"],
            stage_policy={},
            prior_recommendation=None,
            daily_checklist=None,
            compact_policy_text="POLICY_MARKER",
        )
        self.assertIn("## V2_COMPACT_POLICY\nPOLICY_MARKER", semantic_prompt)
        self.assertIn("## V2_COMPACT_POLICY\nPOLICY_MARKER", materialization_prompt)

    def test_policy_projection_tracks_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            knowledge_dir = Path(temp_dir) / "knowledge"
            self._write_knowledge_files(knowledge_dir)
            marker = "Тестовый маркер риска для автономной фикстуры"
            risk_signals = knowledge_dir / "risk_signals.md"
            risk_signals.write_text(
                f"## Главный принцип\n{marker}\n",
                encoding="utf-8",
            )
            with patch("openai_api.llm.analyze_deal.DEFAULT_KNOWLEDGE_DIR", knowledge_dir):
                policy = build_v2_compact_policy(deal_dir=Path(temp_dir) / "reports" / "any", deal_id="7")

            self.assertIn(marker, risk_signals.read_text(encoding="utf-8"))
            self.assertIn(marker, policy["sources"]["risk_signals.md"])

    def test_diagnostics_and_stage_policy_have_separate_prompt_blocks(self) -> None:
        diagnostics = render_v2_compact_diagnostics(build_v2_compact_diagnostics({
            "context_completeness": "partial",
            "gaps": [{"type": "missing_call"}],
        }))
        prompt = build_semantic_update_prompt(
            previous_state=_bootstrap_state(),
            evidence_delta=[{"evidence_id": "call:9"}],
            crm_delta={"change_types": []},
            compact_policy_text="POLICY",
            compact_diagnostics_text=diagnostics,
            stage_policy={"stage": "current"},
        )
        self.assertIn("## CONTEXT_DIAGNOSTICS", prompt)
        self.assertIn("missing_call", prompt)
        self.assertIn("## CRM_STAGE_POLICY", prompt)
        self.assertEqual(prompt.count('"stage": "current"'), 1)


class CumulativeDeltaTests(unittest.TestCase):
    def test_v2_crm_delta_uses_analysis_baseline_not_latest_poll(self) -> None:
        from openai_api.change_detection.snapshot import compare_snapshots
        from openai_api.llm.analyze_deal_if_changed import _v2_crm_delta

        baseline = {
            "deal": {"id": "7", "stage_id": "S1", "opportunity": "100"},
            "activities": [{"id": "a", "kind": "task"}],
        }
        latest_poll = {
            "deal": {"id": "7", "stage_id": "S2", "opportunity": "200"},
            "activities": [{"id": "a", "kind": "task"}, {"id": "b", "kind": "task"}],
        }
        current = {
            "deal": {"id": "7", "stage_id": "S3", "opportunity": "200"},
            "activities": [
                {"id": "a", "kind": "task"}, {"id": "b", "kind": "task"},
                {"id": "c", "kind": "call"},
            ],
        }
        polling_diff = compare_snapshots(latest_poll, current)
        cumulative = compare_snapshots(baseline, current)
        delta = _v2_crm_delta(current, cumulative)
        self.assertNotIn("amount_changed", polling_diff["changes"])
        self.assertIn("amount_changed", delta["change_types"])
        self.assertIn("stage_changed", delta["change_types"])
        self.assertEqual(delta["details"]["new_activity_ids"], ["b", "c"])


class ExactPromptProvenanceTests(unittest.TestCase):
    def _bundle(self, directory: Path, activity_id: str, text: str) -> tuple[Path, Path]:
        md = directory / f"call_{activity_id}.md"
        md.write_text(text, encoding="utf-8")
        js = directory / f"call_{activity_id}.json"
        js.write_text(json.dumps({
            "metadata": {"activity_id": activity_id, "call_start": "2026-08-19T10:00:00+03:00"},
            "transcript_md_path": str(md),
            "text": text,
        }, ensure_ascii=False), encoding="utf-8")
        return js, md

    def test_full_individual_contains_only_selected_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _js1, md1 = self._bundle(root, "1", "Первый звонок")
            self._bundle(root, "2", "Второй звонок")
            ids = transcript_evidence_ids_for_input(root, deal_id="7", transcript_path=md1)
        self.assertEqual(ids, ["call:1"])

    def test_full_aggregate_contains_all_calls_and_ignores_unseen_individual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._bundle(root, "1", "Первый звонок")
            self._bundle(root, "2", "Второй звонок")
            aggregate = root / "deal_7_all_calls_transcript.md"
            aggregate.write_text("aggregate", encoding="utf-8")
            aggregate_ids = transcript_evidence_ids_for_input(root, deal_id="7", transcript_path=aggregate)
            individual_ids = evidence_ids_included_from_context(
                {"deal_id": "7"}, root, transcript_path=root / "call_1.md"
            )
        self.assertEqual(aggregate_ids, ["call:1", "call:2"])
        self.assertEqual(individual_ids, ["call:1"])

    def test_v1_context_records_exact_new_call(self) -> None:
        from openai_api.llm.deal_incremental import build_incremental_context
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _js, md = self._bundle(root, "9", "Новый звонок")
            previous_state = {"last_analysis": {"analysis": {"deal_id": "7"}}}
            previous_snapshot = {"transcript": {"path": str(root / "old.md")}}
            current_snapshot = {"deal": {"id": "7"}, "transcript": {"path": str(md)}}
            with patch("openai_api.llm.deal_incremental.normalize_analysis_for_validation"), patch(
                "openai_api.llm.deal_incremental.validate_deal_analysis"
            ):
                context = build_incremental_context(
                    previous_state=previous_state,
                    previous_snapshot=previous_snapshot,
                    current_snapshot=current_snapshot,
                    diff={"changes": ["transcript_changed"], "details": {}},
                    raw_bundle={"deal_id": "7"},
                    transcript_path=md,
                )
        self.assertEqual(context["evidence_ids_included"], ["call:9"])


class SemanticDriftTests(unittest.TestCase):
    def test_undeclared_semantic_rewrite_is_rejected(self) -> None:
        previous = _bootstrap_state()
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["risk_state"] = {"risk_level": "high"}
        validator = _semantic_envelope_validator(
            previous,
            [{"evidence_id": "call:9"}],
            {"change_types": []},
        )
        with self.assertRaisesRegex(AnalysisValidationError, "undeclared semantic domain"):
            validator({"changed_domains": [], "change_reasons": {}, "semantic_state": current})

    def test_materialization_contract_is_compact_and_has_validator_enum(self) -> None:
        previous = {
            "recommendation_feedback": {
                "status": "unconfirmed", "evidence": ["x" * 5000], "next_action_text": "y" * 5000,
            },
            "manager_action_block": {"primary_text": "z" * 10000, "manager_checklist": ["q" * 1000]},
            "rop_manager_message_block": {"deadline": "2026-08-25"},
        }
        structural, continuity, constraints = build_materialization_contract(
            previous, ["recommendation_feedback", "manager_action_block", "rop_manager_message_block"]
        )
        self.assertLess(len(json.dumps(structural, ensure_ascii=False)), 3000)
        self.assertEqual(continuity, {})
        self.assertEqual(
            constraints["recommendation_feedback.status"],
            sorted(RECOMMENDATION_FEEDBACK_STATUSES),
        )
        self.assertEqual(
            constraints["manager_action_block.recommended_channel"],
            sorted(DEAL_RECOMMENDED_CHANNELS),
        )
        self.assertEqual(
            constraints["rop_manager_message_block.deadline"],
            "required calendar date in exact YYYY-MM-DD format",
        )
        prompt = build_materialization_prompt(
            previous_analysis=previous,
            semantic_state=_bootstrap_state(),
            evidence_delta=[{"evidence_id": "call:9"}],
            affected_sections=["recommendation_feedback", "manager_action_block"],
            stage_policy={}, prior_recommendation=None, daily_checklist=None,
            compact_policy_text="POLICY",
        )
        for status in RECOMMENDATION_FEEDBACK_STATUSES:
            self.assertIn(status, prompt)
        self.assertNotIn("x" * 100, prompt)

    def test_communication_quality_audit_reuses_full_next_action_rule(self) -> None:
        _, _, constraints = build_materialization_contract(
            {"communication_quality_audit": {}},
            ["communication_quality_audit"],
        )
        self.assertEqual(
            constraints["communication_quality_audit"]["next_action"],
            COMMUNICATION_QUALITY_AUDIT_NEXT_ACTION_RULE,
        )
        self.assertIn("Точное время НЕ обязательно", constraints["communication_quality_audit"]["next_action"])
        self.assertNotIn("точной датой и временем", constraints["communication_quality_audit"]["next_action"])

    def test_materialization_rejects_invalid_recommendation_feedback_enum(self) -> None:
        previous = {
            "recommendation_feedback": {
                "status": "unconfirmed",
                "reason_codes": [],
                "missing_evidence": [],
            },
        }
        validator = _materialization_validator({"recommendation_feedback"}, previous)
        with self.assertRaisesRegex(
            AnalysisValidationError,
            "invalid enum at recommendation_feedback.status",
        ):
            validator(
                {
                    "sections": {
                        "recommendation_feedback": {
                            "status": "invented_status",
                            "reason_codes": [],
                            "missing_evidence": [],
                        },
                    },
                }
            )

    def test_materialization_preserves_only_missing_nested_object_keys(self) -> None:
        previous = {
            "deal_context": {
                "decision_path": {
                    "process": "Закупка",
                    "current_step": "Согласование",
                    "influencers": ["Технический директор"],
                    "evidence": ["call:1"],
                },
            },
        }
        value = {
            "sections": {
                "deal_context": {
                    "decision_path": {
                        "process": "Новый процесс",
                        "current_step": "Новый шаг",
                        "evidence": ["call:2"],
                    },
                },
            },
        }
        changes = _materialization_normalizer({"deal_context"}, previous)(value)
        decision_path = value["sections"]["deal_context"]["decision_path"]
        self.assertEqual(decision_path["process"], "Новый процесс")
        self.assertEqual(decision_path["evidence"], ["call:2"])
        self.assertEqual(decision_path["influencers"], ["Технический директор"])
        self.assertIn(
            {"path": "deal_context.decision_path.influencers", "action": "preserved_missing_object_key"},
            changes,
        )

    def test_materialization_normalizer_does_not_replace_invalid_provided_enum(self) -> None:
        previous = {"recommendation_feedback": {"status": "unconfirmed"}}
        value = {"sections": {"recommendation_feedback": {"status": "invented_status"}}}
        _materialization_normalizer({"recommendation_feedback"}, previous)(value)
        self.assertEqual(
            value["sections"]["recommendation_feedback"]["status"],
            "invented_status",
        )

    def test_materialization_normalizer_never_truncates_distinct_list_items(self) -> None:
        previous = {"deal_control_brief": {"known_facts": ["old"]}}
        value = {
            "sections": {
                "deal_control_brief": {
                    "known_facts": [f"fact-{index}" for index in range(8)],
                },
            },
        }
        changes = _materialization_normalizer({"deal_control_brief"}, previous)(value)
        self.assertEqual(len(value["sections"]["deal_control_brief"]["known_facts"]), 8)
        self.assertFalse(
            any(change.get("action") == "trimmed_to_validator_limit" for change in changes)
        )

    def test_materialization_retry_is_narrow_repair_not_full_reanalysis(self) -> None:
        builder = _materialization_repair_prompt_builder(
            affected_sections=["deal_control_brief"],
            structural_templates={"deal_control_brief": {"known_facts": {"type": "array"}}},
            constraints={"list_limits": {"deal_control_brief.known_facts": 5}},
            compact_policy_text="POLICY_MARKER",
        )
        prompt = builder(
            "FULL_ORIGINAL_PROMPT_MARKER",
            "too many items at deal_control_brief.known_facts",
            '{"sections":{"deal_control_brief":{"known_facts":["a","b"]}}}',
        )
        self.assertNotIn("FULL_ORIGINAL_PROMPT_MARKER", prompt)
        self.assertIn("PREVIOUS_MATERIALIZATION_JSON", prompt)
        self.assertIn("POLICY_MARKER", prompt)
        self.assertIn("Не анализируй сделку заново", prompt)
        self.assertIn("не обрезай механически первые N", prompt)


class UsageAndReportMetadataTests(unittest.TestCase):
    def test_usage_distinguishes_logical_calls_and_api_attempts(self) -> None:
        rows = [
            {
                "model": "test-model", "semantic_attempt_count": 1,
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                          "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 3},
                          "output_tokens_details": {"reasoning_tokens": 1}},
                "estimated_cost": {"estimated_cost_usd": 0.1, "estimated_cost_rub": 9,
                                   "usd_rub_rate": 90, "billable_input_tokens": 8},
            },
            {
                "model": "test-model", "semantic_attempt_count": 2,
                "usage": {"input_tokens": 30, "output_tokens": 15, "total_tokens": 45,
                          "input_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 6},
                          "output_tokens_details": {"reasoning_tokens": 2}},
                "estimated_cost": {"estimated_cost_usd": 0.2, "estimated_cost_rub": 18,
                                   "usd_rub_rate": 90, "billable_input_tokens": 26},
            },
        ]
        usage = _usage_summary(rows)
        cost = _estimated_cost_summary(rows, usage)
        self.assertEqual(usage["logical_calls"], 2)
        self.assertEqual(usage["api_attempts"], 3)
        self.assertEqual(usage["calls"], 3)
        self.assertEqual(usage["cache_write_tokens"], 9)
        self.assertEqual(cost["model"], "test-model")
        self.assertEqual(cost["usd_rub_rate"], 90)
        self.assertEqual(cost["estimated_cost_rub"], 27)

    def test_v2_report_receives_existing_renderer_metadata_shape(self) -> None:
        from openai_api.llm.analyze_deal_if_changed import _write_v2_candidate
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "analysis": root / "analysis.json", "v2_shadow": root / "shadow.json",
                "report": root / "report.md", "raw": root / "raw.txt",
            }
            result = IncrementalV2Result(
                analysis={}, semantic_state={"evidence_coverage": {}},
                changed_domains=[], affected_sections=[],
                metadata={
                    "model": "test-model",
                    "usage": {"input_tokens": 40, "api_attempts": 3},
                    "estimated_cost": {
                        "model": "test-model", "usd_rub_rate": 90,
                        "input_tokens": 40, "cached_input_tokens": 4,
                        "cache_write_tokens": 9, "output_tokens": 20,
                        "estimated_cost_usd": 0.3, "estimated_cost_rub": 27,
                    },
                    "_raw_outputs": [],
                },
            )
            with patch(
                "openai_api.llm.analyze_deal_if_changed.load_context_diagnostics_for_analysis",
                return_value=("", {"gaps": []}, []),
            ), patch(
                "openai_api.llm.analyze_deal_if_changed.render_report", return_value="report"
            ) as render:
                _write_v2_candidate(
                    paths=paths,
                    args=SimpleNamespace(deal_id="7", deal_root=str(root)),
                    result=result, stage_policy={}, prior_recommendation=None,
                    daily_checklist=None, production=True,
                )
        metadata = render.call_args.args[1]
        self.assertEqual(metadata["model"], "test-model")
        self.assertEqual(metadata["estimated_cost"]["cache_write_tokens"], 9)
        self.assertEqual(metadata["estimated_cost"]["usd_rub_rate"], 90)
        self.assertNotEqual(metadata["estimated_cost"], metadata["usage"])


class DuplicateRoutingTests(unittest.TestCase):
    def test_duplicate_plus_soft_trigger_stays_mini_and_duplicate_only_skips(self) -> None:
        from openai_api.change_detection.decision_engine import MINI_RECOMMENDATION_NO_LLM, SKIPPED_NO_CHANGES
        from openai_api.llm.analyze_deal_if_changed import duplicate_evidence_legacy_decision
        with patch("openai_api.llm.analyze_deal_if_changed.soft_diff_triggers", return_value=[{"trigger_type": "new_task"}]), patch(
            "openai_api.llm.analyze_deal_if_changed.mini_triggers", return_value=[]
        ):
            mini = duplicate_evidence_legacy_decision(
                diff={"changes": ["transcript_changed", "new_task"]}, snapshot={},
                previous_state={}, last_memory=None,
            )
        with patch("openai_api.llm.analyze_deal_if_changed.soft_diff_triggers", return_value=[]), patch(
            "openai_api.llm.analyze_deal_if_changed.mini_triggers", return_value=[]
        ):
            skip = duplicate_evidence_legacy_decision(
                diff={"changes": ["transcript_changed"]}, snapshot={},
                previous_state={}, last_memory=None,
            )
        self.assertEqual(mini.status, MINI_RECOMMENDATION_NO_LLM)
        self.assertEqual(skip.status, SKIPPED_NO_CHANGES)


if __name__ == "__main__":
    unittest.main()
