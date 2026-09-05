from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import AsyncMock, MagicMock, patch

from api.deal_transcription import (
    AudioTranscriptionRequestError,
    MAX_AUDIO_DURATION_SECONDS,
    MAX_AUDIO_BYTES,
    _UPLOADED_AUDIO_JOBS,
    _UPLOADED_AUDIO_DEALS,
    _probe_duration_seconds,
    _run_uploaded_audio_job,
    get_uploaded_audio_job,
    start_uploaded_audio_job,
    transcribe_manager_voice,
    validate_audio_upload,
)


class FakeUpload:
    def __init__(self, data: bytes, *, content_type: str = "audio/webm", filename: str = "voice.webm") -> None:
        self.data = data
        self.content_type = content_type
        self.filename = filename
        self.size = len(data)
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        if not self.data:
            return b""
        result, self.data = self.data[:size], self.data[size:]
        return result

    async def close(self) -> None:
        self.closed = True


class DealTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _UPLOADED_AUDIO_JOBS.clear()
        _UPLOADED_AUDIO_DEALS.clear()

    def test_live_webm_duration_uses_packet_timeline_when_header_is_missing(self) -> None:
        format_probe = CompletedProcess(args=[], returncode=0, stdout=b"N/A\n", stderr=b"")
        packet_probe = CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                b'{"packets":['
                b'{"pts_time":"-0.007000","duration_time":"0.020000"},'
                b'{"pts_time":"2.993000","duration_time":"0.020000"}'
                b"]}"
            ),
            stderr=b"",
        )

        with patch("api.deal_transcription.subprocess.run", side_effect=[format_probe, packet_probe]) as run:
            duration = _probe_duration_seconds(b"live-webm")

        self.assertAlmostEqual(duration, 3.02)
        self.assertEqual(run.call_count, 2)
        self.assertIn("format=duration", run.call_args_list[0].args[0])
        self.assertIn("packet=pts_time,duration_time", run.call_args_list[1].args[0])

    def test_live_webm_packet_timeline_keeps_five_minute_limit(self) -> None:
        format_probe = CompletedProcess(args=[], returncode=0, stdout=b"N/A\n", stderr=b"")
        packet_probe = CompletedProcess(
            args=[],
            returncode=0,
            stdout=b'{"packets":[{"pts_time":"0","duration_time":"0.02"},{"pts_time":"300.01","duration_time":"0.02"}]}',
            stderr=b"",
        )

        with patch("api.deal_transcription.subprocess.run", side_effect=[format_probe, packet_probe]):
            with self.assertRaisesRegex(AudioTranscriptionRequestError, "5 минут"):
                _probe_duration_seconds(b"too-long-live-webm")

    async def test_multipart_recording_is_transcribed_in_memory(self) -> None:
        upload = FakeUpload(b"browser-audio")
        with patch("api.deal_transcription._probe_duration_seconds", return_value=42.0) as probe, \
             patch("api.deal_transcription.transcribe_voice", new=AsyncMock(return_value="распознанный текст")) as transcribe:
            result = await transcribe_manager_voice(audio=upload, deal_id="101", confirm_paid=True, language="ru")

        self.assertEqual(result, {"text": "распознанный текст"})
        probe.assert_called_once_with(b"browser-audio")
        transcribe.assert_awaited_once_with(
            b"browser-audio", file_name="manager_voice.webm", language="ru"
        )
        self.assertTrue(upload.closed)

    async def test_paid_action_type_size_language_and_duration_are_validated(self) -> None:
        with self.assertRaisesRegex(AudioTranscriptionRequestError, "платную"):
            validate_audio_upload(
                content_type="audio/webm", size_bytes=10, confirm_paid=False, language="ru"
            )
        with self.assertRaisesRegex(AudioTranscriptionRequestError, "тип"):
            validate_audio_upload(
                content_type="text/plain", size_bytes=10, confirm_paid=True, language="ru"
            )
        with self.assertRaisesRegex(AudioTranscriptionRequestError, "25 МБ"):
            validate_audio_upload(
                content_type="audio/webm", size_bytes=MAX_AUDIO_BYTES + 1, confirm_paid=True, language="ru"
            )
        with self.assertRaisesRegex(AudioTranscriptionRequestError, "только язык ru"):
            validate_audio_upload(
                content_type="audio/webm", size_bytes=10, confirm_paid=True, language="en"
            )
        upload = FakeUpload(b"audio")
        with patch("api.deal_transcription._probe_duration_seconds", side_effect=AudioTranscriptionRequestError("5 минут")), \
             patch("api.deal_transcription.transcribe_voice", new=AsyncMock()) as transcribe:
            with self.assertRaisesRegex(AudioTranscriptionRequestError, "5 минут"):
                await transcribe_manager_voice(audio=upload, deal_id="101", confirm_paid=True)
        transcribe.assert_not_awaited()
        self.assertTrue(upload.closed)
        self.assertEqual(MAX_AUDIO_DURATION_SECONDS, 300)

    async def test_audio_handler_does_not_log_bytes_or_transcript(self) -> None:
        response = MagicMock()
        response.text = "секретный transcript"
        create = AsyncMock(return_value=response)
        fake_client = MagicMock()
        fake_client.audio.transcriptions.create = create

        async def run_once(factory, **kwargs):
            return await factory()

        with patch("openai_api.audio.audio_handler.client", fake_client), \
             patch("openai_api.audio.audio_handler.run_with_retry_async", side_effect=run_once), \
             patch("openai_api.audio.audio_handler.logger") as logger:
            from openai_api.audio.audio_handler import transcribe_voice

            result = await transcribe_voice(b"secret-bytes", file_name="client-name.webm", language="ru")

        self.assertEqual(result, "секретный transcript")
        logged = " ".join(str(call) for call in logger.method_calls)
        self.assertNotIn("secret-bytes", logged)
        self.assertNotIn("секретный transcript", logged)
        self.assertNotIn("байт", logged)

    async def test_uploaded_recording_streams_to_temp_file_and_deletes_it(self) -> None:
        paths: list[str] = []

        async def transcribe(path: str, **kwargs):
            paths.append(path)
            self.assertEqual(kwargs["entity_type"], "deal")
            return "единая расшифровка"

        upload = FakeUpload(b"long-audio", content_type="audio/mpeg", filename="call.mp3")
        with patch("api.deal_transcription.get_audio_duration_seconds", return_value=35 * 60), \
             patch("api.deal_transcription.transcribe_file_async", side_effect=transcribe) as transcribe_mock, \
             patch("api.deal_transcription._start_uploaded_audio_thread") as start_thread, \
             patch("api.deal_transcription.record_transcription_spend") as outer_spend:
            started = await start_uploaded_audio_job(audio=upload, deal_id="101", confirm_paid=True)
            await asyncio.to_thread(_run_uploaded_audio_job, started["job_id"])
            result = get_uploaded_audio_job(started["job_id"])

        assert result is not None
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["attachment"]["source_kind"], "manual_audio")
        self.assertTrue(result["attachment"]["provisional"])
        self.assertEqual(result["attachment"]["transcript"], "единая расшифровка")
        self.assertEqual(transcribe_mock.await_count, 1)
        start_thread.assert_called_once_with(started["job_id"])
        outer_spend.assert_not_called()
        self.assertTrue(upload.closed)
        self.assertEqual(len(paths), 1)
        self.assertFalse(Path(paths[0]).exists())

    async def test_uploaded_recording_deletes_temp_file_after_transcription_error(self) -> None:
        paths: list[str] = []

        async def fail(path: str, **_kwargs):
            paths.append(path)
            raise RuntimeError("chunk failed")

        upload = FakeUpload(b"short-real-context", content_type="audio/mpeg", filename="eight-seconds.mp3")
        with patch("api.deal_transcription.get_audio_duration_seconds", return_value=8), \
             patch("api.deal_transcription.transcribe_file_async", side_effect=fail), \
             patch("api.deal_transcription._start_uploaded_audio_thread"):
            started = await start_uploaded_audio_job(audio=upload, deal_id="101", confirm_paid=True)
            await asyncio.to_thread(_run_uploaded_audio_job, started["job_id"])
            result = get_uploaded_audio_job(started["job_id"])

        assert result is not None
        self.assertEqual(result["status"], "error")
        self.assertNotIn("attachment", result)
        self.assertFalse(Path(paths[0]).exists())

    async def test_uploaded_recording_rejects_parallel_job_for_same_deal(self) -> None:
        _UPLOADED_AUDIO_DEALS.add("101")
        upload = FakeUpload(b"another-audio", content_type="audio/mpeg", filename="call.mp3")

        with self.assertRaisesRegex(AudioTranscriptionRequestError, "уже расшифровывается"):
            await start_uploaded_audio_job(audio=upload, deal_id="101", confirm_paid=True)

        self.assertEqual(upload.data, b"another-audio")


