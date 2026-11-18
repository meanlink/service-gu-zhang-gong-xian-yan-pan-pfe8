"""回放静默的业务服务：方案、分段回放、批准、简报。"""
import time
from fastapi import HTTPException
from sqlalchemy import select

from src.models import Dataset, SilencePlan, SilenceReplay, SilenceBrief
from src.services import silence_engine as engine
from src.utils.serialization import checksum


def _edges_to_pairs(edges):
    pairs = []
    seen = set()
    for edge in edges or []:
        pair = (edge.upstream.strip(), edge.downstream.strip())
        if pair in seen or pair[0] == pair[1]:
            continue
        seen.add(pair)
        pairs.append(list(pair))
    return pairs


def _params_from_plan(plan):
    return engine.validate_params(
        plan.direction, plan.window_min, plan.severe_levels, plan.segment_size)


def _validate_edges_cover(events, edges):
    services = {e["service"] for e in events}
    edge_services = {s for pair in edges for s in pair}
    unknown = edge_services - services
    if unknown:
        raise HTTPException(422, f"调用链包含批次中不存在的服务：{', '.join(sorted(unknown))}")


def create_plan(db, data, user):
    dataset = db.get(Dataset, data.dataset_id)
    if not dataset:
        raise HTTPException(404, "数据批次不存在，无法选定回放。")
    name = data.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "方案名称去除空格后至少需要两个字符。")
    if db.scalar(select(SilencePlan).where(SilencePlan.name == name)):
        raise HTTPException(409, "已存在同名静默方案，请修改名称。")

    params = engine.validate_params(
        data.direction, data.window_min, data.severe_levels, data.segment_size)
    events = engine.normalize_events(dataset.records)
    edges = _edges_to_pairs(data.edges)
    _validate_edges_cover(events, edges)

    now = time.time()
    plan = SilencePlan(
        name=name, dataset_id=dataset.id, dataset_checksum=dataset.checksum,
        records_snapshot=[dict(r) for r in dataset.records], edges=edges,
        direction=params["direction"], window_min=params["window_min"],
        severe_levels=params["severe_levels"], segment_size=params["segment_size"],
        params_version=1, approved=False, created_by=user.id,
        created_at=now, updated_at=now)
    db.add(plan)
    db.flush()
    return plan


def update_plan(db, plan, data, user):
    """修改方案：任一口径变化都升版本并撤销已批准状态，须重审。"""
    events = engine.normalize_events(plan.records_snapshot)
    incoming = data.model_dump(exclude_unset=True)
    new_edges = _edges_to_pairs(data.edges) if "edges" in incoming else plan.edges
    _validate_edges_cover(events, new_edges)

    params = engine.validate_params(
        incoming.get("direction", plan.direction),
        incoming.get("window_min", plan.window_min),
        incoming.get("severe_levels", plan.severe_levels),
        incoming.get("segment_size", plan.segment_size))

    before = {
        "edges": [list(p) for p in plan.edges], "direction": plan.direction,
        "window_min": plan.window_min, "severe_levels": list(plan.severe_levels),
        "segment_size": plan.segment_size,
    }
    after = {
        "edges": [list(p) for p in new_edges], "direction": params["direction"],
        "window_min": params["window_min"], "severe_levels": params["severe_levels"],
        "segment_size": params["segment_size"],
    }
    if before != after:
        plan.params_version += 1
        plan.approved = False
        plan.approved_at = None
        plan.approved_by = None
    plan.edges = after["edges"]
    plan.direction = after["direction"]
    plan.window_min = after["window_min"]
    plan.severe_levels = after["severe_levels"]
    plan.segment_size = after["segment_size"]
    plan.updated_at = time.time()
    db.flush()
    return plan


def _get_plan(db, plan_id):
    plan = db.get(SilencePlan, plan_id)
    if not plan:
        raise HTTPException(404, "静默方案不存在。")
    return plan


def _get_replay(db, replay_id):
    replay = db.get(SilenceReplay, replay_id)
    if not replay:
        raise HTTPException(404, "回放不存在。")
    return replay


def start_replay(db, plan_id, user):
    """为方案当前参数版本创建一条分段回放；旧版本回放保留作历史。"""
    plan = _get_plan(db, plan_id)
    params = _params_from_plan(plan)
    events = engine.normalize_events(plan.records_snapshot)
    segments = engine.plan_segments(events, params["segment_size"])
    now = time.time()
    replay = SilenceReplay(
        plan_id=plan.id, params_version=plan.params_version, status="in_progress",
        total_events=len(events), processed_events=0,
        total_segments=len(segments), processed_segments=0,
        cursor_event_id=None, decisions=[], visible_events=[],
        service_stats=[], approval={}, created_by=user.id,
        created_at=now, updated_at=now)
    db.add(replay)
    db.flush()
    return replay


