"""Configuration models for llm-mem."""

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


class OpenAICompatConfig(BaseModel):
    endpoint: str = "https://open.bigmodel.cn/api/paas/v4"
    model: str = "glm-4-flash"
    api_key_env: str = "LLM_MEM_API_KEY"
    timeout: int = 120


class BriefingConfig(BaseModel):
    max_tokens: int = 2000
    focus: str | None = None
    auto_write_session_summary: bool = True
    session_summary_filename: str = "SESSION_SUMMARY.md"
    entity_types: list[str] = Field(default_factory=list)
    max_per_type: int = 20
    include_last_session: bool = True
    default_view: str = "key_points"


class ExtractionConfig(BaseModel):
    batch_size: int = 10


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


class Config(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    llm: LLMBackendConfig = Field(default_factory=LLMBackendConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    openai_compat: OpenAICompatConfig = Field(default_factory=OpenAICompatConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    summarization: SummarizationConfig = Field(default_factory=SummarizationConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    sensitive_data: SensitiveDataConfig = Field(default_factory=SensitiveDataConfig)

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
