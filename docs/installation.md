# Installation guide

total-agent-memory runs on macOS, Linux and Windows, including WSL2. Python 3.11, 3.12 or 3.13 is required (all three are tested in CI).

## Quick start

```bash
pipx install total-agent-memory     # or: pip install total-agent-memory (in a virtual environment)
tam setup
```

`tam setup` asks whether the memory is for you alone or for a company server, finds your
agent clients (Claude Code, Claude Desktop, Codex, Cursor, Windsurf, Gemini CLI, Cline,
OpenCode), registers the server with the ones you pick, and checks that it starts. Restart
the client afterwards; in Claude Code, `/mcp` should show `memory` as **Connected** with 79 tools.

The first start downloads two local models, about 370 MB in total: the text embedder
(about 240 MB) and the cross-encoder that re-ranks results (about 130 MB, ARM64 build;
the x86-64 size was not measured). After that the server works offline.
`TAM_MODEL_CACHE=~/.tam/models` keeps the models out of the system temp directory, which
macOS clears from time to time. Nothing else is required: Ollama and cloud LLMs are optional.

Check it with `memory_save(content="install works", type="fact")` and `memory_stats()` in
any MCP client, or run the [diagnostic script](#verify-the-installation).

## Where to go next

| You want to | Go to |
|---|---|
| Pick a different install channel (uvx, npx, Homebrew, Docker, plugin) | [Install channels](#install-channels) |
| Understand what `tam setup` asks | [First run: `tam setup`](#first-run-tam-setup) |
| Install the optional extras (OCR, media, PostgreSQL, legacy reranker) | [Optional extras](#optional-extras) |
| Install from a clone with hooks and background services | [From a source checkout](#from-a-source-checkout) |
| Use Docker | [Docker](#docker) |
| Use the Claude Code or Codex plugin | [Claude Code and Codex plugins](#claude-code-and-codex-plugins) |
| See what each OS does | [Platform matrix](#platform-matrix): [macOS](#macos), [Linux](#linux), [Windows](#windows-1011-native-without-wsl), [WSL2](#wsl2) |
| Find where each client keeps its MCP config | [Client configuration files](#client-configuration-files) |
| Run a server for several people | [team-server.md](team-server.md) |
| Fix a problem | [Troubleshooting](#troubleshooting) |
| Remove it | [Uninstall](#uninstall) |

## Install channels

All channels land at the same MCP server with the same 79 tools. `npx`, the plugins and
the checkout installers also wire the client for you; with the others, `tam setup`
does it.

| Channel | Command | Notes |
|---|---|---|
| **pip** | `pip install total-agent-memory` | Use a virtual environment. |
| **pipx** | `pipx install total-agent-memory` | Isolated environment; puts `total-agent-memory`, `tam`, `tam-lookup` and `lookup-memory` on PATH. |
| **uvx** | `uvx total-agent-memory` | One-off run with no install. |
| **npx** | `npx -y total-agent-memory connect claude-code` | Creates a Python environment in `~/.tam/.venv` (with uv, or python3 as the fallback), installs the PyPI server and writes the MCP entry. Replace `claude-code` with `codex`, `cursor`, `cline`, `continue`, `aider`, `windsurf`, `gemini-cli` or `opencode`. |
| **Homebrew** (macOS, Linuxbrew) | `brew install vbcherepanov/tap/total-memory` | Builds from the PyPI source tarball in a Homebrew virtualenv (needs `cmake` and `rust` at build time); installs `tam` and the legacy `claude-total-memory`. |
| **Docker** | see [Docker](#docker) | linux/amd64 and linux/arm64. |
| **Claude Code plugin** | `/plugin marketplace add vbcherepanov/total-agent-memory` then `/plugin install total-agent-memory@vbcherepanov` | See [plugins](#claude-code-and-codex-plugins). |
| **Source checkout** | `git clone https://github.com/vbcherepanov/total-agent-memory ~/total-agent-memory && cd ~/total-agent-memory && ./install.sh --ide claude-code` | Hooks, background services and benchmarks; see [From a source checkout](#from-a-source-checkout). |

### First run: `tam setup`

After a pip, pipx, uvx, Homebrew or checkout install, run `tam setup`. A plain `tam`
typed at a terminal starts the same wizard the first time; it never starts in MCP stdio
sessions, in pipes or in CI. The first question is **Just me** or **Company server**.

* **Just me:** detects the clients above and registers the server with the ones you pick, then asks for the language and embedding preset and an optional LLM provider (keys are typed hidden). From a checkout it also offers the hooks and skills. At the end it starts the server once on a throwaway directory to check it.
* **Company server:** sets the data directory, address, public URL and how the server runs (service unit, Docker Compose or `tam-team serve`), then creates the first superadmin, departments and the encrypted provider settings, and prints the admin's invite code once.

Change anything later with `tam setup --reconfigure`. Installers and containers use
`tam setup --non-interactive ...` with flags. A team server started without an
administrator prints a one-time setup code, and `/dashboard/` then shows the same setup as
a web wizard. Details: [SETUP_WIZARD.md](SETUP_WIZARD.md). `tam --help` and `tam --version`
print and exit without starting the server.

### Optional extras

A base install needs no extra: fastembed runs the embedder and the default cross-encoder
through ONNX, and no PyTorch is installed.

| Extra | Install | What it adds |
|---|---|---|
| `[rerank]` | `pip install "total-agent-memory[rerank]"` or `pip install -r requirements-rerank.txt` | The legacy sentence-transformers reranker. It pulls in PyTorch (on Linux the CUDA wheels too, about 3 GB) and is off by default; turn it on with `MEMORY_MODE=deep` or `MEMORY_RERANK_ENABLED=true`. It is **not** the cross-encoder that re-ranks by default; see [configuration](configuration.md#two-rerankers-two-switches). |
| `[ocr]` | `pip install "total-agent-memory[ocr]"` or `pip install -r requirements-ocr.txt` | Apple Vision OCR for images and PDFs in the file watcher (macOS only, through pyobjc). Without it images are stored without their text (pytesseract is used when installed). |
| `[media]` | `pip install "total-agent-memory[media]"` | Image and scanned-PDF OCR, audio and video transcription, RTF and XLS for `ingest_codebase`. Tesseract and its language data are system prerequisites. |
| `[postgres]` | `pip install "total-agent-memory[postgres]"` | PostgreSQL backend for the team server ([team-server.md](team-server.md)). |

`pymacos` is an optional alternative OCR backend that is not part of `[ocr]`; when it is
importable it is tried first, otherwise pyobjc is used. Install it yourself with
`pip install pymacos`. It is a new package with a single maintainer, so review it before
putting it into the process that serves your memory.

### Docker

```bash
docker run -p 127.0.0.1:3737:3737 -p 127.0.0.1:37737:37737 -v ~/.tam:/data ghcr.io/vbcherepanov/total-agent-memory:15.0.2
```

MCP over HTTP is on `:3737/mcp`, the dashboard on `:37737`. Always publish with
`127.0.0.1:` as shown; a bare `-p 3737:3737` exposes the port on every interface. If a port
is taken, for example by the dashboard of a native install, change the host side:
`-p 127.0.0.1:47737:37737`.

Inside the container both servers listen on `0.0.0.0`, so every request must carry the
access token from `<data dir>/state/http-token` (created with mode 0600 on first start;
override with `TAM_HTTP_TOKEN`, at least 32 characters):

- MCP and the API: `Authorization: Bearer <token>`.
- Dashboard: open `/login#token=<token>` once per browser. The fragment is never sent to the server; the page posts it, clears it from the address bar and sets an opaque session cookie that never contains the token. `POST /logout` ends the session.
- `/healthz` stays open.

Changing an endpoint setting (`MEMORY_LLM_API_BASE`, `MEMORY_EMBED_API_BASE`, `OLLAMA_URL`)
in the dashboard asks for an explicit confirmation, because it redirects the stored API key
and memory text.

For the full stack, clone the repository and run `bash install-docker.sh --with-compose`.
It builds `total-agent-memory:latest` locally and starts six services:

| Service | Role | Exposed |
|---|---|---|
| `mcp` | MCP server (HTTP transport) | `127.0.0.1:3737/mcp` |
| `dashboard` | Web UI | `127.0.0.1:37737` |
| `ollama` | Local LLM runtime | `127.0.0.1:11434` |
| `model-init` | Pulls the Ollama models once, then exits | internal |
| `reflection` | File-watch queue drainer | internal |
| `scheduler` | Ofelia cron (backfill and update check) | internal |

The first Compose start pulls `qwen2.5-coder:7b` (about 4.7 GB) and `nomic-embed-text`
(about 275 MB), 5 to 10 minutes cold. They serve the optional LLM stages (summaries,
contradiction checks, query rewriting); the one-line `docker run` needs neither them nor
Ollama. Docker Desktop on macOS does not forward Metal, so a native install is faster on a
Mac. On Linux with the NVIDIA Container Toolkit, uncomment the
`deploy.resources.reservations.devices` block in `docker-compose.yml`.

### Claude Code and Codex plugins

| Plugin | Commands | What it does |
|---|---|---|
| **Claude Code plugin** (this repository) | `/plugin marketplace add vbcherepanov/total-agent-memory`, then `/plugin install total-agent-memory@vbcherepanov` | Installs the MCP server, the `memory-protocol` skill and all seven capture hooks from inside Claude Code. The bootstrap reuses an existing install if it finds one. |
| **Claude plugin, directory edition** | `/plugin marketplace add vbcherepanov/total-agent-memory-plugin`, then `/plugin install total-agent-memory@vbcherepanov` | A small separate repository, [total-agent-memory-plugin](https://github.com/vbcherepanov/total-agent-memory-plugin), for Claude Code, Cowork and Codex. Runs a pinned server with `uvx total-agent-memory==14.8.0` and adds the `memory-protocol` skill; no hooks. Needs [uv](https://docs.astral.sh/uv/). Use either this plugin or the one above, not both. |
| **Codex plugin** | `codex plugin marketplace add vbcherepanov/total-agent-memory-plugin`, then `codex plugin add total-agent-memory@vbcherepanov` | The directory edition for Codex CLI. Data goes to `~/.tam/`, shared with Claude Code. Needs uv. |

With the plugin from this repository, the first start builds a virtual environment in
the memory directory (with `uv` when installed), so it takes longer; `uvx` and `npx` are
the fallback.

## From a source checkout

The installers configure the client entry, hooks and background services in one step. The
checkout is also what you need to run benchmarks or change the server. Requirements: Python
3.11 to 3.13 with the `venv` module (on Debian, Ubuntu and WSL: `sudo apt install python3-venv`),
Git, and an MCP client. About 370 MB of models download on first use; see
[Quick start](#quick-start).

**macOS, Linux, WSL2**

```bash
git clone https://github.com/vbcherepanov/total-agent-memory.git ~/total-agent-memory
cd ~/total-agent-memory
bash install.sh --ide claude-code   # or another client, see the table below
```

**Windows 10/11 (PowerShell 5.1 or newer)**

```powershell
git clone https://github.com/vbcherepanov/total-agent-memory.git $HOME\total-agent-memory
cd $HOME\total-agent-memory
powershell -ExecutionPolicy Bypass -File install.ps1 -Ide claude-code
```

The installer:

1. Creates `.venv` in the clone and installs `requirements.txt`.
2. Pre-downloads the FastEmbed multilingual MiniLM text embedder (about 240 MB). The cross-encoder downloads on first use.
3. Registers the MCP server with the client you chose (for Claude Code, in `~/.claude.json`, the file Claude Code reads).
4. For Claude Code only: copies the hooks into `~/.claude/hooks/`, registers them in `~/.claude/settings.json`, grants `permissions.allow` for the `mcp__memory__*` tools the hooks call, and installs the dashboard service.
5. Installs the background services for your OS (see [Platform matrix](#platform-matrix)).
6. Applies the migrations to a fresh `memory.db` in `~/.tam`.

For Claude Code, the dashboard is then at `http://127.0.0.1:37737`. Restart the client; `/mcp`
should show `memory` as **Connected**.

### What each client gets

The same server, tools and protocol everywhere; the installer (`install.sh --ide <name>`,
`install.ps1 -Ide <name>`) registers the server and installs the skill or rules file. It
installs the capture hooks, the dashboard service and the background jobs for Claude Code
only. The "Hooks installed" column says what the installer sets up, not what the client
could run. `claude-desktop` is also accepted as `--ide`.

| IDE | Skill API | Hooks installed | Sub-agents | Install command |
|---|:-:|:-:|:-:|---|
| Claude Code | ✅ | ✅ full | ✅ | `./install.sh --ide claude-code` |
| Codex CLI | ✅ | ❌ ² | ❌ | `./install.sh --ide codex` |
| Cursor | rules-pane | ❌ | composer | `./install.sh --ide cursor` |
| Cline (VS Code) | `.clinerules/` | ❌ | ❌ | `./install.sh --ide cline` |
| Continue | rules file | ❌ | ❌ | `./install.sh --ide continue` |
| Aider | `.aider.conf.yml` read | ❌ ¹ | ❌ | `./install.sh --ide aider` |
| Windsurf | `.windsurfrules` | ❌ | cascade | `./install.sh --ide windsurf` |
| Gemini CLI | `.gemini/rules/` | ❌ | ❌ | `./install.sh --ide gemini-cli` |
| OpenCode | `.opencode/skills/` | ❌ | custom | `./install.sh --ide opencode` |

¹ Aider has no MCP yet; the bridge is the `lookup_memory.sh` and `save_memory.sh` shell
scripts.

² Codex CLI gets the MCP server in `~/.codex/config.toml` and the skills in
`~/.codex/skills/` and `~/.agents/skills/`. No hooks are registered and
`~/.codex/AGENTS.md` is not touched. To capture finished Codex turns, add
`hooks/codex-notify.sh` as the `notify` program yourself, see
[configuration.md](configuration.md#ambient-memory-hooks).

Running the installer again with a different `--ide` is safe: each client has its own
config file, and the database and venv are reused. Per-client setup, manual fallbacks and
template snippets:
[`skills/memory-protocol/references/ide-setup.md`](../skills/memory-protocol/references/ide-setup.md).

## Platform matrix

| OS | Command | Background services | MCP registration target |
|---|---|---|---|
| macOS | `./install.sh --ide claude-code` | LaunchAgents (`launchctl`) | `~/.claude.json` + `~/.claude/settings.json` |
| Linux | `./install.sh --ide claude-code` | systemd `--user` | `~/.claude.json` + `~/.claude/settings.json` |
| WSL2 (systemd on) | `./install.sh --ide claude-code` | systemd `--user` | depends on where Claude Code runs, see [WSL2](#wsl2) |
| WSL2 (systemd off) | `./install.sh --ide claude-code` | shell-loop fallback | same as above |
| Windows native | `.\install.ps1 -Ide claude-code` | Task Scheduler | `%USERPROFILE%\.claude\settings.json` |

All of them share the same MCP tools, the same `memory.db` schema in `~/.tam` and the same
dashboard (`http://127.0.0.1:37737`). They differ in where the background jobs live and
which file the client reads its MCP config from.

The same four jobs run everywhere, under different orchestrators:

| Task | macOS (LaunchAgent) | Linux and WSL2 (systemd `--user`) | Windows native (Task Scheduler) |
|---|---|---|---|
| Reflection on save | `com.claude.memory.reflection`, triggered by `WatchPaths` on `.reflect-pending` | `claude-memory-reflection.path` + `.service` | `total-agent-memory-reflection`, every 5 minutes |
| Orphan backfill, 4 times a day | `com.claude.memory.orphan-backfill`, 00:00, 06:00, 12:00, 18:00 | `claude-memory-orphan-backfill.timer` | `total-agent-memory-orphan-backfill`, daily 00:00 repeating every 6 hours |
| Update check, Monday 09:00 | `com.claude.memory.check-updates` | `claude-memory-check-updates.timer` | `total-agent-memory-check-updates`, weekly |
| Dashboard (keep-alive) | `com.total-agent-memory.dashboard` (`KeepAlive`, `RunAtLoad`) | `claude-memory-dashboard.service` (`Restart=on-failure`) | `ClaudeTotalMemoryDashboard`, at logon, `RestartCount=3` |

Files: the database is `~/.tam/memory.db` (`%USERPROFILE%\.tam\memory.db` on Windows) and
logs are under `~/.tam/logs/`. Service definitions are in `~/Library/LaunchAgents/` on
macOS, `~/.config/systemd/user/` on Linux and WSL2, and Task Scheduler on Windows.

### macOS

macOS 10.15 or newer, Apple Silicon or Intel. `./install.sh --ide claude-code` renders the
templates in `launchagents/` (replacing `__HOME__`), copies them to
`~/Library/LaunchAgents/` and runs `launchctl bootstrap` on each.

```bash
launchctl list | grep claude.memory
curl -sI http://127.0.0.1:37737 | head -1   # HTTP/1.1 200 OK
```

### Linux

Ubuntu 22.04+, Debian 12+, Fedora 38+. The installer renders the unit templates in
`systemd/` into `~/.config/systemd/user/`, runs `systemctl --user daemon-reload` and
enables the reflection path, dashboard service and the two timers. Unit details:
[systemd/README.md](../systemd/README.md).

```bash
systemctl --user status claude-memory-reflection.path
systemctl --user status claude-memory-dashboard
curl -sI http://127.0.0.1:37737 | head -1
```

**No systemd** (containers, minimal images, WSL1): if `systemctl --user show-environment`
fails, the installer falls back to a shell-loop drop-in (`~/.tam/dashboard-autostart.sh`
registered in `~/.profile`). Reflection then runs on a loose poll instead of a file watch.
The fallback is automatic; to get real systemd, boot under it (the default on Ubuntu 22.04+).

### Windows 10/11 native (without WSL)

PowerShell 5.1 or newer. Run from an **elevated** shell if you want the dashboard Scheduled
Task to register cleanly. `install.ps1` creates `.venv`, installs `requirements.txt`,
writes the MCP entry under `mcpServers.memory` in `%USERPROFILE%\.claude\settings.json`
(the `command` is the venv Python with forward slashes), copies the `.ps1` hook shims to
`%USERPROFILE%\.claude\hooks\` and registers the four Scheduled Tasks above. The hooks are
the `.ps1` versions of auto-capture, memory-trigger, user-prompt-submit, post-tool-use,
pre-edit, on-bash-error, session-start, session-end and on-stop, plus `codex-notify`, an
optional Codex desktop reminder that is not registered and does not capture memory. The
dashboard task runs `.\.venv\Scripts\python.exe src\dashboard.py` with
`AllowStartIfOnBatteries`.

Quirks:

- **File-watch granularity.** The reflection trigger is a 5-minute Scheduled Task, not a file watch, so reflection is slower than on macOS and Linux.
- **Long paths.** Enable `LongPathsEnabled` in the registry if `%USERPROFILE%` is deep; some `site-packages` wheels ship paths over 260 characters.
- **Antivirus.** Some vendors flag the model download. Allow `%USERPROFILE%\.cache\fastembed` if you see quarantined files.

### WSL2

Windows 11 with Ubuntu or Debian inside WSL. There are two places Claude Code can run
from, and the MCP server must be reachable from the one you use.

**Scenario A: Claude Code runs on Windows** (most common). The server lives inside WSL2.
Register it with `wsl` as the command and the Linux Python as an argument:

```json
{
  "mcpServers": {
    "memory": {
      "command": "wsl",
      "args": [
        "-e",
        "/home/USERNAME/total-agent-memory/.venv/bin/python",
        "/home/USERNAME/total-agent-memory/src/server.py"
      ],
      "env": {
        "TAM_MEMORY_DIR": "/home/USERNAME/.tam"
      }
    }
  }
}
```

Replace `USERNAME` with your WSL username (`wsl -- whoami` in PowerShell) and merge the
block into `%USERPROFILE%\.claude\settings.json`. A ready-to-edit copy is
[`examples/settings/claude-code-wsl.json`](../examples/settings/claude-code-wsl.json);
it still uses the older clone name `claude-memory-server` and the legacy
`CLAUDE_MEMORY_DIR` variable, so adjust the paths to your clone.

**Scenario B: Claude Code runs inside WSL** (for example VS Code Remote-WSL). The setup is
identical to [Linux](#linux): `install.sh` writes the native Linux Python path and no `wsl`
prefix is needed.

Whichever side runs the Claude Code binary must be able to resolve the MCP `command`: from
Windows, prefix with `wsl -e`; inside WSL, the command is local.

**systemd inside WSL2.** `install.sh` detects WSL with `grep -qi microsoft /proc/version`
and tries `systemctl --user show-environment`. If WSL booted without systemd, it falls back
to the shell-loop autostart. For real systemd `--user` units, edit `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then run `wsl --shutdown` and `wsl` from PowerShell, and re-run
`./install.sh --ide claude-code`.

**Gotchas**

- **Clock drift.** WSL2 time can drift after a Windows suspend; if reflection timestamps jump, run `sudo hwclock -s` inside WSL.
- **Port forwarding.** The dashboard binds to `127.0.0.1:37737` inside WSL; since Windows 11 22H2 `http://localhost:37737` works in a Windows browser.
- **Filesystem.** Do not put `~/.tam/` on `/mnt/c/...`: SQLite across the 9P bridge is extremely slow. Keep the database on the Linux filesystem (the default).
- **Slow saves.** On a CPU-only WSL2 host with an LLM stage turned on, a save can take tens of seconds. Keep the default `MEMORY_MODE=fast` or use the [enrichment worker](configuration.md#background-enrichment-worker).
- **Ollama.** Install it inside WSL (`curl -fsSL https://ollama.com/install.sh | sh`), or use Windows Ollama and set `OLLAMA_URL` (for example `http://host.docker.internal:11434`) in the MCP env.

## Client configuration files

Where each client keeps the MCP entry (the installer and `tam setup` write it):

| Client | macOS | Linux | WSL2 | Windows native |
|---|---|---|---|---|
| Claude Code | `~/.claude.json` (MCP), `~/.claude/settings.json` (hooks) | same | ✅\* | `%USERPROFILE%\.claude.json` |
| Cursor | `~/.cursor/mcp.json` | same | ✅\* | `%USERPROFILE%\.cursor\mcp.json` |
| Gemini CLI | `~/.gemini/settings.json` | same | ✅\* | same under `%USERPROFILE%` |
| OpenCode | `~/.config/opencode/opencode.json` | same | ✅\* | same |
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` | `~/.config/Claude/...` | ✅\* | `%APPDATA%\Claude\...` |
| Cline | VS Code global storage `saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | same | ✅\* | same |
| Continue | `~/.continue/mcpServers/memory.yaml` | same | ✅\* | same |
| Codex CLI | `~/.codex/config.toml` | same | ✅\* | same |

\*WSL2: if the client runs on the Windows host, wrap the MCP `command` with `wsl -e` and
use Linux paths in `args` (Scenario A above); if it runs inside WSL, use the Linux row.

## Verify the installation

```bash
# macOS / Linux / WSL2
bash scripts/diagnose.sh

# Windows native
powershell -ExecutionPolicy Bypass -File scripts\diagnose.ps1
```

Expected lines look like this; exit code `0` means healthy, `1` means a check failed (details
in the report):

```
  OK OS: Darwin 25.4.0
  OK Python 3.13 venv present
  OK MCP server module importable
  OK LaunchAgents loaded: 3/3
  OK Dashboard HTTP 200
  OK Ollama detected: qwen2.5-coder:7b
  OK DB migrations at head
```

Right after installation, before the MCP server first starts, the database is reported as "not
created yet", a warning rather than a failure. The smoke test works in any MCP client:
`memory_save(content="install works", type="fact")`, then `memory_stats()`.

## Uninstall

- **macOS, Linux, WSL2:** `./install.sh --uninstall` removes the LaunchAgents or systemd units. It keeps the client's MCP entry and your data; to remove the entry, run `PYTHONPATH=<clone>/src python3 -m setup_wizard.register --unregister --client <ide>` or edit the client's config (`~/.claude.json` and the hooks in `~/.claude/settings.json` for Claude Code).
- **Windows native:** `.\install.ps1 -Uninstall` stops and removes the Scheduled Tasks and the client's MCP entry and hooks, and keeps the venv and the database.

Both keep the memory directory (`~/.tam`, containing `memory.db`). Delete it yourself to drop
your memories, and delete the clone to remove the venv.

## Troubleshooting

**The `memory` server shows Disconnected.**
Run the [diagnostic script](#verify-the-installation) and look for failing checks. On macOS,
`launchctl list | grep claude.memory` should list the agents; if it is empty, re-run
`./install.sh`. On Linux and WSL2, `systemctl --user status claude-memory-reflection.path`
should say `Active: active (waiting)`. On Windows, run
`Get-ScheduledTask -TaskName "total-agent-memory*"`. Logs are in `~/.tam/logs/`, for example `tail -f ~/.tam/logs/dashboard.log`.

**Dashboard port 37737 is busy.**
`DASHBOARD_PORT=37800 ./install.sh --ide claude-code` writes the new port into the unit file or
Scheduled Task and replaces the old service.

**Am I on WSL2?**

```bash
grep -qi microsoft /proc/version && echo "WSL detected" || echo "native Linux"
echo "$WSL_DISTRO_NAME"        # populated on WSL only
wsl.exe --list --verbose       # from PowerShell, shows the VERSION column
```

If `VERSION` is `1`, you are on WSL1: systemd `--user` is unavailable and reflection uses the
shell-loop path. Migrate with `wsl --set-version <distro> 2` from PowerShell.

**systemd `--user` is available but units do not start.**

```bash
systemctl --user daemon-reload
systemctl --user --no-pager status claude-memory-reflection.service
journalctl --user -u claude-memory-reflection.service -n 100 --no-pager
```

Common causes: the Python path moved (re-run the installer), or `~/.tam/.reflect-pending` was
deleted and the path unit never re-armed (`touch ~/.tam/.reflect-pending`).

**The models are downloaded again after a restart.**
macOS clears the system temp directory, where fastembed keeps its cache by default. Set
`TAM_MODEL_CACHE=~/.tam/models`.

**Ollama.**
Retrieval, reflection and everything except `memory_answer` work without it; richer triples
and deep enrichment need it.

```bash
curl http://127.0.0.1:11434/api/tags   # should return JSON with a models list
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
```

On WSL2 with Windows-host Ollama, set `OLLAMA_URL` in the MCP env block.

## See also

- [configuration.md](configuration.md): every setting
- [upgrading.md](upgrading.md): updating an existing install
- [examples/README.md](../examples/README.md): hook and rules wiring
- [archive/MANUAL_QA.md](archive/MANUAL_QA.md): end-to-end smoke checklist
