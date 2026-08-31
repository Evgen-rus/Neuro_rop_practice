from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import app as api_app


def _user(role: str, *, manager_id: str | None = None, user_id: int = 1) -> dict[str, object]:
    return {
        "id": user_id,
        "login": f"{role}-{user_id}",
        "role": role,
        "manager_id": manager_id,
        "is_active": True,
    }


def _report() -> dict[str, object]:
    return {"id": 42, "entity_type": "deal", "entity_id": "101"}


class AnalysisTraceApiTests(unittest.TestCase):
    def test_admin_reads_latest_workspace_files_without_paths(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        prompt = tmp / "request_prompt.txt"
        raw = tmp / "raw_output.txt"
        prompt.write_text("PROMPT STUB", encoding="utf-8")
        raw.write_text("RAW STUB", encoding="utf-8")
        admin = _user("admin")

        with (
            patch.object(api_app, "authenticate_request", return_value=admin),
            patch.object(api_app, "require_report", return_value=_report()),
            patch.object(
                api_app,
                "analysis_paths",
                return_value={"request_prompt": prompt, "raw_output": raw},
            ),
        ):
            response = TestClient(api_app.app).get("/api/reports/42/analysis-trace")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["report_id"], 42)
        self.assertEqual(payload["entity_type"], "deal")
        self.assertEqual(payload["entity_id"], "101")
        self.assertTrue(payload["request_prompt_available"])
        self.assertTrue(payload["raw_output_available"])
        self.assertEqual(payload["request_prompt"], "PROMPT STUB")
        self.assertEqual(payload["raw_output"], "RAW STUB")
        self.assertNotIn("path", payload)
        self.assertNotIn("request_prompt_path", payload)
        self.assertNotIn("raw_output_path", payload)
        serialized = str(payload)
        self.assertNotIn(str(prompt), serialized)
        self.assertNotIn(str(raw), serialized)

    def test_admin_gets_one_file_when_the_other_is_missing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        prompt = tmp / "request_prompt.txt"
        missing = tmp / "raw_output.txt"
        prompt.write_text("PROMPT STUB", encoding="utf-8")
        admin = _user("admin")

        with (
            patch.object(api_app, "authenticate_request", return_value=admin),
            patch.object(api_app, "require_report", return_value=_report()),
            patch.object(
                api_app,
                "analysis_paths",
                return_value={"request_prompt": prompt, "raw_output": missing},
            ),
        ):
            response = TestClient(api_app.app).get("/api/reports/42/analysis-trace")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["request_prompt_available"])
        self.assertFalse(payload["raw_output_available"])
        self.assertEqual(payload["request_prompt"], "PROMPT STUB")
        self.assertIsNone(payload["raw_output"])

    def test_admin_gets_404_when_both_files_are_missing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        admin = _user("admin")

        with (
            patch.object(api_app, "authenticate_request", return_value=admin),
            patch.object(api_app, "require_report", return_value=_report()),
            patch.object(
                api_app,
                "analysis_paths",
                return_value={
                    "request_prompt": tmp / "missing_prompt.txt",
                    "raw_output": tmp / "missing_raw.txt",
                },
            ),
        ):
            response = TestClient(api_app.app).get("/api/reports/42/analysis-trace")

        self.assertEqual(response.status_code, 404)

    def test_rop_cannot_read_analysis_trace(self) -> None:
        with patch.object(api_app, "authenticate_request", return_value=_user("rop", manager_id="10")):
            response = TestClient(api_app.app).get("/api/reports/42/analysis-trace")

        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_read_analysis_trace(self) -> None:
        with patch.object(api_app, "authenticate_request", return_value=_user("manager", manager_id="10")):
            response = TestClient(api_app.app).get("/api/reports/42/analysis-trace")

        self.assertEqual(response.status_code, 403)
