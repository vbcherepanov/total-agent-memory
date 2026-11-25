# Team server on PostgreSQL

The team server stores its data in SQLite by default: `identity.db`, `learning.db` and one `memory.db` per workspace under the data directory. From 14.6.0 it can keep all of that in an external PostgreSQL database instead. PostgreSQL is worth it when your organization already runs it (managed backups, point-in-time recovery, monitoring, replicas) or when the data directory should not hold the company's memory.

The personal install (`tam` on a laptop) always stays on SQLite. Nothing on this page changes it.

PostgreSQL is **external**: your DBA or cloud provider installs and runs it. TAM never creates the database, never installs extensions as a superuser and never changes server settings. When something is missing, the dashboard's **Test** button and `tam-team db-check` say what, with the exact SQL for the DBA.

## How the data is laid out

One PostgreSQL database per organization:

| Schema | Holds | Owner |
|---|---|---|
| `tam_control` | users, departments, membership, token and password hashes, dashboard sessions, audit log, encrypted provider settings, installation id, workspace map | TAM role |
| `tam_learning` | onboarding curricula, progress, quiz attempts, notes waiting for personal memory | TAM role |
| `tam_compat` | SQLite-compatible SQL functions (`strftime`, `julianday`, ...) | TAM role |
| `ws_<48 hex>` | one workspace: company-wide, a department or a person | that workspace's own LOGIN role `wsr_<48 hex>` |
| `extensions` | `vector` | DBA or TAM role |

Each workspace schema is owned by its own LOGIN role that has no rights on any other workspace schema, so a department's worker process cannot read another department's memory even through a SQL bug: PostgreSQL answers `permission denied`. Schema and role names are hashes because workspace keys are longer than PostgreSQL's 63-byte identifier limit; `tam_control.workspace_schemas` maps them back. Role passwords are never stored: each is an HMAC of the role name under the server's master key (`master.key` or `TAM_TEAM_MASTER_KEY`), and the server resets it whenever it provisions the workspace.

Workspaces are created lazily, on the first request that needs them, just like `memory.db` files today.

## Requirements

| | Required | Why |
|---|---|---|
| PostgreSQL | 17 or newer (CI runs 18) | the builtin `C.UTF-8` locale provider |
| TAM server | the `[postgres]` extra: `pip install "total-agent-memory[postgres]"` (the Docker image already has it) | the PostgreSQL driver (psycopg) and the pgvector adapter; without it `tam-team` says so and exits |
| Database | `ENCODING 'UTF8'`, `LOCALE_PROVIDER builtin`, `BUILTIN_LOCALE 'C.UTF-8'` (libc `C`/`POSIX` collation also passes) | bytewise text ordering, identical to SQLite; any other collation changes `ORDER BY` and comparisons |
| Extension | `vector` (pgvector; tested with 0.8.1), in schema `extensions`, usable by everyone | embeddings |
| TAM role | `LOGIN`, `CREATEROLE`, `CREATE` on the database; owner of the database, or `CONNECT` and `TEMPORARY` on it `WITH GRANT OPTION` when `PUBLIC` does not hold them | one role and one schema per workspace; TAM grants each workspace role `CONNECT` and `TEMPORARY` |
| Connections | direct or session-mode pooling; **not** transaction-mode poolers (PgBouncer `pool_mode=transaction`, Supabase port 6543, Neon "pooled" endpoints) | TAM uses session settings, advisory locks and per-workspace roles |
| Network | same host, VPC or availability zone as the server; TLS (`sslmode=require` or `verify-full`) for any non-local host | a save issues about 57 SQL statements and a recall about 58: every millisecond of round trip adds roughly 60 ms to each |

Connection budget: each worker holds one connection as its workspace role (a workspace role may open at most `TAM_TEAM_PG_WORKSPACE_CONNECTION_LIMIT`, default 4), plus the control-plane pool (`TAM_TEAM_PG_POOL_MAX_SIZE`, default 8), plus one lease connection that marks the server as running. With `TAM_TEAM_MAX_WORKERS=3` plan for about 15 connections.

Only one team server may run against a database. A second server, even on another host, refuses to start (PostgreSQL advisory lock).

