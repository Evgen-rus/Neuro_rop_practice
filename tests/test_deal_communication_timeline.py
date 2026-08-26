from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from bitrix.deals.communication_timeline import (
    build_deal_communication_timeline,
    render_timeline,
)


def response(item: dict) -> dict:
    return {"ok": True, "response": {"result": item}}


def activity_row(
    item_id: str,
    when: str,
    event_type: str,
    *,
    subject: str = "",
    text: str = "",
    direction: str = "2",
    files: list | None = None,
) -> tuple[dict, dict]:
    type_id = {"call": "2", "email": "4", "message": "1"}[event_type]
    raw = {
        "ID": item_id,
        "TYPE_ID": type_id,
        "START_TIME": when,
        "SUBJECT": subject,
        "DESCRIPTION": text,
        "DIRECTION": direction,
        "FILES": files or [],
    }
    row = {
        "when": when,
        "category": "activity",
        "event_type": event_type,
        "entity_key": "deal:123",
        "entity_type": "deal",
        "entity_id": "123",
        "id": item_id,
        "subject": subject,
        "text": text[:900],
        "direction": direction,
        "raw": raw,
    }
    return row, raw


def internal_row(item_id: str, when: str, text: str, *, category: str = "timeline_comment") -> dict:
    return {
        "when": when,
        "category": category,
        "event_type": "internal_chat_message" if category == "internal_im_chat" else "internal_comment",
        "entity_key": "deal:123",
        "entity_type": "deal",
        "entity_id": "123",
        "id": item_id,
        "author": "Учебный менеджер",
        "subject": "",
        "text": text,
    }


def fixture_bundle() -> dict:
    client_rows: list[dict] = []
    raw_activities: list[dict] = []

    old, raw = activity_row(
        "old-email", "2026-01-01T09:00:00+03:00", "email", text="Старое письмо клиента", direction="1"
    )
    client_rows.append(old)
    raw_activities.append(raw)
    for index in range(27):
        row, raw = activity_row(
            f"fill-{index}",
            f"2026-02-{index + 1:02d}T09:00:00+03:00",
            "message",
            text=f"Учебное сообщение {index}",
        )
        client_rows.append(row)
        raw_activities.append(raw)

    short, raw = activity_row(
        "short-email",
        "2026-03-01T10:00:00+03:00",
        "email",
        subject="Короткая тема",
        text="Полный короткий текст письма\nОт кого:\nНЕ ПОКАЗЫВАТЬ ЦЕПОЧКУ",
        direction="1",
    )
    client_rows.append(short)
    raw_activities.append(raw)
    long_text = "Длинное письмо клиента: " + "я" * 620
    long_row, raw = activity_row(
        "long-email", "2026-03-02T10:00:00+03:00", "email", text=long_text, direction="1"
    )
    client_rows.append(long_row)
    raw_activities.append(raw)
    with_transcript, raw = activity_row(
        "call-1",
        "2026-03-03T10:00:00+03:00",
        "call",
        subject="Разговор с клиентом",
        files=[{"id": "file-1"}],
    )
    client_rows.append(with_transcript)
    raw_activities.append(raw)
    without_transcript, raw = activity_row(
        "call-2", "2026-03-04T10:00:00+03:00", "call", subject="Недозвон"
    )
    client_rows.append(without_transcript)
    raw_activities.append(raw)
    mirrored_activity, raw = activity_row(
        "wa-activity",
        "2026-04-20T10:00:00+03:00",
        "message",
        text="Получил предложение, спасибо.",
        direction="1",
    )
    client_rows.append(mirrored_activity)
    raw_activities.append(raw)

    internal_rows = [
        internal_row("old-comment", "2026-01-02T09:00:00+03:00", "Старый внутренний комментарий")
    ]
    for index in range(17):
        internal_rows.append(
            internal_row(
                f"internal-fill-{index}",
                f"2026-04-{index + 1:02d}T09:00:00+03:00",
                f"Учебный внутренний контекст {index}",
                category="internal_im_chat",
            )
        )
    whatsapp = (
        "[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] "
        "Иван Учебный:\nПолучил предложение, спасибо."
    )
    max_message = (
        "[img]https://static.wazzup24.com/images/bitrix/max.png[/img] "
        "Другой Автор:\nОтправляю уточнение."
    )
    internal_rows.extend(
        [
            internal_row("wa-1", "2026-04-20T10:00:00+03:00", whatsapp),
            internal_row("max-1", "2026-04-21T10:00:00+03:00", max_message),
            internal_row("current-comment", "2026-04-22T10:00:00+03:00", "Актуальная заметка менеджера"),
        ]
    )
    raw_comments = [
        {
            "ID": item["id"],
            "CREATED": item["when"],
            "COMMENT": item["text"],
        }
        for item in internal_rows
        if item["category"] == "timeline_comment"
    ]
    return {
        "bundle_type": "customer_history_bundle",
        "root_entity": {"type": "deal", "id": "123", "title": "Учебная сделка"},
        "history_period": {
            "days": 365,
            "date_from": "2025-04-22T00:00:00+03:00",
            "date_to": "2026-04-22T23:59:59+03:00",
        },
        "deal": response({"ID": "123"}),
        "contacts": {
            "7": response({"ID": "7", "NAME": "Иван", "LAST_NAME": "Учебный"})
        },
        "client_touchpoints": client_rows,
        "internal_context": internal_rows,
        "tasks_and_control": [{"id": "task-1"}],
        "system_events": [{"id": "system-1"}],
        "activities_by_entity": {
            "deal:123": {
                "entity_type": "deal",
                "entity_id": "123",
                "activities": {"items": raw_activities},
                "activity_details": {},
            }
        },
        "timeline_comments_by_entity": {"deal:123": [{"items": raw_comments}]},
    }


