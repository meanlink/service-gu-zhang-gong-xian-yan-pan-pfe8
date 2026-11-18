"""静默引擎口径测试（纯标准库，覆盖全部关键业务规则）。"""
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services import silence_engine as engine  # noqa: E402


def ev(eid, service, minute, severity):
    return {"event_id": eid, "service": service, "service_x": service,
            "occurred_min": minute, "severity": severity}


# 调用链：cache -> api -> queue
EDGES = [("cache", "api"), ("api", "queue")]
PARAMS = engine.validate_params("upstream", 5.0, [1], segment_size=2)


def test_severe_always_visible():
    # 重度（severity=1）即使有可见上游也不被静默
    records = [ev("A1", "cache", 0, 1), ev("B1", "api", 1, 1)]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    assert all(d["visible"] for d in decisions)
    assert decisions[1]["reason"] == "severe_always_visible"


def test_minor_silenced_by_visible_upstream_in_window():
    records = [ev("A1", "cache", 0, 1), ev("B1", "api", 3, 2)]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    b1 = next(d for d in decisions if d["event_id"] == "B1")
    assert b1["silenced"] is True
    assert b1["credential_event_id"] == "A1"
    assert b1["credential_service"] == "cache"
    assert b1["lag_min"] == 3


def test_outside_window_not_silenced():
    records = [ev("A1", "cache", 0, 1), ev("B1", "api", 5.5, 2)]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    assert decisions[1]["visible"] is True
    assert decisions[1]["reason"] == "no_visible_upstream_in_window"


def test_same_timestamp_not_mutual_suppression():
    # 同刻：严格更早才算凭据，同刻不互相压制
    records = [ev("A1", "cache", 2.0, 1), ev("B1", "api", 2.0, 2)]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    b1 = next(d for d in decisions if d["event_id"] == "B1")
    assert b1["visible"] is True
    assert b1["credential_event_id"] is None


def test_silenced_minor_cannot_silence_downstream():
    # cache 重度可见 -> api 轻度被静默（不可见）-> queue 轻度无可见凭据，必须仍可见
    records = [
        ev("A1", "cache", 0, 1),
        ev("B1", "api", 1, 2),
        ev("C1", "queue", 2, 2),
    ]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    by_id = {d["event_id"]: d for d in decisions}
    assert by_id["B1"]["silenced"] is True
    assert by_id["C1"]["visible"] is True
    assert by_id["C1"]["credential_event_id"] is None


def test_visible_minor_chain_propagates():
    # cache 轻度但无上游故可见 -> 作为凭据静默 api 轻度
    records = [ev("A1", "cache", 0, 2), ev("B1", "api", 1, 2)]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    assert decisions[0]["visible"] is True
    assert decisions[1]["silenced"] is True
    assert decisions[1]["credential_event_id"] == "A1"


def test_segmented_matches_full_replay():
    # 分段接续结果必须与全量回放逐条一致
    records = [
        ev("A1", "cache", 0, 1), ev("B1", "api", 1, 2),
        ev("A2", "cache", 10, 2), ev("B2", "api", 11, 2),
        ev("C1", "queue", 12, 2), ev("A3", "cache", 20, 1),
        ev("B3", "api", 21, 2), ev("C2", "queue", 22, 2),
    ]
    events = engine.normalize_events(records)
    full_decisions, _ = engine.evaluate_segment(events, [], EDGES, PARAMS)

    visible, segmented = [], []
    for segment in engine.plan_segments(events, PARAMS["segment_size"]):
        decisions, visible = engine.evaluate_segment(segment, visible, EDGES, PARAMS)
        segmented.extend(decisions)

    assert [d["event_id"] for d in segmented] == [d["event_id"] for d in full_decisions]
    assert [(d["visible"], d["credential_event_id"]) for d in segmented] == \
           [(d["visible"], d["credential_event_id"]) for d in full_decisions]


def test_retry_same_segment_is_idempotent():
    events = engine.normalize_events([
        ev("A1", "cache", 0, 1), ev("B1", "api", 1, 2),
        ev("A2", "cache", 10, 2), ev("B2", "api", 11, 2),
    ])
    first, visible = engine.evaluate_segment(events[:2], [], EDGES, PARAMS)
    # 中断后重放同一段（含已处理事件），不应重复累计
    again, visible2 = engine.evaluate_segment(events[:2], [], EDGES, PARAMS)
    assert len(visible) == len(visible2)
    assert [d["event_id"] for d in first] == [d["event_id"] for d in again]
    assert len({v["event_id"] for v in visible2}) == len(visible2)


def test_resume_after_interruption_continues_from_cursor():
    events = engine.normalize_events([
        ev("A1", "cache", 0, 1), ev("B1", "api", 1, 2),
        ev("A2", "cache", 10, 1), ev("B2", "api", 11, 2),
        ev("C1", "queue", 12, 2),
    ])
    seg1, visible = engine.evaluate_segment(events[:2], [], EDGES, PARAMS)
    seg2, visible_all = engine.evaluate_segment(events[2:4], visible, EDGES, PARAMS)
    seg3, _ = engine.evaluate_segment(events[4:], visible_all, EDGES, PARAMS)
    # 第二段 cache 重度可见 -> B2 被静默；C1 的上游 api(B2) 不可见，故 C1 可见
    b2 = next(d for d in seg2 if d["event_id"] == "B2")
    c1 = seg3[0]
    assert b2["silenced"] is True and b2["credential_event_id"] == "A2"
    assert c1["visible"] is True