### SQL for the DBA

Run as a superuser or the provider's admin role. Replace the password; `CREATE ROLE ... PASSWORD` should be run from a session whose log statement setting does not record it.

```sql
CREATE ROLE tam LOGIN CREATEROLE PASSWORD 'change-me';
CREATE DATABASE tam OWNER tam TEMPLATE template0 ENCODING 'UTF8'
    LOCALE_PROVIDER builtin BUILTIN_LOCALE 'C.UTF-8';
REVOKE ALL ON DATABASE tam FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE tam TO tam;

\connect tam
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

If the TAM role may create extensions itself (it owns the database and the provider allows `vector` for non-superusers), the three `extensions` lines are optional: TAM creates them on first start. If the extension already exists in another schema, move it: `ALTER EXTENSION vector SET SCHEMA extensions;`.

The DSN for the server is then `postgresql://tam:change-me@db.example.internal:5432/tam?sslmode=verify-full&sslrootcert=/etc/ssl/db-ca.pem`.

### Managed PostgreSQL

The checks are the same everywhere; what differs is who may do what. Always confirm with the **Test** button: it checks every row of the requirements table against the real server.

| Provider | Admin role for the DBA SQL | Extensions | Notes |
|---|---|---|---|
| Amazon RDS for PostgreSQL, Aurora PostgreSQL | the master user (member of `rds_superuser`; has `CREATEROLE`) | `vector` is on the supported list; create it as the master user | Engine 17 or newer. Use `sslmode=verify-full` with the RDS CA bundle. RDS Proxy pins sessions that use session settings; connect to the instance endpoint instead. |
| Google Cloud SQL for PostgreSQL | `postgres` (member of `cloudsqlsuperuser`; has `CREATEROLE`) | `vector` supported; create as `postgres` | Version 17 or newer. Connect through the Cloud SQL Auth Proxy (then `sslmode=disable` to the local proxy is fine) or with server certificates. |
| Azure Database for PostgreSQL (Flexible Server) | the server admin (member of `azure_pg_admin`; has `CREATEROLE`) | allow-list it first: server parameter `azure.extensions` must include `VECTOR`; then `CREATE EXTENSION` as the admin | Version 17 or newer. The built-in PgBouncer runs in transaction mode: use port 5432, not 6432. |
| Others (Supabase, Neon, Crunchy Bridge, DigitalOcean, Aiven, self-hosted) | the role the provider gives you, as long as it has `CREATEROLE` and may create databases | `vector` is available on all of these; Supabase already has an `extensions` schema | Use the direct or session-mode endpoint, never a transaction pooler. If the provider does not let you create a database with the builtin locale, ask for one with libc collation `C`. |

## Choosing PostgreSQL

### New installation

Pick **PostgreSQL** in the setup wizard's **Database** step (right after the setup code, before the company and the first administrator), paste the DSN and press **Test**. When every check passes, **Continue** stores the DSN encrypted and moves the (still empty) installation to PostgreSQL before anything else is written.

Headless alternative: set `TAM_TEAM_DATABASE_URL` before the first start. An empty PostgreSQL database with no SQLite data in the data directory starts as a new installation.

### Existing SQLite installation

Dashboard: **Administration → Settings → Database** (superadmin only).

