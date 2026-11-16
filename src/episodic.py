"""Episodic links (Entity → Event → Fact) — v10 P2.10.

Beever Atlas's persister wires every fact through an Event node so the
relationships between entities become first-class graph citizens
("Bob WORKS_ON migration X", "Bob and Alice were both mentioned in the
same conversation about Postgres"). Pure vector RAG misses this — a
similarity search returns the fact text, not the relationships around it.

We piggy-back on the existing graph schema:

  * Each `save_knowledge` spawns one **Event node**
    (`graph_nodes.type='event'`, name = `save:<knowledge_id>:<iso_ts>`,
    properties hold project + session_id + timestamp).
  * Every entity-typed graph node already linked to the new knowledge row
    via `knowledge_nodes` is connected to the Event by a
    `mentioned_in` edge (entity → MENTIONED_IN → event).
  * The Event itself is wired to the Knowledge row through
    `knowledge_nodes` with `role='represents'`.

This module exposes the writer (`record_save_event`) plus two query
helpers (`find_events_for_entity`, `find_co_mentioned_events`) that the
upcoming smart query router and project wiki use.

Failure mode: any error in the writer simply skips event creation. The
underlying `save_knowledge` must never break because graph plumbing
hiccups.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from memory_core.retrieval import SearchScope

LOG = lambda msg: sys.stderr.write(f"[episodic] {msg}\n")

EVENT_NODE_TYPE = "event"
MENTIONED_IN = "mentioned_in"
REPRESENTS = "represents"

# Entity-flavoured types. Tag and project nodes don't qualify as
# "entities" for episodic linking (every save is in *some* project; tags
# explode combinatorially) — keeping them out of the MENTIONED_IN edges
# stops the graph from drowning in noise.
_ENTITY_TYPES = (
    "technology", "project", "company", "person", "concept",
    "tool", "doc", "article", "repo", "pattern", "skill",
)


# ──────────────────────────────────────────────
# Env knobs
# ──────────────────────────────────────────────


def _enabled() -> bool:
    raw = os.environ.get("MEMORY_EPISODIC_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _max_entities_per_event() -> int:
    """Cap on entity-side edges per event. Saves with very long tag
    lists otherwise create dozens of low-value edges."""
    raw = os.environ.get("MEMORY_EPISODIC_MAX_ENTITIES_PER_EVENT")
    if not raw:
        return 12
    try:
        return max(1, int(raw))
    except ValueError:
        return 12


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class EventRecord:
    node_id: str
    name: str
    knowledge_id: int
    entity_count: int


# ──────────────────────────────────────────────
# Writer — used by save_knowledge
# ──────────────────────────────────────────────


def record_save_event(
    db,
    *,
    knowledge_id: int,
    project: str | None,
    session_id: str | None,
) -> EventRecord | None:
    """Create the Event node and the surrounding edges. Returns the
    EventRecord on success, None when disabled / no entities to link /
    on any swallowed error.
    """
    if not _enabled():
        return None
    if knowledge_id is None:
        return None

    # Find entity-typed nodes already linked to this knowledge by
    # `auto_link_knowledge`. If none qualify, skip — an event without
    # entities adds graph clutter for nothing.
    try:
        entity_node_ids = _entities_linked_to(db, knowledge_id)
    except Exception as exc:
        LOG(f"entity lookup failed for k={knowledge_id}: {exc}")
        return None
    if not entity_node_ids:
        return None

    cap = _max_entities_per_event()
    entity_node_ids = entity_node_ids[:cap]

    now = _now_iso()
    event_node_id = _new_id()
    event_name = f"save:{knowledge_id}:{now}"

    properties = {
        "project": project,
        "session_id": session_id,
        "knowledge_id": knowledge_id,
        "kind": "save_event",
    }

    try:
        db.execute(
            """INSERT INTO graph_nodes (
                id, type, name, content, properties, source,
                importance, first_seen_at, last_seen_at, mention_count, status
            ) VALUES (?, ?, ?, NULL, ?, 'auto', 0.5, ?, ?, 1, 'active')""",
            (
                event_node_id, EVENT_NODE_TYPE, event_name,
                json.dumps(properties), now, now,
            ),
        )
        # MENTIONED_IN edges: entity → event
        edges_added = 0
        for ent_id in entity_node_ids:
            try:
                db.execute(
                    """INSERT OR IGNORE INTO graph_edges (
                        id, source_id, target_id, relation_type, weight,
                        context, created_at, last_reinforced_at, reinforcement_count
                    ) VALUES (?, ?, ?, ?, 1.0, ?, ?, ?, 0)""",
                    (
                        _new_id(), ent_id, event_node_id, MENTIONED_IN,
                        f"k={knowledge_id}", now, now,
                    ),
                )
                edges_added += 1
            except Exception as exc:
                LOG(f"edge insert failed for {ent_id}→{event_node_id}: {exc}")

        # Event ↔ Knowledge via knowledge_nodes (role='represents').
        # The same row also gives the dashboard one-shot lookup for
        # "what knowledge does this event represent?".
        try:
            db.execute(
                """INSERT OR IGNORE INTO knowledge_nodes (
                    knowledge_id, node_id, role, strength
                ) VALUES (?, ?, 'represents', 1.0)""",
                (knowledge_id, event_node_id),
            )
        except Exception as exc:
            LOG(f"knowledge_nodes 'represents' link failed: {exc}")

        db.commit()
        return EventRecord(
            node_id=event_node_id, name=event_name,
            knowledge_id=knowledge_id, entity_count=edges_added,
        )
    except Exception as exc:
        LOG(f"record_save_event failed for k={knowledge_id}: {exc}")
        return None


def _entities_linked_to(db, knowledge_id: int) -> list[str]:
    """Return graph_node IDs of entity-typed nodes linked to this knowledge."""
    placeholders = ",".join("?" * len(_ENTITY_TYPES))
    rows = db.execute(
        f"""SELECT n.id FROM knowledge_nodes kn
              JOIN graph_nodes n ON n.id = kn.node_id
             WHERE kn.knowledge_id = ?
               AND n.status = 'active'
               AND n.type IN ({placeholders})""",
        (knowledge_id, *_ENTITY_TYPES),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        nid = r[0] if not hasattr(r, "keys") else r["id"]
        if nid:
            out.append(nid)
    return out


# ──────────────────────────────────────────────
# Read helpers — used by smart router + wiki
# ──────────────────────────────────────────────


@dataclass
class EventHit:
    event_node_id: str
    event_name: str
    knowledge_id: int | None
    project: str | None
    timestamp: str | None


def _row_to_event(row) -> EventHit:
    if hasattr(row, "keys"):
        node_id = row["id"]
        name = row["name"]
        props_raw = row["properties"]
        last_seen = row["last_seen_at"]
    else:
        node_id, name, props_raw, last_seen = row[0], row[1], row[2], row[3]
    props = {}
    try:
        props = json.loads(props_raw) if props_raw else {}
    except Exception:
        props = {}
    return EventHit(
        event_node_id=node_id, event_name=name,
        knowledge_id=props.get("knowledge_id"),
        project=props.get("project"),
        timestamp=last_seen,
    )


def find_events_for_entity(
    db,
    *,
    entity_name: str,
    project: str | None = None,
    limit: int = 20,
    scope: SearchScope | None = None,
) -> list[EventHit]:
    """Return Events that mention an entity, newest-first."""
    if not entity_name:
        return []
    return _find_events_for_entities(db, [entity_name], project=project, limit=limit, scope=scope)


def _find_events_for_entities(
    db, entity_names: list[str], *, project: str | None, limit: int,
    scope: SearchScope | None,
) -> list[EventHit]:
    sql = (
        """SELECT n.id, n.name, n.properties, n.last_seen_at
              FROM graph_nodes n
             WHERE n.type = ? AND n.status = 'active'"""
    )
    params: list[str | int] = [EVENT_NODE_TYPE]
    for name in dict.fromkeys(name.strip().lower() for name in entity_names):
        sql += """ AND EXISTS (SELECT 1 FROM graph_edges e
            JOIN graph_nodes src ON src.id=e.source_id
            WHERE e.target_id=n.id AND e.relation_type=? AND lower(src.name)=?)"""
        params.extend([MENTIONED_IN, name])
    if project:
        # Project filter applies via the JSON properties — cheap because
        # we already filtered down to event-typed nodes.
        sql += " AND json_extract(n.properties, '$.project') = ?"
        params.append(project)
    if scope is not None:
        predicates, scope_params = scope.sql()
        sql += " AND EXISTS (SELECT 1 FROM knowledge k WHERE k.id=CAST(json_extract(n.properties, '$.knowledge_id') AS BIGINT) AND " + " AND ".join(predicates) + ")"
        params.extend(scope_params)
    sql += " ORDER BY n.last_seen_at DESC LIMIT ?"
    params.append(limit)
    try:
        rows = db.execute(sql, params).fetchall()
    except Exception as exc:
        LOG(f"find_events_for_entity query failed: {exc}")
        return []
    return [_row_to_event(r) for r in rows]


def find_co_mentioned_events(
    db,
    *,
    entity_a: str,
    entity_b: str,
    project: str | None = None,
    limit: int = 20,
    scope: SearchScope | None = None,
) -> list[EventHit]:
    """Events where BOTH `entity_a` and `entity_b` are mentioned."""
    if not entity_a or not entity_b:
        return []
    return _find_events_for_entities(db, [entity_a, entity_b], project=project, limit=limit, scope=scope)


def get_event_for_knowledge(db, knowledge_id: int) -> EventHit | None:
    """Inverse direction: which Event node represents this knowledge?"""
    try:
        rows = db.execute(
            """SELECT n.id, n.name, n.properties, n.last_seen_at
                 FROM knowledge_nodes kn
                 JOIN graph_nodes n ON n.id = kn.node_id
                WHERE kn.knowledge_id = ?
                  AND kn.role = 'represents'
                  AND n.type = ?
                  AND n.status = 'active'
                LIMIT 1""",
            (knowledge_id, EVENT_NODE_TYPE),
        ).fetchall()
    except Exception as exc:
        LOG(f"get_event_for_knowledge query failed: {exc}")
        return None
    if not rows:
        return None
    return _row_to_event(rows[0])