class DealTranscriptionRouteTests(unittest.TestCase):
    def test_frontend_route_is_present(self) -> None:
        from api.app import app

        route = next(route for route in app.routes if route.path == "/api/deal-control/voice/transcribe")
        self.assertEqual(route.methods, {"POST"})
        upload_route = next(route for route in app.routes if route.path == "/api/deal-control/audio/transcribe")
        job_route = next(route for route in app.routes if route.path == "/api/deal-control/audio/transcription-jobs/{job_id}")
        self.assertEqual(upload_route.methods, {"POST"})
        self.assertEqual(job_route.methods, {"GET"})

    def test_multipart_http_contract_passes_audio_and_deal_id(self) -> None:
        from fastapi.testclient import TestClient

        from api.app import app

        with patch(
            "api.app.transcribe_manager_voice",
            new=AsyncMock(return_value={"text": "текст из голоса"}),
        ) as transcribe, patch(
            "api.app.authenticate_request",
            return_value={
                "id": 1,
                "login": "admin",
                "role": "admin",
                "manager_id": None,
                "is_active": True,
            },
        ), patch(
            "api.app.require_deal",
            return_value=(None, {"deal_id": "101", "manager_id": "10"}),
        ):
            response = TestClient(app).post(
                "/api/deal-control/voice/transcribe",
                data={"deal_id": "101", "confirm_paid": "true", "language": "ru"},
                files={"audio": ("voice.webm", b"fake-webm", "audio/webm")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"text": "текст из голоса"})
        kwargs = transcribe.await_args.kwargs
        self.assertEqual(kwargs["deal_id"], "101")
        self.assertTrue(kwargs["confirm_paid"])
        self.assertEqual(kwargs["language"], "ru")
        self.assertEqual(kwargs["audio"].content_type, "audio/webm")

    def test_uploaded_audio_route_starts_background_job(self) -> None:
        from fastapi.testclient import TestClient

        from api.app import app

        queued = {"job_id": "audio-job", "deal_id": "101", "status": "queued"}
        with patch("api.app.start_uploaded_audio_job", new=AsyncMock(return_value=queued)) as start, \
             patch("api.app.authenticate_request", return_value={
                 "id": 1, "login": "admin", "role": "admin", "manager_id": None, "is_active": True,
             }), \
             patch("api.app.require_deal", return_value=(None, {"deal_id": "101", "manager_id": "10"})):
            response = TestClient(app).post(
                "/api/deal-control/audio/transcribe",
                data={"deal_id": "101", "confirm_paid": "true"},
                files={"audio": ("call.mp3", b"fake-mp3", "audio/mpeg")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), queued)
        self.assertEqual(start.await_args.kwargs["deal_id"], "101")
        self.assertTrue(start.await_args.kwargs["confirm_paid"])


if __name__ == "__main__":
    unittest.main()
