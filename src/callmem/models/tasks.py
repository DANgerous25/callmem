"""Task data model — structured task tree for agent workflows (A1)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from typing_extensions import Self
from ulid import ULID

from callmem.compat import UTC

TaskStatus = Literal[
    "pending", "in_progress", "completed", "failed", "cancelled",
]


class Task(BaseModel):
    """A structured task in a task tree, optionally with a parent."""

    id: str = Field(default_factory=lambda: str(ULID()))
    project_id: str
    parent_id: str | None = None
    session_id: str | None = None
    title: str
    description: str | None = None
    status: TaskStatus = "pending"
    model_assigned: str | None = None
    model_reason: str | None = None
    eval_score: float | None = None
    eval_feedback: str | None = None
    cost_usd: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    result_ref: str | None = None
    task_type: str | None = None
    complexity_hint: int | None = None
    retry_count: int = 0
    retry_of: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    completed_at: str | None = None
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "parent_id": self.parent_id,
            "session_id": self.session_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "model_assigned": self.model_assigned,
            "model_reason": self.model_reason,
            "eval_score": self.eval_score,
            "eval_feedback": self.eval_feedback,
            "cost_usd": self.cost_usd,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "result_ref": self.result_ref,
            "task_type": self.task_type,
            "complexity_hint": self.complexity_hint,
            "retry_count": self.retry_count,
            "retry_of": self.retry_of,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)
