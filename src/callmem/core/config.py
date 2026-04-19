"""Configuration loading with layered merge chain.

Priority (highest wins):
  1. CLI flags (handled in cli.py)
  2. Environment variables (CALLMEM_ prefix, __ separator for nesting)
  3. Project TOML (.callmem/config.toml, or legacy .llm-mem/config.toml)
  4. Global TOML (~/.config/callmem/config.toml, or legacy ~/.config/llm-mem/)
  5. Built-in defaults (from Config model)

The legacy ``.llm-mem/`` / ``~/.config/llm-mem/`` / ``LLM_MEM_*`` names are
honored as fallbacks so existing installs keep working after the rename.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from callmem.models.config import Config

GLOBAL_CONFIG_PATHS = [
    Path.home() / ".config" / "callmem" / "config.toml",
    Path.home() / ".config" / "llm-mem" / "config.toml",  # legacy
]

PROJECT_CONFIG_DIRS = (".callmem", ".llm-mem")  # preferred, then legacy

ENV_PREFIX = "CALLMEM_"
LEGACY_ENV_PREFIX = "LLM_MEM_"
ENV_SEPARATOR = "__"


def load_config(project_path: Path | None = None) -> Config:
    """Load configuration with full merge chain.

    Order: defaults → global TOML → project TOML → env vars

    For the filesystem layers, the preferred ``callmem`` path wins if present;
    otherwise the legacy ``llm-mem`` equivalent is used.
    """
    merged: dict[str, Any] = {}

    # Layer 1: Global config — first hit wins (prefer new path)
    for global_path in GLOBAL_CONFIG_PATHS:
        if global_path.exists():
            _deep_merge(merged, _load_toml(global_path))
            break

    # Layer 2: Project config — first hit wins (prefer .callmem/)
    if project_path is not None:
        for dir_name in PROJECT_CONFIG_DIRS:
            project_config = project_path / dir_name / "config.toml"
            if project_config.exists():
                _deep_merge(merged, _load_toml(project_config))
                break

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
    """Convert CALLMEM_* (or legacy LLM_MEM_*) env vars to a nested dict.

    CALLMEM_OLLAMA__MODEL=gemma2:9b → {"ollama": {"model": "gemma2:9b"}}

    Legacy ``LLM_MEM_*`` vars are processed first so any ``CALLMEM_*`` var
    set in the same environment overrides them.
    """
    result: dict[str, Any] = {}
    for prefix in (LEGACY_ENV_PREFIX, ENV_PREFIX):
        for key, value in os.environ.items():
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
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
# callmem configuration
# See docs/config.md for all options

[project]
name = "{project_name}"

# LLM backend for memory maintenance (entity extraction, summarization, sensitive scan)
# Options: "ollama", "openai_compat", "none"
[llm]
backend = "ollama"

[ollama]
model = "qwen3:8b"
endpoint = "http://localhost:11434"
# num_ctx = 8192  # Uncomment to cap context window (reduces VRAM usage)

# [openai_compat]
# endpoint = "https://open.bigmodel.cn/api/paas/v4"
# model = "glm-4-flash"
# api_key_env = "CALLMEM_API_KEY"  # name of env var holding the key

[briefing]
max_tokens = 2000

[compaction]
enabled = true
schedule = "on_session_end"

[ui]
port = 9090
# host = "0.0.0.0"    # default — accessible from Tailscale/LAN
# host = "127.0.0.1"  # restrict to localhost only

[sensitive_data]
enabled = true
vault_mode = "auto"
"""
