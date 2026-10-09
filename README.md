<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vbcherepanov/total-agent-memory/main/docs/assets/tam-logo-dark.svg">
    <img src="https://raw.githubusercontent.com/vbcherepanov/total-agent-memory/main/docs/assets/tam-logo-light.svg" alt="total-agent-memory" width="440">
  </picture>
</h1>

<!-- mcp-name: io.github.vbcherepanov/total-agent-memory -->

> Persistent, local memory for AI coding agents: Claude Code, Codex CLI, Cursor and any MCP client.
>
> Website and documentation: **[totalmemory.dev](https://totalmemory.dev)** · [Quick start](https://totalmemory.dev/docs/quick-start) · [Claude Code guide](https://totalmemory.dev/docs/claude-code) · [Benchmarks](https://totalmemory.dev/benchmarks) · [Compare with other tools](https://totalmemory.dev/compare)

[![Version](https://img.shields.io/badge/version-14.8.0-8ad.svg)](https://pypi.org/project/total-agent-memory/)
[![PyPI](https://img.shields.io/badge/PyPI-total--agent--memory-3776AB.svg)](https://pypi.org/project/total-agent-memory/)
[![npm](https://img.shields.io/badge/npm-total--agent--memory-cb3837.svg)](https://www.npmjs.com/package/total-agent-memory)
[![Docker GHCR](https://img.shields.io/badge/docker-ghcr.io-2496ED.svg)](https://github.com/vbcherepanov/total-agent-memory/pkgs/container/total-agent-memory)
[![MCP](https://img.shields.io/badge/MCP-2026--07--28-blue.svg)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/license-MIT-fa4.svg)](LICENSE)

total-agent-memory (TAM) is an open-source memory server for AI coding agents.
Coding agents start every session without memory of earlier ones, so decisions,
fixes and project conventions have to be explained again. TAM stores decisions,
solutions, facts, errors and session summaries on your machine and returns them
through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP), so
any MCP client can use it without code changes.

Each store is one directory built around a SQLite database. Recall combines
full-text BM25, dense embeddings computed locally, fuzzy matching and a
knowledge graph, and fuses the ranked lists with reciprocal rank fusion; an
optional cross-encoder can rerank the result. The default profile makes no LLM
call on write or search, so retrieval can be measured offline and a rerun gives
the same result. Facts can carry validity intervals (`kg_add_fact`, `kg_at`),
and a newer value of a single-valued fact can retire the older one.

TAM is for developers who use coding agents daily and for researchers who need
a memory baseline they can run locally, inspect and change. The same package
can also run as a team server: personal, department and company areas are
separate stores behind one gateway, with roles, authorship and an audit trail
([team server](docs/team-server.md)).

## Installation

TAM needs **Python 3.11 or newer**. CI tests Python 3.11, 3.12 and 3.13 on
Ubuntu, Windows and macOS.

**From PyPI** (use a virtual environment, or pipx / uvx):

```bash
pip install total-agent-memory          # or: pipx install total-agent-memory
uvx total-agent-memory                  # run once without installing
```

This installs the `total-agent-memory` (alias `tam`) MCP server and the
`tam-team`, `tam-remote` and `lookup-memory` commands. The optional
cross-encoder reranker pulls in PyTorch and is an extra:
`pip install "total-agent-memory[rerank]"`. The PostgreSQL backend of the team
server is the `[postgres]` extra.

**Register it with your clients.** Run `tam setup` at a terminal. The wizard
asks "Just me" or "Company server", detects Claude Code, Claude Desktop, Codex,
Cursor, Windsurf, Gemini CLI, Cline and OpenCode, and registers the server with
the ones you pick ([setup wizard](docs/SETUP_WIZARD.md)).

**Other channels:**

| Channel | Command |
|---|---|
| Docker (linux/amd64, linux/arm64) | `docker run -p 3737:3737 -p 37737:37737 -v ~/.tam:/data ghcr.io/vbcherepanov/total-agent-memory:14.8.0` — MCP over HTTP on `:3737/mcp`, dashboard on `:37737` |
| npx connector | `npx -y total-agent-memory connect claude-code` (or `codex`, `cursor`, `cline`, `continue`, `aider`, `windsurf`, `gemini-cli`, `opencode`) |
| Claude Code plugin (server, skill and capture hooks) | `/plugin marketplace add vbcherepanov/total-agent-memory`<br>`/plugin install total-agent-memory@vbcherepanov` |
| Plugin for Claude Code and Cowork, from the [plugin repository](https://github.com/vbcherepanov/total-agent-memory-plugin) (server and skill, no hooks; needs [uv](https://docs.astral.sh/uv/)) | `/plugin marketplace add vbcherepanov/total-agent-memory-plugin`<br>`/plugin install total-agent-memory@vbcherepanov` |
| The same plugin for Codex CLI | `codex plugin marketplace add vbcherepanov/total-agent-memory-plugin`<br>`codex plugin add total-agent-memory@vbcherepanov` |
| Source checkout with IDE hooks and background services | `git clone https://github.com/vbcherepanov/total-agent-memory.git && cd total-agent-memory && ./install.sh --ide claude-code` (Windows: `install.ps1 -Ide claude-code`) |

Use one of the two Claude Code plugins, not both. Per-platform details, WSL2,
the IDE matrix, uninstalling and troubleshooting are in the
[installation guide](docs/installation.md).

## Quick start

1. Install the package and run `tam setup`, or add the server to your client by
   hand. For Claude Code:

   ```bash
   claude mcp add memory -- total-agent-memory
   ```

   For clients that use an `mcpServers` file:

   ```json
   { "mcpServers": { "memory": { "command": "total-agent-memory" } } }
   ```

   If you installed into a virtual environment, use the full path to its
   `total-agent-memory` executable.

2. Restart the client. Memory is stored in `~/.tam/` unless `TAM_MEMORY_DIR`
   points elsewhere.

3. Ask the agent to remember something ("remember that we chose PostgreSQL
   for billing because of row-level security"). It calls `memory_save`. In a
   later session, ask "which database did we choose for billing?"; it calls
   `memory_recall` and gets the record back.

To try the server without an agent, this script starts it over stdio with the
MCP Python SDK (installed as a dependency), saves one record and recalls it.
The SDK passes only a few variables to the server by default, so the script
hands over the full environment, including `TAM_MEMORY_DIR`:

```python
import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    server = StdioServerParameters(command="total-agent-memory", env=dict(os.environ))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("memory_save", {
                "type": "decision",
                "content": "Chose PostgreSQL over MySQL for the billing service",
                "context": "WHY: row-level security per tenant",
                "project": "demo",
            })
            result = await session.call_tool("memory_recall", {
                "query": "which database for billing", "project": "demo", "limit": 3,
            })
            print(result.content[0].text)


asyncio.run(main())
```

```bash
TAM_MEMORY_DIR="$(mktemp -d)" python quickstart.py
```

The first run downloads the default embedding model. The output
is JSON with the saved decision under `results.decision`. More examples:
[tools and interfaces](docs/tools.md).

## Running the tests

The tests run from a source checkout. These commands mirror the CI workflows
in [.github/workflows](.github/workflows):

```bash
git clone https://github.com/vbcherepanov/total-agent-memory.git
cd total-agent-memory
python3 -m venv .venv
.venv/bin/pip install -e . -r requirements-dev.txt

export FASTEMBED_CACHE_PATH="$PWD/.tam-models" TAM_MEMORY_DIR="$PWD/.tam-test-memory"
.venv/bin/python tests/smoke/prewarm_models.py   # downloads the text and code embedding models (~850 MB) once
.venv/bin/python -m pytest tests -q
```

The tests need no API keys and no LLM; the full suite takes 15 to 20 minutes
on a laptop. Do not set `MEMORY_LLM_ENABLED=false` for the full suite: the
configuration tests check LLM auto-detection. Tests that need services you do not
have are skipped:

- **PostgreSQL** (team server backend): install the extra with
  `.venv/bin/pip install -e ".[postgres]"`, then run
  `.venv/bin/python -m pytest tests -m postgres --backend=postgres` or
  `--backend=both`. Without `TAM_TEST_PG_URL` the fixtures start a pgvector
  container through Docker. See [CONTRIBUTING.md](CONTRIBUTING.md) and
  [.github/workflows/postgres.yml](.github/workflows/postgres.yml).
- **Browser tests** (`tests/browser`, Playwright with Chromium, Firefox and
  WebKit) run in the image built from
  [docker/Dockerfile.browser](docker/Dockerfile.browser); see the `browser`
  job in [.github/workflows/smoke.yml](.github/workflows/smoke.yml).
- **Installed-package smoke test**: `python tests/smoke/installed_runtime.py`
  checks an installed wheel over local and remote MCP.

## Reproducing the benchmarks

The retrieval benchmarks (LoCoMo, LongMemEval, BEAM) need no API key and run
with scripts in [benchmarks/](benchmarks) once the public datasets are
downloaded. Results with the default profile, v13.0.0:

| Benchmark | Metric | Result |
|---|---|---:|
| LongMemEval (470 questions) | R@5 (recall_any) | 95.1% |
| LoCoMo (1,536 questions) | R@5 | 0.607 |
| BEAM, 1M-token scale (625 probes) | R@5 | 0.448 |

Dataset locations, commands, end-to-end (LLM-judged) accuracy, negative
controls, latency and the 14.x studies are in
[docs/benchmarks.md](docs/benchmarks.md).

## Documentation

- [Installation guide](docs/installation.md): all channels, per-platform setup, WSL2, IDE matrix, troubleshooting
- [Setup wizard](docs/SETUP_WIZARD.md): `tam setup` and the team server's web wizard
- [Tools and interfaces](docs/tools.md): the 77 MCP tools, CLI, TypeScript SDK, dashboard
- [Configuration](docs/configuration.md): environment variables, LLM providers, performance tuning
- [Local settings page](docs/LOCAL_SETTINGS.md) and [internal LLM settings](docs/LLM_V14.md)
- [Architecture](docs/architecture.md)
- [Team server quick start](docs/team-server.md), with [dashboard](docs/TEAM_DASHBOARD.md), [PostgreSQL](docs/TEAM_POSTGRES.md), [backup](docs/TEAM_BACKUP.md), [onboarding](docs/TEAM_ONBOARDING.md) and [reports](docs/REPORTS.md)
- [Benchmarks](docs/benchmarks.md) and [comparison with other systems (April 2026 snapshot)](docs/vs-competitors.md)
- [Updating and upgrading](docs/upgrading.md)
- [What is new in 14.x](docs/whats-new.md), [roadmap and v8–v13 history](docs/roadmap.md), [CHANGELOG](CHANGELOG.md)
- [Security policy](SECURITY.md)

## Citation

A paper describing TAM is under review at the Journal of Open Source Software
([paper/paper.md](paper/paper.md)). Until it is published, please cite the
software and the preprint:

> Cherepanov, V. total-agent-memory (version 14.8.0) [software].
> https://github.com/vbcherepanov/total-agent-memory
>
> Preprint: [doi:10.5281/zenodo.23011523](https://doi.org/10.5281/zenodo.23011523)

Citation metadata for the software is in [CITATION.cff](CITATION.cff).

## Contributing

Issues, pull requests and benchmark reproductions are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, the rules for a
pull request and the commit convention. Report security issues privately as
described in [SECURITY.md](SECURITY.md).
Donations to support development: [PayPal](https://PayPal.Me/vbcherepanov).

## License

MIT. See [LICENSE](LICENSE). Third-party licenses are listed in
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).
