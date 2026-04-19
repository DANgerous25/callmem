"""Backward-compatibility tests for the llm-mem → callmem rename.

These pin the behavior that keeps existing installs working unchanged:
  - Importing ``llm_mem.mcp.server`` delegates to ``callmem.mcp.server``.
  - A project with only ``.llm-mem/config.toml`` still loads config.
  - ``LLM_MEM_*`` env vars still influence config when ``CALLMEM_*`` is unset.
  - ``CALLMEM_*`` overrides ``LLM_MEM_*`` when both are set.
"""

from __future__ import annotations

import warnings

import pytest


def test_llm_mem_package_emits_deprecation_warning() -> None:
    """Importing ``llm_mem`` should warn but succeed."""
    import importlib
    import sys

    # Force a fresh import so the warning fires
    sys.modules.pop("llm_mem", None)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        importlib.import_module("llm_mem")

    deprecations = [w for w in captured if issubclass(w.category, DeprecationWarning)]
    assert deprecations, "expected a DeprecationWarning when importing llm_mem"
    assert "callmem" in str(deprecations[0].message).lower()


def test_llm_mem_mcp_server_redirects_to_callmem() -> None:
    """``llm_mem.mcp.server.main`` must be the same object as callmem's."""
    from callmem.mcp import server as new_server
    from llm_mem.mcp import server as legacy_server

    assert legacy_server.main is new_server.main


def test_llm_mem_cli_redirects_to_callmem() -> None:
    """``llm_mem.cli.main`` must be the same object as ``callmem.cli.main``."""
    from callmem.cli import main as new_main
    from llm_mem.cli import main as legacy_main

    assert legacy_main is new_main


def test_legacy_dotdir_config_loads(tmp_path) -> None:
    """A project with only ``.llm-mem/config.toml`` should still load."""
    from callmem.core.config import load_config

    (tmp_path / ".llm-mem").mkdir()
    (tmp_path / ".llm-mem" / "config.toml").write_text(
        '[project]\nname = "legacy-project"\n[ui]\nport = 9191\n'
    )

    cfg = load_config(tmp_path)
    assert cfg.project.name == "legacy-project"
    assert cfg.ui.port == 9191


def test_new_dotdir_wins_over_legacy(tmp_path) -> None:
    """If both ``.callmem/`` and ``.llm-mem/`` configs exist, new wins."""
    from callmem.core.config import load_config

    (tmp_path / ".llm-mem").mkdir()
    (tmp_path / ".llm-mem" / "config.toml").write_text(
        '[project]\nname = "old"\n[ui]\nport = 1111\n'
    )
    (tmp_path / ".callmem").mkdir()
    (tmp_path / ".callmem" / "config.toml").write_text(
        '[project]\nname = "new"\n[ui]\nport = 2222\n'
    )

    cfg = load_config(tmp_path)
    assert cfg.project.name == "new"
    assert cfg.ui.port == 2222


def test_legacy_env_prefix_is_honored(monkeypatch) -> None:
    """``LLM_MEM_*`` env vars should still influence config."""
    from callmem.core.config import load_config

    monkeypatch.delenv("CALLMEM_UI__PORT", raising=False)
    monkeypatch.setenv("LLM_MEM_UI__PORT", "8181")

    cfg = load_config(None)
    assert cfg.ui.port == 8181


def test_new_env_prefix_wins_over_legacy(monkeypatch) -> None:
    """``CALLMEM_*`` overrides ``LLM_MEM_*`` when both are set."""
    from callmem.core.config import load_config

    monkeypatch.setenv("LLM_MEM_UI__PORT", "1234")
    monkeypatch.setenv("CALLMEM_UI__PORT", "5678")

    cfg = load_config(None)
    assert cfg.ui.port == 5678


def test_legacy_vault_passphrase_fallback(monkeypatch, tmp_path) -> None:
    """``LLM_MEM_VAULT_PASSPHRASE`` should still unlock the vault."""
    from callmem.core.crypto import VaultKeyManager

    monkeypatch.delenv("CALLMEM_VAULT_PASSPHRASE", raising=False)
    monkeypatch.setenv("LLM_MEM_VAULT_PASSPHRASE", "legacy-secret")

    vault_dir = tmp_path / ".callmem"
    vault_dir.mkdir()
    km = VaultKeyManager(callmem_dir=vault_dir, mode="passphrase")
    # get_fernet() raising ValueError would mean the passphrase wasn't read.
    assert km.get_fernet() is not None


def test_vault_passphrase_missing_raises(monkeypatch, tmp_path) -> None:
    """Neither prefix set → ValueError with guidance."""
    from callmem.core.crypto import VaultKeyManager

    monkeypatch.delenv("CALLMEM_VAULT_PASSPHRASE", raising=False)
    monkeypatch.delenv("LLM_MEM_VAULT_PASSPHRASE", raising=False)

    vault_dir = tmp_path / ".callmem"
    vault_dir.mkdir()
    km = VaultKeyManager(callmem_dir=vault_dir, mode="passphrase")
    with pytest.raises(ValueError, match="CALLMEM_VAULT_PASSPHRASE"):
        km.get_fernet()


def test_legacy_api_key_env_fallback(monkeypatch) -> None:
    """``LLM_MEM_API_KEY`` should still populate the OpenAI-compat client."""
    from callmem.core.openai_compat import OpenAICompatClient

    monkeypatch.delenv("CALLMEM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MEM_API_KEY", "legacy-key-value")

    client = OpenAICompatClient()
    assert client.api_key == "legacy-key-value"


@pytest.mark.parametrize("legacy,new,expected", [
    ("legacy-only", None, "legacy-only"),
    (None, "new-only", "new-only"),
    ("legacy", "new", "new"),
])
def test_api_key_precedence(monkeypatch, legacy, new, expected) -> None:
    """``CALLMEM_API_KEY`` wins over ``LLM_MEM_API_KEY`` when both are set."""
    from callmem.core.openai_compat import OpenAICompatClient

    monkeypatch.delenv("CALLMEM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MEM_API_KEY", raising=False)
    if legacy is not None:
        monkeypatch.setenv("LLM_MEM_API_KEY", legacy)
    if new is not None:
        monkeypatch.setenv("CALLMEM_API_KEY", new)

    client = OpenAICompatClient()
    assert client.api_key == expected
