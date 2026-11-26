# IDE setup guide — total-agent-memory v10.5

Same MCP server, same tools, same protocol — different installation
locations per IDE. The installer (`install.sh --ide <name>`) registers
the MCP server and installs the skill or rules file; it wires the
capture hooks for Claude Code only. This document is both the manual
fallback and the reference for what the installer does.

If your IDE is not listed: contributions welcome. The protocol still
works as long as the client speaks MCP — just paste the SKILL.md
content into your system-prompt or rules file.

---

## Universal preconditions

```
- Python 3.11+ (3.13 recommended)
- One of: Ollama (local LLM, recommended) or any cloud provider
  (OpenAI / Anthropic / Cohere / OpenAI-compat)
- ~/total-agent-memory checked out (or installed via any package channel)
- ~/.claude-memory/ writable for SQLite + cache + wikis
```

```bash
# Verify the MCP server starts in stdio mode:
bash ~/total-agent-memory/scripts/diagnose.sh
```

---

## 1. Claude Code (CLI + desktop)

**Hook API:** ✅ full
**Skill API:** ✅ via `~/.claude/skills/<name>/SKILL.md`
**Sub-agents:** ✅ via `~/.claude/agents/<name>.md`

### Install

```bash
~/total-agent-memory/install.sh --ide claude-code
```

The installer writes:

- `~/.claude/skills/memory-protocol/` (this skill, with `references/`)
- `~/.claude.json` — the MCP server entry (`mcpServers.memory`), the file Claude Code reads MCP servers from
- `~/.claude/settings.json` — the memory hooks
- `~/.claude/CLAUDE.md` — appends `@~/.claude/rules/memory.md` reference

### Manual

Register the server with `claude mcp add --scope user memory -- total-agent-memory` (it lands in
`~/.claude.json`), then add the hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart":    [{"matcher": ".*", "hooks": [{"type": "command", "command": "~/.claude/hooks/session-start.sh"}]}],
    "PreToolUse":      [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "~/.claude/hooks/pre-edit.sh"}]}],
    "UserPromptSubmit":[{"matcher": ".*", "hooks": [{"type": "command", "command": "~/.claude/hooks/user-prompt-submit.sh"}]}],
    "Stop":            [{"matcher": ".*", "hooks": [{"type": "command", "command": "~/.claude/hooks/on-stop.sh"}]}],
    "PostToolUse":     [{"matcher": ".*", "hooks": [{"type": "command", "command": "~/.claude/hooks/post-tool-use.sh"}]}]
  }
}
```

Restart `claude` and run `/skills` to confirm `memory-protocol` is
listed.

---

## 2. Codex CLI

**Hooks installed:** ❌ — the installer registers no hooks with Codex;
the optional `notify` program below captures finished turns
**Skill API:** ✅ `~/.codex/skills/` and `~/.agents/skills/`
**Sub-agents:** N/A

### Install

```bash
~/total-agent-memory/install.sh --ide codex
```

Writes:

- `~/.codex/skills/` — `memory-protocol` and the other shipped skills
  (mirror of the Claude Code skills)
- `~/.agents/skills/` — the Codex `memory` skill and the agent skills
- `~/.codex/config.toml` — the `[mcp_servers.memory]` table and its
  `env`, between two marker comments

It does not write hooks, `~/.codex/AGENTS.md` or a dashboard service.

### Manual `~/.codex/config.toml`:

```toml
[mcp_servers.memory]
command = "total-agent-memory"
args = []
```

The full snippet is in `../templates/codex-config.toml`.

### Optional: capture finished turns

Codex runs a `notify` program after each agent turn. Pointing it at the
checkout's `hooks/codex-notify.sh` (`codex-notify.ps1` on Windows)
records each turn in the action log and, with `TAM_TURN_AUTOSAVE`, saves
a turn summary. `notify` is a top-level key, so it goes above the first
`[table]` of `config.toml`:

```toml
notify = ["/path/to/total-agent-memory/hooks/codex-notify.sh"]
```

### Optional: `~/.codex/AGENTS.md`

The skill already carries the protocol. To state it in `AGENTS.md` as
well, append the block from `../templates/codex-AGENTS-block.md`
yourself.

---

## 3. Cursor

**Hook API:** ❌
**Skill API:** Rules pane (project + global)
**Sub-agents:** Composer custom commands

### Install

```bash
~/total-agent-memory/install.sh --ide cursor
```

Writes:

- `<project>/.cursor/rules/memory-protocol.mdc` — the canonical rules
  file Cursor auto-loads with `description:` and `globs:` set so it
  triggers on every file
- `~/.cursor/mcp.json` — MCP server registration (Cursor reads this)

### Manual

`<project>/.cursor/rules/memory-protocol.mdc`:

```mdc
---
description: total-agent-memory protocol — recall before tasks, save after
globs: ["**/*"]
alwaysApply: true
---

