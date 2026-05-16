"""Backfill v6.0 queues with existing (pre-v6) active knowledge records.

Run once after upgrading from v5.0:
    ~/claude-memory-server/.venv/bin/python src/tools/backfill_v6.py

Enqueues every active knowledge record into all three v6 queues so the next
reflection cycle will generate triples, enrichment, and multi-representations
for them. Idempotent — safe to re-run.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
sys.path.insert(0, str(SRC))

from paths import memory_dir as _resolve_memory_dir


def backfill(db: sqlite3.Connection, project: str | None = None) -> dict[str, int]:
    """Enqueue every active knowledge record into all v6 queues."""
    from triple_extraction_queue import TripleExtractionQueue
    from deep_enrichment_queue import DeepEnrichmentQueue
    from representations_queue import RepresentationsQueue

    tq = TripleExtractionQueue(db)
    dq = DeepEnrichmentQueue(db)
    rq = RepresentationsQueue(db)

    if project:
        rows = db.execute(
            "SELECT id FROM knowledge WHERE status='active' AND project=? ORDER BY id",
            (project,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id FROM knowledge WHERE status='active' ORDER BY id"
        ).fetchall()

    enqueued = {"triples": 0, "enrichment": 0, "representations": 0}
    for r in rows:
        kid = r[0]
        if tq.enqueue(kid):
            enqueued["triples"] += 1
        if dq.enqueue(kid):
            enqueued["enrichment"] += 1
        if rq.enqueue(kid):
            enqueued["representations"] += 1
    return {"scanned": len(rows), **enqueued}


def main() -> None:
    memory_dir = _resolve_memory_dir()
    db_path = memory_dir / "memory.db"
    if not db_path.exists():
        sys.exit(f"memory.db not found at {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    project = None
    if len(sys.argv) > 1:
        project = sys.argv[1]
        print(f"backfilling project: {project}")
    else:
        print("backfilling all projects")

    result = backfill(conn, project)
    print(f"scanned: {result['scanned']} active records")
    print(f"new enqueued:")
    print(f"  triple_extraction_queue:  +{result['triples']}")
    print(f"  deep_enrichment_queue:    +{result['enrichment']}")
    print(f"  representations_queue:    +{result['representations']}")
    print("\nRun reflection to drain them (or wait for the next scheduled cycle).")
    conn.close()


if __name__ == "__main__":
    main()
