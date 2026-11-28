# Team server reference (since 14.0.0)

Separate SQLite stores for each person's personal memory, for each team, and for the shared memory. The server merges the search results the caller is allowed to see. The graphs, indexes and caches of the existing Store/Recall are isolated per process. The local stdio mode is kept.

## Features and permissions

- By default a write is personal; search covers all areas the caller can access. The area is passed as a `scope` structure, for example `{"kind":"team","team_id":"engineering"}`. Topic tags do not widen access.
- A team member has the role `reader`, `editor` or `manager` (a department head: writes like an editor and sees employee activity). Shared memory is read and edited by all authenticated users of this server, including those who have the `reader` role in teams: the shared area is open to the whole company, and the `reader`/`editor`/`manager` roles restrict only team areas. Every edit is written to the history with its author. Administrative changes are made through the local CLI or the `/dashboard/` web panel (organization roles `member`, `company_viewer`, `superadmin`, password login through a one-time invitation code, provider keys); see [TEAM_DASHBOARD.md](TEAM_DASHBOARD.md).
- The author and the client name are taken from the personal token. Only token hashes are stored; a token can be revoked. Membership and the token are checked on every request and before work is sent to a process.
- An edit creates a new version of the record; use the new ID that is returned from then on. The original author is preserved. `expected_revision` protects against overwriting other people's edits. The history includes previous versions, the author, the UTC time, the reason, and the before/after states.
- The record change and the audit entry are made in one transaction. The history is kept after a soft delete. Older records without an author use `Legacy / unknown`.
- `memory_export` and `memory_history` are paginated; `after` is the last record ID or the event sequence number, respectively.
- `memory_save`, `memory_update` and `memory_delete` accept an optional `request_id` (UUID). Repeating the same request from the same user in the same area (`scope`) returns the earlier result. A different payload with this UUID in the same area is rejected. The UUID and the result are stored in the database of that area, in the same transaction as the write; therefore `request_id` accounting works within one area. The same UUID in another area is not recognized: the request is executed again. When repeating after a timeout, keep the original arguments, area and UUID.

The remote catalog currently contains eight tools: `memory_scopes`, `memory_save`, `memory_recall`, `memory_get`, `memory_update`, `memory_delete`, `memory_history`, `memory_export`, the department learning tools `onboarding_*` (see [TEAM_ONBOARDING.md](TEAM_ONBOARDING.md)), and the `memory_report` report (personal, per department or per company; see [REPORTS.md](REPORTS.md)). The local catalog of 79 tools is kept. Arbitrary reads of files on the server machine are not exposed remotely. Moving or publishing an existing record between areas is not part of this catalog yet; an explicit save into a chosen area works.

Authorization uses pre-issued personal Bearer tokens. Interactive OAuth sign-in is not implemented. For a client that cannot set HTTP headers, use the stdio bridge below. Publish an external server behind an HTTPS reverse proxy; HTTP is left for local connections.

## Server from the Python package

After installing the built wheel:

```sh
tam-team --root /path/to/team-data user-add vasya 'Вася'
tam-team --root /path/to/team-data user-add petya 'Петя'
tam-team --root /path/to/team-data team-add engineering 'Разработка'
tam-team --root /path/to/team-data member vasya engineering editor
tam-team --root /path/to/team-data member petya engineering editor
tam-team --root /path/to/team-data token-create vasya --client codex --out /path/to/vasya.token
tam-team --root /path/to/team-data serve --host 127.0.0.1 --port 3738
```

The token is written to a new file with mode 0600; an existing file is not overwritten. Create a separate token for the second user. Revoke: `tam-team --root /path/to/team-data token-revoke --file /path/to/vasya.token`. Remove a membership: `tam-team --root /path/to/team-data member petya engineering remove`. Offboarding: `tam-team --root /path/to/team-data user-disable petya`. The command does not need token files: it revokes all tokens of the user, ends the user's dashboard sessions, invalidates unused invitation codes and blocks sign-in; the last active superadmin cannot be disabled. The Disable button in the dashboard uses the same path. Team records and authorship are kept; the personal area stays on disk and in the backup, but is not available to anyone through the API. `user-enable petya` removes the block; the earlier tokens stay revoked, so a new token or invitation code is needed.

