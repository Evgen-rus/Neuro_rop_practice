from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from bitrix import canonical_state


ROOT = Path(__file__).parents[1]
AUDIT = json.loads((ROOT / "Docs" / "final_audit.json").read_text(encoding="utf-8"))
AUDIT_1_5 = json.loads(
    (ROOT / "Docs" / "final_audit_stage_1_5.json").read_text(encoding="utf-8")
)
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "canonical_state" / "scenarios.json").read_text(
        encoding="utf-8"
    )
)
OWNER = {"entity_type": "deal", "entity_id": "18733"}
ACTIVITY_FIELDS = {
    "ID",
    "TYPE_ID",
    "PROVIDER_ID",
    "PROVIDER_TYPE_ID",
    "ORIGIN_ID",
    "START_TIME",
    "END_TIME",
    "DEADLINE",
    "COMPLETED",
    "STATUS",
    "DIRECTION",
    "RESPONSIBLE_ID",
    "OWNER_TYPE_ID",
    "OWNER_ID",
    "LAST_UPDATED",
    "CREATED",
    "AUTHOR_ID",
    "EDITOR_ID",
    "SETTINGS",
}


def project_audit_activity(payload, *, files=None):
    raw = {key: copy.deepcopy(value) for key, value in payload.items() if key in ACTIVITY_FIELDS}
    file_ids = payload.get("file_ids", [])
    raw["FILES"] = copy.deepcopy(files) if files is not None else [{"id": value} for value in file_ids]
    return canonical_state.project_activity(raw)


def merge(state, entities, *, observed_at="2026-09-07T10:00:00+03:00", source_status=None):
    return canonical_state.merge_canonical_state(
        state,
        owner=OWNER,
        observed_at=observed_at,
        source_status=source_status or {"activities": "ok"},
        entities=entities,
    )


