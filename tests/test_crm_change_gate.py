from __future__ import annotations
import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from crm_sync_fixture import SnapshotHarness
from api.crm_change_gate import _same_amount, acknowledge_refresh, audio_due, deal_job_can_acknowledge, probe_changes, transcript_signature
from bitrix.context_sync import ContextReadClient, atomic_json, dialog_delta, local_sync_lock, retained_response, retain_failed_sources, timeline_delta
from bitrix.deals.download_deals_call_audio import refresh_missing_call_files
from bitrix.internal_im_chat import fetch_internal_im_chats
from storage.rop_db import get_crm_sync_state, put_crm_sync_state, record_manager_trajectory_event


class CrmChangeGateTests(unittest.TestCase):
    def setUp(self):
        self.h = SnapshotHarness()
        self.addCleanup(self.h.close)
        self.deal_id = self.h.ids[0]
        self.h.initial()
        self.h.remote.reset_counts()

    def test_failed_reordered_chat_restores_only_matching_identity(self):
        old = [{"chat_id": "1", "messages_response": {"ok": True, "response": "first"}},
               {"chat_id": "2", "messages_response": {"ok": True, "response": "second"}}]
        current = [{"chat_id": "2", "messages_response": {"ok": False}},
                   {"chat_id": "3", "messages_response": {"ok": False}}]
        restored = retain_failed_sources(current, old)
        self.assertEqual(restored[0]["messages_response"]["response"], "second")
        self.assertFalse(restored[1]["messages_response"]["ok"])
        self.assertNotIn("response", restored[1]["messages_response"])

    def test_historical_relationship_keeps_chat_evidence(self):
        state = self.h.state()
        state["payload"]["customer_history"]["internal_im_chats_by_entity"]["deal:99999"] = {
            "chats": [{"chat_id": "99", "messages_response": {"ok": True, "response": {
                "result": {"messages": [{"id": "10", "text": "Сохранённая история", "author_id": "1"}]}}}}]}
        put_crm_sync_state(self.h.db, f"deal_context:{self.deal_id}", state["payload"], expected_revision=state["revision"])
        self.h.refresh(self.deal_id, self.h.plans()[self.deal_id])
        customer = self.h.state()["payload"]["customer_history"]
        self.assertTrue(customer["internal_im_chats_by_entity"]["deal:99999"]["historical_link"])
        self.assertTrue(any(row.get("id") == "im:99:10" for row in customer["internal_context"]))

    def test_idle_skips_heavy_and_preserves_context_and_empty_commercial_sources(self):
        before = self.h.state()
        plans = self.h.plans()
        self.assertEqual(plans[self.deal_id]["mode"], "skip", plans)
        self.assertEqual(self.h.state(), before)
        for method in ("crm.deal.get", "crm.invoice.list", "crm.item.list", "crm.deal.productrows.get", "im.search.chat.list"):
            self.assertEqual(self.h.remote.commands[method], 0)
        from api.daytime_cycle import _analyze_work_pool
        with patch("api.crm_change_gate.plan_automatic_refresh", return_value=plans), \
             patch("api.jobs.start_analyze_job") as start, \
             patch("api.daytime_cycle.threading.Thread.start"), \
             patch.dict("os.environ", {"SPEND_DIARY_DIR": str(self.h.root / "spend")}):
            result = _analyze_work_pool(db_path=self.h.db, deal_ids=self.h.ids, started_at=self.h.remote.now.isoformat())
        start.assert_not_called()
        self.assertEqual(result["counts"]["heavy_context_fetch_count"], 0)
        self.assertEqual(result["counts"]["skipped_before_fetch"], 1)

    def test_amount_comparison_preserves_precision_and_missing_values(self):
        cases = [
            ("100.00", "100.000000", True),
            (100, "100.00", True),
            (100.0, "100", True),
            (" 100.00 ", "100", True),
            (0, "0.000000", True),
            ("-0.00", "0", True),
            ("-100.00", "-100", True),
            ("100.00", "100.01", False),
            ("9007199254740992.00", "9007199254740993.00", False),
            ("100.00000000000000000001", "100.00000000000000000002", False),
            (None, "", True),
            (None, "0", False),
            ("", 0, False),
            (None, "100", False),
            ("", "100", False),
            ("invalid", "0", False),
            ("invalid", "other", False),
            ("invalid", "invalid", True),
            ("100,00", "100.00", False),
            ("NaN", "100", False),
            ("sNaN", "0", False),
            ("Infinity", "100", False),
            ("Infinity", "-Infinity", False),
        ]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                self.assertEqual(_same_amount(left, right), expected)
                self.assertEqual(_same_amount(right, left), expected)

    def test_amount_format_only_skips_heavy_without_changing_context(self):
        before = self.h.state()
        for amount in ("100.00", "100.000000", 100, 100.0):
            with self.subTest(amount=amount):
                self.h.remote.deals[self.deal_id]["OPPORTUNITY"] = amount
                self.h.sync_portfolio()
                plan = self.h.plans()[self.deal_id]
                self.assertEqual(plan["mode"], "skip", plan)
                self.assertNotIn("deal_fields", plan["reasons"])
        self.assertEqual(self.h.state(), before)
        for method in ("crm.deal.get", "im.search.chat.list"):
            self.assertEqual(self.h.remote.commands[method], 0)

    def test_real_amount_change_refreshes_then_skips_after_acknowledgement(self):
        self.h.remote.deals[self.deal_id]["OPPORTUNITY"] = "100.01"
        self.h.sync_portfolio()
        plan = self.h.plans()[self.deal_id]
        self.assertEqual(plan["mode"], "incremental")
        self.assertIn("deal_fields", plan["reasons"])
        self.h.refresh(self.deal_id, plan)
        self.assertEqual(self.h.plans()[self.deal_id]["mode"], "skip")

    def test_equal_amount_format_does_not_mask_other_deal_fields(self):
        original = copy.deepcopy(self.h.remote.deals[self.deal_id])
        for field, value in (
            ("STAGE_ID", "NEXT"),
            ("ASSIGNED_BY_ID", "8"),
            ("DATE_MODIFY", (self.h.remote.now + timedelta(seconds=1)).isoformat()),
        ):
            with self.subTest(field=field):
                self.h.remote.deals[self.deal_id] = {**original, "OPPORTUNITY": "100.000000", field: value}
                self.h.sync_portfolio()
                plan = self.h.plans()[self.deal_id]
                self.assertEqual(plan["mode"], "incremental")
                self.assertIn("deal_fields", plan["reasons"])

    def test_new_activity_refreshes_delta_without_invoices_and_products(self):
        row = copy.deepcopy(self.h.remote.activities[self.deal_id][0])
        row.update(ID="99901", LAST_UPDATED=self.h.remote.now.isoformat(), SUBJECT="Новое письмо")
        self.h.remote.activities[self.deal_id].append(row)
        plan = self.h.plans()[self.deal_id]
        self.assertEqual(plan["mode"], "incremental")
        self.assertIn("activity", plan["reasons"])
        self.h.refresh(self.deal_id, plan)
        self.assertEqual(len(self.h.state()["payload"]["context"]["activities"]["items"]), 2)
        for method in ("crm.invoice.list", "crm.item.list", "crm.deal.productrows.get"):
            self.assertEqual(self.h.remote.commands[method], 0)

    def test_full_refresh_does_not_query_disabled_commercial_sources(self):
        for method in ("crm.invoice.list", "crm.item.list", "crm.deal.productrows.get"):
            self.h.remote.fail.add(method)
        self.h.refresh(self.deal_id, {**self.h.plans()[self.deal_id], "mode": "full"})
        raw = self.h.state()["payload"]["context"]
        self.assertFalse(raw["structured_commercial_sources_enabled"])
        self.assertIsNone(raw["product_rows"])
        self.assertEqual(raw["invoice_attempts"], [])
        self.assertFalse(raw["sync"]["retry_required"])
        for method in ("crm.invoice.list", "crm.item.list", "crm.deal.productrows.get"):
            self.assertEqual(self.h.remote.commands[method], 0)

    def test_stage_change_and_linked_contact_change_are_detected(self):
        self.h.remote.deals[self.deal_id]["STAGE_ID"] = "NEXT"
        self.h.sync_portfolio()
        self.assertEqual(self.h.plans()[self.deal_id]["mode"], "incremental")
        plan = self.h.plans()[self.deal_id]
        self.h.refresh(self.deal_id, plan)
        contact = self.h.remote.contacts[next(iter(self.h.remote.contacts))]
        contact["DATE_MODIFY"] = self.h.remote.now.isoformat()
        self.assertIn("linked_entity", self.h.plans()[self.deal_id]["reasons"])

    def test_comment_without_deal_modify_is_detected_and_overlap_deduplicates(self):
        self.h.remote.comments[("deal", self.deal_id)].append({"ID": "99902", "CREATED": self.h.remote.now.isoformat(), "COMMENT": "Новый комментарий"})
        plan = self.h.plans()[self.deal_id]
        self.assertIn("timeline", plan["reasons"])
        self.h.refresh(self.deal_id, plan)
        first = self.h.state()["payload"]["context"]["timeline_comments"][0]
        self.h.refresh(self.deal_id, {**self.h.plans()[self.deal_id], "mode": "incremental"})
        second = self.h.state()["payload"]["context"]["timeline_comments"][0]
        self.assertEqual(len(first["items"]), 121)
        self.assertEqual(len(second["items"]), 121)

    def test_failed_timeline_keeps_cursor_and_evidence_then_retries(self):
        old = self.h.state()["payload"]["context"]["timeline_comments"][0]
        self.h.remote.fail.add("crm.timeline.comment.list")
        plan = self.h.plans()[self.deal_id]
        self.h.refresh(self.deal_id, plan)
        new = self.h.state()["payload"]["context"]["timeline_comments"][0]
        self.assertEqual(new["last_success_at"], old["last_success_at"])
        self.assertEqual(new["items"], old["items"])
        self.assertTrue(self.h.state()["payload"]["context"]["sync"]["retry_required"])
        self.assertEqual(self.h.plans()[self.deal_id]["mode"], "skip")
        self.assertNotEqual(self.h.plans(now=self.h.remote.now + timedelta(minutes=30))[self.deal_id]["mode"], "skip")

    def test_failed_activity_cursor_does_not_advance(self):
        old = self.h.state()["payload"]["context"]["sync"]["activity_cursor"]
        self.h.remote.fail.add("crm.activity.list")
        self.h.refresh(self.deal_id, self.h.plans()[self.deal_id])
        self.assertEqual(self.h.state()["payload"]["context"]["sync"]["activity_cursor"], old)

    def test_failed_analysis_leaves_context_unacknowledged(self):
        plan = self.h.plans()[self.deal_id]
        self.h.refresh(self.deal_id, {**plan, "mode": "incremental"}, acknowledge=False)
        self.assertIn("recovery", self.h.plans()[self.deal_id]["reasons"])

    def test_reconciliation_finds_old_comment_edit_beyond_head_probe(self):
        self.h.remote.comments[("deal", self.deal_id)][0]["COMMENT"] = "Исправленная старая запись"
        self.assertEqual(self.h.plans()[self.deal_id]["mode"], "skip")
        plan = self.h.plans(now=self.h.remote.now + timedelta(days=1, minutes=1))[self.deal_id]
        self.assertEqual(plan["mode"], "full")
        self.h.refresh(self.deal_id, plan)
        comments = self.h.state()["payload"]["context"]["timeline_comments"][0]["items"]
        self.assertIn("Исправленная старая запись", [row["COMMENT"] for row in comments])

    def test_new_chat_discovery_is_periodic_and_keeps_all_message_pages(self):
        self.h.remote.chats["55"] = {"entity_id": f"DEAL|{self.deal_id}", "messages": [
            {"id": str(index), "text": "Тестовое сообщение", "date": self.h.remote.now.isoformat(), "author_id": "7"}
            for index in range(1, 131)]}
        plan = self.h.plans(now=self.h.remote.now + timedelta(days=1, minutes=1))[self.deal_id]
        self.h.refresh(self.deal_id, plan)
        customer = self.h.state()["payload"]["customer_history"]
        chat = customer["internal_im_chats_by_entity"][f"deal:{self.deal_id}"]["chats"][0]
        self.assertEqual(len(chat["messages_response"]["response"]["result"]["messages"]), 130)
        self.h.remote.chats["55"]["messages"].append({"id": "131", "text": "Новое", "date": self.h.remote.now.isoformat(), "author_id": "7"})
        self.assertIn("chat", self.h.plans()[self.deal_id]["reasons"])

    def test_chat_discovery_failure_is_not_a_negative_cache(self):
        self.h.remote.fail.add("im.search.chat.list")
        first = fetch_internal_im_chats(self.h.client, entity_type="deal", entity_id=self.deal_id, title="Тест")
        self.assertIsNone(first["discovery_success_at"])
        self.h.remote.reset_counts()
        fetch_internal_im_chats(self.h.client, entity_type="deal", entity_id=self.deal_id, title="Тест", previous_bundle=first)
        self.assertGreater(self.h.remote.commands["im.search.chat.list"], 0)

    def test_failed_chat_update_retains_messages_and_cursor(self):
        previous = {"ok": True, "last_success_at": self.h.remote.now.isoformat(),
            "response": {"result": {"messages": [{"id": "1", "text": "Сохранённое сообщение"}], "users": [], "files": []}}}
        self.h.remote.fail.add("im.dialog.messages.get")
        result = dialog_delta(self.h.client, "chat55", previous)
        self.assertEqual(result["response"], previous["response"])
        self.assertEqual(result["last_success_at"], previous["last_success_at"])
        self.assertFalse(result["refresh_ok"])

    def test_audio_missing_files_remains_independent_of_deal_modification(self):
        activity = self.h.remote.activities[self.deal_id][0]
        activity.update(TYPE_ID="2", FILES=[], PROVIDER_ID="CALL", START_TIME=self.h.remote.now.isoformat())
        state = self.h.state()
        state["payload"]["context"]["activities"]["items"] = [copy.deepcopy(activity)]
        put_crm_sync_state(self.h.db, f"deal_context:{self.deal_id}", state["payload"], expected_revision=state["revision"])
        ack = get_crm_sync_state(self.h.db, f"deal_ack:{self.deal_id}")
        acknowledge_refresh(self.h.db, self.deal_id, {"ack_revision": ack["revision"], "events": ack["payload"]["events"]}, workspace_root=self.h.workspace)
        self.assertEqual(self.h.plans()[self.deal_id]["mode"], "audio")
        activity["FILES"] = [{"id": "88"}]
        refreshed = refresh_missing_call_files(self.h.client, self.h.state()["payload"]["context"])
        self.assertEqual(refreshed["activities"]["items"][0]["FILES"], [{"id": "88"}])
        self.assertEqual(self.h.remote.commands["crm.activity.get"], 1)

    def test_heavy_pipeline_also_rechecks_missing_files_without_activity_update(self):
        from bitrix.deals import download_deals_call_audio as audio
        from types import SimpleNamespace
        state = self.h.state()
        activity = state["payload"]["context"]["activities"]["items"][0]
        activity.update(TYPE_ID="2", PROVIDER_ID="CALL", FILES=[])
        put_crm_sync_state(self.h.db, f"deal_context:{self.deal_id}", state["payload"], expected_revision=state["revision"])
        atomic_json(self.h.raw / f"deal_{self.deal_id}_context.json", state["payload"]["context"])
        self.h.remote.activities[self.deal_id][0].update(TYPE_ID="2", PROVIDER_ID="CALL", FILES=[{"id": "88"}])
        args = SimpleNamespace(deal_ids=self.h.ids, raw_dir=str(self.h.raw), audio_dir=str(self.h.audio),
            redownload=False, recheck_only=False, db_path=str(self.h.db), max_voice_lookback_days=30)
        with patch.object(audio, "parse_args", return_value=args), patch.object(audio, "load_dotenv"), \
             patch.object(audio, "get_env_required", return_value="https://fixture.invalid/rest/test"), \
             patch.object(audio, "build_manifest", return_value={"calls": []}), patch.object(audio, "enrich_manifest_calls", side_effect=lambda value: value):
            audio.main()
        self.assertEqual(self.h.state()["payload"]["context"]["activities"]["items"][0]["FILES"], [{"id": "88"}])

    def test_new_local_transcript_is_analysis_signal_without_crm_fetch(self):
        folder = self.h.workspace / f"deal_{self.deal_id}" / "transcripts"
        folder.mkdir(parents=True)
        (folder / "call.txt").write_text("Синтетическая транскрипция", encoding="utf-8")
        plan = self.h.plans()[self.deal_id]
        self.assertEqual(plan["mode"], "local")
        self.assertIn("new_transcript", plan["reasons"])

    def test_late_trajectory_event_is_not_swallowed_by_job_acknowledgement(self):
        plan = self.h.plans()[self.deal_id]
        record_manager_trajectory_event(self.h.db, entity_type="deal", entity_id=self.deal_id, manager_id="7",
            event_type="crm_activity_observed", source="bitrix", source_event_key="synthetic-late", occurred_at=(self.h.remote.now - timedelta(days=30)).isoformat(), payload={})
        self.h.refresh(self.deal_id, {**plan, "mode": "incremental"})
        self.assertIn("trajectory", self.h.plans()[self.deal_id]["reasons"])

    def test_stale_writer_cannot_replace_newer_snapshot_or_cursor(self):
        old = self.h.state()
        put_crm_sync_state(self.h.db, f"deal_context:{self.deal_id}", old["payload"], expected_revision=old["revision"])
        with self.assertRaisesRegex(RuntimeError, "revision conflict"):
            put_crm_sync_state(self.h.db, f"deal_context:{self.deal_id}", {"context": {}}, expected_revision=old["revision"])
        self.assertEqual(self.h.state()["payload"], old["payload"])

    def test_workspace_lock_is_exclusive_and_released(self):
        path = self.h.root / "lock"
        with local_sync_lock(path):
            with self.assertRaisesRegex(RuntimeError, "busy"):
                with local_sync_lock(path):
                    self.fail("second writer entered")
        with local_sync_lock(path):
            pass

    def test_schema_cache_is_shared_across_jobs_and_failed_reads_are_not_cached(self):
        client = ContextReadClient(self.h.client, db_path=self.h.db)
        client.safe_call("crm.deal.fields")
        self.assertEqual(self.h.remote.commands["crm.deal.fields"], 0)
        self.h.remote.fail.add("crm.test.fields")
        self.assertFalse(client.safe_call("crm.test.fields")["ok"])
        self.assertFalse(client.safe_call("crm.test.fields")["ok"])
        self.assertEqual(self.h.remote.commands["crm.test.fields"], 2)

    def test_manual_force_command_overrides_audio_incremental_modes(self):
        from api.jobs import AnalyzeOptions, build_cli_command
        command = build_cli_command(AnalyzeOptions(entity_type="deal", ids=self.h.ids,
            force_llm=True, context_refresh_mode="audio"), "deal", self.h.ids)
        self.assertEqual(command[command.index("--context-refresh-mode") + 1], "full")

    def test_new_task_and_task_description_changes_are_detected(self):
        activity = copy.deepcopy(self.h.remote.activities[self.deal_id][0])
        activity.update(ID="99908", PROVIDER_ID="CRM_TASKS_TASK", ASSOCIATED_ENTITY_ID="77", COMPLETED="N",
                        LAST_UPDATED=self.h.remote.now.isoformat())
        self.h.remote.activities[self.deal_id].append(activity)
        self.h.remote.tasks["77"] = {"id": "77", "status": "2", "description": "Описание задачи"}
        plan = self.h.plans()[self.deal_id]
        self.h.refresh(self.deal_id, plan)
        self.h.remote.tasks["77"]["description"] = "Обновлённое описание задачи"
        self.assertIn("task", self.h.plans()[self.deal_id]["reasons"])

    def test_commit_failure_does_not_materialize_new_context(self):
        before = (self.h.raw / f"deal_{self.deal_id}_context.json").read_bytes()
        plan = {**self.h.plans()[self.deal_id], "mode": "incremental"}
        with patch.object(self.h.fetch, "put_crm_sync_state", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.h.refresh(self.deal_id, plan)
        self.assertEqual((self.h.raw / f"deal_{self.deal_id}_context.json").read_bytes(), before)

    def test_audio_idle_never_enters_analysis_and_new_transcript_does(self):
        from run_rop_assistant import WorkflowOptions, run_workflow
        options = WorkflowOptions(entity_type="deal", entity_ids=[self.deal_id], history_days=60,
            include_related_contact_deals=True, include_internal_context=True, download_audio=True,
            redownload_audio=False, transcribe_audio=True, analyze=True, force_llm=False,
            transcript_mode="all", context_refresh_mode="audio")
        for before, after, expected in (("same", "same", 0), ("old", "new", 1)):
            with self.subTest(expected=expected), patch("api.crm_change_gate.transcript_signature", side_effect=[before, after]), \
                 patch("run_rop_assistant.run_command"), patch("run_rop_assistant.transcribe_missing_audio"), \
                 patch("run_rop_assistant.run_analysis", return_value=[]) as analyze:
                run_workflow(options)
                self.assertEqual(analyze.call_count, expected)

    def test_schema_ttl_and_entity_memo_do_not_hide_changes_between_jobs(self):
        first = ContextReadClient(self.h.client, db_path=self.h.db)
        first.safe_call("crm.contact.get", {"id": "20001"})
        first.safe_call("crm.contact.get", {"id": "20001"})
        second = ContextReadClient(self.h.client, db_path=self.h.db)
        second.safe_call("crm.contact.get", {"id": "20001"})
        self.assertEqual(self.h.remote.commands["crm.contact.get"], 2)

    def test_failed_invoice_is_not_cached_as_empty(self):
        from bitrix.context_sync import retain_failed_sources
        previous = {"ok": True, "items": [{"ID": "8", "PRICE": "500"}]}
        result = retain_failed_sources({"ok": False, "items": [], "error": "unavailable"}, previous)
        self.assertEqual(result["items"], previous["items"])
        self.assertFalse(result["refresh_ok"])

    def test_shared_contact_uses_each_deals_own_snapshot_version(self):
        payload_a = self.h.state()["payload"]
        payload_b = copy.deepcopy(payload_a)
        new_time = self.h.remote.now.isoformat()
        self.h.remote.contacts["20001"]["DATE_MODIFY"] = new_time
        payload_b["context"]["contacts"]["20001"]["response"]["result"]["DATE_MODIFY"] = new_time
        payload_b["customer_history"]["contacts"]["20001"]["response"]["result"]["DATE_MODIFY"] = new_time
        changes = probe_changes(self.h.client, {"first": payload_a, "second": payload_b})
        self.assertIn("linked_entity", changes["first"])
        self.assertNotIn("linked_entity", changes["second"])

    def test_idle_probe_window_follows_last_skip_probe(self):
        from bitrix.context_sync import parsed_at
        t0 = self.h.remote.now
        self.h.plans(now=t0)
        self.h.plans(now=t0 + timedelta(minutes=30))
        self.h.remote.reset_counts()
        later = t0 + timedelta(minutes=60)
        self.assertEqual(self.h.plans(now=later)[self.deal_id]["mode"], "skip")
        deal_filters = [row for row in self.h.remote.activity_filters if str(row.get("OWNER_TYPE_ID")) == "2"]
        self.assertTrue(deal_filters)
        since = parsed_at(deal_filters[0][">=LAST_UPDATED"])
        self.assertGreaterEqual(since, t0 + timedelta(minutes=10))
        self.assertLess(since, later)

    def test_stale_deal_probe_does_not_widen_fresh_deal_window(self):
        from bitrix.context_sync import parsed_at
        other = SnapshotHarness(2)
        self.addCleanup(other.close)
        other.initial()
        other.plans()
        stale_id, fresh_id = other.ids
        stale_at = (other.remote.now - timedelta(hours=3)).isoformat()
        probe = get_crm_sync_state(other.db, f"deal_probe:{stale_id}")
        put_crm_sync_state(other.db, f"deal_probe:{stale_id}", {"activity_probe_at": stale_at},
                           expected_revision=probe["revision"])
        state = other.state(stale_id)
        payload = copy.deepcopy(state["payload"])
        payload["context"]["sync"]["activity_cursor"] = stale_at
        payload["customer_history"]["sync"]["activity_cursor"] = stale_at
        put_crm_sync_state(other.db, f"deal_context:{stale_id}", payload, expected_revision=state["revision"])
        updated = get_crm_sync_state(other.db, f"deal_context:{stale_id}")
        ack = get_crm_sync_state(other.db, f"deal_ack:{stale_id}")
        ack_payload = copy.deepcopy(ack["payload"])
        ack_payload["context_revision"] = updated["revision"]
        put_crm_sync_state(other.db, f"deal_ack:{stale_id}", ack_payload, expected_revision=ack["revision"])
        other.remote.reset_counts()
        other.plans()
        deal_filters = [row for row in other.remote.activity_filters if str(row.get("OWNER_TYPE_ID")) == "2"]
        by_owner = {}
        for row in deal_filters:
            ids = row.get("OWNER_ID")
            ids = [ids] if not isinstance(ids, list) else ids
            for entity_id in ids:
                by_owner[str(entity_id)] = parsed_at(row[">=LAST_UPDATED"])
        self.assertLess(by_owner[stale_id], by_owner[fresh_id] - timedelta(hours=2))

    def test_missing_linked_contact_list_row_does_not_refresh(self):
        contact_id = next(iter(self.h.remote.contacts))
        del self.h.remote.contacts[contact_id]
        self.assertEqual(self.h.plans()[self.deal_id]["mode"], "skip")

    def test_deal_job_ack_requires_terminal_success_not_error(self):
        self.assertFalse(deal_job_can_acknowledge({}))
        self.assertFalse(deal_job_can_acknowledge(
            {"publish_ready": True, "status": "error", "decision_status": "error"}))
        self.assertTrue(deal_job_can_acknowledge(
            {"publish_ready": True, "status": "done", "decision_status": "skip"}))
        self.assertTrue(deal_job_can_acknowledge({"stage": "audio_idle", "status": "done"}))


if __name__ == "__main__":
    unittest.main()
