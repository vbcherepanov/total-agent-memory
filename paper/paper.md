---
title: 'total-agent-memory: a local, reproducible long-term memory server for AI coding agents'
tags:
  - Python
  - AI agents
  - agent memory
  - information retrieval
  - Model Context Protocol
  - knowledge graph
authors:
  - name: Vitalii Cherepanov
    orcid: 0009-0009-8289-8435
    affiliation: 1
affiliations:
  - name: Independent researcher, Novi Sad, Serbia
    index: 1
date: 28 September 2026
bibliography: paper.bib
---

# Summary

AI coding agents such as Claude Code, Codex CLI and Cursor start every session without memory of earlier ones. Decisions, fixes and project conventions from yesterday have to be explained again. `total-agent-memory` (TAM) is an open-source memory server for such agents. It stores decisions, solutions, facts, errors and session summaries on the user's machine and gives them back through the Model Context Protocol (MCP) [@mcp], so any MCP client can use it without code changes.

Each TAM store is one directory built around a SQLite database. Search combines full-text BM25 [@robertson2009bm25], dense embeddings computed locally, fuzzy matching and a knowledge graph, and fuses the ranked lists with reciprocal rank fusion [@cormack2009rrf]. By default a local multilingual cross-encoder then reranks the result once its model has loaded in the background, and maximal marginal relevance [@carbonell1998mmr] can diversify it. The default profile makes no LLM call on write or search. Facts can carry validity intervals, so the store can answer what was true at a given time, and a newer value of a single-valued fact can retire the older one. Since release 14.6.0 the same code can also run as a team server with separate personal, department and company areas.

The package is written in Python, released under the MIT license, and distributed on PyPI as `total-agent-memory`.

# Statement of need

Research on agent memory needs systems that can be run, inspected and measured on public benchmarks such as LoCoMo [@maharana2024locomo], LongMemEval [@wu2024longmemeval], BEAM [@tavakoli2025beam] and MemoryAgentBench [@hu2025memoryagentbench]. Many memory layers that are used in practice are hosted services, or they call an LLM on every write to extract memories [@chhikara2025mem0; @xu2025amem]. Each such call takes time and API spend (A-MEM reports about 1,200 tokens and 1.1 to 5.4 s per memory operation [@xu2025amem]), and it ties results to a provider's model versions, which makes runs hard to repeat.

TAM is built for this setting. Its default pipeline runs offline, so retrieval quality can be measured without an LLM or an API key. A write takes a median of 31 to 52 ms on stores of 10,000 to 1,000,000 records, and a full LoCoMo retrieval run (5,882 writes, 1,986 queries) takes 7 to 35 minutes on a laptop, depending on the cross-encoder setting (`docs/benchmarks.md`). Two runs on fresh stores give the same ranking for every question. The runner controls the two cases where they would not: a query that arrives before the cross-encoder has loaded, and one LoCoMo question whose relative date is counted from the day of the run. Every runner for the public benchmarks disables usage-based score boosts, which would otherwise let a store score better on its own second run. Negative controls (random, earliest and most recent turns) are scored next to the main metric on LoCoMo, so each reported number comes with its floor. When an LLM is used, for example to answer questions in end-to-end evaluations, the model, prompt and seed are recorded and several runs are reported.

The target users are researchers and engineers who study memory for agents, compare retrieval designs, or need a memory baseline that they can run locally and change. The same server is used in daily work with coding agents, which brings real usage and bug reports.

# State of the field

MemGPT [@packer2023memgpt], now continued as Letta, manages tiers of context for one agent. Mem0 [@chhikara2025mem0] extracts and consolidates facts from conversations with an LLM, with an optional graph variant. Zep [@rasmussen2025zep] builds a temporal knowledge graph of conversations and business data. These systems focus on what to store and how to retrieve it, and they are usually deployed as services with an LLM in the write path.

TAM differs in three ways. First, it is local by default and needs no LLM to write or search, which is what makes deterministic retrieval benchmarks possible. Second, it is MCP-native: the memory is a tool server that any MCP client can call, rather than a library that has to be wired into one agent framework. Third, it treats evaluation as part of the software. The repository ships runners and raw results for LoCoMo, LongMemEval, BEAM and MemoryAgentBench, a head-to-head report (`docs/benchmarks/head-to-head-v14/RESULTS.md`) that grades TAM's answers and Mem0 Platform's published answers under the same judge, and a benchmark of department isolation on the team server [@cherepanov2026orgmemory].

We do not claim that TAM retrieves better than the systems above in general. The head-to-head report found no statistically detected difference from Mem0 Platform on the held-out questions it covers, and it states that this does not show equivalence.

# Example

An agent uses TAM through ordinary MCP tool calls. In one session the user explains a choice, and the agent saves it as a decision with the reason:

