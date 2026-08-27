from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from setup import MSK_TZ
from storage.rop_db import init_db, save_deal_control_scope, upsert_deal_control_deal


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=MSK_TZ)
DIAG_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bitrix_crm_cycle_diag.py"


class CombinedFakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.http_gets: list[str] = []

    def list_all(self, method, payload=None):
        self.calls.append((method, dict(payload or {})))
        if method == "crm.deal.list":
            deal_filter = (payload or {}).get("filter") or {}
            if deal_filter.get("ID"):
                return [{
                    "ID": "101",
                    "TITLE": "Сделка",
                    "ASSIGNED_BY_ID": "10",
                    "CATEGORY_ID": "15",
                    "STAGE_ID": "NEW",
                    "CLOSED": "N",
                    "OPPORTUNITY": "100",
                    "CURRENCY_ID": "RUB",
                    "DATE_CREATE": NOW.isoformat(),
                    "DATE_MODIFY": NOW.isoformat(),
                }]
            return [{
                "ID": "101",
                "TITLE": "Сделка",
                "ASSIGNED_BY_ID": "10",
                "CATEGORY_ID": "15",
                "STAGE_ID": "NEW",
                "CLOSED": "N",
                "OPPORTUNITY": "100",
                "CURRENCY_ID": "RUB",
                "DATE_CREATE": NOW.isoformat(),
                "DATE_MODIFY": NOW.isoformat(),
            }]
        if method == "user.get":
            return [{"ID": "10", "LAST_NAME": "Иванов", "NAME": "Иван"}]
        raise AssertionError(method)

    def safe_list_all(self, method, payload=None):
        self.calls.append((method, dict(payload or {})))
        if method == "crm.activity.list":
            return {"ok": True, "items": []}
        if method == "crm.deal.list":
            return {"ok": True, "items": []}
        if method == "crm.lead.list":
            return {"ok": True, "items": []}
        if method == "user.get":
            return {"ok": True, "items": [{"ID": "10", "NAME": "Иван", "LAST_NAME": "Иванов", "IS_ONLINE": "N"}]}
        if method in {"crm.timeline.comment.list", "crm.stagehistory.list", "task.ctasklogitem.list"}:
            return {"ok": True, "items": []}
        raise AssertionError(method)

    def safe_call(self, method, payload=None):
        self.calls.append((method, dict(payload or {})))
        if method == "disk.file.get":
            return {"ok": True, "response": {"result": {"SIZE": 128, "DOWNLOAD_URL": "https://example.test/secret.mp3"}}}
        raise AssertionError(method)


class BitrixCrmCycleDiagTests(unittest.TestCase):
    def test_source_does_not_import_openai_or_jobs(self) -> None:
        source = DIAG_PATH.read_text(encoding="utf-8")
        self.assertNotIn("openai_api", source)
        self.assertNotIn("api.jobs", source)
        self.assertNotIn("transcribe", source)
        self.assertNotIn("analyze_deal", source)

    def test_two_runs_without_sample_do_not_call_openai_or_download(self) -> None:
        from scripts.bitrix_crm_cycle_diag import run_diagnostic

        client = CombinedFakeClient()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "rop.sqlite"
            usage_dir = root / "usage"
            init_db(db_path)
            save_deal_control_scope(
                db_path,
                initial_deal_ids=["101"],
                manager_ids=["10"],
                pipeline_id="15",
            )
            upsert_deal_control_deal(
                db_path,
                deal_id="101",
                source="initial",
                title="Сделка",
                manager_id="10",
                manager_name="Менеджер",
                stage_id="NEW",
                stage_name="Новая",
                pipeline_id="15",
                amount="100",
                currency_id="RUB",
                created_at_crm=NOW.isoformat(),
                modified_at_crm=NOW.isoformat(),
                is_active=True,
            )
            with patch("scripts.bitrix_crm_cycle_diag.make_client", side_effect=AssertionError("make_client")):
                summary = run_diagnostic(
                    db_path=db_path,
                    client=client,
                    usage_dir=usage_dir,
                    skip_sample=True,
                    now=NOW,
                )

        self.assertEqual(summary["run1"]["cycle"]["run_id"].endswith("_run1"), True)
        self.assertEqual(summary["run2"]["cycle"]["run_id"].endswith("_run2"), True)
        self.assertIsNone(summary["sample"])
        self.assertNotIn("disk.file.get", [method for method, _payload in client.calls])
        self.assertEqual(client.http_gets, [])
        methods = {method for method, _payload in client.calls}
        self.assertIn("crm.deal.list", methods)
        self.assertIn("crm.activity.list", methods)
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("101", serialized)
        self.assertIn("summary.json", summary["summary_path"])

    def test_audio_metadata_uses_disk_file_get_without_downloading_bytes(self) -> None:
        from scripts.bitrix_crm_cycle_diag import inspect_audio_metadata

        client = CombinedFakeClient()
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            audio_dir = Path(directory) / "audio"
            raw_dir.mkdir()
            audio_dir.mkdir()
            (raw_dir / "deal_101_context.json").write_text(
                json.dumps(
                    {
                        "deal_id": "101",
                        "activities": {
                            "items": [
                                {
                                    "ID": "11",
                                    "TYPE_ID": "2",
                                    "PROVIDER_ID": "VOXIMPLANT_CALL",
                                    "START_TIME": NOW.isoformat(),
                                    "END_TIME": NOW.isoformat(),
                                    "FILES": [{"id": "file-7"}],
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch("requests.get", side_effect=AssertionError("audio bytes download")):
                counts = inspect_audio_metadata(
                    client,
                    ["101"],
                    raw_dir=raw_dir,
                    audio_dir=audio_dir,
                    run_id="sample-audio",
                )
        self.assertEqual(counts["call_activities"], 1)
        self.assertEqual(counts["disk_file_get"], 1)
        self.assertEqual(counts["recheck_candidates"], 1)
        self.assertEqual(client.calls, [("disk.file.get", {"id": "file-7"})])


if __name__ == "__main__":
    unittest.main()
