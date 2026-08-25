from __future__ import annotations

import unittest

from openai_api.audio.transcript_quality import is_meaningful_transcript
from openai_api.change_detection.snapshot import transcript_change_type


class TranscriptQualityTests(unittest.TestCase):
    def test_short_substantive_voice_is_meaningful(self) -> None:
        self.assertTrue(is_meaningful_transcript("Клиент согласовал КП, пришлите договор сегодня."))
        self.assertTrue(is_meaningful_transcript("Да, согласны, берём."))

    def test_empty_and_service_audio_are_not_meaningful(self) -> None:
        self.assertFalse(is_meaningful_transcript("Да, хорошо"))
        self.assertFalse(is_meaningful_transcript("Абонент временно недоступен, оставьте сообщение после звукового сигнала."))

    def test_only_meaningful_hash_change_triggers_paid_transcript_change(self) -> None:
        previous = {"content_hash": "old", "meaningful_content_hash": "same"}
        non_meaningful = {"content_hash": "new", "meaningful_content_hash": "same"}
        meaningful = {"content_hash": "newer", "meaningful_content_hash": "changed"}
        self.assertEqual(transcript_change_type(previous, non_meaningful), "transcript_changed_non_meaningful")
        self.assertEqual(transcript_change_type(previous, meaningful), "transcript_changed")

    def test_legacy_snapshot_uses_mtime_of_new_meaningful_item(self) -> None:
        previous = {"content_hash": "old", "mtime": 100.0}
        current = {
            "content_hash": "new",
            "meaningful_content_hash": "hash",
            "meaningful_items": [{"activity_id": "max_1", "mtime": 101.0}],
        }
        self.assertEqual(transcript_change_type(previous, current), "transcript_changed")


if __name__ == "__main__":
    unittest.main()
