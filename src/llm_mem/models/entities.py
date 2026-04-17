"""Entity data models — structured knowledge extracted from events."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field
from ulid import ULID

EntityType = Literal[
    "decision", "todo", "fact", "failure", "discovery",
    "feature", "bugfix", "research", "change",
]
EntityStatus = Literal["open", "done", "cancelled", "unresolved", "resolved"]
Priority = Literal["high", "medium", "low"]


class Entity(BaseModel):
    """A structured piece of knowledge: decision, TODO, fact, failure, or discovery."""

    id: str = Field(default_factory=lambda: str(ULID()))
    project_id: str
    source_event_id: str | None = None
    type: EntityType
    title: str
    content: str
    status: EntityStatus | None = None
    priority: Priority | None = None
    pinned: bool = False
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    resolved_at: str | None = None
    metadata: dict[str, Any] | None = None
    key_points: str | None = None
    synopsis: str | None = None
    archived_at: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "source_event_id": self.source_event_id,
            "type": self.type,
            "title": self.title,
            "content": self.content,
            "key_points": self.key_points,
            "synopsis": self.synopsis,
            "status": self.status,
            "priority": self.priority,
            "pinned": 1 if self.pinned else 0,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
            "archived_at": self.archived_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        if "pinned" in data:
            data["pinned"] = bool(data["pinned"])
        return cls(**data)
