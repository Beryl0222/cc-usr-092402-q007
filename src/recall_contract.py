"""临床样品召回与逆向处置链的领域资料（事件合同）。

在原有“释放 / 预留 / 收货”之外，补齐批次污染通知到达后的逆向处置链：
带版本的召回通知 → 授权复核 → 按保管节点分派任务 → 在途拦截 / 隔离确认 /
退运 / 销毁 / 替代分配 / 结果影响评估 → 结案。

设计目标（与质量负责人约定一一对应）：

- 按批次、箱段、容器或“检测影响范围”发起带版本的行动；
- 相同通知重放只产生一组任务；来源相同但内容变化必须等待授权复核；
- 范围扩大要纳入新增对象（含拆分后的子代），范围缩小不得自动解封
  已经“人工隔离”的样品；
- 院区只能操作自己保管的实体，中心质量员可查看全局守恒与未确认回执；
- 跨机构通知只做最小披露，禁止患者身份字段；
- 离线物流回执、拆分父子数量守恒、过期替代品、跨时区截止期限、
  部分失败重试都可表达；系统重启后不得重复销毁或补发；
- 结案报告从每项受影响检测结果追到原批次、召回范围版本与实际保管节点，
  任何未回执或数量差异都显眼，且不能被强行结案。

本模块只做无副作用的事件流校验与汇总，便于合同核对；不连接任何真实系统。
"""

from __future__ import annotations

from datetime import datetime, timezone

from .clinical_supply_allocation import (
    EVENT_KINDS as FORWARD_EVENT_KINDS,
    REQUIRED_FIELDS,
)

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

# 召回与逆向处置链事件（在既有正向链事件之上扩展）
RECALL_KINDS = [
    # 库存拓扑：登记、拆分、发运（用于确定“谁保管什么、父子数量”）
    'ENTITY_REGISTERED',       # 容器/箱段/批次登记入库，归属某保管节点
    'ENTITY_SPLIT',            # 父容器拆成多个子容器（父子数量必须守恒）
    'SHIPMENT_DISPATCHED',     # 实体移交承运（保管权进入“在途”）
    # 召回行动（带版本）
    'RECALL_NOTICE',           # 召回通知，scope 可为 lot/box/container/result
    'RECALL_SCOPE_REVISION',   # 同一来源通知的范围/内容修订
    'RECALL_AUTHORIZATION',    # 授权复核（内容变化后、结案前均需要）
    'RECALL_TASK',             # 按保管节点分派的逆向处置任务
    'RECALL_TASK_ACK',         # 任务回执（成功 / 离线补传 / 失败重试）
    'IN_TRANSIT_INTERCEPT',    # 在途拦截
    'QUARANTINE_CONFIRMATION', # 隔离确认（可区分人工隔离与系统隔离）
    'RETURN_SHIPMENT',         # 退运
    'DISPOSAL_CERTIFICATE',    # 销毁凭证
    'REPLACEMENT_ALLOCATION',  # 替代分配
    'RESULT_IMPACT_ASSESSMENT',# 受影响检测结果的影响评估
    'RECALL_CLOSURE',          # 结案报告
]

EVENT_KINDS = FORWARD_EVENT_KINDS + RECALL_KINDS

# 召回范围层级
SCOPE_LEVELS = ('lot', 'box', 'container', 'result')
# 逆向处置动作类型
ACTION_TYPES = (
    'intercept_in_transit',  # 在途拦截
    'quarantine_confirm',    # 隔离确认
    'return_to_depot',       # 退运
    'destroy',               # 销毁
    'allocate_replacement',  # 替代分配
    'assess_result_impact',  # 结果影响评估
)
# 隔离来源：人工隔离受“缩小范围不得自动解封”保护
QUARANTINE_REASONS = ('manual', 'recall')
# 回执状态：offline 为离线补传；failed 后必须出现新的重试回执
ACK_STATUSES = ('success', 'offline', 'failed')
# 结果影响结论
IMPACT_VERDICTS = ('reinterpret', 'invalidate', 'no_impact')
# 修订类型
REVISION_TYPES = ('expand', 'narrow')

# 跨机构通知允许出现的患者相关字段（最小披露）：只有去标识研究代号。
# subject_id 本身即研究代号；禁止真实姓名、证件号、病历号、生日等。
FORBIDDEN_IDENTITY_FIELDS = (
    'patient_name', 'real_name', 'id_card_no', 'passport_no',
    'medical_record_no', 'hospital_patient_id', 'date_of_birth', 'phone',
)

