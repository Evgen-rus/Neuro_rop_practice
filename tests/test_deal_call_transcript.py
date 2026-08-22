from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api import app as app_api
from api import deal_call_transcript as transcript_api


CALL_EVENT = {
    "event_id": "crm_activity:77",
    "source_ids": ["77"],
    "channel": "call",
}


class DealCallTranscriptTests(unittest.TestCase):
    def test_finds_saved_lead_transcript_by_verified_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lead_dir = Path(directory)
            transcripts = lead_dir / "transcripts"
            transcripts.mkdir()
            (transcripts / "call_88_transcript.txt").write_text(
                "Расшифровка звонка по лиду", encoding="utf-8",
            )
            with patch.object(transcript_api, "entity_workspace_dir", return_value=lead_dir):
                result = transcript_api.find_call_transcript("lead", "202", "88")
        self.assertEqual(result["text"], "Расшифровка звонка по лиду")

    def test_reads_full_json_transcript_for_deal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            deal_dir = Path(directory)
            transcripts = deal_dir / "transcripts"
            transcripts.mkdir()
            text = "Менеджер: Добрый день.\nКлиент: КП получил."
            (transcripts / "call_77_client_transcript.json").write_text(
                json.dumps({"text": text}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch.object(transcript_api, "_load_local_communications", return_value=[CALL_EVENT]), \
                 patch.object(transcript_api, "deal_workspace_dir", return_value=deal_dir):
                result = transcript_api.get_deal_call_transcript("101", "crm_activity:77")
        self.assertEqual(result["text"], text)
        self.assertFalse(result["truncated"])

    def test_rejects_event_that_does_not_belong_to_deal(self) -> None:
        with patch.object(transcript_api, "_load_local_communications", return_value=[]):
            with self.assertRaisesRegex(
                transcript_api.DealCallTranscriptNotFound,
                "истории этой сделки",
            ):
                transcript_api.get_deal_call_transcript("101", "crm_activity:77")

    def test_reports_missing_transcript_without_exposing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(transcript_api, "_load_local_communications", return_value=[CALL_EVENT]), \
                 patch.object(transcript_api, "deal_workspace_dir", return_value=Path(directory)):
                with self.assertRaisesRegex(
                    transcript_api.DealCallTranscriptNotFound,
                    "пока недоступна",
                ):
                    transcript_api.get_deal_call_transcript("101", "crm_activity:77")

    def test_http_endpoint_checks_deal_scope_before_reading(self) -> None:
        payload = {
            "deal_id": "101",
            "event_id": "crm_activity:77",
            "text": "Текст",
            "truncated": False,
        }
        with patch.object(app_api, "require_deal") as require, \
             patch.object(app_api, "get_deal_call_transcript", return_value=payload) as read:
            result = app_api.deal_call_transcript_get("101", "crm_activity:77")
        self.assertEqual(result, payload)
        require.assert_called_once_with("101", action="open")
        read.assert_called_once_with("101", "crm_activity:77")

    def test_http_endpoint_maps_missing_transcript_to_404(self) -> None:
        with patch.object(app_api, "require_deal"), patch.object(
            app_api,
            "get_deal_call_transcript",
            side_effect=transcript_api.DealCallTranscriptNotFound("Нет расшифровки"),
        ):
            with self.assertRaises(HTTPException) as error:
                app_api.deal_call_transcript_get("101", "crm_activity:77")
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
