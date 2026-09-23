import json
import unittest
from pathlib import Path

from src.recall_reverse_disposition import (
    build_closure_report,
    check_minimal_disclosure,
    evaluate_closure,
    fold_recall_case,
    global_conservation_view,
    plan_tasks,
    quantity_discrepancies,
    state_snapshot,
    unconfirmed_receipts_view,
    validate_recall_event,
)

DATA = Path(__file__).parents[1] / "data" / "recall_case.json"


def load_log():
    return json.loads(DATA.read_text(encoding="utf-8"))


def make_event(event_id, kind, occurred_at, subject_id, payload):
    return {
        "event_id": event_id,
        "kind": kind,
        "occurred_at": occurred_at,
        "subject_id": subject_id,
        "payload": payload,
    }


def resolution_events():
    """样例日志之后的结案补齐事件：差异授权说明 + C-7 任务回执与完成。"""
    return [
        make_event("fix-001", "QUANTITY_DISCREPANCY_RESOLVED", "2026-09-24T18:00:00+08:00", "R-041", {
            "recall_id": "R-041",
            "subject_ref": "C-5",
            "explanation": "退运途中破损1支，现场记录已归档",
            "authorized_by": "CQ-LEAD-01",
        }),
        make_event("fix-002", "DISPOSITION_TASK_ACKED", "2026-09-24T18:10:00+08:00",
                   "N-2026-041:C-7:QUARANTINE_CONFIRM", {
                       "task_id": "N-2026-041:C-7:QUARANTINE_CONFIRM",
                       "receipt_channel": "ONLINE",
                       "actor_node_id": "SITE-EAST",
                       "actor_role": "SITE_OPERATOR",
                   }),
        make_event("fix-003", "DISPOSITION_TASK_COMPLETED", "2026-09-24T18:20:00+08:00",
                   "N-2026-041:C-7:QUARANTINE_CONFIRM", {
                       "task_id": "N-2026-041:C-7:QUARANTINE_CONFIRM",
                       "quantity_handled": 7,
                       "actor_node_id": "SITE-EAST",
                       "actor_role": "SITE_OPERATOR",
                   }),
        make_event("fix-004", "DISPOSITION_TASK_ACKED", "2026-09-24T18:30:00+08:00",
                   "N-2026-041:C-7:DESTRUCTION", {
                       "task_id": "N-2026-041:C-7:DESTRUCTION",
                       "receipt_channel": "ONLINE",
                       "actor_node_id": "SITE-EAST",
                       "actor_role": "SITE_OPERATOR",
                   }),
        make_event("fix-005", "DISPOSITION_TASK_COMPLETED", "2026-09-24T18:40:00+08:00",
                   "N-2026-041:C-7:DESTRUCTION", {
                       "task_id": "N-2026-041:C-7:DESTRUCTION",
                       "quantity_handled": 7,
                       "actor_node_id": "SITE-EAST",
                       "actor_role": "SITE_OPERATOR",
                   }),
        make_event("fix-006", "RECALL_CLOSURE_REQUESTED", "2026-09-24T19:00:00+08:00", "R-041", {
            "recall_id": "R-041",
            "requested_by": "CQ-LEAD-01",
        }),
    ]


def mini_log(objects):
    """最小可用日志：通知 + 发起召回。"""
    return [
        make_event("m-001", "CONTAMINATION_NOTICE_RECEIVED", "2026-09-23T08:00:00+08:00", "N-1", {
            "notice_id": "N-1", "source": "CENTRAL-LAB",
            "notice_revision": 1, "content_digest": "dg-1",
        }),
        make_event("m-002", "RECALL_ACTION_OPENED", "2026-09-23T09:00:00+08:00", "R-1", {
            "recall_id": "R-1", "notice_id": "N-1", "notice_revision": 1,
            "content_digest": "dg-1", "scope": {"kind": "LOT", "refs": ["L-1"]},
            "disposition_policy": "DESTROY", "objects": objects,
        }),
    ]


