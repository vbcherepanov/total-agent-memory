"""
Cognitive Engine -- always-on thinking triggers for session events.

Provides context enrichment on session start, query processing, and
action result tracking. Not invoked by user directly -- runs
automatically through hooks and MCP tool wrappers.

All methods are synchronous for low-latency responses.
Uses lazy imports to avoid circular dependencies.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path

from memory_core.retrieval import SearchScope, fetch_active_records
from tam_db.contracts import Backend

# Ensure src/ is in path for sibling package imports
_src = str(Path(__file__).parent.parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

LOG = lambda msg: sys.stderr.write(f"[memory-cognitive] {msg}\n")

# Token estimation: ~4 chars per token
CHARS_PER_TOKEN = 4


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return uuid.uuid4().hex


def _parse_json(val: str | None, default: list | dict | None = None) -> list | dict:
    """Safely parse a JSON string."""
    if val is None:
        return default if default is not None else []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else []


def _estimate_tokens(text: str) -> int:
    """Estimate token count from text length."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _truncate_to_tokens(items: list[dict], max_tokens: int, key: str = "content") -> list[dict]:
    """Truncate a list of dicts to fit within token budget."""
    result: list[dict] = []
    used = 0
    for item in items:
        text = str(item.get(key, ""))
        tokens = _estimate_tokens(text)
        if used + tokens > max_tokens:
            break
        result.append(item)
        used += tokens
    return result


