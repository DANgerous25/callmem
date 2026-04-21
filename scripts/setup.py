#!/usr/bin/env python3
"""Interactive setup for callmem in a project.

Run from any project root:
    uv run python scripts/setup.py
    make setup
    callmem setup

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


def _toml_list(items: list[str]) -> str:
    """Serialize a list of strings as a TOML array literal."""
    if not items:
        return "[]"
    quoted = ", ".join(json.dumps(x) for x in items)
    return f"[{quoted}]"


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
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
    except OSError:
        return False


def _wait_port_free(
    port: int, host: str = "0.0.0.0", timeout: float = 10.0,
) -> bool:
    """Poll until the port is bindable or timeout elapses."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_available(port, host):
            return True
        time.sleep(0.2)
    return False


def _stop_own_service(project: Path, port: int | None = None) -> str | None:
    """Stop this project's systemd service if running.

    If ``port`` is given, wait (up to 10s) for the socket to actually
    release before returning — systemctl stop returns before the
    process finishes tearing down, and a bare ``time.sleep(1)`` isn't
    enough, which caused setup to keep incrementing to a fresh port.
    Returns the service name so the caller can restart it afterwards.
    """
    import subprocess

    svc_name = _service_name(project)
    unit_path = Path.home() / ".config" / "systemd" / "user" / f"{svc_name}.service"
    if not unit_path.exists():
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", svc_name],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "active":
            subprocess.run(
                ["systemctl", "--user", "stop", svc_name],
                capture_output=True, check=True,
            )
            if port is not None:
                _wait_port_free(port)
            else:
                import time
                time.sleep(1)
            return svc_name
    except Exception:
        pass
    return None


def _restart_service(svc_name: str) -> None:
    """Restart a previously stopped service."""
    import contextlib
    import subprocess

    with contextlib.suppress(Exception):
        subprocess.run(
            ["systemctl", "--user", "start", svc_name],
            capture_output=True, check=True,
        )


def _is_service_active(svc_name: str) -> bool:
    """Check if a systemd user service is currently active."""
    import subprocess

    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", svc_name],
            capture_output=True, text=True,
        )
        return result.stdout.strip() == "active"
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _find_other_callmem_ports(current_project: Path) -> dict[int, str]:
    """Scan other callmem projects for their configured UI ports.

    Looks at sibling callmem service files to find their
    WorkingDirectory, then reads the config.toml for the port.
    Returns {port: project_name}.
    """
    used: dict[int, str] = {}
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if not systemd_dir.is_dir():
        return used

    # Include legacy llm-mem-*.service units so ports on pre-rename installs
    # still count as "in use" when picking a port for the new project.
    units = list(systemd_dir.glob("callmem-*.service")) + list(
        systemd_dir.glob("llm-mem-*.service")
    )
    for unit in units:
        try:
            content = unit.read_text()
            for line in content.splitlines():
                if line.startswith("WorkingDirectory="):
                    proj_path = Path(line.split("=", 1)[1].strip())
                    if proj_path.resolve() == current_project.resolve():
                        continue
                    # Prefer .callmem/ config; fall back to legacy .llm-mem/
                    cfg = proj_path / ".callmem" / "config.toml"
                    if not cfg.exists():
                        cfg = proj_path / ".llm-mem" / "config.toml"
                    if cfg.exists():
                        other = load_existing_config(cfg)
                        p = other.get("ui", {}).get("port")
                        if p is not None:
                            name = other.get(
                                "project", {}
                            ).get("name", proj_path.name)
                            used[int(p)] = name
                    break
        except Exception:
            continue
    return used