def test_downstream_direction_reverses_credentials():
    params = engine.validate_params("downstream", 5.0, [1], segment_size=10)
    # 反向：下游静默上游。queue 重度可见 -> api 轻度被 queue 静默
    records = [ev("C1", "queue", 0, 1), ev("B1", "api", 1, 2)]
    _, decisions = engine.evaluate_all(records, EDGES, params)
    b1 = next(d for d in decisions if d["event_id"] == "B1")
    assert b1["silenced"] is True
    assert b1["credential_service"] == "queue"


def test_approval_rejects_whole_service_silenced():
    # api 全部为轻度且都被 cache 重度压掉
    records = [
        ev("A1", "cache", 0, 1), ev("B1", "api", 1, 2),
        ev("A2", "cache", 10, 1), ev("B2", "api", 11, 2),
    ]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    evaluation = engine.approval_evaluation(decisions, True)
    assert evaluation["approvable"] is False
    codes = {r["code"] for r in evaluation["reasons"]}
    assert engine.REJECT_ALL_SILENCED in codes
    services = {r.get("service") for r in evaluation["reasons"]}
    assert "api" in services


def test_approval_rejects_when_not_finished():
    records = [ev("A1", "cache", 0, 1), ev("B1", "api", 1, 2)]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    evaluation = engine.approval_evaluation(decisions[:1], False)
    assert evaluation["approvable"] is False
    assert evaluation["reasons"][0]["code"] == engine.REJECT_NOT_FINISHED


def test_approval_passes_when_every_service_keeps_visible():
    records = [
        ev("A1", "cache", 0, 1), ev("B1", "api", 1, 2),
        ev("B2", "api", 20, 2),  # 窗外，api 保留可见
    ]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    evaluation = engine.approval_evaluation(decisions, True)
    assert evaluation["approvable"] is True


def test_validation_errors():
    with pytest.raises(engine.SilenceValidationError):
        engine.validate_params("sideways", 5, [1])
    with pytest.raises(engine.SilenceValidationError):
        engine.validate_params("upstream", 999, [1])
    with pytest.raises(engine.SilenceValidationError):
        engine.validate_params("upstream", 5, [9])
    with pytest.raises(engine.SilenceValidationError):
        engine.normalize_events([ev("A1", "cache", -1, 1)])
    with pytest.raises(engine.SilenceValidationError):
        engine.normalize_events([ev("A1", "cache", 1, 1), ev("A1", "api", 2, 2)])


def test_earliest_visible_upstream_chosen_as_credential():
    records = [
        ev("A1", "cache", 0, 1),
        ev("A2", "cache", 3, 2),   # 可见轻度（cache 无上游）
        ev("B1", "api", 4, 2),    # 时窗内两条可见上游，取最早 A1
    ]
    _, decisions = engine.evaluate_all(records, EDGES, PARAMS)
    b1 = next(d for d in decisions if d["event_id"] == "B1")
    assert b1["credential_event_id"] == "A1"
    assert b1["lag_min"] == 4


def test_custom_severe_levels_are_always_visible():
    # 把等级 2 也设为重度：api 的等级2告警即使有上游也照常显示
    params = engine.validate_params("upstream", 5.0, [1, 2], segment_size=2)
    records = [ev("A1", "cache", 0, 1), ev("B1", "api", 1, 2)]
    _, decisions = engine.evaluate_all(records, EDGES, params)
    assert all(d["visible"] for d in decisions)
    stats = engine.service_stats(decisions, params["severe_levels"])
    assert stats[0]["severe"] + stats[1]["severe"] == 2


def test_downstream_segmented_resume_matches_full():
    params = engine.validate_params("downstream", 5.0, [1], segment_size=2)
    records = [
        ev("C1", "queue", 0, 1), ev("B1", "api", 1, 2),
        ev("A1", "cache", 2, 2), ev("C2", "queue", 10, 2),
        ev("B2", "api", 11, 2), ev("A2", "cache", 12, 2),
    ]
    events = engine.normalize_events(records)
    full, _ = engine.evaluate_segment(events, [], EDGES, params)
    visible, segmented = [], []
    for segment in engine.plan_segments(events, 2):
        decisions, visible = engine.evaluate_segment(segment, visible, EDGES, params)
        segmented.extend(decisions)
    assert [(d["event_id"], d["visible"], d["credential_event_id"]) for d in segmented] == \
           [(d["event_id"], d["visible"], d["credential_event_id"]) for d in full]
    # 反向：C1 重度可见静默 B1；B1 已不可见，故 A1 无可见下游凭据，仍可见
    by_id = {d["event_id"]: d for d in segmented}
    assert by_id["B1"]["silenced"] and by_id["B1"]["credential_event_id"] == "C1"
    assert by_id["A1"]["visible"]