# (paste the body of SKILL.md here, dropping the YAML frontmatter)
```

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "memory": {
      "command": "total-agent-memory",
      "args": []
    }
  }
}
```

Cursor doesn't fire IDE hooks. The protocol relies on the rules
content telling the model to call `session_init` on first turn and
`memory_recall` before tasks. Recovery files from `on-stop` are
loaded manually with `/recall` style commands.

---

## 4. Cline (VS Code extension)

**Hook API:** ❌
**Skill API:** `.clinerules/` directory
**Sub-agents:** N/A

### Install

```bash
~/total-agent-memory/install.sh --ide cline
```

Writes:

- Cline's `cline_mcp_settings.json` (VS Code global storage,
  `.../Code/User/globalStorage/saoudrizwan.claude-dev/settings/`) — the MCP server under `mcpServers`

Copy `templates/cline-rules.md` into `<project>/.clinerules/memory-protocol.md` to load the protocol.

### Manual

`<project>/.clinerules/memory-protocol.md`:

```markdown
# (paste SKILL.md body here)
```

Cline → MCP Servers → Configure (`cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "memory": {
      "command": "total-agent-memory",
      "args": []
    }
  }
}
```

---

## 5. Continue (VS Code / JetBrains)

**Hook API:** ❌
**Skill API:** `~/.continue/rules/`
**Sub-agents:** N/A

### Install

```bash
~/total-agent-memory/install.sh --ide continue
```

Writes:

- `~/.continue/rules/memory-protocol.md`
- `~/.continue/mcpServers/memory.yaml` — an MCP server block (`schema: v1`, `mcpServers: [{name: memory, ...}]`)

### Manual

`~/.continue/mcpServers/memory.yaml`:

```yaml
name: total-agent-memory
version: 1.0.0
schema: v1
mcpServers:
  - name: memory
    command: total-agent-memory
    args: []
```

---

## 6. Aider

**Hook API:** ❌
**Skill API:** `--read` files / `.aider.conf.yml`
**Sub-agents:** N/A

Aider has no MCP support yet (as of writing). Memory access is via
the bash bridge:

```yaml
# .aider.conf.yml
read:
  - ~/total-agent-memory/skills/memory-protocol/SKILL.md
```

In the prompt, instruct Aider to shell out:

```
For any past convention, run:
  lookup-memory "<query>"

For saving:
  ~/total-agent-memory/ollama/save_memory.sh --type solution \
      --project <name> --tags "reusable,..." --content "..."
```

---

## 7. Windsurf

**Hook API:** ❌ (planned per docs)
**Skill API:** `.windsurfrules`
**Sub-agents:** Cascade flows

### Install

```bash
~/total-agent-memory/install.sh --ide windsurf
```

Writes:

- `<project>/.windsurfrules` — paste of SKILL.md body
- `~/.windsurf/mcp_config.json` — MCP server entry

---

## 8. Gemini CLI

**Hook API:** ⚠️ partial
**Skill API:** `.gemini/rules/`
**Sub-agents:** N/A

### Install

```bash
~/total-agent-memory/install.sh --ide gemini-cli
```

Writes:

- `~/.gemini/rules/memory-protocol.md`
- `~/.gemini/config.toml` — MCP server registration

---

## 9. OpenCode

**Hook API:** ✅ via `.opencode/hooks/<event>.json`
**Skill API:** `.opencode/skills/`
**Sub-agents:** custom commands

### Install

```bash
~/total-agent-memory/install.sh --ide opencode
```

Writes:

- `~/.opencode/skills/memory-protocol/` (mirror of Claude Code skill)
- `~/.config/opencode/opencode.json` — `mcp.memory` as a local server
  (`"type": "local"`, `"command": [...]`, `"environment": {...}`)

---

## Cross-platform notes

### Windows (native)

Use the PowerShell variants:

```powershell
~\total-agent-memory\install.ps1 -Ide claude-code
```

The `.ps1` hooks read PowerShell-formatted JSON from stdin. Same
behaviour as the bash variants.

### WSL2 (Ubuntu / Debian / Alpine)

Same as Linux — use `install.sh --ide <name>`. The installer detects
WSL2 and adjusts:

- `~/.claude/` paths use the Linux home, not the Windows one
- Bash `<( ... )` process substitution is avoided (some `dash`
  fallbacks)
- Ollama URL defaults to `http://localhost:11434` inside WSL2 but
  can be flipped to `http://host.docker.internal:11434` if Ollama
  runs on Windows

### macOS (Apple Silicon and Intel)

Default. The installer uses bash 3.2 (macOS default) syntax — no
`mapfile`, no `[[`-pcre, no `${VAR^^}` (uppercase). All hooks run
under `/bin/bash` (3.2.x) without the Homebrew bash 5.

### macOS bash 3.2 quirks worth knowing

- No `mapfile` / `readarray` — use `while IFS= read -r line; do ... done`.
- `case` with `;;&` falls through; `;;` is the safe default.
- `${var,,}` lowercase doesn't exist — use `tr '[:upper:]' '[:lower:]'`.
- `local -n` namerefs don't exist — use eval-via-printf if you must.

The installer guards every script with `set -eu` (no `-o pipefail` —
it's not in POSIX sh and breaks on dash; explicit pipe-error checks
are used instead).

---

## Verification

After install, verify in three places:

```bash
# 1. MCP server reachable
bash ~/total-agent-memory/scripts/diagnose.sh

# 2. Skill loaded by IDE
# (Claude Code) /skills | grep memory-protocol
# (Codex)       codex skills list | grep memory
# (Cursor)      Settings → Rules → check memory-protocol.mdc enabled
# (Cline)       reload window, prompt: "what skills are loaded?"

# 3. Hooks firing (where supported)
tail -f ~/.claude-memory/hooks.log
# … then trigger a session start in your IDE; expect a session-start log line
```

If any step fails, the installer's `--diagnose` flag prints a
detailed health report:

```bash
~/total-agent-memory/install.sh --diagnose
```

---

## Removing / switching IDE

```bash
~/total-agent-memory/install.sh --uninstall --ide <name>
```

Removes only that IDE's wiring; the MCP server, DB, and skill files
remain. To wipe everything:

```bash
~/total-agent-memory/install.sh --uninstall --all
# … and optionally:
rm -rf ~/.claude-memory/
```

---

## "Bring your own MCP client" — minimal protocol

If your client speaks MCP but isn't on this list:

1. Register the server: command `total-agent-memory`, no arguments.
2. In your system prompt, paste the body of
   `skills/memory-protocol/SKILL.md` (drop the YAML frontmatter).
3. Memorise: **`session_init` first, `memory_recall` before tasks,
   `memory_save` after, `session_end` last.**

That's the whole skill in one paragraph.
