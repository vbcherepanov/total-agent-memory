"""
Associative Recall — full pipeline: extract -> activate -> find -> compose.

Orchestrates the complete associative memory retrieval process:
1. Extract concepts from query text by matching against graph nodes
2. Find seed nodes in the knowledge graph
3. Spread activation through the graph
4. Retrieve memories connected to activated nodes
5. Optionally compose a unified solution from multiple sources
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from typing import Any

from .activation import SpreadingActivation
from .composition import CompositionEngine

LOG = lambda msg: sys.stderr.write(f"[memory-recall] {msg}\n")

# Common English stop words to skip during concept extraction
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "must", "need", "dare",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "into", "through", "during", "before", "after", "above", "below",
    "between", "out", "off", "over", "under", "again", "further", "then",
    "once", "here", "there", "when", "where", "why", "how", "all", "each",
    "every", "both", "few", "more", "most", "other", "some", "such", "no",
    "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "because", "but", "and", "or", "if", "while", "about", "up",
    "it", "its", "i", "me", "my", "we", "our", "you", "your", "he", "him",
    "his", "she", "her", "they", "them", "their", "this", "that", "these",
    "those", "what", "which", "who", "whom", "use", "using", "used",
})

# Regex to split text into tokens (word boundaries)
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\-\.#+/]+")


class AssociativeRecall:
    """Full associative recall pipeline: extract -> activate -> find -> compose."""

    def __init__(
        self,
        db: sqlite3.Connection,
        activation: SpreadingActivation,
        composition: CompositionEngine,
    ) -> None:
        self.db = db
        self.activation = activation
        self.composition = composition
        # Cache of graph node names for fast concept extraction
        self._node_name_cache: dict[str, str] | None = None
        self._node_name_cache_size: int = 0

    def recall(
        self,
        query: str,
        project: str | None = None,
        mode: str = "recall",
        max_results: int = 10,
        min_coverage: float = 0.7,
    ) -> dict[str, Any]:
        """Full associative recall.

        Args:
            query: Natural language query string.
            project: Optional project name filter for knowledge records.
            mode: "recall" (find related memories) or "composition"
                  (build a solution from parts).
            max_results: Maximum number of memories to return.
            min_coverage: Minimum coverage percentage for composition mode
                          (0.0 to 1.0).

        Returns:
            Dict with keys:
                query_concepts: list of extracted concept names
                seed_nodes: list of {id, name} dicts
                activated_count: number of activated nodes
                memories: list of memory dicts with score
                composition: composition result dict (only in composition mode)
        """
        # Step 1: Extract concepts from query
        concepts = self.extract_concepts(query)
        LOG(f"recall: extracted {len(concepts)} concepts from query")

        if not concepts:
            LOG("recall: no concepts extracted, returning empty result")
            return {
                "query_concepts": [],
                "seed_nodes": [],
                "activated_count": 0,
                "memories": [],
                "composition": None,
            }

        # Step 2: Find seed nodes
        seed_ids = self.activation.find_seed_nodes(concepts)

        # Fetch seed node details for the response
        seed_nodes: list[dict[str, str]] = []
        if seed_ids:
            placeholders = ",".join("?" * len(seed_ids))
            rows = self.db.execute(
                f"SELECT id, name FROM graph_nodes WHERE id IN ({placeholders})",
                seed_ids,
            ).fetchall()
            seed_nodes = [{"id": r[0], "name": r[1]} for r in rows]

        if not seed_ids:
            LOG("recall: no seed nodes found in graph")
            return {
                "query_concepts": concepts,
                "seed_nodes": [],
                "activated_count": 0,
                "memories": [],
                "composition": None,
            }

        # Step 3: Spread activation
        activation_map = self.activation.spread(seed_ids, depth=2)

        # Also include seed nodes in the activation map for memory lookup
        full_activation = {nid: 1.0 for nid in seed_ids}
        full_activation.update(activation_map)

        # Step 4: Find memories connected to activated nodes
        memory_scores = self.activation.get_activated_memories(
            full_activation, top_k=max_results * 2  # fetch extra for filtering
        )

        if not memory_scores:
            LOG("recall: no memories connected to activated nodes")
            return {
                "query_concepts": concepts,
                "seed_nodes": seed_nodes,
                "activated_count": len(activation_map),
                "memories": [],
                "composition": None,
            }

        # Step 5: Load full memory records
        memory_ids = [mid for mid, _ in memory_scores]
        score_lookup = dict(memory_scores)
        memories = self._load_memories(memory_ids, project)

        # Attach scores and sort
        for mem in memories:
            mem["activation_score"] = score_lookup.get(mem["id"], 0.0)
        memories.sort(key=lambda m: m["activation_score"], reverse=True)
        memories = memories[:max_results]

        # Step 6: Composition mode
        composition_result = None
        if mode == "composition" and memories:
            composition_result = self.composition.compose(
                needed_concepts=concepts,
                candidate_memories=memories,
                max_sources=5,
            )
            # Check coverage against minimum
            if composition_result["coverage_percent"] < min_coverage * 100:
                LOG(
                    f"recall: composition coverage {composition_result['coverage_percent']}% "
                    f"below minimum {min_coverage * 100}%"
                )

        return {
            "query_concepts": concepts,
            "seed_nodes": seed_nodes,
            "activated_count": len(activation_map),
            "memories": memories,
            "composition": composition_result,
        }

    def extract_concepts(self, text: str) -> list[str]:
        """Extract concept names from text by matching against existing graph nodes.

        Strategy:
        1. Load all active graph node names into a lookup cache
        2. Tokenize text into unigrams, bigrams, and trigrams
        3. Match tokens against cached node names using:
           - Exact match (case-insensitive)
           - Word-boundary matches of complete concept names
           - Underscore-aware: "error_handling" also matches "error handling"
        4. Return matching concept names (deduplicated, original casing)

        This is a FAST local extraction (no LLM), suitable for real-time use.
        Target: <10ms for typical queries.

        Args:
            text: Natural language text to extract concepts from.

        Returns:
            List of concept names found in the text, deduplicated.
        """
        if not text or not text.strip():
            return []

        # Ensure node name cache is loaded
        name_lookup = self._get_node_name_cache()
        if not name_lookup:
            return []

        # Tokenize
        tokens = _TOKEN_RE.findall(text)
        tokens_lower = [t.lower() for t in tokens]

        found: list[str] = []
        seen: set[str] = set()

        # Phase 1: Check ngrams for exact matches (longer matches first)
        for n in (3, 2, 1):
            for i in range(len(tokens_lower) - n + 1):
                ngram = " ".join(tokens_lower[i : i + n])

                # Skip pure stop words for unigrams
                if n == 1 and ngram in _STOP_WORDS:
                    continue

                # Skip short tokens for unigrams
                if n == 1 and len(ngram) < 2:
                    continue

                if ngram in name_lookup:
                    original_name = name_lookup[ngram]
                    key = original_name.lower()
                    if key not in seen:
                        found.append(original_name)
                        seen.add(key)

        text_lower = text.lower()
        for node_lower, original_name in name_lookup.items():
            key = original_name.lower()
            if key in seen:
                continue

            if len(node_lower) < 2:
                continue
            parts = re.split(r"[\s_]+", node_lower)
            pattern = r"(?<!\w)" + r"[\s_]+".join(re.escape(part) for part in parts) + r"(?!\w)"
            if re.search(pattern, text_lower):
                found.append(original_name)
                seen.add(key)

        return found

    def _get_node_name_cache(self) -> dict[str, str]:
        """Get or build the node name cache.

        Returns a dict mapping lowercase_name -> original_name for all
        active graph nodes. Cache is invalidated when node count changes.

        Returns:
            Dict mapping lowercased node name to original node name.
        """
        # Check if cache needs refresh
        count_row = self.db.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE status = 'active'"
        ).fetchone()
        current_count = count_row[0] if count_row else 0

        if self._node_name_cache is not None and self._node_name_cache_size == current_count:
            return self._node_name_cache

        # Rebuild cache
        rows = self.db.execute(
            "SELECT name FROM graph_nodes WHERE status = 'active'"
        ).fetchall()

        cache: dict[str, str] = {}
        for row in rows:
            name = row[0]
            if name:
                cache[name.lower()] = name

        self._node_name_cache = cache
        self._node_name_cache_size = current_count
        LOG(f"_get_node_name_cache: loaded {len(cache)} node names")
        return cache

    def _load_memories(
        self, memory_ids: list[int], project: str | None = None
    ) -> list[dict[str, Any]]:
        """Load full knowledge records by IDs.

        Args:
            memory_ids: List of knowledge record IDs to load.
            project: Optional project filter. If set, only returns
                     memories from that project.

        Returns:
            List of memory dicts with all knowledge table fields.
        """
        if not memory_ids:
            return []

        placeholders = ",".join("?" * len(memory_ids))
        params: list[Any] = list(memory_ids)

        project_filter = ""
        if project:
            project_filter = " AND project = ?"
            params.append(project)

        rows = self.db.execute(
            f"""SELECT id, session_id, type, content, context, project, tags,
                       status, confidence, source, created_at, recall_count
                FROM knowledge
                WHERE id IN ({placeholders})
                  AND status = 'active'
                  {project_filter}""",
            params,
        ).fetchall()

        memories: list[dict[str, Any]] = []
        for row in rows:
            tags_raw = row[6]
            try:
                tags = json.loads(tags_raw) if tags_raw else []
            except (json.JSONDecodeError, TypeError):
                tags = []

            memories.append(
                {
                    "id": row[0],
                    "session_id": row[1],
                    "type": row[2],
                    "content": row[3],
                    "context": row[4],
                    "project": row[5],
                    "tags": tags,
                    "status": row[7],
                    "confidence": row[8],
                    "source": row[9],
                    "created_at": row[10],
                    "recall_count": row[11],
                }
            )

        return memories
