from __future__ import annotations

import unittest

from openai_api.llm.analyze_deal import (
    HISTORY_SECTION_MARKER,
    build_prompt,
    deal_prompt_cache_markers,
)
from openai_api.llm.deal_call_projection import project_transcript_for_deal_prompt
from openai_api.llm.llm_client import prompt_prefix_before


class DealCallProjectionTests(unittest.TestCase):
    def test_projection_removes_only_local_transcript_paths(self) -> None:
        transcript = """## Тексты звонков

### Звонок 1: activity_id=10

- Дата звонка: 2026-08-01T10:00:00+03:00
- Источник: deal:7
- Subject / CRM label: Bitrix deal:7 activity_id=10; Исходящий звонок
- Длительность: 1:25
- Предварительный сигнал: есть речь/диалог, проверить по тексту
- Transcript JSON: `D:\\private\\call.json`
- Transcript MD: `D:\\private\\call.md`

```text
Клиент подтвердил срок.
```
"""

        projected = project_transcript_for_deal_prompt(transcript, deal_id="7")

        self.assertNotIn("Transcript JSON", projected)
        self.assertNotIn("Transcript MD", projected)
        self.assertNotIn("D:\\private", projected)
        self.assertIn("- Источник: deal:7", projected)
        self.assertIn("### Звонок 1: activity_id=10", projected)
        self.assertIn("- Дата звонка: 2026-08-01T10:00:00+03:00", projected)
        self.assertIn("- Длительность: 1:25", projected)
        self.assertIn("- Предварительный сигнал:", projected)
        self.assertIn("Клиент подтвердил срок.", projected)

    def test_projection_keeps_append_only_cache_boundary(self) -> None:
        first_call = """## Тексты звонков

### Звонок 1: activity_id=10

- Transcript JSON: `D:\\private\\one.json`

```text
Первый разговор.
```
"""
        second_call = """
### Звонок 2: activity_id=11

- Transcript JSON: `D:\\private\\two.json`

```text
Второй разговор.
```
"""
        old_text = project_transcript_for_deal_prompt(first_call, deal_id="7")
        new_text = project_transcript_for_deal_prompt(first_call + second_call, deal_id="7")
        old_prompt = build_prompt("7", "history", old_text, "diagnostics", [], {})
        new_prompt = build_prompt("7", "history changed", new_text, "diagnostics", [], {})

        newest_marker = deal_prompt_cache_markers(new_text)[1]

        self.assertEqual(
            prompt_prefix_before(old_prompt, HISTORY_SECTION_MARKER),
            prompt_prefix_before(new_prompt, newest_marker),
        )


if __name__ == "__main__":
    unittest.main()
