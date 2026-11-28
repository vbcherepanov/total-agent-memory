# Documentation

Total Agent Memory (TAM) is a local memory server that gives AI coding agents long-term memory over MCP. Most people need only the first section.

## Start here

- [Quick start](../README.md#quick-start): install and connect your agent. Read this first.
- [installation.md](installation.md): only if the quick start did not cover your platform or client.
- [tools.md](tools.md): what the agent can call, grouped by purpose, plus the command-line helpers and the dashboard.

## Everyday use

- [configuration.md](configuration.md): environment variables, when you want to change a default.
- [LOCAL_SETTINGS.md](LOCAL_SETTINGS.md): the personal Settings page, privacy controls and erasure.
- [REPORTS.md](REPORTS.md): activity reports (`memory_report`, `tam report`).
- [importing.md](importing.md): bring in chat exports, mem0 data or Markdown notes; move the store between machines.
- [shared-daemon.md](shared-daemon.md): one warm server for many client sessions (opt-in).
- [upgrading.md](upgrading.md) and [UPGRADE_15.md](UPGRADE_15.md): updating; what changes when moving from 14.x to 15.0.0.
- [whats-new.md](whats-new.md): release summaries for 14.x and 15.0.
- [dashboard-i18n.md](dashboard-i18n.md): dashboard languages and how one is chosen.

## Team server

Only if several people share one server.

- [team-server.md](team-server.md): quick start. Read this first.
- [team-server-reference.md](team-server-reference.md): permissions, CLI commands, Docker, backup, remote client, limits.
- [SETUP_WIZARD.md](SETUP_WIZARD.md): the terminal and web setup wizards.
- [TEAM_DASHBOARD.md](TEAM_DASHBOARD.md): the administrator and staff web dashboard.
- [TEAM_POSTGRES.md](TEAM_POSTGRES.md): run the team server on PostgreSQL instead of SQLite.
- [TEAM_BACKUP.md](TEAM_BACKUP.md): snapshots and continuous replication.
- [TEAM_ONBOARDING.md](TEAM_ONBOARDING.md): department onboarding for new employees.
- [llm-settings.md](llm-settings.md): LLM provider and CPU-thread settings.

## Benchmarks and reproducibility

- [benchmarks.md](benchmarks.md): results and the commands that reproduce them.
- [benchmarks/head-to-head-v14/RESULTS.md](benchmarks/head-to-head-v14/RESULTS.md): comparison with Mem0 Platform on the same questions under the same judge.
- [../benchmarks/results/rerank-modes/README.md](../benchmarks/results/rerank-modes/README.md): LoCoMo retrieval by cross-encoder setting, with per-question rows.

The other per-study reports are listed at the end of benchmarks.md.

## How it works / contributing

- [architecture.md](architecture.md): write path, recall path and store layout.
- [provenance.md](provenance.md): how each record keeps track of which client wrote it.
- [../CONTRIBUTING.md](../CONTRIBUTING.md): development setup and contribution rules.
- [../CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md): community conduct.
- [../SECURITY.md](../SECURITY.md): supported versions and how to report a vulnerability.
- [../CHANGELOG.md](../CHANGELOG.md): the complete list of changes per release.

## Archive

- [archive/README.md](archive/README.md): older-version notes, the April 2026 feature comparison and the v8 to v13 roadmap. Not needed to use the software.
