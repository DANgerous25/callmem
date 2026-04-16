"""Configuration loading with layered merge chain.

Priority (highest wins):
  1. CLI flags (handled in cli.py)
  2. Environment variables (LLM_MEM_ prefix, __ separator for nesting)
  3. Project TOML (.llm-mem/config.toml)
  4. Global TOML (~/.config/llm-mem/config.toml)
  5. Built-in defaults (from Config model)
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from llm_mem.models.config import Config

GLOBAL_CONFIG_PATHS = [
    Path.home() / ".config" / "llm-mem" / "config.toml",
]

ENV_PREFIX = "LLM_MEM_"
ENV_SEPARATOR = "__"


def load_config(project_path: Path | None = None) -> Config:
    """Load configuration with full merge chain.

    Order: defaults → global TOML → project TOML → env vars
    """
    merged: dict[str, Any] = {}

    # Layer 1: Global config
    for global_path in GLOBAL_CONFIG_PATHS:
        if global_path.exists():
            _deep_merge(merged, _load_toml(global_path))

    # Layer 2: Project config
    if project_path is not None:
        project_config = project_path / ".llm-mem" / "config.toml"
        if project_config.exists():
            _deep_merge(merged, _load_toml(project_config))

    # Layer 3: Environment variables
    env_overrides = _load_env_vars()
    if env_overrides:
        _deep_merge(merged, env_overrides)

    return Config.from_dict(merged)


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file and return its contents as a dict."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def _load_env_vars() -> dict[str, Any]:
    """Convert LLM_MEM_* env vars to nested dict structure.

    LLM_MEM_OLLAMA__MODEL=gemma2:9b → {"ollama": {"model": "gemma2:9b"}}
    """
    result: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        suffix = key[len(ENV_PREFIX):]
        parts = suffix.lower().split(ENV_SEPARATOR)
        _set_nested(result, parts, _parse_env_value(value))
    return result


def _set_nested(d: dict[str, Any], keys: list[str], value: Any) -> None:
    """Set a value in a nested dict, creating intermediate dicts as needed."""
    for key in keys[:-1]:
        if key not in d or not isinstance(d[key], dict):
            d[key] = {}
        d = d[key]
    d[keys[-1]] = value


def _parse_env_value(value: str) -> Any:
    """Try to parse an env var value as bool, int, float, or keep as string."""
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override into base, recursing into nested dicts."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def generate_default_config(project_name: str) -> str:
    """Generate a default config.toml string for a project."""
    return f"""\
# llm-mem configuration
# See docs/config.md for all options

[project]
name = "{project_name}"

[ollama]
model = "qwen3:8b"
endpoint = "http://localhost:11434"

[briefing]
max_tokens = 2000

[compaction]
enabled = true
schedule = "on_session_end"

[ui]
port = 9090

[sensitive_data]
enabled = true
vault_mode = "auto"
"""
