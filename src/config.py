"""Central configuration + Ollama / LLM availability detection.

Single source of truth for "is the LLM usable right now". Every code path
that calls Ollama should gate on `has_llm()` first so the system degrades
gracefully on machines without Ollama installed (or with the wrong model).

Env vars:
    OLLAMA_URL          — base URL (default http://localhost:11434)
    MEMORY_LLM_MODEL    — model name (default qwen2.5-coder:7b)
    MEMORY_LLM_ENABLED  — "auto" (probe Ollama, default), "true"/"force",
                          "false" (disable LLM features entirely)
    MEMORY_LLM_PROBE_TTL_SEC — cache TTL for the probe (default 60s)
    MEMORY_LLM_TIMEOUT_SEC   — global Ollama timeout fallback (default 60)
    MEMORY_TRIPLE_TIMEOUT_SEC — triple extraction timeout (default 30)
    MEMORY_ENRICH_TIMEOUT_SEC — deep enrichment timeout (default 45)
    MEMORY_REPR_TIMEOUT_SEC   — representations timeout (default 60)
    MEMORY_TRIPLE_MAX_PREDICT — triple extraction num_predict cap (default 2048)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


# ──────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────


def get_ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", "http://localhost:11434")


def get_llm_model() -> str:
    """Configured LLM model name. Default = a model that is usually present."""
    return os.environ.get("MEMORY_LLM_MODEL", "qwen2.5-coder:7b")


def get_llm_mode() -> str:
    """auto | true/force | false."""
    return os.environ.get("MEMORY_LLM_ENABLED", "auto").strip().lower()


def get_probe_ttl() -> float:
    try:
        return float(os.environ.get("MEMORY_LLM_PROBE_TTL_SEC", "60"))
    except ValueError:
        return 60.0


def _get_float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _get_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _get_phase_timeout_env(name: str, phase_default: float) -> float:
    raw = os.environ.get(name)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    if "MEMORY_LLM_TIMEOUT_SEC" in os.environ:
        return get_llm_timeout_sec()
    return phase_default


def get_llm_timeout_sec() -> float:
    """Global fallback timeout for Ollama requests."""
    return _get_float_env("MEMORY_LLM_TIMEOUT_SEC", 60.0)


def get_triple_timeout_sec() -> float:
    """Timeout for deep triple extraction requests."""
    return _get_phase_timeout_env("MEMORY_TRIPLE_TIMEOUT_SEC", 30.0)


def get_enrich_timeout_sec() -> float:
    """Timeout for deep enrichment requests."""
    return _get_phase_timeout_env("MEMORY_ENRICH_TIMEOUT_SEC", 45.0)


def get_repr_timeout_sec() -> float:
    """Timeout for representation generation requests."""
    return _get_phase_timeout_env("MEMORY_REPR_TIMEOUT_SEC", 60.0)


def get_triple_max_predict() -> int:
    """Max tokens requested from Ollama during triple extraction."""
    return _get_int_env("MEMORY_TRIPLE_MAX_PREDICT", 2048)


# ──────────────────────────────────────────────
# Probe cache
# ──────────────────────────────────────────────


_cache: dict[str, object] = {}


def _cache_clear() -> None:
    """Reset the in-memory probe cache (called from tests)."""
    _cache.clear()


def _cache_get(key: str) -> object | None:
    rec = _cache.get(key)
    if rec is None:
        return None
    expires_at, value = rec  # type: ignore[misc]
    if time.time() >= expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: object, ttl: float | None = None) -> None:
    ttl = ttl if ttl is not None else get_probe_ttl()
    _cache[key] = (time.time() + ttl, value)


# ──────────────────────────────────────────────
# Probes
# ──────────────────────────────────────────────


def detect_ollama() -> bool:
    """True if `GET {OLLAMA_URL}/api/tags` returns 200. Cached."""
    cached = _cache_get("ollama_available")
    if cached is not None:
        return bool(cached)

    url = f"{get_ollama_url().rstrip('/')}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            resp.read()
        _cache_set("ollama_available", True)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        _cache_set("ollama_available", False)
        return False


def list_ollama_models() -> list[str]:
    """Names of locally installed models. Empty list if Ollama unreachable."""
    cached = _cache_get("models")
    if cached is not None:
        return list(cached)  # type: ignore[arg-type]

    url = f"{get_ollama_url().rstrip('/')}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
        names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, ValueError):
        names = []
    _cache_set("models", names)
    return names


def has_model(name: str) -> bool:
    """Match exact name OR by prefix without `:tag` (so `qwen2.5-coder`
    matches `qwen2.5-coder:7b`)."""
    if not name:
        return False
    installed = list_ollama_models()
    if not installed:
        return False
    if name in installed:
        return True
    name_lower = name.lower()
    for m in installed:
        m_lower = m.lower()
        if m_lower == name_lower:
            return True
        # User asked "qwen2.5-coder", local has "qwen2.5-coder:7b"
        m_base = m_lower.split(":", 1)[0]
        if m_base == name_lower or m_base == name_lower.split(":", 1)[0]:
            return True
    return False


# ──────────────────────────────────────────────
# Top-level gate
# ──────────────────────────────────────────────


def has_llm() -> bool:
    """One-line check used by Ollama-calling code paths.

    Returns True iff:
      - MEMORY_LLM_ENABLED is not 'false', AND
      - either MEMORY_LLM_ENABLED in {'true','force'} (skip probe), OR
      - Ollama is reachable AND the configured model is installed.
    """
    mode = get_llm_mode()
    if mode == "false":
        return False
    if mode in ("true", "force"):
        return True
    # "auto" — probe
    if not detect_ollama():
        return False
    return has_model(get_llm_model())


# ──────────────────────────────────────────────
# Operator-facing summary
# ──────────────────────────────────────────────


def get_status() -> dict:
    """Snapshot for dashboards / `memory_stats`."""
    avail = detect_ollama()
    model = get_llm_model()
    return {
        "ollama_url": get_ollama_url(),
        "ollama_available": avail,
        "model_configured": model,
        "model_installed": has_model(model) if avail else False,
        "installed_models": list_ollama_models() if avail else [],
        "llm_mode": get_llm_mode(),
        "llm_enabled": has_llm(),
    }
