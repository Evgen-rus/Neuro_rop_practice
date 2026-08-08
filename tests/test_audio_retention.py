from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from bitrix.deals.download_deals_call_audio import (
    audio_file_discovery_expired,
    existing_transcriptions_by_activity,
    process_call,
    record_transcribed_and_purged,
)
from bitrix.leads.download_leads_call_audio import build_manifest as build_lead_audio_manifest
from setup import MSK_TZ


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.retry_callback = None

    def safe_call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        return {
            "ok": True,
            "response": {"result": {"DOWNLOAD_URL": "https://example.invalid/audio.mp3"}},
        }


class AudioRetentionTests(unittest.TestCase):
    def test_recorded_purge_keeps_missing_only_from_downloading_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "activity_42.mp3"
            transcript_path = root / "call_42_transcript.json"
            manifest_path = root / "deal_1_call_audio_manifest.json"
            audio_path.write_bytes(b"audio")
            transcript_path.write_text("{}", encoding="utf-8")
            manifest_path.write_text(
                json.dumps(
                    {
                        "calls": [
                            {
                                "activity_id": "42",
                                "downloads": [{"ok": True, "local_path": str(audio_path), "status": "downloaded"}],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            audio_path.unlink()
            marked = record_transcribed_and_purged(
                manifest_path,
                audio_path,
                "42",
                {"txt_path": "transcript.txt", "md_path": "transcript.md", "json_path": str(transcript_path)},
            )

            self.assertTrue(marked)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            transcription = existing_transcriptions_by_activity(manifest)["42"]
            result = process_call(
                client=None,
                deal_audio_dir=root,
                activity={"ID": "42", "FILES": []},
                existing_transcription=transcription,
                missing_only=True,
            )
            self.assertEqual(result["status"], "transcribed_and_purged")
            self.assertEqual(result["downloads"], [])

    def test_missing_transcript_bundle_does_not_suppress_redownload(self) -> None:
        manifest = {
            "calls": [
                {
                    "activity_id": "42",
                    "transcription": {
                        "status": "transcribed_and_purged",
                        "transcript_json_path": "C:/does-not-exist/transcript.json",
                    },
                }
            ]
        }
        self.assertEqual(existing_transcriptions_by_activity(manifest), {})

    def test_empty_files_never_call_bitrix_audio_methods(self) -> None:
        client = RecordingClient()
        result = process_call(
            client=client,
            deal_audio_dir=Path("unused"),
            activity={"ID": "42", "START_TIME": "2999-08-08T10:00:00+03:00", "FILES": []},
            missing_only=True,
        )

        self.assertEqual(result["status"], "no_files_in_crm_activity")
        self.assertEqual(client.calls, [])
        self.assertNotIn("voximplant_attempts", result)
        self.assertNotIn("call_id_candidates", result)

    def test_empty_files_expire_after_five_days_without_api_calls(self) -> None:
        client = RecordingClient()
        activity = {"ID": "42", "START_TIME": "2026-08-01T09:00:00+03:00", "FILES": []}

        self.assertTrue(
            audio_file_discovery_expired(
                activity,
                now=datetime(2026, 8, 6, 9, 0, 1, tzinfo=MSK_TZ),
            )
        )
        result = process_call(client=client, deal_audio_dir=Path("unused"), activity=activity, missing_only=True)

        self.assertEqual(result["status"], "no_files_check_expired")
        self.assertEqual(result["audio_file_discovery_window_days"], 5)
        self.assertEqual(client.calls, [])

    def test_old_activity_with_file_still_uses_only_disk_file_get(self) -> None:
        client = RecordingClient()
        activity = {
            "ID": "42",
            "START_TIME": "2020-01-01T09:00:00+03:00",
            "FILES": [{"id": "777", "url": "https://example.invalid/direct.mp3"}],
        }
        downloaded = {
            "ok": True,
            "status": "downloaded",
            "local_path": "audio.mp3",
            "size_bytes": 10,
        }

        with patch("bitrix.deals.download_deals_call_audio.try_download_url", return_value=downloaded) as download:
            result = process_call(client=client, deal_audio_dir=Path("unused"), activity=activity, missing_only=True)

        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(client.calls, [("disk.file.get", {"id": "777"})])
        self.assertEqual(result["downloads"][0]["source"], "disk.file.get")
        download.assert_called_once_with(
            "https://example.invalid/audio.mp3",
            Path("unused"),
            "activity_42_file_777",
            None,
        )

    def test_lead_manifest_uses_shared_no_files_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_path = root / "lead_1_context.json"
            raw_path.write_text(
                json.dumps(
                    {
                        "lead_id": "1",
                        "activities": {
                            "items": [
                                {
                                    "ID": "42",
                                    "TYPE_ID": "2",
                                    "START_TIME": "2020-01-01T09:00:00+03:00",
                                    "FILES": [],
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            client = RecordingClient()

            manifest = build_lead_audio_manifest(
                client=client,
                lead_id="1",
                raw_path=raw_path,
                lead_audio_dir=root / "audio",
                existing_manifest={},
                missing_only=True,
            )

        self.assertEqual(manifest["calls"][0]["status"], "no_files_check_expired")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
