"""Configuration models for callmem."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class ProjectConfig(BaseModel):
    name: str | None = None


class LLMBackendConfig(BaseModel):
    """Which LLM to use for memory maintenance.

    backend options:
      - "ollama"        — local Ollama instance
      - "openai_compat" — any OpenAI-compatible API (Z.ai/GLM, OpenAI, Groq, etc.)
      - "none"          — pattern matching only, no LLM features
    """
    backend: str = "ollama"


class OllamaConfig(BaseModel):
    model: str = "qwen3:8b"
    endpoint: str = "http://localhost:11434"
    timeout: int = 120
    num_ctx: int | None = None


class OpenAICompatConfig(BaseModel):
    endpoint: str = "https://openrouter.ai/api/v1"
    model: str = "z-ai/glm-4-flash"
    api_key_env: str = "OPENROUTER_KEY"
    timeout: int = 120


class BriefingScoringConfig(BaseModel):
    """Weights for importance-ranked entity selection in the briefing.

    score = pinned_boost (if pinned)
          + type_weights[entity.type]
          + recency_weight * 0.5 ** (age_days / recency_half_life_days)
          + citation_weight * log1p(cited_count)
                * 0.5 ** (citation_age_days / citation_half_life_days)

    Two always-include floors sit outside the score, applied after
    ranking: entities from the most recent session (see
    BriefingGenerator._most_recent_session_entity_ids), and open
    todo/failure entities up to ``open_items_floor_cap`` (see
    BriefingGenerator._open_items_floor_ids) — an old, unpinned, uncited
    open TODO must not silently vanish under the score cap just because
    nothing else about it is remarkable. ``max_entities`` caps how many
    score-ranked entities are kept beyond those floors; when the cap
    bites, the lowest-scored entities are dropped whole rather than
    truncated.
    """
    pinned_boost: float = 8.0
    type_weights: dict[str, float] = Field(default_factory=lambda: {
        "decision": 3.0, "discovery": 3.0, "failure": 3.0, "fact": 3.0,
        "bugfix": 2.0, "feature": 2.0, "research": 2.0,
        "todo": 1.0,
        "change": 0.0,
    })
    recency_weight: float = 4.0
    recency_half_life_days: float = 14.0
    citation_weight: float = 4.0
    citation_half_life_days: float = 30.0
    max_entities: int = 100
    open_items_floor_cap: int = 20


class BriefingConfig(BaseModel):
    max_tokens: int = 2000
    focus: str | None = None
    auto_write_session_summary: bool = False
    session_summary_filename: str = "SESSION_SUMMARY.md"
    entity_types: list[str] = Field(default_factory=list)
    max_per_type: int = 20
    include_last_session: bool = True
    default_view: str = "key_points"
    scoring: BriefingScoringConfig = Field(default_factory=BriefingScoringConfig)


class ExtractionConfig(BaseModel):
    batch_size: int = 10


class IngestionConfig(BaseModel):
    """Filtering rules applied before events are stored or queued.

    ``skip_tools`` matches tool_call events by the tool name (the token
    before the first ``(`` in the event content). ``skip_patterns``
    applies ``fnmatch`` globs against the full event content (e.g.
    ``Read(*node_modules*)``).
    """
    skip_tools: list[str] = Field(default_factory=list)
    skip_patterns: list[str] = Field(default_factory=list)


class CompactionConfig(BaseModel):
    enabled: bool = True
    schedule: str = "on_session_end"
    max_events: int = 500


class SummarizationConfig(BaseModel):
    chunk_size: int = 20
    cross_session_interval: int = 5


class UIConfig(BaseModel):
    port: int = 9090
    host: str = "0.0.0.0"  # noqa: S104 — bind all interfaces for Tailscale/LAN access; restrict to 127.0.0.1 in config.toml if needed


class SensitiveDataConfig(BaseModel):
    enabled: bool = True
    pattern_scan: bool = True
    llm_scan: bool = True
    llm_scan_confidence: float = 0.7
    vault_mode: str = "auto"


class EndlessModeConfig(BaseModel):
    """Advisory context compression for long-running sessions.

    callmem cannot mutate the agent's context window directly; it exposes
    ``mem_check_context`` + ``mem_compress_context`` tools that the agent
    calls when it approaches the configured threshold.

    ``context_limit`` is the model's context window in tokens. When
    ``None`` the caller derives it from ``ollama.num_ctx`` at runtime.
    """
    enabled: bool = True
    context_limit: int | None = None
    compress_threshold: float = 0.8
    chunk_size: int = 30


class AdaptersConfig(BaseModel):
    """Which live session adapters to run inside the daemon."""

    opencode: bool = False  # SSE adapter — disabled by default (OpenCode has no SSE endpoint)
    opencode_db: bool = True
    opencode_db_poll_interval: float = 3.0
    opencode_db_idle_timeout: float = 300.0
    claude_code: bool = True
    claude_code_idle_timeout: float = 300.0
    claude_code_poll_interval: float = 2.0


class Config(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    llm: LLMBackendConfig = Field(default_factory=LLMBackendConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    openai_compat: OpenAICompatConfig = Field(default_factory=OpenAICompatConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    summarization: SummarizationConfig = Field(default_factory=SummarizationConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    sensitive_data: SensitiveDataConfig = Field(default_factory=SensitiveDataConfig)
    endless_mode: EndlessModeConfig = Field(default_factory=EndlessModeConfig)
    adapters: AdaptersConfig = Field(default_factory=AdaptersConfig)

    @model_validator(mode="after")
    def _validate_config(self) -> Config:
        valid_modes = {"auto", "passphrase", "disabled"}
        if self.sensitive_data.vault_mode not in valid_modes:
            msg = (
                f"Invalid vault_mode '{self.sensitive_data.vault_mode}'. "
                f"Must be one of: {', '.join(sorted(valid_modes))}"
            )
            raise ValueError(msg)

        valid_backends = {"ollama", "openai_compat", "none"}
        if self.llm.backend not in valid_backends:
            msg = (
                f"Invalid llm backend '{self.llm.backend}'. "
                f"Must be one of: {', '.join(sorted(valid_backends))}"
            )
            raise ValueError(msg)

        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        return cls(**data)
