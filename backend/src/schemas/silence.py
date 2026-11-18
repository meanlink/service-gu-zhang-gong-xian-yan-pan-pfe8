from pydantic import BaseModel, Field


class EdgeInput(BaseModel):
    upstream: str = Field(min_length=1, max_length=120)
    downstream: str = Field(min_length=1, max_length=120)


class SilencePlanInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    dataset_id: int
    edges: list[EdgeInput] = Field(default_factory=list)
    direction: str = Field(default="upstream")
    window_min: float = Field(default=5.0)
    severe_levels: list[int] = Field(default_factory=lambda: [1])
    segment_size: int = Field(default=50)


class SilencePlanUpdate(BaseModel):
    edges: list[EdgeInput] | None = None
    direction: str | None = None
    window_min: float | None = None
    severe_levels: list[int] | None = None
    segment_size: int | None = None


class SilenceBriefInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    destination: str = Field(default="值班组 · 静默简报", min_length=2, max_length=120)
