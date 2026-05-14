"""Tests for tools.merge_duplicate_nodes — the one-shot cleanup tool
that collapses graph_nodes duplicates accumulated before migration 026."""

import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from tools import merge_duplicate_nodes as mdn  # noqa: E402


@pytest.fixture
def seeded_db(db):
    """Populate graph_nodes with the duplicate patterns observed in
    production: case variants + type collisions + orphans."""
    # Case variants: three "Vue" rows, all technology.
    rows = [
        ("v_lower",  "technology", "vue",  "vue",  10, "2026-01-01T00:00:00Z"),
        ("v_proper", "technology", "Vue",  "vue",  3,  "2026-01-02T00:00:00Z"),
        ("v_upper",  "technology", "VUE",  "vue",  1,  "2026-01-03T00:00:00Z"),
    ]
    # Type collision: "python" exists as both technology and concept.
    rows += [
        ("py_tech",    "technology", "python", "python", 8, "2026-01-04T00:00:00Z"),
        ("py_concept", "concept",    "Python", "python", 5, "2026-01-05T00:00:00Z"),
    ]
    # Clean rows that must remain untouched.
    rows += [
        ("clean_a", "concept", "billing", "billing", 4, "2026-01-06T00:00:00Z"),
        ("clean_b", "concept", "auth",    "auth",    7, "2026-01-07T00:00:00Z"),
    ]
    for nid, type_, name, name_norm, mc, fs in rows:
        db.execute(
            """INSERT INTO graph_nodes
                  (id, type, name, name_norm, mention_count,
                   first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (nid, type_, name, name_norm, mc, fs, fs),
        )

    # Seed a few edges:
    #   - v_lower -[uses]-> clean_a   (kept, repointed → v_lower itself wins)
    #   - v_upper -[uses]-> clean_a   (duplicate after merge → must dedupe)
    #   - clean_b -[uses]-> v_proper  (incoming on a loser → repoint)
    #   - py_concept -[part_of]-> py_tech (becomes self-loop after collision merge → drop)
    edges = [
        ("e1", "v_lower",    "clean_a",    "uses",    0.5, 1),
        ("e2", "v_upper",    "clean_a",    "uses",    0.3, 1),
        ("e3", "clean_b",    "v_proper",   "uses",    0.4, 1),
        ("e4", "py_concept", "py_tech",    "part_of", 0.6, 1),
    ]
    for eid, src, dst, rel, w, rc in edges:
        db.execute(
            """INSERT INTO graph_edges
                  (id, source_id, target_id, relation_type,
                   weight, reinforcement_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (eid, src, dst, rel, w, rc, "2026-01-10T00:00:00Z"),
        )

    # Knowledge link on a loser — must move to winner without collision.
    db.execute(
        "INSERT INTO knowledge (id, type, content, created_at) "
        "VALUES (1, 'fact', 'about vue', '2026-01-10')"
    )
    db.execute(
        """INSERT INTO knowledge_nodes (knowledge_id, node_id, role, strength)
           VALUES (1, ?, 'mentions', 1.0)""",
        ("v_proper",),
    )
    db.commit()
    return db


class TestDetectGroups:
    def test_case_groups_detected(self, seeded_db):
        groups = mdn.detect_case_groups(seeded_db)
        assert len(groups) == 1
        g = groups[0]
        assert g.name_norm == "vue"
        assert g.winner == "v_lower"          # highest mention_count
        assert sorted(g.losers) == ["v_proper", "v_upper"]

    def test_type_collision_groups_detected(self, seeded_db):
        # Pretend the case phase already touched the Vue trio.
        processed = {"v_lower", "v_proper", "v_upper"}
        groups = mdn.detect_type_collision_groups(seeded_db, processed)
        assert len(groups) == 1
        g = groups[0]
        assert g.name_norm == "python"
        assert g.winner == "py_tech"          # higher mention_count
        assert g.losers == ["py_concept"]

    def test_clean_rows_not_grouped(self, seeded_db):
        case = mdn.detect_case_groups(seeded_db)
        coll = mdn.detect_type_collision_groups(seeded_db, set())
        all_touched = set()
        for g in case + coll:
            all_touched.add(g.winner)
            all_touched.update(g.losers)
        assert "clean_a" not in all_touched
        assert "clean_b" not in all_touched


class TestMergeOps:
    def test_repoint_edges_relocate_and_dedupe(self, seeded_db):
        # Repoint v_upper → v_lower. Edge e2 (v_upper→clean_a, uses) will
        # collide with e1 (v_lower→clean_a, uses) and must dedupe.
        rel, ded, sl = mdn._repoint_edges(seeded_db, "v_upper", "v_lower")
        assert rel == 0
        assert ded == 1
        assert sl == 0
        remaining = seeded_db.execute(
            "SELECT id FROM graph_edges WHERE source_id='v_upper'"
        ).fetchall()
        assert remaining == []
        # e1 must have absorbed reinforcement
        e1 = seeded_db.execute(
            "SELECT reinforcement_count, weight FROM graph_edges WHERE id='e1'"
        ).fetchone()
        assert e1["reinforcement_count"] >= 2

    def test_repoint_edges_collision_makes_self_loop(self, seeded_db):
        # py_concept -[part_of]-> py_tech : after we repoint py_concept→py_tech,
        # the edge becomes py_tech→py_tech (self-loop) — must be dropped.
        rel, ded, sl = mdn._repoint_edges(seeded_db, "py_concept", "py_tech")
        assert sl == 1
        survivors = seeded_db.execute(
            "SELECT id FROM graph_edges WHERE source_id='py_concept' "
            "OR target_id='py_concept'"
        ).fetchall()
        assert survivors == []

    def test_repoint_knowledge_links(self, seeded_db):
        rel, ded = mdn._repoint_knowledge_links(seeded_db, "v_proper", "v_lower")
        assert rel == 1
        assert ded == 0
        link = seeded_db.execute(
            "SELECT node_id FROM knowledge_nodes WHERE knowledge_id=1"
        ).fetchone()
        assert link["node_id"] == "v_lower"


class TestMergeGroupEndToEnd:
    def test_merge_case_group_collapses_to_winner(self, seeded_db):
        groups = mdn.detect_case_groups(seeded_db)
        mdn.merge_group(seeded_db, groups[0])
        seeded_db.commit()

        remaining = seeded_db.execute(
            "SELECT id FROM graph_nodes WHERE name_norm='vue'"
        ).fetchall()
        assert len(remaining) == 1
        assert remaining[0]["id"] == "v_lower"

        # Mention count absorbed: 10 (winner baseline) + 3 + 1 = 14
        w = seeded_db.execute(
            "SELECT mention_count FROM graph_nodes WHERE id='v_lower'"
        ).fetchone()
        assert w["mention_count"] == 14

    def test_merge_type_collision_drops_loser(self, seeded_db):
        groups = mdn.detect_type_collision_groups(seeded_db, set())
        mdn.merge_group(seeded_db, groups[0])
        seeded_db.commit()
        remaining = seeded_db.execute(
            "SELECT type FROM graph_nodes WHERE name_norm='python'"
        ).fetchall()
        assert len(remaining) == 1
        # Winner type preserved (technology); loser's "concept" classification gone.
        assert remaining[0]["type"] == "technology"


class TestInstallUniqueIndex:
    def test_refuses_when_duplicates_remain(self, seeded_db):
        ok = mdn.install_unique_index(seeded_db)
        assert ok is False

    def test_installs_when_clean(self, seeded_db):
        for g in mdn.detect_case_groups(seeded_db):
            mdn.merge_group(seeded_db, g)
        for g in mdn.detect_type_collision_groups(seeded_db, set()):
            mdn.merge_group(seeded_db, g)
        seeded_db.commit()
        assert mdn.install_unique_index(seeded_db) is True
        # And UNIQUE prevents future duplication
        with pytest.raises(sqlite3.IntegrityError):
            seeded_db.execute(
                "INSERT INTO graph_nodes (id, type, name, name_norm) "
                "VALUES (?, ?, ?, ?)",
                ("dup", "concept", "billing", "billing"),
            )


class TestCli:
    def test_dry_run_does_not_mutate(self, seeded_db, monkeypatch, tmp_path):
        # Reroute the tool to our seeded DB by copying it onto disk
        # — the CLI opens a fresh sqlite3 connection to the path.
        target = tmp_path / "memory.db"
        seeded_db.commit()
        # Materialize the in-memory DB to disk via backup.
        ondisk = sqlite3.connect(str(target))
        seeded_db.backup(ondisk)
        ondisk.close()

        before_nodes = sqlite3.connect(str(target)).execute(
            "SELECT COUNT(*) FROM graph_nodes"
        ).fetchone()[0]

        rc = mdn.main(["--dry-run", f"--db={target}"])
        assert rc == 0

        after_nodes = sqlite3.connect(str(target)).execute(
            "SELECT COUNT(*) FROM graph_nodes"
        ).fetchone()[0]
        assert before_nodes == after_nodes

    def test_apply_collapses(self, seeded_db, tmp_path):
        target = tmp_path / "memory.db"
        seeded_db.commit()
        ondisk = sqlite3.connect(str(target))
        seeded_db.backup(ondisk)
        ondisk.close()

        rc = mdn.main(["--apply", f"--db={target}"])
        assert rc == 0

        conn = sqlite3.connect(str(target))
        conn.row_factory = sqlite3.Row
        # Vue trio (3 nodes) → 1 winner ("vue" technology).
        # Python pair (2 nodes) merges, the only edge becomes a self-loop
        # and is dropped, then the remaining node is left without any
        # edges or knowledge links → removed by the orphan sweep.
        # clean_a / clean_b stay. Net: 3 nodes (vue, clean_a, clean_b).
        n = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
        assert n == 3
        survivors = {
            r["name_norm"] for r in conn.execute(
                "SELECT name_norm FROM graph_nodes"
            ).fetchall()
        }
        assert survivors == {"vue", "billing", "auth"}
        conn.close()

    def test_apply_skip_orphans_preserves_disconnected(
        self, seeded_db, tmp_path
    ):
        target = tmp_path / "memory.db"
        seeded_db.commit()
        ondisk = sqlite3.connect(str(target))
        seeded_db.backup(ondisk)
        ondisk.close()

        rc = mdn.main(["--apply", "--skip-orphans", f"--db={target}"])
        assert rc == 0

        conn = sqlite3.connect(str(target))
        n = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
        # No orphan sweep → 4 nodes remain (vue, python_winner, clean_a, clean_b).
        assert n == 4
        conn.close()