# 销毁、补发类“终态/对外”动作，重复事件判定只以 task_id+attempt 为准，
# 同一 task 的终态成功凭证不得出现两张（防止重启后重复销毁/补发）。
_TERMINAL_SUCCESS = {'destroy', 'return_to_depot', 'allocate_replacement'}


# ---------------------------------------------------------------------------
# 基础事件校验
# ---------------------------------------------------------------------------

def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # 统一折算为 UTC，用于跨时区截止期限比较
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_ts_aware(value: object) -> datetime | None:
    """截止期限专用：裸时间（不带时区）一律拒绝，避免跨院区误解。"""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def validate_event(record: dict) -> list[str]:
    """检查样例事件是否具备可交换的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    if "occurred_at" in record and _parse_ts(record.get("occurred_at")) is None:
        problems.append("occurred_at")
    return problems


# ---------------------------------------------------------------------------
# 事件流索引
# ---------------------------------------------------------------------------

def _build_index(events: list[dict]):
    """按 id / 召回号 / 任务号建立索引，并检查基础结构问题。"""
    problems: list[str] = []
    by_id: dict[str, dict] = {}
    notices: dict[str, dict] = {}          # recall_id -> 现行有效通知(v最大)
    notice_versions: dict[str, list[dict]] = {}
    revisions: list[dict] = []
    authz: dict[str, list[dict]] = {}      # recall_id -> 授权（针对版本）
    tasks: dict[str, dict] = {}
    entities: dict[str, dict] = {}         # entity_id -> 登记/最新状态
    children: dict[str, list[str]] = {}    # parent -> [child]
    acks: dict[str, list[dict]] = {}       # task_id -> 回执
    closure: dict[str, dict] = {}
    canonical: list[dict] = []          # 去重后的规范事件（重放副本只计一次）

    for ev in events:
        for p in validate_event(ev):
            problems.append(f"{ev.get('event_id', '?')}: 缺少或非法字段 {p}")
        eid = ev.get("event_id")
        if eid in by_id:
            # 相同事件的幂等重投（重启/补发重放）：整条跳过，不产生第二组动作；
            # 同一 event_id 携带不同内容才是冲突。
            if by_id[eid] == ev:
                continue
            problems.append(f"{eid}: 同一 event_id 内容不一致（疑似篡改或错投）")
            continue
        by_id[eid] = ev
        canonical.append(ev)

        kind = ev.get("kind")
        p = ev.get("payload", {})
        rid = p.get("recall_id")

        if kind == "ENTITY_REGISTERED":
            entities[p.get("entity_id")] = ev
        elif kind == "ENTITY_SPLIT":
            children.setdefault(p.get("parent_entity_id"), []).extend(
                p.get("child_entity_ids", [])
            )
        elif kind == "RECALL_NOTICE":
            notice_versions.setdefault(rid, []).append(ev)
        elif kind == "RECALL_SCOPE_REVISION":
            revisions.append(ev)
            notice_versions.setdefault(rid, []).append(ev)
        elif kind == "RECALL_AUTHORIZATION":
            authz.setdefault(rid, []).append(ev)
        elif kind == "RECALL_TASK":
            tid = p.get("task_id")
            # 相同 event_id 的整事件幂等重投已在上面跳过；能走到这里的
            # 同 task_id 必为另一条事件 -> 重复任务，重放不得再生成一组。
            if tid in tasks:
                problems.append(
                    f"{eid}: task_id {tid} 重复，相同通知重放只能产生一组任务")
            else:
                tasks[tid] = ev
            acks.setdefault(tid, [])
        elif kind == "RECALL_TASK_ACK":
            acks.setdefault(p.get("task_id"), []).append(ev)
        elif kind == "RECALL_CLOSURE":
            closure[rid] = ev

    # 现行通知 = 版本号最大者；相同 (来源,版本) 的重投只保留一条
    for rid, evs in notice_versions.items():
        dedup: dict[tuple, dict] = {}
        for ev in evs:
            p = ev["payload"]
            dedup.setdefault((p.get("source_ref"), p.get("version", 1)), ev)
        kept = list(dedup.values())
        notice_versions[rid] = kept
        notices[rid] = max(kept, key=lambda e: e["payload"].get("version", 1))

    return {
        "problems": problems,
        "by_id": by_id,
        "canonical": canonical,
        "notices": notices,
        "notice_versions": notice_versions,
        "revisions": revisions,
        "authz": authz,
        "tasks": tasks,
        "entities": entities,
        "children": children,
        "acks": acks,
        "closure": closure,
    }


# ---------------------------------------------------------------------------
# 单条业务规则
# ---------------------------------------------------------------------------

def _is_identity_minimal(ev: dict) -> bool:
    """跨机构通知/任务只允许去标识研究代号，禁止真实身份字段。"""
    payload = ev.get("payload", {})
    if any(k in payload for k in FORBIDDEN_IDENTITY_FIELDS):
        return False
    target = payload.get("affected_subjects")
    if target is not None and not isinstance(target, list):
        return False
    return True


def validate_recall_chain(events: list[dict]) -> list[str]:
    """对完整事件流做召回逆向处置链校验，返回可读的违规说明列表（空=通过）。

    纯函数：同一份事件（含重放、重启后重放）输出恒定，可安全重复执行，
    不会据此触发任何销毁或补发。
    """
    idx = _build_index(events)
    out: list[str] = idx["problems"]

    def err(msg: str) -> None:
        out.append(msg)

    # -- 1. 召回通知：版本、幂等重放、内容变更待授权 ----------------------
    for rid, evs in idx["notice_versions"].items():
        seen: dict[tuple, str] = {}
        for ev in evs:
            p = ev["payload"]
            if ev["kind"] == "RECALL_NOTICE":
                if p.get("scope_level") not in SCOPE_LEVELS:
                    err(f"{ev['event_id']}: scope_level 非法")
                if not isinstance(p.get("scope_refs"), list) or not p["scope_refs"]:
                    err(f"{ev['event_id']}: scope_refs 必须为非空列表")
                if not _is_identity_minimal(ev):
                    err(f"{ev['event_id']}: 跨机构通知含未脱敏的患者身份字段")
                key = (p.get("source_ref"), p.get("version"))
                if key in seen:
                    # 相同通知重放：允许但必须只产生同一组任务（在任务段校验）
                    continue
                seen[key] = ev["event_id"]

    # 修订必须 expand/narrow，且版本递增、来源相同（与前一版本逐项比较）
    current = idx["notices"]
    for rid, evs in idx["notice_versions"].items():
        ordered = sorted(evs, key=lambda e: e["payload"].get("version", 1))
        for prev_e, rev in zip(ordered, ordered[1:]):
            if rev["kind"] != "RECALL_SCOPE_REVISION":
                continue
            p = rev["payload"]
            if p.get("change_type") not in REVISION_TYPES:
                err(f"{rev['event_id']}: change_type 非法")
            if p.get("source_ref") != prev_e["payload"].get("source_ref"):
                err(f"{rev['event_id']}: 修订来源与原通知不一致")
            if p.get("version", 1) <= prev_e["payload"].get("version", 1):
                err(f"{rev['event_id']}: 修订版本号未递增")
            if not _is_identity_minimal(rev):
                err(f"{rev['event_id']}: 修订通知含未脱敏的患者身份字段")

    # -- 2. 授权闸门：内容变化后，未获针对该版本授权不得推进 --------------
    def authorized_version(rid: str) -> int | None:
        vers = [
            a["payload"].get("scope_version")
            for a in idx["authz"].get(rid, [])
            if a["payload"].get("decision") == "approved"
        ]
        return max(vers) if vers else None

    for rid, notice in current.items():
        av = authorized_version(rid)
        need_auth = bool(notice["payload"].get("requires_authorization", True))
        if need_auth and av is None:
            err(f"召回 {rid}: 缺少授权复核")
        elif need_auth and av is not None and av < notice["payload"].get("version", 1):
            err(f"召回 {rid}: 内容已变化到 v{notice['payload'].get('version')}，"
                f"仍停留在已授权 v{av}，须等待授权复核")

    # -- 3. 实体拆分数量守恒 ---------------------------------------------
    for ev in idx["canonical"]:
        if ev.get("kind") != "ENTITY_SPLIT":
            continue
        p = ev["payload"]
        parent = idx["entities"].get(p.get("parent_entity_id"))
        if parent is None:
            err(f"{ev['event_id']}: 父实体未登记")
            continue
        pq = parent["payload"].get("quantity", 0)
        cq = sum(p.get("child_quantities", []))
        if pq != cq:
            err(f"{ev['event_id']}: 拆分数量不守恒 "
                f"父={pq} 子合计={cq}（差异 {pq - cq}）")
        if len(p.get("child_entity_ids", [])) != len(p.get("child_quantities", [])):
            err(f"{ev['event_id']}: 子容器与子数量条目不一致")

    # -- 4. 任务：保管权、范围归属、截止期限、最小披露、动作合法性 --------
    # 计算每个实体当前保管节点（登记 custodian；发运后转为在途 custodian）
    custodian: dict[str, str] = {}
    for eid, ev in idx["entities"].items():
        custodian[eid] = ev["payload"].get("custodian_site")
    for ev in idx["canonical"]:
        if ev.get("kind") == "SHIPMENT_DISPATCHED":
            for e in ev["payload"].get("entity_ids", []):
                custodian[e] = f"TRANSIT::{ev['payload'].get('carrier')}"
        elif ev.get("kind") == "IN_TRANSIT_INTERCEPT" and \
                ev["payload"].get("status") == "intercepted":
            # 拦截确认后实体移交到实际扣留节点（如退运仓），该院区方可继续处置
            custodian[ev["payload"].get("entity_id")] = \
                ev["payload"].get("holding_site")

    # 当前召回范围（展开拆分子代）与被“缩小移除”的实体
    scoped: dict[str, set[str]] = {}
    removed_by_narrow: dict[str, set[str]] = {}
    manually_quarantined: set[str] = set()

    def expand_refs(refs: list[str]) -> set[str]:
        out_refs = set(refs)
        stack = list(refs)
        while stack:
            cur = stack.pop()
            for ch in idx["children"].get(cur, []):
                if ch not in out_refs:
                    out_refs.add(ch)
                    stack.append(ch)
        return out_refs

    for rid, notice in current.items():
        p = notice["payload"]
        refs = expand_refs(p.get("scope_refs", []))
        scoped[rid] = refs
        # 找出曾在旧版本、但不在现行版本的 refs（被缩小移除）
        ever: set[str] = set()
        for old in idx["notice_versions"][rid]:
            ever |= expand_refs(old["payload"].get("scope_refs", []))
        removed_by_narrow[rid] = ever - refs

    # 人工隔离集合（在任务分派前先扫描，保证“缩小不能解封人工隔离”）
    for ev in idx["canonical"]:
        if ev.get("kind") == "QUARANTINE_CONFIRMATION" and \
                ev["payload"].get("reason") == "manual":
            manually_quarantined.add(ev["payload"].get("entity_id"))
    # LOT_QUARANTINED 视为对该批次实体的人工/既有隔离
    for ev in idx["canonical"]:
        if ev.get("kind") == "LOT_QUARANTINED":
            lot = ev["payload"].get("lot_id")
            for eid, reg in idx["entities"].items():
                if reg["payload"].get("lot_id") == lot:
                    manually_quarantined.add(eid)

    # 每个 (recall, 实体, 动作) 只允许一条有效任务（重放收敛为一组任务）
    task_signatures: dict[tuple, str] = {}
    tasks_by_recall: dict[str, list[dict]] = {}
    for tid, ev in idx["tasks"].items():
        p = ev["payload"]
        rid = p.get("recall_id")
        tasks_by_recall.setdefault(rid, []).append(ev)

        if p.get("action") not in ACTION_TYPES:
            err(f"{ev['event_id']}: action 非法")
        if rid not in current:
            err(f"{ev['event_id']}: 任务引用未知召回 {rid}")
            continue
        # 任务所针对版本必须已授权
        av = authorized_version(rid)
        tv = p.get("scope_version", current[rid]["payload"].get("version", 1))
        if av is not None and tv > av:
            err(f"{ev['event_id']}: 任务基于未授权的范围 v{tv}")
        if not _is_identity_minimal(ev):
            err(f"{ev['event_id']}: 任务含未脱敏的患者身份字段")

        # 跨时区截止期限：deadline 必须带时区，且不早于任务创建时间
        dl = _parse_ts_aware(p.get("deadline_at"))
        created = _parse_ts(ev.get("occurred_at"))
        if dl is None:
            err(f"{ev['event_id']}: deadline_at 缺失或不可解析（须带时区）")
        elif created is not None and dl < created:
            err(f"{ev['event_id']}: 截止期限早于任务创建时间（UTC 比较）")

        refs = p.get("entity_ids", [])
        if p.get("action") == "assess_result_impact":
            if not p.get("result_ids"):
                err(f"{ev['event_id']}: 结果影响评估任务缺少 result_ids")
        else:
            if not refs:
                err(f"{ev['event_id']}: 处置任务缺少 entity_ids")

        site = p.get("assigned_site")
        for ref in refs:
            # 保管权：院区只能操作自己保管的实体；在途实体仅承运拦截任务可触达
            holder = custodian.get(ref)
            if holder is None:
                # 可能是批次/箱段层级引用，放行由层级展开处理
                continue
            if str(holder).startswith("TRANSIT::"):
                if p.get("action") != "intercept_in_transit":
                    err(f"{ev['event_id']}: 实体 {ref} 在途，"
                        f"院区 {site} 不得直接处置，须先在途拦截")
            elif holder != site:
                err(f"{ev['event_id']}: 保管权冲突：{ref} 由 {holder} 保管，"
                    f"不得分派给 {site}")
            # 缩小范围后，人工隔离实体不得出现“解封/释放”类动作
            if ref in removed_by_narrow.get(rid, set()) and \
                    ref in manually_quarantined and \
                    p.get("action") in ("allocate_replacement",):
                err(f"{ev['event_id']}: 实体 {ref} 已人工隔离，"
                    f"范围缩小不得自动解封/重新分配")

        sig = (rid, tuple(sorted(refs)),
               tuple(sorted(p.get("result_ids", []))), p.get("action"))
        if sig in task_signatures:
            err(f"{ev['event_id']}: 与 {task_signatures[sig]} 重复的任务，"
                f"相同通知重放只能产生一组任务")
        else:
            task_signatures[sig] = ev["event_id"]

    # 范围扩大：新增对象必须被任务覆盖（在扩大后的版本上）
    latest_versions = idx["notice_versions"]
    for rid, evs in latest_versions.items():
        ordered = sorted(
            [e for e in evs if e["kind"] in ("RECALL_NOTICE", "RECALL_SCOPE_REVISION")],
            key=lambda e: e["payload"].get("version", 1),
        )
        for prev_e, next_e in zip(ordered, ordered[1:]):
            if next_e["payload"].get("change_type", "expand") != "expand" and \
                    next_e["kind"] == "RECALL_SCOPE_REVISION":
                continue
            new_refs = expand_refs(next_e["payload"].get("scope_refs", [])) - \
                expand_refs(prev_e["payload"].get("scope_refs", []))
            covered = {
                r for t in tasks_by_recall.get(rid, [])
                for r in t["payload"].get("entity_ids", [])
                if t["payload"].get("scope_version", 1) >=
                next_e["payload"].get("version", 1)
            }
            for r in new_refs:
                if custodian.get(r) and r not in covered:
                    err(f"召回 {rid} v{next_e['payload'].get('version')} "
                        f"扩大范围新增 {r}，但缺少对应处置任务")

    # -- 5. 回执：离线、失败重试、终态唯一 --------------------------------
    for tid, acks in idx["acks"].items():
        task = idx["tasks"].get(tid)
        if task is None:
            err(f"回执引用未知任务 {tid}")
            continue
        action = task["payload"].get("action")
        ordered = sorted(acks, key=lambda a: (a["payload"].get("attempt", 1),
                                              _parse_ts(a["occurred_at"]) or
                                              datetime.min.replace(tzinfo=timezone.utc)))
        successes = 0
        attempts_seen: set[int] = set()
        for a in ordered:
            ap = a["payload"]
            st = ap.get("status")
            if st not in ACK_STATUSES:
                err(f"{a['event_id']}: 回执状态非法")
            if st == "offline" and not ap.get("recorded_at_utc"):
                err(f"{a['event_id']}: 离线回执须补 recorded_at_utc（实际发生时间）")
            if st == "failed" and not ap.get("retryable"):
                err(f"{a['event_id']}: 失败回执须标明 retryable 与原因")
            att = ap.get("attempt", 1)
            if att in attempts_seen and st in ("success", "offline"):
                # 同一次尝试的成功/离线凭证重复 -> 重启重放风险
                err(f"{a['event_id']}: 任务 {tid} attempt {att} 出现重复终态回执，"
                    f"存在重复销毁/补发风险")
            attempts_seen.add(att)
            if st in ("success", "offline"):
                successes += 1
                # 离线补传与正常成功等价，均需携带凭证号
                if not ap.get("reference_no"):
                    err(f"{a['event_id']}: 成功/离线回执缺少凭证号 reference_no")
        # 终态动作只允许一张成功凭证（销毁、退运、替代补发不得重复）
        if action in _TERMINAL_SUCCESS and successes > 1:
            err(f"任务 {tid}（{action}）存在 {successes} 张成功凭证，"
                f"终态动作不得重复执行")
        # 最后一次若仍 failed，必须有更高 attempt 在途（这里只标记未闭环）
        if ordered and ordered[-1]["payload"].get("status") == "failed":
            err(f"任务 {tid}: 最近一次回执仍为失败，部分失败尚未重试闭环")

    # -- 6. 销毁/退运/替代的前置与替代品效期 ------------------------------
    # 销毁必须先有隔离确认；退运必须先拦截或隔离
    quarantine_confirmed: set[str] = set()
    intercepted: set[str] = set()
    for ev in idx["canonical"]:
        if ev.get("kind") == "QUARANTINE_CONFIRMATION" and \
                ev["payload"].get("status") == "confirmed":
            quarantine_confirmed.add(ev["payload"].get("entity_id"))
        if ev.get("kind") == "IN_TRANSIT_INTERCEPT" and \
                ev["payload"].get("status") == "intercepted":
            intercepted.add(ev["payload"].get("entity_id"))

    for ev in idx["canonical"]:
        p = ev.get("payload", {})
        if ev.get("kind") == "DISPOSAL_CERTIFICATE":
            for r in p.get("entity_ids", []):
                if r not in quarantine_confirmed:
                    err(f"{ev['event_id']}: 销毁 {r} 前缺少隔离确认")
        if ev.get("kind") == "RETURN_SHIPMENT":
            for r in p.get("entity_ids", []):
                if r not in quarantine_confirmed and r not in intercepted:
                    err(f"{ev['event_id']}: 退运 {r} 前缺少隔离/拦截确认")
        if ev.get("kind") == "REPLACEMENT_ALLOCATION":
            exp = _parse_ts(p.get("replacement_expires_at"))
            if exp is None:
                err(f"{ev['event_id']}: 替代品缺少效期")
            elif exp < _parse_ts(ev.get("occurred_at")):
                err(f"{ev['event_id']}: 替代品已过期，不得分配")

    # -- 7. 结果影响评估：每项受影响结果必须可溯源 ------------------------
    # result_id -> 评估事件
    assessments: dict[str, dict] = {}
    for ev in idx["canonical"]:
        if ev.get("kind") == "RESULT_IMPACT_ASSESSMENT":
            p = ev["payload"]
            if p.get("verdict") not in IMPACT_VERDICTS:
                err(f"{ev['event_id']}: 评估结论非法")
            for key in ("source_lot_id", "scope_version", "custodian_site_at_test"):
                if not p.get(key):
                    err(f"{ev['event_id']}: 评估缺少溯源字段 {key}")
            for r in p.get("result_ids", []):
                if r in assessments:
                    prev = assessments[r]["payload"]
                    # 范围升版后允许以更高版本“取代”旧评估，结案以最新版本为准
                    if p.get("supersedes") and \
                            p.get("scope_version") > prev.get("scope_version"):
                        assessments[r] = ev
                    else:
                        err(f"{ev['event_id']}: 结果 {r} 重复评估"
                            f"（升版评估须标记 supersedes 且版本更高）")
                else:
                    assessments[r] = ev

    # -- 8. 结案：守恒、未回执、数量差异、溯源、授权版本 ------------------
    for rid, closure in idx["closure"].items():
        cp = closure["payload"]
        if rid not in current:
            err(f"{closure['event_id']}: 结案引用未知召回 {rid}")
            continue
        notice = current[rid]
        av = authorized_version(rid)
        if av is None or av < notice["payload"].get("version", 1):
            err(f"召回 {rid}: 现行范围版本尚未授权，不能结案")
        if cp.get("scope_version") != notice["payload"].get("version"):
            err(f"召回 {rid}: 结案报告基于 v{cp.get('scope_version')}，"
                f"与现行 v{notice['payload'].get('version')} 不符")

        # 守恒：受影响数量 = 已销毁 + 已退运 + 仍隔离 + 已替代覆盖
        counted = (cp.get("destroyed_qty", 0) + cp.get("returned_qty", 0)
                   + cp.get("quarantined_remaining_qty", 0))
        if counted != cp.get("affected_qty", 0):
            err(f"召回 {rid}: 结案数量不守恒 受影响={cp.get('affected_qty')} "
                f"销毁+退运+在隔={counted}（差异 {cp.get('affected_qty', 0) - counted}）")

        # 该召回全部任务必须成功闭环（离线补传视同成功）
        for t in tasks_by_recall.get(rid, []):
            tid = t["payload"].get("task_id")
            acks = idx["acks"].get(tid, [])
            ok = [a for a in acks if a["payload"].get("status") in ("success", "offline")]
            if not ok:
                err(f"召回 {rid}: 任务 {tid} 无确认回执，不能结案")

        # 每项受影响结果都须有评估，且评估的范围版本/批次与结案一致
        for r in cp.get("affected_result_ids", []):
            a = assessments.get(r)
            if a is None:
                err(f"召回 {rid}: 受影响结果 {r} 缺少影响评估，不能结案")
                continue
            if a["payload"].get("scope_version") != cp.get("scope_version"):
                err(f"召回 {rid}: 结果 {r} 的评估版本与结案版本不一致")
            if a["payload"].get("source_lot_id") != cp.get("original_lot_id"):
                err(f"召回 {rid}: 结果 {r} 无法追溯到原批次 "
                    f"{cp.get('original_lot_id')}")

        if cp.get("force", False):
            err(f"召回 {rid}: 结案报告带 force 标记——"
                f"未回执/数量差异不得被强行结案")

    return out


# ---------------------------------------------------------------------------
# 全局看板（中心质量员视图）
# ---------------------------------------------------------------------------

def recall_dashboard(events: list[dict]) -> dict:
    """汇总各召回的守恒与回执状态。院区视图应只过滤自己保管的实体。

    不暴露任何患者身份字段，仅含研究/批次/容器维度的计数。
    """
    idx = _build_index(events)
    board: dict[str, dict] = {}
    for rid, notice in idx["notices"].items():
        task_rows = []
        pending_tasks = []
        for tid, t in idx["tasks"].items():
            if t["payload"].get("recall_id") != rid:
                continue
            acks = idx["acks"].get(tid, [])
            last = sorted(
                acks,
                key=lambda a: a["payload"].get("attempt", 1),
            )[-1] if acks else None
            status = (last["payload"].get("status") if last else "unacknowledged")
            row = {
                "task_id": tid,
                "action": t["payload"].get("action"),
                "assigned_site": t["payload"].get("assigned_site"),
                "last_status": status,
                "offline": bool(last and last["payload"].get("status") == "offline"),
                "attempts": max((a["payload"].get("attempt", 1) for a in acks),
                                default=0),
            }
            task_rows.append(row)
            if status in ("unacknowledged", "failed"):
                pending_tasks.append(tid)

        closure = idx["closure"].get(rid)
        qty_gap = None
        if closure:
            cp = closure["payload"]
            qty_gap = cp.get("affected_qty", 0) - (
                cp.get("destroyed_qty", 0) + cp.get("returned_qty", 0)
                + cp.get("quarantined_remaining_qty", 0))

        board[rid] = {
            "current_version": notice["payload"].get("version", 1),
            "scope_level": notice["payload"].get("scope_level"),
            "scope_refs": sorted(notice["payload"].get("scope_refs", [])),
            "authorized_version": max(
                (a["payload"].get("scope_version", 1)
                 for a in idx["authz"].get(rid, [])
                 if a["payload"].get("decision") == "approved"),
                default=None),
            "tasks": task_rows,
            "unacknowledged_or_failed_tasks": pending_tasks,
            "quantity_gap": qty_gap,
            "closed": closure is not None,
            "violations": [m for m in validate_recall_chain(events) if rid in m],
        }
    return board
