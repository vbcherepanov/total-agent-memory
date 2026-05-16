"""Tests for noise filter on recall (Wave A — A3).

Operational tags like ``recovery`` and ``auto-extract`` mark records that
have forensic value but pollute top-K recall. They are filtered out of the
default ``memory_recall`` path. Direct ``memory_search_by_tag`` still surfaces
them.
"""

from __future__ import annotations

import pytest

import config


def test_default_excluded_tags():
    excluded = config.get_recall_excluded_tags()
    assert "recovery" in excluded
    assert "auto-extract" in excluded


def test_env_override_replaces_defaults(monkeypatch):
    monkeypatch.setenv("MEMORY_RECALL_EXCLUDED_TAGS", "debug,test-data,scratch")
    excluded = config.get_recall_excluded_tags()
    assert excluded == ("debug", "test-data", "scratch")
    # Defaults are replaced, not extended — keeps behavior predictable.
    assert "recovery" not in excluded


def test_env_disable_returns_empty(monkeypatch):
    monkeypatch.setenv("MEMORY_RECALL_EXCLUDE", "0")
    assert config.get_recall_excluded_tags() == tuple()


def test_env_disable_variants(monkeypatch):
    for v in ("0", "false", "no", "off", "FALSE", "Off"):
        monkeypatch.setenv("MEMORY_RECALL_EXCLUDE", v)
        assert config.get_recall_excluded_tags() == tuple(), (
            f"value {v!r} should disable the filter"
        )


def test_env_empty_list(monkeypatch):
    """Empty env value collapses to empty tuple (filter no-ops)."""
    monkeypatch.setenv("MEMORY_RECALL_EXCLUDED_TAGS", "")
    assert config.get_recall_excluded_tags() == tuple()


def test_env_whitespace_trimmed(monkeypatch):
    monkeypatch.setenv("MEMORY_RECALL_EXCLUDED_TAGS", "  recovery , auto-extract  ")
    assert config.get_recall_excluded_tags() == ("recovery", "auto-extract")
