# Memory Protocol

Optional. `install.sh --ide codex` installs the `memory-protocol` skill
and does not touch `~/.codex/AGENTS.md`; append the block below yourself
if you also want the protocol stated there.

---

## Memory Protocol (total-agent-memory v10.5)

A persistent cross-session memory is wired in via the `memory` MCP
server. The same protocol applies as in Claude Code / Cursor / Cline:

1. **Session start →** `session_init(project=...)` first, then
   `memory_recall(query, project)` for the current task.
2. **Before any non-trivial task →** `memory_recall` to find the
   recipe (don't reinvent conventions you can recall).
3. **After every significant action →** `memory_save` immediately
   with the digest template (`ЧТО / ПРОЕКТ / ФАЙЛЫ / СТЕК / ПОДХОД /
   НЮАНСЫ`, ≤ 30 lines, tags `['reusable', '<tech>']` if applicable).
4. **On bash non-zero (reproducible) →** `learn_error(file, error,
   root_cause, fix, pattern)`. After 3 identical patterns auto-rules.
5. **End of session →** `session_end(session_id, summary, next_steps,
   pitfalls)`.

Cross-project lookup: `analogize(text, exclude_project=<current>)`.
Time-travel: `kg_at(timestamp)`. Stuck: 3 failures → STOP, save
`type='lesson'`, ask the user A/B/C.

Full reference: `~/total-agent-memory/skills/memory-protocol/`.
