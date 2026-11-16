"""Smart query router — v10 P2.9.

Beever Atlas's QA agent has a "smart router" that picks between vector
RAG and graph traversal per question. We need the same: a question like
"who worked on X with Y?" or "when did we last touch the migration?" is
*relational* — pure cosine on a vector store can't answer it
efficiently because the answer lives in the *edges*, not the document
text.

The router runs as a cheap heuristic classifier (no LLM, no embedding
calls) on top of `memory_recall(query)` and returns:

  * **relational** — strong relational signals (WH-words, multiple
    capitalised entity tokens, explicit "between X and Y" / "связан с"
    / "works with" patterns). Recall should consult the graph
    (`episodic.find_co_mentioned_events`, knowledge linked via
    `knowledge_nodes`) BEFORE falling back to hybrid search.
  * **semantic** — fact-shaped questions ("what database are we using",
    "how do we deploy") that benefit from vector similarity.
  * **hybrid** — default. Mixed signals; current 6-tier fusion already
    handles this best.

The classifier is bilingual (English + Russian) because the user's
own queries flip between the two within a single session.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable

from memory_core.retrieval import SearchScope, fetch_active_records

LOG = lambda msg: sys.stderr.write(f"[query-router] {msg}\n")


# ──────────────────────────────────────────────
# Patterns
# ──────────────────────────────────────────────

# WH-question openers (English + Russian). Strong relational signal when
# combined with two or more named entities.
_WH_PATTERN = re.compile(
    r"\b("
    r"who|whom|whose|which|where|when|why|how\s+often|how\s+many|"
    r"кто|кому|чей|чья|чьё|который|которая|где|куда|откуда|когда|почему|"
    r"сколько|каким\s+образом"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# Explicit relational connectors.
_RELATIONAL_CONNECTORS = re.compile(
    r"\b("
    r"between|together\s+with|along\s+with|alongside|"
    r"related\s+to|connected\s+to|linked\s+to|associated\s+with|"
    r"works?\s+(?:with|on)|owns|owned\s+by|created\s+by|maintained\s+by|"
    r"depends?\s+on|uses?|integrate[sd]?\s+with|replaced\s+by|"
    r"contradict[s]?|supersede[sd]?|"
    r"между|связан[аыо]?\s+с|связь\s+между|вместе\s+с|"
    r"работа(?:ет|ют|л[аи]?)\s+(?:над|с)|"
    r"использу(?:ет|ют|л[аи]?)|зависит\s+от"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)

# Entity-like tokens: capitalised words 3+ chars, or snake_case /
# kebab-case identifiers. Stop-list keeps common WH-words and project
# words from being counted as entities.
_ENTITY_LIKE = re.compile(
    r"\b("
    r"[A-ZА-Я][a-zа-я0-9_-]{2,}|"     # Capitalised word
    r"[a-z][a-z0-9_]{2,}_[a-z0-9_]+|"  # snake_case
    r"[a-z][a-z0-9-]{2,}-[a-z0-9-]+"   # kebab-case
    r")\b",
    re.UNICODE,
)

_ENTITY_STOPLIST = frozenset({
    "what", "which", "where", "when", "who", "how", "why",
    "the", "and", "but", "for", "with", "from", "into", "onto",
    "что", "кто", "где", "когда", "почему", "как", "какой",
    "это", "тот", "такой",
})


# ──────────────────────────────────────────────
# Data
# ──────────────────────────────────────────────


@dataclass
class QueryClassification:
    """Verdict from the heuristic router. Always populated, never None."""
    kind: str                   # 'relational' | 'semantic' | 'hybrid'
    confidence: float           # 0.0–1.0
    entities: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# Env knobs
# ──────────────────────────────────────────────


def _enabled() -> bool:
    raw = os.environ.get("MEMORY_SMART_ROUTER", "auto").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _force_kind() -> str | None:
    raw = os.environ.get("MEMORY_SMART_ROUTER_FORCE")
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw in ("relational", "semantic", "hybrid"):
        return raw
    return None


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


def extract_entity_candidates(query: str) -> list[str]:
    """Return capitalised / identifier-shaped tokens that look like entities.

    Used by the relational path to decide which entity names to look up
    in the graph. Order is preserved (dedup keeps first occurrence).
    """
    if not query:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _ENTITY_LIKE.finditer(query):
        token = match.group(0)
        norm = token.lower()
        if norm in _ENTITY_STOPLIST:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(token)
    return out


def classify_query(query: str) -> QueryClassification:
    """Heuristic classifier — sub-millisecond, no model calls."""
    if not query or not query.strip():
        return QueryClassification(kind="hybrid", confidence=0.0,
                                    entities=[], signals=["empty"])

    # Hard override for ops/CI testing — saves us from "MEMORY_*=true,
    # but I want to verify the relational path locally" gymnastics.
    forced = _force_kind()
    if forced:
        return QueryClassification(
            kind=forced, confidence=1.0,
            entities=extract_entity_candidates(query),
            signals=[f"forced-by-env={forced}"],
        )

    if not _enabled():
        return QueryClassification(
            kind="hybrid", confidence=0.0,
            entities=extract_entity_candidates(query),
            signals=["router disabled"],
        )

    signals: list[str] = []
    score_relational = 0.0

    if _WH_PATTERN.search(query):
        signals.append("wh-word")
        score_relational += 0.35

    connector_match = _RELATIONAL_CONNECTORS.search(query)
    if connector_match:
        signals.append(f"connector:{connector_match.group(0).lower()}")
        score_relational += 0.45

    entities = extract_entity_candidates(query)
    if len(entities) >= 2:
        signals.append(f"entities:{len(entities)}")
        score_relational += 0.35
    elif len(entities) == 1:
        signals.append("entity:1")
        score_relational += 0.10

    # Question mark on its own is weak signal — many semantic questions
    # also end with one. Don't count it.

    score_relational = min(1.0, score_relational)

    if score_relational >= 0.6:
        kind = "relational"
    elif score_relational >= 0.3:
        kind = "hybrid"
    elif entities and len(query.split()) <= 5:
        # Short query mostly composed of entity tokens → semantic lookup
        # by name is the cheapest answer.
        kind = "semantic"
        signals.append("short-entity-query")
    else:
        kind = "semantic"

    return QueryClassification(
        kind=kind,
        confidence=score_relational if kind == "relational" else 1.0 - score_relational,
        entities=entities,
        signals=signals,
    )


# ──────────────────────────────────────────────
# Graph-side resolver — used by relational path
# ──────────────────────────────────────────────


def graph_search(
    db,
    *,
    entities: list[str],
    project: str | None = None,
    limit: int = 10,
    scope: SearchScope | None = None,
) -> list[dict]:
    """Walk the graph for the given entity names and return knowledge
    rows referenced by their Events. Newest-first.

    Strategy:
      * 2+ entities → `find_co_mentioned_events` (intersection)
      * 1 entity   → `find_events_for_entity`
      * Translate event → knowledge_id (EventHit carries it).
      * Fetch the knowledge rows in a single SELECT.
    """
    if not entities:
        return []
    scope = scope or SearchScope(project=project)
    try:
        import episodic as _ep
        if len(entities) >= 2:
            hits = _ep.find_co_mentioned_events(
                db, entity_a=entities[0], entity_b=entities[1],
                project=project, limit=limit, scope=scope,
            )
        else:
            hits = _ep.find_events_for_entity(
                db, entity_name=entities[0], project=project, limit=limit, scope=scope,
            )
    except Exception as exc:
        LOG(f"graph_search failed: {exc}")
        return []

    knowledge_ids = [h.knowledge_id for h in hits if h.knowledge_id is not None]
    if not knowledge_ids:
        return []

    try:
        by_id = fetch_active_records(db, knowledge_ids, scope=scope)
    except Exception as exc:
        LOG(f"graph_search knowledge fetch failed: {exc}")
        return []

    # Preserve newest-first ordering from the graph hits.
    out: list[dict] = []
    for h in hits:
        row = by_id.get(h.knowledge_id)
        if row is None:
            continue
        d = dict(row)
        d["_via"] = "graph_router"
        d["_event_node_id"] = h.event_node_id
        out.append(d)
    return out
