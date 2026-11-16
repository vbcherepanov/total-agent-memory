"""
Reflection Agent -- background process for knowledge consolidation.

Like sleep for the brain: transfers short-term to long-term memory,
consolidates patterns, resolves contradictions, and evolves the knowledge graph.

Orchestrates DigestPhase (cleanup) and SynthesizePhase (pattern finding).
Runs in three modes: quick (post-session), full (periodic), weekly (deep analysis).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections.abc import Callable

# Relative imports via sys.path for flat project structure
_src = str(Path(__file__).parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from memory_core.embedding_identity import EmbedIdentity

from reflection.digest import DigestPhase
from reflection.synthesize import SynthesizePhase

LOG = lambda msg: sys.stderr.write(f"[memory-reflection] {msg}\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return uuid.uuid4().hex


class ReflectionAgent:
    """Background process that consolidates, synthesizes, and evolves knowledge.

    Like sleep for the brain -- transfers short-term to long-term memory.
    Orchestrates two phases:
      1. Digest: cleanup, dedup, decay, contradiction resolution
      2. Synthesize: pattern finding, clustering, skill proposals
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        embedder=None,
        embed_identity: Callable[[], EmbedIdentity | None] | None = None,
    ) -> None:
        """
        Args:
            db: SQLite connection (row_factory=Row).
            embedder: Optional callable (text: str) -> list[float]. If None,
                a fresh server.Store() is instantiated lazily for multi-repr
                generation. Injected in tests to avoid heavy init.
        """
        self.db = db
        self.digest = DigestPhase(db)
        self.synthesize = SynthesizePhase(db)
        self._injected_embedder = embedder
        self._embed_identity = embed_identity
        self._owned_embed_store = None

    async def run(self, scope: str = "full") -> dict:
        """
        Run reflection pipeline.

        Args:
            scope: 'quick' (digest only), 'full' (digest + synthesize),
                   'weekly' (full + deep analysis + digest generation)

        Returns:
            ReflectionReport as dict.
        """
        if scope == "quick":
            return await self.run_quick()
        elif scope == "weekly":
            return await self.run_weekly()
        else:
            return await self.run_full()

    async def run_quick(self) -> dict:
        """Quick reflection: dedup + decay only. Runs after each session."""
        LOG("Starting quick reflection...")
        started_at = _now()

        # Run synchronously — SQLite connections are not thread-safe
        digest_stats = self._run_digest_quick()

        report = {
            "id": _new_id(),
            "type": "session",
            "scope": "quick",
            "started_at": started_at,
            "completed_at": _now(),
            "digest": digest_stats,
            "synthesis": None,
            "weekly_digest": None,
        }

        self._save_report(report)
        LOG(f"Quick reflection complete: {digest_stats}")
        return report

    async def run_full(self) -> dict:
        """Full reflection: digest + synthesize + triple extraction + fact merging.

        Runs every 6 hours. SQLite is not thread-safe, so all phases are sync.
        """
        LOG("Starting full reflection...")
        started_at = _now()

        # Phase 1: Digest (dedup, decay, contradictions)
        digest_stats = self.digest.run()

        # Phase 2: Synthesize (clusters, patterns — depends on clean data)
        synthesis_stats = self.synthesize.run(days=7)

        # Phase 3: Drain triple extraction queue (async pipeline from memory_save)
        triple_stats = self._run_triple_extraction()

        # Phase 4: Semantic fact merging (LLM consolidation of related facts)
        merge_stats = self._run_fact_merger()

        # Phase 5: Drain deep enrichment queue (entities/intent/topics)
        enrich_stats = self._run_deep_enrichment()

        # Phase 6: Generate multi-representation embeddings (GEM-RAG)
        repr_stats = self._run_representations()

        report = {
            "id": _new_id(),
            "type": "periodic",
            "scope": "full",
            "started_at": started_at,
            "completed_at": _now(),
            "digest": digest_stats,
            "synthesis": synthesis_stats,
            "triple_extraction": triple_stats,
            "fact_merge": merge_stats,
            "deep_enrichment": enrich_stats,
            "representations": repr_stats,
            "weekly_digest": None,
        }

        self._save_report(report)
        LOG(
            f"Full reflection complete: digest={digest_stats}, "
            f"synthesis={synthesis_stats}, triples={triple_stats}, "
            f"merge={merge_stats}, enrich={enrich_stats}, repr={repr_stats}"
        )
        return report

    # ──────────────────────────────────────────────
    # Phase 3 — drain triple extraction queue
    # ──────────────────────────────────────────────

    def _run_triple_extraction(self, limit: int = 500) -> dict[str, int]:
        """Pull pending knowledge_ids from the queue and run deep KG extraction.

        Skipped silently if Ollama / model unavailable — items remain in the
        queue and will be drained on a future run when LLM comes back online.
        """
        try:
            from config import has_llm
            if not has_llm("triple"):
                LOG("triple extraction skipped: LLM unavailable")
                return {"processed": 0, "failed": 0, "skipped": 0, "deferred": "no_llm"}
        except Exception:
            pass

        try:
            from triple_extraction_queue import TripleExtractionQueue
            from ingestion.extractor import ConceptExtractor
        except Exception as e:  # noqa: BLE001
            LOG(f"triple extraction imports failed: {e}")
            return {"processed": 0, "failed": 0, "skipped": 0, "error": str(e)}

        q = TripleExtractionQueue(self.db)
        extractor = ConceptExtractor(self.db)

        def extract(knowledge_id: int, content: str) -> dict:
            return extractor.extract_and_link(
                text=content, knowledge_id=knowledge_id, deep=True
            )

        try:
            return q.process_pending(extract, limit=limit)
        except Exception as e:  # noqa: BLE001
            LOG(f"triple queue processing error: {e}")
            return {"processed": 0, "failed": 0, "skipped": 0, "error": str(e)}

    # ──────────────────────────────────────────────
    # Phase 4 — semantic fact merging
    # ──────────────────────────────────────────────

    def _run_fact_merger(self) -> dict[str, int]:
        """Find clusters of related (non-duplicate) facts and synthesize via LLM."""
        try:
            from fact_merger import FactMerger
        except Exception as e:  # noqa: BLE001
            LOG(f"fact_merger import failed: {e}")
            return {"clusters_found": 0, "merged": 0, "rejected": 0, "error": str(e)}

        try:
            similarity = self._make_cosine_similarity_fn()
            llm_merge = self._make_llm_merge_fn()
            if similarity is None or llm_merge is None:
                # Dependencies unavailable; skip silently
                return {"clusters_found": 0, "merged": 0, "rejected": 0, "skipped": "deps"}

            merger = FactMerger(self.db, similarity_fn=similarity, llm_merge_fn=llm_merge)
            return merger.run()
        except Exception as e:  # noqa: BLE001
            LOG(f"fact_merger error: {e}")
            return {"clusters_found": 0, "merged": 0, "rejected": 0, "error": str(e)}

    def _make_cosine_similarity_fn(self):
        """Build a cosine-similarity closure over the embeddings table.

        Returns None if no embeddings are stored yet (cold start).
        """
        import struct

        try:
            count = self.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        except Exception:
            return None
        if not count:
            return None

        cache: dict[int, list[float]] = {}

        def _get(kid: int) -> list[float] | None:
            if kid in cache:
                return cache[kid]
            row = self.db.execute(
                "SELECT float32_vector, embed_dim FROM embeddings WHERE knowledge_id=?",
                (kid,),
            ).fetchone()
            if row is None:
                cache[kid] = None  # type: ignore[assignment]
                return None
            vec = list(struct.unpack(f"{row['embed_dim']}f", row["float32_vector"]))
            cache[kid] = vec
            return vec

        def cosine(id_a: int, id_b: int) -> float:
            va, vb = _get(id_a), _get(id_b)
            if not va or not vb or len(va) != len(vb):
                return 0.0
            num = sum(x * y for x, y in zip(va, vb))
            da = sum(x * x for x in va) ** 0.5
            db2 = sum(y * y for y in vb) ** 0.5
            if da == 0 or db2 == 0:
                return 0.0
            return max(0.0, min(1.0, num / (da * db2)))

        return cosine

    # ──────────────────────────────────────────────
    # Phase 6 — generate multi-representation embeddings
    # ──────────────────────────────────────────────

    def _run_representations(self, limit: int = 500) -> dict[str, int]:
        """Drain representations_queue: LLM-generate views + embed each.

        When LLM is unavailable we still run — the queue worker will save the
        `raw` embedding (no LLM needed) and skip summary/keywords/etc. The
        gate inside `generate_representations` returns empty for LLM views.
        """
        try:
            from representations_queue import RepresentationsQueue
            from representations import generate_representations
        except Exception as e:  # noqa: BLE001
            LOG(f"representations imports failed: {e}")
            return {"processed": 0, "failed": 0, "skipped": 0, "error": str(e)}

        if self._injected_embedder is not None and self._embed_identity is None:
            return {"processed": 0, "failed": 0, "skipped": 0,
                    "error": "Injected representation embedder requires an identity callback"}

        q = RepresentationsQueue(self.db)

        embedder = self._make_embedder()
        if embedder is None:
            return {"processed": 0, "failed": 0, "skipped": 0, "skipped_reason": "no embedder"}

        try:
            if self._owned_embed_store is not None:
                q.enqueue_incompatible(self._owned_embed_store._primary_identity())
            return q.process_pending(
                generator=lambda content, project=None: generate_representations(content, project),
                embedder=embedder,
                model_name="auto",
                limit=limit,
                identity_of=self._embed_identity,
            )
        except Exception as e:  # noqa: BLE001
            LOG(f"representations processing error: {e}")
            return {"processed": 0, "failed": 0, "skipped": 0, "error": str(e)}
        finally:
            if self._owned_embed_store is not None:
                self._owned_embed_store.db.close()
                self._owned_embed_store = None

    def _make_embedder(self):
        """Return an embedder callable, or None if unavailable.

        Priority: injected via constructor → lazy server.Store init.
        """
        if self._injected_embedder is not None:
            return self._injected_embedder
        try:
            import server as _srv

            store = _srv.Store()
            self._owned_embed_store = store
            self._embed_identity = store.last_embed_identity
            def embed(text: str) -> list[float]:
                embs = store.embed([text])
                return embs[0] if embs else []
            return embed
        except Exception as e:  # noqa: BLE001
            LOG(f"embedder init failed: {e}")
            return None

    # ──────────────────────────────────────────────
    # Phase 5 — drain deep enrichment queue
    # ──────────────────────────────────────────────

    def _run_deep_enrichment(self, limit: int = 500) -> dict[str, int]:
        """Drain queue with deep_enricher.deep_enrich (entities/intent/topics).

        Skipped if no LLM — items wait in queue.
        """
        try:
            from config import has_llm
            if not has_llm("enrich"):
                LOG("deep enrichment skipped: LLM unavailable")
                return {"processed": 0, "failed": 0, "skipped": 0, "deferred": "no_llm"}
        except Exception:
            pass

        try:
            from deep_enrichment_queue import DeepEnrichmentQueue
            from deep_enricher import deep_enrich
        except Exception as e:  # noqa: BLE001
            LOG(f"deep enrichment imports failed: {e}")
            return {"processed": 0, "failed": 0, "skipped": 0, "error": str(e)}

        q = DeepEnrichmentQueue(self.db)
        try:
            return q.process_pending(deep_enrich, limit=limit)
        except Exception as e:  # noqa: BLE001
            LOG(f"deep enrichment processing error: {e}")
            return {"processed": 0, "failed": 0, "skipped": 0, "error": str(e)}

    def _make_llm_merge_fn(self):
        """Build a provider-backed merger. Returns None if LLM unreachable.

        Uses the `llm_provider` abstraction: Ollama by default, OpenAI /
        Anthropic / any OpenAI-compatible endpoint when configured via
        MEMORY_LLM_PROVIDER. On provider/network errors during a merge the
        closure degrades to an empty string (FactMerger's validator rejects
        empty merges, so we simply skip the cluster).
        """
        # Don't even build the closure if no LLM configured
        try:
            from config import has_llm
            if not has_llm():
                return None
        except Exception:
            pass

        import config as _config
        import urllib.error as _urlerr

        prompt_tmpl = (
            "You are consolidating related facts into ONE concise sentence.\n"
            "Preserve any code blocks, URLs, and file paths EXACTLY as written.\n"
            "Do not invent details not in the sources.\n\n"
            "SOURCES:\n{sources}\n\nMERGED:"
        )

        # Build provider once; reuse across cluster merges in this run.
        try:
            from llm_provider import make_provider
            provider = make_provider(_config.get_llm_provider())
        except Exception as exc:  # noqa: BLE001 — bad provider name etc.
            LOG(f"fact_merger provider init failed: {exc}")
            return None
        if not provider.available():
            LOG(f"fact_merger provider '{getattr(provider, 'name', '?')}' unavailable")
            return None

        model_name = _config.get_llm_model_for_provider(_config.get_llm_provider())
        timeout = 60.0

        def merge(contents: list[str]) -> str:
            sources = "\n\n---\n\n".join(f"- {c}" for c in contents)
            prompt = prompt_tmpl.format(sources=sources)
            try:
                return provider.complete(
                    prompt,
                    model=model_name,
                    max_tokens=200,
                    temperature=0.1,
                    timeout=timeout,
                )
            except (_urlerr.URLError, RuntimeError, OSError) as exc:
                sys.stderr.write(f"[memory-reflection] LLM merge error: {exc}\n")
                return ""

        return merge

    async def run_drain(self) -> dict:
        """Fast path: drain only the v6 async queues (Phase 3, 5, 6).

        Skips expensive digest/synthesize. Use when you want new edges from a
        handful of fresh saves ASAP (file-watch trigger after memory_save).
        Typical latency: 10-30s for <10 pending items vs 2-3 min for run_full.
        """
        LOG("Starting drain reflection (queues only)...")
        started_at = _now()

        triple_stats = self._run_triple_extraction()
        enrich_stats = self._run_deep_enrichment()
        repr_stats   = self._run_representations()

        report = {
            "id": _new_id(),
            # CHECK constraint on reflection_reports.type only allows
            # session|periodic|weekly|manual — use 'manual' for drain runs.
            "type": "manual",
            "scope": "drain",
            "started_at": started_at,
            "completed_at": _now(),
            "triple_extraction": triple_stats,
            "deep_enrichment": enrich_stats,
            "representations": repr_stats,
        }
        self._save_report(report)
        LOG(
            f"Drain complete: triples={triple_stats}, "
            f"enrich={enrich_stats}, repr={repr_stats}"
        )
        return report

    async def run_weekly(self) -> dict:
        """Weekly deep reflection with digest generation."""
        LOG("Starting weekly reflection...")
        started_at = _now()

        # Run synchronously — SQLite connections are not thread-safe
        # Phase 1: Full digest
        digest_stats = self.digest.run()

        # Phase 2: Extended synthesis (30 days lookback)
        synthesis_stats = self.synthesize.run(days=30)

        # Phase 3: Generate weekly digest report
        weekly_digest = self.synthesize.generate_weekly_digest()

        # Phase 4: Update graph importance via PageRank
        try:
            self._update_graph_importance()
        except Exception as e:
            LOG(f"PageRank update error: {e}")

        report = {
            "id": _new_id(),
            "type": "weekly",
            "scope": "weekly",
            "started_at": started_at,
            "completed_at": _now(),
            "digest": digest_stats,
            "synthesis": synthesis_stats,
            "weekly_digest": weekly_digest,
        }

        self._save_report(report)
        LOG(f"Weekly reflection complete")
        return report

    def _run_digest_quick(self) -> dict:
        """Run a lightweight digest: only dedup and decay, skip contradiction analysis."""
        stats: dict = {
            "duplicates_merged": 0,
            "decay": {"checked": 0, "archived": 0, "kept": 0},
        }

        try:
            stats["duplicates_merged"] = self.digest.merge_duplicates()
        except Exception as e:
            LOG(f"quick merge_duplicates error: {e}")

        try:
            stats["decay"] = self.digest.apply_intelligent_decay()
        except Exception as e:
            LOG(f"quick apply_intelligent_decay error: {e}")

        return stats

    def _update_graph_importance(self) -> None:
        """Update graph node importance via PageRank."""
        try:
            from graph.query import GraphQuery
            from graph.store import GraphStore

            store = GraphStore(self.db)
            query = GraphQuery(store)
            query.update_importance()
            LOG("Graph importance updated via PageRank")
        except ImportError:
            LOG("graph.query not available, skipping PageRank update")
        except Exception as e:
            LOG(f"PageRank update failed: {e}")

    def _save_report(self, report: dict) -> str:
        """Save reflection report to DB. Returns report ID."""
        report_id = report.get("id", _new_id())
        report_type = report.get("type", "periodic")

        # Calculate period from report timing
        now = datetime.now(timezone.utc)
        if report_type == "weekly":
            period_start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif report_type == "periodic":
            period_start = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            period_start = report.get("started_at", _now())
        period_end = report.get("completed_at", _now())

        # Extract stats for report fields
        digest = report.get("digest") or {}
        synthesis = report.get("synthesis") or {}

        try:
            self.db.execute(
                """INSERT INTO reflection_reports
                   (id, period_start, period_end, type,
                    new_nodes, patterns_found, skills_refined,
                    rules_proposed, contradictions, archived,
                    focus_areas, key_findings, proposed_changes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report_id,
                    period_start,
                    period_end,
                    report_type,
                    synthesis.get("edges_strengthened", 0),
                    synthesis.get("clusters_found", 0),
                    synthesis.get("skills_proposed", 0),
                    0,  # rules_proposed
                    digest.get("contradictions_found", 0),
                    digest.get("decay", {}).get("archived", 0),
                    json.dumps((report.get("weekly_digest") or {}).get("focus_areas", [])),
                    json.dumps((report.get("weekly_digest") or {}).get("top_concepts", [])),
                    json.dumps(synthesis),
                    _now(),
                ),
            )
            self.db.commit()
            LOG(f"Saved reflection report: {report_id} ({report_type})")
        except Exception as e:
            LOG(f"Failed to save reflection report: {e}")

        return report_id

    def _get_pending_proposals(self) -> list[dict]:
        """Get pending proposals for user review."""
        rows = self.db.execute(
            """SELECT id, type, content, evidence, confidence, created_at
               FROM pending_proposals
               WHERE status = 'pending'
               ORDER BY confidence DESC, created_at DESC"""
        ).fetchall()

        proposals: list[dict] = []
        for row in rows:
            proposal = dict(row)
            # Parse JSON fields
            if isinstance(proposal.get("content"), str):
                try:
                    proposal["content"] = json.loads(proposal["content"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(proposal.get("evidence"), str):
                try:
                    proposal["evidence"] = json.loads(proposal["evidence"])
                except (json.JSONDecodeError, TypeError):
                    pass
            proposals.append(proposal)

        return proposals

    def approve_proposal(self, proposal_id: str) -> bool:
        """Mark proposal as approved and apply it.

        For skill proposals: creates the skill in the skills table.
        Returns True if proposal was found and approved.
        """
        row = self.db.execute(
            "SELECT id, type, content, status FROM pending_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()

        if not row:
            LOG(f"Proposal {proposal_id} not found")
            return False

        if row["status"] != "pending":
            LOG(f"Proposal {proposal_id} already {row['status']}")
            return False

        # Parse content
        content = row["content"]
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                content = {}

        proposal_type = row["type"]

        # Apply the proposal
        if proposal_type == "skill" and isinstance(content, dict):
            self._apply_skill_proposal(content)

        # Mark as approved
        self.db.execute(
            """UPDATE pending_proposals
               SET status = 'approved', reviewed_at = ?
               WHERE id = ?""",
            (_now(), proposal_id),
        )
        self.db.commit()
        LOG(f"Approved proposal {proposal_id} ({proposal_type})")
        return True

    def reject_proposal(self, proposal_id: str) -> bool:
        """Mark proposal as rejected."""
        row = self.db.execute(
            "SELECT id, status FROM pending_proposals WHERE id = ?",
            (proposal_id,),
        ).fetchone()

        if not row:
            LOG(f"Proposal {proposal_id} not found")
            return False

        if row["status"] != "pending":
            LOG(f"Proposal {proposal_id} already {row['status']}")
            return False

        self.db.execute(
            """UPDATE pending_proposals
               SET status = 'rejected', reviewed_at = ?
               WHERE id = ?""",
            (_now(), proposal_id),
        )
        self.db.commit()
        LOG(f"Rejected proposal {proposal_id}")
        return True

    def _apply_skill_proposal(self, content: dict) -> None:
        """Create a skill from an approved proposal."""
        skill_id = _new_id()
        name = content.get("skill_name", f"skill_{skill_id[:8]}")
        trigger = content.get("trigger_pattern", "")
        steps = content.get("steps", [])
        projects = content.get("projects", [])
        episode_ids = content.get("episode_ids", [])

        try:
            self.db.execute(
                """INSERT INTO skills
                   (id, name, trigger_pattern, steps, anti_patterns,
                    times_used, success_rate, projects, stack,
                    related_skills, learned_from, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 'draft', ?)""",
                (
                    skill_id,
                    name,
                    trigger,
                    json.dumps(steps),
                    json.dumps([]),
                    content.get("success_rate", 0.0),
                    json.dumps(projects),
                    json.dumps([]),
                    json.dumps([]),
                    json.dumps(episode_ids),
                    _now(),
                ),
            )
            LOG(f"Created skill from proposal: {name} ({skill_id})")
        except sqlite3.IntegrityError:
            LOG(f"Skill {name} already exists, skipping")
