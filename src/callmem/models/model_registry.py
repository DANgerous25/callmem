"""Model registry entry model (A5)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from typing_extensions import Self

from callmem.compat import UTC


class ModelRegistryEntry(BaseModel):
    """A known LLM model with capabilities, pricing, and availability."""

    model_name: str
    provider: str | None = None
    display_name: str | None = None
    pricing_input: float | None = None
    pricing_output: float | None = None
    context_window: int | None = None
    max_output: int | None = None
    supports_tools: bool = False
    supports_vision: bool = False
    supports_streaming: bool = False
    strengths: list[str] | None = None
    weaknesses: list[str] | None = None
    benchmarks: dict[str, float] | None = None
    latency_p50_ms: int | None = None
    geo_available: list[str] | None = None
    geo_blocked: list[str] | None = None
    geo_notes: str | None = None
    quality_tier: str | None = None
    use_case_scores: dict[str, float] | None = None
    known_issues: list[str] | None = None
    release_date: str | None = None
    deprecation_date: str | None = None
    gateways: list[str] | None = None
    last_synced: str | None = None
    last_researched: str | None = None
    last_updated: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    metadata: dict[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "provider": self.provider,
            "display_name": self.display_name,
            "pricing_input": self.pricing_input,
            "pricing_output": self.pricing_output,
            "context_window": self.context_window,
            "max_output": self.max_output,
            "supports_tools": 1 if self.supports_tools else 0,
            "supports_vision": 1 if self.supports_vision else 0,
            "supports_streaming": 1 if self.supports_streaming else 0,
            "strengths": json.dumps(self.strengths) if self.strengths else None,
            "weaknesses": json.dumps(self.weaknesses) if self.weaknesses else None,
            "benchmarks": json.dumps(self.benchmarks) if self.benchmarks else None,
            "latency_p50_ms": self.latency_p50_ms,
            "geo_available": json.dumps(self.geo_available) if self.geo_available else None,
            "geo_blocked": json.dumps(self.geo_blocked) if self.geo_blocked else None,
            "geo_notes": self.geo_notes,
            "quality_tier": self.quality_tier,
            "use_case_scores": json.dumps(self.use_case_scores) if self.use_case_scores else None,
            "known_issues": json.dumps(self.known_issues) if self.known_issues else None,
            "release_date": self.release_date,
            "deprecation_date": self.deprecation_date,
            "gateways": json.dumps(self.gateways) if self.gateways else None,
            "last_synced": self.last_synced,
            "last_researched": self.last_researched,
            "last_updated": self.last_updated,
            "metadata": json.dumps(self.metadata) if self.metadata else None,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Self:
        data = dict(row)
        for key in ("strengths", "weaknesses", "known_issues", "gateways",
                     "geo_available", "geo_blocked"):
            val = data.get(key)
            if val and isinstance(val, str):
                data[key] = json.loads(val)
        for key in ("benchmarks", "use_case_scores", "metadata"):
            val = data.get(key)
            if val and isinstance(val, str):
                data[key] = json.loads(val)
        for key in ("supports_tools", "supports_vision", "supports_streaming"):
            if key in data:
                data[key] = bool(data[key])
        return cls(**data)
