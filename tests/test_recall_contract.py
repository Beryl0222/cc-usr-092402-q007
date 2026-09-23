import copy
import json
import unittest
from pathlib import Path

from src.clinical_supply_allocation import validate_event
from src.recall_contract import (
    EVENT_KINDS,
    RECALL_KINDS,
    validate_recall_chain,
    recall_dashboard,
)

DATA = Path(__file__).parents[1] / "data"


def _ev(event_id, kind, occurred_at, payload):
    return {
        "event_id": event_id,
        "kind": kind,
        "occurred_at": occurred_at,
        "subject_id": "demo-019",
        "payload": payload,
    }


def base_chain():
    """一条最小且合法、可结案的召回处置链（容器级，单院区）。"""
    return [
        _ev("reg-1", "ENTITY_REGISTERED", "2026-09-10T08:00:00+08:00",
             {"entity_id": "c-1", "entity_type": "container", "lot_id": "L-1",
              "quantity": 10, "unit": "aliquot", "custodian_site": "SITE-A"}),
        _ev("notice-1", "RECALL_NOTICE", "2026-09-20T09:00:00+08:00",
             {"recall_id": "R-1", "source_ref": "SRC-9", "version": 1,
              "scope_level": "container", "scope_refs": ["c-1"],
              "requires_authorization": True}),
        _ev("authz-1", "RECALL_AUTHORIZATION", "2026-09-20T09:30:00+08:00",
             {"recall_id": "R-1", "scope_version": 1, "decision": "approved",
              "reviewer_role": "central_quality_officer"}),
        _ev("task-q", "RECALL_TASK", "2026-09-20T10:00:00+08:00",
             {"task_id": "T-Q", "recall_id": "R-1", "scope_version": 1,
              "action": "quarantine_confirm", "entity_ids": ["c-1"],
              "assigned_site": "SITE-A",
              "deadline_at": "2026-09-21T18:00:00+08:00"}),
        _ev("task-d", "RECALL_TASK", "2026-09-20T10:05:00+08:00",
             {"task_id": "T-D", "recall_id": "R-1", "scope_version": 1,
              "action": "destroy", "entity_ids": ["c-1"],
              "assigned_site": "SITE-A",
              "deadline_at": "2026-09-22T18:00:00+08:00"}),
        _ev("task-r", "RECALL_TASK", "2026-09-20T10:10:00+08:00",
             {"task_id": "T-R", "recall_id": "R-1", "scope_version": 1,
              "action": "assess_result_impact", "entity_ids": [],
              "result_ids": ["res-1"], "assigned_site": "SITE-A",
              "deadline_at": "2026-09-22T18:00:00+08:00"}),
        _ev("q-1", "QUARANTINE_CONFIRMATION", "2026-09-21T10:00:00+08:00",
             {"entity_id": "c-1", "status": "confirmed", "reason": "recall",
              "site": "SITE-A"}),
        _ev("ack-q", "RECALL_TASK_ACK", "2026-09-21T10:05:00+08:00",
             {"task_id": "T-Q", "attempt": 1, "status": "success",
              "reference_no": "QA-1"}),
        _ev("disp-1", "DISPOSAL_CERTIFICATE", "2026-09-22T09:00:00+08:00",
             {"certificate_id": "DES-1", "entity_ids": ["c-1"],
              "site": "SITE-A", "method": "autoclave_incineration"}),
        _ev("ack-d", "RECALL_TASK_ACK", "2026-09-22T09:05:00+08:00",
             {"task_id": "T-D", "attempt": 1, "status": "success",
              "reference_no": "DES-1"}),
        _ev("assess-1", "RESULT_IMPACT_ASSESSMENT", "2026-09-22T14:00:00+08:00",
             {"result_ids": ["res-1"], "verdict": "reinterpret",
              "source_lot_id": "L-1", "scope_version": 1,
              "custodian_site_at_test": "SITE-A"}),
        _ev("ack-r", "RECALL_TASK_ACK", "2026-09-22T14:05:00+08:00",
             {"task_id": "T-R", "attempt": 1, "status": "success",
              "reference_no": "ASSESS-1"}),
        _ev("close-1", "RECALL_CLOSURE", "2026-09-22T18:00:00+08:00",
             {"recall_id": "R-1", "scope_version": 1,
              "original_lot_id": "L-1", "affected_qty": 10,
              "destroyed_qty": 10, "returned_qty": 0,
              "quarantined_remaining_qty": 0,
              "affected_result_ids": ["res-1"]}),
    ]


