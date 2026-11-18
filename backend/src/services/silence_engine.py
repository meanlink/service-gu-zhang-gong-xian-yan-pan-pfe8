"""选定批次回放静默的纯函数引擎（仅标准库）。

口径（与 BUSINESS 需求逐条对应）：
- 重度告警（severity 命中 severe_levels）永远可见，不参与被静默；
- 轻度告警只认时窗内“实际可见”的上游告警作为静默凭据；
- 凭据必须严格早于当前事件（同刻不互相压制），且时间差 <= window_min；
- 上游可见既包括始终可见的重度，也包括本身未被静默的轻度（可见性沿时间序传递）；
- 分段接续：每段都基于此前已判定的可见告警计算，结果与一次性全量回放完全一致；
- 同段重试确定性重算，按 event_id 去重，不重复累计；
- 任一服务的告警全被压掉时，给出拒绝批准原因。

引擎不落库、不读环境，便于离线单元测试。
"""
from __future__ import annotations

DIRECTION_UPSTREAM = "upstream"      # 上游告警静默下游告警（默认）
DIRECTION_DOWNSTREAM = "downstream"  # 下游告警静默上游告警（反向静默）
DIRECTIONS = (DIRECTION_UPSTREAM, DIRECTION_DOWNSTREAM)

DEFAULT_WINDOW_MIN = 5.0
DEFAULT_SEGMENT = 50
DEFAULT_SEVERE_LEVELS = (1,)
MIN_WINDOW, MAX_WINDOW = 1.0, 60.0
MIN_SEGMENT, MAX_SEGMENT = 1, 500
VALID_SEVERITIES = (1, 2, 3)

# 拒绝原因代码
REJECT_ALL_SILENCED = "service_all_silenced"
REJECT_NOT_FINISHED = "replay_not_finished"


class SilenceValidationError(ValueError):
    """输入或参数不合法（对应 HTTP 422）。"""


def validate_params(direction, window_min, severe_levels, segment_size=DEFAULT_SEGMENT):
    """校验静默方案参数，返回规范化后的参数字典。"""
    if direction not in DIRECTIONS:
        raise SilenceValidationError("静默方向须为 upstream（上游静默下游）或 downstream（下游静默上游）。")
    try:
        window = float(window_min)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SilenceValidationError("静默时窗须为有效数值（分钟）。") from exc
    if not (MIN_WINDOW <= window <= MAX_WINDOW):
        raise SilenceValidationError(f"静默时窗须在 {MIN_WINDOW:g}~{MAX_WINDOW:g} 分钟之间。")
    if not isinstance(severe_levels, (list, tuple, set)) or not severe_levels:
        raise SilenceValidationError("重度等级集合不能为空。")
    levels = sorted({int(s) for s in severe_levels})
    if any(s not in VALID_SEVERITIES for s in levels):
        raise SilenceValidationError("告警等级只能取 1、2、3。")
    try:
        segment = int(segment_size)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SilenceValidationError("分段大小须为整数。") from exc
    if not (MIN_SEGMENT <= segment <= MAX_SEGMENT):
        raise SilenceValidationError(f"分段大小须在 {MIN_SEGMENT}~{MAX_SEGMENT} 条之间。")
    return {
        "direction": direction,
        "window_min": window,
        "severe_levels": levels,
        "segment_size": segment,
    }