class CanonicalStateAuditReplayTests(unittest.TestCase):
    def test_files_efd_variability_replays_as_unchanged(self):
        historical = AUDIT_1_5["files_experiment"]["raw_variability"][
            "historical_full_snapshots_A_B_Z"
        ]
        self.assertTrue(historical["all_id_only_stable"])
        self.assertTrue(historical["all_semantic_stable"])
        self.assertTrue(historical["all_raw_changed"])

        audit_ids = [item["activity_id"] for item in historical["per_activity"]]
        self.assertEqual(audit_ids, FIXTURE["full_convergence"]["audit_manifest"]["url_only_activity_ids"])
        for item in AUDIT_1_5["files_experiment"]["activities"]:
            with self.subTest(activity_id=item["activity_id"]):
                replay = item["live_list"]
                base = {"ID": item["activity_id"], "TYPE_ID": "4", "PROVIDER_ID": "EMAIL"}
                before = project_audit_activity(base, files=replay["files_before"])
                after = project_audit_activity(base, files=replay["files_after"])
                self.assertEqual(before["semantic_fingerprint"], after["semantic_fingerprint"])
                self.assertEqual(before["raw_fingerprint"], after["raw_fingerprint"])
                state, _ = merge(None, [before])
                _, delta = merge(state, [after])
                self.assertEqual(delta["entries"], [])

    def test_call_completion_replays_as_same_meaningful_entity(self):
        observations = {
            item["label"]: item["list_summary"]
            for item in AUDIT_1_5["call_experiment"]["observations"]
        }
        self.assertEqual(
            AUDIT_1_5["call_experiment"]["changed_fields_over_time"]["T1_to_T2"],
            ["COMPLETED", "STATUS", "LAST_UPDATED"],
        )
        before = project_audit_activity(observations["T1"])
        after = project_audit_activity(observations["T2"])
        state, _ = merge(None, [before])
        _, delta = merge(state, [after])
        self.assertEqual(before["key"], after["key"])
        self.assertEqual(delta["entries"][0]["change_type"], "UPDATED_MEANINGFUL")
        self.assertEqual(delta["entries"][0]["changed_semantic_fields"], ["completed", "status"])
        self.assertEqual(delta["entries"][0]["changed_metadata_fields"], ["last_updated"])

    def test_recording_replacement_replays_as_file_revision(self):
        recording = AUDIT_1_5["call_experiment"]["recording_behavior"][
            "answered_call_file_replacement_663127"
        ]
        self.assertTrue(recording["activity_id_stable"])
        self.assertTrue(recording["origin_id_stable"])
        self.assertTrue(recording["file_ids_changed"])

        def projected(version):
            raw = copy.deepcopy(recording[version])
            raw.update(
                {
                    "ID": recording["activity_id"],
                    "TYPE_ID": "2",
                    "PROVIDER_ID": "VOXIMPLANT_CALL",
                    "OWNER_TYPE_ID": recording["owner_type_id"],
                    "OWNER_ID": recording["owner_id"],
                }
            )
            return project_audit_activity(raw)

        before, after = projected("t_early"), projected("t_later")
        state, _ = merge(None, [before])
        _, delta = merge(state, [after])
        self.assertEqual(before["key"], after["key"])
        self.assertEqual(delta["entries"][0]["change_type"], "UPDATED_MEANINGFUL")
        self.assertEqual(delta["entries"][0]["changed_semantic_fields"], ["file_ids"])

    def test_email_new_then_revision_replays_by_stable_id(self):
        revisions = AUDIT["revision_histories"]["email:662983"]["revisions"]
        self.assertEqual(revisions[0]["fields_changed_from_previous"], ["NEW"])
        self.assertEqual(
            revisions[1]["fields_changed_from_previous"],
            ["COMPLETED", "LAST_UPDATED", "STATUS"],
        )
        before = project_audit_activity(revisions[0]["payload"])
        after = project_audit_activity(revisions[1]["payload"])
        state, delta = merge(None, [before])
        self.assertEqual(delta["entries"][0]["change_type"], "NEW")
        _, delta = merge(state, [after])
        self.assertEqual(before["key"], after["key"])
        self.assertEqual(delta["entries"][0]["change_type"], "UPDATED_MEANINGFUL")
        self.assertEqual(delta["entries"][0]["changed_semantic_fields"], ["completed", "status"])

    def test_timeline_revision_replays_by_stable_id(self):
        revisions = AUDIT["revision_histories"]["timeline_comment:2910619"]["revisions"]

        def projected(revision):
            payload = revision["payload"]
            return canonical_state.project_timeline_comment(
                {
                    "ID": payload["ID"],
                    "CREATED": payload["CREATED"],
                    "AUTHOR_ID": payload["AUTHOR_ID"],
                    "COMMENT": payload["comment_hash"],
                }
            )

        before, after = map(projected, revisions)
        state, _ = merge(None, [before], source_status={"timeline_comments": "ok"})
        _, delta = merge(state, [after], source_status={"timeline_comments": "ok"})
        self.assertEqual(before["key"], after["key"])
        self.assertEqual(delta["entries"][0]["change_type"], "UPDATED_MEANINGFUL")
        self.assertEqual(delta["entries"][0]["changed_semantic_fields"], ["comment_hash"])

    def test_task_deadline_revisions_replay_in_both_namespaces(self):
        task_revisions = AUDIT["revision_histories"]["task:49769"]["revisions"]
        task_entities = []
        for revision in task_revisions:
            payload = revision["payload"]
            task_entities.append(
                canonical_state.project_task(
                    {
                        "id": payload["id"],
                        "status": payload["status"],
                        "deadline": payload["deadline"],
                        "closedDate": payload["closedDate"],
                        "responsibleId": payload["responsibleId"],
                        "changedDate": payload["changedDate"],
                    }
                )
            )
        state, _ = merge(None, [task_entities[0]], source_status={"tasks": "ok"})
        _, delta = merge(state, [task_entities[1]], source_status={"tasks": "ok"})
        self.assertEqual(delta["entries"][0]["changed_semantic_fields"], ["deadline"])
        self.assertEqual(delta["entries"][0]["changed_metadata_fields"], ["changed_date"])

        activity_revisions = AUDIT["revision_histories"]["task:627763"]["revisions"]
        before, after = [project_audit_activity(item["payload"]) for item in activity_revisions]
        state, _ = merge(None, [before])
        _, delta = merge(state, [after])
        self.assertEqual(before["key"], after["key"])
        self.assertEqual(
            delta["entries"][0]["changed_semantic_fields"], ["deadline", "end_time"]
        )

    def test_accumulated_incremental_and_final_full_replay_converge(self):
        comparison = AUDIT["incremental_vs_final_full"]
        self.assertEqual(comparison["from"], "snapshot_C_018_incremental")
        self.assertEqual(comparison["to"], "snapshot_Z_final_full")
        self.assertEqual(comparison["left_only"], [])
        self.assertEqual(comparison["right_only"], [])
        self.assertEqual({tuple(item["changed_fields"]) for item in comparison["changes"]}, {("files_hash",)})

        files_by_id = {
            item["activity_id"]: item["live_list"]
            for item in AUDIT_1_5["files_experiment"]["activities"]
        }
        accumulated = []
        full = []
        for change in comparison["changes"]:
            activity_id = change["stable_id"]
            files = files_by_id[activity_id]
            accumulated.append(
                project_audit_activity(
                    change["before"], files=files["hashes_before"]["semantic_projection"]
                )
            )
            full.append(
                project_audit_activity(
                    change["after"], files=files["hashes_after"]["semantic_projection"]
                )
            )

        accumulated_state, _ = merge(None, accumulated)
        full_state, _ = merge(None, full, observed_at="2026-09-07T10:05:00+03:00")
        self.assertEqual(set(accumulated_state["entities"]), set(full_state["entities"]))
        self.assertEqual(
            accumulated_state["semantic_fingerprint"], full_state["semantic_fingerprint"]
        )
        self.assertEqual(accumulated_state["raw_fingerprint"], full_state["raw_fingerprint"])
        _, delta = merge(accumulated_state, full)
        self.assertEqual(delta["entries"], [])


if __name__ == "__main__":
    unittest.main()
