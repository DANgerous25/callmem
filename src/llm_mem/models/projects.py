"""Project data models."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, Field
from ulid import ULID

from llm_mem.compat import UTC


class Project(BaseModel):
    """A top-level project grouping for memories."""

    id: str = Field(default_factory=lambda: str(ULID()))
    name: str
    root_path: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "root_path": self.root_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)
