from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bitrix.customer_history import communication_activity_kind
from bitrix.deals.communication_history import include_source_lead_communications
from bitrix.deals.history_compaction import COMPACT_HISTORY_POLICIES, compact_history_coverage
from openai_api.llm.analyze_deal import (
    DEAL_PROMPT_CACHE_KEY,
    HISTORY_SECTION_MARKER,
    build_prompt,
    deal_prompt_cache_markers,
)
from openai_api.llm.deal_current_situation import (
    CURRENT_SITUATION_CONTEXT_MARKER,
    build_deal_current_situation_context,
    is_substantive_client_content,
    load_deal_current_situation_context,
)
from openai_api.llm.deal_manager_situation import build_situation_prompt
from openai_api.llm.llm_client import prompt_prefix_before


def _response(item: dict) -> dict:
    return {"ok": True, "response": {"result": item}}


def _touchpoint(
    *,
    when: str,
    event_id: str,
    event_type: str,
    direction: str,
    text: str = "",
    subject: str = "",
    completed: str = "Y",
    files: list | None = None,
) -> dict:
    return {
        "when": when,
        "event_type": event_type,
        "entity_type": "deal",
        "entity_id": "1",
        "entity_key": "deal:1",
        "id": event_id,
        "subject": subject,
        "text": text,
        "direction": direction,
        "raw": {
            "ID": event_id,
            "DIRECTION": direction,
            "COMPLETED": completed,
            "START_TIME": when,
            "DESCRIPTION": text,
            "SUBJECT": subject,
            "FILES": files or [],
        },
    }


def _internal(
    *,
    when: str,
    event_id: str,
    text: str,
    category: str = "timeline_comment",
) -> dict:
    return {
        "when": when,
        "category": category,
        "event_type": "internal_comment",
        "entity_type": "deal",
        "entity_id": "1",
        "entity_key": "deal:1",
        "id": event_id,
        "text": text,
        "author": "Менеджер",
    }


def _bundle(
    *,
    touchpoints: list[dict] | None = None,
    internal: list[dict] | None = None,
    contact_name: str = "Олег Клиент",
) -> dict:
    first, last = contact_name.split(" ", 1)
    return {
        "bundle_type": "customer_history_bundle",
        "deal": _response({"ID": "1", "TITLE": "Сделка", "LEAD_ID": ""}),
        "contacts": {"7": _response({"ID": "7", "NAME": first, "LAST_NAME": last})},
        "client_touchpoints": touchpoints or [],
        "internal_context": internal or [],
        "activities_by_entity": {},
    }


class SubstantiveContentTests(unittest.TestCase):
    def test_acks_are_not_substantive_and_business_replies_are(self) -> None:
        self.assertFalse(is_substantive_client_content("ок"))
        self.assertFalse(is_substantive_client_content("спасибо"))
        self.assertFalse(is_substantive_client_content("получил"))
        self.assertFalse(is_substantive_client_content("понял"))
        self.assertFalse(is_substantive_client_content("Ок, спасибо!"))
        self.assertTrue(is_substantive_client_content("получил КП, посмотрю завтра"))
        self.assertTrue(is_substantive_client_content("дорого, рассматриваем Китай"))
        self.assertTrue(is_substantive_client_content("решение примет собственник во вторник"))
        self.assertTrue(is_substantive_client_content("нужна производительность 2000 шт/час"))