class CognitiveEngine:
    """
    Always-on thinking engine. Triggers on every session event.
    Not invoked by user -- runs automatically through hooks.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        # Lazy-initialized submodules
        self._activation = None
        self._graph_store = None
        self._graph_query = None

    # ──────────────────────────────────────────────
    # Lazy properties for submodules
    # ──────────────────────────────────────────────

    @property
    def activation(self):
        """Lazy-load SpreadingActivation to avoid circular imports."""
        if self._activation is None:
            try:
                from associative.activation import SpreadingActivation
                self._activation = SpreadingActivation(self.db)
            except ImportError:
                LOG("SpreadingActivation not available")
        return self._activation

    @property
    def graph_store(self):
        """Lazy-load GraphStore."""
        if self._graph_store is None:
            try:
                from graph.store import GraphStore
                self._graph_store = GraphStore(self.db)
            except ImportError:
                LOG("GraphStore not available")
        return self._graph_store

    @property
    def graph_query(self):
        """Lazy-load GraphQuery."""
        if self._graph_query is None:
            try:
                from graph.query import GraphQuery
                if self.graph_store:
                    self._graph_query = GraphQuery(self.graph_store)
            except ImportError:
                LOG("GraphQuery not available")
        return self._graph_query

    # ──────────────────────────────────────────────
    # Session lifecycle hooks
    # ──────────────────────────────────────────────

    def on_session_start(self, project: str) -> dict:
        """
        Called at session start. Returns context bundle with
        project-specific rules, open episodes, pending proposals,
        blind spots, and recently used skills.
        """
        result: dict = {
            "vito_identity": "",
            "vito_context": "",
            "project_context": {},
            "open_episodes": [],
            "pending_proposals": [],
            "blind_spots": [],
            "recent_skills": [],
        }

        # Vito context layers (L0 + L1 + L2)
        try:
            from tools.context_layers import wake_up
            db_path = self.db.execute("PRAGMA database_list").fetchone()[2]
            layers = wake_up(db_path, project)
            result["vito_identity"] = layers.get("l0", "")
            result["vito_context"] = layers.get("l1", "")
            if layers.get("l2"):
                result["vito_context"] += "\n\n" + layers["l2"]
            LOG(f"Vito layers loaded: {layers.get('total_tokens', 0)} tokens")
        except Exception as e:
            LOG(f"on_session_start vito_layers error: {e}")

        # Project context: rules and conventions from graph
        try:
            result["project_context"] = self._get_project_context(project)
        except Exception as e:
            LOG(f"on_session_start project_context error: {e}")

        # Recent unfinished episodes
        try:
            result["open_episodes"] = self._get_recent_episodes(project, days=3)
        except Exception as e:
            LOG(f"on_session_start open_episodes error: {e}")

        # Pending reflection proposals
        try:
            rows = self.db.execute(
                """SELECT id, type, content, confidence, created_at
                   FROM pending_proposals
                   WHERE status = 'pending'
                   ORDER BY confidence DESC
                   LIMIT 5"""
            ).fetchall()
            result["pending_proposals"] = [dict(r) for r in rows]
        except Exception as e:
            LOG(f"on_session_start pending_proposals error: {e}")

        # Active blind spots relevant to this project
        try:
            result["blind_spots"] = self._get_relevant_blind_spots(project)
        except Exception as e:
            LOG(f"on_session_start blind_spots error: {e}")

        # Recently used skills for this project
        try:
            result["recent_skills"] = self._get_recent_skills(project)
        except Exception as e:
            LOG(f"on_session_start recent_skills error: {e}")

        LOG(f"Session start context for '{project}': "
            f"{len(result['open_episodes'])} episodes, "
            f"{len(result['pending_proposals'])} proposals, "
            f"{len(result['blind_spots'])} blind spots")

        return result

    def on_query(self, query: str, project: str | None = None, *, scope: SearchScope | None = None) -> dict:
        """
        Called on every user message. Returns relevant context
        by activating concepts in the knowledge graph and finding
        related knowledge, rules, failures, and skills.
        """
        result: dict = {
            "activated_concepts": [],
            "relevant_rules": [],
            "past_failures": [],
            "available_solutions": [],
            "applicable_skills": [],
            "competency": None,
        }

        # Extract concept names from query (simple word extraction)
        concepts = self._extract_concepts(query)
        if not concepts:
            return result

        # Spreading activation through graph
        activated: dict[str, float] = {}
        if self.activation:
            try:
                seed_nodes = self.activation.find_seed_nodes(concepts)
                if seed_nodes:
                    activated = self.activation.spread(seed_nodes, depth=2)
                    result["activated_concepts"] = [
                        {"node_id": nid, "activation": score}
                        for nid, score in sorted(
                            activated.items(), key=lambda x: x[1], reverse=True
                        )[:15]
                    ]
            except Exception as e:
                LOG(f"on_query activation error: {e}")

        # Find rules from graph nodes
        try:
            result["relevant_rules"] = self._find_relevant_rules(
                concepts, project, activated, scope=scope
            )
        except Exception as e:
            LOG(f"on_query relevant_rules error: {e}")

        # Find past failures with similar concepts
        try:
            result["past_failures"] = self._find_past_failures(
                concepts, project, scope=scope
            )
        except Exception as e:
            LOG(f"on_query past_failures error: {e}")

        # Find available solutions
        try:
            result["available_solutions"] = self._find_solutions(
                concepts, project, activated, scope=scope
            )
        except Exception as e:
            LOG(f"on_query available_solutions error: {e}")

        # Find applicable skills
        try:
            result["applicable_skills"] = self._find_applicable_skills(
                concepts, project
            )
        except Exception as e:
            LOG(f"on_query applicable_skills error: {e}")

        # Self-assessment for the primary domain
        try:
            if concepts:
                result["competency"] = self._get_competency(concepts[0])
        except Exception as e:
            LOG(f"on_query competency error: {e}")

        return result

    def on_action_result(
        self,
        success: bool,
        domain: str,
        concepts: list[str] | None = None,
        skill_used: str | None = None,
    ) -> dict:
        """
        Called after an action completes. Updates skills and self-model.
        Returns: {"updates": [...]} list of what was updated.
        """
        updates: list[str] = []

        # Update competency for the domain
        try:
            self._update_competency(domain, success)
            updates.append(f"competency:{domain}")
        except Exception as e:
            LOG(f"on_action_result competency error: {e}")

        # Record skill usage if applicable
        if skill_used:
            try:
                self._record_skill_use(skill_used, success)
                updates.append(f"skill:{skill_used}")
            except Exception as e:
                LOG(f"on_action_result skill_use error: {e}")

        # Track blind spots on failure
        if not success and concepts:
            try:
                self._check_blind_spot(domain, concepts)
                updates.append("blind_spot_check")
            except Exception as e:
                LOG(f"on_action_result blind_spot error: {e}")

        # Reinforce graph edges for successful concept combinations
        if success and concepts and len(concepts) >= 2:
            try:
                self._reinforce_concept_edges(concepts)
                updates.append("graph_reinforcement")
            except Exception as e:
                LOG(f"on_action_result graph error: {e}")

        return {"updates": updates}

    def build_context(
        self,
        query: str,
        project: str | None = None,
        max_tokens: int = 4000,
    ) -> dict:
        """
        Build optimal context bundle for a query.
        Combines: activation + graph + episodes + skills + self-model.
        Fits within token budget.
        """
        bundle: dict = {
            "knowledge": [],
            "episodes": [],
            "skills": [],
            "rules": [],
            "competency": None,
            "blind_spots": [],
            "total_tokens": 0,
        }

        # Token budget allocation (approximate)
        budget = {
            "knowledge": int(max_tokens * 0.40),
            "episodes": int(max_tokens * 0.20),
            "skills": int(max_tokens * 0.15),
            "rules": int(max_tokens * 0.15),
            "meta": int(max_tokens * 0.10),
        }

        concepts = self._extract_concepts(query)
        activated: dict[str, float] = {}

        # Step 1: Activate concepts
        if self.activation and concepts:
            try:
                seeds = self.activation.find_seed_nodes(concepts)
                if seeds:
                    activated = self.activation.spread(seeds, depth=2)
            except Exception as e:
                LOG(f"build_context activation error: {e}")

        # Step 2: Retrieve knowledge via activation
        if activated and self.activation:
            try:
                memory_scores = self.activation.get_activated_memories(
                    activated, top_k=30
                )
                if memory_scores:
                    kid_list = [kid for kid, _ in memory_scores]
                    score_map = dict(memory_scores)
                    placeholders = ",".join("?" * len(kid_list))
                    rows = self.db.execute(
                        f"""SELECT id, type, content, project, tags, confidence, session_id, source, created_at
                            FROM knowledge
                            WHERE id IN ({placeholders}) AND status = 'active'
                              AND (? IS NULL OR project = ?)""",
                        [*kid_list, project, project],
                    ).fetchall()

                    knowledge_items = []
                    for row in rows:
                        item = dict(row)
                        item["activation_score"] = score_map.get(item["id"], 0)
                        knowledge_items.append(item)

                    # Sort by activation score
                    knowledge_items.sort(
                        key=lambda x: x["activation_score"], reverse=True
                    )
                    bundle["knowledge"] = _truncate_to_tokens(
                        knowledge_items, budget["knowledge"]
                    )
            except Exception as e:
                LOG(f"build_context knowledge error: {e}")

        # Fallback: FTS search if no activation results
        if not bundle["knowledge"] and concepts:
            try:
                from memory_core.query_terms import fts_match_query

                fts_query = fts_match_query(" ".join(concepts[:5]))
                rows = self.db.execute(
                    """SELECT k.id, k.type, k.content, k.project, k.tags, k.confidence,
                              k.session_id, k.source, k.created_at
                       FROM knowledge k
                       JOIN knowledge_fts fts ON k.id = fts.rowid
                       WHERE knowledge_fts MATCH ?
                         AND k.status = 'active' AND (? IS NULL OR k.project = ?)
                       ORDER BY rank
                       LIMIT 15""",
                    (fts_query, project, project),
                ).fetchall()
                items = [dict(r) for r in rows]
                bundle["knowledge"] = _truncate_to_tokens(
                    items, budget["knowledge"]
                )
            except Exception as e:
                LOG(f"build_context FTS fallback error: {e}")

        # Step 3: Recent episodes
        if project:
            try:
                bundle["episodes"] = _truncate_to_tokens(
                    self._get_recent_episodes(project, days=7),
                    budget["episodes"],
                    key="narrative",
                )
            except Exception as e:
                LOG(f"build_context episodes error: {e}")

        # Step 4: Applicable skills
        try:
            skills = self._find_applicable_skills(concepts, project)
            bundle["skills"] = _truncate_to_tokens(
                skills, budget["skills"], key="trigger_pattern"
            )
        except Exception as e:
            LOG(f"build_context skills error: {e}")

        # Step 5: Rules
        try:
            rules = self._find_relevant_rules(concepts, project, activated)
            bundle["rules"] = _truncate_to_tokens(
                rules, budget["rules"]
            )
        except Exception as e:
            LOG(f"build_context rules error: {e}")

        # Step 6: Competency and blind spots
        try:
            if concepts:
                bundle["competency"] = self._get_competency(concepts[0])
            if project:
                bundle["blind_spots"] = self._get_relevant_blind_spots(project)[:3]
        except Exception as e:
            LOG(f"build_context meta error: {e}")

        from memory_core.context import bound_context

        bundle = bound_context(bundle, max_tokens)
        total = bundle["total_tokens"]

        LOG(f"Built context: {len(bundle['knowledge'])} knowledge, "
            f"{len(bundle['episodes'])} episodes, "
            f"{len(bundle['skills'])} skills, "
            f"{total} tokens")

        return bundle

    # ──────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────

    def _extract_concepts(self, query: str) -> list[str]:
        """Extract concept names from a query string.

        Simple approach: split on whitespace and punctuation,
        filter out stopwords and short tokens, lowercase.
        """
        import re

        stopwords = frozenset({
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "will",
            "would", "could", "should", "may", "might", "can", "shall",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "as", "into", "through", "during", "before", "after", "above",
            "below", "between", "out", "off", "over", "under", "again",
            "further", "then", "once", "here", "there", "when", "where",
            "why", "how", "all", "each", "every", "both", "few", "more",
            "most", "other", "some", "such", "no", "nor", "not", "only",
            "own", "same", "so", "than", "too", "very", "just", "but",
            "and", "or", "if", "this", "that", "these", "those", "it",
            "its", "i", "me", "my", "we", "our", "you", "your", "he",
            "him", "his", "she", "her", "they", "them", "their", "what",
            "which", "who", "whom",
            # Russian stopwords
            "и", "в", "на", "с", "по", "для", "из", "к", "о", "от",
            "не", "как", "что", "это", "все", "так", "но", "да", "же",
            "ли", "бы", "уже", "при", "до",
        })

        # Split on non-alphanumeric (keep underscores and hyphens)
        tokens = re.split(r'[^\w-]+', query.lower())
        concepts = [
            t for t in tokens
            if len(t) >= 3 and t not in stopwords and not t.isdigit()
        ]

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for c in concepts:
            if c not in seen:
                seen.add(c)
                unique.append(c)

        return unique[:20]  # cap to avoid overly broad activation

    def _get_project_context(self, project: str) -> dict:
        """Get rules, conventions, and lessons for a project."""
        context: dict = {
            "rules": [],
            "conventions": [],
            "lessons": [],
        }

        # Rules from the rules table
        rows = self.db.execute(
            """SELECT id, content, category, priority
               FROM rules
               WHERE status = 'active'
                 AND (project = ? OR scope = 'global')
               ORDER BY priority DESC
               LIMIT 10""",
            (project,),
        ).fetchall()
        context["rules"] = [dict(r) for r in rows]

        # Conventions from graph nodes
        if self.graph_store:
            try:
                conv_rows = self.db.execute(
                    """SELECT gn.name, gn.content
                       FROM graph_nodes gn
                       JOIN knowledge_nodes kn ON kn.node_id = gn.id
                       JOIN knowledge k ON kn.knowledge_id = k.id
                       WHERE gn.type IN ('convention', 'rule', 'prohibition')
                         AND gn.status = 'active'
                         AND k.project = ?
                       GROUP BY gn.id
                       ORDER BY gn.importance DESC
                       LIMIT 10""",
                    (project,),
                ).fetchall()
                context["conventions"] = [dict(r) for r in conv_rows]
            except Exception:
                pass

        # Recent lessons for this project
        rows = self.db.execute(
            """SELECT content, confidence
               FROM knowledge
               WHERE project = ? AND type = 'lesson' AND status = 'active'
               ORDER BY created_at DESC
               LIMIT 5""",
            (project,),
        ).fetchall()
        context["lessons"] = [dict(r) for r in rows]

        return context

    def _get_recent_episodes(self, project: str, days: int = 3) -> list[dict]:
        """Get recent episodes for a project."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        rows = self.db.execute(
            """SELECT id, narrative, outcome, impact_score, key_insight,
                      concepts, timestamp
               FROM episodes
               WHERE project = ? AND timestamp >= ?
               ORDER BY timestamp DESC
               LIMIT 10""",
            (project, cutoff),
        ).fetchall()

        return [dict(r) for r in rows]

    def _get_relevant_blind_spots(self, project: str) -> list[dict]:
        """Get active blind spots relevant to the project."""
        rows = self.db.execute(
            """SELECT id, description, domains, severity
               FROM blind_spots
               WHERE status = 'active'
               ORDER BY severity DESC
               LIMIT 5"""
        ).fetchall()

        result: list[dict] = []
        for row in rows:
            bs = dict(row)
            domains = _parse_json(bs.get("domains"))
            # Include if project matches or domains is empty (global)
            if not domains or project in domains:
                result.append(bs)

        return result

    def _get_recent_skills(self, project: str) -> list[dict]:
        """Get recently used skills for this project."""
        rows = self.db.execute(
            """SELECT s.id, s.name, s.trigger_pattern, s.success_rate,
                      s.times_used, s.status
               FROM skills s
               WHERE s.status IN ('active', 'mastered')
                 AND (s.projects LIKE ? OR s.projects LIKE '%[]%')
               ORDER BY s.times_used DESC
               LIMIT 10""",
            (f"%{project}%",),
        ).fetchall()

        return [dict(r) for r in rows]

    def _find_relevant_rules(
        self,
        concepts: list[str],
        project: str | None,
        activated: dict[str, float],
        scope: SearchScope | None = None,
    ) -> list[dict]:
        """Find rules relevant to the given concepts."""
        rules: list[dict] = []
        scope = scope or SearchScope(project=project)
        project = scope.project

        # From rules table
        if concepts:
            like_clauses = " OR ".join(["content LIKE ?" for _ in concepts[:5]])
            params = [f"%{c}%" for c in concepts[:5]]
            if project:
                like_clauses = f"({like_clauses}) AND (project = ? OR scope = 'global')"
                params.append(project)

            rows = self.db.execute(
                f"""SELECT id, content, category, priority
                    FROM rules
                    WHERE status = 'active' AND ({like_clauses})
                    ORDER BY priority DESC
                    LIMIT 5""",
                params,
            ).fetchall()
            rules.extend(dict(r) for r in rows)

        # From graph nodes of type 'rule' or 'prohibition' via activation
        if activated and self.graph_store:
            activated_node_ids = list(activated.keys())[:20]
            if activated_node_ids:
                placeholders = ",".join("?" * len(activated_node_ids))
                graph_filter = ""
                scope_params = []
                if scope.as_of is not None or scope.project or scope.branch or scope.spaces or scope.kind != "all":
                    predicates, scope_params = scope.sql()
                    graph_filter = " AND EXISTS (SELECT 1 FROM knowledge_nodes kn JOIN knowledge k ON k.id=kn.knowledge_id WHERE kn.node_id=gn.id AND " + " AND ".join(predicates) + ")"
                rows = self.db.execute(
                    f"""SELECT gn.id, gn.name, gn.content, gn.type
                        FROM graph_nodes gn
                        WHERE gn.type IN ('rule', 'prohibition', 'convention')
                          AND gn.status = 'active'
                          AND EXISTS (SELECT 1 FROM graph_edges ge WHERE
                            (ge.source_id IN ({placeholders}) AND ge.target_id = gn.id)
                            OR (ge.target_id IN ({placeholders}) AND ge.source_id = gn.id))
                          {graph_filter}
                        ORDER BY gn.importance DESC
                        LIMIT 5""",
                    [*activated_node_ids, *activated_node_ids, *scope_params],
                ).fetchall()
                for row in rows:
                    r = dict(row)
                    r["source"] = "graph"
                    rules.append(r)

        return rules

    def _find_past_failures(
        self, concepts: list[str], project: str | None, scope: SearchScope | None = None,
    ) -> list[dict]:
        """Find past failure episodes with similar concepts."""
        if not concepts:
            return []
        scope = scope or SearchScope(project=project)
        project = scope.project

        # Search episodes with matching concepts and failure outcome
        like_clauses = " OR ".join(["concepts LIKE ?" for _ in concepts[:5]])
        params: list = [f"%{c}%" for c in concepts[:5]]

        project_filter = ""
        if project:
            project_filter = " AND project = ?"
            params.append(project)
        if scope.as_of is not None:
            from memory_core.snapshot import Snapshot
            instant = Snapshot.at(scope.as_of).instant
            project_filter += " AND julianday(timestamp)<=julianday(?) AND julianday(created_at)<=julianday(?)"
            params.extend([instant, instant])

        rows = self.db.execute(
            f"""SELECT id, narrative, key_insight, impact_score, timestamp
                FROM episodes
                WHERE outcome = 'failure'
                  AND ({like_clauses})
                  {project_filter}
                ORDER BY impact_score DESC, timestamp DESC
                LIMIT 5""",
            params,
        ).fetchall()

        return [dict(r) for r in rows]

    def _solutions_matching(self, concepts: list[str], project: str | None,
                            scope: SearchScope | None = None) -> list:
        """Active solutions whose content has a word starting with one of `concepts`.

        Uses the full-text index: a `LIKE '%concept%'` scan reads every
        solution in the store, which took 850 ms at 1M records. Falls back to
        that scan on databases without `knowledge_fts`.
        """
        scope = scope or SearchScope(project=project)
        if not concepts or scope.kind not in ("all", "solution"):
            return []
        predicates, params = replace(scope, kind="solution").sql()
        where = " AND ".join(predicates)
        terms = " OR ".join('"' + c.replace('"', '""') + '"*' for c in concepts)
        match = f"content : ({terms})"
        if scope.project:
            from memory_core.fts_schema import project_token
            match += f" AND fts_project : {project_token(scope.project)}"
        try:
            source = "SELECT rowid AS id FROM knowledge_fts WHERE knowledge_fts MATCH ?"
            source_params = [match]
            if getattr(self.db, "backend", None) is Backend.POSTGRES:
                from memory_core.pg_fts import match_source
                source, source_params = match_source("knowledge_fts", match)
            return self.db.execute(
                f"""WITH f AS MATERIALIZED ({source})
                    SELECT k.id, k.content, k.project, k.tags, k.confidence
                    FROM f JOIN knowledge k ON k.id = f.id
                    WHERE {where}
                    ORDER BY k.confidence DESC, k.recall_count DESC
                    LIMIT 10""",
                [*source_params, *params],
            ).fetchall()
        except sqlite3.OperationalError:
            like_clauses = " OR ".join("k.content LIKE ?" for _ in concepts)
            return self.db.execute(
                f"""SELECT k.id, k.content, k.project, k.tags, k.confidence
                    FROM knowledge k
                    WHERE ({like_clauses}) AND {where}
                    ORDER BY k.confidence DESC, k.recall_count DESC
                    LIMIT 10""",
                [f"%{c}%" for c in concepts] + params,
            ).fetchall()

    def _find_solutions(
        self,
        concepts: list[str],
        project: str | None,
        activated: dict[str, float],
        scope: SearchScope | None = None,
    ) -> list[dict]:
        """Find available solutions matching the concepts."""
        solutions: list[dict] = []

        scope = scope or SearchScope(project=project)
        if not concepts or scope.kind not in ("all", "solution"):
            return solutions
        scope = replace(scope, kind="solution")

        # From knowledge table
        rows = self._solutions_matching(concepts[:5], project, scope=scope)

        solutions.extend(dict(r) for r in rows)

        # From activation-linked knowledge
        if activated and self.activation:
            try:
                memory_scores = self.activation.get_activated_memories(
                    activated, top_k=10, scope=scope
                )
                if memory_scores:
                    existing_ids = {s["id"] for s in solutions}
                    kid_list = [
                        kid for kid, _ in memory_scores if kid not in existing_ids
                    ]
                    if kid_list:
                        records = fetch_active_records(self.db, kid_list, scope=scope)
                        solutions.extend(
                            {key: records[kid][key] for key in ("id", "content", "project", "tags", "confidence")}
                            for kid in kid_list if kid in records)
            except Exception:
                logging.getLogger(__name__).exception('Activated solution lookup failed',
                                                      extra={'project': project})

        return solutions[:10]

    def _find_applicable_skills(
        self, concepts: list[str], project: str | None
    ) -> list[dict]:
        """Find skills whose trigger patterns match the concepts."""
        if not concepts:
            return []

        like_clauses = " OR ".join(
            ["trigger_pattern LIKE ?" for _ in concepts[:5]]
        )
        params: list = [f"%{c}%" for c in concepts[:5]]
        project_values = "json_each(skills.projects)"
        if getattr(self.db, "backend", None) is Backend.POSTGRES:
            project_values = "jsonb_array_elements_text(CAST(skills.projects AS JSONB)) AS allowed(value)"

        rows = self.db.execute(
            f"""SELECT id, name, trigger_pattern, steps, success_rate,
                       times_used, status
                FROM skills
                WHERE status IN ('active', 'mastered')
                  AND ({like_clauses})
                  AND (CAST(? AS TEXT) IS NULL OR projects IS NULL OR projects = '[]'
                       OR EXISTS (SELECT 1 FROM {project_values} WHERE value=?))
                ORDER BY success_rate DESC, times_used DESC
                LIMIT 5""",
            [*params, project, project],
        ).fetchall()

        return [dict(r) for r in rows]

    def _get_competency(self, domain: str) -> dict | None:
        """Get self-assessment for a domain."""
        row = self.db.execute(
            "SELECT * FROM competencies WHERE domain = ?", (domain,)
        ).fetchone()

        if row:
            return dict(row)
        return None

    def _update_competency(self, domain: str, success: bool) -> None:
        """Update competency level for a domain based on action result."""
        row = self.db.execute(
            "SELECT * FROM competencies WHERE domain = ?", (domain,)
        ).fetchone()

        if row:
            level = row["level"]
            confidence = row["confidence"]
            based_on = row["based_on"]

            # Bayesian-like update
            delta = 0.02 if success else -0.03
            new_level = max(0.0, min(1.0, level + delta))

            # Confidence increases with more data points
            new_confidence = min(1.0, confidence + 0.01)
            new_based_on = based_on + 1

            # Determine trend
            if new_level > level + 0.01:
                trend = "improving"
            elif new_level < level - 0.01:
                trend = "declining"
            elif new_level < 0.3:
                trend = "stable_low"
            else:
                trend = "stable"

            self.db.execute(
                """UPDATE competencies
                   SET level = ?, confidence = ?, based_on = ?,
                       trend = ?, last_updated = ?
                   WHERE domain = ?""",
                (new_level, new_confidence, new_based_on, trend, _now(), domain),
            )
        else:
            # Create new competency entry
            initial_level = 0.5 if success else 0.3
            self.db.execute(
                """INSERT INTO competencies (domain, level, confidence, based_on, trend, last_updated)
                   VALUES (?, ?, 0.3, 1, 'unknown', ?)""",
                (domain, initial_level, _now()),
            )

        self.db.commit()

    def _record_skill_use(self, skill_id: str, success: bool) -> None:
        """Record a skill usage and update its stats."""
        # Check skill exists
        skill = self.db.execute(
            "SELECT id, times_used, success_rate FROM skills WHERE id = ?",
            (skill_id,),
        ).fetchone()

        if not skill:
            # Try by name
            skill = self.db.execute(
                "SELECT id, times_used, success_rate FROM skills WHERE name = ?",
                (skill_id,),
            ).fetchone()

        if not skill:
            return

        actual_id = skill["id"]
        times_used = skill["times_used"] + 1
        old_rate = skill["success_rate"]

        # Incremental average
        new_rate = old_rate + (1.0 if success else 0.0 - old_rate) / times_used

        self.db.execute(
            """UPDATE skills
               SET times_used = ?, success_rate = ?, last_refined_at = ?
               WHERE id = ?""",
            (times_used, round(new_rate, 4), _now(), actual_id),
        )

        # Record individual use
        self.db.execute(
            """INSERT INTO skill_uses (id, skill_id, success, used_at)
               VALUES (?, ?, ?, ?)""",
            (_new_id(), actual_id, success, _now()),
        )

        # Auto-promote draft -> active after 3 successful uses
        if success and times_used >= 3 and new_rate >= 0.6:
            self.db.execute(
                "UPDATE skills SET status = 'active' WHERE id = ? AND status = 'draft'",
                (actual_id,),
            )

        # Auto-promote active -> mastered after 10 uses with high success
        if times_used >= 10 and new_rate >= 0.9:
            self.db.execute(
                "UPDATE skills SET status = 'mastered' WHERE id = ? AND status = 'active'",
                (actual_id,),
            )

        self.db.commit()

    def _check_blind_spot(self, domain: str, concepts: list[str]) -> None:
        """Check if a failure pattern indicates a blind spot."""
        # Count recent failures with similar concepts
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=30)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        like_clauses = " OR ".join(["concepts LIKE ?" for _ in concepts[:5]])
        params: list = [f"%{c}%" for c in concepts[:5]]
        params.append(cutoff)

        count = self.db.execute(
            f"""SELECT COUNT(*) FROM episodes
                WHERE outcome = 'failure'
                  AND ({like_clauses})
                  AND timestamp >= ?""",
            params,
        ).fetchone()[0]

        # 3+ failures with same concepts = potential blind spot
        if count >= 3:
            description = f"Repeated failures ({count}x) in domain '{domain}' " \
                          f"involving: {', '.join(concepts[:5])}"

            # Check if already exists
            existing = self.db.execute(
                """SELECT id FROM blind_spots
                   WHERE description LIKE ? AND status = 'active'""",
                (f"%{domain}%",),
            ).fetchone()

            if not existing:
                self.db.execute(
                    """INSERT INTO blind_spots
                       (id, description, domains, evidence, severity, status, discovered_at)
                       VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                    (
                        _new_id(),
                        description,
                        json.dumps([domain]),
                        json.dumps({"failure_count": count, "concepts": concepts[:5]}),
                        min(0.3 + 0.1 * count, 1.0),
                        _now(),
                    ),
                )
                self.db.commit()
                LOG(f"New blind spot detected: {description}")

    def _reinforce_concept_edges(self, concepts: list[str]) -> None:
        """Strengthen edges between concepts that co-occurred in a successful action."""
        if not self.graph_store or len(concepts) < 2:
            return

        # Find node IDs for concepts
        node_ids: list[str] = []
        for name in concepts[:5]:
            node = self.db.execute(
                "SELECT id FROM graph_nodes WHERE LOWER(name) = LOWER(?) AND status = 'active'",
                (name,),
            ).fetchone()
            if node:
                node_ids.append(node["id"])

        # Reinforce edges between all pairs
        for i in range(len(node_ids)):
            for j in range(i + 1, len(node_ids)):
                try:
                    self.graph_store.add_edge(
                        node_ids[i],
                        node_ids[j],
                        "mentioned_with",
                        weight=0.3,
                        context="Co-occurred in successful action",
                    )
                except (ValueError, sqlite3.IntegrityError):
                    pass
