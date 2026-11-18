"""静默回放 API 集成测试：SQLite + FastAPI TestClient，不依赖 Postgres/Redis。"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="silence-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SEED_DEMO"] = "false"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["ARTIFACT_DIR"] = _tmp
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from src.main import app  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.models import User, Audit  # noqa: E402
from src.utils.security import hash_password  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from migrations.initial import migrate
    migrate()
    with Session() as db:
        for role, name in [("admin", "负责人"), ("analyst", "分析员"), ("viewer", "观察员")]:
            email = f"{role}@test.local"
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, name=name, role=role,
                            password=hash_password("Pass123!")))
        db.commit()
    with TestClient(app) as c:
        yield c


def login(client, role):
    r = client.post("/auth/login", json={"email": f"{role}@test.local", "password": "Pass123!"})
    assert r.status_code == 200
    return r.json()["access_token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def make_dataset(client, token, records, name=None):
    name = name or f"批次-{len(records)}-{id(records)}"
    r = client.post("/datasets", headers=auth(token),
                    json={"name": name, "source": "test.json", "records": records})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def ev(eid, service, minute, severity):
    return {"event_id": eid, "service": service, "occurred_min": minute, "severity": severity}


EDGES = [{"upstream": "cache", "downstream": "api"},
         {"upstream": "api", "downstream": "queue"}]


def test_viewer_cannot_create_plan(client):
    viewer = login(client, "viewer")
    r = client.post("/silence/plans", headers=auth(viewer),
                    json={"name": "无权方案", "dataset_id": 1, "edges": EDGES})
    assert r.status_code == 403


def test_full_flow_segmented_replay_and_credentials(client):
    analyst = login(client, "analyst")
    records = [
        ev("E1", "cache", 0, 1), ev("E2", "api", 1, 2),
        ev("E3", "cache", 10, 2), ev("E4", "api", 11, 2),
        ev("E5", "queue", 12, 2), ev("E6", "object-store", 30, 3),
    ]
    ds = make_dataset(client, analyst, records, "分段接续批次")
    r = client.post("/silence/plans", headers=auth(analyst), json={
        "name": "分段接续方案", "dataset_id": ds, "edges": EDGES,
        "direction": "upstream", "window_min": 5, "severe_levels": [1],
        "segment_size": 2})
    assert r.status_code == 201, r.text
    plan = r.json()
    assert plan["params_version"] == 1 and plan["approved"] is False

    r = client.post(f"/silence/plans/{plan['id']}/replays", headers=auth(analyst))
    assert r.status_code == 201
    replay = r.json()
    assert replay["total_segments"] == 3 and replay["processed_segments"] == 0

    # 逐段推进，校验中断恢复接续
    last = replay
    while last["status"] != "completed":
        r = client.post(f"/silence/replays/{last['id']}/advance", headers=auth(analyst))
        assert r.status_code == 200
        current = r.json()
        assert current["processed_segments"] == last["processed_segments"] + 1
        last = current
    assert last["processed_events"] == 6

    detail = client.get(f"/silence/replays/{last['id']}", headers=auth(analyst)).json()
    by_id = {d["event_id"]: d for d in detail["decisions"]}
    # 重度常显
    assert by_id["E1"]["visible"] and by_id["E1"]["reason"] == "severe_always_visible"
    # 轻度被时窗内可见上游静默，凭据可查
    assert by_id["E2"]["silenced"] and by_id["E2"]["credential_event_id"] == "E1"
    # E4 被 E3（可见轻度）静默；E5 的上游 E4 不可见，故 E5 仍可见——告警链不会整体失明
    assert by_id["E4"]["silenced"] and by_id["E4"]["credential_event_id"] == "E3"
    assert by_id["E5"]["visible"] and by_id["E5"]["credential_event_id"] is None
    # 同刻不互相压制由引擎单测覆盖；此处确认服务统计
    stats = {s["service"]: s for s in detail["service_stats"]}
    assert stats["queue"]["visible"] == 1 and stats["api"]["silenced"] == 2


def test_approval_gate_rejects_fully_silenced_service(client):
    admin = login(client, "admin")
    analyst = login(client, "analyst")
    records = [
        ev("F1", "cache", 0, 1), ev("F2", "api", 1, 2),
        ev("F3", "cache", 10, 1), ev("F4", "api", 11, 2),
        ev("F5", "queue", 40, 3), ev("F6", "object-store", 50, 3),
    ]
    ds = make_dataset(client, analyst, records, "全压拒绝批次")
    plan = client.post("/silence/plans", headers=auth(analyst), json={
        "name": "全压拒绝方案", "dataset_id": ds, "edges": EDGES,
        "window_min": 5, "segment_size": 3}).json()
    replay = client.post(f"/silence/plans/{plan['id']}/replays",
                         headers=auth(analyst)).json()
    done = client.post(f"/silence/replays/{replay['id']}/run-to-end",
                       headers=auth(analyst)).json()
    assert done["status"] == "completed"

    r = client.post(f"/silence/replays/{replay['id']}/approve", headers=auth(admin))
    assert r.status_code == 422
    payload = r.json()["detail"]
    assert payload["reasons"][0]["code"] == "service_all_silenced"
    assert "api" in payload["reasons"][0]["service"]

    # 方案仍未获准，简报被拒
    r = client.post(f"/silence/plans/{plan['id']}/briefs", headers=auth(admin),
                    json={"name": "不应发出的简报"})
    assert r.status_code == 409


def test_modification_bumps_version_resets_approval_and_blocks_old_replay(client):
    admin = login(client, "admin")
    analyst = login(client, "analyst")
    records = [
        ev("G1", "cache", 0, 1), ev("G2", "api", 1, 2),
        ev("G3", "api", 20, 2), ev("G4", "queue", 40, 2),
        ev("G5", "object-store", 60, 3),
    ]
    ds = make_dataset(client, analyst, records, "可批准批次")
    plan = client.post("/silence/plans", headers=auth(analyst), json={
        "name": "可批准方案", "dataset_id": ds, "edges": EDGES,
        "window_min": 5, "segment_size": 2}).json()
    replay = client.post(f"/silence/plans/{plan['id']}/replays",
                         headers=auth(analyst)).json()
    client.post(f"/silence/replays/{replay['id']}/run-to-end", headers=auth(analyst))
    r = client.post(f"/silence/replays/{replay['id']}/approve", headers=auth(admin))
    assert r.status_code == 200
    plan_after = client.get(f"/silence/plans/{plan['id']}", headers=auth(admin)).json()
    assert plan_after["approved"] is True

    # 改静默方案：升版本 + 撤批准，须重审
    r = client.patch(f"/silence/plans/{plan['id']}", headers=auth(analyst),
                     json={"window_min": 8})
    assert r.status_code == 200
    updated = r.json()
    assert updated["params_version"] == 2 and updated["approved"] is False

    # 旧版本回放不能再推进
    r = client.post(f"/silence/replays/{replay['id']}/advance", headers=auth(analyst))
    assert r.status_code == 409

    # 新版本回放跑通后再批准
    replay2 = client.post(f"/silence/plans/{plan['id']}/replays",
                          headers=auth(analyst)).json()
    assert replay2["params_version"] == 2
    done2 = client.post(f"/silence/replays/{replay2['id']}/run-to-end",
                        headers=auth(analyst)).json()
    assert done2["status"] == "completed"
    r = client.post(f"/silence/replays/{replay2['id']}/approve", headers=auth(admin))
    assert r.status_code == 200


def test_brief_freezes_approved_snapshot_and_survives_later_edits(client):
    admin = login(client, "admin")
    analyst = login(client, "analyst")
    records = [
        ev("H1", "cache", 0, 1), ev("H2", "api", 1, 2),
        ev("H3", "api", 20, 2), ev("H4", "queue", 45, 2),
        ev("H5", "object-store", 70, 3),
    ]
    ds = make_dataset(client, analyst, records, "简报快照批次")
    plan = client.post("/silence/plans", headers=auth(analyst), json={
        "name": "简报快照方案", "dataset_id": ds, "edges": EDGES,
        "window_min": 5, "segment_size": 2}).json()
    replay = client.post(f"/silence/plans/{plan['id']}/replays",
                         headers=auth(analyst)).json()
    client.post(f"/silence/replays/{replay['id']}/run-to-end", headers=auth(analyst))
    client.post(f"/silence/replays/{replay['id']}/approve", headers=auth(admin))

    r = client.post(f"/silence/plans/{plan['id']}/briefs", headers=auth(admin),
                    json={"name": "值班静默简报 · 第1期"})
    assert r.status_code == 201
    brief = r.json()
    assert brief["params_version"] == 1
    assert brief["snapshot"]["params"]["window_min"] == 5

    # 之后修改方案：已发简报保留原依据
    client.patch(f"/silence/plans/{plan['id']}", headers=auth(analyst),
                 json={"window_min": 12})
    briefs = client.get(f"/silence/plans/{plan['id']}/briefs",
                        headers=auth(admin)).json()
    assert len(briefs) == 1
    assert briefs[0]["params_version"] == 1
    assert briefs[0]["snapshot"]["params"]["window_min"] == 5
    assert briefs[0]["basis_checksum"] == brief["basis_checksum"]

    # 修改后未获准，不能再发新简报（修改与分发并发时只收当前获准回放）
    r = client.post(f"/silence/plans/{plan['id']}/briefs", headers=auth(admin),
                    json={"name": "值班静默简报 · 第2期"})
    assert r.status_code == 409


def test_segmented_and_run_to_end_replays_agree(client):
    analyst = login(client, "analyst")
    records = [ev(f"I{i}", "cache" if i % 2 == 0 else "api", i * 3,
                  1 if i % 4 == 0 else 2) for i in range(8)]
    ds = make_dataset(client, analyst, records, "一致性批次")
    plan = client.post("/silence/plans", headers=auth(analyst), json={
        "name": "一致性方案", "dataset_id": ds,
        "edges": [{"upstream": "cache", "downstream": "api"}],
        "window_min": 4, "segment_size": 2}).json()

    r1 = client.post(f"/silence/plans/{plan['id']}/replays",
                     headers=auth(analyst)).json()
    while True:
        cur = client.post(f"/silence/replays/{r1['id']}/advance",
                          headers=auth(analyst)).json()
        if cur["status"] == "completed":
            break
    # 完成后再推进必须幂等，不重复累计
    again = client.post(f"/silence/replays/{r1['id']}/advance",
                        headers=auth(analyst)).json()
    assert again["processed_events"] == cur["processed_events"] == 8

    r2 = client.post(f"/silence/plans/{plan['id']}/replays",
                     headers=auth(analyst)).json()
    full = client.post(f"/silence/replays/{r2['id']}/run-to-end",
                       headers=auth(analyst)).json()

    d1 = client.get(f"/silence/replays/{r1['id']}", headers=auth(analyst)).json()
    key = lambda d: (d["event_id"], d["visible"], d["silenced"],
                     d["credential_event_id"], d["reason"])
    assert [key(d) for d in d1["decisions"]] == [key(d) for d in full["decisions"]]


def test_downstream_direction_and_same_timestamp(client):
    analyst = login(client, "analyst")
    records = [
        ev("J1", "queue", 0, 1), ev("J2", "api", 1, 2),
        ev("J3", "cache", 2, 2), ev("J4", "cache", 2, 2),
        ev("J5", "object-store", 80, 3),
    ]
    ds = make_dataset(client, analyst, records, "反向静默批次")
    plan = client.post("/silence/plans", headers=auth(analyst), json={
        "name": "反向静默方案", "dataset_id": ds, "edges": EDGES,
        "direction": "downstream", "window_min": 5, "segment_size": 5}).json()
    replay = client.post(f"/silence/plans/{plan['id']}/replays",
                         headers=auth(analyst)).json()
    done = client.post(f"/silence/replays/{replay['id']}/run-to-end",
                       headers=auth(analyst)).json()
    by_id = {d["event_id"]: d for d in done["decisions"]}
    # 反向：下游 queue 重度可见，静默上游 api
    assert by_id["J2"]["silenced"] and by_id["J2"]["credential_service"] == "queue"


def test_audit_trail_records_silence_actions(client):
    with Session() as db:
        actions = {a.action for a in db.scalars(select(Audit)).all()}
    assert "silence.plan.create" in actions
    assert "silence.replay.approve" in actions
    assert "silence.brief.create" in actions
    assert "silence.plan.update" in actions