def normalize_events(records):
    """校验并排序批次事件，返回规范事件列表。"""
    if not isinstance(records, list) or not records:
        raise SilenceValidationError("选定批次没有可回放的告警事件。")
    events = []
    seen = set()
    for index, row in enumerate(records):
        try:
            event_id = str(row["event_id"]).strip()
            service = str(row["service"]).strip()
            minute = float(row["occurred_min"])
            severity = int(row["severity"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise SilenceValidationError(f"第 {index + 1} 条告警字段不完整或类型非法。") from exc
        if not event_id or not service:
            raise SilenceValidationError(f"第 {index + 1} 条告警的事件 ID 与服务不能为空。")
        if event_id in seen:
            raise SilenceValidationError(f"事件 ID 重复：{event_id}，请先去重再回放。")
        if minute < 0 or minute != minute or minute in (float("inf"), float("-inf")):
            raise SilenceValidationError(f"事件 {event_id} 的时间须为非负有限值。")
        if severity not in VALID_SEVERITIES:
            raise SilenceValidationError(f"事件 {event_id} 的等级须为 1~3。")
        seen.add(event_id)
        events.append({
            "event_id": event_id,
            "service": service,
            "occurred_min": minute,
            "severity": severity,
        })
    events.sort(key=lambda e: (e["occurred_min"], e["event_id"]))
    return events


def _upstream_service(direction, source_service, edges):
    """按静默方向返回 source_service 的“凭据服务”集合（即压制方向上的上游）。"""
    if direction == DIRECTION_UPSTREAM:
        # edge (a,b) 表示 a→b（a 是 b 的上游），source 的凭据服务是其上游 a
        return {a for (a, b) in edges if b == source_service}
    # 反向静默：下游压制上游，source 的凭据服务是其下游 b
    return {b for (a, b) in edges if a == source_service}


def evaluate_segment(events, prior_visible, edges, params):
    """判定一个分段内事件的可见/静默，确定性纯函数。

    events: 本段已排序事件（全局时间序中的连续切片）。
    prior_visible: 此前分段已判定为可见的事件列表（含重度与未静默轻度）。
    返回 (decisions, visible_events)：
      decisions: 与 events 等长、同序的判定字典；
      visible_events: 传入的 prior_visible 去重后并入本段可见事件，供下一段接续。
    """
    direction = params["direction"]
    window = float(params["window_min"])
    severe = set(params["severe_levels"])

    # 以 event_id 去重，保证同段重试/重放不重复累计。
    known = {}
    for ev in prior_visible:
        known[ev["event_id"]] = ev
    visible = list(known.values())
    visible.sort(key=lambda e: (e["occurred_min"], e["event_id"]))

    decisions = []
    for ev in events:
        # 重试同段时，已在历史可见集中的事件直接复用，不二次累计。
        if ev["event_id"] in known:
            prior = known[ev["event_id"]]
            decisions.append({
                "event_id": ev["event_id"],
                "service": ev["service"],
                "occurred_min": ev["occurred_min"],
                "severity": ev["severity"],
                "visible": bool(prior.get("visible", True)),
                "silenced": not bool(prior.get("visible", True)),
                "credential_event_id": prior.get("credential_event_id"),
                "credential_service": prior.get("credential_service"),
                "lag_min": prior.get("lag_min"),
                "reason": prior.get("reason", "replay_resumed"),
            })
            continue

        is_severe = ev["severity"] in severe
        decision = {
            "event_id": ev["event_id"],
            "service": ev["service"],
            "occurred_min": ev["occurred_min"],
            "severity": ev["severity"],
            "visible": True,
            "silenced": False,
            "credential_event_id": None,
            "credential_service": None,
            "lag_min": None,
            "reason": "severe_always_visible" if is_severe else "no_visible_upstream_in_window",
        }

        if not is_severe:
            credential_services = _upstream_service(direction, ev["service"], edges)
            # 只认可见上游；严格更早（同刻不互相压制）且落在时窗内。
            candidates = [
                v for v in visible
                if v["service"] in credential_services
                and v["occurred_min"] < ev["occurred_min"]
                and ev["occurred_min"] - v["occurred_min"] <= window
            ]
            if candidates:
                # 取时窗内最早的一条可见上游作为静默凭据（确定口径）。
                cred = min(candidates, key=lambda v: (v["occurred_min"], v["event_id"]))
                decision.update({
                    "visible": False,
                    "silenced": True,
                    "credential_event_id": cred["event_id"],
                    "credential_service": cred["service"],
                    "lag_min": round(ev["occurred_min"] - cred["occurred_min"], 4),
                    "reason": "silenced_by_visible_upstream",
                })

        decisions.append(decision)
        if decision["visible"]:
            visible.append(ev)
            known[ev["event_id"]] = ev
    return decisions, visible


def plan_segments(events, segment_size):
    """把全局排序事件切成连续分段（接续上一段的可见告警）。"""
    return [events[i:i + segment_size] for i in range(0, len(events), segment_size)]


def evaluate_all(records, edges, params):
    """一次性全量回放，作为分段结果的对照基准与服务统计来源。"""
    events = normalize_events(records)
    decisions, _ = evaluate_segment(events, [], edges, params)
    return events, decisions


def service_stats(decisions, severe_levels=DEFAULT_SEVERE_LEVELS):
    """按服务汇总总告警、可见、静默数量。"""
    severe_set = set(severe_levels)
    stats = {}
    for d in decisions:
        bucket = stats.setdefault(d["service"], {
            "service": d["service"], "total": 0, "visible": 0,
            "silenced": 0, "severe": 0,
        })
        bucket["total"] += 1
        if d["severity"] in severe_set:
            bucket["severe"] += 1
        if d["visible"]:
            bucket["visible"] += 1
        else:
            bucket["silenced"] += 1
    return [stats[name] for name in sorted(stats)]


def approval_evaluation(decisions, finished):
    """批准闸门：回放未完成或任一服务告警全被压掉则拒绝。"""
    reasons = []
    if not finished:
        reasons.append({"code": REJECT_NOT_FINISHED,
                        "message": "回放尚未覆盖全部告警，不能批准。"})
    fully_silenced = [s["service"] for s in service_stats(decisions) if s["visible"] == 0]
    for service in fully_silenced:
        reasons.append({
            "code": REJECT_ALL_SILENCED,
            "service": service,
            "message": f"服务 {service} 的告警在当前方案下全部被静默，拒绝批准（避免整条告警链失明）。",
        })
    return {"approvable": finished and not reasons, "reasons": reasons}


def summarize(decisions, params, finished):
    total = len(decisions)
    silenced = sum(1 for d in decisions if d["silenced"])
    return {
        "total_alerts": total,
        "visible_alerts": total - silenced,
        "silenced_alerts": silenced,
        "silenced_pct": round(silenced / total * 100, 2) if total else 0.0,
        "finished": finished,
        "direction": params["direction"],
        "window_min": params["window_min"],
        "severe_levels": list(params["severe_levels"]),
    }
