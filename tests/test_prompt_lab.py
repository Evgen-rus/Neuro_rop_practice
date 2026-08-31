from __future__ import annotations

import gc
import inspect
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api import app as api_app
from api import prompt_lab as lab
from openai_api.llm.deal_manager_situation import MANAGER_MODEL, MANAGER_REASONING_EFFORT
from openai_api.llm.prompt_lab_models import list_lab_models, validate_model_reasoning
from openai_api.llm.prompt_parts import sha256_text
from storage import prompt_lab_db as lab_db


FORBIDDEN_PRODUCTION_WRITES = (
    "save_deal_manager_quick_help",
    "save_deal_manager_full_script",
    "save_deal_manager_call_script",
    "save_deal_manager_email_script",
    "save_deal_manager_followups",
    "save_deal_manager_companion",
    "record_quick_help_opened_event",
    "record_recommendation_lifecycle_event",
    "start_quick_help_job",
    "start_full_script_job",
    "start_followups_job",
    "start_companion_job",
    "start_analyze_job",
)


CONTEXT = {
    "deal": {"deal_id": "101", "title": "Тест"},
    "deal_projection": {"deal_id": "101"},
    "report": {"id": 17, "analysis_run_id": 44},
    "source_report_id": 17,
    "analysis_projection": {"deal_state": {"summary": "КП отправлено"}},
    "current_bitrix_task": None,
    "situation": {"status": "confirmed"},
    "situation_review": {"id": 9},
    "situation_id": 9,
    "situation_status": "confirmed",
    "situation_projection": {"current_situation": "Клиент получил КП"},
}


def _email_content(subject: str) -> dict:
    return {
        "email_contract": "v1",
        "subject": subject,
        "greeting": "Здравствуйте",
        "context": "КП отправлено",
        "questions": [],
        "value_point": "польза",
        "call_to_action": "ответьте",
        "closing": "спасибо",
    }


def _qh_entry(mode: str, entry_id: int) -> dict:
    return {
        "id": entry_id,
        "mode": mode,
        "content": {"mode": mode, "client_messages": {"primary": mode.upper()}},
        "model_meta": {"model": MANAGER_MODEL, "reasoning_effort": MANAGER_REASONING_EFFORT},
    }


def _save_lab_snapshot(db: Path, *, deal_id: str = "101", snapshot_hash: str = "snap") -> dict:
    return lab_db.save_snapshot(
        db,
        deal_id=deal_id,
        source_report_id=17,
        analysis_run_id=44,
        situation_id=9,
        situation_status="confirmed",
        snapshot_hash=snapshot_hash,
        provenance={"deal_id": deal_id},
        context={
            "deal": {"deal_id": deal_id, "title": "Тест"},
            "deal_projection": {"deal_id": deal_id},
            "analysis_projection": CONTEXT["analysis_projection"],
            "situation_projection": CONTEXT["situation_projection"],
            "current_bitrix_task": None,
            "communication_pattern_context": {},
            "last_contact": {},
            "objection_handling": {"items": []},
            "manager_tactics_hash": "t",
        },
    )


def _start_email(
    lab_path: Path,
    *,
    snapshot_id: int,
    upstream_run_id: int | None,
    branch: str = "experiment",
    quick_help_mode: str | None = "reanimator",
    deal_id: str = "101",
) -> dict:
    return lab.start_lab_run(
        deal_id=deal_id,
        module_key="full_script.email",
        branch=branch,
        snapshot_id=int(snapshot_id),
        upstream_run_id=upstream_run_id,
        quick_help_mode=quick_help_mode,
        reuse_existing=False,
        prompt_template="SYSTEM_RULES:\ntest",
        model=MANAGER_MODEL,
        reasoning=MANAGER_REASONING_EFFORT,
        selected_strategy="primary",
        lab_db_path=lab_path,
        db_path=lab_path.parent / "unused.sqlite",
    )


def _qh_result(mode: str) -> dict:
    return {"mode": mode, "client_messages": {"primary": mode.upper()}}


def _save_lab_run(db: Path, snapshot_id: int, *, module_key: str, branch: str, result: dict, fingerprint: str, upstream_run_id: int | None = None, deal_id: str = "101") -> dict:
    return lab_db.save_run(
        db,
        session_id=None,
        turn_id=None,
        snapshot_id=int(snapshot_id),
        deal_id=str(deal_id),
        module_key=module_key,
        branch=branch,
        prompt_version_id=None,
        prompt_hash="h",
        prompt_text="t",
        effective_prompt="t",
        dependency_fingerprints={},
        schema_version="strategy_v3",
        material_revision=None,
        model=MANAGER_MODEL,
        reasoning=MANAGER_REASONING_EFFORT,
        max_output_tokens=1,
        question="",
        selected_strategy=None,
        upstream_run_id=upstream_run_id,
        fingerprint=fingerprint,
        status="success",
        error=None,
        result=result,
        usage=None,
        cost=None,
        latency_seconds=0.1,
        response_status="completed",
        semantic_attempt_count=1,
        call_type="prompt_lab_quick_help" if module_key.startswith("quick_help.") else "prompt_lab_full_script",
    )


def _admin():
    return {"id": 1, "role": "admin", "manager_id": None, "login": "admin"}


def _manager():
    return {"id": 2, "role": "manager", "manager_id": "10", "login": "manager"}