Personal area of a disabled user. `tam-team --root /path/to/team-data user-export petya --out /path/to/petya.jsonl` exports all of the user's personal records (including replaced and deleted ones) with authorship and full history, as well as the undelivered and delivered learning notes from `learning.db`, to JSONL. The file is created with mode 0600; an existing file is not overwritten. The export reads the databases read-only and works while the server is running. `tam-team --root /path/to/team-data user-purge petya --confirm petya` deletes the personal area directory and the learning notes of this user, ends the user's sessions again, invalidates invitation codes and revokes tokens. Both commands work only for a user after `user-disable`. Deletion, like `backup`, requires a stopped server: a running server or a leftover area process is detected by a lock, and the command refuses. The administrator log receives the events `personal_exported` and `personal_purged` without record contents. Team and shared records of this user are stored in other databases and keep the original author. Backups made before the deletion still contain the personal area; delete them or store them separately according to company rules.

From a checkout the same CLI runs as `PYTHONPATH=src python -m team_memory.cli ...`. In PowerShell, first set `$env:PYTHONPATH='src'`, then run `python -m team_memory.cli ...`. These commands were not run against a production installation.

## Docker

A separate profile does not mount the single-user memory volume:

```sh
docker compose -f docker-compose.team.yml config
docker compose -f docker-compose.team.yml build
docker compose -f docker-compose.team.yml run --rm team-memory python /app/src/team_memory/cli.py user-add vasya 'Вася'
docker compose -f docker-compose.team.yml run --rm team-memory python /app/src/team_memory/cli.py token-create vasya --client codex --out /team-data/vasya.token
docker compose -f docker-compose.team.yml up -d
```

Check port 3738 before starting. The web interface and `/mcp/` are on the same port. Parameters: `.env`, `TAM_TEAM_PORT`, `TAM_TEAM_PUBLISH_HOST`, `TAM_TEAM_MAX_WORKERS`, `TAM_TEAM_OPERATION_TIMEOUT`, `TAM_TEAM_QUEUE_TIMEOUT` (wait in the queue for an area, in seconds, 30 by default), and the shared LLM settings. The volumes `tam-team-memory-data` and `tam-team-model-cache` can be renamed with Compose variables. Do not mount an old production database as team-data: moving it needs a separate check by its owner and a backup.

The server profile uses an external reachable model if LLM tasks are enabled. For Ollama the address is set with `OLLAMA_URL`; the profile adds `host.docker.internal:host-gateway` so that the Docker host is reachable on Linux. The setting can be overridden with `TAM_TEAM_HOST_GATEWAY`. The presence of an API adapter does not mean that the chosen model supports images.

## PostgreSQL instead of SQLite

Since 14.6.0 the team server can keep all data in an external PostgreSQL 17+ (one database per organization, a schema and a dedicated LOGIN role for each area, and the control schemas `tam_control` and `tam_learning`). A personal installation always stays on SQLite. Choose it in the **Database** step of the web wizard, in **Administration → Settings → Database**, or with `TAM_TEAM_DATABASE_URL` (a DSN set in the dashboard takes priority). An existing SQLite server is migrated with the **Migrate** button (dry run, maintenance mode, copy, SHA-256 verification of every table, switch-over, SQLite archive in `archive/sqlite-<ts>/`, rollback confirmed by typing the organization name) or, without the web interface, with the server stopped:

```sh
export TAM_TEAM_NEW_DSN='postgresql://tam:...@db.example.internal/tam?sslmode=verify-full'
tam-team --root /path/to/team-data db-check --dsn-env TAM_TEAM_NEW_DSN
tam-team --root /path/to/team-data db-migrate --dsn-env TAM_TEAM_NEW_DSN --dry-run
tam-team --root /path/to/team-data db-migrate --dsn-env TAM_TEAM_NEW_DSN
```

The DSN is passed only by the name of an environment variable. Rows that violate SQLite foreign keys are not lost: they go to the `migration_quarantine` table of their area, and the dry run shows their number and sample keys in advance. Server requirements, the SQL for the database administrator, the table of managed PostgreSQL services (RDS, Cloud SQL, Azure and others), limitations and ranking differences: [TEAM_POSTGRES.md](TEAM_POSTGRES.md). For a trial run: `docker compose -f docker-compose.team.yml -f docker-compose.team.postgres.yml up -d` (not for production).

## Backup and restore

SQLite: stop the server, then run:

```sh
tam-team --root /path/to/team-data backup --out /path/to/new-backup
tam-team --root /path/to/new-team-data restore --from /path/to/new-backup
```

In Docker, pass the same subcommands to `python /app/src/team_memory/cli.py` through `docker compose -f docker-compose.team.yml run --rm team-memory ...` after stopping the service; the backup path must be on a separately mounted volume.

The backup contains identity.db and the SQLite databases of all areas, including authorship, history and idempotency. The manifest contains SHA256; SQLite integrity and foreign keys are checked. An active server or a leftover workspace process blocks offline copying. Restore accepts only a new directory and publishes it after a full check; a failed copy does not leave a partially restored server. Models, client token files and logs are not part of the backup. Store client tokens separately: the access hashes are restored from identity.db.

