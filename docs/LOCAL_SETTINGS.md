# Personal settings, privacy and erasure

This page covers a personal install (`tam setup` → "Just me"). The team server has its own
**Administration → Settings** page; see [TEAM_DASHBOARD.md](TEAM_DASHBOARD.md).

## The Settings page

Open the local dashboard (`http://127.0.0.1:37737` by default) and choose **Settings**.

| Section | Settings |
|---|---|
| Language model | Provider (Ollama, OpenAI, Anthropic, OpenAI-compatible, auto), model, API base URL, API key, LLM tasks on/off, timeout |
| Embeddings | Provider. OpenAI, Cohere and DashScope take a model, key, base URL and dimensions. The local FastEmbed model is a setup preset: change it with `tam setup --reconfigure`, then run `memory_rebuild_embeddings` |
| Search answers | Characters per search result (`MEMORY_RECALL_MAX_RESULT_CHARS`, default 6000), flag records that address the agent (`MEMORY_FLAG_INSTRUCTIONS`, default on), cross-encoder re-ranking (`MEMORY_CROSS_RERANK`) |
| Storage | Days to keep raw call logs (`MEMORY_RAW_LOG_RETENTION_DAYS`, not set = forever), embedding model cache folder (`TAM_MODEL_CACHE`), days to keep the agent action log (`TAM_ACTION_LOG_RETENTION_DAYS`, default 30) |
| Agent interface | Tool set offered to agents (`TAM_TOOL_PROFILE`: `full` default, `lean`, `minimal`), usage instructions sent on connect (`TAM_MCP_INSTRUCTIONS`, default on), per-tool call statistics (`TAM_TOOL_TELEMETRY`, default on); ambient memory through the hooks: recall with every prompt (`TAM_AMBIENT_RECALL`, its budget `TAM_AMBIENT_TOKEN_BUDGET` and time limit `TAM_AMBIENT_LATENCY_MS`), rule reminders before commands and edits (`TAM_RULE_REMINDERS`), the end-of-turn rule check (`TAM_RULE_COMPLIANCE`), turn summaries (`TAM_TURN_AUTOSAVE`), rule drafts from user corrections (`TAM_CORRECTION_CAPTURE`) and the agent action log (`TAM_ACTION_LOG`), all default on — see [configuration.md](configuration.md#ambient-memory-hooks) |
| Stored credentials | Scan the store for credentials saved by earlier versions, then back up and redact them |

**Precedence.** A value saved on this page overrides the environment and the `env` block of an MCP
client config, which override the built-in default. Each field shows where its current value comes from.

**When changes apply.** Every agent session reads the settings when its MCP server starts. Restart the
agent, or run `/mcp` → reconnect in Claude Code, after a change.

**Where values live.** `<memory dir>/settings.json`, mode 0600. API keys in it are Fernet tokens, encrypted
with `TAM_MASTER_KEY` when that is set, otherwise with `<memory dir>/master.key` (created 0600 on the
first saved key). The page never receives a key back: it shows `••••` and the last four characters.
Keep a copy of `master.key` with your backups. Without it the saved keys cannot be read and must be
entered again.

**Who can change them.** The page's writes need a same-origin request that carries a token issued to that
page. Other web sites and plain form posts are refused, as are writes to any other dashboard address.

## Secrets never reach disk

Every tool call's arguments pass through one redaction list (`src/secret_redaction.py`) before the raw
call log or any table sees them. The same applies to prompts captured by the `UserPromptSubmit` hook, tool
output queued by the `PostToolUse` hook, and transcript extraction. The list covers:

- PEM private keys and passwords in URLs;
- Anthropic, OpenAI, Stripe, GitHub, GitLab, Slack, Google, Hugging Face, npm, Telegram and AWS keys;
- JWTs, bearer headers, and `password=` / `*_secret:` / `token=` / `api_key=` assignments;
- e-mail addresses and payment card numbers.

Each match becomes `[REDACTED]`. Write text between `<private>` and `</private>` to keep other passages
out of memory.

On POSIX the memory directory is made owner-only on start: the directory becomes 0700, and `memory.db`,
`settings.json` and `master.key` become 0600.

### Credentials saved by earlier versions

```bash
tam redact-existing            # report only: rows per table and raw logs that hold credentials
tam redact-existing --apply    # back up to backups/pre-redact-<time>*.db, then redact
```

The dashboard's **Scan memory** and **Back up and redact** buttons do the same. The backup keeps the old
values, so delete it once you have checked the store. Backups made before the run are listed too, since
they may hold credentials as well.

## Search answers stay small

`memory_recall` and `memory_search_fast` cut a record longer than the per-result limit. The cut record
carries `truncated: true` and `content_chars`, and its text ends with a pointer to
`memory_get(ids=[…])`, which returns the whole record. Evals and `memory_answer` read full records either way.

A record that addresses the reading agent rather than stating a fact is marked
`untrusted_instructions: true`, for example "ignore previous instructions" or "send the SSH key to …".
Once a result holds such a record, it also carries a `notice` telling the agent to treat it as data. Nothing is
removed. The team server applies both rules with its own settings.

## Agent interface

`TAM_TOOL_PROFILE` picks the catalogue `tools/list` returns. `full` (the default) is every tool with its
long description, as the Claude Code plugin and its skill expect. `lean` lists 28 everyday tools and `minimal`
the 12 core ones (recall, save, get, update, delete, session start and end, rules, file context, learn_error,
kg_add_fact); both use one-line descriptions with an inline signature and drop per-parameter descriptions
from the schemas. A tool the profile hides still answers when called, so older prompts keep working. An
unknown value stops the server at startup. `scripts/tool_catalogue_size.py` prints the size of each catalogue.

`TAM_MCP_INSTRUCTIONS=false` stops the server from sending its short usage guide (read first, what to save,
what not to save, and the server start date for resolving relative dates) when a client connects.

With `TAM_TOOL_TELEMETRY` on, every tool call adds a row to `tool_calls`: tool name, duration, outcome, error
class and the byte sizes of arguments and result, never their contents. Rows are written in batches and
when the server exits, and are kept 7 days. `memory_perf_report` returns per-tool count, p50, p95 and error
rate for the last 24 hours under `tool_calls`.

## Erasing a record

`memory_delete(id)` hides a record: it leaves search and the vector index but stays in the database.
`memory_delete(id, hard=true)` erases it for good, together with:

- every earlier version it replaced;
- its vectors in every embedding space;
- its full-text, atomic-fact, graph-link, evidence-passage, queue and log rows;
- quotes of it inside later versions, which become `[erased]`;
- copies of its text in the raw call logs and in Chroma's write queue.

Full-text indexes are merged, freed database pages are zeroed, the database is rebuilt with `VACUUM`
(this needs free disk space of about the database size), the write-ahead log is truncated, Chroma's
database is vacuumed and the unused part of its vector files is zeroed. If another process is writing
and SQLite refuses the rebuild, the record is still erased and the result carries `sqlite_not_compacted`:
old bytes may then stay in the file's free space until the next hard delete rebuilds the database.
Search queries that mention the
record are not rewritten; set a raw-log retention period to limit how long those are kept. Hard erasure
is for personal (SQLite) memory. Team records keep their audited history and are removed through
offboarding.
