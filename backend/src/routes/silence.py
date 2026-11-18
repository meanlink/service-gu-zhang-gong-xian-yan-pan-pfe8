from fastapi import APIRouter, Depends
from sqlalchemy import select

from src.config.database import get_db
from src.models import SilencePlan, SilenceReplay, SilenceBrief
from src.schemas.silence import SilencePlanInput, SilencePlanUpdate, SilenceBriefInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import silence as svc
from src.services.audit import record
from src.utils.silence_serialization import plan_dict, replay_dict, brief_dict

router = APIRouter(prefix="/silence")


@router.get("/plans")
def list_plans(db=Depends(get_db), u=Depends(current_user)):
    plans = db.scalars(select(SilencePlan).order_by(SilencePlan.id.desc()))
    return [plan_dict(p) for p in plans]


@router.post("/plans", status_code=201)
def create_plan(data: SilencePlanInput, db=Depends(get_db), u=Depends(can_write)):
    plan = svc.create_plan(db, data, u)
    record(db, u.email, "silence.plan.create",
           {"id": plan.id, "version": plan.params_version})
    db.commit()
    return plan_dict(plan)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, db=Depends(get_db), u=Depends(current_user)):
    plan = svc._get_plan(db, plan_id)
    data = plan_dict(plan)
    replays = db.scalars(
        select(SilenceReplay).where(SilenceReplay.plan_id == plan_id)
        .order_by(SilenceReplay.id.desc()))
    data["replays"] = [replay_dict(r) for r in replays]
    return data


@router.patch("/plans/{plan_id}")
def update_plan(plan_id: int, data: SilencePlanUpdate,
                db=Depends(get_db), u=Depends(can_write)):
    plan = svc._get_plan(db, plan_id)
    old_version = plan.params_version
    plan = svc.update_plan(db, plan, data, u)
    record(db, u.email, "silence.plan.update",
           {"id": plan.id, "old_version": old_version,
            "new_version": plan.params_version, "re_approval_required": old_version != plan.params_version})
    db.commit()
    return plan_dict(plan)


@router.post("/plans/{plan_id}/replays", status_code=201)
def start_replay(plan_id: int, db=Depends(get_db), u=Depends(can_write)):
    replay = svc.start_replay(db, plan_id, u)
    record(db, u.email, "silence.replay.start",
           {"id": replay.id, "plan_id": plan_id, "version": replay.params_version})
    db.commit()
    return replay_dict(replay)


@router.post("/replays/{replay_id}/advance")
def advance_replay(replay_id: int, db=Depends(get_db), u=Depends(can_write)):
    replay = svc.advance_replay(db, replay_id)
    record(db, u.email, "silence.replay.advance",
           {"id": replay.id, "segment": replay.processed_segments,
            "processed": replay.processed_events, "status": replay.status})
    db.commit()
    return replay_dict(replay)


@router.post("/replays/{replay_id}/run-to-end")
def run_to_end(replay_id: int, db=Depends(get_db), u=Depends(can_write)):
    """连续推进到完成；每个分段仍走同一确定性入口，便于验收断点接续。"""
    replay = svc._get_replay(db, replay_id)
    guard = 0
    while replay.status != "completed" and guard <= replay.total_segments + 1:
        replay = svc.advance_replay(db, replay_id)
        guard += 1
    record(db, u.email, "silence.replay.run_to_end",
           {"id": replay.id, "segments": replay.processed_segments})
    db.commit()
    return replay_dict(replay, include_decisions=True)


@router.get("/replays/{replay_id}")
def get_replay(replay_id: int, db=Depends(get_db), u=Depends(current_user)):
    replay = svc._get_replay(db, replay_id)
    return replay_dict(replay, include_decisions=True)


@router.post("/replays/{replay_id}/approve")
def approve_replay(replay_id: int, db=Depends(get_db), u=Depends(admin_only)):
    replay, plan = svc.approve_replay(db, replay_id, u)
    record(db, u.email, "silence.replay.approve",
           {"id": replay_id, "plan_id": plan.id, "version": plan.params_version})
    db.commit()
    return replay_dict(replay, include_decisions=True)


@router.post("/plans/{plan_id}/briefs", status_code=201)
def create_brief(plan_id: int, data: SilenceBriefInput,
                 db=Depends(get_db), u=Depends(admin_only)):
    brief = svc.create_brief(db, plan_id, data, u)
    record(db, u.email, "silence.brief.create",
           {"id": brief.id, "plan_id": plan_id, "version": brief.params_version})
    db.commit()
    return brief_dict(brief)


@router.get("/plans/{plan_id}/briefs")
def list_briefs(plan_id: int, db=Depends(get_db), u=Depends(current_user)):
    svc._get_plan(db, plan_id)
    rows = db.scalars(
        select(SilenceBrief).where(SilenceBrief.plan_id == plan_id)
        .order_by(SilenceBrief.id.desc()))
    return [brief_dict(b) for b in rows]


@router.get("/briefs")
def all_briefs(db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(SilenceBrief).order_by(SilenceBrief.id.desc()))
    return [brief_dict(b) for b in rows]
