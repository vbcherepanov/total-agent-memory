# Updating and upgrading

Release-by-release changes are in the [CHANGELOG](../CHANGELOG.md), with a summary of each recent version in [what is new](whats-new.md).
Upgrading from 14.x to 15: read [UPGRADE_15.md](UPGRADE_15.md) first. It lists the schema migrations that run on first start and how to back up before you upgrade.

- [Update a package install](#update-a-package-install)
- [Update a source checkout](#update-a-source-checkout)
- [Docker](#docker)
- [After any update](#after-any-update)
- [Older versions](#older-versions)

## Update a package install

Use the channel you installed from:

| Channel | Update |
|---|---|
| pip | `pip install --upgrade total-agent-memory` |
| pipx | `pipx upgrade total-agent-memory` |
| uvx | `uvx --refresh total-agent-memory` fetches the newest release |
| npx | Run the same `npx -y total-agent-memory connect <client>` command again |
| Homebrew | `brew update && brew upgrade vbcherepanov/tap/total-memory` |
| Claude Code plugin | The plugin starts the server the plugin repository pins; update the plugin ([installation](installation.md#claude-code-and-codex-plugins)). |

An existing install stays a single-user install after an upgrade and never shows the
setup wizard ([SETUP_WIZARD.md](SETUP_WIZARD.md#upgrading-an-existing-install)).
A data directory from 11.x or earlier (`~/.claude-memory/`) is moved to `~/.tam/` on
first run, and a symlink is left behind.

## Update a source checkout

```bash
cd ~/total-agent-memory   # legacy clones: ~/claude-memory-server
./update.sh
```

`./update.sh` runs these stages and stops with the previous source restored if the test
gate fails:

1. Pre-flight: disk check and a database snapshot (the last 7 are kept).
2. Source: `git pull`, or a SHA-256-verified tarball when `UPDATE_URL` is set.
3. Dependencies, only if a `requirements*.txt` hash changed.
4. The full pytest suite.
5. Schema migrations.
6. Reload of the LaunchAgents and the dashboard.
7. A reminder to reconnect the MCP server (in Claude Code: `/mcp`, `memory`, Reconnect).

`./update.sh --check` reports without changing anything; `--skip-tests` skips the test
gate and is not recommended.

The manual equivalent:

```bash
cd ~/total-agent-memory   # legacy clones: ~/claude-memory-server
git pull
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python src/tools/version_status.py
.venv/bin/python -m pytest tests/
```

## Docker

Pull or start the newer image tag and keep the same data volume:

```bash
docker run -p 127.0.0.1:3737:3737 -p 127.0.0.1:37737:37737 -v ~/.tam:/data ghcr.io/vbcherepanov/total-agent-memory:15.0.2
```

The Compose installation is described in [installation](installation.md#docker).

## After any update

Restart the MCP server so the new code and migrations load. Check the version with
`tam --version` and the tool count in the client (`/mcp` in Claude Code; 79 tools with the
default `TAM_TOOL_PROFILE=full`). `update.sh` also refreshes the hook scripts it installed.

## Older versions

The upgrade guides for v7 to v8 and v8 to v9 predate the PyPI distribution and are kept in
[archive/upgrading-v7-v9.md](archive/upgrading-v7-v9.md).
