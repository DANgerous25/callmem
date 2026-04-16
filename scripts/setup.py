#!/usr/bin/env python3
"""Interactive setup for llm-mem in a project.

Run from any project root:
    uv run python scripts/setup.py
    make setup
    llm-mem setup

Safe to run multiple times — never wipes data. On repeat runs:
- Reads existing config and shows current values as defaults
- Backs up config.toml before overwriting
- Preserves database, vault keys, and all memory data
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from pathlib import Path

# ── Helpers ──────────────────────────────────────────────────────────


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer if answer else default


def ask_bool(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"  {prompt} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "1", "true")


def ask_choice(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    print(f"  {prompt}")
    for i, (key, desc) in enumerate(options, 1):
        marker = " (current)" if key == default else ""
        print(f"    {i}) {key} — {desc}{marker}")
    while True:
        raw = input(f"  Choice [default: {default}]: ").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        if raw in [o[0] for o in options]:
            return raw
        print(f"    Invalid choice. Enter 1-{len(options)} or a name.")


def port_available(port: int, host: str = "0.0.0.0") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
            return True
    except OSError:
        return False


def check_ollama(endpoint: str) -> tuple[bool, list[str]]:
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


def check_openai_compat(endpoint: str, api_key: str, model: str) -> bool:
    if not api_key:
        return False
    try:
        import httpx
        resp = httpx.post(
            f"{endpoint}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


def load_existing_config(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    try:
        import tomllib
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def find_opencode_config(project: Path) -> Path | None:
    for name in ("opencode.json", ".opencode.json"):
        p = project / name
        if p.exists():
            return p
    return None


# ── Session import ───────────────────────────────────────────────────


def _offer_session_import(project: Path, db_path: Path) -> None:
    """Check for existing OpenCode sessions and offer to import them."""
    try:
        from llm_mem.adapters.opencode_import import (
            DEFAULT_SESSION_DIR,
            discover_session_files,
            import_sessions,
            read_session_file,
        )
    except ImportError:
        return

    session_dir = DEFAULT_SESSION_DIR
    if not session_dir.is_dir():
        return

    files = discover_session_files(session_dir)
    if not files:
        return

    # Filter to files that look like real sessions (have messages)
    valid_files: list[tuple[Path, dict]] = []
    for f in files:
        data = read_session_file(f)
        if data and data.get("messages"):
            valid_files.append((f, data))

    if not valid_files:
        return

    print()
    print("── Existing session history ──")
    print()
    print(f"  Found {len(valid_files)} OpenCode session(s) in {session_dir}")
    print()

    # Show a preview of the sessions
    for i, (path, data) in enumerate(valid_files[:10], 1):
        sid = data.get("id", path.stem)
        title = data.get("title", "untitled")[:50]
        msg_count = len(data.get("messages", []))
        print(f"    {i}) {sid[:12]}... — {title} ({msg_count} messages)")

    if len(valid_files) > 10:
        print(f"    ... and {len(valid_files) - 10} more")

    print()
    do_import = ask_bool(
        "Import these sessions into llm-mem?",
        default=True,
    )

    if not do_import:
        print("  Skipped. You can import later with:")
        print(f"    llm-mem import --source opencode --all --project {project}")
        return

    if not db_path.exists():
        print("  Database not ready — skipping import.")
        print(f"  Run: llm-mem import --source opencode --all --project {project}")
        return

    try:
        from llm_mem.core.config import load_config
        from llm_mem.core.database import Database
        from llm_mem.core.engine import MemoryEngine

        config = load_config(project)
        db = Database(db_path)
        db.initialize()
        engine = MemoryEngine(db, config)

        print()
        print(f"  Importing {len(valid_files)} session(s)...")

        results = import_sessions(
            engine,
            session_dir,
            import_all=True,
        )

        imported = [r for r in results if not r.get("dry_run")]
        total_events = sum(r.get("event_count", 0) for r in imported)
        errors = sum(len(r.get("errors", [])) for r in imported)

        print(f"  Imported {len(imported)} session(s), {total_events} events")
        if errors:
            print(f"  {errors} error(s) during import (non-fatal, events still stored)")

    except Exception as exc:
        print(f"  Import failed: {exc}")
        print(f"  You can retry: llm-mem import --source opencode --all --project {project}")


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    print()
    print("=" * 56)
    print("  llm-mem setup")
    print("=" * 56)
    print()

    # ── Project ───────────────────────────────────────────────────
    cwd = Path.cwd()
    project = Path(ask("Project root", str(cwd)))
    project = project.expanduser().resolve()

    if not project.is_dir():
        print(f"  Error: {project} is not a directory")
        sys.exit(1)

    llm_mem_dir = project / ".llm-mem"
    config_path = llm_mem_dir / "config.toml"
    existing = load_existing_config(config_path)
    is_rerun = bool(existing)

    if is_rerun:
        print(f"  Existing setup found in {llm_mem_dir}")
        print("  Current values shown as defaults. Data is never wiped.")
    else:
        print(f"  New setup in {project}")

    print()

    # ── Project name ──────────────────────────────────────────────
    default_name = existing.get("project", {}).get("name", project.name)
    project_name = ask("Project name", default_name)

    # ── LLM backend ──────────────────────────────────────────────
    print()
    print("── LLM backend ──")
    print("  Used for entity extraction, summarization, and sensitive data scanning.")
    print()

    current_backend = existing.get("llm", {}).get("backend", "ollama")

    backend = ask_choice(
        "Which LLM backend?",
        [
            ("ollama", "Local Ollama instance (free, private, needs GPU)"),
            ("openai_compat", "OpenAI-compatible API (Z.ai/GLM, OpenAI, Groq, etc.)"),
            ("none", "Pattern matching only (no LLM features)"),
        ],
        default=current_backend,
    )

    # ── Ollama config ─────────────────────────────────────────────
    ollama_endpoint = existing.get("ollama", {}).get("endpoint", "http://localhost:11434")
    ollama_model = existing.get("ollama", {}).get("model", "qwen3:8b")
    ollama_timeout = existing.get("ollama", {}).get("timeout", 120)
    ollama_ok = False

    if backend == "ollama":
        print()
        print("── Ollama configuration ──")
        print()
        ollama_endpoint = ask("Ollama endpoint", ollama_endpoint)
        ollama_ok, available_models = check_ollama(ollama_endpoint)

        if ollama_ok:
            print(f"  Connected to Ollama at {ollama_endpoint}")
            if available_models:
                print(f"  Models available: {', '.join(available_models[:10])}")
        else:
            print(f"  Ollama not reachable at {ollama_endpoint}")
            print("  You can configure it now and start Ollama later.")

        ollama_model = ask("Model", ollama_model)
        ollama_timeout = int(ask("Timeout (seconds)", str(ollama_timeout)))

    # ── OpenAI-compatible config ──────────────────────────────────
    oai_endpoint = existing.get("openai_compat", {}).get("endpoint", "https://open.bigmodel.cn/api/paas/v4")
    oai_model = existing.get("openai_compat", {}).get("model", "glm-4-flash")
    oai_key_env = existing.get("openai_compat", {}).get("api_key_env", "LLM_MEM_API_KEY")
    oai_timeout = existing.get("openai_compat", {}).get("timeout", 120)
    oai_ok = False

    if backend == "openai_compat":
        print()
        print("── OpenAI-compatible API configuration ──")
        print()
        oai_endpoint = ask("API endpoint", oai_endpoint)
        oai_model = ask("Model name", oai_model)
        oai_key_env = ask("Env var name for API key", oai_key_env)
        oai_timeout = int(ask("Timeout (seconds)", str(oai_timeout)))

        api_key = os.environ.get(oai_key_env, "")
        if api_key:
            print(f"  Found {oai_key_env} in environment. Testing connection...")
            oai_ok = check_openai_compat(oai_endpoint, api_key, oai_model)
            if oai_ok:
                print("  API connection successful.")
            else:
                print("  API connection failed. Check endpoint/model/key.")
                print("  Config will be saved anyway — fix and re-run setup.")
        else:
            print(f"  {oai_key_env} not set in environment.")
            print(f"  Set it before running workers: export {oai_key_env}=your-key-here")

    if backend == "none":
        print()
        print("  No LLM backend selected.")
        print("  Pattern-based secret detection still works.")
        print("  Entity extraction, summarization, and compaction are disabled.")
        print("  You can change this later by re-running setup.")

    # ── UI ────────────────────────────────────────────────────────
    print()
    print("── Web UI ──")
    print()

    default_host = existing.get("ui", {}).get("host", "0.0.0.0")
    default_port = existing.get("ui", {}).get("port", 9090)

    ui_host = ask("Bind address (0.0.0.0 for network, 127.0.0.1 for local only)", default_host)
    ui_port = int(ask("Port", str(default_port)))

    if not port_available(ui_port, ui_host):
        print(f"  Port {ui_port} is in use on {ui_host}")
        while True:
            suggested = ui_port + 1
            ui_port = int(ask("Choose a different port", str(suggested)))
            if port_available(ui_port, ui_host):
                print(f"  Port {ui_port} is available.")
                break
            print(f"  Port {ui_port} is also in use.")

    # ── Sensitive data ────────────────────────────────────────────
    print()
    print("── Sensitive data protection ──")
    print()

    sd = existing.get("sensitive_data", {})
    sensitive_enabled = ask_bool(
        "Enable sensitive data detection?",
        default=sd.get("enabled", True),
    )

    vault_mode = sd.get("vault_mode", "auto")
    llm_scan = sd.get("llm_scan", backend != "none")

    if sensitive_enabled:
        vault_mode = ask_choice(
            "Vault mode (how to encrypt detected secrets):",
            [
                ("auto", "Random key stored in .llm-mem/vault.key (simplest)"),
                ("passphrase", "Key derived from LLM_MEM_VAULT_PASSPHRASE env var"),
                ("disabled", "Detect but don't encrypt (not recommended)"),
            ],
            default=vault_mode,
        )
        if backend != "none":
            llm_scan = ask_bool(
                "Enable LLM-based scanning (in addition to patterns)?",
                default=llm_scan,
            )
        else:
            llm_scan = False

    # ── Write config ──────────────────────────────────────────────
    print()
    print("── Writing configuration ──")
    print()

    llm_mem_dir.mkdir(exist_ok=True)

    config_content = f"""# llm-mem configuration
