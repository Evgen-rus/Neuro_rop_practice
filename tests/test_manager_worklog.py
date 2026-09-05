from __future__ import annotations

import hashlib
import unittest

from bitrix.manager_worklog import (
    classify_manager_worklog,
    normalize_worklog_text,
    normalized_text_sha256,
    parse_manager_worklog,
)


class ManagerWorklogParserTests(unittest.TestCase):
    def test_case_a_accepts_compact_three_block_utf8_worklog(self) -> None:
        comment = {
            "ID": "comment-1",
            "CREATED": "2026-08-04T10:00:00+03:00",
            "AUTHOR_ID": "17",
            "COMMENT": (
                "01.08 Позвонил клиенту, уточнил задачу.\n"
                "02/08 Отправил КП, жду ответа.\n"
                "03.08 Согласовал следующий шаг."
            ),
        }

        result = parse_manager_worklog(comment)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["comment_id"], "comment-1")
        self.assertEqual(result["bitrix_created_at"], comment["CREATED"])
        self.assertEqual(result["author_id"], "17")
        self.assertEqual([entry["entry_date"] for entry in result["entries"]], [
            "2026-08-01", "2026-08-02", "2026-08-03",
        ])
        self.assertEqual(result["latest_entry_date"], "2026-08-03")
        self.assertTrue(classify_manager_worklog(comment))

    def test_case_b_long_comment_without_dated_blocks_is_not_worklog(self) -> None:
        comment = {"COMMENT": "Много текста о работе без дат. " * 20}

        self.assertIsNone(parse_manager_worklog(comment))
        self.assertFalse(classify_manager_worklog(comment))

    def test_case_c_date_lines_without_substantive_text_are_not_worklog(self) -> None:
        comment = {
            "CREATED": "2026-08-04T10:00:00+03:00",
            "COMMENT": "01.08 —\n02.08 —\n03.08 —",
        }

        self.assertIsNone(parse_manager_worklog(comment))

    def test_content_hash_is_stable_for_normalized_utf8_text(self) -> None:
        text = "  01.08 Позвонил клиенту, уточнил задачу.\r\n02.08 Отправил КП, жду ответ.\n03.08 Зафиксировал следующий шаг.  "
        result = parse_manager_worklog({"CREATED": "2026-08-04", "COMMENT": text})

        self.assertIsNotNone(result)
        assert result is not None
        normalized = normalize_worklog_text(text)
        expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        self.assertEqual(result["content_hash"], expected)
        self.assertEqual(result["content_hash"], normalized_text_sha256(text))

    def test_december_january_transition_uses_created_year_safely(self) -> None:
        text = (
            "31.12 Закрыл старый вопрос клиента и отправил итог.\n"
            "01.01 Получил уточнения клиента и записал решение.\n"
            "02/01 Подтвердил следующий шаг и срок ответа."
        )

        result = parse_manager_worklog({
            "CREATED": "2026-01-03T09:00:00+03:00",
            "COMMENT": text,
        })

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual([entry["entry_date"] for entry in result["entries"]], [
            "2025-12-31", "2026-01-01", "2026-01-02",
        ])

    def test_explicit_neighbor_year_is_used_for_yearless_entries(self) -> None:
        text = (
            "31.12.25 Закрыл старый вопрос и отправил итог клиенту.\n"
            "01.01 Получил уточнения и записал решение клиента.\n"
            "02.01 Подтвердил следующий шаг и срок ответа."
        )

        result = parse_manager_worklog({"COMMENT": text})

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual([entry["entry_date"] for entry in result["entries"]], [
            "2025-12-31", "2026-01-01", "2026-01-02",
        ])


if __name__ == "__main__":
    unittest.main()