def check_ollama(endpoint: str) -> tuple[bool, list[str], list[dict]]:
    """Check Ollama availability and return (ok, model_names, model_details).

    model_details is a list of dicts with 'name' and 'size' keys.
    """
    try:
        import httpx
        resp = httpx.get(f"{endpoint}/api/tags", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("models", [])
            model_names = [m["name"] for m in models]
            model_details = [
                {"name": m.get("name", ""), "size": m.get("size", 0)}
                for m in models
            ]
            return True, model_names, model_details
    except Exception:
        pass
    return False, [], []


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
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib  # type: ignore[no-redef]
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


def find_claude_mcp_config(project: Path) -> Path | None:
    p = project / ".mcp.json"
    return p if p.exists() else None


def _claude_md_is_separate_file(claude_path: Path, agents_path: Path) -> bool:
    """True if CLAUDE.md exists as a separate file (not a symlink to AGENTS.md)."""
    if not claude_path.exists() and not claude_path.is_symlink():
        return False
    if claude_path.is_symlink():
        try:
            target = claude_path.resolve(strict=False)
            return target != agents_path.resolve(strict=False)
        except OSError:
            return True
    return True


from callmem.core.integrations import (
    detect_mcp_command as _detect_mcp_command,
    ensure_claude_code_commands as _ensure_claude_code_commands,
    ensure_claude_code_mcp as _ensure_claude_code_mcp,
    ensure_opencode_plugin as _ensure_opencode_plugin,
)


# ── Session import ───────────────────────────────────────────────────


def _offer_session_import(project: Path, db_path: Path) -> None:
    """Check for existing OpenCode sessions and offer to import them."""
    try:
        from callmem.adapters.opencode_import import (
            DEFAULT_DB_PATH,
            discover_sessions,
            import_sessions,
        )
    except ImportError:
        return

    oc_db = DEFAULT_DB_PATH
    if not oc_db.is_file():
        return

    all_sessions = discover_sessions(db_path=oc_db, project_path=str(project))
    if not all_sessions:
        return

    projects: dict[str, list[dict]] = {}
    for s in all_sessions:
        key = s.get("project_worktree", "unknown")
        projects.setdefault(key, []).append(s)

    print()
    print("── Existing session history ──")
    print()
    print(f"  Found {len(all_sessions)} OpenCode session(s) across {len(projects)} project(s)")
    print()

    for worktree, sessions in projects.items():
        name = sessions[0].get("project_name", Path(worktree).name if worktree else "unknown")
        print(f"  {name} ({worktree}): {len(sessions)} session(s)")
        for s in sessions[:5]:
            title = (s.get("title") or "untitled")[:50]
            print(f"    - {s['id'][:12]}... — {title} ({s['message_count']} messages)")
        if len(sessions) > 5:
            print(f"    ... and {len(sessions) - 5} more")

    print()
    do_import = ask_bool(
        "Import these sessions into callmem?",
        default=True,
    )

    if not do_import:
        print("  Skipped. You can import later with:")
        print(
            f"    callmem import --source opencode --all "
            f"--project {project} --project-path {project}"
        )
        return

    if not db_path.exists():
        print("  Database not ready — skipping import.")
        print(
            f"  Run: callmem import --source opencode --all "
            f"--project {project} --project-path {project}"
        )
        return

    total_sessions = len(all_sessions)
    est_minutes = max(1, total_sessions // 10)
    print()
    print(f"  Import {total_sessions} sessions (estimated ~{est_minutes} minute(s))?")
    print("    [1] Import now (wait for completion)")
    print("    [2] Import in background (start working immediately)")
    print()
    choice = ask("Choice", "1")

    if choice.strip() == "2":
        _run_setup_background_import(project, oc_db)
        return

    try:
        import time

        from callmem.core.config import load_config
        from callmem.core.database import Database
        from callmem.core.engine import MemoryEngine

        config = load_config(project)
        db = Database(db_path)
        db.initialize()
        engine = MemoryEngine(db, config)

        print()
        print(f"  Importing {total_sessions} session(s)...")

        start_time = time.monotonic()

        def _on_progress(update: dict) -> None:
            phase = update.get("phase", "")
            if phase == "discovery":
                print(
                    f"  Discovered {update['total_sessions']} sessions "
                    f"(~{update['total_events_estimate']} events)"
                )
            elif phase == "importing":
                idx = update["session_index"]
                total = update["total_sessions"]
                title = (update.get("session_title") or "untitled")[:40]
                events = update.get("session_events", 0)
                print(
                    f"  [{idx}/{total}] {title} — {events} events "
                    f"({update['total_events_so_far']} total)"
                )

        results = import_sessions(
            engine,
            db_path=oc_db,
            project_path=str(project),
            import_all=True,
            progress_callback=_on_progress,
            project=project,
        )

        elapsed = time.monotonic() - start_time
        imported = [r for r in results if not r.get("dry_run")]
        total_events = sum(r.get("event_count", 0) for r in imported)
        errors = sum(len(r.get("errors", [])) for r in imported)

        if elapsed < 60:
            elapsed_str = f"{elapsed:.0f}s"
        else:
            elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

        print()
        print("  Import complete:")
        print(f"    Sessions: {len(imported)} imported")
        print(f"    Events:   {total_events} ingested")
        print(f"    Errors:   {errors}")
        print(f"    Time:     {elapsed_str}")
        print()
        print("  Extraction will continue in the background via the worker.")

    except Exception as exc:
        print(f"  Import failed: {exc}")
        print(
            f"  You can retry: callmem import --source opencode --all "
            f"--project {project} --project-path {project}"
        )


def _run_setup_background_import(project: Path, oc_db: Path) -> None:
    """Fork import into background from setup wizard."""
    import subprocess
    import sys

    cmd = [
        sys.executable, "-m", "callmem.cli",
        "import",
        "--source", "opencode",
        "--project", str(project),
        "--project-path", str(project),
        "--opencode-db", str(oc_db),
        "--all",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    print()
    print(f"  Import running in background (PID {proc.pid}).")
    print("  Check progress: callmem import --status")
    print("  Extraction will begin automatically once events are ingested.")
    print("  You can open OpenCode now — new memories will appear as they're processed.")


# ── Systemd service ───────────────────────────────────────────────


def _service_name(project: Path) -> str:
    """Derive a systemd service name from the project path."""
    return f"callmem-{project.name}"


def _offer_systemd_service(
    project: Path, ui_host: str, ui_port: int
) -> None:
    """Offer to install a systemd user service for the daemon."""
    # Only offer on Linux systems with systemd
    if sys.platform != "linux":
        return

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if not systemd_dir.parent.exists():
        # No systemd user directory structure
        return

    print()
    print("── Autostart ──")
    print()
    print(
        "  callmem can install a systemd user service so the"
        " daemon"
    )
    print(
        "  (UI + workers + adapter) starts automatically"
        " on login."
    )
    print()

    install = ask_bool(
        "Install systemd user service?", default=True
    )
    if not install:
        print("  Skipped. Start manually with:")
        print(f"    callmem daemon --project {project}")
        return

    svc_name = _service_name(project)
    unit_path = systemd_dir / f"{svc_name}.service"

    # Find the callmem binary
    callmem_bin = shutil.which("callmem")
    if callmem_bin is None:
        # Fall back to uv run
        callmem_bin = f"{shutil.which('uv') or 'uv'} run callmem"

    # Collect env vars the service needs
    env_lines = [
        f"Environment=PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
    ]
    # Pass through API key env vars if set
    config_path = project / ".callmem" / "config.toml"
    if config_path.exists():
        cfg = load_existing_config(config_path)
        key_env = cfg.get(
            "openai_compat", {}
        ).get("api_key_env", "CALLMEM_API_KEY")
        key_val = os.environ.get(key_env, "")
        if key_val:
            env_lines.append(f"Environment={key_env}={key_val}")
    # Also pass HOME (needed for ~/.local/share/opencode)
    env_lines.append(
        f"Environment=HOME={Path.home()}"
    )

    env_block = "\n".join(env_lines)

    unit_content = f"""[Unit]
Description=callmem daemon for {project.name}
After=network.target

[Service]
Type=simple
WorkingDirectory={project}
ExecStart={callmem_bin} daemon --project {project}
Restart=on-failure
RestartSec=5
{env_block}

[Install]
WantedBy=default.target
"""

    systemd_dir.mkdir(parents=True, exist_ok=True)

    if unit_path.exists():
        print(f"  Updating existing service: {svc_name}")
    else:
        print(f"  Creating service: {svc_name}")

    unit_path.write_text(unit_content)
    print(f"  Wrote {unit_path}")

    # Reload and enable
    try:
        import subprocess

        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", svc_name],
            check=True,
            capture_output=True,
        )

        start_now = ask_bool("Start the service now?", default=True)
        if start_now:
            subprocess.run(
                [
                    "systemctl", "--user",
                    "restart", svc_name,
                ],
                check=True,
                capture_output=True,
            )
            print(f"  Service {svc_name} started.")
        else:
            print("  Service enabled. Start with:")
            print(
                f"    systemctl --user start {svc_name}"
            )

        print()
        print("  Useful commands:")
        print(
            f"    systemctl --user status {svc_name}"
        )
        print(
            f"    journalctl --user -u {svc_name} -f"
        )
        print(
            f"    systemctl --user restart {svc_name}"
        )

    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"  systemctl command failed: {exc}")
        print("  Service file written but not enabled.")
        print("  Enable manually:")
        print(
            f"    systemctl --user daemon-reload"
            f" && systemctl --user enable --now {svc_name}"
        )


# ── AGENTS.md patching ──────────────────────────────────────────────

_SESSION_SUMMARY_SNIPPET = (
    "\n## Startup briefing\n\n"
    "At the **start of every session**, read `SESSION_SUMMARY.md` (in the project root) "
    "if it exists. It contains an auto-generated briefing with recent context, key "
    "entities, and open tasks from previous sessions.\n\n"
    "After reading it, present a brief summary to the user: greet them, state the "
    "project name, mention the most recent session activity, highlight any open TODOs "
    "or unresolved failures, and ask what they'd like to work on.\n"
)


def _ensure_agents_session_summary(agents_path: Path) -> None:
    """Patch an existing AGENTS.md to reference SESSION_SUMMARY.md if missing."""
    if not agents_path.exists():
        return
    content = agents_path.read_text(encoding="utf-8")
    if "SESSION_SUMMARY.md" in content:
        print("  AGENTS.md already references SESSION_SUMMARY.md")
        return
    content += _SESSION_SUMMARY_SNIPPET
    agents_path.write_text(content, encoding="utf-8")
    print("  Patched AGENTS.md with SESSION_SUMMARY.md startup reference")


_MCP_BLOCK_SNIPPET = (

    "\n## Memory (callmem)\n\n"
    "This project uses callmem for persistent memory via MCP tools.\n\n"
    "**Start of session:**\n"
    "1. Read `SESSION_SUMMARY.md` (if it exists) for an auto-generated briefing\n"
    "2. Call `mem_session_start` to register this session\n"
    "3. Present a brief summary: greet the user, mention recent activity, highlight open TODOs\n\n"
    "**During the session:**\n"
    "- When you make a design decision, call `mem_ingest` with type \"decision\"\n"
    "- When you identify a TODO, call `mem_ingest` with type \"todo\"\n"
    "- When you discover something notable, call `mem_ingest` with type \"discovery\"\n"
    "- When something fails unexpectedly, call `mem_ingest` with type \"failure\"\n"
    "- To recall past context, call `mem_search` with keywords\n"
    "- To see open tasks, call `mem_get_tasks`\n\n"
    "**Before re-reading a file you've worked on before:**\n"
    "- Call `mem_file_context` with the file path\n"
    "- If the returned timeline covers what you need, skip the raw read (saves tokens)\n"
    "- If you need exact line-level details, read the file normally\n\n"
    "**Long sessions (50+ messages):**\n"
    "- Every ~30 messages, call `mem_check_context` with your approximate message count\n"
    "- If it returns `compress_recommended`, summarize the oldest ~30 messages (preserve decisions/TODOs/failures verbatim) and call `mem_compress_context`\n"
    "- Replace the compressed span in your context with the returned marker; use `mem_search` to recall specifics\n\n"
    "**End of session:**\n"
    "- Call `mem_session_end` to trigger summary generation\n\n"
    "**Guidelines:**\n"
    "- Be specific in memory content (include file paths, function names, error messages)\n"
    "- Set priority on TODOs: high, medium, or low\n"
    "- Mark failures as resolved when you fix them\n"
    "- The system captures raw events automatically — focus on recording decisions and TODOs\n"
)

_MCP_SENTINELS = ("## Memory (callmem)", "mem_ingest", "mem_session_start")


def _ensure_agents_mcp_block(agents_path: Path) -> None:
    """Patch an existing AGENTS.md with callmem MCP tool usage instructions."""
    if not agents_path.exists():
        return
    content = agents_path.read_text(encoding="utf-8")
    if any(s in content for s in _MCP_SENTINELS):
        print("  AGENTS.md already has callmem instructions")
        return
    content += _MCP_BLOCK_SNIPPET
    agents_path.write_text(content, encoding="utf-8")
    print("  Patched AGENTS.md with callmem MCP tool instructions")


# ── Initial briefing generation ────────────────────────────────────


def _generate_initial_briefing(project: Path, db_path: Path) -> None:
    """Generate SESSION_SUMMARY.md so agents get context on first launch."""
    if not db_path.exists():
        return
    try:
        from callmem.core.briefing import BriefingGenerator
        from callmem.core.config import load_config
        from callmem.core.database import Database
        from callmem.core.engine import MemoryEngine

        config = load_config(project)
        db = Database(db_path)
        db.initialize()
        engine = MemoryEngine(db, config)
        gen = BriefingGenerator(engine.repo, config, engine.ollama)

        project_name = config.project.name or "default"
        briefing = gen.write_session_summary(
            project_id=engine.project_id,
            project_name=project_name,
            worktree_path=project,
        )
        print(f"  Wrote SESSION_SUMMARY.md ({briefing.token_count} tokens)")
    except Exception as exc:
        print(f"  Could not generate SESSION_SUMMARY.md: {exc}")
        print("  Generate manually: callmem briefing --write -p .")


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    print()
    print("=" * 56)
    print("  callmem setup")
    print("=" * 56)
    print()

    # ── Project ───────────────────────────────────────────────────
    cwd = Path.cwd()
    project = Path(ask("Project root", str(cwd)))
    project = project.expanduser().resolve()

    if not project.is_dir():
        print(f"  Error: {project} is not a directory")
        sys.exit(1)

    callmem_dir = project / ".callmem"
    config_path = callmem_dir / "config.toml"
    existing = load_existing_config(config_path)
    is_rerun = bool(existing)

    if is_rerun:
        print(f"  Existing setup found in {callmem_dir}")
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
        print("  TIP: If using a VPN that blocks localhost access from other tools,")
        print("       use your LAN IP (e.g. http://192.168.1.100:11434).")
        print()
        ollama_endpoint = ask("Ollama endpoint", ollama_endpoint)
        ollama_ok, available_models, model_details = check_ollama(ollama_endpoint)

        if ollama_ok:
            print(f"  Connected to Ollama at {ollama_endpoint}")

            from callmem.core.gpu_scan import (
                ModelInfo,
                SystemInfo,
                detect_system,
                fetch_ollama_models,
                format_recommendation_table,
                pick_best,
                recommend_models,
            )

            sys_info = detect_system()
            if sys_info.gpu.available:
                models_with_size = [
                    ModelInfo(name=d["name"], size_bytes=d["size"])
                    for d in model_details if d["size"]
                ]
                if models_with_size:
                    recs = recommend_models(models_with_size, sys_info.gpu)
                    table = format_recommendation_table(
                        sys_info.gpu, sys_info.ram_mb, recs,
                    )
                    print()
                    print(table)
                    print()

                    best = pick_best(recs)
                    if best:
                        print(f"  Recommended: {best.model.name} ({best.note})")
                        if ollama_model not in [m.name for m in models_with_size]:
                            ollama_model = best.model.name
                else:
                    print(f"  Models available: {', '.join(available_models[:10])}")
            else:
                print("  (GPU info not available — skipping VRAM estimates)")
                if available_models:
                    print(f"  Models available: {', '.join(available_models[:10])}")
        else:
            print(f"  Ollama not reachable at {ollama_endpoint}")
            print("  You can configure it now and start Ollama later.")

        ollama_model = ask("Model", ollama_model)
        ollama_timeout = int(ask("Timeout (seconds)", str(ollama_timeout)))

        existing_num_ctx = existing.get("ollama", {}).get("num_ctx", None)
        num_ctx_str = ""
        if existing_num_ctx is not None:
            num_ctx_str = str(existing_num_ctx)

        if ollama_ok and sys_info.gpu.available:
            chosen_model_info = None
            for d in model_details:
                if d["name"] == ollama_model:
                    chosen_model_info = ModelInfo(
                        name=d["name"], size_bytes=d["size"],
                    )
                    break

            if chosen_model_info and chosen_model_info.size_bytes:
                recs = recommend_models([chosen_model_info], sys_info.gpu)
                if recs and recs[0].fit_status in ("tight", "oom"):
                    rec = recs[0]
                    suggested_ctx = rec.recommended_ctx or 8192
                    print()
                    print(
                        f"  \u26a0\ufe0f  {ollama_model} uses ~{chosen_model_info.size_gb:.1f} GB "
                        f"— leaving ~{rec.free_after_mb} MB for context."
                    )
                    print(
                        "  callmem extraction batches are small, "
                        "so a reduced context window works fine."
                    )
                    print()
                    num_ctx_str = ask(
                        f"Context window (num_ctx)",
                        num_ctx_str or str(suggested_ctx),
                    )
                elif recs and recs[0].fit_status in ("easy", "ok"):
                    if not num_ctx_str:
                        num_ctx_str = ""

        ollama_num_ctx = None
        if num_ctx_str.strip():
            try:
                ollama_num_ctx = int(num_ctx_str.strip())
            except ValueError:
                ollama_num_ctx = None
    else:
        ollama_num_ctx = None

    # ── OpenAI-compatible config ──────────────────────────────────
    oai_endpoint = existing.get("openai_compat", {}).get("endpoint", "https://open.bigmodel.cn/api/paas/v4")
    oai_model = existing.get("openai_compat", {}).get("model", "glm-4-flash")
    oai_key_env = existing.get("openai_compat", {}).get("api_key_env", "CALLMEM_API_KEY")
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

    # ── Event filtering ──────────────────────────────────────────
    print()
    print("── Event filtering ──")
    print()
    print("  Skip noisy tool calls to reduce extraction load.")
    print("  Common candidates: Glob, TodoWrite, BashOutput.")
    print()

    existing_ingestion = existing.get("ingestion", {}) or {}
    existing_skip_tools = existing_ingestion.get("skip_tools", []) or []
    skip_tools_default = ", ".join(existing_skip_tools)
    skip_tools_raw = ask(
        "Skip tools (comma-separated, empty for none)", skip_tools_default,
    )
    skip_tools = [
        s.strip() for s in skip_tools_raw.split(",") if s.strip()
    ]

    existing_skip_patterns = existing_ingestion.get("skip_patterns", []) or []
    skip_patterns_default = ", ".join(existing_skip_patterns)
    skip_patterns_raw = ask(
        "Skip patterns (glob on event content, comma-separated)",
        skip_patterns_default,
    )
    skip_patterns = [
        s.strip() for s in skip_patterns_raw.split(",") if s.strip()
    ]

    # ── UI ────────────────────────────────────────────────────────
    print()
    print("── Web UI ──")
    print()

    default_host = existing.get("ui", {}).get("host", "0.0.0.0")
    default_port = existing.get("ui", {}).get("port", 9090)

    # Stop this project's own service so its port isn't flagged as in-use,
    # and wait for the socket to actually release before the availability
    # probe below runs.
    stopped_service = (
        _stop_own_service(project, port=default_port) if is_rerun else None
    )
    if stopped_service:
        print(f"  Stopped {stopped_service} for port check (will restart after setup)")

    # Check what ports other callmem projects are using
    other_ports = _find_other_callmem_ports(project)
    if other_ports:
        print("  Ports used by other callmem projects:")
        for p, name in sorted(other_ports.items()):
            print(f"    {p} — {name}")
        # Auto-suggest a free port if default conflicts with a *different*
        # project. On a re-run we always prefer reclaiming the existing
        # port — we just stopped our own service to free it.
        if default_port in other_ports and not is_rerun:
            default_port = max(other_ports.keys()) + 1
            print(f"  Suggesting {default_port} to avoid conflict.")
        print()

    ui_host = ask(
        "Bind address (0.0.0.0 for network, 127.0.0.1 for local only)",
        default_host,
    )
    ui_port = int(ask("Port", str(default_port)))

    # Warn if this port is claimed by another project
    if ui_port in other_ports:
        print(
            f"  Warning: port {ui_port} is used by"
            f" '{other_ports[ui_port]}'"
        )

    if not port_available(ui_port, ui_host):
        print(f"  Port {ui_port} is in use on {ui_host}")
        while True:
            suggested = ui_port + 1
            while suggested in other_ports:
                suggested += 1
            ui_port = int(
                ask("Choose a different port", str(suggested))
            )
            if ui_port in other_ports:
                print(
                    f"  Warning: port {ui_port} is used"
                    f" by '{other_ports[ui_port]}'"
                )
                continue
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
                ("auto", "Random key stored in .callmem/vault.key (simplest)"),
                ("passphrase", "Key derived from CALLMEM_VAULT_PASSPHRASE env var"),
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

    callmem_dir.mkdir(exist_ok=True)

    config_content = f"""# callmem configuration
# See docs/config.md for all options
# Re-run 'make setup' or 'callmem setup' to change these safely

[project]
name = "{project_name}"

[llm]
backend = "{backend}"

[ollama]
model = "{ollama_model}"
endpoint = "{ollama_endpoint}"
timeout = {ollama_timeout}{''
if ollama_num_ctx is not None else
''}"""
    if ollama_num_ctx is not None:
        config_content += f"\nnum_ctx = {ollama_num_ctx}"

    config_content += f"""

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

[ingestion]
skip_tools = {_toml_list(skip_tools)}
skip_patterns = {_toml_list(skip_patterns)}
"""

    if config_path.exists():
        backup = config_path.with_suffix(".toml.bak")
        shutil.copy2(config_path, backup)
        print(f"  Backed up existing config to {backup.name}")

    config_path.write_text(config_content)
    print(f"  Wrote {config_path}")

    # ── Database ──────────────────────────────────────────────────
    db_path = callmem_dir / "memory.db"
    if db_path.exists():
        print(f"  Database exists: {db_path} (preserved)")
    else:
        try:
            from callmem.core.database import Database
            db = Database(db_path)
            db.initialize()
            print(f"  Created database: {db_path} (schema v{db.get_schema_version()})")
        except ImportError:
            print("  Skipping DB init — callmem not installed.")
            print(f"  Run: callmem init --project {project}")

    # ── .gitignore ────────────────────────────────────────────────
    gitignore_path = project / ".gitignore"
    vault_entries = ["vault.key", "vault.salt"]
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        missing = [e for e in vault_entries if e not in content]
        if missing:
            with open(gitignore_path, "a") as f:
                f.write("\n# callmem vault secrets\n")
                for entry in missing:
                    f.write(f"{entry}\n")
            print(f"  Added {', '.join(missing)} to .gitignore")
    else:
        print("  No .gitignore — remember to exclude vault.key and vault.salt")

    # ── Session import ───────────────────────────────────────────
    _offer_session_import(project, db_path)

    # ── AGENTS.md / CLAUDE.md — ensure SESSION_SUMMARY + MCP reference ──
    agents_path = project / "AGENTS.md"
    claude_path = project / "CLAUDE.md"
    _ensure_agents_session_summary(agents_path)
    _ensure_agents_mcp_block(agents_path)
    if _claude_md_is_separate_file(claude_path, agents_path):
        _ensure_agents_session_summary(claude_path)
        _ensure_agents_mcp_block(claude_path)

    # ── Generate initial briefing ────────────────────────────────
    _generate_initial_briefing(project, db_path)

    # ── Coding tool integration ──────────────────────────────────
    print()
    print("── Coding tool integration ──")
    print()

    oc_config_path = find_opencode_config(project)
    claude_config_path = find_claude_mcp_config(project)
    has_claude_md = _claude_md_is_separate_file(claude_path, agents_path)

    oc_detected = oc_config_path is not None
    claude_detected = claude_config_path is not None or has_claude_md

    if oc_detected and claude_detected:
        default_tool = "both"
    elif oc_detected:
        default_tool = "opencode"
    elif claude_detected:
        default_tool = "claude"
    else:
        default_tool = "both"

    def _label(base: str, detected: bool) -> str:
        return f"{base} [detected]" if detected else base

    tool_choice = ask_choice(
        "Which coding tools do you use?",
        [
            ("opencode", _label("OpenCode only", oc_detected)),
            ("claude", _label("Claude Code only", claude_detected)),
            ("both", "Both OpenCode and Claude Code"),
            ("skip", "Skip (manual integration)"),
        ],
        default=default_tool,
    )

    configure_opencode = tool_choice in ("opencode", "both")
    configure_claude = tool_choice in ("claude", "both")

    if configure_opencode:
        print()
        print("── OpenCode MCP ──")
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

        detected_cmd = _detect_mcp_command(project)
        old_cmd = oc_config["mcp"].get("callmem", {}).get("command")
        oc_config["mcp"]["callmem"] = {
            "type": "local",
            "command": detected_cmd,
            "enabled": True,
        }
        if old_cmd is not None and old_cmd != detected_cmd:
            print("  Updated MCP server command in opencode.json")

        instructions = oc_config.get("instructions", [])
        if "SESSION_SUMMARY.md" not in instructions:
            instructions.append("SESSION_SUMMARY.md")
            oc_config["instructions"] = instructions
            print("  Added SESSION_SUMMARY.md to OpenCode instructions")

        oc_config_path.write_text(json.dumps(oc_config, indent=2) + "\n")
        print(f"  Wrote MCP config to {oc_config_path.name}")

        _ensure_opencode_plugin(project)

    if configure_claude:
        print()
        print("── Claude Code MCP ──")
        _ensure_claude_code_mcp(project)
        _ensure_claude_code_commands(project)

    if tool_choice == "skip":
        print("  Skipped coding tool integration.")
        print("  To configure later, re-run setup or edit opencode.json / .mcp.json manually.")

    # ── Autostart ─────────────────────────────────────────────────
    _offer_systemd_service(project, ui_host, ui_port)

    # ── Restart daemon if service exists ──────────────────────────
    svc_name = _service_name(project)
    svc_was_active = _is_service_active(svc_name)

    # If we stopped the service for the port check, make sure it's restarted
    if stopped_service:
        _restart_service(stopped_service)

    # If the service is installed (active or was active before port check), restart it
    unit_path = Path.home() / ".config" / "systemd" / "user" / f"{svc_name}.service"
    if unit_path.exists() and (svc_was_active or stopped_service):
        _restart_service(svc_name)
        print(f"  Restarted {svc_name} to pick up config changes.")

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

    daemon_active = _is_service_active(svc_name)
    if daemon_active:
        print(f"  Daemon:       {svc_name} (running)")
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

    if not daemon_active and not unit_path.exists():
        print("    Start everything (UI + workers + adapter):")
        print(f"      callmem daemon --project {project}")
        print()

    if not daemon_active:
        print("    Import existing sessions (if not done above):")
        print(
            f"      callmem import --source opencode --all"
            f" --project {project} --project-path {project}"
        )
        print()

    print("    Start coding with memory:")
    print(f"      cd {project} && opencode")
    print()
    print("  Re-run this wizard anytime: make setup")
    print()


if __name__ == "__main__":
    main()
