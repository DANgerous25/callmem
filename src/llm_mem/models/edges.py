"""Memory edge data models — relationships between memory items."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, Field
from ulid import ULID

EdgeRelation = Literal["caused_by", "relates_to", "supersedes", "resolves", "blocks"]


class MemoryEdge(BaseModel):
    """A directed relationship between two memory items."""

    id: str = Field(default_factory=lambda: str(ULID()))
    source_id: str
    source_type: str  # entity, event, summary
    target_id: str
    target_type: str  # entity, event, summary
    relation: EdgeRelation
    weight: float = 1.0
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "source_type": self.source_type,
            "target_id": self.target_id,
            "target_type": self.target_type,
            "relation": self.relation,
            "weight": self.weight,
            "created_at": self.created_at,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        if data.get("metadata") and isinstance(data["metadata"], str):
            data["metadata"] = json.loads(data["metadata"])
        return cls(**data)