def by_id(events, eid):
    return next(e for e in events if e["event_id"] == eid)


class SampleFileTest(unittest.TestCase):
    def test_forward_sample_still_valid(self):
        record = json.loads((DATA / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(record), [])

    def test_recall_kinds_registered(self):
        for kind in ("RECALL_NOTICE", "RECALL_TASK", "RECALL_TASK_ACK",
                     "IN_TRANSIT_INTERCEPT", "DISPOSAL_CERTIFICATE",
                     "RESULT_IMPACT_ASSESSMENT", "RECALL_CLOSURE"):
            self.assertIn(kind, RECALL_KINDS)
            self.assertIn(kind, EVENT_KINDS)

    def test_full_documented_case_passes(self):
        events = json.loads(
            (DATA / "recall_case.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_recall_chain(events), [])


class RecallChainRuleTest(unittest.TestCase):
    def setUp(self):
        self.events = base_chain()

    def assertReject(self, needle, events=None):
        problems = validate_recall_chain(events if events is not None
                                         else self.events)
        self.assertTrue(
            any(needle in p for p in problems),
            f"期望违规包含 {needle!r}，实际：{problems}")

    def assertAccept(self, events=None):
        problems = validate_recall_chain(events if events is not None
                                         else self.events)
        self.assertEqual(problems, [])

    # ---- 幂等重放与重启安全 ------------------------------------------
    def test_identical_event_replay_is_idempotent(self):
        # 系统重启后把全部事件原样重放：不得出现重复销毁/补发类违规
        self.assertAccept(self.events + copy.deepcopy(self.events))

    def test_same_notice_replay_keeps_single_task_group(self):
        replay = copy.deepcopy(by_id(self.events, "notice-1"))
        replay["event_id"] = "notice-1-redelivered"
        self.assertAccept(self.events + [replay])

    def test_duplicate_task_from_replayed_notice_rejected(self):
        # 同 task_id 的第二张任务单（重启重放误建）
        dup = copy.deepcopy(by_id(self.events, "task-d"))
        dup["event_id"] = "task-d-dup"
        dup["payload"]["deadline_at"] = "2026-09-23T18:00:00+08:00"
        self.assertReject("只能产生一组任务", self.events + [dup])

        # 不同 task_id 但相同 (召回,对象,动作) 的重复任务组
        dup2 = copy.deepcopy(dup)
        dup2["event_id"] = "task-d-dup2"
        dup2["payload"]["task_id"] = "T-D2"
        self.assertReject("重复的任务", self.events + [dup, dup2])

    def test_same_event_id_with_changed_content_rejected(self):
        evil = copy.deepcopy(by_id(self.events, "ack-d"))
        evil["payload"]["reference_no"] = "DES-FORGED"
        self.assertReject("内容不一致", self.events + [evil])

    def test_duplicate_terminal_certificate_rejected(self):
        dup = copy.deepcopy(by_id(self.events, "ack-d"))
        dup["event_id"] = "ack-d-again"
        # 同一销毁任务出现第二张成功凭证 = 重启后重复销毁
        self.assertReject("不得重复执行", self.events + [dup])

    # ---- 授权闸门 -----------------------------------------------------
    def test_content_change_without_reauthorization_blocked(self):
        events = [e for e in self.events
                  if e["event_id"] != "close-1"]
        events.append(_ev(
            "notice-2", "RECALL_SCOPE_REVISION", "2026-09-23T09:00:00+08:00",
            {"recall_id": "R-1", "source_ref": "SRC-9", "version": 2,
             "change_type": "expand", "scope_level": "container",
             "scope_refs": ["c-1"], "requires_authorization": True}))
        self.assertReject("须等待授权复核", events)

    def test_task_on_unauthorized_version_rejected(self):
        events = [e for e in self.events if e["event_id"] != "close-1"]
        events += [
            _ev("reg-2", "ENTITY_REGISTERED", "2026-09-15T08:00:00+08:00",
                {"entity_id": "c-2", "entity_type": "container",
                 "lot_id": "L-1", "quantity": 5, "unit": "aliquot",
                 "custodian_site": "SITE-A"}),
            _ev("notice-2", "RECALL_SCOPE_REVISION",
                "2026-09-23T09:00:00+08:00",
                {"recall_id": "R-1", "source_ref": "SRC-9", "version": 2,
                 "change_type": "expand", "scope_level": "container",
                 "scope_refs": ["c-1", "c-2"],
                 "requires_authorization": True}),
            _ev("task-x", "RECALL_TASK", "2026-09-23T10:00:00+08:00",
                {"task_id": "T-X", "recall_id": "R-1", "scope_version": 2,
                 "action": "quarantine_confirm", "entity_ids": ["c-2"],
                 "assigned_site": "SITE-A",
                 "deadline_at": "2026-09-24T18:00:00+08:00"}),
        ]
        self.assertReject("未授权的范围", events)

    # ---- 范围扩大 / 缩小 ----------------------------------------------
    def test_expand_requires_tasks_for_new_entities(self):
        events = [e for e in self.events if e["event_id"] != "close-1"]
        events += [
            _ev("reg-2", "ENTITY_REGISTERED", "2026-09-15T08:00:00+08:00",
                {"entity_id": "c-2", "entity_type": "container",
                 "lot_id": "L-1", "quantity": 5, "unit": "aliquot",
                 "custodian_site": "SITE-A"}),
            _ev("notice-2", "RECALL_SCOPE_REVISION",
                "2026-09-23T09:00:00+08:00",
                {"recall_id": "R-1", "source_ref": "SRC-9", "version": 2,
                 "change_type": "expand", "scope_level": "container",
                 "scope_refs": ["c-1", "c-2"],
                 "requires_authorization": True}),
            _ev("authz-2", "RECALL_AUTHORIZATION",
                "2026-09-23T09:30:00+08:00",
                {"recall_id": "R-1", "scope_version": 2,
                 "decision": "approved",
                 "reviewer_role": "central_quality_officer"}),
        ]
        self.assertReject("扩大范围新增", events)

    def test_narrow_does_not_release_manual_quarantine(self):
        # c-2 被人工隔离；v2 将其移出召回范围，仍不得自动解封/重新分配
        events = [e for e in self.events if e["event_id"] != "close-1"]
        events += [
            _ev("reg-2", "ENTITY_REGISTERED", "2026-09-15T08:00:00+08:00",
                {"entity_id": "c-2", "entity_type": "container",
                 "lot_id": "L-1", "quantity": 5, "unit": "aliquot",
                 "custodian_site": "SITE-A"}),
            _ev("q-manual", "QUARANTINE_CONFIRMATION",
                "2026-09-19T08:00:00+08:00",
                {"entity_id": "c-2", "status": "confirmed",
                 "reason": "manual", "site": "SITE-A"}),
            _ev("notice-1b", "RECALL_NOTICE", "2026-09-20T09:00:00+08:00",
                {"recall_id": "R-2", "source_ref": "SRC-10", "version": 1,
                 "scope_level": "container", "scope_refs": ["c-1", "c-2"],
                 "requires_authorization": False}),
            _ev("narrow-2", "RECALL_SCOPE_REVISION",
                "2026-09-21T09:00:00+08:00",
                {"recall_id": "R-2", "source_ref": "SRC-10", "version": 2,
                 "change_type": "narrow", "scope_level": "container",
                 "scope_refs": ["c-1"]}),
            _ev("task-realloc", "RECALL_TASK",
                "2026-09-21T10:00:00+08:00",
                {"task_id": "T-REALLOC", "recall_id": "R-2",
                 "scope_version": 2, "action": "allocate_replacement",
                 "entity_ids": ["c-2"], "assigned_site": "SITE-A",
                 "deadline_at": "2026-09-22T18:00:00+08:00"}),
        ]
        self.assertReject("不得自动解封", events)

    # ---- 保管权与最小披露 ---------------------------------------------
    def test_site_cannot_touch_other_site_entity(self):
        by_id(self.events, "task-d")["payload"]["assigned_site"] = "SITE-B"
        self.assertReject("保管权冲突")

    def test_in_transit_entity_must_be_intercepted_first(self):
        events = [e for e in self.events if e["event_id"] != "close-1"]
        events += [
            _ev("ship-1", "SHIPMENT_DISPATCHED", "2026-09-19T08:00:00+08:00",
                {"shipment_id": "S-1", "entity_ids": ["c-1"],
                 "from_site": "SITE-A", "carrier": "COLD-9"}),
            _ev("task-rogue", "RECALL_TASK",
                "2026-09-20T12:00:00+08:00",
                {"task_id": "T-ROGUE", "recall_id": "R-1",
                 "scope_version": 1, "action": "destroy",
                 "entity_ids": ["c-1"], "assigned_site": "SITE-A",
                 "deadline_at": "2026-09-22T18:00:00+08:00"}),
        ]
        self.assertReject("在途", events)

    def test_identity_fields_forbidden_in_notice(self):
        by_id(self.events, "notice-1")["payload"]["patient_name"] = "张某某"
        self.assertReject("未脱敏")

    # ---- 拆分守恒 / 离线回执 / 效期 / 时区 / 失败重试 -----------------
    def test_split_quantity_must_conserve(self):
        events = [
            _ev("reg-box", "ENTITY_REGISTERED", "2026-09-10T08:00:00+08:00",
                {"entity_id": "box", "entity_type": "box_segment",
                 "lot_id": "L-9", "quantity": 100, "unit": "aliquot",
                 "custodian_site": "SITE-A"}),
            _ev("reg-k1", "ENTITY_REGISTERED", "2026-09-11T08:00:00+08:00",
                {"entity_id": "k1", "entity_type": "container",
                 "lot_id": "L-9", "quantity": 60, "unit": "aliquot",
                 "custodian_site": "SITE-A"}),
            _ev("reg-k2", "ENTITY_REGISTERED", "2026-09-11T08:00:00+08:00",
                {"entity_id": "k2", "entity_type": "container",
                 "lot_id": "L-9", "quantity": 30, "unit": "aliquot",
                 "custodian_site": "SITE-A"}),
            _ev("split", "ENTITY_SPLIT", "2026-09-11T09:00:00+08:00",
                {"parent_entity_id": "box",
                 "child_entity_ids": ["k1", "k2"],
                 "child_quantities": [60, 30]}),
        ]
        self.assertReject("拆分数量不守恒", events)

    def test_offline_ack_needs_recorded_at(self):
        ack = by_id(self.events, "ack-d")
        ack["payload"] = {"task_id": "T-D", "attempt": 1,
                          "status": "offline", "reference_no": "DES-1"}
        self.assertReject("离线回执须补")

    def test_expired_replacement_rejected(self):
        events = [e for e in self.events if e["event_id"] != "close-1"]
        events.append(_ev(
            "rpl-1", "REPLACEMENT_ALLOCATION", "2026-09-23T10:00:00+08:00",
            {"allocation_id": "RPL-1", "for_entity_id": "c-1",
             "replacement_entity_id": "c-1b",
             "replacement_expires_at": "2026-08-01T00:00:00+08:00",
             "site": "SITE-A"}))
        self.assertReject("替代品已过期", events)

    def test_deadline_without_timezone_rejected(self):
        by_id(self.events, "task-d")["payload"]["deadline_at"] = \
            "2026-09-22T18:00:00"
        self.assertReject("deadline_at")

    def test_deadline_compared_across_timezones(self):
        # 创建于北京 10:05(+08)；截止 UTC 01:00 = 北京 09:00，已过期
        by_id(self.events, "task-d")["payload"]["deadline_at"] = \
            "2026-09-20T01:00:00+00:00"
        self.assertReject("截止期限早于")

    def test_failed_then_retry_success_accepted(self):
        ack = by_id(self.events, "ack-d")
        ack["payload"]["attempt"] = 2
        failed = _ev("ack-d-f1", "RECALL_TASK_ACK",
                     "2026-09-22T08:00:00+08:00",
                     {"task_id": "T-D", "attempt": 1, "status": "failed",
                      "retryable": True, "reason": "冷库门禁故障"})
        self.assertAccept(self.events + [failed])

    def test_last_failure_blocks_closure(self):
        by_id(self.events, "ack-d")["payload"] = {
            "task_id": "T-D", "attempt": 1, "status": "failed",
            "retryable": True, "reason": "门禁故障"}
        self.assertReject("尚未重试闭环")

    # ---- 销毁前置 / 结案约束 / 溯源 -----------------------------------
    def test_destroy_without_quarantine_rejected(self):
        self.events = [e for e in self.events
                       if e["event_id"] not in ("q-1", "ack-q", "task-q")]
        # 隔离任务与确认都缺失：销毁前置不足且任务无回执
        problems = validate_recall_chain(self.events)
        self.assertTrue(any("销毁 c-1 前缺少隔离确认" in p for p in problems))

    def test_closure_blocked_by_missing_ack(self):
        self.events = [e for e in self.events if e["event_id"] != "ack-d"]
        self.assertReject("无确认回执")

    def test_closure_blocked_by_quantity_gap(self):
        by_id(self.events, "close-1")["payload"]["destroyed_qty"] = 9
        self.assertReject("结案数量不守恒")

    def test_closure_cannot_be_forced(self):
        c = by_id(self.events, "close-1")["payload"]
        c["destroyed_qty"] = 9
        c["force"] = True
        problems = validate_recall_chain(self.events)
        self.assertTrue(any("强行结案" in p for p in problems))
        self.assertTrue(any("数量不守恒" in p for p in problems))

    def test_closure_requires_result_traceability(self):
        by_id(self.events, "assess-1")["payload"]["source_lot_id"] = "L-OTHER"
        self.assertReject("无法追溯到原批次")

    def test_closure_version_must_match_current_scope(self):
        # v2 缩小到空集之外的对象但未重出结案：结案停留在 v1
        by_id(self.events, "notice-1")["payload"]["version"] = 2
        self.assertReject("结案报告基于")


class DashboardTest(unittest.TestCase):
    def test_board_flags_pending_and_gap(self):
        # 缺失销毁回执，且尚未结案：应显眼挂起，无结案数量差异
        events = [e for e in base_chain()
                  if e["event_id"] not in ("ack-d", "close-1")]
        board = recall_dashboard(events)["R-1"]
        self.assertIn("T-D", board["unacknowledged_or_failed_tasks"])
        self.assertIsNone(board["quantity_gap"])
        self.assertFalse(board["closed"])

    def test_board_surfaces_quantity_gap_on_closure(self):
        events = [e for e in base_chain() if e["event_id"] != "ack-d"]
        # 强行带数量差异结案（验证看板把差异显眼暴露）
        close = next(e for e in events if e["event_id"] == "close-1")
        close["payload"]["destroyed_qty"] = 7
        board = recall_dashboard(events)["R-1"]
        self.assertEqual(board["quantity_gap"], 3)
        self.assertTrue(board["closed"])

    def test_board_clean_case(self):
        events = json.loads(
            (DATA / "recall_case.json").read_text(encoding="utf-8"))
        board = recall_dashboard(events)["R-2026-09-001"]
        self.assertEqual(board["current_version"], 2)
        self.assertEqual(board["authorized_version"], 2)
        self.assertEqual(board["unacknowledged_or_failed_tasks"], [])
        self.assertEqual(board["quantity_gap"], 0)
        self.assertTrue(board["closed"])
        # 看板只暴露去标识维度，无身份字段
        text = json.dumps(board, ensure_ascii=False)
        for forbidden in ("patient_name", "id_card_no", "medical_record_no"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
