from __future__ import annotations

import subprocess
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import call, patch

from api.jobs import AnalyzeOptions, JobState, _JOBS, _run_job
from run_rop_assistant import (
    WorkflowOptions,
    analysis_error_file_signature,
    analysis_failure_error_message,
    refresh_workspace_after_transcription,
    run_analysis,
    transcribable_gaps,
)


def workflow_options() -> WorkflowOptions:
    return WorkflowOptions(
        entity_type="lead",
        entity_ids=["1", "2", "3"],
        history_days=60,
        include_related_contact_deals=True,
        include_internal_context=True,
        download_audio=False,
        redownload_audio=False,
        transcribe_audio=False,
        analyze=True,
        force_llm=False,
        transcript_mode="all",
    )


class AnalysisBatchResilienceTests(unittest.TestCase):
    def test_new_short_max_voice_is_transcribable_without_diagnostic_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entity_dir = root / "deal_42"
            audio_dir = entity_dir / "audio"
            diagnostics_dir = entity_dir / "diagnostics"
            audio_dir.mkdir(parents=True)
            diagnostics_dir.mkdir()
            audio_path = audio_dir / "max_voice_100.mp3"
            audio_path.write_bytes(b"short-voice")
            (diagnostics_dir / "context_gaps.json").write_text(json.dumps({"gaps": []}), encoding="utf-8")
            (audio_dir / "deal_42_call_audio_manifest.json").write_text(
                json.dumps(
                    {
                        "calls": [{
                            "activity_id": "max_100_abc",
                            "audio_kind": "max_voice",
                            "direction": "incoming",
                            "participant_name": "Клиент",
                            "source": "deal",
                            "owner_id": "42",
                            "start_time": "2026-08-24T10:00:00+03:00",
                            "downloads": [{
                                "ok": True,
                                "local_path": str(audio_path),
                                "duration_seconds": 8.0,
                                "is_short_no_answer": True,
                                "recording_ready_for_transcription": True,
                            }],
                        }]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("run_rop_assistant.workspace_root", return_value=root):
                gaps = transcribable_gaps("deal", "42")

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["activity_id"], "max_100_abc")
        self.assertEqual(gaps[0]["audio_kind"], "max_voice")
        self.assertEqual(gaps[0]["direction"], "incoming")

    def test_grown_source_makes_stale_transcript_transcribable_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entity_dir = root / "deal_42"
            audio_dir = entity_dir / "audio"
            transcript_dir = entity_dir / "transcripts"
            diagnostics_dir = entity_dir / "diagnostics"
            audio_dir.mkdir(parents=True)
            transcript_dir.mkdir()
            diagnostics_dir.mkdir()
            audio_path = audio_dir / "activity_77.mp3"
            audio_path.write_bytes(b"grown-audio")
            transcript_path = transcript_dir / "call_77_transcript.json"
            transcript_path.write_text(
                json.dumps({"metadata": {"activity_id": "77"}, "text": "old"}),
                encoding="utf-8",
            )
            (diagnostics_dir / "context_gaps.json").write_text(
                json.dumps({"gaps": []}),
                encoding="utf-8",
            )
            (audio_dir / "deal_42_call_audio_manifest.json").write_text(
                json.dumps(
                    {
                        "calls": [
                            {
                                "activity_id": "77",
                                "source": "deal",
                                "owner_id": "42",
                                "start_time": "2026-08-20T10:00:00+03:00",
                                "transcription": {"status": "stale_source_grew"},
                                "downloads": [
                                    {
                                        "ok": True,
                                        "local_path": str(audio_path),
                                        "recording_ready_for_transcription": True,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("run_rop_assistant.workspace_root", return_value=root),
                patch("openai_api.audio.short_call.is_short_no_answer_audio", return_value=False),
            ):
                gaps = transcribable_gaps("deal", "42")

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["activity_id"], "77")
        self.assertEqual(gaps[0]["audio_path"], str(audio_path))

    def test_deal_workspace_is_fully_refreshed_after_transcription(self) -> None:
        commands: list[tuple[list[str], str]] = []

        def fake_run(command: list[str], title: str) -> None:
            commands.append((command, title))

        with patch("run_rop_assistant.run_command", side_effect=fake_run):
            refresh_workspace_after_transcription("deal", ["42"])

        self.assertEqual(
            [title for _, title in commands],
            [
                "Обновление рабочего манифеста после транскрибации",
                "Обновление диагностики после транскрибации",
                "Обновление контекста сделки после транскрибации",
            ],
        )
        script_paths = [command[1].replace("\\", "/") for command, _ in commands]
        self.assertTrue(script_paths[0].endswith("bitrix/deals/3_prepare_deals_workspace.py"))
        self.assertTrue(script_paths[1].endswith("bitrix/context_diagnostics.py"))
        self.assertTrue(script_paths[2].endswith("bitrix/deals/4_build_deals_llm_context.py"))
        self.assertIn("42", commands[0][0])
        self.assertIn("42", commands[1][0])
        self.assertIn("42", commands[2][0])

    def test_run_analysis_continues_after_one_entity_fails(self) -> None:
        attempted: list[str] = []

        def fake_run(_command: list[str], title: str) -> None:
            attempted.append(title)
            if title.endswith("lead_2"):
                raise subprocess.CalledProcessError(1, ["analyze", "2"])

        with patch("run_rop_assistant.run_command", side_effect=fake_run):
            failures = run_analysis(workflow_options())

        self.assertEqual(attempted, ["LLM-анализ lead_1", "LLM-анализ lead_2", "LLM-анализ lead_3"])
        self.assertEqual([(item.entity_id, item.returncode) for item in failures], [("2", 1)])

    def test_analysis_failure_reads_saved_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            error_path = root / "deal_42" / "analysis" / "deal_42_analysis_error.json"
            error_path.parent.mkdir(parents=True)
            error_path.write_text(
                json.dumps({"error": "Invalid deal analysis: bad enum"}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch("run_rop_assistant.workspace_root", return_value=root):
                message = analysis_failure_error_message(
                    "deal", "42", 1, error_signature_before=None
                )
                missing = analysis_failure_error_message(
                    "deal", "99", 1, error_signature_before=None
                )

        self.assertEqual(message, "Invalid deal analysis: bad enum")
        self.assertEqual(missing, "exit code 1")

    def test_analysis_failure_ignores_unchanged_error_from_previous_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            error_path = root / "deal_42" / "analysis" / "deal_42_analysis_error.json"
            error_path.parent.mkdir(parents=True)
            error_path.write_text(
                json.dumps({"error": "Previous validation failure"}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch("run_rop_assistant.workspace_root", return_value=root):
                signature_before = analysis_error_file_signature("deal", "42")
                message = analysis_failure_error_message(
                    "deal", "42", 1, error_signature_before=signature_before
                )

        self.assertEqual(message, "exit code 1")

    def test_api_job_collects_partial_results_after_cli_failure(self) -> None:
        job_id = "partial-test"
        options = AnalyzeOptions(entity_type="lead", ids=["1", "2"], download_audio=False, transcribe_audio=False)
        _JOBS[job_id] = JobState(job_id=job_id, options=asdict(options))
        try:
            with (
                patch("api.jobs.resolve_entity_type", return_value="lead"),
                patch("api.jobs.build_cli_command", return_value=["rop"]),
                patch("api.jobs.run_command", side_effect=RuntimeError("batch failed")),
                patch("api.jobs._collect_group_results") as collect_results,
            ):
                _run_job(job_id)

            collect_results.assert_has_calls([call(_JOBS[job_id], "lead", ["1", "2"])])
            self.assertEqual(_JOBS[job_id].status, "error")
            self.assertTrue(any(stage["key"] == "collect_lead" for stage in _JOBS[job_id].stages))
        finally:
            _JOBS.pop(job_id, None)

    def test_deal_publish_error_does_not_hide_already_collected_result(self) -> None:
        from api.jobs import _publish_deal_result

        job = JobState(job_id="keep-published")
        job.results = [{"entity_type": "deal", "entity_id": "101", "report_id": 15}]
        job.report_ids = [15]
        job.entity_progress["deal:202"] = {
            "entity_type": "deal",
            "entity_id": "202",
            "publish_ready": True,
            "decision_status": "error",
            "status": "error",
            "stage": "error",
        }
        _publish_deal_result(job, "202", allow_raise=False)
        by_id = {item["entity_id"]: item for item in job.results}
        self.assertEqual(by_id["101"]["report_id"], 15)
        self.assertIsNone(by_id["202"]["report_id"])
        self.assertEqual(job.report_ids, [15])


if __name__ == "__main__":
    unittest.main()
