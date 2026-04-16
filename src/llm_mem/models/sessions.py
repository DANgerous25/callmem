"""Session data models."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field
from ulid import ULID

SessionStatus = Literal["active", "ended", "abandoned"]


class Session(BaseModel):
    """A continuous period of agent interaction."""

    id: str = Field(default_factory=lambda: str(ULID()))
    project_id: str
    started_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    ended_at: str | None = None
    status: SessionStatus = "active"
    agent_name: str | None = None
    model_name: str | None = None
    summary: str | None = None
    event_count: int = 0
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "agent_name": self.agent_name,
            "model_name": self.model_name,
            "summary": self.summary,
            "event_count": self.event_count,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)
