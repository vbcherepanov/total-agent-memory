"""
Concept Extractor — extract concepts, entities, and relations from text.

Two modes:
1. Fast local extraction (match against existing graph nodes) — <10ms
2. Deep extraction via Ollama LLM (create new concepts) — ~2-5 seconds

All graph operations use raw SQLite. Ollama calls use urllib (no deps).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from config import get_triple_max_predict, get_triple_timeout_sec

OLLAMA_URL = "http://localhost:11434"
LOG = lambda msg: sys.stderr.write(f"[memory-extractor] {msg}\n")


def _now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    """Generate a new UUID hex string."""
    return uuid.uuid4().hex


class ConceptExtractor:
    """Extract concepts, entities, and relations from text.

    Two modes:
    1. Fast local extraction (match against existing graph nodes) -- <10ms
    2. Deep extraction via Ollama (create new concepts) -- ~2-5 seconds
    """

    # Overridable via env; falls back to a model that's actually installed locally.
    import os as _os
    OLLAMA_MODEL: str = _os.environ.get("MEMORY_LLM_MODEL", "qwen2.5-coder:7b")

    EXTRACTION_PROMPT: str = """Analyze this content and extract structured information.
Return ONLY valid JSON, no explanation.

{{
  "concepts": [
    {{"name": "concept_name_lowercase", "category": "domain|pattern|technology", "strength": 0.9}}
  ],
  "capabilities": ["what this code/solution CAN DO"],
  "composable_with": ["what other systems this needs or works with"],
  "entities": [
    {{"name": "entity_name", "type": "person|project|company|technology"}}
  ],
  "relations": [
    {{"source": "entity_a", "target": "entity_b", "type": "relation_type"}}
  ],
  "key_patterns": ["architectural patterns used"]
}}

