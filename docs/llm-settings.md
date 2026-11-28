# LLM and CPU settings (since 14.0.0)

Main choice: `MEMORY_LLM_PROVIDER=ollama|openai|openai-compatible|anthropic`. The model is set with `MEMORY_LLM_MODEL`, the key with `MEMORY_LLM_API_KEY`, the base address with `MEMORY_LLM_API_BASE`. For Ollama, `OLLAMA_URL` is also used.

For an OpenAI-compatible server, the address and the model are required. The key can be omitted if the endpoint itself accepts requests without authorization. The key from `OPENAI_API_KEY` is not carried over to this mode automatically. Plain `openai` still requires a key. The Chat Completions protocol is supported; an arbitrary incompatible HTTP API needs a separate adapter. For structured responses the endpoint must support JSON Schema.

Episode generation, ingestion summaries, HyDE and LLM reranking now use the shared provider. For Ollama, the separate `HYDE_MODEL` and `RERANKER_MODEL` remain; without them the shared model is used. The hard-coded 32B choice for episodes and summaries was removed. `MEMORY_LLM_ENABLED=false` disables these calls.

Image descriptions use the same selected provider: Ollama, OpenAI-compatible or Anthropic. Set a separate `MEMORY_VISION_MODEL` that supports images; without it no large vision model is called automatically. PNG/JPEG/GIF/WebP are detected from the content, and the size is limited to 6 MiB; convert other formats first. `MEMORY_LLM_ENABLED=false` disables this route as well. The adapters were checked with transport regressions; the availability of a specific user model requires connecting it.

## Docker

The standard `docker-compose.yml` keeps the local stack with Ollama. The shared provider settings and `MEMORY_EMBED_THREADS` are passed to MCP, the dashboard and background services through `.env`.

If the model runs outside this Compose stack, first set its provider, address, model and the required keys in `.env`, then use both files:

```sh
docker compose -f docker-compose.yml -f docker-compose.external.yml config --services
docker compose -f docker-compose.yml -f docker-compose.external.yml up -d --build
```

The external variant starts MCP, the dashboard, reflection and the scheduler; Ollama/model-init are not started. Inside a container `localhost` means the container itself. The address of the external model must be reachable from the container. The `config --services` check does not confirm that the model is reachable and does not replace a request to it. These commands are prepared for the user; the running stack was not started or updated during development.

A Docker Compose with `!reset` support is required; the Compose used for verification was 5.5.1. [Compose merge rules](https://docs.docker.com/reference/compose-file/merge/).

## CPU limits

`MEMORY_EMBED_THREADS=1` by default limits the threads of each FastEmbed model on all four of its loading paths: the server, the embedding provider, reembed, and the fact index. The value must be a positive integer. For server-side batch indexing it can be increased after measuring CPU and latency.

`MEMORY_TORCH_THREADS=1` limits the intra-op CPU threads of PyTorch when loading NLI and CrossEncoder. The value must be a positive integer. The setting applies to the PyTorch process, not to the whole server. The OpenMP/BLAS limits are set before PyTorch is imported; restart the process after changing the setting. The final BGE check on Linux ARM64 with the thread limit applied early gave p95 2179.29 ms against a 200 ms budget. Limiting the parallel load increased latency; it is not a speed-up. See [CPU measurements](benchmarks/grounded-v14/CPU_RESULTS.md).

These are model limits, not a general CPU limit for all processes. They do not limit Ollama, the number of clients or background processes. Zero load on the whole system is not claimed.
