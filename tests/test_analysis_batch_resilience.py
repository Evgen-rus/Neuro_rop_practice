from __future__ import annotations

import subprocess
import unittest
from dataclasses import asdict
from unittest.mock import call, patch

from api.jobs import AnalyzeOptions, JobState, _JOBS, _run_job
from run_rop_assistant import WorkflowOptions, run_analysis


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


if __name__ == "__main__":
    unittest.main()
