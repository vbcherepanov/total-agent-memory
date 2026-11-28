# Manual QA Checklist

Smoke-tests run by hand before tagging a release. Automated pytest covers most of this, but the provider wiring / env-plumbing paths are hardest to regress via unit tests.

## v8.0 smoke checklist

- [ ] `MEMORY_LLM_PROVIDER=openai MEMORY_LLM_API_KEY=sk-... python -c "from config import has_llm; print(has_llm())"` → True
- [ ] `save_knowledge(content="hi <private>secret</private> bye")` → content stored is `"hi  bye"`, `privacy_redacted_sections=1`
- [ ] `memory_recall(query="test", mode="index", limit=5)` → results have only {id, title, score, type, project, created_at}
- [ ] `memory_get(ids=[1,2])` → full content for 2 records
- [ ] `classify_task("refactor auth middleware")` → level: 3
- [ ] `task_create(task_id="t1", description="test")` → in phase "van"
- [ ] `phase_transition("t1", "plan")` → closes van, opens plan, response has `rules_preview`
- [ ] `save_decision(title="...", options=[...], criteria_matrix={...}, selected="X", rationale="...")` → returns `{saved_id, structured: True}`
- [ ] `session_end("proj", summary="s", auto_compress=True)` → markdown at `~/Documents/project/Projects/proj/activeContext.md`
- [ ] `curl http://localhost:37737/api/knowledge/1` → JSON with related[] expansion
- [ ] `./install.sh --ide cursor` (INSTALL_TEST_MODE=1 HOME=/tmp/X) → creates `~/.cursor/mcp.json`
