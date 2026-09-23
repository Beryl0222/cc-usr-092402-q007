"""召回与逆向处置链的领域约定。

在既有临床样品合同（``src/clinical_supply_allocation.py``）之上扩展：
批次污染通知到达后，质量负责人可以按批次、箱段、容器或检测影响范围
发起带版本的召回行动，并将在途拦截、隔离确认、退运、销毁、替代分配
与结果影响评估作为独立任务分别推进。

本模块只包含纯函数与数据约定，不包含真实个人信息或外部连接：

- ``RECALL_EVENT_KINDS`` / ``RECALL_REQUIRED_FIELDS``：事件种类与每类
  事件的最小字段；
- ``fold_recall_case``：把事件日志折叠为召回案例状态。折叠是确定性的
  纯函数，系统重启后重放同一份日志得到同一状态——相同通知重放只产生
  一组任务，销毁与补发不会因重试或重启而重复；
- ``evaluate_closure``：结案评估。任何未确认回执或数量差异都保持显眼，
  不存在强行结案的路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.clinical_supply_allocation import REQUIRED_FIELDS

# --- 事件种类 ---------------------------------------------------------------

RECALL_EVENT_KINDS = [
    "CONTAMINATION_NOTICE_RECEIVED",  # 批次污染通知到达
    "RECALL_ACTION_OPENED",           # 带版本的召回行动发起
    "RECALL_SCOPE_REVISED",           # 范围修订（扩大纳入新对象；缩小不自动解封人工隔离）
    "RECALL_REVIEW_RESOLVED",         # 同源内容变化的授权复核结论（范围不变）
    "DISPOSITION_TASK_ISSUED",        # 处置任务下发（含物流离线补录与失败重试）
    "DISPOSITION_TASK_ACKED",         # 处置任务回执（线上或离线纸质补录）
    "DISPOSITION_TASK_COMPLETED",     # 处置任务完成
    "DISPOSITION_TASK_FAILED",        # 处置任务部分失败（可重试）
    "MANUAL_HOLD_PLACED",             # 人工隔离
    "MANUAL_HOLD_RELEASED",           # 人工解封（只能显式发生）
    "REPLACEMENT_ALLOCATED",          # 替代分配完成（替代品须未过期且不在召回范围）
    "RESULT_IMPACT_ASSESSED",         # 结果影响评估完成
    "QUANTITY_DISCREPANCY_RESOLVED",  # 数量差异的授权说明（差异记录仍保留）
    "RECALL_CLOSURE_REQUESTED",       # 结案申请（存在阻塞原因时结案失败并显眼记录）
]

# --- 处置任务与范围词汇 -------------------------------------------------------

TASK_KINDS = [
    "INTERCEPT_IN_TRANSIT",      # 在途拦截
    "QUARANTINE_CONFIRM",        # 隔离确认
    "RETURN_SHIPMENT",           # 退运
    "DESTRUCTION",               # 销毁
    "REPLACEMENT_ALLOCATION",    # 替代分配
    "RESULT_IMPACT_ASSESSMENT",  # 结果影响评估
]

SCOPE_KINDS = ["LOT", "BOX_RANGE", "CONTAINER", "ASSAY_IMPACT"]

SITUATIONS = [
    "IN_STOCK",     # 仍在库内
    "IN_TRANSIT",   # 运输途中
    "SPLIT",        # 已拆分给多个试验的子容器
    "ALLOCATED",    # 已分配待用
    "CONSUMED",     # 已使用
    "RESULT_ONLY",  # 只剩衍生检测结果
]

DISPOSITION_POLICIES = ["RETURN", "DESTROY"]

ROLES = ["SITE_OPERATOR", "CENTRAL_QUALITY"]

RECEIPT_CHANNELS = ["ONLINE", "OFFLINE"]

# 各处境对应的任务种类；实物处境额外追加处置策略任务（退运或销毁）。
TASK_KINDS_BY_SITUATION = {
    "IN_TRANSIT": ("INTERCEPT_IN_TRANSIT", "QUARANTINE_CONFIRM"),
    "IN_STOCK": ("QUARANTINE_CONFIRM",),
    "SPLIT": ("QUARANTINE_CONFIRM",),
    "ALLOCATED": ("QUARANTINE_CONFIRM", "REPLACEMENT_ALLOCATION"),
    "CONSUMED": ("RESULT_IMPACT_ASSESSMENT",),
    "RESULT_ONLY": ("RESULT_IMPACT_ASSESSMENT",),
}
PHYSICAL_SITUATIONS = ("IN_TRANSIT", "IN_STOCK", "SPLIT")
POLICY_TASK_KIND = {"RETURN": "RETURN_SHIPMENT", "DESTROY": "DESTRUCTION"}

# --- 每类事件的最小字段（payload 内） -----------------------------------------

RECALL_REQUIRED_FIELDS = {
    "CONTAMINATION_NOTICE_RECEIVED": ("notice_id", "source", "notice_revision", "content_digest"),
    "RECALL_ACTION_OPENED": (
        "recall_id", "notice_id", "notice_revision", "content_digest",
        "scope", "objects", "disposition_policy",
    ),
    "RECALL_SCOPE_REVISED": ("recall_id", "scope_version", "added_objects", "removed_refs", "authorized_by"),
    "RECALL_REVIEW_RESOLVED": ("recall_id", "resolution", "authorized_by"),
    "DISPOSITION_TASK_ISSUED": ("task", "actor_node_id", "actor_role"),
    "DISPOSITION_TASK_ACKED": ("task_id", "receipt_channel", "actor_node_id", "actor_role"),
    "DISPOSITION_TASK_COMPLETED": ("task_id", "quantity_handled", "actor_node_id", "actor_role"),
    "DISPOSITION_TASK_FAILED": ("task_id", "retryable", "actor_node_id", "actor_role"),
    "MANUAL_HOLD_PLACED": ("hold_id", "subject_ref", "custody_node_id", "actor_node_id", "actor_role"),
    "MANUAL_HOLD_RELEASED": ("hold_id", "actor_node_id", "actor_role"),
    "REPLACEMENT_ALLOCATED": (
        "task_id", "replacement_lot", "replacement_expiry", "quantity", "actor_node_id", "actor_role",
    ),
    "RESULT_IMPACT_ASSESSED": (
        "task_id", "affected_result_id", "source_lot", "assessment", "actor_node_id", "actor_role",
    ),
    "QUANTITY_DISCREPANCY_RESOLVED": ("recall_id", "subject_ref", "explanation", "authorized_by"),
    "RECALL_CLOSURE_REQUESTED": ("recall_id", "requested_by"),
}

# 跨机构通知中禁止出现的直接患者标识；只允许假名 subject_ref。
DIRECT_IDENTIFIER_FIELDS = (
    "patient_name", "patient_id", "name", "id_number", "id_card",
    "mrn", "phone", "address", "birth_date",
)


# --- 时间工具 -----------------------------------------------------------------

def _parse(timestamp: Any) -> datetime | None:
    """把 ISO 8601 字符串解析为 datetime；无法解析时返回 None。"""
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError:
        return None


def _is_aware(timestamp: Any) -> bool:
    """时间戳必须带时区偏移，跨时区截止期限才能安全比较。"""
    parsed = _parse(timestamp)
    return parsed is not None and parsed.tzinfo is not None


# --- 校验 ---------------------------------------------------------------------

def check_minimal_disclosure(payload: dict) -> list[str]:
    """跨机构通知中患者标识保持最少披露：发现直接标识字段则列出路径。"""
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in DIRECT_IDENTIFIER_FIELDS:
                    found.append(f"{path}{key}")
                else:
                    walk(value, f"{path}{key}.")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}{index}.")

    walk(payload, "")
    return found


def _object_problems(objects: Any, prefix: str) -> list[str]:
    """范围对象的最小字段：保管节点、处境、数量与带时区的截止期限。"""
    problems: list[str] = []
    if not isinstance(objects, list):
        return [prefix]
    for index, obj in enumerate(objects):
        tag = f"{prefix}[{index}]"
        if not isinstance(obj, dict):
            problems.append(tag)
            continue
        for name in ("subject_ref", "situation", "custody_node_id", "quantity", "due_at"):
            if name not in obj:
                problems.append(f"{tag}.{name}")
        if obj.get("situation") not in SITUATIONS:
            problems.append(f"{tag}.situation")
        if obj.get("situation") == "SPLIT":
            for name in ("parent_ref", "parent_quantity"):
                if name not in obj:
                    problems.append(f"{tag}.{name}")
        if not _is_aware(obj.get("due_at")):
            problems.append(f"{tag}.due_at")
        policy = obj.get("disposition_policy")
        if policy is not None and policy not in DISPOSITION_POLICIES:
            problems.append(f"{tag}.disposition_policy")
    return problems


def _kind_specific_problems(kind: str, payload: dict, occurred_at: Any) -> list[str]:
    problems: list[str] = []
    if "actor_role" in RECALL_REQUIRED_FIELDS[kind] and payload.get("actor_role") not in ROLES:
        problems.append("actor_role")
    if kind == "CONTAMINATION_NOTICE_RECEIVED":
        if not isinstance(payload.get("notice_revision"), int):
            problems.append("notice_revision")
    elif kind == "RECALL_ACTION_OPENED":
        scope = payload.get("scope")
        if not isinstance(scope, dict) or scope.get("kind") not in SCOPE_KINDS:
            problems.append("scope")
        if payload.get("disposition_policy") not in DISPOSITION_POLICIES:
            problems.append("disposition_policy")
        problems.extend(_object_problems(payload.get("objects"), "objects"))
    elif kind == "RECALL_SCOPE_REVISED":
        problems.extend(_object_problems(payload.get("added_objects"), "added_objects"))
    elif kind == "DISPOSITION_TASK_ISSUED":
        task = payload.get("task")
        if not isinstance(task, dict):
            problems.append("task")
        else:
            for name in ("task_kind", "subject_ref", "custody_node_id", "due_at", "quantity"):
                if name not in task:
                    problems.append(f"task.{name}")
            if task.get("task_kind") not in TASK_KINDS:
                problems.append("task.task_kind")
            if not _is_aware(task.get("due_at")):
                problems.append("task.due_at")
    elif kind == "DISPOSITION_TASK_ACKED":
        if payload.get("receipt_channel") not in RECEIPT_CHANNELS:
            problems.append("receipt_channel")
        # 物流离线回执必须保留纸质记录时间，便于事后对账。
        if payload.get("receipt_channel") == "OFFLINE" and not _is_aware(payload.get("offline_recorded_at")):
            problems.append("offline_recorded_at")
    elif kind == "REPLACEMENT_ALLOCATED":
        expiry = _parse(payload.get("replacement_expiry"))
        occurred = _parse(occurred_at)
        if expiry is None or expiry.tzinfo is None:
            problems.append("replacement_expiry")
        elif occurred is not None and occurred.tzinfo is not None and expiry <= occurred:
            # 过期替代品不能用于补发。
            problems.append("replacement_expired")
    return problems


def validate_recall_event(record: dict) -> list[str]:
    """检查召回链事件是否具备可交换的最小字段与领域约束。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    kind = record.get("kind")
    if kind not in RECALL_EVENT_KINDS:
        problems.append("kind")
        return problems
    payload = record.get("payload")
    if not isinstance(payload, dict):
        problems.append("payload")
        return problems
    for name in RECALL_REQUIRED_FIELDS[kind]:
        if name not in payload:
            problems.append(name)
    occurred_at = record.get("occurred_at")
    if occurred_at is not None and not _is_aware(occurred_at):
        problems.append("occurred_at_naive")
    problems.extend(_kind_specific_problems(kind, payload, occurred_at))
    problems.extend(check_minimal_disclosure(payload))
    return problems


