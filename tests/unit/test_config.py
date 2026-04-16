"""Tests for configuration loading and merging."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from llm_mem.core.config import (
    _deep_merge,
    _parse_env_value,
    _set_nested,
    generate_default_config,
    load_config,
)
from llm_mem.models.config import Config


class TestConfigDefaults:
    def test_all_defaults_applied(self) -> None:
        config = Config()
        assert config.ollama.model == "qwen3:8b"
        assert config.ollama.endpoint == "http://localhost:11434"
        assert config.ollama.timeout == 120
        assert config.briefing.max_tokens == 2000
        assert config.ui.port == 9090
        assert config.ui.host == "0.0.0.0"
        assert config.compaction.enabled is True
        assert config.compaction.schedule == "on_session_end"
        assert config.sensitive_data.enabled is True
        assert config.sensitive_data.vault_mode == "auto"

    def test_from_dict_partial(self) -> None:
        config = Config.from_dict({"ollama": {"model": "llama3.1:8b"}})
        assert config.ollama.model == "llama3.1:8b"
        assert config.ollama.endpoint == "http://localhost:11434"

    def test_from_dict_full(self) -> None:
        config = Config.from_dict({
            "project": {"name": "test"},
            "ollama": {"model": "m1", "endpoint": "http://host:1234", "timeout": 60},
            "briefing": {"max_tokens": 500, "focus": "bugs"},
            "compaction": {"enabled": False, "schedule": "manual", "max_events": 100},
            "ui": {"port": 8080, "host": "0.0.0.0"},
            "sensitive_data": {"enabled": False, "vault_mode": "disabled"},
        })
        assert config.project.name == "test"
        assert config.ollama.timeout == 60
        assert config.briefing.focus == "bugs"
        assert config.compaction.max_events == 100
        assert config.ui.host == "0.0.0.0"
        assert config.sensitive_data.vault_mode == "disabled"


class TestConfigValidation:
    def test_invalid_vault_mode_rejected(self) -> None:
        with pytest.raises(Exception, match="Invalid vault_mode"):
            Config(sensitive_data={"vault_mode": "bad"})

    def test_invalid_port_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            Config(ui={"port": "not_a_number"})

    def test_valid_vault_modes(self) -> None:
        for mode in ["auto", "passphrase", "disabled"]:
            config = Config(sensitive_data={"vault_mode": mode})
            assert config.sensitive_data.vault_mode == mode


class TestConfigTomlOverride:
    def test_project_config_overrides_defaults(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".llm-mem"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('[ollama]\nmodel = "llama3.1:8b"\n')
        config = load_config(tmp_path)
        assert config.ollama.model == "llama3.1:8b"

    def test_project_config_partial_override(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".llm-mem"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('[briefing]\nmax_tokens = 500\n')
        config = load_config(tmp_path)
        assert config.briefing.max_tokens == 500
        assert config.ollama.model == "qwen3:8b"

    def test_missing_project_config_uses_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path)
        assert config.ollama.model == "qwen3:8b"

    def test_no_project_path_uses_defaults(self) -> None:
        config = load_config()
        assert config.ollama.model == "qwen3:8b"


class TestConfigEnvOverride:
    def test_env_overrides_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MEM_OLLAMA__MODEL", "gemma2:9b")
        config = load_config()
        assert config.ollama.model == "gemma2:9b"

    def test_env_overrides_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_dir = tmp_path / ".llm-mem"
        config_dir.mkdir()
        config_file = config_dir / "config.toml"
        config_file.write_text('[ollama]\nmodel = "llama3.1:8b"\n')

        monkeypatch.setenv("LLM_MEM_OLLAMA__MODEL", "gemma2:9b")
        config = load_config(tmp_path)
        assert config.ollama.model == "gemma2:9b"

    def test_env_bool_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MEM_COMPACTION__ENABLED", "false")
        config = load_config()
        assert config.compaction.enabled is False

    def test_env_int_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MEM_UI__PORT", "8080")
        config = load_config()
        assert config.ui.port == 8080

    def test_env_multiple_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_MEM_OLLAMA__MODEL", "m1")
        monkeypatch.setenv("LLM_MEM_BRIEFING__MAX_TOKENS", "500")
        config = load_config()
        assert config.ollama.model == "m1"
        assert config.briefing.max_tokens == 500

    def test_env_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_MEM_OLLAMA__MODEL", raising=False)
        config = load_config()
        assert config.ollama.model == "qwen3:8b"


class TestParseEnvValue:
    def test_true_values(self) -> None:
        for v in ["true", "True", "TRUE", "yes", "1"]:
            assert _parse_env_value(v) is True

    def test_false_values(self) -> None:
        for v in ["false", "False", "FALSE", "no", "0"]:
            assert _parse_env_value(v) is False

    def test_int_values(self) -> None:
        assert _parse_env_value("42") == 42
        assert _parse_env_value("0") is False  # 0 is parsed as bool first

    def test_float_values(self) -> None:
        assert _parse_env_value("3.14") == 3.14

    def test_string_values(self) -> None:
        assert _parse_env_value("hello") == "hello"
        assert _parse_env_value("qwen3:8b") == "qwen3:8b"


class TestDeepMerge:
    def test_simple_merge(self) -> None:
        base: dict = {"a": 1}
        _deep_merge(base, {"b": 2})
        assert base == {"a": 1, "b": 2}

    def test_nested_merge(self) -> None:
        base: dict = {"ollama": {"model": "a", "timeout": 30}}
        _deep_merge(base, {"ollama": {"model": "b"}})
        assert base == {"ollama": {"model": "b", "timeout": 30}}

    def test_override_non_dict_with_dict(self) -> None:
        base: dict = {"key": "value"}
        _deep_merge(base, {"key": {"nested": True}})
        assert base == {"key": {"nested": True}}


class TestSetNested:
    def test_single_key(self) -> None:
        d: dict = {}
        _set_nested(d, ["a"], 1)
        assert d == {"a": 1}

    def test_nested_keys(self) -> None:
        d: dict = {}
        _set_nested(d, ["a", "b", "c"], 1)
        assert d == {"a": {"b": {"c": 1}}}

    def test_overwrite_existing(self) -> None:
        d: dict = {"a": 1}
        _set_nested(d, ["a"], 2)
        assert d == {"a": 2}


class TestGenerateDefaultConfig:
    def test_contains_all_sections(self) -> None:
        text = generate_default_config("my-project")
        assert 'name = "my-project"' in text
        assert "qwen3:8b" in text
        assert "2000" in text
        assert "9090" in text
        assert "on_session_end" in text

    def test_is_valid_toml(self, tmp_path: Path) -> None:
        import tomllib

        text = generate_default_config("test")
        (tmp_path / "config.toml").write_text(text)
        with open(tmp_path / "config.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["name"] == "test"
