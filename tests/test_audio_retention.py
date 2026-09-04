from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from bitrix.deals.download_deals_call_audio import (
    audio_file_discovery_expired,
    client_day_related_call_activities,
    existing_transcriptions_by_activity,
    max_voice_messages,
    max_voice_urls,
    process_call,
    process_max_voice,
    recording_readiness,
    record_transcribed_and_purged,
    refresh_missing_call_files,
    should_recheck_recording,
)
from bitrix.leads.download_leads_call_audio import build_manifest as build_lead_audio_manifest
from setup import MSK_TZ


class RecordingClient:
    def __init__(self, *, size: int | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.retry_callback = None
        self.size = size

    def safe_call(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        result = {"DOWNLOAD_URL": "https://example.invalid/audio.mp3"}
        if self.size is not None:
            result["SIZE"] = str(self.size)
        return {
            "ok": True,
            "response": {"result": result},
        }


class AudioRetentionTests(unittest.TestCase):
    def test_max_voice_extraction_keeps_client_and_manager_messages_in_30_day_window(self) -> None:
        icon = "https://static.wazzup24.com/images/bitrix/max.png"
        client_voice = "https://store.wazzup24.com/client-voice"
        manager_voice = "https://store.wazzup24.com/manager-voice"
        bundle = {
            "normalized_communications": [
                {
                    "channel": "max",
                    "source_ids": ["100"],
                    "occurred_at": "2026-08-24T10:00:00+03:00",
                    "entity_type": "deal",
                    "entity_id": "42",
                    "direction": "incoming",
                    "participant_role": "client",
                    "participant_name": "Клиент",
                },
                {
                    "channel": "max",
                    "source_ids": ["101"],
                    "occurred_at": "2026-08-23T10:00:00+03:00",
                    "entity_type": "deal",
                    "entity_id": "42",
                    "direction": "unknown",
                    "participant_role": "unknown",
                    "participant_name": "Менеджер",
                },
                {
                    "channel": "max",
                    "source_ids": ["old"],
                    "occurred_at": "2026-07-01T10:00:00+03:00",
                    "entity_type": "deal",
                    "entity_id": "42",
                },
            ],
            "internal_context": [
                {"id": "100", "raw": {"COMMENT": f"[img]{icon}[/img] [url={client_voice}]voice[/url]"}},
                {"id": "101", "raw": {"COMMENT": f"[img]{icon}[/img] {manager_voice} {manager_voice}"}},
                {"id": "old", "raw": {"COMMENT": "https://store.wazzup24.com/old"}},
            ],
        }

        messages = max_voice_messages(
            bundle,
            now=datetime(2026, 8, 25, 12, 0, tzinfo=MSK_TZ),
            lookback_days=30,
        )

        self.assertEqual(len(messages), 2)
        self.assertEqual({item["direction"] for item in messages}, {"incoming", "unknown"})
        self.assertTrue(all(item["activity_id"].startswith("max_") for item in messages))
        self.assertEqual(max_voice_urls(f"{icon} {client_voice}"), [client_voice])

    def test_max_voice_download_is_manifest_managed_and_always_transcribable(self) -> None:
        downloaded = {
            "ok": True,
            "status": "downloaded",
            "local_path": "voice.mp3",
            "duration_seconds": 8.0,
            "is_short_no_answer": True,
            "skip_transcribe": True,
        }
        message = {
            "activity_id": "max_100_abc",
            "timeline_comment_id": "100",
            "entity_id": "42",
            "start_time": "2026-08-24T10:00:00+03:00",
            "direction": "incoming",
            "url": "https://store.wazzup24.com/voice",
            "url_fingerprint": "abc",
        }
        with patch("bitrix.deals.download_deals_call_audio.try_download_url", return_value=downloaded):
            result = process_max_voice(Path("unused"), message, missing_only=True)

        self.assertEqual(result["audio_kind"], "max_voice")
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["downloads"][0]["recording_ready_for_transcription"])
        self.assertFalse(result["downloads"][0]["skip_transcribe"])

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
                                "downloads": [
                                    {
                                        "ok": True,
                                        "file_id": "777",
                                        "local_path": str(audio_path),
                                        "status": "downloaded",
                                        "size_bytes": 5,
                                        "remote_size_bytes": 5,
                                        "duration_seconds": 25.0,
                                    }
                                ],
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
            self.assertEqual(transcription["source_file_id"], "777")
            self.assertEqual(transcription["source_remote_size_bytes"], 5)
            self.assertEqual(transcription["source_duration_seconds"], 25.0)
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

    def test_recording_shorter_than_activity_waits_for_growth(self) -> None:
        activity = {
            "START_TIME": "2026-08-20T10:00:00+03:00",
            "END_TIME": "2026-08-20T10:02:00+03:00",
        }
        result = recording_readiness(
            {"ok": True, "duration_seconds": 25.0, "size_bytes": 50},
            activity,
            remote_size_bytes=50,
            size_changed=True,
        )

        self.assertFalse(result["recording_ready_for_transcription"])
        self.assertEqual(result["recording_stability_status"], "duration_incomplete")
        self.assertEqual(result["expected_call_duration_seconds"], 120.0)

    def test_unknown_call_duration_requires_two_equal_remote_size_observations(self) -> None:
        activity = {"START_TIME": "2026-08-20T10:00:00+03:00"}
        first = recording_readiness(
            {"ok": True, "duration_seconds": 25.0, "size_bytes": 50},
            activity,
            remote_size_bytes=50,
            size_changed=True,
        )
        second = recording_readiness(
            first,
            activity,
            previous=first,
            remote_size_bytes=50,
            size_changed=False,
        )

        self.assertFalse(first["recording_ready_for_transcription"])
        self.assertEqual(first["recording_stable_observations"], 1)
        self.assertTrue(second["recording_ready_for_transcription"])
        self.assertEqual(second["recording_stability_status"], "size_stable_twice")

    def test_grown_recording_is_replaced_and_becomes_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "call.mp3"
            audio_path.write_bytes(b"x" * 50)
            previous = {
                "ok": True,
                "file_id": "777",
                "local_path": str(audio_path),
                "size_bytes": 50,
                "remote_size_bytes": 50,
                "duration_seconds": 25.0,
                "recording_stable_observations": 1,
            }
            activity = {
                "ID": "42",
                "START_TIME": "2026-08-20T10:00:00+03:00",
                "END_TIME": "2026-08-20T10:02:00+03:00",
                "FILES": [{"id": "777"}],
            }
            refreshed = {
                "ok": True,
                "status": "redownloaded_grown_file",
                "local_path": str(audio_path),
                "size_bytes": 200,
                "duration_seconds": 120.0,
            }

            with (
                patch("bitrix.deals.download_deals_call_audio.should_recheck_recording", return_value=True),
                patch("bitrix.deals.download_deals_call_audio.try_download_url", return_value=refreshed) as download,
            ):
                result = process_call(
                    client=RecordingClient(size=200),
                    deal_audio_dir=root,
                    activity=activity,
                    existing_downloads=[previous],
                    missing_only=True,
                )

        self.assertEqual(result["status"], "redownloaded_grown_file")
        self.assertTrue(result["downloads"][0]["recording_ready_for_transcription"])
        self.assertEqual(result["downloads"][0]["duration_seconds"], 120.0)
        self.assertTrue(download.call_args.kwargs["replace_existing"])

    def test_transcribed_recording_growth_marks_transcript_stale(self) -> None:
        activity = {
            "ID": "42",
            "START_TIME": "2026-08-20T10:00:00+03:00",
            "END_TIME": "2026-08-20T10:02:00+03:00",
            "FILES": [{"id": "777"}],
        }
        transcription = {
            "status": "transcribed_and_purged",
            "source_file_id": "777",
            "source_remote_size_bytes": 50,
        }
        refreshed = {
            "ok": True,
            "status": "downloaded",
            "local_path": "audio.mp3",
            "size_bytes": 200,
            "duration_seconds": 120.0,
        }

        with (
            patch("bitrix.deals.download_deals_call_audio.should_recheck_recording", return_value=True),
            patch("bitrix.deals.download_deals_call_audio.try_download_url", return_value=refreshed),
        ):
            result = process_call(
                client=RecordingClient(size=200),
                deal_audio_dir=Path("unused"),
                activity=activity,
                existing_transcription=transcription,
                missing_only=True,
            )

        self.assertEqual(result["status"], "recording_refreshed_transcript_stale")
        self.assertEqual(result["transcription"]["status"], "stale_source_grew")
        self.assertTrue(result["downloads"][0]["recording_ready_for_transcription"])

    def test_recheck_window_is_today_plus_first_morning_for_evening_call(self) -> None:
        today = {"START_TIME": "2026-08-20T09:00:00+03:00"}
        yesterday_morning = {"START_TIME": "2026-08-19T09:00:00+03:00"}
        friday_evening = {"START_TIME": "2026-08-21T17:45:00+03:00"}

        self.assertTrue(should_recheck_recording(today, now=datetime(2026, 8, 20, 12, 0, tzinfo=MSK_TZ)))
        self.assertFalse(should_recheck_recording(yesterday_morning, now=datetime(2026, 8, 20, 8, 0, tzinfo=MSK_TZ)))
        self.assertTrue(should_recheck_recording(friday_evening, now=datetime(2026, 8, 24, 8, 15, tzinfo=MSK_TZ)))
        self.assertFalse(should_recheck_recording(friday_evening, now=datetime(2026, 8, 24, 9, 0, tzinfo=MSK_TZ)))

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

    def test_related_client_day_call_uses_raw_recheck_and_skips_old_calls(self) -> None:
        from datetime import timedelta

        from tests.test_deal_control import related_history_bundle

        now = datetime.now(MSK_TZ).astimezone(MSK_TZ)
        started = now.replace(microsecond=0) - timedelta(hours=1)
        today = {
            "ID": "662009", "OWNER_ID": "2", "OWNER_TYPE_ID": "2", "TYPE_ID": "2",
            "PROVIDER_ID": "VOXIMPLANT_CALL", "DIRECTION": "2", "COMPLETED": "Y",
            "START_TIME": started.isoformat(timespec="seconds"),
            "END_TIME": (started + timedelta(minutes=4)).isoformat(timespec="seconds"),
            "FILES": [],
        }
        old = {
            "ID": "old-call", "OWNER_ID": "2", "OWNER_TYPE_ID": "2", "TYPE_ID": "2",
            "PROVIDER_ID": "VOXIMPLANT_CALL", "DIRECTION": "2", "COMPLETED": "Y",
            "START_TIME": (now - timedelta(days=20)).isoformat(timespec="seconds"),
            "FILES": [{"id": "9"}],
        }
        bundle = related_history_bundle("2", today, old)
        related = client_day_related_call_activities(bundle, deal_id="1", now=now)
        self.assertEqual([item["ID"] for item in related], ["662009"])
        self.assertEqual(related[0]["_source"], "related_deal")
        self.assertEqual(related[0]["_owner_id"], "2")

        class DiscoveryClient:
            retry_callback = None

            def safe_call(self, method, params):
                self.method = method
                self.params = params
                return {
                    "ok": True,
                    "response": {"result": {**today, "FILES": [{"id": "88", "ID": "88"}]}},
                }

        extras = client_day_related_call_activities(bundle, deal_id="1", now=now)
        client = DiscoveryClient()
        refresh_missing_call_files(
            client,
            {"deal_id": "1", "activities": {"items": []}},
            extra_activities=extras,
        )
        self.assertEqual(client.method, "crm.activity.get")
        self.assertEqual(client.params, {"id": "662009"})
        self.assertEqual(extras[0]["FILES"], [{"id": "88", "ID": "88"}])

        downloaded = {
            "ok": True,
            "status": "downloaded",
            "local_path": "audio.mp3",
            "size_bytes": 10,
        }
        with patch("bitrix.deals.download_deals_call_audio.try_download_url", return_value=downloaded):
            processed = process_call(
                client=RecordingClient(),
                deal_audio_dir=Path("unused"),
                activity=extras[0],
                missing_only=True,
            )
        self.assertEqual(processed["source"], "related_deal")
        self.assertEqual(processed["source_label"], "related_deal:2")
        self.assertEqual(processed["owner_id"], "2")

        from api.crm_change_gate import audio_due

        with_files = related_history_bundle("2", {**today, "FILES": [{"id": "88"}]}, old)
        self.assertTrue(audio_due(
            {"context": {"deal_id": "1", "activities": {"items": []}}, "customer_history": with_files},
            "1",
            now,
        ))
        old_only = related_history_bundle("2", old)
        self.assertFalse(audio_due(
            {"context": {"deal_id": "1", "activities": {"items": []}}, "customer_history": old_only},
            "1",
            now,
        ))


if __name__ == "__main__":
    unittest.main()
