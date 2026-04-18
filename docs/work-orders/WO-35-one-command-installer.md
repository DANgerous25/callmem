# WO-35 — One-Command Installer with Dependency Prompts

## Goal

Make `llm-mem` installable from a single command that handles all dependencies, installs the package, and optionally runs setup — so a new user goes from zero to working in one shot.

## Background

Currently, installing llm-mem requires multiple steps:

1. `git clone https://github.com/DANgerous25/llm-mem.git`
2. Figure out Python install method (pip, venv, pipx — varies by OS)
3. Handle PEP 668 on Ubuntu 24.04+ (externally-managed-environment error)
4. `pip install -e .`
5. Manually run `llm-mem setup` in the target project

New machines may also be missing system dependencies (e.g. `python3-venv`, `python3-dev`, build tools). The user shouldn't need to debug these one by one.

## Deliverables

### 1. Installer script: `install.sh` (in repo root)

A self-contained bash script that:

a) **Checks system dependencies** and prompts to install missing ones:
   - `python3` (>= 3.10)
   - `python3-venv` or `python3.X-venv` (for the venv)
   - `python3-pip` (as fallback)
   - `git` (should already be there if they cloned, but check anyway)
   - `build-essential` / `python3-dev` (if any compiled deps need it)

   For each missing dependency:
   ```
   python3-venv is required but not installed.
   Install it now? [Y/n]:
   ```
   Use `sudo apt install` on Debian/Ubuntu, detect the package manager.

b) **Creates a virtual environment** if not already present:
   ```
   Creating virtual environment in ~/llm-mem/.venv...
   ```
   Skip if `.venv/` already exists and has a working Python.

c) **Installs the package** in the venv:
   ```
   Installing llm-mem and dependencies...
   .venv/bin/pip install -e .
   ```

d) **Adds the venv bin to PATH** — either:
   - Creates a symlink: `~/.local/bin/llm-mem -> ~/llm-mem/.venv/bin/llm-mem`
   - Or adds to `.bashrc` / `.profile` if `~/.local/bin` isn't on PATH
   - Prompt the user:
     ```
     Add llm-mem to your PATH? [Y/n]:
       1) Symlink to ~/.local/bin (recommended)
       2) Add ~/llm-mem/.venv/bin to .bashrc
     ```

e) **Prompts to run setup** at the end:
   ```
   Installation complete!

   Run setup now for a project? [Y/n]:
   Project path [.]:
   ```
   If yes, runs `llm-mem setup` (using the venv python). If no, prints the manual command.

### 2. One-liner install

The script should be runnable directly from a fresh clone:

```bash
git clone https://github.com/DANgerous25/llm-mem.git ~/llm-mem && bash ~/llm-mem/install.sh
```

Or if the user already has it cloned:

```bash
cd ~/llm-mem && bash install.sh
```

### 3. Idempotent / safe to re-run

- If venv exists and package is installed, skip those steps (just verify)
- If PATH symlink exists, skip
- If dependencies are already installed, skip (don't re-prompt)
- Always offer the setup prompt at the end (user may want to set up a new project)

### 4. Update README

Update the README install instructions to show the one-liner:

```bash
git clone https://github.com/DANgerous25/llm-mem.git ~/llm-mem
bash ~/llm-mem/install.sh
```

Remove the manual `pip install -e .` / `uv sync` instructions and replace with the installer.

## Constraints

- Must work on Ubuntu 22.04 (Python 3.10) and Ubuntu 24.04 (Python 3.12, PEP 668)
- Must handle the externally-managed-environment error gracefully (use venv, never `--break-system-packages`)
- No AI attribution
- Detect package manager: `apt` (Debian/Ubuntu), fall back to manual instructions for other distros
- Never run `sudo` without prompting first
- Python 3.10 minimum — fail with a clear message if older

## Acceptance criteria

- [ ] Fresh Ubuntu 24.04 box: single command installs everything and prompts for setup
- [ ] Fresh Ubuntu 22.04 box: same
- [ ] Re-running the installer on an already-installed system is a no-op (except setup prompt)
- [ ] Missing `python3-venv` is detected and install prompted
- [ ] PEP 668 is handled automatically (venv, no `--break-system-packages`)
- [ ] `llm-mem` command is on PATH after install (without manually activating venv)
- [ ] Setup prompt at the end works and runs `llm-mem setup` correctly
- [ ] All existing tests pass

## Suggested tests

- Manual test on Ubuntu 24.04 (clean Docker container or VM)
- Manual test on Ubuntu 22.04
- Re-run test: install twice, confirm no errors or duplicates
- Test with `python3-venv` missing: confirm it prompts and recovers
