# Team server quick start

One server holds personal, team and shared memory for several people. This page is the short English walkthrough; the full reference is [TEAM_SERVER_V14.md](TEAM_SERVER_V14.md) (Russian), with [TEAM_DASHBOARD.md](TEAM_DASHBOARD.md), [TEAM_POSTGRES.md](TEAM_POSTGRES.md), [TEAM_BACKUP.md](TEAM_BACKUP.md) and [TEAM_ONBOARDING.md](TEAM_ONBOARDING.md) for the individual features. `tam setup` → **Company server** runs the same steps interactively ([SETUP_WIZARD.md](SETUP_WIZARD.md)).

## Choose local or server mode

| Mode | Install and use |
|---|---|
| **Local, one person** | Follow [installation](installation.md), then the [Quick start](../README.md#quick-start). Memory and models run on your machine; the local MCP catalogue has 77 tools. |
| **Server, multiple people** | Follow the setup below. Memory and models run on the server; clients need only Python and their token. The remote catalogue exposes eight core tools. |

The server instructions work on Linux, macOS and Windows. Run commands in a directory where you can create the data folder and token files.

## Start a server without Docker

Install the package in a dedicated environment. To use a locally built wheel instead, pass its path (for example `./dist/total_agent_memory-14.8.0-py3-none-any.whl`) in place of the package name. Linux/macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install total-agent-memory
```

Windows PowerShell, using the environment directly without changing the execution policy:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install total-agent-memory
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
```

Then, on any of these systems:

```sh
tam-team --root ./team-data user-add vasya 'Vasya'
tam-team --root ./team-data user-add petya 'Petya'
tam-team --root ./team-data team-add engineering 'Engineering'
tam-team --root ./team-data member vasya engineering editor
tam-team --root ./team-data member petya engineering editor
tam-team --root ./team-data token-create vasya --client codex --out ./vasya.token
tam-team --root ./team-data token-create petya --client cursor --out ./petya.token
tam-team --root ./team-data serve --host 127.0.0.1 --port 3738
```

Open `http://127.0.0.1:3738/` and sign in with the contents of your token file. Keep each token private; give Petya his own token, not Vasya's. For access from other machines, configure an HTTPS reverse proxy to the server's `/mcp/` endpoint and web interface. [Permissions, backup, restore and server configuration](TEAM_SERVER_V14.md).

## Start a server with Docker Compose

From a source checkout, with port 3738 available:

```sh
docker compose -f docker-compose.team.yml build
docker compose -f docker-compose.team.yml run --rm team-memory python /app/src/team_memory/cli.py user-add vasya 'Vasya'
docker compose -f docker-compose.team.yml run --rm team-memory python /app/src/team_memory/cli.py token-create vasya --client codex --out /team-data/vasya.token
docker compose -f docker-compose.team.yml up -d
docker compose -f docker-compose.team.yml cp team-memory:/team-data/vasya.token ./vasya.token
```

The web interface is at `http://127.0.0.1:3738/`. The token copied to the host is a credential: restrict file access to its owner. To add Petya, a team and membership, use the same CLI subcommands shown above through `docker compose -f docker-compose.team.yml run --rm team-memory python /app/src/team_memory/cli.py`.

Compose keeps data and model caches in persistent volumes. Set `TAM_TEAM_PORT`, `TAM_TEAM_MAX_WORKERS` and LLM settings in `.env` as needed; see [.env.example](../.env.example). Plan at least 4 GiB RAM for three warm MiniLM workers and measure your own workload. [Docker server details](TEAM_SERVER_V14.md#docker).

Continuous backup is optional: set `TAM_TEAM_REPLICA_URL=s3://bucket/path` and the bucket credentials, then add `--profile litestream`. A Litestream sidecar streams every transaction of the server's SQLite files to the bucket while the server runs, and `tam-team replication restore --to DIR [--timestamp ...]` rebuilds the server on any machine. [Backup, point-in-time restore and data deletion](TEAM_BACKUP.md).

## Connect a remote IDE

Copy `src/team_memory/remote.py` to the client and supply its personal token file. This bridge uses only the Python standard library. For clients with an `mcpServers` configuration:

```json
{
  "mcpServers": {
    "total-agent-memory": {
      "command": "python3",
      "args": ["/absolute/path/remote.py"],
      "env": {
        "TAM_REMOTE_URL": "https://YOUR_SERVER/mcp/",
        "TAM_REMOTE_TOKEN_FILE": "/absolute/path/vasya.token"
      }
    }
  }
}
```

Replace the paths and server address. On Windows use the path to `python.exe` and Windows file paths. Local testing may use `http://127.0.0.1:3738/mcp/`; remote connections require HTTPS. If the package is installed on the client, `tam-remote` is also available. [Client configuration details](TEAM_SERVER_V14.md#лёгкий-удалённый-клиент).

## Save, search and see who changed what

Ask your agent to call `memory_scopes` first to list available areas. These are example arguments for the remote `memory_save` tool:

```jsonl
{"content":"My investigation notes", "scope":{"kind":"personal"}, "tags":["release-v14"]}
{"content":"Engineering release checklist", "scope":{"kind":"team","team_id":"engineering"}, "tags":["release-v14"]}
{"content":"Company-wide onboarding guide", "scope":{"kind":"shared"}, "tags":["onboarding"]}
```

- **Personal:** visible only to the token's owner; this is the default for saves.
- **Team:** visible to members; `reader` can read, `editor` can also change records.
- **Shared:** readable and editable by every authenticated user of this server.
- **Tags:** organize topics. Access comes from `scope` and membership; reserved `scope:`, `team:` and `user:` tags are managed by the server.

Call `memory_recall` with `{"query":"release checklist"}` to search all accessible areas, or add a `scope` to narrow the search. Use `memory_get` and `memory_history` with the returned record's `id` and `scope` to inspect content, author and changes. `memory_update` requires those fields plus `expected_revision`, `content` and `reason`; use the new ID returned by the update for subsequent operations. The author is taken from the token automatically.

The remote tools are `memory_scopes`, `memory_save`, `memory_recall`, `memory_get`, `memory_update`, `memory_delete`, `memory_history` and `memory_export`. History and export are paginated. [Full behavior and retry rules](TEAM_SERVER_V14.md).

## Configure internal models and CPU

Fast mode works without an LLM. To use a local model for optional internal tasks, set these environment variables (or `.env` for Compose):

```dotenv
MEMORY_LLM_ENABLED=true
MEMORY_LLM_PROVIDER=ollama
MEMORY_LLM_MODEL=YOUR_INSTALLED_MODEL
OLLAMA_URL=http://127.0.0.1:11434
MEMORY_EMBED_THREADS=1
MEMORY_TORCH_THREADS=1
```

For Docker, the Ollama address must be reachable from the container; the team profile defaults to `http://host.docker.internal:11434`. For another compatible server, select `MEMORY_LLM_PROVIDER=openai-compatible` and set `MEMORY_LLM_API_BASE`, `MEMORY_LLM_MODEL` and, if required, `MEMORY_LLM_API_KEY`. Compatibility means Chat Completions, with JSON Schema support for structured tasks. Set `MEMORY_VISION_MODEL` separately for images. Restart after changing environment settings. [Provider settings and CPU limits](LLM_V14.md).

Optional remote LLM providers receive the content used in those tasks. Keep Ollama local or set `MEMORY_LLM_ENABLED=false` if that content must stay on your own infrastructure.
