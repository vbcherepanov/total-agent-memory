# v11.0 hot-path benchmark

_Generated: 2026-04-27 22:52:20 CEST_

MEMORY_MODE=fast, MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false.

| metric              | p50 | p95 | p99 |
|---------------------|-----|-----|-----|
| save_fast           | 6.4 | 9.5 | 10.3 |
| save_fast (cached)  | 0.3 | 0.4 | 1.1 |
| search_fast         | 3.8 | 6.3 | 7.6 |
| cached_search       | 0.0 | 0.0 | 0.0 |
| llm_calls           | 0                |
| network_calls       | 0                |
