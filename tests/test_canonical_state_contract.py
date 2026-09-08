from __future__ import annotations

import copy
import importlib
import json
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "canonical_state" / "scenarios.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
OWNER = {"entity_type": "deal", "entity_id": "18733"}

try:
    canonical_state = importlib.import_module("bitrix.canonical_state")
except ModuleNotFoundError as error:
    if error.name != "bitrix.canonical_state":
        raise
    canonical_state = None


def walk(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


class CanonicalStateFixtureTests(unittest.TestCase):
    def test_fixture_covers_t01_through_t18(self):
        self.assertEqual(FIXTURE["scenario_ids"], [f"T{number:02d}" for number in range(1, 19)])

    def test_fixture_covers_p01_through_p05(self):
        self.assertEqual(FIXTURE["projection_ids"], [f"P{number:02d}" for number in range(1, 6)])

    def test_fixture_is_privacy_safe(self):
        for key, value in walk(FIXTURE):
            if key.upper() == "VALUE":
                self.assertIn(value, {"synthetic-contact-value-a", "synthetic-contact-value-b"})
            if not isinstance(value, str):
                continue
            self.assertNotIn("@", value)
            self.assertNotIn("auth=", value.lower())
            if value.startswith(("http://", "https://")):
                parsed = urlparse(value)
                self.assertEqual(parsed.hostname, "example.invalid")
                token = parse_qs(parsed.query).get("_efd", [])
                self.assertTrue(not token or token in (["AAA"], ["BBB"]))


@unittest.skipIf(canonical_state is None, "Stage 2C: bitrix.canonical_state is intentionally absent")
class CanonicalStateContractTests(unittest.TestCase):
    def activity(self, value, *, source="crm.activity.list"):
        return canonical_state.project_activity(copy.deepcopy(value), source=source)

    def merge(self, state, entities, *, source_status, observed_at=None):
        return canonical_state.merge_canonical_state(
            state,
            owner=OWNER,
            observed_at=observed_at or FIXTURE["observed_at"]["a"],
            source_status=source_status,
            entities=entities,
        )

    def one_entry(self, delta, change_type):
        self.assertEqual(len(delta["entries"]), 1, delta)
        entry = delta["entries"][0]
        self.assertEqual(entry["change_type"], change_type)
        return entry

    def email_with_files(self, files):
        value = copy.deepcopy(FIXTURE["email_efd"]["base"])
        value["FILES"] = copy.deepcopy(files)
        return value

    def test_owner_mismatch_is_rejected_without_mutating_state(self):
        entity = self.activity(self.email_with_files(FIXTURE["email_efd"]["files_a"]))
        state, _ = self.merge(None, [entity], source_status={"activities": "ok"})
        original = copy.deepcopy(state)
        with self.assertRaises(ValueError):
            canonical_state.merge_canonical_state(
                state,
                owner={"entity_type": "deal", "entity_id": "different"},
                observed_at=FIXTURE["observed_at"]["b"],
                source_status={"activities": "ok"},
                entities=[],
            )
        self.assertEqual(state, original)

    def test_t01_observed_at_does_not_break_idempotency(self):
        entity = self.activity(self.email_with_files(FIXTURE["email_efd"]["files_a"]))
        first, first_delta = self.merge(None, [entity], source_status={"activities": "ok"})
        second, second_delta = self.merge(
            first,
            [entity],
            source_status={"activities": "ok"},
            observed_at=FIXTURE["observed_at"]["b"],
        )
        self.assertEqual(self.one_entry(first_delta, "NEW")["before"], None)
        self.assertEqual(len(second["entities"]), 1)
        self.assertEqual(first["semantic_fingerprint"], second["semantic_fingerprint"])
        self.assertEqual(first["raw_fingerprint"], second["raw_fingerprint"])
        self.assertEqual(second_delta["entries"], [])

    def test_t02_efd_rotation_is_ignored(self):
        before = self.activity(self.email_with_files(FIXTURE["email_efd"]["files_a"]))
        after = self.activity(self.email_with_files(FIXTURE["email_efd"]["files_b"]))
        self.assertEqual(before["semantic"], FIXTURE["email_efd"]["expected_semantic"])
        self.assertEqual(before["metadata"], FIXTURE["email_efd"]["expected_metadata"])
        self.assertEqual(before["semantic_fingerprint"], FIXTURE["email_efd"]["expected_fingerprints"]["semantic"])
        self.assertEqual(before["raw_fingerprint"], FIXTURE["email_efd"]["expected_fingerprints"]["raw"])
        state, _ = self.merge(None, [before], source_status={"activities": "ok"})
        updated, delta = self.merge(state, [after], source_status={"activities": "ok"})
        self.assertEqual(before["semantic"]["file_ids"], ["964461"])
        self.assertEqual(before["semantic_fingerprint"], after["semantic_fingerprint"])
        self.assertEqual(before["raw_fingerprint"], after["raw_fingerprint"])
        self.assertEqual(state["raw_fingerprint"], updated["raw_fingerprint"])
        self.assertEqual(delta["entries"], [])

    def test_t03_file_order_is_ignored(self):
        before = self.activity(self.email_with_files(FIXTURE["file_order"]["a"]))
        after = self.activity(self.email_with_files(FIXTURE["file_order"]["b"]))
        self.assertEqual(canonical_state.normalize_file_ids(FIXTURE["file_order"]["a"]), ["964461", "964463"])
        self.assertEqual(before["semantic"]["file_ids"], after["semantic"]["file_ids"])
        self.assertEqual(before["semantic_fingerprint"], after["semantic_fingerprint"])
        self.assertEqual(before["raw_fingerprint"], after["raw_fingerprint"])
        state, _ = self.merge(None, [before], source_status={"activities": "ok"})
        _, delta = self.merge(state, [after], source_status={"activities": "ok"})
        self.assertEqual(delta["entries"], [])

    def test_t04_recording_replacement_is_meaningful_revision(self):
        fixture = FIXTURE["recording_revision"]
        state, _ = self.merge(None, [self.activity(fixture["before"])], source_status={"activities": "ok"})
        updated, delta = self.merge(state, [self.activity(fixture["after"])], source_status={"activities": "ok"})
        entry = self.one_entry(delta, "UPDATED_MEANINGFUL")
        self.assertEqual(entry["key"], "activity:663127")
        self.assertIn("file_ids", entry["changed_semantic_fields"])
        self.assertEqual(updated["entities"]["activity:663127"]["semantic"]["file_ids"], ["1012215"])

    def test_t05_call_completion_is_meaningful_revision(self):
        fixture = FIXTURE["call_completion"]
        state, _ = self.merge(None, [self.activity(fixture["before"])], source_status={"activities": "ok"})
        _, delta = self.merge(state, [self.activity(fixture["after"])], source_status={"activities": "ok"})
        entry = self.one_entry(delta, "UPDATED_MEANINGFUL")
        self.assertEqual(entry["key"], "activity:663125")
        self.assertEqual(set(entry["changed_semantic_fields"]), {"completed", "status"})
        self.assertIn("last_updated", entry["changed_metadata_fields"])

    def test_t06_last_updated_only_is_technical_revision(self):
        before_raw = copy.deepcopy(FIXTURE["call_completion"]["after"])
        after_raw = copy.deepcopy(before_raw)
        after_raw["LAST_UPDATED"] = "2026-09-07T12:22:00+03:00"
        before, after = self.activity(before_raw), self.activity(after_raw)
        self.assertEqual(before["semantic_fingerprint"], after["semantic_fingerprint"])
        self.assertNotEqual(before["raw_fingerprint"], after["raw_fingerprint"])
        state, _ = self.merge(None, [before], source_status={"activities": "ok"})
        _, delta = self.merge(state, [after], source_status={"activities": "ok"})
        entry = self.one_entry(delta, "UPDATED_TECHNICAL")
        self.assertEqual(entry["changed_semantic_fields"], [])
        self.assertIn("last_updated", entry["changed_metadata_fields"])

    def test_t07_deal_metadata_only_is_technical_revision(self):
        fixture = FIXTURE["deal_metadata"]
        before = canonical_state.project_deal(copy.deepcopy(fixture["before"]))
        after = canonical_state.project_deal(copy.deepcopy(fixture["after"]))
        self.assertEqual(before["semantic_fingerprint"], after["semantic_fingerprint"])
        self.assertNotEqual(before["raw_fingerprint"], after["raw_fingerprint"])
        state, _ = self.merge(None, [before], source_status={"deal": "ok"})
        _, delta = self.merge(state, [after], source_status={"deal": "ok"})
        entry = self.one_entry(delta, "UPDATED_TECHNICAL")
        self.assertEqual(entry["changed_semantic_fields"], [])
        self.assertEqual(set(entry["changed_metadata_fields"]), {"date_modify", "modify_by_id"})

    def test_t08_new_email_is_new(self):
        entity = self.activity(FIXTURE["email_maturation"]["before"])
        state, delta = self.merge(None, [entity], source_status={"activities": "ok"})
        entry = self.one_entry(delta, "NEW")
        self.assertEqual(entry["key"], "activity:662983")
        self.assertIsNone(entry["before"])
        self.assertEqual(entry["after"], entity["semantic"])
        self.assertIn("activity:662983", state["entities"])

    def test_t09_existing_email_matures_as_revision(self):
        fixture = FIXTURE["email_maturation"]
        state, _ = self.merge(None, [self.activity(fixture["before"])], source_status={"activities": "ok"})
        updated, delta = self.merge(state, [self.activity(fixture["after"])], source_status={"activities": "ok"})
        entry = self.one_entry(delta, "UPDATED_MEANINGFUL")
        self.assertEqual(entry["key"], "activity:662983")
        self.assertEqual(set(entry["changed_semantic_fields"]), {"completed", "status"})
        self.assertIn("last_updated", entry["changed_metadata_fields"])
        self.assertEqual(list(updated["entities"]).count("activity:662983"), 1)

    def test_t10_timeline_comment_keeps_identity_on_revision(self):
        fixture = FIXTURE["timeline_revision"]
        before = canonical_state.project_timeline_comment(copy.deepcopy(fixture["before"]))
        after = canonical_state.project_timeline_comment(copy.deepcopy(fixture["after"]))
        state, _ = self.merge(None, [before], source_status={"timeline_comments": "ok"})
        updated, delta = self.merge(state, [after], source_status={"timeline_comments": "ok"})
        entry = self.one_entry(delta, "UPDATED_MEANINGFUL")
        self.assertEqual(entry["key"], "timeline_comment:2910619")
        self.assertEqual(entry["changed_semantic_fields"], ["comment_hash"])
        self.assertEqual(updated["derived"]["communications_index"]["crm_timeline_comment:2910619"], entry["key"])

    def test_t11_task_namespaces_both_keep_identity(self):
        fixture = FIXTURE["task_deadline"]
        before = [
            canonical_state.project_task(copy.deepcopy(fixture["task_before"])),
            self.activity(fixture["activity_before"]),
        ]
        after = [
            canonical_state.project_task(copy.deepcopy(fixture["task_after"])),
            self.activity(fixture["activity_after"]),
        ]
        statuses = {"tasks": "ok", "activities": "ok"}
        state, _ = self.merge(None, before, source_status=statuses)
        updated, delta = self.merge(state, after, source_status=statuses)
        self.assertEqual(set(updated["entities"]), {"task:49769", "activity:627763"})
        self.assertEqual({entry["change_type"] for entry in delta["entries"]}, {"UPDATED_MEANINGFUL"})
        self.assertEqual({entry["key"] for entry in delta["entries"]}, {"task:49769", "activity:627763"})
        for entry in delta["entries"]:
            self.assertIn("deadline", entry["changed_semantic_fields"])

    def test_t12_repeated_overlap_is_idempotent(self):
        entity = self.activity(FIXTURE["email_maturation"]["before"])
        state, _ = self.merge(None, [entity], source_status={"activities": "ok"})
        once, once_delta = self.merge(state, [self.activity(FIXTURE["email_maturation"]["after"])], source_status={"activities": "ok"})
        twice, twice_delta = self.merge(once, [self.activity(FIXTURE["email_maturation"]["after"])], source_status={"activities": "ok"})
        self.one_entry(once_delta, "UPDATED_MEANINGFUL")
        self.assertEqual(len(twice["entities"]), 1)
        self.assertEqual(twice_delta["entries"], [])

    def test_t13_incomplete_incremental_does_not_delete(self):
        fixture = FIXTURE["incremental_set"]
        initial = [self.activity(item) for item in fixture["initial"]]
        state, _ = self.merge(None, initial, source_status={"activities": "ok"})
        updated, delta = self.merge(
            state,
            [self.activity(item) for item in fixture["only_b_updated"]],
            source_status={"activities": "ok"},
        )
        self.assertEqual(set(updated["entities"]), {"activity:700001", "activity:700002", "activity:700003"})
        self.assertEqual(self.one_entry(delta, "UPDATED_MEANINGFUL")["key"], "activity:700002")
        self.assertNotIn("REMOVED", {entry["change_type"] for entry in delta["entries"]})

    def test_t14_failed_source_retains_previous_entities(self):
        initial = [self.activity(item) for item in FIXTURE["incremental_set"]["initial"]]
        state, _ = self.merge(None, initial, source_status={"activities": "ok"})
        updated, delta = self.merge(state, [], source_status={"activities": "failed"})
        self.assertEqual(updated["entities"], state["entities"])
        self.assertEqual(updated["source_status"]["activities"], "failed")
        self.assertEqual(delta["entries"], [])

    def test_t15_full_and_accumulated_incremental_converge(self):
        fixture = FIXTURE["full_convergence"]
        self.assertEqual(fixture["audit_manifest"]["missing_objects"], 0)
        self.assertEqual(fixture["audit_manifest"]["extra_objects"], 0)
        self.assertEqual(len(fixture["audit_manifest"]["url_only_activity_ids"]), 9)
        incremental_entities = [self.activity(item) for item in fixture["incremental"]]
        full_entities = [self.activity(item) for item in fixture["full"]]
        incremental, _ = self.merge(None, incremental_entities, source_status={"activities": "ok"})
        full, _ = self.merge(
            None,
            full_entities,
            source_status={"activities": "ok"},
            observed_at=FIXTURE["observed_at"]["b"],
        )
        self.assertEqual(set(incremental["entities"]), set(full["entities"]))
        self.assertEqual(incremental["semantic_fingerprint"], full["semantic_fingerprint"])
        self.assertEqual(incremental["raw_fingerprint"], full["raw_fingerprint"])
        _, delta = self.merge(incremental, full_entities, source_status={"activities": "ok"})
        self.assertEqual(delta["entries"], [])

    def test_t16_activity_get_is_rejected_as_canonical_source(self):
        raw = self.email_with_files(FIXTURE["email_efd"]["files_a"])
        listed = self.activity(raw, source="crm.activity.list")
        self.assertEqual(listed["key"], "activity:627981")
        with self.assertRaises(ValueError):
            self.activity(raw, source="crm.activity.get")

    def test_t17_subtype_is_not_identity(self):
        fixture = FIXTURE["call_completion"]
        before, after = self.activity(fixture["before"]), self.activity(fixture["after"])
        self.assertEqual(canonical_state.activity_subtype(fixture["before"]), "call")
        self.assertEqual(before["subtype"], "call")
        self.assertEqual(after["subtype"], "call")
        self.assertEqual(before["key"], canonical_state.canonical_key("activity", "663125"))
        self.assertEqual(before["key"], after["key"])
        state, _ = self.merge(None, [before], source_status={"activities": "ok"})
        updated, delta = self.merge(state, [after], source_status={"activities": "ok"})
        self.assertEqual(len(updated["entities"]), 1)
        self.one_entry(delta, "UPDATED_MEANINGFUL")

    def test_t18_unproven_file_metadata_is_ignored(self):
        fixture = FIXTURE["files_metadata"]
        before = self.activity(self.email_with_files(fixture["before"]))
        after = self.activity(self.email_with_files(fixture["after"]))
        self.assertEqual(before["semantic"]["file_ids"], ["964461"])
        self.assertEqual(before["semantic_fingerprint"], after["semantic_fingerprint"])
        self.assertEqual(before["raw_fingerprint"], after["raw_fingerprint"])
        state, _ = self.merge(None, [before], source_status={"activities": "ok"})
        _, delta = self.merge(state, [after], source_status={"activities": "ok"})
        self.assertEqual(delta["entries"], [])

    def test_p01_deal_semantic_projection_and_stage_revision(self):
        fixture = FIXTURE["deal_projection"]
        before = canonical_state.project_deal(copy.deepcopy(fixture["before"]))
        after = canonical_state.project_deal(copy.deepcopy(fixture["stage_changed"]))
        self.assertEqual(before["key"], "deal:990001")
        self.assertEqual(before["semantic"], fixture["expected_semantic"])
        self.assertEqual(before["metadata"], fixture["expected_metadata"])
        state, _ = self.merge(None, [before], source_status={"deal": "ok"})
        _, delta = self.merge(state, [after], source_status={"deal": "ok"})
        entry = self.one_entry(delta, "UPDATED_MEANINGFUL")
        self.assertEqual(entry["changed_semantic_fields"], ["stage_id"])

    def test_p02_activity_projection_privacy_order_and_end_time_revision(self):
        fixture = FIXTURE["activity_projection"]
        raw = copy.deepcopy(fixture["before"])
        before = self.activity(raw)
        self.assertEqual(before["semantic"], fixture["expected_semantic"])
        self.assertTrue(before["semantic"]["missed_call"])
        self.assertNotIn("VALUE", {key.upper() for key, _ in walk(before)})
        self.assertNotIn("synthetic-contact-value", json.dumps(before, ensure_ascii=False))

        reordered_raw = copy.deepcopy(raw)
        reordered_raw["COMMUNICATIONS"].reverse()
        reordered = self.activity(reordered_raw)
        self.assertEqual(before["semantic_fingerprint"], reordered["semantic_fingerprint"])
        self.assertEqual(before["raw_fingerprint"], reordered["raw_fingerprint"])

        end_changed_raw = copy.deepcopy(raw)
        end_changed_raw["END_TIME"] = "2026-09-07T10:06:00+03:00"
        end_changed = self.activity(end_changed_raw)
        state, _ = self.merge(None, [before], source_status={"activities": "ok"})
        _, delta = self.merge(state, [end_changed], source_status={"activities": "ok"})
        entry = self.one_entry(delta, "UPDATED_MEANINGFUL")
        self.assertEqual(entry["changed_semantic_fields"], ["end_time"])

    def test_p03_task_projection_semantic_and_technical_revisions(self):
        fixture = FIXTURE["task_projection"]
        before_raw = copy.deepcopy(fixture["before"])
        before = canonical_state.project_task(before_raw)
        self.assertEqual(before["key"], "task:990401")
        self.assertEqual(before["semantic"], fixture["expected_semantic"])
        self.assertEqual(before["metadata"], fixture["expected_metadata"])

        status_changed_raw = copy.deepcopy(before_raw)
        status_changed_raw["status"] = "3"
        status_changed = canonical_state.project_task(status_changed_raw)
        state, _ = self.merge(None, [before], source_status={"tasks": "ok"})
        _, delta = self.merge(state, [status_changed], source_status={"tasks": "ok"})
        entry = self.one_entry(delta, "UPDATED_MEANINGFUL")
        self.assertEqual(entry["changed_semantic_fields"], ["status"])

        changed_date_raw = copy.deepcopy(before_raw)
        changed_date_raw["changedDate"] = "2026-09-07T11:05:00+03:00"
        changed_date = canonical_state.project_task(changed_date_raw)
        self.assertEqual(before["semantic_fingerprint"], changed_date["semantic_fingerprint"])
        self.assertNotEqual(before["raw_fingerprint"], changed_date["raw_fingerprint"])
        _, delta = self.merge(state, [changed_date], source_status={"tasks": "ok"})
        entry = self.one_entry(delta, "UPDATED_TECHNICAL")
        self.assertEqual(entry["changed_semantic_fields"], [])
        self.assertEqual(entry["changed_metadata_fields"], ["changed_date"])

    def test_p04_timeline_projection_ignores_file_transport_metadata(self):
        fixture = FIXTURE["timeline_projection"]
        before_raw = copy.deepcopy(fixture["before"])
        before = canonical_state.project_timeline_comment(before_raw)
        self.assertEqual(before["key"], "timeline_comment:990501")
        self.assertEqual(before["semantic"], fixture["expected_semantic"])

        transport_changed_raw = copy.deepcopy(before_raw)
        transport_changed_raw["FILES"].reverse()
        for file_value in transport_changed_raw["FILES"]:
            file_value["url"] = file_value["url"].replace("_efd=AAA", "_efd=BBB")
            file_value["name"] = "changed.bin"
            file_value["size"] = 999
        transport_changed = canonical_state.project_timeline_comment(transport_changed_raw)
        self.assertEqual(before["semantic_fingerprint"], transport_changed["semantic_fingerprint"])
        self.assertEqual(before["raw_fingerprint"], transport_changed["raw_fingerprint"])

    def test_p05_im_message_projection_and_text_revision(self):
        fixture = FIXTURE["im_message_projection"]
        before = canonical_state.project_im_message(copy.deepcopy(fixture["before"]))
        after = canonical_state.project_im_message(copy.deepcopy(fixture["text_changed"]))
        self.assertEqual(before["key"], "im_message:990601")
        self.assertEqual(before["semantic"], fixture["expected_semantic"])
        self.assertEqual(after["key"], before["key"])
        state, _ = self.merge(None, [before], source_status={"im_messages": "ok"})
        _, delta = self.merge(state, [after], source_status={"im_messages": "ok"})
        entry = self.one_entry(delta, "UPDATED_MEANINGFUL")
        self.assertEqual(entry["key"], "im_message:990601")
        self.assertEqual(entry["changed_semantic_fields"], ["text_hash"])


if __name__ == "__main__":
    unittest.main()
