from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

from bitrix.customer_history_report import render_customer_history_markdown
from openai_api.llm.analyze_lead import LEAD_ID_SECTION_MARKER, build_prompt, load_lead_crm_state


class LeadPromptContextTests(unittest.TestCase):
    def test_lead_history_uses_tasks_without_todo_but_deal_history_keeps_todo(self):
        task = {"event_type": "task", "subject": "Настоящая задача", "raw": {"PROVIDER_ID": "CRM_TASKS_TASK"}}
        todo = {"event_type": "task", "subject": "Дело возврата", "raw": {"PROVIDER_ID": "CRM_TODO"}}
        bundle = {"root_entity": {"type": "lead", "id": "123"},
                  "tasks_and_control": [task, todo], "unified_timeline": [task, todo]}
        text = render_customer_history_markdown(bundle)
        self.assertIn("Настоящая задача", text)
        self.assertNotIn("Дело возврата", text)
        bundle["root_entity"]["type"] = "deal"
        self.assertIn("Дело возврата", render_customer_history_markdown(bundle))

    def test_lead_history_without_related_entities_excludes_todo(self):
        path = Path(__file__).resolve().parents[1] / "bitrix/leads/2_build_leads_customer_path_report.py"
        spec = importlib.util.spec_from_file_location("lead_task_renderer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = module.render_lead_markdown({"lead_id": "123", "activities": {"items": [
            {"ID": "1", "TYPE_ID": "6", "PROVIDER_ID": "CRM_TASKS_TASK", "SUBJECT": "Настоящая задача"},
            {"ID": "2", "TYPE_ID": "6", "PROVIDER_ID": "CRM_TODO", "SUBJECT": "Дело возврата"},
        ]}})
        self.assertIn("Настоящая задача", text)
        self.assertNotIn("Дело возврата", text)

    def test_failed_root_card_stops_fetch_before_activity_reads(self):
        path = Path(__file__).resolve().parents[1] / "bitrix/leads/1_fetch_leads_context.py"
        spec = importlib.util.spec_from_file_location("lead_context_fetcher", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module, "fetch_entity_by_id", return_value={"ok": False, "error": "timeout"}), \
             patch.object(module, "fetch_activities") as activities:
            with self.assertRaisesRegex(RuntimeError, "анализ остановлен"):
                module.fetch_lead_bundle(object(), "123")
            activities.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "статус лида недоступен"):
                load_lead_crm_state(Path(directory) / "lead_123")

    def test_history_without_related_entities_keeps_email_body(self):
        path = Path(__file__).resolve().parents[1] / "bitrix/leads/2_build_leads_customer_path_report.py"
        spec = importlib.util.spec_from_file_location("lead_history_renderer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = module.render_lead_markdown({
            "lead_id": "123", "lead": {"ok": True, "response": {"result": {"ID": "123"}}},
            "activities": {"items": [{"ID": "9", "TYPE_ID": "4", "SUBJECT": "Покупка",
                                      "DIRECTION": "1", "DESCRIPTION": "Планируем приобрести оборудование в следующем году"}]},
        })
        self.assertIn("Планируем приобрести оборудование в следующем году", text)
        self.assertIn("activity_id=9", text)
        self.assertIn("Направление: 1", text)

    def test_cache_prefix_is_shared_and_contains_rules_but_no_client_facts(self):
        rules = [(Path("rules.md"), "Правила квалификации")]
        first = build_prompt("123", "Клиент покупает сейчас", "Звонок один", "Диагностика один", rules)
        second = build_prompt("456", "Клиент покупает через год", "Звонок два", "Диагностика два", rules)
        prefix = first.split(LEAD_ID_SECTION_MARKER)[0]
        self.assertEqual(prefix, second.split(LEAD_ID_SECTION_MARKER)[0])
        self.assertIn("Правила квалификации", prefix)
        self.assertNotIn("Клиент покупает", prefix)
        self.assertNotIn('"lead_id": "123"', prefix)
        self.assertIn("\n123\n", first)

    def test_lead_history_includes_message_body_direction_and_source(self):
        bundle = {
            "root_entity": {"type": "lead", "id": "123"},
            "normalized_communications": [
                {"channel": "message", "event_id": "crm_mirror:42", "source_ids": ["42"],
                 "entity_key": "lead:123", "direction": "incoming", "contact_class": "confirmed_contact",
                 "subject": "Обсуждение", "content": "Мы уже выбираем поставщика"},
                {"channel": "internal_comment", "contact_class": "internal_information", "content": "Внутренняя заметка"},
                {"channel": "email", "direction": "outgoing", "contact_class": "attempt", "content": ""},
            ],
        }
        text = render_customer_history_markdown(bundle)
        self.assertIn("Мы уже выбираем поставщика", text)
        self.assertIn("crm_mirror:42", text)
        self.assertIn("Источники: 42", text)
        self.assertIn("incoming; контакт: confirmed_contact", text)
        self.assertIn("Текст сообщения недоступен", text)
        self.assertNotIn("Внутренняя заметка", text)
        bundle["root_entity"]["type"] = "deal"
        self.assertNotIn("Содержание клиентской переписки", render_customer_history_markdown(bundle))


if __name__ == "__main__":
    unittest.main()