class DealCommunicationTimelineTests(unittest.TestCase):
    def test_compact_rows_keep_the_existing_exact_format(self) -> None:
        module_path = Path(__file__).parents[1] / "bitrix" / "deals" / "4_build_deals_llm_context.py"
        spec = importlib.util.spec_from_file_location("deal_llm_context_for_timeline_test", module_path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        items = [
            {
                "when": "2026-01-01T10:00:00+03:00",
                "entity_type": "deal",
                "entity_id": "123",
                "event_type": "email",
                "id": "1",
                "subject": "Тема",
                "text": "Тело",
            },
            {
                "when": "2026-01-02T10:00:00+03:00",
                "entity_type": "deal",
                "entity_id": "123",
                "event_type": "internal_chat_message",
                "category": "internal_im_chat",
                "id": "2",
                "subject": "Не брать эту тему",
                "text": "Брать этот текст",
            },
        ]
        self.assertEqual(
            module.compact_history_rows(items, limit=2, text_limit=500),
            [
                "- 2026-01-01T10:00:00+03:00 source=deal:123 type=email id=1: Тема",
                "- 2026-01-02T10:00:00+03:00 source=deal:123 type=internal_chat_message id=2: Брать этот текст",
            ],
        )

    def test_renders_required_coverage_and_chronology(self) -> None:
        bundle = fixture_bundle()
        with tempfile.TemporaryDirectory() as directory:
            transcripts = Path(directory)
            transcript_text = "Менеджер: Добрый день.\nКлиент: Предложение получил."
            (transcripts / "call_call-1_transcript.json").write_text(
                json.dumps(
                    {
                        "metadata": {
                            "activity_id": "call-1",
                            "call_start": "2026-03-03T10:00:00+03:00",
                            "subject": "Разговор с клиентом",
                        },
                        "text": transcript_text,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            markdown = render_timeline(bundle, transcripts)

        self.assertLess(markdown.index("Старое письмо клиента"), markdown.index("Полный короткий текст письма"))
        self.assertIn("не ушло в сжатую историю", markdown)
        self.assertIn("урезано: в промпт 500 из 644 символов", markdown)
        self.assertIn("модель увидела тема: «Короткая тема»", markdown)
        self.assertIn("Получил предложение, спасибо.", markdown)
        self.assertIn("как внутренний контекст, не как слова клиента", markdown)
        self.assertIn("Автор не совпал с контактом, направление не подтверждено", markdown)
        self.assertIn("Актуальная заметка менеджера", markdown)
        self.assertIn("Транскрипт ушёл вторым блоком полностью", markdown)
        self.assertIn("Расшифровки нет: в локальном снимке CRM нет ссылки или метаданных файла записи", markdown)
        self.assertIn("Менеджер: Добрый день.", markdown)
        self.assertIn("Клиент: Предложение получил.", markdown)
        self.assertNotIn("НЕ ПОКАЗЫВАТЬ ЦЕПОЧКУ", markdown)
        self.assertIn("Процитированная предыдущая email-цепочка не показана", markdown)
        self.assertIn("По каналам: email", markdown)
        self.assertEqual(markdown.count("20.04.2026 10:00 МСК — 💬 WHATSAPP"), 1)
        self.assertIn("ID источника: wa-1, wa-activity", markdown)
        self.assertIn("раздел «Клиентские касания»", markdown)
        self.assertIn("задачи/контроль (1) и системные CRM-события (1)", markdown)
        self.assertIn("Учебная сделка", markdown)

    def test_cli_builder_writes_utf8_to_the_expected_workspace_path(self) -> None:
        bundle = fixture_bundle()
        with tempfile.TemporaryDirectory() as directory:
            deal_root = Path(directory)
            deal_dir = deal_root / "deal_123"
            raw_dir = deal_dir / "raw"
            raw_dir.mkdir(parents=True)
            bundle_path = raw_dir / "deal_123_customer_history_bundle.json"
            bundle_path.write_text(
                json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
            )

            output = build_deal_communication_timeline("123", deal_root=deal_root)
            raw_content = output.read_bytes()
            content = raw_content.decode("utf-8")

        self.assertEqual(
            output,
            deal_dir / "history" / "deal_123_communication_timeline.md",
        )
        self.assertIn("Учебная".encode("utf-8"), raw_content)
        self.assertIn("Учебная сделка", content)

    def test_missing_bundle_is_an_honest_local_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(FileNotFoundError, "Bitrix не вызывался"):
                build_deal_communication_timeline("123", deal_root=Path(directory))


if __name__ == "__main__":
    unittest.main()