1. Choose **PostgreSQL**, paste the DSN, press **Test**. The DSN is not stored by a test.
2. **Dry run** writes nothing to the target. It repeats the checks, confirms the target is empty (or holds an interrupted migration of this same installation), counts rows and bytes of every database and table, estimates the time, and lists blockers: failed checks, a database that belongs to another TAM installation, and rows that break a declared foreign key in SQLite.
3. **Migrate** (confirm). The server enters maintenance mode: MCP calls and every mutating dashboard call get `503` with `Retry-After`; signing in, signing out and the migration panel keep working. Workers stop. The progress panel shows the phase, the database and table being copied, rows copied, and workspaces done.
4. The copy runs in this order: identity and learning into the control schemas, then each workspace in its own transaction. SQLite column values are converted to the PostgreSQL types, ids are kept, identity sequences continue after the highest id (and after SQLite's `AUTOINCREMENT` high-water mark, so deleted ids are never reused), embeddings get their `vector` column, and the full-text side tables and BM25 statistics are rebuilt from the copied rows. Counters the empty schema starts with (`privacy_counters`, `vector_index_revision`) take the SQLite values; the migration ledger is not copied, but every SQLite migration version must be known to this build (a workspace written by a newer TAM blocks the dry run).
5. **Verification**: for every table, the row count and a SHA-256 over all rows (canonical values, ordered by primary key) must match between the SQLite snapshot and PostgreSQL. Any difference fails the migration and names the table.
6. **Activation**: `database.json` is written (generation + 1, the previous configuration kept for rollback), the control plane switches to PostgreSQL, workers restart on PostgreSQL, maintenance ends, and the SQLite files move to `archive/sqlite-<timestamp>/` in the data directory so nothing opens them again. Dashboard sessions moved with the data: you stay signed in.

**Cancel** is possible until activation. A cancel or a failure leaves `database.json` and the SQLite files untouched, drops the workspace that was being copied and lifts maintenance. The job journal (`migration/<job_id>.json`) remembers finished workspaces: the next **Migrate** against the same database resumes after them, verifies them again and recopies any that changed in between.

Someone who signs in while the copy runs gets a session that is not copied (the identity snapshot was taken before); they sign in again afterwards.

Headless, with the server stopped:

```bash
export TAM_TEAM_NEW_DSN='postgresql://tam:...@db.example.internal/tam?sslmode=verify-full'
tam-team db-check --dsn-env TAM_TEAM_NEW_DSN
tam-team db-migrate --dsn-env TAM_TEAM_NEW_DSN --dry-run
tam-team db-migrate --dsn-env TAM_TEAM_NEW_DSN
```

The DSN is passed only as the **name** of an environment variable, never as an argument, so it does not appear in `ps` output or shell history.

### Where the DSN lives

`<data dir>/database.json` (mode 0600) holds the DSN encrypted with the master key, the installation id and the generation. Precedence: the dashboard's setting (`database.json`) > `TAM_TEAM_DATABASE_URL` > SQLite. The browser only ever sees `postgresql://tam:••••@host:5432/tam?sslmode=require`; to change the password or host of the **same** database use **Repoint**, which refuses a database that belongs to another installation.

Only `postgresql://` or `postgres://` URIs are accepted, with the parameters `sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `connect_timeout`, `application_name`, `target_session_attrs` and `channel_binding`. `options` is rejected: it could change `search_path` or the role and bypass workspace isolation.

The server refuses to start instead of guessing when the configured database belongs to another installation, when it is empty but the data directory holds SQLite data (migrate first, or unset `TAM_TEAM_DATABASE_URL`), or when the master key is missing and the DSN cannot be decrypted. It also refuses when the data directory last ran on PostgreSQL and no DSN is set at all. That usually means a shell or service without `TAM_TEAM_DATABASE_URL`, and without the check the server would open a new, empty SQLite installation next to your data. Every start on PostgreSQL writes `<data dir>/postgres-installation.json` (mode 0600, installation id and host/database, no secret); a Rollback removes it. **Back up `master.key` or `TAM_TEAM_MASTER_KEY`**: without it neither the DSN nor the workspace role passwords can be recovered (a DBA can reset the roles, and the server resets every workspace password on provisioning once the key is back).

## Rollback

**Rollback** restores the SQLite archive made at activation and switches the server back to SQLite. Everything written on PostgreSQL after the switch is lost, so the dashboard asks you to type the organization name. The PostgreSQL database is left as it is; a later **Migrate** into it resumes and overwrites it with the current SQLite data.

Rollback is available while `archive/sqlite-<timestamp>/` exists. Purging a person's personal area on PostgreSQL also deletes that person's copy inside every archive, so a rollback cannot bring it back.

## Backup and restore

Litestream (`docs/TEAM_BACKUP.md`) replicates SQLite only. On PostgreSQL use PostgreSQL's own tools:

- **Point-in-time recovery**: your provider's automated backups or WAL archiving (pgBackRest, Barman, WAL-G). This is the recommended continuous backup.
- **Logical snapshot**: `tam-team backup --out DIR` runs one `pg_dump --format=custom --no-owner --no-privileges` of `tam_control`, `tam_learning`, `tam_compat` and every `ws_*` schema from a single exported snapshot, and writes `manifest.json` (format 2: dump SHA-256, schemas, workspace roles, exact row count of every table in that snapshot). The server can keep running.
- **Restore**: `tam-team restore --from DIR --dsn-env VAR` restores into an **empty** database that meets the requirements (`pg_restore --single-transaction --exit-on-error`), checks every table's row count against the manifest, and then provisions every workspace role, password and grant again, because roles belong to the PostgreSQL cluster and are never part of a dump. Point the server at the restored database with **Repoint** or `TAM_TEAM_DATABASE_URL`; the master key must be the one the backup was taken with. Restore refuses a data directory that holds `identity.db` or `database.json`, or that already runs on PostgreSQL while `TAM_TEAM_DATABASE_URL` names a different database than the target: restore into a new `--root`, or set `TAM_TEAM_DATABASE_URL` to the restored database first.

`pg_dump`/`pg_restore` must be at least the server's major version. Set `TAM_TEAM_PG_DUMP` / `TAM_TEAM_PG_RESTORE` to their paths if they are not on `PATH`, and `TAM_TEAM_PG_DUMP_TIMEOUT_SECONDS` (default 21600) for very large databases. The password reaches them through a temporary 0600 passfile, never through the command line or `PGPASSWORD`.

Backups taken earlier, WAL archives and PITR windows still contain a person's data after a purge, exactly as with SQLite snapshots.

## Trying it locally

`docker-compose.team.postgres.yml` adds a PostgreSQL 18 + pgvector container prepared by `docker/postgres-init/01-tam.sql` (the DBA SQL above). It is for evaluation and tests only: same host, no TLS, no backups.

```bash
export TAM_TEAM_PG_PASSWORD=$(openssl rand -hex 24)
export TAM_POSTGRES_SUPERUSER_PASSWORD=$(openssl rand -hex 24)
docker compose -f docker-compose.team.yml -f docker-compose.team.postgres.yml up -d
```

## Limits and differences from SQLite

- **Ranking**: full-text search is a port of SQLite FTS5 (the `unicode61` tokenizer's separators and diacritic folding, ported exactly; BM25 with FTS5's parameters). Results match closely but not bit for bit; `benchmarks/pg_parity_bench.py` measures the difference (top-10 Jaccard and Kendall tau per tier, Recall@k, MRR, nDCG, latency at 1k/10k/50k records). Vector search is an exact cosine scan over `float32` embeddings, equal to or better than SQLite's two-stage binary search.
- **Measured on 14.6.0** (same host, `pgvector/pgvector:0.8.1-pg18`): Recall@10 100% on both backends, top-10 Jaccard 1.0 on every tier, recall p50 485 vs 492 ms at 10k records and 552 vs 486 ms at 50k (PostgreSQL vs SQLite). Details: `docs/benchmarks/org-memory-v14-20260925/RESULTS.md`, section E5; raw numbers: `benchmarks/results/pg-parity-14.6.0.json`.
- **Latency** grows with the network round trip (see Requirements). On the same host PostgreSQL is within the plan's budget of 1.5 x SQLite's p95 recall at 10k records; across regions it is not.
- **Ids** are never reused (identity columns), as with `AUTOINCREMENT`.
- **Background enrichment** (`MEMORY_ASYNC_ENRICHMENT`) is not supported on PostgreSQL workers; the team server already runs with it off.
- **Personal-only features** of the local TAM (reflection, consolidation, wiki, ingest) do not run in team workers on either backend.
- **No PostgreSQL -> SQLite migration.** Use rollback (to the archive made at activation) or keep PostgreSQL and restore from `pg_dump`/PITR.
- **Catalog size**: each workspace schema has about 70 tables and 190 indexes. At 1,000 people that is a few hundred thousand catalog objects, which makes `pg_dump` and autovacuum of the catalog slower; plan maintenance windows accordingly.