class SampleLogTest(unittest.TestCase):
    def test_sample_events_match_format_contract(self):
        for record in load_log():
            self.assertEqual(validate_recall_event(record), [], record["event_id"])

    def test_replayed_notice_produces_single_task_set(self):
        state = fold_recall_case(load_log())
        # 通知重放（rc-041-003）不产生重复任务：v1 七对象 14 项 + v2 新增 C-7 两项。
        self.assertEqual(len(state.tasks), 16)
        self.assertEqual(len(state.rejected_events), 1)
        self.assertEqual(state.rejected_events[0]["event_id"], "rc-041-006")
        self.assertIn("PENDING_REVIEW", state.rejected_events[0]["problems"])

    def test_changed_notice_waits_for_authorized_review(self):
        log = load_log()
        pending = fold_recall_case(log[:6])
        self.assertEqual(pending.status, "PENDING_REVIEW")
        # 复核期间不得下发新任务。
        self.assertNotIn("N-2026-041:C-8:INTERCEPT_IN_TRANSIT", pending.tasks)
        resolved = fold_recall_case(log)
        self.assertEqual(resolved.status, "OPEN")
        self.assertEqual(resolved.accepted_notice_revision, 2)

    def test_review_resolved_without_scope_change(self):
        log = mini_log([{
            "subject_ref": "C-1", "situation": "IN_STOCK", "custody_node_id": "SITE-NORTH",
            "quantity": 2, "due_at": "2026-09-25T18:00:00+08:00", "lot": "L-1",
        }])
        log.append(make_event("m-003", "CONTAMINATION_NOTICE_RECEIVED", "2026-09-23T10:00:00+08:00", "N-1", {
            "notice_id": "N-1", "source": "CENTRAL-LAB",
            "notice_revision": 2, "content_digest": "dg-2",
        }))
        log.append(make_event("m-004", "RECALL_REVIEW_RESOLVED", "2026-09-23T11:00:00+08:00", "R-1", {
            "recall_id": "R-1", "resolution": "SCOPE_UNCHANGED", "authorized_by": "CQ-LEAD-01",
        }))
        state = fold_recall_case(log)
        self.assertEqual(state.status, "OPEN")
        self.assertEqual(state.accepted_notice_revision, 2)
        self.assertEqual(state.scope_version, 1)

    def test_scope_expansion_adds_new_objects(self):
        state = fold_recall_case(load_log())
        self.assertEqual(state.scope_version, 2)
        self.assertIn("C-7", state.scope_refs)
        c7 = state.tasks["N-2026-041:C-7:QUARANTINE_CONFIRM"]
        self.assertEqual(c7.scope_version, 2)

    def test_scope_reduction_keeps_manual_hold(self):
        state = fold_recall_case(load_log())
        self.assertNotIn("C-3", state.scope_refs)
        # 自动任务被取消，人工隔离不自动解封。
        self.assertEqual(state.tasks["N-2026-041:C-3:QUARANTINE_CONFIRM"].status, "CANCELLED")
        self.assertEqual(state.tasks["N-2026-041:C-3:DESTRUCTION"].cancel_reason, "SCOPE_REDUCED")
        self.assertFalse(state.manual_holds["H-041-1"]["released"])

    def test_custody_scoping_rejects_other_nodes_operator(self):
        log = load_log() + [make_event(
            "x-001", "DISPOSITION_TASK_ACKED", "2026-09-24T17:30:00+08:00",
            "N-2026-041:C-7:QUARANTINE_CONFIRM", {
                "task_id": "N-2026-041:C-7:QUARANTINE_CONFIRM",
                "receipt_channel": "ONLINE",
                "actor_node_id": "SITE-NORTH",  # C-7 由 SITE-EAST 保管
                "actor_role": "SITE_OPERATOR",
            })]
        state = fold_recall_case(log)
        self.assertEqual(state.tasks["N-2026-041:C-7:QUARANTINE_CONFIRM"].status, "ISSUED")
        self.assertIn("CUSTODY_VIOLATION", state.rejected_events[-1]["problems"])

    def test_central_quality_sees_global_views(self):
        state = fold_recall_case(load_log())
        central = global_conservation_view(state, actor_role="CENTRAL_QUALITY", actor_node_id="CENTRAL-QA")
        self.assertEqual({row["custody_node_id"] for row in central}, {"SITE-NORTH", "SITE-EAST"})
        site = global_conservation_view(state, actor_role="SITE_OPERATOR", actor_node_id="SITE-NORTH")
        self.assertTrue(site)
        self.assertEqual({row["custody_node_id"] for row in site}, {"SITE-NORTH"})

    def test_unconfirmed_receipts_and_cross_timezone_overdue(self):
        state = fold_recall_case(load_log())
        before = unconfirmed_receipts_view(
            state, actor_role="CENTRAL_QUALITY", actor_node_id="CENTRAL-QA",
            now="2026-09-24T17:30:00+08:00")
        self.assertEqual(
            {row["task_id"] for row in before},
            {"N-2026-041:C-7:QUARANTINE_CONFIRM", "N-2026-041:C-7:DESTRUCTION"})
        self.assertFalse(any(row["overdue"] for row in before))
        # C-7 截止于 2026-09-26T09:00:00-05:00，即 UTC 14:00；跨时区比较须按实际时刻。
        after = unconfirmed_receipts_view(
            state, actor_role="CENTRAL_QUALITY", actor_node_id="CENTRAL-QA",
            now="2026-09-26T15:00:00+00:00")
        self.assertTrue(all(row["overdue"] for row in after))

    def test_partial_failure_retry_keeps_single_task(self):
        state = fold_recall_case(load_log())
        task = state.tasks["N-2026-041:C-2:RETURN_SHIPMENT"]
        self.assertEqual(task.attempt, 2)
        self.assertEqual(task.status, "COMPLETED")
        self.assertEqual(task.quantity_handled, 12)
        retries = [t for t in state.tasks.values() if t.subject_ref == "C-2" and t.task_kind == "RETURN_SHIPMENT"]
        self.assertEqual(len(retries), 1)

    def test_offline_receipt_recorded(self):
        state = fold_recall_case(load_log())
        task = state.tasks["N-2026-041:C-2:INTERCEPT_IN_TRANSIT"]
        self.assertEqual(task.receipt_channel, "OFFLINE")

    def test_restart_replay_never_duplicates_destruction_or_replacement(self):
        log = load_log()
        once = fold_recall_case(log)
        twice = fold_recall_case(log + log)
        self.assertEqual(state_snapshot(once), state_snapshot(twice))
        destructions = [
            t for t in twice.tasks.values()
            if t.task_kind == "DESTRUCTION" and t.subject_ref == "C-1" and t.status == "COMPLETED"
        ]
        self.assertEqual(len(destructions), 1)
        self.assertEqual(len(twice.replacements), 1)

    def test_closure_blocked_stays_prominent(self):
        state = fold_recall_case(load_log())
        self.assertEqual(state.status, "OPEN")
        reasons = {item["reason"] for item in state.closure_blocked_reasons}
        self.assertEqual(
            reasons, {"UNACKNOWLEDGED_RECEIPTS", "UNFINISHED_TASKS", "QUANTITY_DISCREPANCY"})
        discrepancy = [
            item for item in state.closure_blocked_reasons
            if item["reason"] == "QUANTITY_DISCREPANCY"
        ][0]
        self.assertEqual(discrepancy["details"][0]["subject_ref"], "C-5")
        self.assertEqual(discrepancy["details"][0]["handled"], 4)

    def test_closure_report_traces_result_to_lot_version_and_node(self):
        state = fold_recall_case(load_log() + resolution_events())
        self.assertEqual(state.status, "CLOSED")
        self.assertEqual(state.closure_blocked_reasons, [])
        self.assertEqual(state.closure_report, [{
            "affected_result_id": "RES-9",
            "source_lot": "L-778",
            "scope_version": 2,
            "custody_node_id": "SITE-EAST",
            "assessment": "REINTERPRET_REQUIRED",
        }])
        # 差异说明留痕，但不再阻塞结案。
        self.assertIn("C-5", state.resolved_discrepancies)


