# Claude Total Memory

**Persistent memory for Claude Code across sessions.**

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)
![MCP Server](https://img.shields.io/badge/MCP-Server-purple.svg)
![Version 2.2](https://img.shields.io/badge/Version-2.2.0-orange.svg)

An MCP server that gives Claude Code a persistent, searchable memory. It stores decisions, solutions, lessons, facts, and conventions -- then retrieves them automatically across sessions using a 4-tier search pipeline.

---

## The Problem

Claude Code starts every session with a blank slate. Yesterday you spent an hour debugging a Docker networking issue and found the fix. Today, Claude has no idea it happened. You explained your project architecture last week. Claude forgot.

This means you repeat yourself, re-explain decisions, re-discover solutions, and lose the compounding value of working with an AI assistant over time.

## The Solution

Claude Total Memory is an MCP server that runs alongside Claude Code. It provides 13 tools for saving and retrieving knowledge. When Claude saves a decision, a bug fix, or a project convention, that knowledge persists in a local database. Next session, Claude searches memory before starting work and builds on what it already knows.

No cloud services. No API keys. Everything stays on your machine.

---

## Features

**Search and Retrieval**
- 4-tier search pipeline: FTS5 keyword (BM25) -> semantic (ChromaDB) -> fuzzy (SequenceMatcher) -> graph expansion
- Decay scoring: recent knowledge ranks higher, stale knowledge fades
- Spaced repetition: frequently recalled knowledge gets boosted
- Progressive disclosure: summary mode (150 chars) saves tokens, full mode returns everything

**Knowledge Management**
- Five knowledge types: decision, solution, lesson, fact, convention
- Automatic deduplication via Jaccard + fuzzy similarity (thresholds: 0.85 / 0.90)
- Version history with supersession chains
- Knowledge graph with typed relations between records
- Tag-based browsing and filtering

**Lifecycle and Maintenance**
- Retention zones: active -> archived (180d) -> purged (365d)
- Consolidation: find and merge similar records
- Full JSON export for backup and migration
- Session transcript extraction for post-session knowledge mining

**Dashboard**
- Web UI at `localhost:37737`
- Statistics, health score, knowledge table, session browser
- Interactive knowledge graph visualization
- Read-only -- safe to leave running

---

## Quick Start

### Option A: One-Command Install

**macOS / Linux:**

```bash
git clone https://github.com/vbcherepanov/claude-total-memory.git
cd claude-total-memory
bash install.sh
```

**Windows (PowerShell):**

```powershell
git clone https://github.com/vbcherepanov/claude-total-memory.git
cd claude-total-memory
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer creates a Python venv, installs dependencies, downloads the embedding model, and configures Claude Code automatically.

### Option B: Manual Setup

**1. Clone and install dependencies**

macOS / Linux:

```bash
git clone https://github.com/vbcherepanov/claude-total-memory.git
cd claude-total-memory
python3 -m venv .venv
source .venv/bin/activate
pip install "mcp[cli]>=1.0.0" chromadb sentence-transformers
```

Windows (PowerShell):

```powershell
git clone https://github.com/vbcherepanov/claude-total-memory.git
cd claude-total-memory
python -m venv .venv
.venv\Scripts\activate
pip install "mcp[cli]>=1.0.0" chromadb sentence-transformers
```

**2. Configure Claude Code**

Edit `~/.claude/settings.json` (macOS/Linux) or `%USERPROFILE%\.claude\settings.json` (Windows) and add the MCP server. All paths must be absolute:

macOS / Linux:

```json
{
  "mcpServers": {
    "memory": {
      "command": "/FULL/PATH/TO/claude-total-memory/.venv/bin/python",
      "args": ["/FULL/PATH/TO/claude-total-memory/src/server.py"],
      "env": {
        "CLAUDE_MEMORY_DIR": "/Users/yourname/.claude-memory",
        "EMBEDDING_MODEL": "all-MiniLM-L6-v2"
      }
    }
  }
}
```

Windows:

```json
{
  "mcpServers": {
    "memory": {
      "command": "C:/Users/yourname/claude-total-memory/.venv/Scripts/python.exe",
      "args": ["C:/Users/yourname/claude-total-memory/src/server.py"],
      "env": {
        "CLAUDE_MEMORY_DIR": "C:/Users/yourname/.claude-memory",
        "EMBEDDING_MODEL": "all-MiniLM-L6-v2"
      }
    }
  }
}
```

> **Important:** MCP server configuration does not support `~`, `$HOME`, or `%USERPROFILE%` in paths. You must use fully expanded absolute paths. On Windows, use forward slashes (`/`) in JSON paths -- they work correctly with Python.

**3. Verify**

Restart Claude Code. You should see `memory` listed in the MCP servers. Run `memory_stats()` to confirm the connection.

---

## MCP Tools

### Core (4 tools)

| Tool | Description |
|------|-------------|
| `memory_recall` | Search all past knowledge. 4-tier search with decay scoring. Use before starting any task. |
| `memory_save` | Save knowledge with type, project, tags, and context. Auto-deduplicates. |
| `memory_update` | Find existing knowledge by search query, supersede it, create a new version. |
| `memory_search_by_tag` | Browse all active knowledge matching a tag (partial match supported). |

### Browsing and Analytics (3 tools)

| Tool | Description |
|------|-------------|
| `memory_timeline` | Browse session history by number, date range, or keyword search. |
| `memory_stats` | View statistics: knowledge counts, health score, storage size, config. |
| `memory_export` | Export all knowledge as JSON for backup or migration. |

### Lifecycle (3 tools)

| Tool | Description |
|------|-------------|
| `memory_consolidate` | Find and merge duplicate/similar records. Supports dry run preview. |
| `memory_forget` | Apply retention policy: archive stale records, purge old archives. |
| `memory_delete` | Soft-delete a knowledge record. Removes from search and vector store. |

### Graph and Versioning (2 tools)

| Tool | Description |
|------|-------------|
| `memory_relate` | Create typed relations between records (causal, solution, context, related, contradicts). |
| `memory_history` | View the version chain for a record -- walk through how knowledge evolved. |

### Extraction (1 tool)

| Tool | Description |
|------|-------------|
| `memory_extract_session` | Process pending session transcripts. List, read, and mark as complete. |

---

## How It Works

### Search Pipeline

When `memory_recall` is called, the query passes through four tiers:

```
Query: "docker networking between containers"
             |
             v
  +---------------------+
  | Tier 1: FTS5 + BM25 |  Keyword search with relevance ranking
  +---------------------+
             |
             v
  +---------------------+
  | Tier 2: Semantic     |  ChromaDB cosine similarity on embeddings
  +---------------------+
             |
             v
  +---------------------+
  | Tier 3: Fuzzy        |  SequenceMatcher for typos and partial matches
  +---------------------+
             |
             v
  +---------------------+
  | Tier 4: Graph        |  Follow relations from top 5 results (1 hop)
  +---------------------+
             |
             v
  +---------------------+
  | Decay + Rank + Boost |  Apply time decay, recall boost, final sort
  +---------------------+
             |
             v
        Top N results
```

Results from all tiers are merged. Records found by multiple tiers receive combined scores.

### Decay Scoring

Knowledge decays over time unless confirmed or recalled:

```
score = base_score * e^(-days * ln(2) / half_life) + recall_boost
```

- `half_life`: 90 days (configurable via `DECAY_HALF_LIFE`)
- `recall_boost`: `min(0.3, recall_count * 0.05)` -- frequently used knowledge stays relevant
- Records confirmed on last recall get their `last_confirmed` timestamp refreshed

### Retention Zones

```
                  180 days               365 days
    Active --------+-----> Archived ------+-----> Purged
                   |                      |
        (unrecalled, low confidence)  (all archived)
```

- **Active**: searchable, fully available
- **Archived**: removed from search, still in database
- **Purged**: marked for cleanup

Only records with `recall_count = 0` and `confidence < 0.8` are candidates for archival.

### Deduplication

On every `memory_save`, the server checks for existing similar knowledge:

1. FTS5 search for candidate matches (top 5)
2. Jaccard similarity > 0.85 -- deduplicated
3. Fuzzy ratio > 0.90 -- deduplicated

When a duplicate is found, the existing record's `last_confirmed` timestamp is refreshed instead of creating a new record.

---

## Web Dashboard

Start the dashboard:

macOS / Linux:

```bash
cd claude-total-memory
.venv/bin/python src/dashboard.py
```

Windows:

```powershell
cd claude-total-memory
.venv\Scripts\python.exe src\dashboard.py
```

Open [http://localhost:37737](http://localhost:37737) in your browser.

The dashboard provides:

- **Statistics**: total knowledge, sessions, projects, health score, storage size
- **Knowledge table**: searchable and filterable list of all active records with detail modal
- **Sessions**: chronological session history with knowledge counts
- **Graph**: interactive force-directed visualization of knowledge relations

The dashboard is read-only and connects to the same SQLite database used by the MCP server (via WAL mode for safe concurrent access).

---

## Configuration

All configuration is via environment variables set in the MCP server config:

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_MEMORY_DIR` | `~/.claude-memory` | Root directory for all storage |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence transformer model for semantic search |
| `DECAY_HALF_LIFE` | `90` | Days until knowledge score decays to 50% |
| `ARCHIVE_AFTER_DAYS` | `180` | Days before unrecalled records are archived |
| `PURGE_AFTER_DAYS` | `365` | Days before archived records are purged |
| `DASHBOARD_PORT` | `37737` | HTTP port for the web dashboard |

---

## CLAUDE.md Integration

Copy `CLAUDE.md.template` to your project root as `CLAUDE.md` to instruct Claude to use memory automatically:

```bash
cp CLAUDE.md.template /path/to/your/project/CLAUDE.md
```

The template includes instructions for:
- Searching memory before starting tasks
- Saving decisions with context
- Using all knowledge types correctly
- Browsing history and running maintenance

Customize the `project` parameter in the template to match your project name.

---

## Hooks (Optional)

Ready-to-use hooks are provided in the `hooks/` directory:

| Hook | macOS/Linux | Windows | What it does |
|------|-------------|---------|--------------|
| SessionStart | `hooks/session-start.sh` | `hooks/session-start.ps1` | Reminds Claude to use `memory_recall` |
| Stop | `hooks/on-stop.sh` | `hooks/on-stop.ps1` | Reminds Claude to save knowledge |
| PostToolUse:Bash | `hooks/memory-trigger.sh` | `hooks/memory-trigger.ps1` | Suggests `memory_save` after git/docker |

**macOS / Linux** -- add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "/FULL/PATH/TO/claude-total-memory/hooks/session-start.sh"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "/FULL/PATH/TO/claude-total-memory/hooks/on-stop.sh"
      }
    ],
    "PostToolUse": [
      {
        "type": "command",
        "command": "/FULL/PATH/TO/claude-total-memory/hooks/memory-trigger.sh",
        "matcher": "Bash"
      }
    ]
  }
}
```

**Windows** -- add to `%USERPROFILE%\.claude\settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "type": "command",
        "command": "powershell -ExecutionPolicy Bypass -File C:/Users/yourname/claude-total-memory/hooks/session-start.ps1"
      }
    ],
    "Stop": [
      {
        "type": "command",
        "command": "powershell -ExecutionPolicy Bypass -File C:/Users/yourname/claude-total-memory/hooks/on-stop.ps1"
      }
    ],
    "PostToolUse": [
      {
        "type": "command",
        "command": "powershell -ExecutionPolicy Bypass -File C:/Users/yourname/claude-total-memory/hooks/memory-trigger.ps1",
        "matcher": "Bash"
      }
    ]
  }
}
```

You can also add a session-end hook that calls `extract_transcript.py` to compress the session transcript and queue it for knowledge extraction on the next session start:

```bash
# macOS / Linux
python3 /FULL/PATH/TO/claude-total-memory/src/extract_transcript.py \
  --transcript "$TRANSCRIPT_PATH" \
  --session-id "$SESSION_ID" \
  --output-dir ~/.claude-memory/extract-queue \
  --db ~/.claude-memory/memory.db
