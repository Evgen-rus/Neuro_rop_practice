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


class ReportMarkdownApiTests(unittest.TestCase):
    def test_admin_and_rop_read_markdown_without_filesystem_path(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        md_path = tmp / "deal_101_rop_report.md"
        md_path.write_text("# Отчёт по сделке\n", encoding="utf-8")

        for role, user in (
            ("admin", _user("admin")),
            ("rop", _user("rop", manager_id="10")),
        ):
            with self.subTest(role=role):
                with (
                    patch.object(api_app, "authenticate_request", return_value=user),
                    patch.object(api_app, "require_report", return_value=_report()),
                    patch.object(api_app, "_report_markdown_path", return_value=md_path),
                ):
                    response = TestClient(api_app.app).get("/api/reports/42/markdown")

                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["report_id"], 42)
                self.assertEqual(payload["markdown"], "# Отчёт по сделке\n")
                self.assertNotIn("path", payload)
                self.assertNotIn(str(md_path), str(payload))

    def test_markdown_404_when_file_is_missing(self) -> None:
        missing = Path(tempfile.mkdtemp()) / "missing.md"
        with (
            patch.object(api_app, "authenticate_request", return_value=_user("rop", manager_id="10")),
            patch.object(api_app, "require_report", return_value=_report()),
            patch.object(api_app, "_report_markdown_path", return_value=missing),
        ):
            response = TestClient(api_app.app).get("/api/reports/42/markdown")

        self.assertEqual(response.status_code, 404)