class DomainRuleTest(unittest.TestCase):
    def test_minimal_disclosure_rejects_direct_identifiers(self):
        problems = check_minimal_disclosure({
            "subject_ref": "C-1",
            "patient_name": "虚构姓名",
            "nested": {"mrn": "000"},
        })
        self.assertEqual(sorted(problems), ["nested.mrn", "patient_name"])

    def test_naive_timestamp_rejected(self):
        record = make_event("t-001", "CONTAMINATION_NOTICE_RECEIVED", "2026-09-23T08:00:00", "N-1", {
            "notice_id": "N-1", "source": "CENTRAL-LAB",
            "notice_revision": 1, "content_digest": "dg-1",
        })
        self.assertIn("occurred_at_naive", validate_recall_event(record))

    def test_offline_receipt_requires_recorded_time(self):
        record = make_event("t-002", "DISPOSITION_TASK_ACKED", "2026-09-23T10:00:00+08:00", "T-1", {
            "task_id": "T-1", "receipt_channel": "OFFLINE",
            "actor_node_id": "SITE-NORTH", "actor_role": "SITE_OPERATOR",
        })
        self.assertIn("offline_recorded_at", validate_recall_event(record))

    def test_expired_replacement_rejected(self):
        record = make_event("t-003", "REPLACEMENT_ALLOCATED", "2026-09-23T10:00:00+08:00", "T-1", {
            "task_id": "T-1", "replacement_lot": "L-9",
            "replacement_expiry": "2026-09-01T00:00:00+08:00",
            "quantity": 1, "actor_node_id": "SITE-NORTH", "actor_role": "SITE_OPERATOR",
        })
        self.assertIn("replacement_expired", validate_recall_event(record))

    def test_recalled_lot_cannot_be_replacement(self):
        log = mini_log([{
            "subject_ref": "A-1", "situation": "ALLOCATED", "custody_node_id": "SITE-NORTH",
            "quantity": 2, "due_at": "2026-09-25T18:00:00+08:00", "lot": "L-1",
        }])
        log.append(make_event("m-003", "REPLACEMENT_ALLOCATED", "2026-09-23T12:00:00+08:00",
                              "N-1:A-1:REPLACEMENT_ALLOCATION", {
                                  "task_id": "N-1:A-1:REPLACEMENT_ALLOCATION",
                                  "replacement_lot": "L-1",
                                  "replacement_expiry": "2027-01-01T00:00:00+08:00",
                                  "quantity": 2,
                                  "actor_node_id": "SITE-NORTH", "actor_role": "SITE_OPERATOR",
                              }))
        state = fold_recall_case(log)
        self.assertIn("REPLACEMENT_UNDER_RECALL", state.rejected_events[-1]["problems"])
        self.assertEqual(state.replacements, {})

    def test_split_children_must_sum_to_parent_quantity(self):
        log = mini_log([
            {"subject_ref": "K-1", "situation": "SPLIT", "custody_node_id": "SITE-EAST",
             "quantity": 3, "parent_ref": "P-1", "parent_quantity": 5,
             "due_at": "2026-09-25T09:00:00-05:00", "lot": "L-1"},
            {"subject_ref": "K-2", "situation": "SPLIT", "custody_node_id": "SITE-EAST",
             "quantity": 1, "parent_ref": "P-1", "parent_quantity": 5,
             "due_at": "2026-09-25T09:00:00-05:00", "lot": "L-1"},
        ])
        state = fold_recall_case(log)
        discrepancies = quantity_discrepancies(state)
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["subject_ref"], "P-1")

    def test_plan_tasks_is_deterministic(self):
        objects = [
            {"subject_ref": "C-2", "situation": "IN_TRANSIT", "custody_node_id": "SITE-EAST",
             "quantity": 12, "due_at": "2026-09-25T09:00:00-05:00",
             "disposition_policy": "RETURN", "lot": "L-1"},
        ]
        first = plan_tasks("N-1", objects, "DESTROY")
        second = plan_tasks("N-1", objects, "DESTROY")
        self.assertEqual(first, second)
        self.assertEqual(
            [task["task_kind"] for task in first],
            ["INTERCEPT_IN_TRANSIT", "QUARANTINE_CONFIRM", "RETURN_SHIPMENT"])

    def test_evaluate_closure_flags_pending_review(self):
        log = mini_log([{
            "subject_ref": "C-1", "situation": "IN_STOCK", "custody_node_id": "SITE-NORTH",
            "quantity": 2, "due_at": "2026-09-25T18:00:00+08:00", "lot": "L-1",
        }])
        log.append(make_event("m-003", "CONTAMINATION_NOTICE_RECEIVED", "2026-09-23T10:00:00+08:00", "N-1", {
            "notice_id": "N-1", "source": "CENTRAL-LAB",
            "notice_revision": 2, "content_digest": "dg-2",
        }))
        state = fold_recall_case(log)
        reasons = {item["reason"] for item in evaluate_closure(state)}
        self.assertIn("PENDING_REVIEW", reasons)

    def test_closure_report_empty_until_results_assessed(self):
        log = mini_log([{
            "subject_ref": "C-1", "situation": "IN_STOCK", "custody_node_id": "SITE-NORTH",
            "quantity": 2, "due_at": "2026-09-25T18:00:00+08:00", "lot": "L-1",
        }])
        state = fold_recall_case(log)
        self.assertEqual(build_closure_report(state), [])


if __name__ == "__main__":
    unittest.main()
