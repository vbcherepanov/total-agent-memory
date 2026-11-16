from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
import sqlite3

from memory_core.telemetry import counters, op_timer


@dataclass(frozen=True)
class Snapshot:
    instant: str

    @classmethod
    def at(cls, value: datetime | str) -> Snapshot:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        instant = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return cls(instant)

    def predicate(self) -> tuple[str, list[str]]:
        return ("(SELECT e.state FROM knowledge_state_events e WHERE e.knowledge_id=k.id "
                "AND julianday(e.occurred_at)<=julianday(?) "
                "ORDER BY julianday(e.occurred_at) DESC,e.id DESC LIMIT 1)='active'",
                [self.instant])

    def allows(self, db: sqlite3.Connection, knowledge_id: int) -> bool:
        with op_timer("retrieval_snapshot_visibility_ms"):
            predicate, params = self.predicate()
            row = db.execute(f"SELECT k.id FROM knowledge k WHERE k.id=? AND {predicate}",
                             [knowledge_id, *params]).fetchone()
            counters.bump("retrieval_snapshot_visibility_checks")
            return row is not None


@dataclass(frozen=True)
class SnapshotQuery:
    query: str
    snapshot: Snapshot
    project: str | None
    limit: int

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> SnapshotQuery:
        query = arguments.get("query")
        instant = arguments.get("as_of")
        project = arguments.get("project")
        limit = arguments.get("limit", 10)
        if not isinstance(query, str) or not query.strip():
            raise ValueError("snapshot requires a non-empty query")
        if not isinstance(instant, str) or not instant.strip():
            raise ValueError("snapshot requires an ISO as_of timestamp")
        if project is not None and not isinstance(project, str):
            raise ValueError("snapshot project must be a string")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("snapshot limit must be a positive integer")
        return cls(query.strip(), Snapshot.at(instant.strip()), project, limit)
