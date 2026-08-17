from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str) -> Any:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = load_module("deal_fetch_operational_test", "bitrix/deals/1_fetch_deals_context.py")
context = load_module("deal_context_operational_test", "bitrix/deals/4_build_deals_llm_context.py")


def task_response(
    task_id: str,
    *,
    status: str = "2",
    deadline: str | None = None,
    closed_date: str | None = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "response": {
            "result": {
                "task": {
                    "id": task_id,
                    "title": f"Задача {task_id}",
                    "description": f"Описание задачи {task_id}",
                    "status": status,
                    "deadline": deadline,
                    "closedDate": closed_date,
                    "chatId": chat_id,
                }
            }
        },
    }


class DealOperationalFetchTests(unittest.TestCase):
    def test_selects_three_nearest_open_tasks(self) -> None:
        tasks = {
            "1": task_response("1", deadline="2026-08-03T10:00:00+03:00"),
            "2": task_response("2", deadline="2026-08-01T10:00:00+03:00"),
            "3": task_response("3", deadline="2026-08-02T10:00:00+03:00"),
            "4": task_response("4", deadline="2026-07-31T10:00:00+03:00"),
            "5": task_response("5", status="5", deadline="2026-07-30T10:00:00+03:00"),
        }

        self.assertEqual(fetch.select_open_task_ids(tasks), ["4", "2", "3"])

    def test_missing_or_closed_tasks_do_not_break_selection(self) -> None:
        tasks = {
            "1": {"ok": False, "error": "denied"},
            "2": task_response("2", status="5", closed_date="2026-07-30T10:00:00+03:00"),
        }

        self.assertEqual(fetch.select_open_task_ids(tasks), [])

    def test_task_ids_are_taken_only_from_bitrix_task_activities(self) -> None:
        activities = [
            {"PROVIDER_ID": "CRM_TASKS_TASK", "ASSOCIATED_ENTITY_ID": "20"},
            {"PROVIDER_ID": "CRM_TASKS_TASK", "ASSOCIATED_ENTITY_ID": "10"},
            {"PROVIDER_ID": "CRM_EMAIL", "ASSOCIATED_ENTITY_ID": "99"},
            {"PROVIDER_ID": "CRM_TASKS_TASK", "ASSOCIATED_ENTITY_ID": ""},
        ]

        self.assertEqual(fetch.task_ids_from_activities(activities), ["10", "20"])

    def test_attachment_references_are_removed_from_saved_chat(self) -> None:
        payload = {
            "response": {
                "result": {
                    "files": [{"id": "1", "urlDownload": "secret"}],
                    "messages": [{"text": "Срок согласован", "params": {"ATTACH": {"id": "1"}}}],
                }
            }
        }

        cleaned = fetch.strip_attachment_references(payload)

        self.assertEqual(cleaned["response"]["result"]["files"], "[excluded: attachment reference]")
        self.assertEqual(
            cleaned["response"]["result"]["messages"][0]["params"]["ATTACH"],
            "[excluded: attachment reference]",
        )
        self.assertEqual(cleaned["response"]["result"]["messages"][0]["text"], "Срок согласован")