# --- 任务规划 -----------------------------------------------------------------

def task_id_for(notice_id: str, subject_ref: str, task_kind: str) -> str:
    """任务标识由通知、对象与任务种类唯一决定，重放与重启后保持稳定。"""
    return f"{notice_id}:{subject_ref}:{task_kind}"


def plan_tasks(notice_id: str, objects: list[dict], default_policy: str) -> list[dict]:
    """按范围对象生成确定性的任务清单：同一通知重放只产生一组任务。"""
    planned: list[dict] = []
    for obj in objects:
        kinds = list(TASK_KINDS_BY_SITUATION[obj["situation"]])
        if obj["situation"] in PHYSICAL_SITUATIONS:
            kinds.append(POLICY_TASK_KIND[obj.get("disposition_policy", default_policy)])
        for kind in kinds:
            planned.append({
                "task_id": task_id_for(notice_id, obj["subject_ref"], kind),
                "task_kind": kind,
                "subject_ref": obj["subject_ref"],
                "custody_node_id": obj["custody_node_id"],
                "due_at": obj["due_at"],
                "quantity": obj["quantity"],
            })
    return planned


# --- 状态 ---------------------------------------------------------------------

@dataclass
class DispositionTask:
    """单项处置任务及其生命周期。"""

    task_id: str
    recall_id: str
    task_kind: str
    subject_ref: str
    custody_node_id: str
    due_at: str
    quantity: float
    scope_version: int
    status: str = "ISSUED"  # ISSUED / ACKED / COMPLETED / FAILED / CANCELLED
    attempt: int = 1
    quantity_handled: float = 0
    receipt_channel: str | None = None
    cancel_reason: str | None = None


