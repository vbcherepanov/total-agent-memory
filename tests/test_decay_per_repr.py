"""Tests for per-representation half-life decay (Wave A — A1).

A LLM-generated ``summary`` ages faster than ``raw`` content because the
summary encodes the model's understanding of the world at generation time.
``config.get_repr_half_life_days`` exposes this knob; ``Store._decay_factor``
applies it.
"""

from __future__ import annotations

import math

import pytest

import config
from server import Store


# ──────────────────────────────────────────────
# config knobs
# ──────────────────────────────────────────────


def test_defaults_for_known_repr_types():
    assert config.get_repr_half_life_days("raw") == 180
    assert config.get_repr_half_life_days("keywords") == 90
    assert config.get_repr_half_life_days("compressed") == 60
    assert config.get_repr_half_life_days("questions") == 45
    assert config.get_repr_half_life_days("summary") == 30


def test_unknown_repr_falls_back_to_parent(monkeypatch):
    monkeypatch.setenv("MEMORY_DECAY_PARENT_DAYS", "75")
    assert config.get_repr_half_life_days(None) == 75
    assert config.get_repr_half_life_days("") == 75
    assert config.get_repr_half_life_days("totally-unknown") == 75


def test_env_overrides_per_repr(monkeypatch):
    monkeypatch.setenv("MEMORY_DECAY_SUMMARY_DAYS", "7")
    monkeypatch.setenv("MEMORY_DECAY_RAW_DAYS", "365")
    assert config.get_repr_half_life_days("summary") == 7
    assert config.get_repr_half_life_days("raw") == 365


def test_repr_type_is_case_insensitive():
    assert config.get_repr_half_life_days("SUMMARY") == 30
    assert config.get_repr_half_life_days("Raw") == 180


# ──────────────────────────────────────────────
# decay curve
# ──────────────────────────────────────────────


def _iso_days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_summary_decays_faster_than_raw_at_same_age():
    """At 90 days old: summary (hl=30) is far below raw (hl=180)."""
    ts = _iso_days_ago(90)
    decay_summary = Store._decay_factor(ts, half_life_days=30)
    decay_raw = Store._decay_factor(ts, half_life_days=180)
    assert decay_summary < decay_raw
    # 90 days at hl=30 → exp(-90*ln2/30) ≈ 0.125
    assert 0.10 < decay_summary < 0.16
    # 90 days at hl=180 → exp(-90*ln2/180) ≈ 0.707
    assert 0.65 < decay_raw < 0.75


def test_fresh_record_keeps_score():
    """0-day-old record: both summary and raw should be ~1.0."""
    ts = _iso_days_ago(0)
    assert Store._decay_factor(ts, half_life_days=30) > 0.99
    assert Store._decay_factor(ts, half_life_days=180) > 0.99


def test_very_old_record_clamped_floor():
    """Even a 10-year-old record stays above the 0.01 floor."""
    ts = _iso_days_ago(3650)
    assert Store._decay_factor(ts, half_life_days=30) >= 0.01
    assert Store._decay_factor(ts, half_life_days=180) >= 0.01


def test_missing_last_confirmed_default_05():
    """Missing/empty timestamp falls back to 0.5 (uncertain, not zero)."""
    assert Store._decay_factor("", half_life_days=30) == 0.5
    assert Store._decay_factor(None, half_life_days=30) == 0.5  # type: ignore[arg-type]