class DealCurrentSituationContextTests(unittest.TestCase):
    def test_deal_18633_keeps_china_position_and_treats_manager_messages_as_followup(self) -> None:
        bundle = _bundle(
            touchpoints=[],
            internal=[
                _internal(
                    when="2026-08-24T15:10:00+03:00",
                    event_id="18633-in",
                    text=(
                        "[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Олег Клиент:\n"
                        "Рассматриваем самостоятельный ввоз оборудования из Китая. "
                        "Полный комплект с полуавтоматом и таможней около 5 млн ₽."
                    ),
                ),
                _internal(
                    when="2026-08-25T11:00:00+03:00",
                    event_id="18633-out-1",
                    text=(
                        "[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Менеджер Иван:\n"
                        "Отправил альтернативное КП, критерии, монтаж, запуск и сервис."
                    ),
                ),
                _internal(
                    when="2026-08-25T14:20:00+03:00",
                    event_id="18633-out-2",
                    text=(
                        "[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Менеджер Иван:\n"
                        "Уточнил состав решения и сервис после монтажа."
                    ),
                ),
            ],
        )
        context = build_deal_current_situation_context(bundle, deal_id="18633")
        anchor = context["last_substantive_client_contact"]
        self.assertTrue(context["available"])
        self.assertEqual(anchor["occurred_at"][:10], "2026-08-24")
        self.assertIn("Китая", anchor["content"])
        self.assertIn("5 млн", anchor["content"])
        self.assertFalse(context["has_newer_client_response"])
        self.assertGreaterEqual(context["outgoing_messages_after_contact"], 2)
        self.assertEqual(context["call_attempts_after_contact"], 0)
        self.assertTrue(all(item["kind"] != "client_reply" for item in context["manager_actions_after_contact"]))

    def test_deal_18785_waits_for_technical_solution_after_bottleneck_contact(self) -> None:
        bundle = _bundle(
            internal=[
                _internal(
                    when="2026-08-24T12:00:00+03:00",
                    event_id="18785-in",
                    text=(
                        "[img]https://static.wazzup24.com/images/bitrix/telegram.png[/img] Олег Клиент:\n"
                        "Узкое место — розлив густого продукта; укупорщик и этикетировщик сами проблему не решают."
                    ),
                ),
                _internal(
                    when="2026-08-24T16:40:00+03:00",
                    event_id="18785-comment",
                    text="Вопрос передали специалисту по розливу. Технического заключения ещё нет.",
                ),
            ]
        )
        context = build_deal_current_situation_context(bundle, deal_id="18785")
        anchor = context["last_substantive_client_contact"]
        self.assertEqual(anchor["occurred_at"][:10], "2026-08-24")
        self.assertIn("розлив", anchor["content"].lower())
        self.assertFalse(context["has_newer_client_response"])
        self.assertEqual(
            [item["kind"] for item in context["manager_actions_after_contact"]],
            ["internal_comment"],
        )

    def test_deal_18735_owner_review_is_not_replaced_by_later_manager_attempts(self) -> None:
        bundle = _bundle(
            touchpoints=[
                _touchpoint(
                    when="2026-08-25T10:00:00+03:00",
                    event_id="18735-email",
                    event_type="email",
                    direction="2",
                    subject="Видео испытаний и обновлённое КП",
                    text="Направил видео испытаний и обновлённое КП с принтером.",
                ),
                _touchpoint(
                    when="2026-08-25T11:15:00+03:00",
                    event_id="18735-call-1",
                    event_type="call",
                    direction="2",
                    subject="Исходящий звонок",
                    completed="Y",
                ),
                _touchpoint(
                    when="2026-08-25T15:40:00+03:00",
                    event_id="18735-call-2",
                    event_type="call",
                    direction="2",
                    subject="Общий номер",
                    completed="Y",
                ),
            ],
            internal=[
                _internal(
                    when="2026-08-17T13:20:00+03:00",
                    event_id="18735-in",
                    text=(
                        "[img]https://static.wazzup24.com/images/bitrix/max.png[/img] Олег Клиент:\n"
                        "Собственник разрешил продолжать работу. Видео испытаний и обновлённое КП "
                        "с принтером покажем собственнику при следующем визите, дата неизвестна."
                    ),
                ),
            ],
        )
        context = build_deal_current_situation_context(bundle, deal_id="18735")
        anchor = context["last_substantive_client_contact"]
        self.assertEqual(anchor["occurred_at"][:10], "2026-08-17")
        self.assertIn("собственник", anchor["content"].lower())
        self.assertFalse(context["has_newer_client_response"])
        self.assertEqual(context["outgoing_emails_after_contact"], 1)
        self.assertEqual(context["call_attempts_after_contact"], 2)
        self.assertNotIn("conversation", {item["kind"] for item in context["manager_actions_after_contact"]})

    def test_inbound_thanks_does_not_replace_substantive_anchor(self) -> None:
        bundle = _bundle(
            internal=[
                _internal(
                    when="2026-08-24T12:00:00+03:00",
                    event_id="in-1",
                    text=(
                        "[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Олег Клиент:\n"
                        "Дорого, рассматриваем Китай."
                    ),
                ),
                _internal(
                    when="2026-08-25T09:00:00+03:00",
                    event_id="in-thanks",
                    text="[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Олег Клиент:\nспасибо",
                ),
            ]
        )
        context = build_deal_current_situation_context(bundle)
        self.assertIn("Китай", context["last_substantive_client_contact"]["content"])
        self.assertTrue(context["has_newer_client_response"])
        self.assertEqual(context["last_substantive_client_contact"]["occurred_at"][:10], "2026-08-24")
        self.assertEqual(len(context["later_non_substantive_client_replies"]), 1)
        self.assertIn("спасибо", context["later_non_substantive_client_replies"][0]["content"].lower())

    def test_received_kp_with_next_step_can_become_new_anchor(self) -> None:
        bundle = _bundle(
            internal=[
                _internal(
                    when="2026-08-20T12:00:00+03:00",
                    event_id="in-old",
                    text="[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Олег Клиент:\nНужна линия розлива.",
                ),
                _internal(
                    when="2026-08-25T09:00:00+03:00",
                    event_id="in-new",
                    text="[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Олег Клиент:\nполучил КП, посмотрю завтра",
                ),
            ]
        )
        context = build_deal_current_situation_context(bundle)
        self.assertEqual(context["last_substantive_client_contact"]["occurred_at"][:10], "2026-08-25")
        self.assertIn("КП", context["last_substantive_client_contact"]["content"])

    def test_manager_comment_is_not_client_evidence(self) -> None:
        bundle = _bundle(
            internal=[
                _internal(
                    when="2026-08-25T12:00:00+03:00",
                    event_id="mgr",
                    text="Клиент согласовал бюджет 5 млн.",
                )
            ]
        )
        context = build_deal_current_situation_context(bundle)
        self.assertIsNone(context["last_substantive_client_contact"])
        self.assertEqual(context["manager_actions_after_contact"], [])
        self.assertFalse(context["has_newer_client_response"])

    def test_completed_outgoing_call_without_transcript_is_dial_attempt(self) -> None:
        bundle = _bundle(
            touchpoints=[
                _touchpoint(
                    when="2026-08-25T11:00:00+03:00",
                    event_id="call-1",
                    event_type="call",
                    direction="2",
                    completed="Y",
                )
            ],
            internal=[
                _internal(
                    when="2026-08-24T10:00:00+03:00",
                    event_id="in-1",
                    text="[img]https://static.wazzup24.com/images/bitrix/whatsapp.png[/img] Олег Клиент:\nНужна производительность 2000 шт/час.",
                )
            ],
        )
        context = build_deal_current_situation_context(bundle)
        self.assertEqual(context["last_substantive_client_contact"]["occurred_at"][:10], "2026-08-24")
        self.assertEqual(context["call_attempts_after_contact"], 1)
        self.assertEqual(context["manager_actions_after_contact"][0]["kind"], "dial_attempt")
        self.assertEqual(communication_activity_kind({
            "channel": "call",
            "direction": "outgoing",
            "call_outcome": "no_answer",
            "contact_class": "attempt",
        }), "dial_attempt")

    def test_call_transcript_can_become_the_client_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcripts = Path(directory)
            (transcripts / "call.json").write_text(
                json.dumps({
                    "metadata": {
                        "activity_id": "call-9",
                        "call_start": "2026-08-26T10:00:00+03:00",
                    },
                    "text": "Клиент: рассматриваем Китай, полный комплект около 5 млн.",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            bundle = _bundle(
                touchpoints=[
                    _touchpoint(
                        when="2026-08-26T10:00:00+03:00",
                        event_id="call-9",
                        event_type="call",
                        direction="2",
                        files=[{"ID": "8"}],
                    )
                ]
            )
            context = build_deal_current_situation_context(
                bundle,
                transcripts_dir=transcripts,
                deal_id="1",
            )
        self.assertEqual(context["last_substantive_client_contact"]["occurred_at"][:10], "2026-08-26")
        self.assertIn("Китай", context["last_substantive_client_contact"]["content"])
        self.assertEqual(context["last_substantive_client_contact"]["evidence_id"], "call:call-9")

    def test_messenger_mirror_from_client_is_client_evidence(self) -> None:
        for channel, marker in (
            ("whatsapp", "whatsapp"),
            ("telegram", "telegram"),
            ("max", "max"),
        ):
            with self.subTest(channel=channel):
                bundle = _bundle(
                    internal=[
                        _internal(
                            when="2026-08-24T10:00:00+03:00",
                            event_id=f"{channel}-1",
                            text=f"[img]https://static.wazzup24.com/images/bitrix/{marker}.png[/img] Олег Клиент:\nНужен монтаж и запуск.",
                        )
                    ]
                )
                context = build_deal_current_situation_context(bundle)
                self.assertEqual(context["last_substantive_client_contact"]["channel"], channel)
                self.assertIn("монтаж", context["last_substantive_client_contact"]["content"])

    def test_source_lead_communication_is_available_to_the_deal(self) -> None:
        history = {
            "bundle_type": "customer_history_bundle",
            "client_touchpoints": [],
            "internal_context": [],
            "normalized_communications": [],
            "contacts": {"7": _response({"ID": "7", "NAME": "Олег", "LAST_NAME": "Клиент"})},
        }
        context = {
            "deal_id": "101",
            "deal": {"item": {"ID": "101", "LEAD_ID": "202"}},
            "source_lead": {
                "lead_id": "202",
                "activities": {
                    "ok": True,
                    "items": [{
                        "ID": "701",
                        "TYPE_ID": "4",
                        "OWNER_ID": "202",
                        "COMPLETED": "Y",
                        "DIRECTION": "1",
                        "START_TIME": "2026-08-24T10:10:00+03:00",
                        "DESCRIPTION": "Клиент подтвердил бюджет и срок решения во вторник.",
                    }],
                },
                "activity_details": {},
                "timeline_comments": [],
            },
        }
        bundle = include_source_lead_communications(history, context, deal_id="101")
        result = build_deal_current_situation_context(bundle, deal_id="101")
        self.assertIsNotNone(result["last_substantive_client_contact"])
        self.assertIn("бюджет", result["last_substantive_client_contact"]["content"])

    def test_compact_history_limit_does_not_drop_the_anchor(self) -> None:
        client = _touchpoint(
            when="2026-08-01T10:00:00+03:00",
            event_id="client-1",
            event_type="email",
            direction="1",
            text="Рассматриваем Китай, полный комплект около 5 млн.",
        )
        followups = [
            _touchpoint(
                when=f"2026-08-03T10:00:{index:02d}+03:00",
                event_id=f"out-{index}",
                event_type="email",
                direction="2",
                text=f"Исходящее сообщение менеджера {index}",
            )
            for index in range(31)
        ]
        bundle = _bundle(touchpoints=[client, *followups])
        coverage = compact_history_coverage(
            bundle["client_touchpoints"],
            **COMPACT_HISTORY_POLICIES["client_touchpoints"],
        )
        self.assertFalse(coverage[0]["included"])
        context = build_deal_current_situation_context(bundle)
        self.assertEqual(context["last_substantive_client_contact"]["occurred_at"][:10], "2026-08-01")
        self.assertIn("Китай", context["last_substantive_client_contact"]["content"])
        self.assertEqual(context["outgoing_emails_after_contact"], 31)

    def test_load_returns_unavailable_context_without_workspace_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = load_deal_current_situation_context(Path(directory), "999")
        self.assertFalse(context["available"])
        self.assertIsNone(context["last_substantive_client_contact"])
        self.assertIsNone(context["call_attempts_after_contact"])
        self.assertEqual(context["later_non_substantive_client_replies"], [])


class CurrentSituationPromptAndV2Tests(unittest.TestCase):
    def test_full_prompt_keeps_history_cache_boundary_and_new_rules(self) -> None:
        context = {
            "available": True,
            "last_substantive_client_contact": {
                "occurred_at": "2026-08-24T15:00:00+03:00",
                "channel": "whatsapp",
                "content": "Рассматриваем Китай",
            },
            "manager_actions_after_contact": [],
            "call_attempts_after_contact": 0,
            "outgoing_messages_after_contact": 1,
            "outgoing_emails_after_contact": 0,
            "has_newer_client_response": False,
        }
        prompt = build_prompt(
            "18633",
            "История сделки",
            "Транскрипт",
            "Диагностика",
            [],
            {},
            current_situation_context=context,
        )
        history_prefix = prompt_prefix_before(prompt, HISTORY_SECTION_MARKER)
        self.assertNotIn("Рассматриваем Китай", history_prefix)
        self.assertLess(prompt.find(HISTORY_SECTION_MARKER), prompt.find(CURRENT_SITUATION_CONTEXT_MARKER))
        self.assertIn("last_substantive_client_contact", prompt)
        self.assertIn("4-6 коротких предложений", prompt)
        self.assertIn("не заменяют предыдущий содержательный якорь", prompt)
        self.assertEqual(DEAL_PROMPT_CACHE_KEY, "neuro-rop:full-deal:v3")
        self.assertEqual(deal_prompt_cache_markers("text"), ["## ID СДЕЛКИ", HISTORY_SECTION_MARKER])

    def test_manager_situation_refinement_cannot_invent_client_position(self) -> None:
        prompt = build_situation_prompt(
            analysis_projection={"deal_control_brief": {"current_situation": "Клиент сравнивает Китай"}},
            deal={"deal_id": "18633"},
            current_bitrix_task=None,
            previous_manager_projection={},
            manager_context="Клиент согласовал бюджет",
        )
        self.assertIn("не должен превращать слова менеджера в подтверждённую новую позицию клиента", prompt)
        self.assertIn("NEW_MANAGER_CONTEXT", prompt)


if __name__ == "__main__":
    unittest.main()