class PromptLabIsolationTests(unittest.TestCase):
    def tearDown(self) -> None:
        with lab._LOCK:
            lab._JOBS.clear()
            lab._RAW_EXCHANGES.clear()

    def test_source_does_not_call_production_write_helpers(self) -> None:
        source = inspect.getsource(lab)
        for name in FORBIDDEN_PRODUCTION_WRITES:
            self.assertNotIn(name, source, name)

    def test_non_admin_gets_403(self) -> None:
        with patch.object(api_app, "auth_current_user", return_value=_manager()):
            with self.assertRaises(HTTPException) as raised:
                api_app.prompt_lab_bootstrap_get(deal_id="101")
        self.assertEqual(raised.exception.status_code, 403)

    def test_raw_exchange_is_admin_only_and_consumed_once(self) -> None:
        job_id = "raw-job"
        bundle = {
            "format": "prompt_lab_raw_exchange_v1",
            "job_id": job_id,
            "run_id": 7,
            "deal_id": "101",
            "module_key": "quick_help.push",
            "branch": "experiment",
            "status": "done",
            "created_at": "2026-08-31T10:00:00+03:00",
            "completed_at": "2026-08-31T10:00:01+03:00",
            "attempts": [{"attempt": 1, "request": {"input": "raw"}, "response": {"output": []}}],
        }
        with lab._LOCK:
            lab._JOBS[job_id] = lab.PromptLabJob(
                job_id=job_id,
                deal_id="101",
                module_key="quick_help.push",
                branch="experiment",
                status="done",
            )
            lab._RAW_EXCHANGES[job_id] = (lab.time() + 60, bundle)

        with patch.object(api_app, "auth_current_user", return_value=_manager()):
            with self.assertRaises(HTTPException) as forbidden:
                api_app.prompt_lab_job_raw_exchange_get(job_id)
        self.assertEqual(forbidden.exception.status_code, 403)

        with patch.object(api_app, "auth_current_user", return_value=_admin()), patch.object(
            api_app, "require_deal"
        ):
            received = api_app.prompt_lab_job_raw_exchange_get(job_id)
            self.assertEqual(received, bundle)
            with self.assertRaises(HTTPException) as consumed:
                api_app.prompt_lab_job_raw_exchange_get(job_id)
        self.assertEqual(consumed.exception.status_code, 404)

    def test_bootstrap_does_not_start_production_jobs_or_opened_events(self) -> None:
        with patch.object(api_app, "auth_current_user", return_value=_admin()), patch.object(
            api_app, "require_deal"
        ), patch.object(lab, "load_manager_screen_context", return_value=CONTEXT), patch.object(
            lab, "_storage_call", return_value=None
        ) as storage_call, patch.object(
            lab, "load_manager_tactics", return_value="## T1 — test\n"
        ), patch("api.prompt_lab.find_last_contact", return_value=None):
            result = api_app.prompt_lab_bootstrap_get(deal_id="101", module_key="quick_help.push")
        self.assertFalse(result["production_current"]["exists"])
        self.assertEqual(result["runtime"]["model"], MANAGER_MODEL)
        self.assertEqual(result["runtime"]["reasoning"], MANAGER_REASONING_EFFORT)
        called = [item.args[0] for item in storage_call.call_args_list]
        self.assertTrue(all(name.startswith("get_") or name.startswith("list_") for name in called))

    def test_confirmation_gate_blocks_unrefined_situation(self) -> None:
        blocked = dict(CONTEXT)
        blocked["situation_status"] = "refined"
        spec = lab.get_module("quick_help.push") if hasattr(lab, "get_module") else None
        from api.prompt_lab_modules import get_module
        gate = lab._gate_state(blocked, get_module("quick_help.push"))
        self.assertFalse(gate["ok"])
        self.assertIn("не подтверждена", gate["reason"])

    def test_lab_run_does_not_write_production_quick_help(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "prompt_lab.sqlite"
            snapshot = {
                "id": 1,
                "deal_id": "101",
                "snapshot_hash": "abc",
                "situation_status": "confirmed",
                "source_report_id": 17,
                "situation_id": 9,
                "context": {
                    "deal": CONTEXT["deal"],
                    "deal_projection": CONTEXT["deal_projection"],
                    "analysis_projection": CONTEXT["analysis_projection"],
                    "situation_projection": CONTEXT["situation_projection"],
                    "current_bitrix_task": None,
                    "communication_pattern_context": {},
                    "last_contact": {},
                    "objection_handling": {"items": []},
                    "manager_tactics_hash": "t",
                },
            }
            saved_runs: list[dict] = []

            def fake_save_run(_path, **kwargs):
                saved_runs.append(kwargs)
                return {"id": 7, **kwargs}

            def fake_generate(**kwargs):
                kwargs["raw_exchange_callback"]({
                    "attempt": 1,
                    "requested_at": "2026-08-31T10:00:00.000+00:00",
                    "latency_seconds": 0.2,
                    "request": {"input": "точный запрос"},
                    "response": {"output": []},
                    "raw_output_text": '{"mode":"push"}',
                    "error": None,
                })
                return {"mode": "push"}, {
                    "call_type": "prompt_lab_quick_help",
                    "usage": {},
                    "latency_seconds": 0.2,
                }

            with patch.object(lab, "create_snapshot", return_value=snapshot), patch.object(
                lab_db, "latest_snapshot", return_value=snapshot
            ), patch.object(lab_db, "get_snapshot", return_value=snapshot), patch.object(
                lab_db, "find_run_by_fingerprint", return_value=None
            ), patch.object(lab_db, "create_session", return_value={"id": 1}), patch.object(
                lab_db, "create_turn", return_value={"id": 1}
            ), patch.object(lab_db, "save_run", side_effect=fake_save_run), patch.object(
                lab, "generate_deal_manager_quick_help", side_effect=fake_generate
            ) as generate, patch.object(
                lab, "_storage_call"
            ) as storage_call:
                job = lab.start_lab_run(
                    deal_id="101",
                    module_key="quick_help.push",
                    branch="experiment",
                    snapshot_id=1,
                    reuse_existing=False,
                    lab_db_path=lab_path,
                    prompt_template="SYSTEM_RULES:\ntest",
                    model=MANAGER_MODEL,
                    reasoning=MANAGER_REASONING_EFFORT,
                )
                job_id = job["job_id"]
                for _ in range(50):
                    current = lab.get_lab_job(job_id)
                    if current and current["status"] in {"done", "error"}:
                        break
                    time.sleep(0.02)
            self.assertTrue(generate.called)
            self.assertEqual(generate.call_args.kwargs["call_type"], "prompt_lab_quick_help")
            self.assertFalse(storage_call.called)
            self.assertEqual(saved_runs[0]["status"], "success")
            self.assertEqual(saved_runs[0]["call_type"], "prompt_lab_quick_help")
            self.assertNotIn("raw_exchange", saved_runs[0])
            raw = lab.consume_lab_raw_exchange(job_id)
            self.assertEqual(raw["attempts"][0]["request"]["input"], "точный запрос")
            self.assertIsNone(lab.consume_lab_raw_exchange(job_id))

    def test_error_run_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "prompt_lab.sqlite"
            snapshot = {
                "id": 1,
                "deal_id": "101",
                "snapshot_hash": "abc",
                "situation_status": "confirmed",
                "source_report_id": 17,
                "situation_id": 9,
                "context": {
                    "deal": CONTEXT["deal"],
                    "deal_projection": CONTEXT["deal_projection"],
                    "analysis_projection": CONTEXT["analysis_projection"],
                    "situation_projection": CONTEXT["situation_projection"],
                    "current_bitrix_task": None,
                    "communication_pattern_context": {},
                    "last_contact": {},
                    "objection_handling": {"items": []},
                    "manager_tactics_hash": "t",
                },
            }
            saved = []

            def fake_save(_path, **kwargs):
                saved.append(kwargs)
                return {"id": 3, **kwargs}

            with patch.object(lab_db, "get_snapshot", return_value=snapshot), patch.object(
                lab_db, "find_run_by_fingerprint", return_value=None
            ), patch.object(lab_db, "create_session", return_value={"id": 1}), patch.object(
                lab_db, "create_turn", return_value={"id": 1}
            ), patch.object(lab_db, "save_run", side_effect=fake_save), patch.object(
                lab, "generate_deal_manager_quick_help", side_effect=ValueError("Quick Help имеет неподдерживаемый контракт")
            ):
                job = lab.start_lab_run(
                    deal_id="101",
                    module_key="quick_help.reanimator",
                    branch="experiment",
                    snapshot_id=1,
                    reuse_existing=False,
                    lab_db_path=lab_path,
                    prompt_template="broken",
                    model=MANAGER_MODEL,
                    reasoning=MANAGER_REASONING_EFFORT,
                )
                for _ in range(50):
                    current = lab.get_lab_job(job["job_id"])
                    if current and current["status"] in {"done", "error"}:
                        break
                    time.sleep(0.02)
            self.assertEqual(saved[0]["status"], "error")
            self.assertTrue(saved[0]["error"])


class PromptLabStorageTests(unittest.TestCase):
    def test_prompt_version_is_immutable_and_edit_creates_next(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "lab.sqlite"
            first = lab_db.save_prompt_version(
                db, prompt_key="quick_help.push", prompt_text="v1 text", prompt_hash="h1"
            )
            second = lab_db.save_prompt_version(
                db,
                prompt_key="quick_help.push",
                prompt_text="v2 text",
                prompt_hash="h2",
                based_on_id=int(first["id"]),
            )
            self.assertEqual(first["version_number"], 1)
            self.assertEqual(second["version_number"], 2)
            self.assertEqual(lab_db.get_prompt_version(db, int(first["id"]))["prompt_text"], "v1 text")

    def test_used_version_cannot_be_hard_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "lab.sqlite"
            version = lab_db.save_prompt_version(
                db, prompt_key="followups", prompt_text="text", prompt_hash="h"
            )
            snapshot = lab_db.save_snapshot(
                db,
                deal_id="101",
                source_report_id=1,
                analysis_run_id=2,
                situation_id=3,
                situation_status="confirmed",
                snapshot_hash="snap",
                provenance={"deal_id": "101"},
                context={"deal": {"deal_id": "101"}},
            )
            lab_db.save_run(
                db,
                session_id=None,
                turn_id=None,
                snapshot_id=int(snapshot["id"]),
                deal_id="101",
                module_key="followups",
                branch="experiment",
                prompt_version_id=int(version["id"]),
                prompt_hash="h",
                prompt_text="text",
                effective_prompt="text",
                dependency_fingerprints={},
                schema_version="followup_plan_v1",
                material_revision=None,
                model="gpt-5.6-luna",
                reasoning="low",
                max_output_tokens=100,
                question="",
                selected_strategy=None,
                upstream_run_id=None,
                fingerprint="fp",
                status="error",
                error="validation failed",
                result=None,
                usage=None,
                cost=None,
                latency_seconds=1.2,
                response_status="incomplete",
                semantic_attempt_count=1,
                call_type="prompt_lab_followups",
            )
            archived = lab_db.archive_prompt_version(db, int(version["id"]))
            self.assertTrue(archived["archived"])
            with self.assertRaises(ValueError):
                lab_db.delete_prompt_version(db, int(version["id"]))

    def test_same_snapshot_hash_for_both_branches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "lab.sqlite"
            snapshot = lab_db.save_snapshot(
                db,
                deal_id="101",
                source_report_id=1,
                analysis_run_id=2,
                situation_id=3,
                situation_status="confirmed",
                snapshot_hash="same-hash",
                provenance={"snapshot_hash": "same-hash"},
                context={},
            )
            for branch in ("current", "experiment"):
                lab_db.save_run(
                    db,
                    session_id=None,
                    turn_id=None,
                    snapshot_id=int(snapshot["id"]),
                    deal_id="101",
                    module_key="quick_help.push",
                    branch=branch,
                    prompt_version_id=None,
                    prompt_hash="p",
                    prompt_text="t",
                    effective_prompt="t",
                    dependency_fingerprints={"snapshot_hash": "same-hash"},
                    schema_version="strategy_v3",
                    material_revision=None,
                    model="m",
                    reasoning="low",
                    max_output_tokens=1,
                    question="q",
                    selected_strategy=None,
                    upstream_run_id=None,
                    fingerprint=f"fp-{branch}",
                    status="success",
                    error=None,
                    result={"ok": True},
                    usage=None,
                    cost=None,
                    latency_seconds=0.1,
                    response_status="completed",
                    semantic_attempt_count=1,
                    call_type="prompt_lab_quick_help",
                )
            runs = lab_db.list_runs(db, deal_id="101")
            hashes = {item["dependency_fingerprints"]["snapshot_hash"] for item in runs}
            self.assertEqual(hashes, {"same-hash"})
            gc.collect()

    def test_export_payload_has_no_client_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "lab.sqlite"
            version = lab.save_version(
                prompt_key="quick_help.push",
                prompt_text="SYSTEM_RULES:\nбез CRM",
                lab_db_path=db,
            )
            lab_db.update_prompt_version_labels(db, int(version["id"]), candidate=True)
            payload = lab.export_payload(mode="candidates_all", lab_db_path=db)
            blob = str(payload)
            self.assertNotIn("Клиент", blob)
            self.assertNotIn("deal_id", blob)
            self.assertIn("без CRM", blob)

    def test_model_reasoning_validation(self) -> None:
        with self.assertRaises(ValueError):
            validate_model_reasoning("unknown-model", "low")
        model, effort = validate_model_reasoning(MANAGER_MODEL, MANAGER_REASONING_EFFORT)
        self.assertEqual(model, MANAGER_MODEL)
        self.assertEqual(effort, MANAGER_REASONING_EFFORT)

    def test_production_schema_is_reused(self) -> None:
        from openai_api.llm.deal_manager_quick_help import _ANSWER_CONTRACT, validate_quick_help
        from api.prompt_lab_modules import get_module
        self.assertEqual(get_module("quick_help.push")["schema_version"], _ANSWER_CONTRACT)
        with self.assertRaises(ValueError):
            validate_quick_help({"answer_contract": "nope"}, allowed_tactic_ids=("T1",), expected_mode="push")

    def test_downstream_uses_branch_quick_help(self) -> None:
        extra = {"quick_help": {"mode": "push", "client_messages": {"primary": "EXPERIMENT TEXT"}}}
        spec = __import__("api.prompt_lab_modules", fromlist=["get_module"]).get_module("full_script.email")
        snapshot = {"context": {
            "analysis_projection": {},
            "situation_projection": {},
            "deal": {},
            "current_bitrix_task": None,
            "communication_pattern_context": {},
            "objection_handling": {"items": []},
        }}
        kwargs = lab._generate_kwargs(spec, snapshot, extra, prompt_template=None)
        self.assertEqual(kwargs["quick_help"]["client_messages"]["primary"], "EXPERIMENT TEXT")
        self.assertEqual(kwargs["selected_strategy"], "primary")

    def test_companion_keeps_manager_note(self) -> None:
        spec = __import__("api.prompt_lab_modules", fromlist=["get_module"]).get_module("companion")
        snapshot = {"context": {
            "analysis_projection": {},
            "situation_projection": {},
            "deal": {},
            "current_bitrix_task": None,
            "communication_pattern_context": {},
            "last_contact": {"event_id": "crm_activity:1"},
        }}
        kwargs = lab._generate_kwargs(
            spec,
            snapshot,
            {"manager_note": "короче", "previous_message": "было длинно"},
            prompt_template=None,
        )
        self.assertEqual(kwargs["manager_note"], "короче")
        self.assertEqual(kwargs["previous_message"], "было длинно")
        self.assertEqual(kwargs["last_contact"]["event_id"], "crm_activity:1")

    def test_prompt_hash_helper_is_stable(self) -> None:
        self.assertEqual(sha256_text("привет"), sha256_text("привет"))
        self.assertNotEqual(sha256_text("привет"), sha256_text("пока"))

    def test_verified_capability_map_rejects_unsupported_reasoning(self) -> None:
        from openai_api.llm.prompt_lab_models import MODEL_REASONING
        with self.assertRaises(ValueError):
            validate_model_reasoning("gpt-5.4-mini", "xhigh")
        with self.assertRaises(ValueError):
            validate_model_reasoning("gpt-5.4", "max")
        self.assertEqual(MODEL_REASONING["gpt-5.4-mini"], ("none", "low", "medium", "high"))
        self.assertNotIn("max", MODEL_REASONING["gpt-5.4"])
        ids = {item["id"] for item in list_lab_models()}
        self.assertNotIn("gpt-5.6-terra-mini", ids)
        self.assertFalse(any("terra mini" in str(item["label"]).lower() for item in list_lab_models()))

    def test_export_current_uses_selected_version_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "lab.sqlite"
            first = lab.save_version(prompt_key="quick_help.push", prompt_text="первая версия", lab_db_path=db)
            second = lab.save_version(prompt_key="quick_help.push", prompt_text="выбранная версия", lab_db_path=db)
            with self.assertRaises(ValueError):
                lab.export_payload(mode="current", prompt_key="quick_help.push", lab_db_path=db)
            payload = lab.export_payload(
                mode="current",
                prompt_key="quick_help.push",
                version_id=int(first["id"]),
                lab_db_path=db,
            )
            self.assertEqual(len(payload["items"]), 1)
            self.assertEqual(payload["items"][0]["prompt_text"], "первая версия")
            self.assertNotEqual(payload["items"][0]["prompt_text"], second["prompt_text"])

    def test_import_production_current_is_zero_cost_lab_run(self) -> None:
        entry = {
            "id": 88,
            "content": {"mode": "push", "situation_summary": "КП отправлено"},
            "model_meta": {"model": MANAGER_MODEL, "reasoning_effort": MANAGER_REASONING_EFFORT},
            "question": "как дожать",
        }

        def storage(name, *_args, **_kwargs):
            if name == "get_current_deal_manager_quick_help":
                return entry
            return None

        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            with patch.object(lab, "load_manager_screen_context", return_value=CONTEXT), patch.object(
                lab, "_storage_call", side_effect=storage
            ), patch.object(lab, "_load_local_communications", return_value=[]), patch.object(
                lab, "find_last_contact", return_value=None
            ), patch.object(lab, "load_manager_tactics", return_value="## T1 — test\n"), patch.object(
                lab, "generate_deal_manager_quick_help"
            ) as generate:
                current, snapshot = lab.import_production_current(
                    deal_id="101",
                    module_key="quick_help.push",
                    context=CONTEXT,
                    db_path=Path(directory) / "unused.sqlite",
                    lab_db_path=lab_path,
                )
            generate.assert_not_called()
            run = current["lab_run"]
            self.assertIsNotNone(snapshot)
            self.assertIsNotNone(run)
            self.assertGreater(int(run["id"]), 0)
            self.assertEqual(run["branch"], "current")
            self.assertEqual(run["response_status"], "imported")
            self.assertEqual(run["cost"]["estimated_cost_usd"], 0)
            self.assertEqual(run["dependency_fingerprints"]["origin"], "production_reference")
            self.assertEqual(run["dependency_fingerprints"]["production_id"], 88)

    def test_import_production_email_reuses_get_helpers(self) -> None:
        quick_help = {"id": 12, "mode": "push", "content": {"mode": "push", "client_messages": {"primary": "текст"}}}
        email = {
            "id": 4,
            "content": {
                "email_contract": "v1",
                "subject": "Тема",
                "greeting": "Здравствуйте",
                "context": "КП отправлено",
                "questions": [],
                "value_point": "польза",
                "call_to_action": "ответьте",
                "closing": "спасибо",
            },
            "model_meta": {"model": MANAGER_MODEL, "reasoning_effort": MANAGER_REASONING_EFFORT},
        }

        def storage(name, *_args, **_kwargs):
            if name == "get_current_deal_manager_quick_help":
                return quick_help
            if name == "get_deal_manager_email_script":
                return email
            return None

        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            with patch.object(lab, "load_manager_screen_context", return_value=CONTEXT), patch.object(
                lab, "_storage_call", side_effect=storage
            ), patch.object(lab, "_load_local_communications", return_value=[]), patch.object(
                lab, "find_last_contact", return_value=None
            ), patch.object(lab, "load_manager_tactics", return_value="## T1 — test\n"), patch.object(
                lab, "generate_deal_manager_email"
            ) as generate:
                current, snapshot = lab.import_production_current(
                    deal_id="101",
                    module_key="full_script.email",
                    context=CONTEXT,
                    selected_strategy="primary",
                    quick_help_mode="push",
                    db_path=Path(directory) / "unused.sqlite",
                    lab_db_path=lab_path,
                )
            generate.assert_not_called()
            run = current["lab_run"]
            self.assertGreater(int(run["id"]), 0)
            self.assertEqual(run["result"]["subject"], "Тема")
            self.assertIsNotNone(run["upstream_run_id"])
            upstream = lab_db.get_run(lab_path, int(run["upstream_run_id"]))
            self.assertEqual(upstream["module_key"], "quick_help.push")
            self.assertEqual(int(upstream["snapshot_id"]), int(snapshot["id"]))
            self.assertEqual(upstream["branch"], "current")

    def test_generate_both_orchestration_shares_one_snapshot_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            saved_ids: list[int] = []
            original_save = lab_db.save_snapshot

            def tracking_save(*args, **kwargs):
                row = original_save(*args, **kwargs)
                saved_ids.append(int(row["id"]))
                return row

            with patch.object(lab, "load_manager_screen_context", return_value=CONTEXT), patch.object(
                lab, "_storage_call", return_value=None
            ), patch.object(lab, "_load_local_communications", return_value=[]), patch.object(
                lab, "find_last_contact", return_value=None
            ), patch.object(lab, "load_manager_tactics", return_value="## T1 — test\n"), patch.object(
                lab_db, "save_snapshot", side_effect=tracking_save
            ), patch.object(lab, "_effective_prompt", return_value="SYSTEM_RULES:\ntest"), patch.object(
                lab,
                "generate_deal_manager_quick_help",
                return_value=({"mode": "push"}, {"call_type": "prompt_lab_quick_help", "usage": {}, "latency_seconds": 0.01, "response_status": "completed"}),
            ):
                snapshot = lab.get_or_create_snapshot(
                    deal_id="101",
                    db_path=Path(directory) / "unused.sqlite",
                    lab_db_path=lab_path,
                )
                snapshot_id = int(snapshot["id"])

                def start(branch: str) -> dict:
                    return lab.start_lab_run(
                        deal_id="101",
                        module_key="quick_help.push",
                        branch=branch,
                        snapshot_id=snapshot_id,
                        reuse_existing=False,
                        prompt_template="SYSTEM_RULES:\ntest",
                        model=MANAGER_MODEL,
                        reasoning=MANAGER_REASONING_EFFORT,
                        db_path=Path(directory) / "unused.sqlite",
                        lab_db_path=lab_path,
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    jobs = list(pool.map(start, ("current", "experiment")))
                for job in jobs:
                    for _ in range(80):
                        current = lab.get_lab_job(job["job_id"])
                        if current and current["status"] in {"done", "error"}:
                            break
                        time.sleep(0.02)
            runs = lab_db.list_runs(lab_path, deal_id="101")
            self.assertEqual(len(saved_ids), 1)
            self.assertEqual({int(item["snapshot_id"]) for item in runs}, {snapshot_id})
            self.assertEqual({item["branch"] for item in runs}, {"current", "experiment"})

    def test_parallel_start_without_snapshot_id_does_not_create_two_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            saved_ids: list[int] = []
            original_save = lab_db.save_snapshot

            def slow_save(*args, **kwargs):
                time.sleep(0.04)
                row = original_save(*args, **kwargs)
                saved_ids.append(int(row["id"]))
                return row

            with patch.object(lab, "load_manager_screen_context", return_value=CONTEXT), patch.object(
                lab, "_storage_call", return_value=None
            ), patch.object(lab, "_load_local_communications", return_value=[]), patch.object(
                lab, "find_last_contact", return_value=None
            ), patch.object(lab, "load_manager_tactics", return_value="## T1 — test\n"), patch.object(
                lab_db, "save_snapshot", side_effect=slow_save
            ), patch.object(lab, "_effective_prompt", return_value="SYSTEM_RULES:\ntest"), patch.object(
                lab,
                "generate_deal_manager_quick_help",
                return_value=({"mode": "push"}, {"call_type": "prompt_lab_quick_help", "usage": {}, "latency_seconds": 0.01, "response_status": "completed"}),
            ):
                def start(branch: str) -> dict:
                    return lab.start_lab_run(
                        deal_id="101",
                        module_key="quick_help.push",
                        branch=branch,
                        snapshot_id=None,
                        reuse_existing=False,
                        prompt_template="SYSTEM_RULES:\ntest",
                        model=MANAGER_MODEL,
                        reasoning=MANAGER_REASONING_EFFORT,
                        db_path=Path(directory) / "unused.sqlite",
                        lab_db_path=lab_path,
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    jobs = list(pool.map(start, ("current", "experiment")))
                for job in jobs:
                    for _ in range(80):
                        current = lab.get_lab_job(job["job_id"])
                        if current and current["status"] in {"done", "error"}:
                            break
                        time.sleep(0.02)
            runs = lab_db.list_runs(lab_path, deal_id="101")
            self.assertEqual(len(saved_ids), 1)
            self.assertEqual(len({int(item["snapshot_id"]) for item in runs}), 1)

    def test_import_full_script_uses_requested_quick_help_mode(self) -> None:
        called_modes: list[str | None] = []

        def storage(name, *_args, **kwargs):
            if name == "get_current_deal_manager_quick_help":
                called_modes.append(kwargs.get("mode"))
                mode = str(kwargs.get("mode") or "")
                return _qh_entry(mode, 1 if mode == "push" else 2)
            if name == "get_deal_manager_email_script":
                subject = "PUSH MAIL" if kwargs.get("quick_help_id") == 1 else "REANIMATOR MAIL"
                return {"id": 10 if kwargs.get("quick_help_id") == 1 else 20, "content": _email_content(subject)}
            return None

        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            with patch.object(lab, "load_manager_screen_context", return_value=CONTEXT), patch.object(
                lab, "_storage_call", side_effect=storage
            ), patch.object(lab, "_load_local_communications", return_value=[]), patch.object(
                lab, "find_last_contact", return_value=None
            ), patch.object(lab, "load_manager_tactics", return_value="## T1 — test\n"):
                current, snapshot = lab.import_production_current(
                    deal_id="101",
                    module_key="full_script.email",
                    context=CONTEXT,
                    selected_strategy="primary",
                    quick_help_mode="reanimator",
                    db_path=Path(directory) / "unused.sqlite",
                    lab_db_path=lab_path,
                )
            self.assertTrue(called_modes)
            self.assertTrue(all(item == "reanimator" for item in called_modes))
            self.assertNotIn("push", called_modes)
            self.assertEqual(current["production_quick_help"]["mode"], "reanimator")
            self.assertEqual(current["lab_run"]["result"]["subject"], "REANIMATOR MAIL")
            upstream = lab_db.get_run(lab_path, int(current["lab_run"]["upstream_run_id"]))
            self.assertEqual(upstream["module_key"], "quick_help.reanimator")
            self.assertEqual(upstream["deal_id"], "101")
            self.assertEqual(upstream["branch"], "current")
            self.assertEqual(int(upstream["snapshot_id"]), int(snapshot["id"]))
            self.assertEqual(upstream["status"], "success")

    def test_experiment_downstream_selects_same_quick_help_mode(self) -> None:
        mixed = [
            {"id": 11, "deal_id": "101", "snapshot_id": 1, "module_key": "quick_help.push", "branch": "experiment", "status": "success"},
            {"id": 22, "deal_id": "101", "snapshot_id": 1, "module_key": "quick_help.reanimator", "branch": "experiment", "status": "success"},
            {"id": 33, "deal_id": "101", "snapshot_id": 1, "module_key": "quick_help.reanimator", "branch": "current", "status": "success"},
            {"id": 44, "deal_id": "101", "snapshot_id": 2, "module_key": "quick_help.reanimator", "branch": "experiment", "status": "success"},
        ]
        chosen = lab.select_qh_upstream_run(
            mixed,
            branch="experiment",
            mode="reanimator",
            deal_id="101",
            snapshot_id=1,
        )
        self.assertEqual(chosen["id"], 22)
        self.assertEqual(
            lab.select_qh_upstream_run(mixed, branch="experiment", mode="push", deal_id="101", snapshot_id=1)["id"],
            11,
        )

        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            snapshot = _save_lab_snapshot(lab_path)
            _save_lab_run(
                lab_path,
                int(snapshot["id"]),
                module_key="quick_help.push",
                branch="experiment",
                result=_qh_result("push"),
                fingerprint="fp-push",
            )
            reanimator = _save_lab_run(
                lab_path,
                int(snapshot["id"]),
                module_key="quick_help.reanimator",
                branch="experiment",
                result=_qh_result("reanimator"),
                fingerprint="fp-reanimator",
            )
            selected = lab.select_qh_upstream_run(
                lab_db.list_runs(lab_path, deal_id="101"),
                branch="experiment",
                mode="reanimator",
                deal_id="101",
                snapshot_id=int(snapshot["id"]),
            )
            self.assertEqual(int(selected["id"]), int(reanimator["id"]))
            captured: dict = {}

            def fake_email(**kwargs):
                captured["quick_help"] = kwargs.get("quick_help")
                return (
                    _email_content("ok"),
                    {"call_type": "prompt_lab_full_script", "usage": {}, "latency_seconds": 0.01, "response_status": "completed"},
                )

            with patch.object(lab, "_effective_prompt", return_value="SYSTEM_RULES:\ntest"), patch.object(
                lab, "generate_deal_manager_email", side_effect=fake_email
            ):
                job = lab.start_lab_run(
                    deal_id="101",
                    module_key="full_script.email",
                    branch="experiment",
                    snapshot_id=int(snapshot["id"]),
                    upstream_run_id=int(selected["id"]),
                    quick_help_mode="reanimator",
                    reuse_existing=False,
                    prompt_template="SYSTEM_RULES:\ntest",
                    model=MANAGER_MODEL,
                    reasoning=MANAGER_REASONING_EFFORT,
                    selected_strategy="primary",
                    lab_db_path=lab_path,
                    db_path=Path(directory) / "unused.sqlite",
                )
                for _ in range(80):
                    current = lab.get_lab_job(job["job_id"])
                    if current and current["status"] in {"done", "error"}:
                        break
                    time.sleep(0.02)
            self.assertEqual(captured["quick_help"]["mode"], "reanimator")
            self.assertEqual(captured["quick_help"]["client_messages"]["primary"], "REANIMATOR")

    def test_downstream_rejects_old_experiment_quick_help_of_other_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            old_snapshot = _save_lab_snapshot(lab_path, snapshot_hash="old")
            current_snapshot = _save_lab_snapshot(lab_path, snapshot_hash="current")
            old_qh = _save_lab_run(
                lab_path,
                int(old_snapshot["id"]),
                module_key="quick_help.reanimator",
                branch="experiment",
                result=_qh_result("reanimator"),
                fingerprint="fp-old-reanimator",
            )
            with self.assertRaises(ValueError) as raised:
                _start_email(
                    lab_path,
                    snapshot_id=int(current_snapshot["id"]),
                    upstream_run_id=int(old_qh["id"]),
                    quick_help_mode="reanimator",
                )
            self.assertEqual(str(raised.exception), lab.QH_UPSTREAM_REQUIRED)

    def test_downstream_rejects_current_run_as_experiment_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            snapshot = _save_lab_snapshot(lab_path)
            current_qh = _save_lab_run(
                lab_path,
                int(snapshot["id"]),
                module_key="quick_help.reanimator",
                branch="current",
                result=_qh_result("reanimator"),
                fingerprint="fp-current-reanimator",
            )
            with self.assertRaises(ValueError) as raised:
                _start_email(
                    lab_path,
                    snapshot_id=int(snapshot["id"]),
                    upstream_run_id=int(current_qh["id"]),
                    branch="experiment",
                    quick_help_mode="reanimator",
                )
            self.assertEqual(str(raised.exception), lab.QH_UPSTREAM_REQUIRED)

    def test_downstream_rejects_push_when_active_mode_is_reanimator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            snapshot = _save_lab_snapshot(lab_path)
            push_qh = _save_lab_run(
                lab_path,
                int(snapshot["id"]),
                module_key="quick_help.push",
                branch="experiment",
                result=_qh_result("push"),
                fingerprint="fp-push",
            )
            with self.assertRaises(ValueError) as raised:
                _start_email(
                    lab_path,
                    snapshot_id=int(snapshot["id"]),
                    upstream_run_id=int(push_qh["id"]),
                    quick_help_mode="reanimator",
                )
            self.assertEqual(str(raised.exception), lab.QH_UPSTREAM_REQUIRED)

    def test_downstream_rejects_quick_help_from_another_deal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            snapshot = _save_lab_snapshot(lab_path)
            other_qh = _save_lab_run(
                lab_path,
                int(snapshot["id"]),
                module_key="quick_help.reanimator",
                branch="experiment",
                result=_qh_result("reanimator"),
                fingerprint="fp-other-deal",
                deal_id="202",
            )
            with self.assertRaises(ValueError) as raised:
                _start_email(
                    lab_path,
                    snapshot_id=int(snapshot["id"]),
                    upstream_run_id=int(other_qh["id"]),
                    quick_help_mode="reanimator",
                    deal_id="101",
                )
            self.assertEqual(str(raised.exception), lab.QH_UPSTREAM_REQUIRED)

    def test_downstream_accepts_same_deal_branch_snapshot_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            snapshot = _save_lab_snapshot(lab_path)
            matching = _save_lab_run(
                lab_path,
                int(snapshot["id"]),
                module_key="quick_help.reanimator",
                branch="experiment",
                result=_qh_result("reanimator"),
                fingerprint="fp-matching",
            )
            captured: dict = {}

            def fake_email(**kwargs):
                captured["quick_help"] = kwargs.get("quick_help")
                return (
                    _email_content("ok"),
                    {"call_type": "prompt_lab_full_script", "usage": {}, "latency_seconds": 0.01, "response_status": "completed"},
                )

            with patch.object(lab, "_effective_prompt", return_value="SYSTEM_RULES:\ntest"), patch.object(
                lab, "generate_deal_manager_email", side_effect=fake_email
            ):
                job = _start_email(
                    lab_path,
                    snapshot_id=int(snapshot["id"]),
                    upstream_run_id=int(matching["id"]),
                    quick_help_mode="reanimator",
                )
                for _ in range(80):
                    current = lab.get_lab_job(job["job_id"])
                    if current and current["status"] in {"done", "error"}:
                        break
                    time.sleep(0.02)
            finished = lab.get_lab_job(job["job_id"])
            self.assertEqual(finished["status"], "done")
            self.assertEqual(captured["quick_help"]["mode"], "reanimator")
            self.assertEqual(captured["quick_help"]["client_messages"]["primary"], "REANIMATOR")

    def test_imported_current_downstream_keeps_same_quick_help_mode(self) -> None:
        def storage(name, *_args, **kwargs):
            if name == "get_current_deal_manager_quick_help":
                return _qh_entry(str(kwargs.get("mode") or ""), 7 if kwargs.get("mode") == "reanimator" else 3)
            if name == "get_deal_manager_call_script":
                if kwargs.get("quick_help_id") != 7:
                    return {"id": 99, "content": {"script_contract": "call", "conversation_goal": "push call"}}
                return {
                    "id": 8,
                    "content": {
                        "script_contract": "call_v1",
                        "conversation_goal": "reanimator call",
                        "blocks": [],
                        "closing_agreement": "договорённость",
                    },
                }
            if name == "get_deal_manager_full_script":
                return {"id": 5, "content": {"conversation_goal": "push message"}}
            return None

        with tempfile.TemporaryDirectory() as directory:
            lab_path = Path(directory) / "lab.sqlite"
            with patch.object(lab, "load_manager_screen_context", return_value=CONTEXT), patch.object(
                lab, "_storage_call", side_effect=storage
            ), patch.object(lab, "_load_local_communications", return_value=[]), patch.object(
                lab, "find_last_contact", return_value=None
            ), patch.object(lab, "load_manager_tactics", return_value="## T1 — test\n"):
                current, snapshot = lab.import_production_current(
                    deal_id="101",
                    module_key="full_script.call",
                    context=CONTEXT,
                    selected_strategy="primary",
                    quick_help_mode="reanimator",
                    db_path=Path(directory) / "unused.sqlite",
                    lab_db_path=lab_path,
                )
            self.assertEqual(current["lab_run"]["result"]["conversation_goal"], "reanimator call")
            upstream = lab_db.get_run(lab_path, int(current["lab_run"]["upstream_run_id"]))
            self.assertEqual(upstream["module_key"], "quick_help.reanimator")
            self.assertEqual(upstream["result"]["mode"], "reanimator")
            self.assertEqual(upstream["deal_id"], "101")
            self.assertEqual(upstream["branch"], "current")
            self.assertEqual(int(upstream["snapshot_id"]), int(snapshot["id"]))
            self.assertEqual(upstream["status"], "success")


if __name__ == "__main__":
    unittest.main()