@dataclass
class RecallCaseState:
    """一份污染通知对应的召回案例状态。"""

    notice_id: str | None = None
    recall_id: str | None = None
    status: str = "IDLE"  # IDLE / OPEN / PENDING_REVIEW / CLOSED
    accepted_notice_revision: int = 0
    accepted_digest: str | None = None
    pending_notice: dict | None = None
    disposition_policy: str = "DESTROY"
    scope_version: int = 0
    scope_refs: set[str] = field(default_factory=set)
    objects: dict[str, dict] = field(default_factory=dict)
    recalled_lots: set[str] = field(default_factory=set)
    tasks: dict[str, DispositionTask] = field(default_factory=dict)
    manual_holds: dict[str, dict] = field(default_factory=dict)
    result_impacts: dict[str, dict] = field(default_factory=dict)
    replacements: dict[str, dict] = field(default_factory=dict)
    resolved_discrepancies: dict[str, dict] = field(default_factory=dict)
    closure_blocked_reasons: list[dict] = field(default_factory=list)
    closure_report: list[dict] = field(default_factory=list)
    rejected_events: list[dict] = field(default_factory=list)


def _reject(state: RecallCaseState, record: dict, *problems: str) -> None:
    """被拒事件显眼留痕，不静默丢弃。"""
    state.rejected_events.append({"event_id": record.get("event_id"), "problems": list(problems)})


