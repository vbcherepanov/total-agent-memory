# Setup wizard

total-agent-memory has two first-run wizards:

* **`tam setup`**: a terminal wizard. It sets up personal memory on one machine, or it prepares a company server.
* **The web setup wizard**: a team server that has no administrator yet shows this at `/dashboard/` instead of the sign-in page.

Both ask their questions first, show one review screen, and change nothing until you confirm.

## `tam setup` (terminal)

```sh
tam setup                # first setup
tam setup --reconfigure  # change it later; the current values are the defaults
```

A plain `tam`, typed at a terminal before any setup has run, starts the wizard by itself. It does **not** start in these cases:

* stdin or stdout is not a terminal. MCP clients launch `tam` with pipes, so an MCP stdio session never sees the wizard.
* `MCP_TRANSPORT` is set.
* A CI variable is set (`CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `BUILDKITE`, `TF_BUILD`, `JENKINS_URL`, `TEAMCITY_VERSION`, `CONTINUOUS_INTEGRATION`).
* `TAM_NO_SETUP=1` is set.
* The setup record already exists.

The setup record is `setup.json` in the memory directory (`TAM_MEMORY_DIR`, default `~/.tam`). Set `TAM_SETUP_FILE` to use a different path. The record holds the choices, never keys or passwords. When it exists, `tam setup` prints the current setup and exits. Use `--reconfigure` to change it.

### Upgrading an existing install

A machine that already runs TAM stays a single-user install after an upgrade. That holds for every upgrade path: `update.sh`, a pip/uv/pipx/brew/npm upgrade, or simply the first start of the new version. The upgrade never shows the wizard and does not change the install's behavior.

* **Detection.** On every start, before anything else, `tam` checks for the setup record. If the record is missing but the install already exists, `tam` writes the record silently. An install counts as existing when the memory directory holds `memory.db`, or when a client already has a `memory` MCP entry.
* **What gets recorded.**
  * The record is written with `"mode": "personal"` and `"source": "upgrade"`.
  * It lists the clients that already have the entry, and infers the embedding preset and LLM provider from that entry's env. Keys are never copied into the record.
  * Memory data and client configs are only read. A client config that does not parse is logged and skipped.
* **No blocking.** The check is a few file reads and one small atomic write. If the record cannot be written (for example, a read-only home directory), a JSON warning is logged and the server starts anyway.
* **`update.sh`** runs the same step after its schema stage (`setup_wizard.upgrade.adopt_existing`).
* **Why the record matters.** Because the record now exists, a later `tam` at a terminal does not start the wizard. `tam setup` shows the recorded setup, and `tam setup --reconfigure` edits it with the inferred values as defaults.

### Adding a company server to a personal install

Run `tam setup --mode company`, or in `tam setup --reconfigure` choose **Add a company server**. Either one sets up a team server next to the personal memory.

* The personal store and the client registrations are not touched. The data directory of the company server must not overlap the personal memory directory; the wizard refuses one that does.
* The setup record keeps both sections: `personal` and `company`.
* **Migration is not automatic.** TAM has no documented path that moves records from a personal store into a team workspace. `team-server-reference.md` explicitly warns not to mount an existing personal database as team data. The wizard says so at the end instead of offering a migration.

### Mode 1: "Just me"

Personal memory on this machine, for your own AI clients.

| Step | What it does |
|---|---|
| Connect your AI clients | Checks the usual config locations and binaries of Claude Code, Claude Desktop, Codex CLI, Cursor, Windsurf, Gemini CLI, Cline, Continue, OpenCode and Aider, and pre-selects what it finds. The server is registered under the key `memory` with `TAM_MEMORY_DIR` in its env. Aider has no MCP; it is offered from a git checkout and reads the `memory-protocol` skill instead. |
| Language and embeddings | Presets from `choose_embed`: multilingual MiniLM (the default), multilingual-e5-large, and BGE-M3 (offered only when `sentence-transformers` is installed). If a memory database already exists and you change the model, the wizard reminds you to run `memory_rebuild_embeddings`. |
| Language model (optional) | None, Ollama, OpenAI, Anthropic, or OpenAI-compatible. Each provider's fields and their validation come from the same catalogue as the dashboards' provider pages (`settings_catalog`). Keys are typed as hidden input. You can test the connection with the dashboard's provider check. |
| Hooks and skills | Offered only from a git checkout, because the wheel does not ship them. The hook set is the installers' set (session start/end, stop, prompt submit, pre-edit guard, and the Bash / Write\|Edit / all-tool post hooks), as `.sh` scripts or, on Windows, `.ps1` scripts run through PowerShell. Existing hook scripts and hook entries are kept. Skills go to Claude Code, Codex (plus `~/.agents/skills`), OpenCode and Continue (a rules file). |

**Where the key goes.** The language-model answers, the key included, go to `<memory dir>/settings.json` (mode 0600), the file the local dashboard's **Settings** page edits. The key is stored encrypted with `TAM_MASTER_KEY` or `<memory dir>/master.key` (created 0600). No key is written into any client config, and setup or `--reconfigure` removes LLM keys that an older installer left in a client's `env`. On `--reconfigure`, pressing Enter at the key prompt keeps the current key, whether it was in the settings file or in a client config. See [LOCAL_SETTINGS.md](LOCAL_SETTINGS.md).

**Verification.** After applying, the wizard starts the registered server command on a throwaway memory directory, runs the MCP `initialize` + `tools/list` handshake over stdio, and reports the tool count. Your real memory directory is not touched. Skip this step with `--skip-verify`.

**Config files are edited safely:**

* Existing files are parsed strictly. If one does not parse (for example broken JSON or TOML), the wizard stops before writing anything, names the file, and leaves it as it is.
* Other MCP servers and unrelated keys are kept. Inside the `memory` entry, env variables the wizard does not manage (for example `MEMORY_EMBED_THREADS`) are kept too.
* Codex gets a marked block (`# --- total-agent-memory MCP Server ---`). An unmarked `[mcp_servers.memory]` table, and its `.env` sub-table, is replaced by that block, which is what the installers always did.
* Every file is written to a temporary file and then renamed into place.

### One registration module for every installer

`src/setup_wizard/register.py` (`python -m setup_wizard.register`, or `tam setup register` from an installed package) is the only code that writes client configs. It is used by the wizard, `install.sh`, `install.ps1`, `install-codex.ps1` and the npm wrapper's `connect`. Installers pass their own command, arguments and env (`--command`, `--arg`, `--env KEY=VALUE`); env values the user set earlier, such as keys from the wizard, are kept. Every config is parsed before any is written. After writing, each entry is read back, and the personal setup record is created or extended so `tam` does not start the wizard afterwards. `--unregister` removes the entry and the memory hooks; user hooks stay.

| Client | File the entry goes to |
|---|---|
| Claude Code | `~/.claude.json` (`mcpServers.memory`); hooks in `~/.claude/settings.json` |
| Claude Desktop | `claude_desktop_config.json` in the app's config directory |
| Codex CLI | `$CODEX_HOME/config.toml` (default `~/.codex`), marked block |
| Cursor / Windsurf / Gemini CLI | `~/.cursor/mcp.json`, `~/.codeium/windsurf/mcp_config.json`, `~/.gemini/settings.json` |
| Cline | `cline_mcp_settings.json` in VS Code global storage (`saoudrizwan.claude-dev/settings/`) |
| Continue | `~/.continue/mcpServers/memory.yaml`, a standalone block owned by TAM |
| OpenCode | `$XDG_CONFIG_HOME/opencode/opencode.json` (default `~/.config`), `mcp.memory` of type `local` |
| Aider | a marked `read:` block in `~/.aider.conf.yml` pointing at the memory-protocol skill; a `read:` list you already have is reported, not edited |

Older installers wrote some of these to files the client never reads: `~/.claude/settings.json` (Claude Code), VS Code `settings.json` `cline.mcpServers` or `~/.cline/mcp.json` (Cline), `~/.opencode/config.json` or `~/.config/opencode/config.json` (OpenCode), `~/.continue/config.json` (Continue). On upgrade, the first start logs one line naming those files and suggesting `tam setup --reconfigure`; the old files are not changed. An entry the npm wrapper made under the name `total-agent-memory` in a file TAM registers into is replaced by the `memory` entry, so the server does not appear twice.

### Mode 2: "Company server"

One shared memory server that your teams connect to.

| Step | What it does |
|---|---|
| Data directory | Default `TAM_TEAM_DIR` or `~/.tam-server`. An existing server is inspected read-only: administrators, departments, company profile, stored settings. |
| Address and port | Bind address (default `127.0.0.1`), port (default 3737; the wizard warns if something already answers there), and the public URL used in links and client snippets. |
| How the server runs | **Background service**: a systemd unit on Linux or a launchd agent on macOS, written to `<data dir>/deploy/`. The wizard prints the one-line install command and does not run it. **Docker Compose**: writes `<data dir>/deploy/compose.env` for `docker-compose.team.yml`; the rest of the setup happens in the web wizard. **Run it yourself**: prints the `tam-team serve` command. |
| Company | The company name. It is shown in the dashboard header and every change is audited. |
| First superadmin | Uses the existing `bootstrap-admin` logic. The invite code is printed once at the end. Skipped when an administrator already exists. |
| Departments | Optional. Add several: a name, then an ID derived from the name. Departments that already exist are skipped. |
| Model providers | LLM and embedding provider, with the dashboard's fields. Values are written through `SettingsStore`, so keys are encrypted with the server's master key. When settings already exist, "Keep current settings" is the default. |

Run the wizard with the same `TAM_TEAM_MASTER_KEY` environment as the server, if the server uses one. Otherwise the wizard encrypts with `<data dir>/master.key`.

At the end the wizard prints the administrator's invite code (once), the dashboard URL, how employees connect (invite code, then a personal token, then an MCP snippet for `tam-remote`), and whether a server already answers at the public URL. The last line is a pointer to paid rollout help: "Need help with rollout or support? See https://totalmemory.dev/pricing". The URL is defined once, as `team_memory.setup.SUPPORT_URL`, and the web wizard's Done step shows the same line. The personal path does not show it. There is no license step, no license key, no seat limit and no telemetry.

### Non-interactive use (installers, containers)

Every answer can come from a flag. Keys are never passed as flags: name an environment variable that holds the key.

```sh
tam setup --non-interactive --mode personal --clients detected --embed-preset multilingual \
  --llm openai --llm-api-key-env OPENAI_API_KEY --no-hooks --skills

tam setup --non-interactive --json --mode company --data-dir /srv/tam --host 0.0.0.0 --port 3737 \
  --public-url https://memory.example.com --deploy manual --company-name "Acme" \
  --admin-id alice --admin-name "Alice Admin" --department eng=Engineering --department ops=Operations \
  --llm ollama --ollama-url http://ollama:11434 --embed-provider fastembed
```

`--mode` also works without `--non-interactive`: it skips the first question. `--clients` takes client IDs (`claude-code`, `claude-desktop`, `codex`, `cursor`, `windsurf`, `gemini-cli`, `cline`, `opencode`), `detected`, or `none`. With `--json`, progress goes to stderr and a result object goes to stdout. In company mode that object includes the invite code. Inside the Docker image the same wizard runs as `python -m setup_wizard` with `PYTHONPATH=/app/src`.

| Exit code | Meaning |
|---|---|
| 0 | Applied (and verified, if verification ran) |
| 1 | Nothing applied: a problem you can fix, such as an unparseable config file |
| 2 | Applied, but the personal server did not start in the check |
| 3 | You declined at the review screen |
| 64 | Wrong or missing flags |
| 130 | Cancelled |

### Ctrl-C and partial writes

* **During the questions:** nothing has been written.
* **During apply:** Ctrl-C is held until the apply step finishes. Every file, the record included, is complete; then the wizard stops before the check.
* **New company data directory:** it is built in a hidden sibling directory and renamed into place at the end. If apply fails, that directory is deleted.
* **Any failure during apply:** the wizard lists what was already applied.

### Adding a step

Steps are an ordered registry (`setup_wizard.steps.registry()`). A `Step` has `id`, `title`, `applies_to` (the modes), `run` (asks the questions and returns a typed `Answer`), `contribute` (adds actions, env and record fields to the apply plan), and `when` (an optional condition). To add a step, insert one `Step` into the list. No other step changes. `tests/test_setup_wizard.py::test_a_new_step_plugs_in_without_touching_the_others` covers this.

## Web setup wizard (team server)

When `tam-team serve` starts and no active superadmin exists, it issues a one-time **setup code** and prints it in the console or log, the way Jupyter and Grafana print their tokens:

```
========================================================================
  Team Memory has no administrator yet. Finish setup in the browser:

    http://127.0.0.1:3737/dashboard/#setup=ABCD-EFGH-JKLM-NPQR-STUV-WXYZ

  Setup code: ABCD-EFGH-JKLM-NPQR-STUV-WXYZ
  Valid until 2026-09-25T12:00:00+00:00, single use.
  New code: restart the server or run `tam-team --root <data dir> setup-token`.
========================================================================
```

The code sits in the URL fragment, which the browser never sends to the server. The page reads it and then removes it from the address bar. Opening `/dashboard/` without the fragment asks for the code.

**Security:**

* **The code:** 24 characters from a 32-symbol alphabet (120 bits). Only its SHA-256 digest is stored, in `setup_tokens` in `identity.db`. It expires after `TAM_TEAM_SETUP_TOKEN_TTL_MINUTES` (default 60). Issuing a new code (at restart or with `tam-team setup-token`) voids the old one and also lifts the shared setup lockout. The code is written to the server log at WARNING level, so anyone who can read `docker logs` or the log file before the first administrator exists can claim the superadmin: keep first-run log access restricted and the TTL short.
* **Creating the administrator** claims the code, creates the superadmin with a scrypt password hash, and saves the company profile, all in one `BEGIN IMMEDIATE` transaction. A second attempt with the same code, including a concurrent one, fails. If the chosen user ID already exists, the transaction rolls back and the code stays valid.
* **Rate limit:** 5 wrong codes from one IP, or 50 from all IPs together, within 15 minutes lock setup for 15 minutes. The limit also applies to a correct code during the lockout. If strangers keep the shared limit locked, run `tam-team --root <data dir> setup-token` (or restart): a new code clears it, and `bootstrap-admin` from the CLI always works. Lockouts are written to the audit log. The per-IP counter uses the same `login_failures` table as sign-in.
* **Once a superadmin exists,** `GET /dashboard/api/setup`, `POST /dashboard/api/setup/verify` and `POST /dashboard/api/setup/complete` return 404, and no code is issued. `bootstrap-admin` from the CLI closes setup the same way.
* **Browser protections:** the setup routes use the dashboard's checks: JSON bodies only, cross-origin `Origin` rejected, the 1 MB body limit, the same CSP and security headers. The pages keep the strict CSP (`script-src 'self'; style-src 'self'`, no inline code). The wizard is `static/setup.js` and builds the DOM with `textContent` only.

**Steps:**

1. Setup code
2. Company: the name and the public address
3. First admin: user ID, full name, password (12 or more characters); submitting signs you in
4. Departments: add several
5. Providers: the dashboard's own provider cards (the `settings` section, mounted inside the wizard)
6. Connect your team: the MCP URL, how invites and tokens work, and a copyable `tam-remote` or `remote.py` snippet; the public address can be corrected here
7. Done: summary; *Open the dashboard* marks setup complete

Steps 4–7 run as the signed-in superadmin and use the normal admin APIs. If the page is closed before *Done*, the next sign-in of a superadmin resumes the wizard at step 4. The company profile is stored in the `organization` table:

| Key | Value |
|---|---|
| `name` | The company name |
| `public_url` | The public address |
| `setup_state` | `admin_created` or `complete` |

Each change is written to the audit log as `organization_updated`. A superadmin can edit the name and public address later under *Administration → System*.

| New endpoint | Who | What |
|---|---|---|
| `GET /dashboard/api/setup` | anyone, until setup | `{required, token_active, expires_at, organization}` |
| `POST /dashboard/api/setup/verify` | anyone, until setup | `{token}`: checks the code without using it |
| `POST /dashboard/api/setup/complete` | anyone, until setup | `{token, company_name, public_url?, user_id, name, password}`: returns the session overview and sets the session cookie |
| `GET` / `POST /dashboard/api/admin/organization` | superadmin | read the company profile / update `{name?, public_url?}` |
| `POST /dashboard/api/admin/setup/finish` | superadmin | set `setup_state=complete` |

The session overview (`/dashboard/api/session`) now also returns `organization.name` and `setup_pending`.

Metrics: the `setup` counter carries `step` and `outcome` labels. HTTP latency is covered by the existing `dashboard_api` route class.
