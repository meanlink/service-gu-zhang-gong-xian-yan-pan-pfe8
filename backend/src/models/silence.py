from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class SilencePlan(Base):
    """选定批次的回放静默方案。

    方案保存不可变批次快照、调用链边与静默参数；改参数即升版本并撤销批准，
    已分发的简报因自带快照不受影响。
    """
    __tablename__ = 'silence_plans'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    dataset_checksum: Mapped[str] = mapped_column(String(64))
    records_snapshot: Mapped[list] = mapped_column(JSON)
    edges: Mapped[list] = mapped_column(JSON, default=list)
    direction: Mapped[str] = mapped_column(String(16), default='upstream')
    window_min: Mapped[float] = mapped_column(Double, default=5.0)
    severe_levels: Mapped[list] = mapped_column(JSON, default=list)
    segment_size: Mapped[int] = mapped_column(Integer, default=50)
    params_version: Mapped[int] = mapped_column(Integer, default=1)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class SilenceReplay(Base):
    """方案的一次分段回放。

    决策与可见事件整体随每次确定性重算覆盖写回，同段重试按 event_id 去重，
    不重复累计；中断恢复后从已算时刻继续，逐段结果与全量回放一致。
    """
    __tablename__ = 'silence_replays'
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey('silence_plans.id'))
    params_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default='in_progress')  # in_progress/completed
    total_events: Mapped[int] = mapped_column(Integer, default=0)
    processed_events: Mapped[int] = mapped_column(Integer, default=0)
    total_segments: Mapped[int] = mapped_column(Integer, default=0)
    processed_segments: Mapped[int] = mapped_column(Integer, default=0)
    cursor_event_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decisions: Mapped[list] = mapped_column(JSON, default=list)
    visible_events: Mapped[list] = mapped_column(JSON, default=list)
    service_stats: Mapped[list] = mapped_column(JSON, default=list)
    approval: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class SilenceBrief(Base):
    """静默简报。创建即冻结当前获准回放的快照，保留原依据。"""
    __tablename__ = 'silence_briefs'
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey('silence_plans.id'))
    replay_id: Mapped[int] = mapped_column(ForeignKey('silence_replays.id'))
    name: Mapped[str] = mapped_column(String(160))
    params_version: Mapped[int] = mapped_column(Integer)
    basis_checksum: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
