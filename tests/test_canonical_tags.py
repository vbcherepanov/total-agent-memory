"""Tests for the v10 canonical-topic vocabulary."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import canonical_tags as ct


@pytest.fixture(autouse=True)
def _reset_caches():
    ct.reset_vocabulary_cache()
    ct.reset_embedding_cache()
    yield
    ct.reset_vocabulary_cache()
    ct.reset_embedding_cache()


@pytest.fixture
def custom_vocab(tmp_path, monkeypatch):
    """Materialise a small vocabulary file and point the loader at it."""
    path = tmp_path / "topics.txt"
    path.write_text(
        textwrap.dedent(
            """
            # Test vocabulary
            database, db, dbal
            azure-sql, mssql, sql-server
            golang, go
            performance, perf, optimization
            """
        ).strip(), encoding="utf-8"
    )
    monkeypatch.setenv("MEMORY_TAG_VOCAB_PATH", str(path))
    return path


# ──────────────────────────────────────────────
# Vocabulary loading
# ──────────────────────────────────────────────


def test_load_default_vocabulary_is_non_empty():
    vocab = ct.load_vocabulary()
    assert vocab.topics, "shipped vocabulary must contain canonicals"
    assert "database" in vocab.canonical_set
    assert "azure-sql" in vocab.canonical_set
    assert "mcp" in vocab.canonical_set


def test_load_custom_vocabulary_parses_aliases(custom_vocab):
    vocab = ct.load_vocabulary()
    assert vocab.canonicals == ("database", "azure-sql", "golang", "performance")
    # alias maps back to canonical
    assert vocab.by_form["mssql"] == "azure-sql"
    assert vocab.by_form["dbal"] == "database"
    assert vocab.by_form["go"] == "golang"
    assert vocab.by_form["optimization"] == "performance"


def test_load_rejects_duplicate_canonicals(tmp_path, monkeypatch):
    path = tmp_path / "dup.txt"
    path.write_text("database\ndatabase\n", encoding="utf-8")
    monkeypatch.setenv("MEMORY_TAG_VOCAB_PATH", str(path))
    with pytest.raises(ValueError, match="duplicate canonical"):
        ct.load_vocabulary()


def test_load_handles_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_TAG_VOCAB_PATH", str(tmp_path / "ghost.txt"))
    vocab = ct.load_vocabulary()
    assert vocab.topics == ()
    assert vocab.canonicals == ()


# ──────────────────────────────────────────────
# resolve_tag
# ──────────────────────────────────────────────


def test_resolve_exact_canonical(custom_vocab):
    assert ct.resolve_tag("database") == "database"


def test_resolve_alias(custom_vocab):
    assert ct.resolve_tag("MsSQL") == "azure-sql"
    assert ct.resolve_tag("perf") == "performance"


def test_resolve_substring_match(custom_vocab):
    # Free-form contains a long-enough alias → routes to canonical.
    # Short aliases (len < 3) are excluded from substring matching to
    # avoid 'go' matching 'good', 'going', etc.
    assert ct.resolve_tag("dbal-fix") == "database"  # alias 'dbal' (len 4)
    assert ct.resolve_tag("optimization-plan") == "performance"


def test_resolve_levenshtein_match(custom_vocab, monkeypatch):
    monkeypatch.setenv("MEMORY_TAG_LEVENSHTEIN_THRESHOLD", "0.7")
    # Typo "performonce" → close enough to "performance".
    assert ct.resolve_tag("performonce") == "performance"


def test_resolve_returns_none_for_unknown(custom_vocab):
    assert ct.resolve_tag("totally-novel-thing-2026") is None


def test_resolve_empty_input(custom_vocab):
    assert ct.resolve_tag("") is None
    assert ct.resolve_tag("   ") is None


# ──────────────────────────────────────────────
# normalise_tags (the public hot-path API)
# ──────────────────────────────────────────────


def test_normalise_tags_replaces_aliases_with_canonical(custom_vocab):
    out = ct.normalise_tags(["MSSQL", "perf"])
    assert "azure-sql" in out
    assert "performance" in out
    # Original aliases preserved alongside (so legacy recalls keep working).
    assert "mssql" in out
    assert "perf" in out


def test_normalise_tags_keeps_unknown_as_is(custom_vocab):
    out = ct.normalise_tags(["azure-sql", "Some-Brand-New-Thing"])
    assert "azure-sql" in out
    assert "some-brand-new-thing" in out


def test_normalise_tags_dedups_and_lowercases(custom_vocab):
    out = ct.normalise_tags(["DB", "db", "Database"])
    # All three should collapse to canonical 'database' + their lowercased forms.
    # Insertion order: "db" mapped → 'database' first, then 'db' alias kept,
    # then 'database' is already in seen.
    assert out.count("database") == 1
    assert out.count("db") == 1


def test_normalise_tags_handles_none_and_garbage(custom_vocab):
    assert ct.normalise_tags(None) == []
    out = ct.normalise_tags([None, 42, "", "go"])  # mixed input
    assert out == ["golang", "go"]


def test_normalise_tags_empty_returns_empty(custom_vocab):
    assert ct.normalise_tags([]) == []


# ──────────────────────────────────────────────
# Levenshtein helper sanity
# ──────────────────────────────────────────────


def test_levenshtein_ratio_known_values():
    assert ct._levenshtein_ratio("database", "database") == 1.0
    assert ct._levenshtein_ratio("", "anything") == 0.0
    assert 0.7 < ct._levenshtein_ratio("performance", "performonce") < 1.0
    # Vastly different lengths short-circuit to 0.0.
    assert ct._levenshtein_ratio("a", "abcdefghijk") == 0.0


def test_bounded_levenshtein_matches_unbounded_for_small_inputs():
    from itertools import product
    words = [''.join(chars) for length in range(1, 5) for chars in product('ab', repeat=length)]
    for left in words:
        for right in words:
            expected = ct._levenshtein_ratio(left, right)
            for bound in (0, 1, 2):
                actual = ct._levenshtein_ratio(left, right, max_distance=bound)
                distance = round((1 - expected) * max(len(left), len(right)))
                assert actual == (expected if distance <= bound else 0.0), (left, right, bound)


def test_bounded_levenshtein_preserves_long_common_context():
    left = 'prefix' * 100 + 'old' + 'suffix' * 100
    right = 'prefix' * 100 + 'oXd' + 'suffix' * 100
    assert ct._levenshtein_ratio(left, right, max_distance=1) == 1 - 1 / len(left)