def _accept_pending(state: RecallCaseState) -> None:
    """授权复核通过后，接受待复核的通知版本。"""
    if state.pending_notice is not None:
        state.accepted_notice_revision = state.pending_notice["notice_revision"]
        state.accepted_digest = state.pending_notice["content_digest"]
        state.pending_notice = None
    if state.status == "PENDING_REVIEW":
        state.status = "OPEN"


def _issue_task(state: RecallCaseState, planned: dict, scope_version: int, attempt: int = 1) -> None:
    """按确定性标识下发任务；同键任务不重复下发，失败重试沿用同一任务。"""
    existing = state.tasks.get(planned["task_id"])
    if existing is not None:
        if existing.status == "FAILED" and attempt > existing.attempt:
            existing.status = "ISSUED"
            existing.attempt = attempt
        return
    state.tasks[planned["task_id"]] = DispositionTask(
        task_id=planned["task_id"],
        recall_id=state.recall_id or "",
        task_kind=planned["task_kind"],
        subject_ref=planned["subject_ref"],
        custody_node_id=planned["custody_node_id"],
        due_at=planned["due_at"],
        quantity=planned["quantity"],
        scope_version=scope_version,
        attempt=attempt,
    )


def _register_objects(state: RecallCaseState, objects: list[dict], scope_version: int) -> None:
    """把范围对象纳入召回：范围扩大时新增对象获得自己的任务组。"""
    for obj in objects:
        ref = obj["subject_ref"]
        if ref in state.objects:
            continue
        state.scope_refs.add(ref)
        state.objects[ref] = obj
        if obj.get("lot"):
            state.recalled_lots.add(obj["lot"])
        for planned in plan_tasks(state.notice_id or "", [obj], state.disposition_policy):
            _issue_task(state, planned, scope_version)


def _custody_ok(payload: dict, custody_node_id: str) -> bool:
    """各院区只能操作自己保管的实体；中心质量员查看全局但不代为操作。"""
    return payload.get("actor_role") == "SITE_OPERATOR" and payload.get("actor_node_id") == custody_node_id


