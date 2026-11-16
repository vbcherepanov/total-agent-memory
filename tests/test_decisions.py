"""Unit tests for structured decision schema (v8.0)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# Ensure src/ is importable (belt + suspenders with conftest.py).
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from decisions import (  # noqa: E402
    Decision,
    DecisionOption,
    SCHEMA_VERSION,
    STRUCTURED_TAG,
    parse_stored_decision,
    save_decision,
)


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────


def _sample_options() -> list[DecisionOption]:
    return [
        DecisionOption(
            name="FastAPI",
            pros=["async", "pydantic"],
            cons=["fewer batteries"],
            unknowns=["HTTP2"],
        ),
        DecisionOption(
            name="Django",
            pros=["admin", "ORM"],
            cons=["sync"],
        ),
    ]


def _sample_decision(**overrides) -> Decision:
    base = dict(
        title="Pick web framework",
        options=_sample_options(),
        criteria_matrix={
            "performance": {"FastAPI": 5.0, "Django": 3.0},
            "ecosystem": {"FastAPI": 3.0, "Django": 5.0},
            "type-safety": {"FastAPI": 5.0, "Django": 3.0},
        },
        selected="FastAPI",
        rationale="Async I/O and type-safety outweigh ecosystem gap.",
        discarded=["Django"],
        project="demo",
        tags=["web", "python"],
    )
    base.update(overrides)
    return Decision(**base)


# ─────────────────────────────────────────────────────────────
# Dataclass validation
# ─────────────────────────────────────────────────────────────


def test_decision_dataclass_validation_selected_in_options():
    with pytest.raises(ValueError, match="selected"):
        _sample_decision(selected="Flask")


def test_decision_dataclass_rejects_discarded_overlapping_selected():
    with pytest.raises(ValueError, match="discarded"):
        _sample_decision(selected="FastAPI", discarded=["FastAPI"])


def test_decision_dataclass_rejects_unknown_discarded():
    with pytest.raises(ValueError, match="unknown"):
        _sample_decision(discarded=["Flask"])


def test_decision_dataclass_rejects_empty_options():
    with pytest.raises(ValueError, match="options"):
        _sample_decision(options=[])


def test_decision_dataclass_rejects_duplicate_option_names():
    dup_options = [
        DecisionOption(name="X", pros=[], cons=[]),
        DecisionOption(name="X", pros=[], cons=[]),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        Decision(
            title="t",
            options=dup_options,
            criteria_matrix={},
            selected="X",
            rationale="why",
        )


def test_decision_dataclass_rejects_rating_out_of_range():
    with pytest.raises(ValueError, match=r"outside 0\.\.5"):
        _sample_decision(criteria_matrix={"perf": {"FastAPI": 7.0, "Django": 2.0}})


def test_decision_dataclass_rejects_rating_for_unknown_option():
    with pytest.raises(ValueError, match="unknown option"):
        _sample_decision(criteria_matrix={"perf": {"Mystery": 4.0}})


def test_decision_dataclass_rejects_non_numeric_rating():
    with pytest.raises(ValueError, match="numeric"):
        _sample_decision(criteria_matrix={"perf": {"FastAPI": "great"}})


def test_decision_dataclass_rejects_empty_rationale():
    with pytest.raises(ValueError, match="rationale"):
        _sample_decision(rationale="   ")


# ─────────────────────────────────────────────────────────────
# Serialization
# ─────────────────────────────────────────────────────────────


def test_decision_serializes_to_content_template():
    d = _sample_decision()
    content = d.to_content()

    assert content.startswith("DECISION: Pick web framework")
    assert "SELECTED: FastAPI" in content
    assert "OPTIONS CONSIDERED:" in content
    assert "- FastAPI: pros=async, pydantic; cons=fewer batteries" in content
    assert "- Django: pros=admin, ORM; cons=sync" in content
    assert "unknowns: HTTP2" in content  # FastAPI unknowns line
    assert "RATIONALE: Async I/O and type-safety outweigh ecosystem gap." in content
    assert "DISCARDED: Django" in content


def test_decision_content_omits_discarded_section_when_empty():
    d = _sample_decision(discarded=[])
    content = d.to_content()
    assert "DISCARDED:" not in content


def test_decision_serializes_to_json_context():
    d = _sample_decision()
    ctx = d.to_context_json()
    payload = json.loads(ctx)

    assert payload["schema"] == SCHEMA_VERSION
    assert payload["title"] == "Pick web framework"
    assert payload["selected"] == "FastAPI"
    assert payload["rationale"].startswith("Async")
    assert payload["discarded"] == ["Django"]
    assert len(payload["options"]) == 2
    fastapi = next(o for o in payload["options"] if o["name"] == "FastAPI")
    assert fastapi["pros"] == ["async", "pydantic"]
    assert fastapi["unknowns"] == ["HTTP2"]
    assert payload["criteria_matrix"]["performance"]["FastAPI"] == 5.0


def test_parse_stored_decision_roundtrip():
    d = _sample_decision()
    parsed = parse_stored_decision(d.to_context_json())
    assert parsed is not None
    assert parsed["selected"] == "FastAPI"
    assert parsed["schema"] == SCHEMA_VERSION


def test_parse_stored_decision_rejects_non_schema_blobs():
    assert parse_stored_decision("") is None
    assert parse_stored_decision("not-json") is None
    assert parse_stored_decision(json.dumps({"foo": "bar"})) is None
    assert parse_stored_decision(json.dumps({"schema": "other/v1"})) is None


def test_criterion_repr_content_lists_selected_rating_per_criterion():
    d = _sample_decision()
    text = d.criterion_repr_content()
    # One line per criterion, selected option rating rendered.
    lines = text.splitlines()
    assert len(lines) == 3
    assert "performance: 5" in text
    assert "ecosystem: 3" in text
    assert "type-safety: 5" in text


def test_summary_repr_content_includes_selected_and_rationale():
    d = _sample_decision()
    s = d.summary_repr_content()
    assert s.startswith("Selected FastAPI.")
    assert "Async I/O" in s


# ─────────────────────────────────────────────────────────────
# Store integration (real Store, temp MEMORY_DIR, no LLM required)
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def store(monkeypatch, tmp_path):
    # Force LLM disabled — embed() returns None, multi-repr views are skipped
    # gracefully and the knowledge row still lands in SQLite.
    monkeypatch.setenv("MEMORY_LLM_ENABLED", "false")

    (tmp_path / "blobs").mkdir(exist_ok=True)
    (tmp_path / "chroma").mkdir(exist_ok=True)

    import server, config
    config._cache_clear()
    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)

    s = server.Store()
    s.db.execute(
        "INSERT INTO sessions (id, started_at, project, status) "
        "VALUES ('sess-decisions', '2026-04-19T00:00:00Z', 'demo', 'open')"
    )
    s.db.commit()
    yield s
    try:
        s.db.close()
    except Exception:
        pass


def test_save_decision_inserts_knowledge_row(store):
    d = _sample_decision()
    rid = save_decision(store, d, session_id="sess-decisions")
    assert isinstance(rid, int) and rid > 0

    row = store.db.execute(
        "SELECT id, type, content, context, project, tags FROM knowledge WHERE id=?",
        (rid,),
    ).fetchone()
    assert row is not None
    assert row["type"] == "decision"
    assert row["project"] == "demo"
    # BM25-searchable content format
    assert "DECISION: Pick web framework" in row["content"]
    assert "SELECTED: FastAPI" in row["content"]
    # JSON context with structured schema
    payload = json.loads(row["context"])
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["selected"] == "FastAPI"
    # Tags include "structured" + original tags
    tags = json.loads(row["tags"])
    assert STRUCTURED_TAG in tags
    assert "web" in tags and "python" in tags


def test_save_decision_enqueues_representations(store):
    d = _sample_decision()
    rid = save_decision(store, d, session_id="sess-decisions")

    row = store.db.execute(
        "SELECT status FROM representations_queue WHERE knowledge_id=?", (rid,)
    ).fetchone()
    assert row is not None
    assert row["status"] == "pending"


def test_save_decision_generates_criterion_multi_repr(store, monkeypatch):
    """When embed() returns a vector, both summary+criterion repr rows land."""
    import server as _srv
    from memory_core.embedding_identity import EmbedIdentity
    from multi_repr_search import search_with_winners

    identity = EmbedIdentity("actual-fallback", "fallback-provider", "mean", "test-version")
    monkeypatch.setattr(store, "last_embed_identity", lambda: identity)

    # Stub embed() so multi_repr upsert exercises the criterion path.
    def fake_embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setattr(_srv.Store, "embed", fake_embed, raising=True)

    d = _sample_decision()
    rid = save_decision(store, d, session_id="sess-decisions")

    rows = store.db.execute(
        "SELECT representation, content FROM knowledge_representations "
        "WHERE knowledge_id=? ORDER BY representation",
        (rid,),
    ).fetchall()
    reprs = {r["representation"]: r["content"] for r in rows}
    # Both `summary` and `criterion` rows produced.
    assert "summary" in reprs
    assert reprs["summary"].startswith("Selected FastAPI.")
    assert "criterion" in reprs
    for crit in ("performance", "ecosystem", "type-safety"):
        assert crit in reprs["criterion"]
    identities = store.db.execute(
        "SELECT embed_model, embed_identity_key FROM knowledge_representations WHERE knowledge_id=?",
        (rid,),
    ).fetchall()
    assert all(row["embed_model"] == identity.model and row["embed_identity_key"] == identity.key(4)
               for row in identities)
    hits, _ = search_with_winners(store.db, [0.1, 0.2, 0.3, 0.4], query_identity=identity)
    assert rid in [kid for kid, _ in hits]


def test_save_decision_without_embedder_still_succeeds(store, monkeypatch):
    """Offline mode: save_knowledge works, multi_repr rows are skipped."""
    import server as _srv

    monkeypatch.setattr(_srv.Store, "embed", lambda self, texts: None, raising=True)

    d = _sample_decision()
    rid = save_decision(store, d, session_id="sess-decisions")
    assert rid

    rows = store.db.execute(
        "SELECT representation FROM knowledge_representations WHERE knowledge_id=?",
        (rid,),
    ).fetchall()
    # No criterion/summary rows added manually by save_decision — they'll be
    # produced by the async repr worker when LLM/embedder is available.
    reprs = {r["representation"] for r in rows}
    assert "criterion" not in reprs
    assert "summary" not in reprs


def test_save_decision_preserves_tags_idempotent_structured(store):
    """Passing tags=['structured', 'x'] must not duplicate the tag."""
    d = _sample_decision(tags=[STRUCTURED_TAG, "architecture"])
    rid = save_decision(store, d, session_id="sess-decisions")
    row = store.db.execute(
        "SELECT tags FROM knowledge WHERE id=?", (rid,)
    ).fetchone()
    tags = json.loads(row["tags"])
    assert tags.count(STRUCTURED_TAG) == 1
    assert "architecture" in tags