class DealOperationalContextTests(unittest.TestCase):
    def fixture(self) -> dict[str, Any]:
        return {
            "deal": {
                "item": {
                    "ID": "1",
                    "UF_MODEL": "ЭН00.05",
                    "UF_DAYS": "25",
                }
            },
            "fields": {
                "deal": {
                    "ok": True,
                    "response": {
                        "result": {
                            "UF_MODEL": {"formLabel": "Модель оборудования (из каталога ПрактикМ)"},
                            "UF_DAYS": {"formLabel": "Срок изготовления (дней)"},
                        }
                    },
                }
            },
            "stage_history": {
                "ok": True,
                "items": [
                    {"ID": "1", "CREATED_TIME": "2026-07-20T16:00:00+03:00", "STAGE_ID": "A"},
                    {"ID": "2", "CREATED_TIME": "2026-07-20T17:00:00+03:00", "STAGE_ID": "B"},
                ],
            },
            "stage_history_lookup": {
                "A": {"stage": {"name": "Договор согласован"}},
                "B": {"stage": {"name": "Счёт отправлен"}},
            },
            "bitrix_tasks": {
                "10": task_response(
                    "10",
                    deadline="2026-07-31T17:00:00+07:00",
                    chat_id="100",
                )
            },
            "bitrix_open_task_ids": ["10"],
            "bitrix_task_chats": {
                "10": {
                    "ok": True,
                    "response": {
                        "result": {
                            "messages": [
                                {"id": "1", "date": "2026-07-30T10:00:00+07:00", "text": "ок"},
                                {
                                    "id": "2",
                                    "date": "2026-07-30T11:00:00+07:00",
                                    "text": "Документы по договору отправлены на согласование.",
                                },
                                {
                                    "id": "3",
                                    "date": "2026-07-30T12:00:00+07:00",
                                    "text": "Срок оплаты клиент пока не подтвердил.",
                                },
                                {
                                    "id": "4",
                                    "date": "2026-07-30T13:00:00+07:00",
                                    "text": "Причина задержки оплаты уточняется у бухгалтерии.",
                                },
                            ]
                        }
                    },
                }
            },
        }

    def test_renders_compact_stage_task_chat_and_selected_fields(self) -> None:
        bundle = self.fixture()

        self.assertEqual(
            context.stage_history_rows(bundle),
            ["- 20.07.2026: Договор согласован → Счёт отправлен"],
        )
        task_rows = context.open_task_context_rows(bundle)
        rendered = "\n".join(task_rows)
        self.assertIn("срок_мск=31.07.2026 13:00", rendered)
        self.assertIn("Срок оплаты клиент пока не подтвердил.", rendered)
        self.assertIn("Причина задержки оплаты уточняется", rendered)
        self.assertNotIn("Документы по договору отправлены", rendered)
        self.assertNotIn(": ок", rendered)
        self.assertEqual(
            context.enriched_deal_context_rows(bundle),
            [
                "- Модель оборудования из карточки CRM: ЭН00.05",
                "- Срок изготовления из карточки CRM: 25 дней",
            ],
        )

    def test_stage_history_keeps_more_than_six_days(self) -> None:
        items = []
        lookup = {}
        for index in range(8):
            stage_id = str(index)
            items.append({
                "ID": stage_id,
                "CREATED_TIME": f"2026-07-{10 + index:02d}T12:00:00+03:00",
                "STAGE_ID": stage_id,
            })
            lookup[stage_id] = {"stage": {"name": f"Стадия {index}"}}
        bundle = {
            "deal": {"item": {}},
            "stage_history": {"ok": True, "items": items},
            "stage_history_lookup": lookup,
        }
        rows = context.stage_history_rows(bundle)
        self.assertEqual(len(rows), 8)
        self.assertIn("Стадия 0", rows[0])
        self.assertIn("Стадия 7", rows[-1])

    def test_nested_customer_history_operational_context_is_supported(self) -> None:
        nested = {"bundle_type": "customer_history_bundle", "deal_operational_context": self.fixture()}

        self.assertTrue(context.stage_history_rows(nested))
        self.assertTrue(context.open_task_context_rows(nested))
        self.assertTrue(context.enriched_deal_context_rows(nested))

    def test_missing_optional_data_produces_no_sections(self) -> None:
        bundle = {"deal": {"item": {}}, "fields": {"deal": {"ok": False}}}

        self.assertEqual(context.stage_history_rows(bundle), [])
        self.assertEqual(context.open_task_context_rows(bundle), [])
        self.assertEqual(context.enriched_deal_context_rows(bundle), [])


if __name__ == "__main__":
    unittest.main()
