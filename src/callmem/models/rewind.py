"""Rewind point model (A6)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from typing_extensions import Self
from ulid import ULID

from callmem.compat import UTC


class RewindPoint(BaseModel):
    """A snapshot of memory state at a point in time for undo/rewind."""

    id: str = Field(default_factory=lambda: str(ULID()))
    project_id: str
    label: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    event_count: int | None = None
    entity_count: int | None = None
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "label": self.label,
            "created_at": self.created_at,
            "event_count": self.event_count,
            "entity_count": self.entity_count,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)
