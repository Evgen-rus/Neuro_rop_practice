from __future__ import annotations

import unittest

from openai_api.llm.deal_manager_situation import build_situation_prompt


class DealManagerAudioContextTests(unittest.TestCase):
    def test_manual_audio_is_explicitly_provisional_and_separate(self) -> None:
        prompt = build_situation_prompt(
            analysis_projection={},
            deal={},
            current_bitrix_task=None,
            previous_manager_projection={},
            manager_context="",
            manual_audio_attachment={
                "source_kind": "manual_audio",
                "provisional": True,
                "transcript": "Клиент обсуждает параметры оборудования",
            },
        )

        self.assertIn("MANUAL_AUDIO_CONTEXT", prompt)
        self.assertIn("Клиент обсуждает параметры оборудования", prompt)
        self.assertIn("не подтверждён CRM", prompt)
        self.assertIn("не считай его новым звонком", prompt)


if __name__ == "__main__":
    unittest.main()