```python
memory_save(type="decision",
            content="Chose pgvector over ChromaDB for the vector store",
            context="WHY: one Postgres instance, row-level security per tenant",
            project="billing")
```

Days later, in a new session and possibly another directory, the user asks why pgvector was chosen. The agent calls:

```python
memory_recall(query="vector database choice", project="billing")
```

and gets the decision back with its reason, the time it was saved and its ID. If the decision changes, `memory_update` stores a new version, the next recall returns it, and `memory_history` still shows the old one. The benchmark runners in the repository use the same calls, so experiments exercise the code path that users run.

# Software design

![Main parts of total-agent-memory. Agents call MCP tools; writes and searches go to one store per memory area, built around a SQLite database.\label{fig:architecture}](architecture.png){ width=85% }

\autoref{fig:architecture} shows the main parts. The design follows from three goals: memory must stay on the user's machine, results must be repeatable, and any MCP client must be able to use it.

**One store, one directory.** A store is a directory built around a SQLite database with FTS5 for text search and tables for records, versions and the knowledge graph. Embeddings are computed with FastEmbed on the CPU and stored in SQLite next to the records, both as full float32 vectors and as binary-quantized copies for fast candidate search. A store in one directory is easy to copy, back up, delete and reset between experiments. The cost is one writer per database at a time, which is enough for one person and is handled by one worker process per area on the team server.

**No LLM in the default path.** TAM stores what the agent sends and indexes it locally instead of asking an LLM to extract facts. Writes cost only local CPU time, data stays on the machine, and the retrieval stages are deterministic. An LLM can be configured for optional stages such as summaries, contradiction checks or query rewriting, and each of them can be switched off.

**Hybrid retrieval with fusion.** Reciprocal rank fusion needs no score calibration between the BM25, dense, fuzzy and graph retrievers. The default cross-encoder scores each candidate on the CPU and raises the cost of a query in exchange for higher recall: on the 1,536 graded LoCoMo questions, release 15.0.2 reaches R@5 0.745 against 0.609 with the cross-encoder off, and the median query time on an Apple M2 Max (measured on 15.0.0, which has the same retrieval code) is about 0.9 s against about 50 ms. The runs, per-question rows and commands are in the repository under `benchmarks/results/rerank-modes`.

**Facts that change.** Records are versioned instead of overwritten, and knowledge-graph facts carry `valid_from` and `valid_to`, so a query can ask what was true at a point in time. The store stays append-only and auditable, at the cost of a larger database.

**MCP as the interface.** The server exposes its operations as MCP tools over stdio or HTTP. An index mode of `memory_recall` returns only IDs and titles, and `memory_get` fetches the few records the agent needs, which keeps its context small.

**Team server.** For companies, a gateway authenticates personal tokens and runs each personal, department and shared area in its own worker process and store, checking membership on every request. On a synthetic company, attacks across nine attempt types returned no records of other departments, and membership changes applied from the next request [@cherepanov2026orgmemory]. The cost is one process per active area. A PostgreSQL backend with one schema and database role per area is also available.

**Tests.** The repository has more than 370 test files. Continuous integration installs and smoke-tests the package on Linux, macOS and Windows, and runs the team-server suite on both SQLite and PostgreSQL.

# Research impact statement

TAM is used as a method in MemoryAgentBench [@hu2025memoryagentbench]: its maintainers merged an adapter that runs TAM under the same configurations as the other memory methods in the benchmark (HUST-AI-HYZ/MemoryAgentBench pull request 25, September 2026). An independent atlas of open-source agent-memory systems described one TAM technique, a second retrieval that looks for contradicting evidence before answering, as worth reusing in other systems. The saved verdicts of the head-to-head report were recomputed by an external reviewer from the published files, and the report was revised after that review; the revision history is in the report.

The project has been developed in public since February 2026, with 39 tagged releases by the end of November 2026. Between June and November 2026 the package was downloaded about 4,200 times from PyPI (mirrors excluded) and its npm installer about 4,700 times. The repository has 78 stars and 19 forks, and nine pull requests from five outside contributors have been merged. The team-server isolation study built on TAM is published as a preprint with its data and harness [@cherepanov2026orgmemory].

# AI usage disclosure

Generative AI tools were used throughout this project. AI coding assistants (Claude Code with Anthropic Claude models, and OpenAI Codex) were used to write and refactor code, write tests and benchmark harnesses, and draft documentation. An AI assistant (Claude Code with Claude Opus 5.5) helped draft this paper and check its statements against the repository. The author designed the system and chose its architecture, reviewed the generated code and text, decided what to measure and report, and is responsible for the accuracy of all submitted material.

# Acknowledgements

I thank the outside contributors who sent pull requests and bug reports, and the reviewer who recomputed the head-to-head verdicts.

# References