def advance_replay(db, replay_id):
    """处理且仅处理下一个分段；中断恢复后沿已算时刻接续，重试同段幂等。"""
    replay = _get_replay(db, replay_id)
    plan = _get_plan(db, replay.plan_id)
    params = _params_from_plan(plan)
    events = engine.normalize_events(plan.records_snapshot)
    segments = engine.plan_segments(events, params["segment_size"])

    if replay.params_version != plan.params_version:
        raise HTTPException(409, "方案已修改并升版，旧回放只读；请基于新版本重新发起回放。")

    if replay.processed_segments >= replay.total_segments:
        return replay  # 已全部算完，重复调用不重复累计。

    segment = segments[replay.processed_segments]
    decisions, visible = engine.evaluate_segment(
        segment, replay.visible_events, [tuple(p) for p in plan.edges], params)

    replay.decisions = list(replay.decisions) + decisions
    replay.visible_events = visible
    replay.processed_segments += 1
    replay.processed_events = len(replay.decisions)
    replay.cursor_event_id = decisions[-1]["event_id"] if decisions else replay.cursor_event_id
    replay.updated_at = time.time()

    finished = replay.processed_segments >= replay.total_segments
    if finished:
        replay.status = "completed"
        replay.service_stats = engine.service_stats(
            replay.decisions, plan.severe_levels)
        replay.approval = engine.approval_evaluation(replay.decisions, True)
    else:
        replay.status = "in_progress"
    db.flush()
    return replay


def approve_replay(db, replay_id, user):
    """批准闸门：未完成或任一服务告警全被压掉，一律拒绝。"""
    replay = _get_replay(db, replay_id)
    plan = _get_plan(db, replay.plan_id)
    if replay.params_version != plan.params_version:
        raise HTTPException(409, "该回放基于旧参数版本，请用新版本回放重审。")
    evaluation = engine.approval_evaluation(replay.decisions, replay.status == "completed")
    replay.approval = evaluation
    if not evaluation["approvable"]:
        db.flush()
        raise HTTPException(422, {
            "message": "批准被拒绝：存在不可静默的服务或回放尚未完成。",
            "reasons": evaluation["reasons"],
        })
    plan.approved = True
    plan.approved_at = time.time()
    plan.approved_by = user.id
    plan.updated_at = time.time()
    db.flush()
    return replay, plan


def create_brief(db, plan_id, data, user):
    """简报只收当前获准的回放，并冻结快照；方案后续修改不影响已发简报。"""
    plan = _get_plan(db, plan_id)
    if not plan.approved:
        raise HTTPException(409, "方案当前版本尚未获准，不能生成静默简报。")
    replay = db.scalar(
        select(SilenceReplay)
        .where(SilenceReplay.plan_id == plan.id,
               SilenceReplay.params_version == plan.params_version,
               SilenceReplay.status == "completed")
        .order_by(SilenceReplay.id.desc()))
    if not replay or replay.params_version != plan.params_version:
        raise HTTPException(409, "没有匹配当前获准版本的完成回放，请重新回放并获批。")
    evaluation = engine.approval_evaluation(replay.decisions, True)
    if not evaluation["approvable"]:
        raise HTTPException(422, {"message": "当前回放不满足批准条件。",
                                  "reasons": evaluation["reasons"]})

    snapshot = {
        "plan": {"id": plan.id, "name": plan.name,
                 "params_version": plan.params_version},
        "params": {"direction": plan.direction, "window_min": plan.window_min,
                   "severe_levels": list(plan.severe_levels),
                   "segment_size": plan.segment_size, "edges": plan.edges},
        "replay": {"id": replay.id, "summary": engine.summarize(
            replay.decisions, _params_from_plan(plan), True),
            "service_stats": replay.service_stats},
        "decisions": replay.decisions,
        "approval": evaluation,
        "frozen_at": time.time(),
    }
    brief = SilenceBrief(
        plan_id=plan.id, replay_id=replay.id, name=data.name.strip(),
        params_version=plan.params_version, basis_checksum=checksum(snapshot),
        snapshot=snapshot, created_by=user.id, created_at=time.time())
    db.add(brief)
    db.flush()
    return brief
