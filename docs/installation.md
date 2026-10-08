# Installation Guide

total-agent-memory runs on macOS, Linux and Windows, including WSL2. Install it
from a package (pip, pipx, uvx, npx, Homebrew, Docker, or an agent plugin), or
from a source checkout with an installer that also wires IDE hooks and
background services (LaunchAgents, systemd `--user` or Windows Task Scheduler).

> **Legacy paths.** The per-platform sections from [Platform matrix](#platform-matrix)
> onward were written for v8 and use the old layout: clone directory
> `~/claude-memory-server` and data directory `~/.claude-memory`. Current
> versions use `~/total-agent-memory` and `~/.tam`, and migrate a legacy data
> directory on first run.

---

## Table of contents

- [Install channels](#install-channels)
- [From a source checkout](#from-a-source-checkout)
- [Platform matrix](#platform-matrix)
- [Prerequisites](#prerequisites)
- [macOS (10.15+, Apple Silicon or Intel)](#macos-1015-apple-silicon-or-intel)
- [Linux (Ubuntu 22.04+, Debian 12+, Fedora 38+)](#linux-ubuntu-2204-debian-12-fedora-38)
- [Windows 10/11 native (without WSL)](#windows-1011-native-without-wsl)
- [WSL2 (Windows 11 + Ubuntu/Debian inside WSL)](#wsl2-windows-11--ubuntudebian-inside-wsl)
- [IDE coverage matrix](#ide-coverage-matrix)
- [Background services comparison](#background-services-comparison)
- [Post-install verification](#post-install-verification)
- [Uninstall](#uninstall)
- [Troubleshooting](#troubleshooting)

---

## Install channels

These are the published distribution channels. Python 3.11 or newer is required (3.11, 3.12 and 3.13 are tested in CI). For a server shared by several people, see [team-server.md](team-server.md).

| Channel | Command | What it does |
|---|---|---|
| **pip** | `pip install total-agent-memory` | Installs the package into the current Python environment with the same entry points as pipx. Use a virtual environment. |
| **npx** (Node) | `npx -y total-agent-memory connect claude-code` | Zero-install. Bootstraps a Python venv in `~/.tam/.venv` via uv (or python3 fallback), pulls the PyPI server, wires the MCP entry into your IDE. Replace `claude-code` with `codex` / `cursor` / `cline` / `continue` / `aider` / `windsurf` / `gemini-cli` / `opencode`. |
| **uvx** (Python via uv) | `uvx total-agent-memory` | One-off run with no install. Best for trying without commitment. |
| **pipx** (Python isolated) | `pipx install total-agent-memory` | Installs the `total-agent-memory`, `tam`, `tam-lookup`, `lookup-memory` binaries on PATH in an isolated venv. |
| **brew** (macOS / Linuxbrew) | `brew install vbcherepanov/tap/total-memory` | Bottle-style install with `tam` and legacy `claude-total-memory` symlinks. |
| **Docker** (multi-arch) | `docker run -p 3737:3737 -p 37737:37737 -v ~/.tam:/data ghcr.io/vbcherepanov/total-agent-memory:14.8.0` | Containerized (linux/amd64 + linux/arm64). MCP over HTTP on `:3737/mcp`, dashboard on `:37737`. |
| **Claude Code plugin** | `/plugin marketplace add vbcherepanov/total-agent-memory`<br>`/plugin install total-agent-memory@vbcherepanov` | Installs the MCP server, the `memory-protocol` skill and all seven capture hooks in one step, from inside Claude Code. The bootstrap reuses an existing install if it finds one, so nothing is downloaded twice. |
| **Claude plugin (directory edition)** | `/plugin marketplace add vbcherepanov/total-agent-memory-plugin`<br>`/plugin install total-agent-memory@vbcherepanov` | A small separate repository, [total-agent-memory-plugin](https://github.com/vbcherepanov/total-agent-memory-plugin), for Claude Code, Cowork and Codex (see the next row). Runs the pinned server with `uvx total-agent-memory==14.8.0` and adds the `memory-protocol` skill; no hooks. Needs [uv](https://docs.astral.sh/uv/). Use either this or the plugin above, not both. |
| **Codex plugin** | `codex plugin marketplace add vbcherepanov/total-agent-memory-plugin`<br>`codex plugin add total-agent-memory@vbcherepanov` | The same plugin repository for Codex CLI: the pinned server and the `memory-protocol` skill. Data goes to `~/.tam/`, shared with Claude Code. Needs [uv](https://docs.astral.sh/uv/). |
| **Manual clone** | `git clone https://github.com/vbcherepanov/total-agent-memory ~/total-agent-memory && cd ~/total-agent-memory && ./install.sh --ide claude-code` | Full control. Lets you hack on the server, run benchmarks, and pick which background services to enable. Detailed walkthrough below. |

All channels land at the same MCP server. The `npx` and `./install.sh` paths
additionally configure IDE-specific MCP entries and hooks. Other channels start
the server bare — you wire the IDE afterwards (see [IDE coverage matrix](#ide-coverage-matrix)).

### First run: `tam setup`

After a pip, pipx, uvx, brew or checkout install, run `tam setup`. A plain `tam` typed at a terminal starts the same wizard the first time. It never starts in MCP stdio sessions, in pipes, or in CI. The wizard asks one question first: **Just me** (personal memory on this machine) or **Company server** (shared memory for teams).

* **Just me:** detects Claude Code, Claude Desktop, Codex, Cursor, Windsurf, Gemini CLI, Cline and OpenCode and registers the server with the ones you pick. Then it asks for the language/embedding preset and an optional LLM provider (keys are typed hidden). From a checkout it also offers the hooks and skills. At the end it starts the server once on a throwaway directory to check it.
* **Company server:** sets the data directory, address, public URL and how the server runs (service unit, Docker Compose or `tam-team serve`). Then it creates the first superadmin, departments and the encrypted provider settings, and prints the admin's invite code once.

Change anything later with `tam setup --reconfigure`. Installers and containers use `tam setup --non-interactive ...` with flags. A team server started without an administrator prints a one-time setup code, and `/dashboard/` then shows the same setup as a web wizard. Details: [docs/SETUP_WIZARD.md](SETUP_WIZARD.md).

### Optional reranker

The reranker is an extra, not a dependency. A base install is 97 packages
and ~113 MB of wheels: fastembed runs the embeddings through ONNX and no torch
is resolved anywhere. The CrossEncoder / BGE reranker needs the torch stack,
which on Linux drags in the whole `nvidia-cu*` set — 147 packages and ~3.1 GB —
so it ships separately, and the default `MEMORY_MODE=fast` does not use it. Turn
it on with `MEMORY_MODE=deep` (or `MEMORY_RERANK_ENABLED=true`) and install it:

```bash
pip install "total-agent-memory[rerank]"      # pip / uvx / pipx
pip install -r requirements-rerank.txt        # clone / Docker
```

### Upgrading from v11.x

Whatever channel you pick will auto-migrate
`~/.claude-memory/` → `~/.tam/` on first run and keep a symlink for backward
compat. No manual data move required.

---

## From a source checkout

The installers configure IDE entries, hooks and background services in one step. Same 77 tools and dashboard on every path.

### Path A — native (macOS / Linux / WSL2)

```bash
git clone https://github.com/vbcherepanov/total-agent-memory.git ~/total-agent-memory
cd ~/total-agent-memory
bash install.sh --ide claude-code   # or: cursor | gemini-cli | opencode | codex
```

The installer:

1. Clones + creates `~/total-agent-memory/.venv/`
2. Installs deps from `requirements.txt` and `requirements-dev.txt`
3. Pre-downloads the FastEmbed multilingual MiniLM model
4. Registers the MCP server via `claude mcp add-json memory ...` (stored in `~/.claude.json`, the canonical store Claude Code actually reads)
5. Copies **all hooks** (`session-*`, `user-prompt-submit.sh`, `post-tool-use.sh`, `pre-edit.sh`, `on-bash-error.sh`, etc.) into `~/.claude/hooks/` and registers them in `~/.claude/settings.json`
6. Grants `permissions.allow` for 20+ `mcp__memory__*` tools so hook-driven calls don't prompt for confirmation
7. Installs **background services** for the current OS:
   - **macOS** — 4 LaunchAgents (`reflection`, `orphan-backfill`, `check-updates`, `dashboard`) under `~/Library/LaunchAgents/`
   - **Linux / WSL2** — 7 systemd `--user` units (`*.service`, `*.timer`, `*.path`) under `~/.config/systemd/user/`; gracefully degrades if `systemd --user` is unavailable (WSL without `/etc/wsl.conf`)
8. Applies all migrations to a fresh `memory.db`
9. Starts the dashboard at `http://127.0.0.1:37737`

Restart Claude Code → `/mcp` → `memory` should show **Connected** with 77 tools.

### Path A — native (Windows 10/11)

```powershell
git clone https://github.com/vbcherepanov/total-agent-memory.git $HOME\total-agent-memory
cd $HOME\total-agent-memory
powershell -ExecutionPolicy Bypass -File install.ps1 -Ide claude-code
```

Same 9 steps as Unix, but:

- MCP config path is `%USERPROFILE%\.claude\settings.json` (or `.cursor\mcp.json`, etc.)
- Hooks copied to `%USERPROFILE%\.claude\hooks\` — `.ps1` versions (auto-capture, memory-trigger, user-prompt-submit, post-tool-use, pre-edit, on-bash-error, session-start/end, on-stop, codex-notify)
- Background services via **Task Scheduler**:
  - `total-agent-memory-reflection` — every 5 min (no native FileSystemWatcher equivalent)
  - `total-agent-memory-orphan-backfill` — daily 00:00 + 6h repetition
  - `total-agent-memory-check-updates` — weekly Mon 09:00
  - `TotalAgentMemoryDashboard` — AtLogon

### Path B — Docker (everything containerized, cross-platform)

```bash
git clone https://github.com/vbcherepanov/total-agent-memory.git
cd total-agent-memory
bash install-docker.sh --with-compose
```

Brings up 5 services:

| Service | Role | Exposed |
|---|---|---|
| `mcp` | MCP server (HTTP transport) | `127.0.0.1:3737/mcp` |
| `dashboard` | Web UI | `127.0.0.1:37737` |
| `ollama` | Local LLM runtime | `127.0.0.1:11434` |
| `reflection` | File-watch queue drainer | internal |
| `scheduler` | Ofelia cron (backfill + update check) | internal |

First run pulls `qwen2.5-coder:7b` (~4.7 GB) + `nomic-embed-text` (~275 MB) — 5–10 min cold start.

**GPU note:** Docker Desktop on macOS doesn't forward Metal. Native install is faster on Mac. On Linux with NVIDIA Container Toolkit, uncomment the `deploy.resources.reservations.devices` block in `docker-compose.yml`.

### Verify (both paths)

```
memory_save(content="install works", type="fact")
memory_stats()
```

Open <http://127.0.0.1:37737/> — dashboard, knowledge graph, token savings.

### Installer IDE matrix

The same MCP server, same tools, same protocol — different installation
locations and hook wiring per IDE. The installer (`install.sh --ide <name>`)
automates all of it.

| IDE | Skill API | Hook API | Sub-agents | Install command |
|---|:-:|:-:|:-:|---|
| Claude Code | ✅ | ✅ full | ✅ | `./install.sh --ide claude-code` |
| Codex CLI | ✅ | ✅ | ❌ | `./install.sh --ide codex` |
| Cursor | rules-pane | ❌ | composer | `./install.sh --ide cursor` |
| Cline (VS Code) | `.clinerules/` | ❌ | ❌ | `./install.sh --ide cline` |
| Continue | rules file | ❌ | ❌ | `./install.sh --ide continue` |
| Aider | `.aider.conf.yml` read | ❌ ¹ | ❌ | `./install.sh --ide aider` |
| Windsurf | `.windsurfrules` | ❌ | cascade | `./install.sh --ide windsurf` |
| Gemini CLI | `.gemini/rules/` | ⚠️ partial | ❌ | `./install.sh --ide gemini-cli` |
| OpenCode | `.opencode/skills/` | ✅ | custom | `./install.sh --ide opencode` |

¹ Aider has no MCP yet — the bridge is via `lookup_memory.sh` /
`save_memory.sh` shell scripts.

Full per-IDE setup, manual fallbacks, and template snippets:
[`skills/memory-protocol/references/ide-setup.md`](../skills/memory-protocol/references/ide-setup.md).

---

## Platform matrix

| OS | Command | Background services | MCP registration target |
|---|---|---|---|
| macOS | `./install.sh --ide claude-code` | LaunchAgents (`launchctl`) | `~/.claude.json` + `~/.claude/settings.json` |
| Linux | `./install.sh --ide claude-code` | systemd `--user` | `~/.claude.json` + `~/.claude/settings.json` |
| WSL2 (systemd on) | `./install.sh --ide claude-code` | systemd `--user` | depends on where Claude Code runs — see [WSL2](#wsl2-windows-11--ubuntudebian-inside-wsl) |
| WSL2 (systemd off) | `./install.sh --ide claude-code` | shell-loop / cron fallback | same as above |
| Windows native | `.\install.ps1 -Ide claude-code` | Task Scheduler | `%USERPROFILE%\.claude\settings.json` |

All flavors share the same MCP tool surface, the same `~/.claude-memory/memory.db`
schema, and the same dashboard (`http://127.0.0.1:37737`). The only differences
are *where* background jobs live and *which* path Claude Code reads the MCP
config from.

---

## Prerequisites

- **Python 3.11+** (3.13 recommended)
- **Git** for cloning the repo
- **~2 GB disk** (code + venv + MiniLM model + DB)
- **Claude Code** (or Codex CLI / Cursor / Gemini CLI / OpenCode — any MCP client)
- Optional but strongly recommended: **[Ollama](https://ollama.com)** running
  locally (`ollama serve`) with `qwen2.5-coder:7b` + `nomic-embed-text` pulled

Clone the repo first (same on every platform):

```bash
git clone https://github.com/vbcherepanov/total-agent-memory.git ~/claude-memory-server
cd ~/claude-memory-server
```

On Windows native the equivalent is:

```powershell
git clone https://github.com/vbcherepanov/total-agent-memory.git $env:USERPROFILE\claude-memory-server
cd $env:USERPROFILE\claude-memory-server
```

---

## macOS (10.15+, Apple Silicon or Intel)

```bash
./install.sh --ide claude-code
```

What happens:

1. Creates `~/claude-memory-server/.venv/` and installs `requirements.txt`
   + `requirements-dev.txt`.
2. Pre-downloads the FastEmbed multilingual MiniLM model (~90 MB, one-time).
3. Registers the MCP server via `claude mcp add-json memory ...` — stored in
   `~/.claude.json`, which is the canonical store Claude Code reads.
4. Writes hook entries into `~/.claude/settings.json` and drops the helper
   scripts into `~/.claude/hooks/`.
5. Substitutes `__HOME__` in the three LaunchAgent templates under
   `launchagents/` and installs them to `~/Library/LaunchAgents/`, then
   `launchctl bootstrap`s each one:
   - `com.claude.memory.reflection.plist` — `WatchPaths`-triggered drainer
     of `triple_extraction_queue` / `deep_enrichment_queue` /
     `representations_queue`.
   - `com.claude.memory.orphan-backfill.plist` — runs 4× daily
     (`StartCalendarInterval` at 00:00, 06:00, 12:00, 18:00).
   - `com.claude.memory.check-updates.plist` — Monday 09:00 weekly check.
6. Installs the dashboard LaunchAgent
   (`com.claude-total-memory.dashboard.plist`) with `KeepAlive` and
   `RunAtLoad=true` so the dashboard survives reboots.
7. Applies migrations to a fresh `~/.claude-memory/memory.db`.

Verify:

```bash
launchctl list | grep claude.memory
curl -sI http://127.0.0.1:37737 | head -1   # HTTP/1.1 200 OK
```

Restart Claude Code → `/mcp` → `memory` should show **Connected**.

---

## Linux (Ubuntu 22.04+, Debian 12+, Fedora 38+)

```bash
./install.sh --ide claude-code
```

What happens (differences from macOS):

1. Same Python venv + MCP registration flow.
2. Instead of LaunchAgents, the installer renders the systemd unit templates
   from `systemd/` into `~/.config/systemd/user/`:
   - `claude-memory-reflection.path` — `PathModified=%h/.claude-memory/.reflect-pending`
     trigger.
   - `claude-memory-reflection.service` — `Type=oneshot`, runs the drainer.
   - `claude-total-memory-dashboard.service` — `Type=simple`,
     `Restart=on-failure`.
3. `systemctl --user daemon-reload` + `systemctl --user enable --now` on each
   unit.
4. Uses **XDG paths**: logs land in `~/.claude-memory/logs/`, config in
   `~/.config/systemd/user/`.

### No systemd? (containers, minimal images, WSL1)

If `systemctl --user show-environment` fails at install time, the installer
falls back to a shell-loop drop-in (`~/.claude-memory/dashboard-autostart.sh`)
registered in `~/.profile`. Reflection then runs on a loose poll instead of
inotify. This is automatic — no flag needed — but you lose real-time
`WatchPaths`-equivalent behavior. To enable systemd properly on Ubuntu, make
sure you booted under `systemd` (default on Ubuntu 22.04+ server/desktop).

Verify:

```bash
systemctl --user status claude-memory-reflection.path
systemctl --user status claude-total-memory-dashboard
curl -sI http://127.0.0.1:37737 | head -1
```

---

## Windows 10/11 native (without WSL)

PowerShell 5.1+ required (ships with Windows 10/11). Run from an **elevated**
shell if you want the dashboard Scheduled Task to register cleanly:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Ide claude-code
```

What happens:

1. Creates `.venv\` via `python -m venv`, activates with
   `.\.venv\Scripts\python.exe`.
2. Installs deps (`pip install -r requirements.txt -r requirements-dev.txt`).
3. Writes MCP config into `%USERPROFILE%\.claude\settings.json` under
   `mcpServers.memory`. The `command` is the venv Python path with forward
   slashes normalized by the installer.
4. Copies hook `.ps1` shims into `%USERPROFILE%\.claude\hooks\`.
5. Registers a Windows Scheduled Task `ClaudeTotalMemoryDashboard`:
   - Trigger: `AtLogon`
   - Action: `.\.venv\Scripts\python.exe src\dashboard.py`
   - Settings: `AllowStartIfOnBatteries`, `RestartCount=3`
6. Uses `%USERPROFILE%\.claude-memory\` as the DB/state dir.

### Known Windows-native quirks

- **File-watch granularity.** The reflection trigger uses a Scheduled-Task
  `FileSystemWatcher` shim rather than `WatchPaths` / `systemd.path`. Debounce
  is the same (5 s) but NTFS file-change latency is a touch higher than
  macOS/Linux.
- **Long paths.** Enable `LongPathsEnabled` in the registry if your
  `%USERPROFILE%` is deep — some `site-packages` wheels ship paths >260 chars.
- **Antivirus.** Some AV vendors flag the FastEmbed model download. Allow
  `%USERPROFILE%\.cache\fastembed` if you see quarantined artifacts.

---

## WSL2 (Windows 11 + Ubuntu/Debian inside WSL)

This is the most nuanced configuration because there are **two possible
places Claude Code can run from**, and the MCP server must be reachable from
whichever one you use.

### Scenario A — Claude Code runs on Windows (most common)

Claude Code is installed via its Windows installer and shows up in the
Windows Start menu. The MCP server lives inside WSL2 at
`\\wsl$\Ubuntu\home\<user>\claude-memory-server`.

MCP registration must point to the **Linux** Python binary, but use `wsl` as
the command from the Windows host's perspective:

```json
{
  "mcpServers": {
    "memory": {
      "command": "wsl",
      "args": [
        "-e",
        "/home/USERNAME/claude-memory-server/.venv/bin/python",
        "/home/USERNAME/claude-memory-server/src/server.py"
      ],
      "env": {
        "CLAUDE_MEMORY_DIR": "/home/USERNAME/.claude-memory"
      }
    }
  }
}
```

A ready-to-edit copy lives at
[`examples/settings/claude-code-wsl.json`](../examples/settings/claude-code-wsl.json).
Replace `USERNAME` with your WSL username (check with `wsl -- whoami` from
PowerShell) and merge the `mcpServers.memory` block into
`%USERPROFILE%\.claude\settings.json`.

### Scenario B — Claude Code runs inside WSL (VS Code Remote-WSL)

If you launch Claude Code from inside the WSL shell (e.g. via the VS Code
Remote-WSL extension that injects its CLI into the Linux environment), the
configuration is identical to the [Linux](#linux-ubuntu-2204-debian-12-fedora-38)
install — `install.sh` writes the MCP command as the native Linux Python
path, no `wsl` prefix needed.

**Rule of thumb:** whichever side runs the Claude Code binary is the side
that must be able to resolve the MCP `command`. If Claude Code runs on
Windows, MCP must be callable from Windows (so prefix with `wsl -e`). If
Claude Code runs inside WSL, MCP is local.

### Enabling systemd inside WSL2

`install.sh` detects WSL2 via `grep -qi microsoft /proc/version` and/or the
`WSL_DISTRO_NAME` env var. It then tries `systemctl --user
show-environment`. If WSL was booted without systemd, that check fails and
the installer falls back to the shell-loop autostart path
(`~/.claude-memory/dashboard-autostart.sh` wired through `~/.profile`).

To get real systemd `--user` support inside WSL2, add or edit
`/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then from PowerShell:

```powershell
wsl --shutdown
wsl
```

Re-run `./install.sh --ide claude-code` and the installer will pick up
systemd and install the `.path` + `.service` units like on native Linux.

### WSL2 gotchas

- **Clock drift.** WSL2 virtual-time can drift after a Windows suspend/resume;
  if you see reflection timestamps jumping, run `sudo hwclock -s` inside WSL.
- **Port forwarding.** The dashboard binds to `127.0.0.1:37737` inside WSL;
  Windows auto-forwards localhost ports for WSL2 since Windows 11 22H2, so
  `http://localhost:37737` in a Windows browser works out of the box.
- **Filesystem cross-access.** Do **not** put `~/.claude-memory/` on
  `/mnt/c/...` — SQLite performance across the 9P bridge is catastrophic.
  Keep the DB in the Linux native filesystem (default).
- **Ollama.** Install Ollama either fully inside WSL (`curl -fsSL
  https://ollama.com/install.sh | sh`) or use Windows Ollama and point
  `OLLAMA_HOST=http://host.docker.internal:11434` in the MCP env.

---

## IDE coverage matrix

| IDE | macOS | Linux | WSL2 | Windows native |
|---|:---:|:---:|:---:|:---:|
| Claude Code | ✅ `~/.claude.json` (MCP) + `~/.claude/settings.json` (hooks) | ✅ | ✅\* | ✅ `%USERPROFILE%\.claude.json` |
| Cursor | ✅ `~/.cursor/mcp.json` | ✅ | ✅\* | ✅ `%USERPROFILE%\.cursor\mcp.json` |
| Gemini CLI | ✅ `~/.gemini/settings.json` | ✅ | ✅\* | ✅ |
| OpenCode | ✅ `~/.config/opencode/opencode.json` | ✅ | ✅\* | ✅ |
| Claude Desktop | ✅ `~/Library/Application Support/Claude/claude_desktop_config.json` | ✅ `~/.config/Claude/…` | ✅\* | ✅ `%APPDATA%\Claude\…` |
| Cline | ✅ VS Code global storage `saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | ✅ | ✅\* | ✅ |
| Continue | ✅ `~/.continue/mcpServers/memory.yaml` | ✅ | ✅\* | ✅ |
| Codex CLI | ✅ `~/.codex/config.toml` | ✅ | ✅\* | ✅ |

\*WSL2: if the IDE runs on the Windows host, wrap the MCP `command` with
`wsl -e` and use Linux paths in `args` (see Scenario A above). If the IDE
runs inside WSL, use native Linux paths exactly like the Linux row.

Switch IDEs by passing `--ide <name>` to `install.sh` / `install.ps1`:

```bash
./install.sh --ide cursor        # or: gemini-cli / opencode / codex
```

Running the installer twice with different `--ide` values is safe — each
target IDE has its own config file, and the shared `memory.db` and venv are
reused.

---

## Background services comparison

The same three jobs run everywhere, just under different orchestrators:

| Task | macOS (LaunchAgent) | Linux & WSL2 (systemd `--user`) | Windows native (Task Scheduler) |
|---|---|---|---|
| Reflection on save | `com.claude.memory.reflection` + `WatchPaths` on `.reflect-pending` | `claude-memory-reflection.path` + `claude-memory-reflection.service` | FileSystemWatcher-backed Scheduled Task |
| Orphan backfill, 4× daily | `com.claude.memory.orphan-backfill` + `StartCalendarInterval` @ 00/06/12/18 | `claude-memory-orphan-backfill.timer` + `OnCalendar=*-*-* 00,06,12,18:00:00` | Scheduled Task with four `DailyTrigger`s |
| Update check, Monday 09:00 | `com.claude.memory.check-updates` weekly plist | `claude-memory-check-updates.timer` with `OnCalendar=Mon 09:00` | Scheduled Task `WeeklyTrigger -DaysOfWeek Monday -At 09:00` |
| Dashboard (keepalive) | `com.claude-total-memory.dashboard` `KeepAlive=true` | `claude-total-memory-dashboard.service` `Restart=on-failure` | Scheduled Task `AtLogon` + `RestartCount=3` |

State locations:

| Artifact | macOS | Linux / WSL2 | Windows native |
|---|---|---|---|
| DB | `~/.claude-memory/memory.db` | `~/.claude-memory/memory.db` | `%USERPROFILE%\.claude-memory\memory.db` |
| Logs | `/tmp/claude-memory-*.log` + `~/.claude-memory/logs/` | `~/.claude-memory/logs/` + `journalctl --user` | `%USERPROFILE%\.claude-memory\logs\` |
| Service manifests | `~/Library/LaunchAgents/` | `~/.config/systemd/user/` | Task Scheduler (`\memory*`) |

---

## Post-install verification

The quickest way to confirm everything is wired up is the diagnostic script:

```bash
# macOS / Linux / WSL2
bash scripts/diagnose.sh

# Windows native
powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1
```

Expected output includes lines like:

```
  OK OS: Darwin 25.4.0
  OK Python 3.13 venv present
  OK MCP server module importable
  OK LaunchAgents loaded: 3/3
  OK Dashboard HTTP 200
  OK Ollama detected: qwen2.5-coder:7b
  OK DB migrations at head
```

Exit code `0` = healthy; `1` = one or more checks failed (details in the
report).

You can also run the smoke test in any MCP client:

```
memory_save(content="install works", type="fact")
memory_stats()
```

---

## Uninstall

- **macOS / Linux / WSL2:** `./install.sh --uninstall`
- **Windows native:** `.\install.ps1 -Uninstall`

Both preserve `~/.claude-memory/memory.db` (or
`%USERPROFILE%\.claude-memory\memory.db`) by default. Pass `--purge` / `-Purge`
to drop the database as well.

What gets removed:

- Registered MCP server entry in the IDE's settings file.
- Hook scripts and their registrations in the IDE settings.
- LaunchAgents / systemd units / Scheduled Tasks and their log paths in
  `/tmp/` (macOS only — Linux keeps them under `~/.claude-memory/logs/`).
- The `.venv/` inside the clone (the clone itself is left alone — delete it
  manually if you want).

---

## Troubleshooting

### "memory" MCP server shows Disconnected

1. Run the diagnostic script and look for failing checks.
2. On macOS: `launchctl list | grep claude.memory` — should list 3 loaded
   agents. If empty, re-run `./install.sh`.
3. On Linux / WSL2: `systemctl --user status claude-memory-reflection.path`
   — look for `Active: active (waiting)`.
4. On Windows: `Get-ScheduledTask -TaskPath "\" -TaskName "ClaudeTotal*"`.
5. Tail the dashboard log: `tail -f ~/.claude-memory/logs/dashboard.log`
   (or the Windows equivalent).

### Dashboard port 37737 is busy

The installer honors `DASHBOARD_PORT`:

```bash
DASHBOARD_PORT=37800 ./install.sh --ide claude-code
```

Re-run — the old service is replaced and the new port is written into the
unit file or Scheduled Task.

### Am I really on WSL2?

```bash
grep -qi microsoft /proc/version && echo "WSL detected" || echo "native Linux"
echo "$WSL_DISTRO_NAME"        # populated on WSL only
wsl.exe --list --verbose       # from PowerShell, shows VERSION column
```

If `VERSION` column shows `1`, you are on WSL1 — systemd `--user` is
unavailable and reflection falls back to the shell-loop path. Migrate with
`wsl --set-version <distro> 2` from PowerShell.

### systemd `--user` is available but units don't start

```bash
systemctl --user daemon-reload
systemctl --user --no-pager status claude-memory-reflection.service
journalctl --user -u claude-memory-reflection.service -n 100 --no-pager
```

Common causes: Python binary path moved (re-run the installer), or
`~/.claude-memory/.reflect-pending` was deleted and `PathExists` never
re-armed — `touch ~/.claude-memory/.reflect-pending` to nudge it.

### Ollama integration

`memory_recall` / reflection are fully functional without Ollama (FastEmbed
covers retrieval), but richer triples / deep enrichment need it:

```bash
curl http://127.0.0.1:11434/api/tags   # should return JSON with models list
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```

On WSL2 with Windows-host Ollama, export
`OLLAMA_HOST=http://host.docker.internal:11434` in the MCP env block.

---

See also:

- [examples/README.md](../examples/README.md) — hook and rules wiring
- [systemd/README.md](../systemd/README.md) — Linux unit details
- [docs/MANUAL_QA.md](MANUAL_QA.md) — end-to-end smoke checklist