# See docs/config.md for all options
# Re-run 'make setup' or 'llm-mem setup' to change these safely

[project]
name = "{project_name}"

[llm]
backend = "{backend}"

[ollama]
model = "{ollama_model}"
endpoint = "{ollama_endpoint}"
timeout = {ollama_timeout}

[openai_compat]
endpoint = "{oai_endpoint}"
model = "{oai_model}"
api_key_env = "{oai_key_env}"
timeout = {oai_timeout}

[briefing]
max_tokens = {existing.get("briefing", {}).get("max_tokens", 2000)}

[compaction]
enabled = {str(existing.get("compaction", {}).get("enabled", True)).lower()}
schedule = "{existing.get("compaction", {}).get("schedule", "on_session_end")}"

[summarization]
chunk_size = {existing.get("summarization", {}).get("chunk_size", 20)}
cross_session_interval = {existing.get("summarization", {}).get("cross_session_interval", 5)}

[ui]
port = {ui_port}
host = "{ui_host}"

[sensitive_data]
enabled = {str(sensitive_enabled).lower()}
pattern_scan = true
llm_scan = {str(llm_scan).lower()}
vault_mode = "{vault_mode}"
"""

    if config_path.exists():
        backup = config_path.with_suffix(".toml.bak")
        shutil.copy2(config_path, backup)
        print(f"  Backed up existing config to {backup.name}")

    config_path.write_text(config_content)
    print(f"  Wrote {config_path}")

    # ── Database ──────────────────────────────────────────────────
    db_path = llm_mem_dir / "memory.db"
    if db_path.exists():
        print(f"  Database exists: {db_path} (preserved)")
    else:
        try:
            from llm_mem.core.database import Database
            db = Database(db_path)
            db.initialize()
            print(f"  Created database: {db_path} (schema v{db.get_schema_version()})")
        except ImportError:
            print("  Skipping DB init — llm-mem not installed.")
            print(f"  Run: llm-mem init --project {project}")

    # ── .gitignore ────────────────────────────────────────────────
    gitignore_path = project / ".gitignore"
    vault_entries = ["vault.key", "vault.salt"]
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        missing = [e for e in vault_entries if e not in content]
        if missing:
            with open(gitignore_path, "a") as f:
                f.write("\n# llm-mem vault secrets\n")
                for entry in missing:
                    f.write(f"{entry}\n")
            print(f"  Added {', '.join(missing)} to .gitignore")
    else:
        print("  No .gitignore — remember to exclude vault.key and vault.salt")

    # ── Session import ───────────────────────────────────────────
    _offer_session_import(project, db_path)

    # ── OpenCode MCP ──────────────────────────────────────────────
    print()
    print("── OpenCode MCP integration ──")
    print()

    oc_config_path = find_opencode_config(project)
    if oc_config_path:
        print(f"  Found {oc_config_path.name}")

    configure_mcp = ask_bool("Configure OpenCode to use llm-mem MCP server?", default=True)

    if configure_mcp:
        if oc_config_path is None:
            oc_config_path = project / "opencode.json"

        oc_config: dict = {}
        if oc_config_path.exists():
            try:
                oc_config = json.loads(oc_config_path.read_text())
            except json.JSONDecodeError:
                print(f"  Warning: could not parse {oc_config_path.name}")

        if "mcp" not in oc_config:
            oc_config["mcp"] = {}

        oc_config["mcp"]["llm-mem"] = {
            "type": "local",
            "command": ["uv", "run", "python", "-m", "llm_mem.mcp.server", "--project", "."],
            "enabled": True,
        }

        oc_config_path.write_text(json.dumps(oc_config, indent=2) + "\n")
        print(f"  Wrote MCP config to {oc_config_path.name}")

    # ── Summary ───────────────────────────────────────────────────
    print()
    print("=" * 56)
    print("  Setup complete!")
    print("=" * 56)
    print()
    print(f"  Project:      {project_name}")
    print(f"  Database:     {db_path}")
    print(f"  LLM backend:  {backend}", end="")
    if backend == "ollama":
        status = "connected" if ollama_ok else "not reachable"
        print(f" ({ollama_model}, {status})")
    elif backend == "openai_compat":
        status = "connected" if oai_ok else "not tested"
        print(f" ({oai_model} via {oai_key_env}, {status})")
    else:
        print()
    print(f"  Web UI:       http://{ui_host}:{ui_port}")
    print(f"  Vault:        {vault_mode}")
    print()
    print("  Next steps:")
    print()

    if backend == "ollama" and not ollama_ok:
        print("    Start Ollama and pull the model:")
        print(f"      ollama pull {ollama_model}")
        print()
    elif backend == "openai_compat" and not oai_ok:
        print("    Set your API key:")
        print(f"      export {oai_key_env}=your-key-here")
        print()

    print("    Start the web UI:")
    print(f"      llm-mem ui --project {project}")
    print()
    if backend != "none":
        print("    Start background workers:")
        print(f"      llm-mem workers --project {project}")
        print()
    print("    Import existing sessions (if not done above):")
    print(f"      llm-mem import --source opencode --all --project {project}")
    print()
    print("    Start coding with memory:")
    print(f"      cd {project} && opencode")
    print()
    print("  Re-run this wizard anytime: make setup")
    print()


if __name__ == "__main__":
    main()