PostgreSQL: the same commands work without stopping the server. `backup` runs `pg_dump` of the TAM schemas from a single snapshot and writes a version 2 manifest with SHA-256 and the row count of each table; `restore --from DIR --dsn-env VAR` restores into an empty database, compares the row counts and re-creates the area roles (roles are not part of the dump). It needs `pg_dump`/`pg_restore` no older than the server (the Docker image has a PostgreSQL 18 client) and the same master key. For continuous protection use the provider's PITR or WAL archiving.

Continuous replication (SQLite only) without stopping the server, and point-in-time restore (`--timestamp`), are done with Litestream: `tam-team replication config|prepare|status|restore|drop|init-bucket`, the `litestream` sidecar in the Compose profile `litestream`, and local S3 storage in the profile `s3-local`. After `user-purge`, the replica of the deleted personal area must be removed with `tam-team replication drop --user <id> --confirm <id>`: Litestream does not delete it by itself. Details, the `TAM_TEAM_REPLICA_*` variables, WAL and retention periods: [TEAM_BACKUP.md](TEAM_BACKUP.md).

## Lightweight remote client

`src/team_memory/remote.py` uses only the Python standard library. It can be copied on its own to macOS/Linux/Windows/WSL: the client needs no models, embedding packages, Docker or background services.

Client variables: `TAM_REMOTE_URL` is the final HTTPS `/mcp/` endpoint, `TAM_REMOTE_TOKEN_FILE` is the path to the personal token. The token is re-read for every request. Redirects are disabled so that the token is not sent to another address. Local HTTP is allowed only for loopback.

Example stdio configuration for an IDE that supports `mcpServers`:

```json
{
  "mcpServers": {
    "total-agent-memory": {
      "command": "python3",
      "args": ["/absolute/path/remote.py"],
      "env": {
        "TAM_REMOTE_URL": "https://YOUR_SERVER/mcp/",
        "TAM_REMOTE_TOKEN_FILE": "/absolute/path/personal.token"
      }
    }
  }
}
```

On Windows use the absolute path to `python.exe` and Windows paths. For Codex, the same command/args/env are moved into the TOML table `mcp_servers.total-agent-memory`; follow the format of the specific client. The package also provides the `tam-remote` command.

## Limits and diagnostics

The default number of warm processes is 3: the personal area, one team and the shared area stay loaded at the same time. Operations run sequentially. With a lower limit or more areas, search first uses the processes that are already loaded and then opens the rest; the order of results does not change because of this. Old processes are evicted when TAM_TEAM_MAX_WORKERS is reached. More warm processes need more RAM; a lower limit increases cold loads and CPU. On a timeout the server does not retry a write automatically: check the history before repeating, because the operation may have completed.

For a configuration with three warm MiniLM processes, plan for at least 4 GiB of RAM on the server and check consumption on your own database. In a Linux ARM64 micro-check the sum of worker RSS was 1975 MiB with 3 processes and 1315 MiB with 2; the RSS sum counts shared pages repeatedly and does not include a separate reserve for the gateway, database and OS. On a small machine set TAM_TEAM_MAX_WORKERS=1 or 2 and account for the cost of cold loads. Zero CPU load during search is not promised.

The cross-encoder (`MEMORY_CROSS_RERANK`, `auto` by default) runs once per search, in the server process: each area returns its own candidates without reranking (window `MEMORY_CROSS_RERANK_WINDOW`, 50), the server merges the windows of only those areas the caller is allowed to read, reranks the merged window and keeps `limit` records. The model is loaded once, lazily, in the server process; area processes do not load it. While the model is not ready (`auto`), on an error, or when `TAM_TEAM_OPERATION_TIMEOUT` expires, the merge order is kept. The response contains `ordering` (`cross_rerank_then_score`) and `rerank` (`applied`, `not_ready`, `skipped`, `failed`, `timeout`). With `MEMORY_CROSS_RERANK=off` the merge is as before: rank within the area, then score.

A request counter and a latency histogram are kept in the service; each call writes a structured event without the memory text and the token. The public health endpoint does not show the data path or users.

Web interface: sign-in with a token, area selection, search, browsing pages of records, saving, editing and history. The token lives only in the page memory. 12 scenarios passed in Chromium/Firefox/WebKit, including the local dashboard. Native installations and background services on Ubuntu/macOS/Windows were checked; an extended CI matrix remains a separate stage. Details: [checks](benchmarks/browser-cpu-v14-20260915/RESULTS.md).
