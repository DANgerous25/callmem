"""Event data models."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from typing_extensions import Self
from ulid import ULID

from callmem.compat import UTC

EventType = Literal[
    "prompt", "response", "tool_call", "file_change",
    "decision", "todo", "failure", "discovery", "fact", "note",
]


class EventInput(BaseModel):
    """Input for creating a new event (no ID or timestamp required)."""

    type: EventType
    content: str
    metadata: dict[str, Any] | None = None
    timestamp: str | None = None


class Event(BaseModel):
    """A stored event in memory."""

    id: str = Field(default_factory=lambda: str(ULID()))
    session_id: str
    project_id: str
    type: EventType
    content: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    token_count: int | None = None
    metadata: dict[str, Any] | None = None
    archived_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        """Convert to a dict suitable for SQLite insertion."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "type": self.type,
            "content": self.content,
            "timestamp": self.timestamp,
            "token_count": self.token_count,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
            "archived_at": self.archived_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        """Construct from a SQLite row dict."""
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)
