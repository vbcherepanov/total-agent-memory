#!/usr/bin/env python3
"""Migrate existing knowledge records to the knowledge graph.

Reads all active knowledge records from the `knowledge` table that are
NOT yet linked in `knowledge_nodes`, extracts concepts using the fast
local extractor (or deep via Ollama), links them to graph nodes, and
creates co-occurrence edges between concepts that appear together.

Usage:
    python src/migrate_knowledge_to_graph.py [--db PATH] [--batch SIZE] [--deep] [--dry-run]

Options:
    --db PATH      Database path (default: <memory-dir>/memory.db)
    --batch SIZE   Process N records at a time (default: 50)
    --deep         Use Ollama for deep extraction (slow but better, default: fast local)
    --dry-run      Don't write to DB, just show what would happen
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

# Allow imports from the src directory
sys.path.insert(0, str(Path(__file__).parent))

from paths import memory_dir

from graph.store import GraphStore
from ingestion.extractor import ConceptExtractor


def migrate(
    db_path: str,
    batch_size: int = 50,
    deep: bool = False,
    dry_run: bool = False,
) -> None:
    """Run the migration: link all unlinked knowledge records to the graph.

    Args:
        db_path: Path to the SQLite database.
        batch_size: Number of records to process per batch.
        deep: Use Ollama deep extraction instead of fast local matching.
        dry_run: If True, don't write anything to the database.
    """
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    gs = GraphStore(db)
    extractor = ConceptExtractor(db)

    # Count unlinked active records
    total = db.execute(
        """SELECT COUNT(*) FROM knowledge k
           WHERE k.status = 'active'
           AND k.id NOT IN (SELECT knowledge_id FROM knowledge_nodes)"""
    ).fetchone()[0]

    print(f"Records to migrate: {total}")
    if total == 0:
        print("Nothing to do.")
        db.close()
        return

    mode = "deep (Ollama)" if deep else "fast (local)"
    print(f"Extraction mode: {mode}")
    if dry_run:
        print("DRY RUN: no changes will be written")
    print()

    processed = 0
    linked = 0
    skipped = 0
    errors = 0
    t_start = time.monotonic()

    # Track processed IDs for dry-run mode (where we don't write to DB)
    processed_ids: set[int] = set()

    while True:
        if dry_run and processed_ids:
            placeholders = ",".join(str(i) for i in processed_ids)
            rows = db.execute(
                f"""SELECT k.* FROM knowledge k
                    WHERE k.status = 'active'
                    AND k.id NOT IN (SELECT knowledge_id FROM knowledge_nodes)
                    AND k.id NOT IN ({placeholders})
                    LIMIT ?""",
                (batch_size,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT k.* FROM knowledge k
                   WHERE k.status = 'active'
                   AND k.id NOT IN (SELECT knowledge_id FROM knowledge_nodes)
                   LIMIT ?""",
                (batch_size,),
            ).fetchall()

        if not rows:
            break

        for row in rows:
            kid = row["id"]
            processed_ids.add(kid)
            content = row["content"] or ""
            context = row["context"] or ""
            project = row["project"] or "general"
            tags_str = row["tags"] or "[]"
            ktype = row["type"] or "fact"

            # Skip records with empty content
            if not content.strip():
                skipped += 1
                processed += 1
                # Insert a dummy link so we don't re-process
                if not dry_run:
                    _link_fallback(db, gs, kid, project, ktype, [])
                continue

            try:
                full_text = f"{content}\n{context}" if context.strip() else content
                links_before = linked

                # Extract concepts
                if deep:
                    result = extractor.extract_deep(full_text)
                else:
                    result = extractor.extract_fast(full_text)

                concepts = result.get("concepts", [])
                entities = result.get("entities", [])

                # Parse tags safely
                try:
                    tags = json.loads(tags_str) if isinstance(tags_str, str) else []
                except (json.JSONDecodeError, TypeError):
                    tags = []
                if not isinstance(tags, list):
                    tags = []

                if not dry_run:
                    # Link extracted concepts
                    for concept in concepts:
                        name = concept.get("name", "") if isinstance(concept, dict) else str(concept)
                        if not name or len(name) < 2:
                            continue
                        strength = float(concept.get("strength", 0.8)) if isinstance(concept, dict) else 0.8
                        node_id = concept.get("id") if isinstance(concept, dict) else None
                        if not node_id:
                            node_id = gs.get_or_create(name.lower(), "concept")
                        gs.link_knowledge(kid, node_id, role="provides", strength=strength)
                        linked += 1

                    # Link extracted entities
                    for entity in entities:
                        name = entity.get("name", "") if isinstance(entity, dict) else str(entity)
                        etype = entity.get("type", "concept") if isinstance(entity, dict) else "concept"
                        if not name or len(name) < 2:
                            continue
                        node_id = entity.get("id") if isinstance(entity, dict) else None
                        if not node_id:
                            node_id = gs.get_or_create(name.lower(), etype)
                        gs.link_knowledge(kid, node_id, role="mentions")
                        linked += 1

                    # If no concepts/entities extracted, use fallback
                    if not concepts and not entities:
                        fallback_links = _link_fallback(db, gs, kid, project, ktype, tags)
                        linked += fallback_links
                    else:
                        # Still link project and tags even when we have extracted concepts
                        if project and project != "general":
                            proj_id = gs.get_or_create(project, "project")
                            gs.link_knowledge(kid, proj_id, role="belongs_to")
                            linked += 1
                        for tag in tags:
                            if isinstance(tag, str) and len(tag) > 2:
                                tag_clean = tag.strip().lower().replace("-", "_")
                                tag_id = gs.get_or_create(tag_clean, "concept")
                                gs.link_knowledge(kid, tag_id, role="tagged")
                                linked += 1
                else:
                    # Dry run: count what would happen
                    n_links = len(concepts) + len(entities)
                    if not n_links:
                        n_links = 1  # at least project/type fallback
                    if project and project != "general":
                        n_links += 1
                    n_links += sum(1 for t in tags if isinstance(t, str) and len(t) > 2)
                    linked += n_links

                processed += 1

            except Exception as e:
                errors += 1
                processed += 1
                print(f"  ERROR on id={kid}: {e}")

        # Commit after each batch
        if not dry_run:
            db.commit()

        elapsed = time.monotonic() - t_start
        rate = processed / elapsed if elapsed > 0 else 0
        print(
            f"  Batch: {processed}/{total} processed, "
            f"{linked} links, {errors} errors, {skipped} skipped "
            f"({rate:.1f} rec/s)"
        )

    # Create co-occurrence edges
    cooccurrence_count = 0
    if not dry_run:
        print("\nCreating co-occurrence edges...")
        cooccurrence_count = _create_cooccurrences(db, gs)
        print(f"  Created {cooccurrence_count} co-occurrence edges")

    db.close()

    elapsed = time.monotonic() - t_start
    print(f"\nMigration {'(DRY RUN) ' if dry_run else ''}complete in {elapsed:.1f}s:")
    print(f"  Records processed: {processed}")
    print(f"  Records skipped (empty): {skipped}")
    print(f"  Links created: {linked}")
    print(f"  Co-occurrence edges: {cooccurrence_count}")
    print(f"  Errors: {errors}")


