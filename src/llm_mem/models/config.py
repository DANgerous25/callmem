"""Configuration models for llm-mem."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class ProjectConfig(BaseModel):
    name: str | None = None


class OllamaConfig(BaseModel):
    model: str = "qwen3:8b"
    endpoint: str = "http://localhost:11434"
    timeout: int = 120


class BriefingConfig(BaseModel):
    max_tokens: int = 2000
    focus: str | None = None


class CompactionConfig(BaseModel):
    enabled: bool = True
    schedule: str = "on_session_end"
    max_events: int = 500


class UIConfig(BaseModel):
    port: int = 9090
    host: str = "127.0.0.1"


class SensitiveDataConfig(BaseModel):
    enabled: bool = True
    pattern_scan: bool = True
    llm_scan: bool = True
    llm_scan_confidence: float = 0.7
    vault_mode: str = "auto"


class Config(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    sensitive_data: SensitiveDataConfig = Field(default_factory=SensitiveDataConfig)

    @model_validator(mode="after")
    def _validate_vault_mode(self) -> Config:
        valid_modes = {"auto", "passphrase", "disabled"}
        if self.sensitive_data.vault_mode not in valid_modes:
            msg = (
                f"Invalid vault_mode '{self.sensitive_data.vault_mode}'. "
                f"Must be one of: {', '.join(sorted(valid_modes))}"
            )
            raise ValueError(msg)
        return self

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        return cls(**data)
