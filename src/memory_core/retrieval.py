from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict
from memory_core.incidents import IncidentFilter


class EventEvidence(TypedDict):
    key: str
    observed_at: str
    temporal_anchor_at: str
    start: str
    end: str
    precision: str
    expression: str
    source_ids: list[int]


class PassageReference(TypedDict):
    start: int
    end: int
    speaker: str


class MemoryHit(TypedDict, total=False):
    id: int
    content: str
    context: str
    source_format: str
    project: str
    type: str
    branch: str
    status: str
    score: float
    source: str
    created_at: str
    session_id: str
    source_ref: str
    anchor_id: int
    via: list[str]
    evidence_ids: list[int]
    event_start: str
    event_end: str
    event_precision: str
    events: list[EventEvidence]
    passages: list[PassageReference]
    evidence_weight: float


KNOWLEDGE_BATCH_SIZE = 400


def fetch_active_records(db: sqlite3.Connection, identities: list[int],
                         scope: SearchScope | None = None) -> dict[int, MemoryHit]:
    from memory_core.telemetry import counters, op_timer

    result: dict[int, MemoryHit] = {}
    unique = list(dict.fromkeys(identities))
    with op_timer("retrieval_hydration_ms"):
        for offset in range(0, len(unique), KNOWLEDGE_BATCH_SIZE):
            batch = unique[offset:offset + KNOWLEDGE_BATCH_SIZE]
            predicates, params = (scope or SearchScope()).sql()
            rows = db.execute(
                "SELECT k.* FROM knowledge k WHERE " + " AND ".join(predicates) + " AND k.id IN ("
                + ",".join("?" for _ in batch) + ")", [*params, *batch],
            ).fetchall()
            result.update((row["id"], dict(row)) for row in rows)
            counters.bump("retrieval_hydration_batches")
        counters.bump("retrieval_hydration_records", len(result))
    return result


def flatten_results(result: object) -> list[MemoryHit]:
    if isinstance(result, Mapping):
        result = result.get("results", [])
    if isinstance(result, Mapping):
        groups = list(result.values())
        if any(not isinstance(group, list) for group in groups):
            raise ValueError("Search result groups must be lists")
        result = [hit for group in groups for hit in group]
    if not isinstance(result, list) or any(not isinstance(hit, dict) for hit in result):
        raise ValueError("Search results must contain memory records")
    return result


@dataclass(frozen=True)
class SearchScope:
    project: str | None = None
    kind: str = "all"
    branch: str | None = None
    spaces: str | list[str] | None = None
    as_of: datetime | str | None = None
    incident: IncidentFilter | None = None

    def sql(self) -> tuple[list[str], list[str]]:
        predicates = ["k.status='active'"]
        params: list[str] = []
        if self.as_of is not None:
            from memory_core.snapshot import Snapshot
            predicate, params = Snapshot.at(self.as_of).predicate()
            predicates = [predicate]
        for column, value in (
            ("project", self.project),
            ("type", self.kind if self.kind != "all" else None),
        ):
            if value:
                predicates.append(f"k.{column}=?")
                params.append(value)
        if self.branch:
            predicates.append("(k.branch=? OR k.branch='')")
            params.append(self.branch)
        if self.spaces:
            spaces = [self.spaces] if isinstance(self.spaces, str) else self.spaces
            table = "snapshot_embedding_cache" if self.as_of is not None else "embeddings"
            predicates.append(
                f"EXISTS (SELECT 1 FROM {table} e WHERE e.knowledge_id=k.id "
                "AND COALESCE(e.embedding_space,'text') IN ("
                + ",".join("?" for _ in spaces)
                + "))"
            )
            params.extend(space.strip().lower() for space in spaces)
        if self.incident is not None:
            incident_predicates, incident_params = self.incident.sql()
            predicates.extend(incident_predicates)
            params.extend(incident_params)
        return predicates, params

    def allows(self, record: Mapping[str, object], db: sqlite3.Connection) -> bool:
        if self.incident is not None:
            predicates, params = self.sql()
            return db.execute("SELECT k.id FROM knowledge k WHERE k.id=? AND " + " AND ".join(predicates),
                              [record["id"], *params]).fetchone() is not None
        if self.as_of is not None:
            from memory_core.snapshot import Snapshot
            if not Snapshot.at(self.as_of).allows(db, int(record["id"])):
                return False
        elif record.get("status", "active") != "active":
            return False
        if self.project and record.get("project") != self.project:
            return False
        if self.kind != "all" and record.get("type") != self.kind:
            return False
        if self.branch and record.get("branch", "") not in ("", self.branch):
            return False
        if self.spaces:
            spaces = [self.spaces] if isinstance(self.spaces, str) else self.spaces
            spaces = {space.strip().lower() for space in spaces}
            table = "snapshot_embedding_cache" if self.as_of is not None else "embeddings"
            rows = db.execute(
                f"SELECT COALESCE(embedding_space, 'text') FROM {table} WHERE knowledge_id=?",
                (record["id"],),
            ).fetchall()
            return any(row[0] in spaces for row in rows)
        return True