def _link_fallback(
    db: sqlite3.Connection,
    gs: GraphStore,
    kid: int,
    project: str,
    ktype: str,
    tags: list,
) -> int:
    """Fallback linking: use project, type, and tags when no concepts extracted.

    Returns number of links created.
    """
    count = 0

    # Link to project node
    if project and project != "general":
        proj_id = gs.get_or_create(project, "project")
        gs.link_knowledge(kid, proj_id, role="belongs_to")
        count += 1

    # Link to type node
    type_id = gs.get_or_create(ktype, "concept")
    gs.link_knowledge(kid, type_id, role="is_type")
    count += 1

    # Link to tag nodes
    for tag in tags:
        if isinstance(tag, str) and len(tag) > 2:
            tag_clean = tag.strip().lower().replace("-", "_")
            tag_id = gs.get_or_create(tag_clean, "concept")
            gs.link_knowledge(kid, tag_id, role="tagged")
            count += 1

    return count


def _create_cooccurrences(
    db: sqlite3.Connection,
    gs: GraphStore,
    min_count: int = 2,
) -> int:
    """Create 'mentioned_with' edges between concepts that co-occur in knowledge records.

    Only creates edges for pairs appearing together in at least min_count records.
    Uses GraphStore.add_edge which handles dedup (reinforces existing edges).
    """
    pairs = db.execute(
        """SELECT kn1.node_id AS n1, kn2.node_id AS n2, COUNT(*) AS cnt
           FROM knowledge_nodes kn1
           JOIN knowledge_nodes kn2 ON kn1.knowledge_id = kn2.knowledge_id
           WHERE kn1.node_id < kn2.node_id
           GROUP BY kn1.node_id, kn2.node_id
           HAVING cnt >= ?""",
        (min_count,),
    ).fetchall()

    count = 0
    for row in pairs:
        try:
            gs.add_edge(
                row["n1"],
                row["n2"],
                "mentioned_with",
                weight=min(row["cnt"] * 0.2, 3.0),
                context=f"co-occurred in {row['cnt']} records",
            )
            count += 1
        except (ValueError, Exception) as e:
            # Skip invalid edges (self-loops, missing nodes)
            pass

    db.commit()
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate existing knowledge records to the knowledge graph"
    )
    parser.add_argument(
        "--db",
        default=str(memory_dir() / "memory.db"),
        help="Path to SQLite database (default: <memory-dir>/memory.db)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=50,
        help="Batch size for processing (default: 50)",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Use Ollama deep extraction (slower but better)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write to DB, just show stats",
    )
    args = parser.parse_args()

    migrate(args.db, args.batch, args.deep, args.dry_run)