Content:
{content}"""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._node_names_cache: dict[str, dict] | None = None
        self._cache_timestamp: float = 0

    def extract_fast(self, text: str) -> dict:
        """Fast local extraction by matching against existing graph nodes.

        Tokenizes text, matches against node names. No LLM call.
        <10ms for typical inputs.

        Returns:
            {"concepts": [{"id": str, "name": str, "strength": float}],
             "entities": [{"id": str, "name": str, "type": str}]}
        """
        if not text:
            return {"concepts": [], "entities": []}

        node_cache = self._get_node_names()
        if not node_cache:
            return {"concepts": [], "entities": []}

        tokens = self._tokenize(text)
        concepts: list[dict] = []
        entities: list[dict] = []
        seen_ids: set[str] = set()

        for token in tokens:
            if token in node_cache:
                node = node_cache[token]
                nid = node["id"]
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)

                entry = {"id": nid, "name": node["name"], "strength": 1.0}
                node_type = node.get("type", "")

                if node_type in ("person", "project", "company", "technology"):
                    entry["type"] = node_type
                    entities.append(entry)
                else:
                    concepts.append(entry)

        return {"concepts": concepts, "entities": entities}

    def extract_deep(self, text: str) -> dict:
        """Deep extraction via Ollama. Creates new graph nodes if needed.

        ~2-5 seconds depending on text length and model speed.

        Returns full extraction result with concepts, capabilities,
        composable_with, entities, relations, key_patterns.
        Returns empty structure on Ollama failure.
        """
        empty: dict[str, Any] = {
            "concepts": [],
            "capabilities": [],
            "composable_with": [],
            "entities": [],
            "relations": [],
            "key_patterns": [],
        }

        if not text:
            return empty

        # Skip silently if no LLM is configured (no errors, no logs)
        try:
            from config import has_llm
            if not has_llm():
                return empty
        except Exception:
            pass

        # Truncate very long text to avoid overwhelming the model
        max_chars = 6000
        truncated = text[:max_chars] if len(text) > max_chars else text

        prompt = self.EXTRACTION_PROMPT.format(content=truncated)
        raw_response = self._ollama_generate(prompt)
        if raw_response is None:
            LOG("Deep extraction failed: no response from Ollama")
            return empty

        # Parse JSON from response — handle markdown fences and stray text
        parsed = self._parse_json_response(raw_response)
        if parsed is None:
            LOG(f"Deep extraction failed: could not parse JSON from response")
            return empty

        # Validate and normalize structure
        result: dict[str, Any] = {}
        for key in ("concepts", "capabilities", "composable_with", "entities", "relations", "key_patterns"):
            val = parsed.get(key, [])
            result[key] = val if isinstance(val, list) else []

        # Normalize concept names to lowercase
        for concept in result["concepts"]:
            if isinstance(concept, dict) and "name" in concept:
                concept["name"] = str(concept["name"]).lower().strip()
                if "strength" not in concept:
                    concept["strength"] = 0.8
                if "category" not in concept:
                    concept["category"] = "domain"

        # Normalize entity types
        valid_entity_types = {"person", "project", "company", "technology"}
        for entity in result["entities"]:
            if isinstance(entity, dict):
                if entity.get("type") not in valid_entity_types:
                    entity["type"] = "technology"

        LOG(
            f"Deep extraction: {len(result['concepts'])} concepts, "
            f"{len(result['entities'])} entities, {len(result['relations'])} relations"
        )
        return result

    def extract_and_link(
        self,
        text: str,
        knowledge_id: int | None = None,
        deep: bool = False,
    ) -> dict:
        """Extract concepts and optionally link to a knowledge record.

        Uses fast extraction by default, deep if requested.
        If knowledge_id provided, creates knowledge_nodes links.
        Creates new graph_nodes for concepts not yet in graph.
        Creates graph_edges for extracted relations.

        Returns extraction result.
        """
        if deep:
            result = self.extract_deep(text)
        else:
            result = self.extract_fast(text)

        # Ensure all concepts exist as graph nodes
        concept_node_ids: list[tuple[str, float]] = []
        for concept in result.get("concepts", []):
            if not isinstance(concept, dict):
                continue
            name = concept.get("name", "")
            if not name:
                continue
            strength = float(concept.get("strength", 0.8))
            category = concept.get("category", "domain")
            node_id = self._ensure_node(name, type=category)
            concept["id"] = node_id
            concept_node_ids.append((node_id, strength))

        # Ensure all entities exist as graph nodes
        entity_node_ids: list[tuple[str, float]] = []
        for entity in result.get("entities", []):
            if not isinstance(entity, dict):
                continue
            name = entity.get("name", "")
            if not name:
                continue
            etype = entity.get("type", "technology")
            node_id = self._ensure_node(name, type=etype)
            entity["id"] = node_id
            entity_node_ids.append((node_id, 1.0))

        # Link to knowledge record if provided
        if knowledge_id is not None:
            all_links = concept_node_ids + entity_node_ids
            for node_id, strength in all_links:
                try:
                    self.db.execute(
                        """INSERT OR REPLACE INTO knowledge_nodes
                           (knowledge_id, node_id, role, strength)
                           VALUES (?, ?, 'related', ?)""",
                        (knowledge_id, node_id, strength),
                    )
                except sqlite3.Error as exc:
                    LOG(f"Failed to link knowledge {knowledge_id} -> node {node_id}: {exc}")

        # Create graph edges for extracted relations (deep mode only)
        for relation in result.get("relations", []):
            if not isinstance(relation, dict):
                continue
            source_name = str(relation.get("source", "")).lower().strip()
            target_name = str(relation.get("target", "")).lower().strip()
            rel_type = str(relation.get("type", "related")).lower().strip()

            if not source_name or not target_name or source_name == target_name:
                continue

            source_id = self._ensure_node(source_name, type="concept")
            target_id = self._ensure_node(target_name, type="concept")

            try:
                # Check for existing edge
                existing = self.db.execute(
                    """SELECT id, weight FROM graph_edges
                       WHERE source_id = ? AND target_id = ? AND relation_type = ?""",
                    (source_id, target_id, rel_type),
                ).fetchone()

                if existing:
                    new_weight = min((existing[1] or 1.0) + 0.1, 10.0)
                    self.db.execute(
                        """UPDATE graph_edges
                           SET weight = ?, last_reinforced_at = ?,
                               reinforcement_count = reinforcement_count + 1
                           WHERE id = ?""",
                        (new_weight, _now(), existing[0]),
                    )
                else:
                    edge_id = _new_id()
                    self.db.execute(
                        """INSERT INTO graph_edges
                           (id, source_id, target_id, relation_type, weight, created_at)
                           VALUES (?, ?, ?, ?, 1.0, ?)""",
                        (edge_id, source_id, target_id, rel_type, _now()),
                    )
            except sqlite3.Error as exc:
                LOG(f"Failed to create edge {source_name} -> {target_name}: {exc}")

        try:
            self.db.commit()
        except sqlite3.Error as exc:
            LOG(f"Commit failed after extract_and_link: {exc}")

        # Invalidate node cache since we may have created new nodes
        self._node_names_cache = None

        return result

    def _get_node_names(self) -> dict[str, dict]:
        """Cache of all graph node names for fast matching. Refreshes every 60s."""
        now = time.monotonic()
        if self._node_names_cache is not None and (now - self._cache_timestamp) < 60:
            return self._node_names_cache

        cache: dict[str, dict] = {}
        try:
            rows = self.db.execute(
                "SELECT id, name, type FROM graph_nodes WHERE status = 'active'"
            ).fetchall()
            for row in rows:
                name_lower = row[1].lower().strip() if row[1] else ""
                if name_lower:
                    cache[name_lower] = {
                        "id": row[0],
                        "name": row[1],
                        "type": row[2],
                    }
        except sqlite3.Error as exc:
            LOG(f"Failed to load node names: {exc}")
            return cache

        self._node_names_cache = cache
        self._cache_timestamp = now
        LOG(f"Node cache refreshed: {len(cache)} nodes")
        return cache

    def _tokenize(self, text: str) -> set[str]:
        """Tokenize text into words, bigrams, and trigrams (lowercased)."""
        # Extract words (at least 2 chars, alphanumeric + hyphens/underscores)
        words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9][\w-]{1,}", text.lower())
        tokens: set[str] = set()

        for word in words:
            clean = word.strip("-_")
            if len(clean) >= 2:
                tokens.add(clean)

        # Bigrams from consecutive words
        for i in range(len(words) - 1):
            bigram = f"{words[i]} {words[i + 1]}"
            tokens.add(bigram)

        # Trigrams from consecutive words
        for i in range(len(words) - 2):
            trigram = f"{words[i]} {words[i + 1]} {words[i + 2]}"
            tokens.add(trigram)

        return tokens

    def _ollama_generate(self, prompt: str) -> str | None:
        """Call Ollama generate API. Returns response text or None on error."""
        payload = json.dumps(
            {
                "model": self.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": get_triple_max_predict(),
                },
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=get_triple_timeout_sec()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("response", "")
        except urllib.error.URLError as exc:
            LOG(f"Ollama request failed: {exc}")
            return None
        except (json.JSONDecodeError, OSError) as exc:
            LOG(f"Ollama response error: {exc}")
            return None

    def _parse_json_response(self, text: str) -> dict | None:
        """Extract and parse JSON from LLM response, handling markdown fences."""
        # Try direct parse first
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code fences
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Try finding first { ... } block
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def _ensure_node(self, name: str, type: str, **kwargs: Any) -> str:
        """Get or create graph node. Returns node_id."""
        name_clean = name.lower().strip()
        if not name_clean:
            raise ValueError("Node name cannot be empty")

        # Check cache first for fast path
        cache = self._get_node_names()
        if name_clean in cache:
            node_id = cache[name_clean]["id"]
            # Touch: update last_seen_at and mention_count
            try:
                self.db.execute(
                    """UPDATE graph_nodes
                       SET last_seen_at = ?, mention_count = mention_count + 1
                       WHERE id = ?""",
                    (_now(), node_id),
                )
            except sqlite3.Error:
                pass
            return node_id

        # Check DB directly (cache might be stale)
        try:
            row = self.db.execute(
                "SELECT id FROM graph_nodes WHERE name = ? AND type = ?",
                (name_clean, type),
            ).fetchone()
            if row:
                node_id = row[0]
                self.db.execute(
                    """UPDATE graph_nodes
                       SET last_seen_at = ?, mention_count = mention_count + 1
                       WHERE id = ?""",
                    (_now(), node_id),
                )
                return node_id
        except sqlite3.Error as exc:
            LOG(f"DB check for node '{name_clean}' failed: {exc}")

        # Create new node
        node_id = _new_id()
        now = _now()
        try:
            self.db.execute(
                """INSERT INTO graph_nodes
                   (id, type, name, source, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, 'auto', ?, ?)""",
                (node_id, type, name_clean, now, now),
            )
            # Invalidate cache
            self._node_names_cache = None
            LOG(f"Created node: '{name_clean}' ({type}) -> {node_id}")
        except sqlite3.IntegrityError:
            # Race condition: node was created between check and insert
            row = self.db.execute(
                "SELECT id FROM graph_nodes WHERE name = ? AND type = ?",
                (name_clean, type),
            ).fetchone()
            if row:
                return row[0]
            raise
        except sqlite3.Error as exc:
            LOG(f"Failed to create node '{name_clean}': {exc}")
            raise

        return node_id
