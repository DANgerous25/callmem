"""Model performance statistics model (A2)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from typing_extensions import Self
from ulid import ULID

from callmem.compat import UTC


class ModelStats(BaseModel):
    """Aggregated per-model performance metrics for a project."""

    id: str = Field(default_factory=lambda: str(ULID()))
    project_id: str
    model_name: str
    task_type: str | None = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    avg_eval_score: float | None = None
    total_cost_usd: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    first_seen: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    last_seen: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "model_name": self.model_name,
            "task_type": self.task_type,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "avg_eval_score": self.avg_eval_score,
            "total_cost_usd": self.total_cost_usd,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)
