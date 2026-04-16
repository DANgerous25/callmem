"""Summary data models."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field
from ulid import ULID

SummaryLevel = Literal["chunk", "session", "cross_session"]


class Summary(BaseModel):
    """A compressed summary of events at a given granularity."""

    id: str = Field(default_factory=lambda: str(ULID()))
    project_id: str
    session_id: str | None = None
    level: SummaryLevel
    content: str
    event_range_start: str | None = None
    event_range_end: str | None = None
    event_count: int | None = None
    token_count: int | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "level": self.level,
            "content": self.content,
            "event_range_start": self.event_range_start,
            "event_range_end": self.event_range_end,
            "event_count": self.event_count,
            "token_count": self.token_count,
            "created_at": self.created_at,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)
