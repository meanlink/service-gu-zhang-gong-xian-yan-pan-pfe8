def plan_dict(p):
    return {k: getattr(p, k) for k in [
        'id', 'name', 'dataset_id', 'dataset_checksum', 'edges', 'direction',
        'window_min', 'severe_levels', 'segment_size', 'params_version',
        'approved', 'approved_at', 'approved_by', 'created_by',
        'created_at', 'updated_at']}


def replay_dict(r, include_decisions=False):
    data = {k: getattr(r, k) for k in [
        'id', 'plan_id', 'params_version', 'status', 'total_events',
        'processed_events', 'total_segments', 'processed_segments',
        'cursor_event_id', 'service_stats', 'approval', 'created_by',
        'created_at', 'updated_at']}
    if include_decisions:
        data['decisions'] = r.decisions
    return data


def brief_dict(b):
    return {k: getattr(b, k) for k in [
        'id', 'plan_id', 'replay_id', 'name', 'params_version',
        'basis_checksum', 'snapshot', 'created_by', 'created_at']}