# --- 各类事件的折叠规则 ---------------------------------------------------------

def _apply_notice(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    notice_id = payload["notice_id"]
    revision = payload["notice_revision"]
    digest = payload["content_digest"]
    if state.notice_id is None:
        state.notice_id = notice_id
        state.accepted_notice_revision = revision
        state.accepted_digest = digest
        return
    if notice_id != state.notice_id:
        _reject(state, record, "NOTICE_ID_MISMATCH")
        return
    if revision < state.accepted_notice_revision:
        return  # 过期版本的重放
    if revision == state.accepted_notice_revision:
        if digest != state.accepted_digest:
            _reject(state, record, "NOTICE_DIGEST_MISMATCH")
        return  # 相同通知重放：只产生一组任务
    pending = state.pending_notice
    if pending is not None and revision == pending["notice_revision"]:
        if digest != pending["content_digest"]:
            _reject(state, record, "NOTICE_DIGEST_MISMATCH")
        return  # 复核期间同一新版本重复到达
    # 来源相同但内容变化：必须等待授权复核。
    state.pending_notice = {"notice_revision": revision, "content_digest": digest}
    state.status = "PENDING_REVIEW"


def _apply_open(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    if state.recall_id is not None:
        if (
            payload["recall_id"] == state.recall_id
            and payload["notice_revision"] == state.accepted_notice_revision
            and payload["content_digest"] == state.accepted_digest
        ):
            return  # 重启后重放：不重复产生任务
        _reject(state, record, "RECALL_ALREADY_OPEN")
        return
    if (
        payload["notice_id"] != state.notice_id
        or payload["notice_revision"] != state.accepted_notice_revision
        or payload["content_digest"] != state.accepted_digest
    ):
        _reject(state, record, "NOTICE_MISMATCH")
        return
    state.recall_id = payload["recall_id"]
    state.status = "OPEN"
    state.scope_version = 1
    state.disposition_policy = payload["disposition_policy"]
    scope = payload["scope"]
    if scope.get("kind") == "LOT":
        state.recalled_lots.update(scope.get("refs", []))
    _register_objects(state, payload["objects"], scope_version=1)


def _apply_scope_revised(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    if payload["recall_id"] != state.recall_id:
        _reject(state, record, "UNKNOWN_RECALL")
        return
    if payload["scope_version"] != state.scope_version + 1:
        _reject(state, record, "STALE_SCOPE_VERSION")
        return
    state.scope_version += 1
    _register_objects(state, payload["added_objects"], scope_version=state.scope_version)
    for ref in payload["removed_refs"]:
        state.scope_refs.discard(ref)
        for task in state.tasks.values():
            if task.subject_ref == ref and task.status in ("ISSUED", "ACKED", "FAILED"):
                task.status = "CANCELLED"
                task.cancel_reason = "SCOPE_REDUCED"
        # 范围缩小不自动解封：manual_holds 中该对象的人工隔离保持不变。
    _accept_pending(state)  # 授权修订同时构成对内容变化的复核结论


def _apply_review_resolved(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    if payload["recall_id"] != state.recall_id:
        _reject(state, record, "UNKNOWN_RECALL")
        return
    if payload["resolution"] != "SCOPE_UNCHANGED":
        _reject(state, record, "UNSUPPORTED_RESOLUTION")
        return
    _accept_pending(state)


def _apply_task_issued(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    spec = payload["task"]
    if state.recall_id is None:
        _reject(state, record, "RECALL_NOT_OPEN")
        return
    if state.status == "PENDING_REVIEW":
        _reject(state, record, "PENDING_REVIEW")  # 内容变化须先等待授权复核
        return
    if not _custody_ok(payload, spec["custody_node_id"]):
        _reject(state, record, "CUSTODY_VIOLATION")
        return
    planned = {
        "task_id": task_id_for(state.notice_id or "", spec["subject_ref"], spec["task_kind"]),
        "task_kind": spec["task_kind"],
        "subject_ref": spec["subject_ref"],
        "custody_node_id": spec["custody_node_id"],
        "due_at": spec["due_at"],
        "quantity": spec["quantity"],
    }
    _issue_task(state, planned, state.scope_version, attempt=spec.get("attempt", 1))


def _lookup_task(state: RecallCaseState, record: dict) -> DispositionTask | None:
    task = state.tasks.get(record["payload"]["task_id"])
    if task is None:
        _reject(state, record, "UNKNOWN_TASK")
    return task


def _apply_task_acked(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    task = _lookup_task(state, record)
    if task is None:
        return
    if not _custody_ok(payload, task.custody_node_id):
        _reject(state, record, "CUSTODY_VIOLATION")
        return
    if task.status == "CANCELLED":
        _reject(state, record, "TASK_CANCELLED")
        return
    if task.status == "COMPLETED":
        return  # 重放
    task.status = "ACKED"
    task.receipt_channel = payload["receipt_channel"]


def _apply_task_completed(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    task = _lookup_task(state, record)
    if task is None:
        return
    if not _custody_ok(payload, task.custody_node_id):
        _reject(state, record, "CUSTODY_VIOLATION")
        return
    if task.status == "CANCELLED":
        _reject(state, record, "TASK_CANCELLED")
        return
    if task.status == "COMPLETED":
        return  # 重启重放：不重复销毁或退运
    task.status = "COMPLETED"
    task.quantity_handled = payload["quantity_handled"]
    if task.receipt_channel is None:
        task.receipt_channel = payload.get("receipt_channel", "IMPLIED")


def _apply_task_failed(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    task = _lookup_task(state, record)
    if task is None:
        return
    if not _custody_ok(payload, task.custody_node_id):
        _reject(state, record, "CUSTODY_VIOLATION")
        return
    if task.status in ("COMPLETED", "CANCELLED"):
        _reject(state, record, "TASK_NOT_ACTIVE")
        return
    task.status = "FAILED"
    task.quantity_handled = payload.get("quantity_handled", task.quantity_handled)


def _apply_hold_placed(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    if not _custody_ok(payload, payload["custody_node_id"]):
        _reject(state, record, "CUSTODY_VIOLATION")
        return
    if payload["hold_id"] in state.manual_holds:
        return  # 重放
    state.manual_holds[payload["hold_id"]] = {
        "subject_ref": payload["subject_ref"],
        "custody_node_id": payload["custody_node_id"],
        "released": False,
    }


def _apply_hold_released(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    hold = state.manual_holds.get(payload["hold_id"])
    if hold is None:
        _reject(state, record, "UNKNOWN_HOLD")
        return
    if not _custody_ok(payload, hold["custody_node_id"]):
        _reject(state, record, "CUSTODY_VIOLATION")
        return
    hold["released"] = True  # 人工解封只能显式发生


def _apply_replacement(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    task = _lookup_task(state, record)
    if task is None:
        return
    if task.task_kind != "REPLACEMENT_ALLOCATION":
        _reject(state, record, "TASK_KIND_MISMATCH")
        return
    if not _custody_ok(payload, task.custody_node_id):
        _reject(state, record, "CUSTODY_VIOLATION")
        return
    if payload["replacement_lot"] in state.recalled_lots:
        _reject(state, record, "REPLACEMENT_UNDER_RECALL")
        return
    if task.status == "COMPLETED":
        return  # 重启重放：不重复补发
    task.status = "COMPLETED"
    task.quantity_handled = payload["quantity"]
    if task.receipt_channel is None:
        task.receipt_channel = "IMPLIED"
    state.replacements[task.task_id] = {
        "replacement_lot": payload["replacement_lot"],
        "replacement_expiry": payload["replacement_expiry"],
        "quantity": payload["quantity"],
    }


def _apply_result_impact(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    task = _lookup_task(state, record)
    if task is None:
        return
    if task.task_kind != "RESULT_IMPACT_ASSESSMENT":
        _reject(state, record, "TASK_KIND_MISMATCH")
        return
    if not _custody_ok(payload, task.custody_node_id):
        _reject(state, record, "CUSTODY_VIOLATION")
        return
    if task.status == "COMPLETED":
        return  # 重放
    task.status = "COMPLETED"
    task.quantity_handled = task.quantity
    if task.receipt_channel is None:
        task.receipt_channel = "IMPLIED"
    state.result_impacts[payload["affected_result_id"]] = {
        "source_lot": payload["source_lot"],
        "scope_version": state.scope_version,
        "custody_node_id": task.custody_node_id,
        "assessment": payload["assessment"],
    }


def _apply_discrepancy_resolved(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    if payload["recall_id"] != state.recall_id:
        _reject(state, record, "UNKNOWN_RECALL")
        return
    state.resolved_discrepancies[payload["subject_ref"]] = {
        "explanation": payload["explanation"],
        "authorized_by": payload["authorized_by"],
    }


def _apply_closure_requested(state: RecallCaseState, record: dict) -> None:
    payload = record["payload"]
    if payload["recall_id"] != state.recall_id:
        _reject(state, record, "UNKNOWN_RECALL")
        return
    if state.status == "CLOSED":
        return  # 重放
    blockers = evaluate_closure(state)
    if blockers:
        state.closure_blocked_reasons = blockers  # 阻塞原因保持显眼
        return
    state.status = "CLOSED"
    state.closure_blocked_reasons = []
    state.closure_report = build_closure_report(state)


_HANDLERS = {
    "CONTAMINATION_NOTICE_RECEIVED": _apply_notice,
    "RECALL_ACTION_OPENED": _apply_open,
    "RECALL_SCOPE_REVISED": _apply_scope_revised,
    "RECALL_REVIEW_RESOLVED": _apply_review_resolved,
    "DISPOSITION_TASK_ISSUED": _apply_task_issued,
    "DISPOSITION_TASK_ACKED": _apply_task_acked,
    "DISPOSITION_TASK_COMPLETED": _apply_task_completed,
    "DISPOSITION_TASK_FAILED": _apply_task_failed,
    "MANUAL_HOLD_PLACED": _apply_hold_placed,
    "MANUAL_HOLD_RELEASED": _apply_hold_released,
    "REPLACEMENT_ALLOCATED": _apply_replacement,
    "RESULT_IMPACT_ASSESSED": _apply_result_impact,
    "QUANTITY_DISCREPANCY_RESOLVED": _apply_discrepancy_resolved,
    "RECALL_CLOSURE_REQUESTED": _apply_closure_requested,
}


def _sort_key(record: dict) -> tuple:
    """按实际时刻（而非字符串）排序，跨时区时间戳也能正确先后。"""
    parsed = _parse(record.get("occurred_at"))
    aware = parsed is not None and parsed.tzinfo is not None
    fallback = datetime.max.replace(tzinfo=timezone.utc)
    return (not aware, parsed if aware else fallback, record.get("event_id", ""))


def fold_recall_case(events: list[dict]) -> RecallCaseState:
    """把事件日志折叠为召回案例状态。

    折叠是确定性的纯函数：系统重启后重放同一份日志得到同一状态，
    相同通知重放只产生一组任务，销毁与补发不会因重试或重启而重复。
    格式不合法或违反领域规则的事件记入 ``rejected_events``，不静默丢弃。
    """
    state = RecallCaseState()
    for record in sorted(events, key=_sort_key):
        problems = validate_recall_event(record)
        if problems:
            _reject(state, record, *problems)
            continue
        _HANDLERS[record["kind"]](state, record)
    return state


# --- 数量守恒与结案 -------------------------------------------------------------

def quantity_discrepancies(state: RecallCaseState) -> list[dict]:
    """数量差异：已完成任务的实处理量与应有量不符，或拆分子量合计不等于父量。"""
    found: list[dict] = []
    for task in state.tasks.values():
        if task.status == "COMPLETED" and task.quantity_handled != task.quantity:
            found.append({
                "subject_ref": task.subject_ref,
                "task_id": task.task_id,
                "expected": task.quantity,
                "handled": task.quantity_handled,
            })
    children_by_parent: dict[str, dict] = {}
    for ref in state.scope_refs:
        obj = state.objects.get(ref)
        if obj and obj.get("situation") == "SPLIT":
            entry = children_by_parent.setdefault(
                obj["parent_ref"], {"expected": 0, "parent_quantity": obj["parent_quantity"]}
            )
            entry["expected"] += obj["quantity"]
    for parent_ref, entry in sorted(children_by_parent.items()):
        if entry["expected"] != entry["parent_quantity"]:
            found.append({
                "subject_ref": parent_ref,
                "expected": entry["parent_quantity"],
                "handled": entry["expected"],
            })
    return found


def evaluate_closure(state: RecallCaseState) -> list[dict]:
    """结案阻塞原因。任何未确认回执或数量差异都保持显眼，不能被强行结案。"""
    reasons: list[dict] = []
    if state.status == "PENDING_REVIEW":
        reasons.append({"reason": "PENDING_REVIEW", "details": [state.notice_id]})
    unacked = sorted(t.task_id for t in state.tasks.values() if t.status in ("ISSUED", "FAILED"))
    if unacked:
        reasons.append({"reason": "UNACKNOWLEDGED_RECEIPTS", "details": unacked})
    unfinished = sorted(
        t.task_id for t in state.tasks.values() if t.status not in ("COMPLETED", "CANCELLED")
    )
    if unfinished:
        reasons.append({"reason": "UNFINISHED_TASKS", "details": unfinished})
    discrepancies = [
        item for item in quantity_discrepancies(state)
        if item["subject_ref"] not in state.resolved_discrepancies
    ]
    if discrepancies:
        reasons.append({"reason": "QUANTITY_DISCREPANCY", "details": discrepancies})
    return reasons


def build_closure_report(state: RecallCaseState) -> list[dict]:
    """结案报告：每项受影响结果可追到原批次、召回范围版本与实际保管节点。"""
    return [
        {
            "affected_result_id": result_id,
            "source_lot": impact["source_lot"],
            "scope_version": impact["scope_version"],
            "custody_node_id": impact["custody_node_id"],
            "assessment": impact["assessment"],
        }
        for result_id, impact in sorted(state.result_impacts.items())
    ]


# --- 视图 -----------------------------------------------------------------------

def global_conservation_view(state: RecallCaseState, *, actor_role: str, actor_node_id: str) -> list[dict]:
    """全局守恒视图：中心质量员看全部节点，院区只看自己保管的实体。"""
    rows = []
    for task in sorted(state.tasks.values(), key=lambda t: t.task_id):
        if actor_role != "CENTRAL_QUALITY" and task.custody_node_id != actor_node_id:
            continue
        rows.append({
            "custody_node_id": task.custody_node_id,
            "task_id": task.task_id,
            "status": task.status,
            "expected": task.quantity,
            "handled": task.quantity_handled,
        })
    return rows


def unconfirmed_receipts_view(
    state: RecallCaseState, *, actor_role: str, actor_node_id: str, now: str
) -> list[dict]:
    """未确认回执视图：列出尚未回执的任务，并按跨时区安全的比较标记逾期。"""
    now_dt = _parse(now)
    if now_dt is None or now_dt.tzinfo is None:
        raise ValueError("now 必须是带时区偏移的 ISO 8601 时间戳")
    rows = []
    for task in sorted(state.tasks.values(), key=lambda t: t.task_id):
        if task.status not in ("ISSUED", "FAILED"):
            continue
        if actor_role != "CENTRAL_QUALITY" and task.custody_node_id != actor_node_id:
            continue
        due = _parse(task.due_at)
        rows.append({
            "custody_node_id": task.custody_node_id,
            "task_id": task.task_id,
            "due_at": task.due_at,
            "overdue": due is not None and due < now_dt,
        })
    return rows


def state_snapshot(state: RecallCaseState) -> dict:
    """状态摘要：用于核对重启重放的一致性（不含被拒事件等过程记录）。"""
    return {
        "status": state.status,
        "scope_version": state.scope_version,
        "accepted_notice_revision": state.accepted_notice_revision,
        "scope_refs": sorted(state.scope_refs),
        "tasks": {
            task_id: (task.status, task.attempt, task.quantity_handled, task.receipt_channel)
            for task_id, task in sorted(state.tasks.items())
        },
        "manual_holds": {
            hold_id: (hold["subject_ref"], hold["released"])
            for hold_id, hold in sorted(state.manual_holds.items())
        },
        "result_impacts": {
            result_id: (
                impact["source_lot"], impact["scope_version"],
                impact["custody_node_id"], impact["assessment"],
            )
            for result_id, impact in sorted(state.result_impacts.items())
        },
        "replacements": {
            task_id: (item["replacement_lot"], item["quantity"])
            for task_id, item in sorted(state.replacements.items())
        },
        "closure_report": state.closure_report,
        "closure_blocked_reasons": [item["reason"] for item in state.closure_blocked_reasons],
    }