```

The transcript extractor:
- Compresses transcripts to under 200 KB
- Redacts sensitive data (API keys, tokens, secrets)
- Auto-saves a session summary directly to the database
- Queues the full transcript for detailed extraction via `memory_extract_session`

---

## Storage Structure

```
~/.claude-memory/
  memory.db              SQLite database (FTS5, knowledge, sessions, relations)
  raw/                   Raw JSONL session logs
    mcp_20260215_*.jsonl
  chroma/                ChromaDB vector store (semantic embeddings)
  transcripts/           Archived session transcripts
  extract-queue/         Pending/completed transcript extractions
    pending-*.json
    done-*.json
  backups/               JSON exports from memory_export
    export_all_*.json
```

Typical storage sizes after moderate use:

| Component | Approximate Size |
|-----------|-----------------|
| SQLite (memory.db) | 1-10 MB |
| ChromaDB vectors | 10-50 MB |
| Raw logs | 5-20 MB |
| Transcripts | 1-10 MB |

---

## Knowledge Types

| Type | When to Use | Example |
|------|------------|---------|
| `decision` | Architectural or design choice. **Always include WHY in context.** | "Chose pgx over database/sql for connection pooling and pgx-specific features" |
| `solution` | Bug fix, workaround, or resolution to a problem. | "Fixed Bitrix24 batch timeout by chunking requests to 50 items with 200ms delay" |
| `lesson` | Gotcha, pitfall, or unexpected behavior discovered. | "Docker Compose v2 requires `depends_on.condition: service_healthy` -- silent failure without it" |
| `fact` | Configuration, version, endpoint, or objective information. | "Production PostgreSQL 18 on port 5433, max_connections=200" |
| `convention` | Project pattern, coding standard, or team agreement. | "All DTOs must be final readonly classes with constructor promotion" |

---

## Relation Types

| Type | Meaning | Example |
|------|---------|---------|
| `causal` | A caused or led to B | "Timeout error (A) caused us to implement chunking (B)" |
| `solution` | B is the solution to A | "Memory leak (A) was solved by connection pooling (B)" |
| `context` | B provides context for A | "API rate limits (B) explain why we use queues (A)" |
| `related` | A and B are related | "Docker config (A) relates to CI/CD pipeline (B)" |
| `contradicts` | A contradicts B | "Old API docs (A) contradict actual behavior (B)" |

---

## Architecture

The server is a single-file MCP server (`src/server.py`, ~1200 lines) built on:

- **MCP SDK** (`mcp` package): protocol implementation and stdio transport
- **SQLite FTS5**: full-text search with BM25 scoring, triggers for index sync
- **ChromaDB**: persistent vector store with cosine similarity search
- **sentence-transformers**: local embedding model (`all-MiniLM-L6-v2`, 384d)

```
Claude Code
    |
    | (MCP protocol over stdio)
    v
+---------------------------+
|  MCP Server (server.py)   |
|                           |
|  +--------+  +---------+  |
|  | Store   |  | Recall  |  |
|  | (write) |  | (read)  |  |
|  +----+----+  +----+----+  |
|       |            |        |
|  +----v------------v----+   |
|  |   SQLite FTS5        |   |
|  |   + ChromaDB         |   |
|  |   + Relations Graph  |   |
|  +-----------------------+  |
+---------------------------+
```

- **Store**: handles writes -- save, update, delete, consolidate, retention, dedup
- **Recall**: handles reads -- 4-tier search, timeline browsing, statistics
- **Dashboard** (`src/dashboard.py`): standalone HTTP server using Python stdlib, read-only SQLite access

The server creates a new session ID on each startup and logs all tool calls to raw JSONL files for auditability.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
