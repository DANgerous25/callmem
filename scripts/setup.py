#!/usr/bin/env python3
"""Interactive setup for llm-mem in a project.

Run from any project root:
    python -m scripts.setup
    # or
    uv run python scripts/setup.py
    # or after install
    llm-mem setup

Handles first-time and repeat setup:
- Detects existing .llm-mem/ directory
- Creates/updates config.toml
- Checks Ollama availability and model
- Validates port availability
- Configures OpenCode MCP integration
- Initializes the database
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def ask(prompt: str, default: str = "") -> str:
    """Prompt the user with an optional default."""
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer if answer else default


def ask_bool(prompt: str, default: bool = True) -> bool:
    """Prompt for yes/no."""
    hint = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "1", "true")


def port_available(port: int, host: str = "0.0.0.0") -> bool:
    """Check if a port is available."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
            return True
    except OSError:
        return False


def check_ollama(endpoint: str) -> tuple[bool, list[str]]:
    """Check if Ollama is reachable and list available models."""
    try:
        import httpx
        resp = httpx.get(f"{endpoint}/api/tags", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return True, models
    except Exception:
        pass
    return False, []


def find_opencode_config(project: Path) -> Path | None:
    """Find the OpenCode config file."""
    candidates = [
        project / "opencode.json",
        project / ".opencode.json",
        project / ".opencode" / "config.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def main() -> None:
    print()
    print("=" * 50)
    print("  llm-mem setup")
    print("=" * 50)
    print()

    # ── Project path ──────────────────────────────────────────────
    cwd = Path.cwd()
    project = Path(ask("Project root", str(cwd)))
    project = project.expanduser().resolve()

    if not project.is_dir():
        print(f"  Error: {project} is not a directory")
        sys.exit(1)

    llm_mem_dir = project / ".llm-mem"
    is_existing = llm_mem_dir.exists()

    if is_existing:
        print(f"  Found existing .llm-mem/ in {project}")
        print("  Running in update mode — existing data preserved.")
    else:
        print(f"  New setup in {project}")

    print()

    # ── Project name ──────────────────────────────────────────────
    default_name = project.name
    existing_config = {}
    config_path = llm_mem_dir / "config.toml"

    if config_path.exists():
        try:
            import tomllib
            with open(config_path, "rb") as f:
                existing_config = tomllib.load(f)
            default_name = existing_config.get("project", {}).get("name", default_name)
        except Exception:
            pass

    project_name = ask("Project name", default_name)

    # ── Ollama ────────────────────────────────────────────────────
    print()
    print("── Ollama (local LLM for memory maintenance) ──")
    print()

    default_endpoint = existing_config.get("ollama", {}).get("endpoint", "http://localhost:11434")
    ollama_endpoint = ask("Ollama endpoint", default_endpoint)

    ollama_ok, available_models = check_ollama(ollama_endpoint)

    use_ollama = True
    ollama_model = existing_config.get("ollama", {}).get("model", "qwen3:8b")

    if ollama_ok:
        print(f"  Ollama is reachable at {ollama_endpoint}")
        if available_models:
            print(f"  Available models: {', '.join(available_models[:10])}")
            if ollama_model not in [m.split(":")[0] for m in available_models] and ollama_model not in available_models:
                print(f"  Note: configured model '{ollama_model}' not found in available models")
        ollama_model = ask("Ollama model", ollama_model)
    else:
        print(f"  Ollama not reachable at {ollama_endpoint}")
        print("  Without Ollama:")
        print("    - Pattern-based secret detection still works")
        print("    - LLM-based sensitive data scanning is disabled")
        print("    - Entity extraction, summarization, compaction won't run")
        print("    - Memory still captures and stores events via MCP")
        print()
        use_ollama = ask_bool("Configure Ollama anyway (for later)?", default=True)
        if use_ollama:
            ollama_model = ask("Ollama model (to pull later)", ollama_model)

    # ── UI ────────────────────────────────────────────────────────
    print()
    print("── Web UI ──")
    print()

    default_host = existing_config.get("ui", {}).get("host", "0.0.0.0")
    default_port = existing_config.get("ui", {}).get("port", 9090)

    ui_host = ask("Bind address (0.0.0.0 for network, 127.0.0.1 for local only)", default_host)
    ui_port = int(ask("Port", str(default_port)))

    if not port_available(ui_port, ui_host):
        print(f"  Warning: port {ui_port} is already in use on {ui_host}")
        while True:
            ui_port = int(ask("Choose a different port", str(ui_port + 1)))
            if port_available(ui_port, ui_host):
                print(f"  Port {ui_port} is available.")
                break
            print(f"  Port {ui_port} is also in use.")

    # ── Sensitive data ────────────────────────────────────────────
    print()
    print("── Sensitive data protection ──")
    print()

    default_sensitive = existing_config.get("sensitive_data", {}).get("enabled", True)
    sensitive_enabled = ask_bool("Enable sensitive data detection?", default=default_sensitive)

    vault_mode = "auto"
    llm_scan = use_ollama
    if sensitive_enabled:
        default_vault = existing_config.get("sensitive_data", {}).get("vault_mode", "auto")
        print("  Vault modes:")
        print("    auto       — random key stored in .llm-mem/vault.key (simplest)")
        print("    passphrase — key derived from LLM_MEM_VAULT_PASSPHRASE env var")
        print("    disabled   — detect but don't encrypt (not recommended)")
        vault_mode = ask("Vault mode", default_vault)
        if use_ollama:
            llm_scan = ask_bool("Enable LLM-based scanning (requires Ollama)?", default=True)

    # ── Write config ──────────────────────────────────────────────
    print()
    print("── Writing configuration ──")
    print()

    llm_mem_dir.mkdir(exist_ok=True)

    config_content = f"""# llm-mem configuration
# See docs/config.md for all options

[project]
name = "{project_name}"

[ollama]
model = "{ollama_model}"
endpoint = "{ollama_endpoint}"
"""
    if not use_ollama:
        config_content += """# Ollama is not currently available. Start it and re-run setup,
# or the background workers will skip LLM-dependent tasks.
"""

    config_content += f"""
[ui]
port = {ui_port}
host = "{ui_host}"

[briefing]
max_tokens = 2000

[compaction]
enabled = true
schedule = "on_session_end"

[sensitive_data]
enabled = {"true" if sensitive_enabled else "false"}
pattern_scan = true
llm_scan = {"true" if llm_scan else "false"}
vault_mode = "{vault_mode}"
"""

    if config_path.exists():
        backup = config_path.with_suffix(".toml.bak")
        shutil.copy2(config_path, backup)
        print(f"  Backed up existing config to {backup.name}")

    config_path.write_text(config_content)
    print(f"  Wrote {config_path}")

    # ── Initialize database ───────────────────────────────────────
    db_path = llm_mem_dir / "memory.db"
    if db_path.exists():
        print(f"  Database already exists: {db_path}")
    else:
        try:
            from llm_mem.core.database import Database
            db = Database(db_path)
            db.initialize()
            print(f"  Created database: {db_path} (schema v{db.get_schema_version()})")
        except ImportError:
            print("  Skipping DB init — llm-mem not installed in this environment.")
            print(f"  Run: cd {project} && llm-mem init --project .")

    # ── .gitignore ────────────────────────────────────────────────
    gitignore_path = project / ".gitignore"
    entries_needed = ["vault.key", "vault.salt"]
    if gitignore_path.exists():
        existing_gitignore = gitignore_path.read_text()
        missing = [e for e in entries_needed if e not in existing_gitignore]
        if missing:
            with open(gitignore_path, "a") as f:
                f.write("\n# llm-mem vault secrets\n")
                for entry in missing:
                    f.write(f"{entry}\n")
            print(f"  Added {', '.join(missing)} to .gitignore")
        else:
            print("  .gitignore already has vault exclusions")
    else:
        print("  No .gitignore found — remember to exclude vault.key and vault.salt")

    # ── OpenCode MCP config ───────────────────────────────────────
    print()
    print("── OpenCode MCP integration ──")
    print()

    configure_mcp = ask_bool("Configure OpenCode to use llm-mem MCP server?", default=True)

    if configure_mcp:
        oc_config_path = find_opencode_config(project)
        if oc_config_path is None:
            oc_config_path = project / "opencode.json"
            print(f"  No existing OpenCode config found. Will create {oc_config_path.name}")

        oc_config: dict = {}
        if oc_config_path.exists():
            try:
                oc_config = json.loads(oc_config_path.read_text())
            except json.JSONDecodeError:
                print(f"  Warning: could not parse {oc_config_path.name}, creating fresh")

        if "mcp" not in oc_config:
            oc_config["mcp"] = {}

        oc_config["mcp"]["llm-mem"] = {
            "type": "local",
            "command": ["uv", "run", "python", "-m", "llm_mem.mcp.server", "--project", "."],
            "enabled": True,
        }

        oc_config_path.write_text(json.dumps(oc_config, indent=2) + "\n")
        print(f"  Wrote MCP config to {oc_config_path.name}")
        print("  OpenCode will auto-start the llm-mem MCP server on launch.")

    # ── Summary ───────────────────────────────────────────────────
    print()
    print("=" * 50)
    print("  Setup complete!")
    print("=" * 50)
    print()
    print(f"  Project:    {project_name}")
    print(f"  Database:   {db_path}")
    print(f"  Config:     {config_path}")
    print(f"  Ollama:     {'available' if ollama_ok else 'not reachable'} ({ollama_model})")
    print(f"  Web UI:     http://{ui_host}:{ui_port}")
    print(f"  Vault:      {vault_mode}")
    print()
    print("  Next steps:")
    print()
    if not ollama_ok and use_ollama:
        print(f"    1. Start Ollama and pull the model:")
        print(f"       ollama pull {ollama_model}")
        print()
    print("    Start the web UI:")
    print(f"       cd {project} && llm-mem ui")
    print()
    print("    Start background workers (needs Ollama):")
    print(f"       cd {project} && llm-mem workers")
    print()
    print("    Start coding with memory:")
    print(f"       cd {project} && opencode")
    print()


if __name__ == "__main__":
    main()
