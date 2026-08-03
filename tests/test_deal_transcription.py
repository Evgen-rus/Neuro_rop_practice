from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from api.deal_transcription import (
    AudioTranscriptionRequestError,
    MAX_AUDIO_DURATION_SECONDS,
    MAX_AUDIO_BYTES,
    transcribe_manager_voice,
    validate_audio_upload,
)


class FakeUpload:
    def __init__(self, data: bytes, *, content_type: str = "audio/webm") -> None:
        self.data = data
        self.content_type = content_type
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


class DealTranscriptionRouteTests(unittest.TestCase):
    def test_frontend_route_is_present(self) -> None:
        from api.app import app

        route = next(route for route in app.routes if route.path == "/api/deal-control/voice/transcribe")
        self.assertEqual(route.methods, {"POST"})

    def test_multipart_http_contract_passes_audio_and_deal_id(self) -> None:
        from fastapi.testclient import TestClient

        from api.app import app

        with patch(
            "api.app.transcribe_manager_voice",
            new=AsyncMock(return_value={"text": "текст из голоса"}),
        ) as transcribe:
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


if __name__ == "__main__":
    unittest.main()
