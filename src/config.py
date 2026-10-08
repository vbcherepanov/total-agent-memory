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

Cloud provider knobs (used by src/llm_provider.py and src/embed_provider.py;
wiring into call-sites happens in a separate wave — these just expose env):
    MEMORY_LLM_PROVIDER   — ollama|openai|anthropic|auto (default ollama)
    MEMORY_LLM_API_BASE   — override provider base URL
    MEMORY_LLM_API_KEY    — bearer/auth key for the LLM provider
    MEMORY_EMBED_PROVIDER — fastembed|openai|cohere|dashscope (default fastembed)
    MEMORY_EMBED_MODEL    — embedding model name (provider-specific default)
    MEMORY_EMBED_API_BASE — override embedding provider base URL
    MEMORY_EMBED_API_KEY  — key for embedding provider
    MEMORY_EMBED_DIMENSIONS      — requested output dimensions (dashscope default 1024)
    MEMORY_EMBED_BATCH_SIZE      — texts per request (openai 64, dashscope 10)
    MEMORY_EMBED_MAX_RETRIES     — retries on 408/425/429/5xx/network errors (default 6)
    MEMORY_EMBED_TIMEOUT_SEC     — per-request timeout (default 60)
    MEMORY_EMBED_MAX_BACKOFF_SEC — cap for a single retry delay (default 30)
    MEMORY_{TRIPLE|ENRICH|REPR}_PROVIDER — per-phase provider override
    MEMORY_{TRIPLE|ENRICH|REPR}_MODEL    — per-phase model override
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


# ──────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────

DEFAULT_EMBED_THREADS = 1
DEFAULT_TORCH_THREADS = 1
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"


def get_embed_threads() -> int:
    value = int(os.environ.get("MEMORY_EMBED_THREADS", str(DEFAULT_EMBED_THREADS)))
    if value < 1:
        raise ValueError("MEMORY_EMBED_THREADS must be a positive integer")
    return value


def get_torch_threads() -> int:
    value = int(os.environ.get("MEMORY_TORCH_THREADS", str(DEFAULT_TORCH_THREADS)))
    if value < 1:
        raise ValueError("MEMORY_TORCH_THREADS must be a positive integer")
    return value


def get_ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", "http://localhost:11434")


def get_llm_model() -> str:
    """Configured LLM model name. Default = a model that is usually present."""
    return os.environ.get("MEMORY_LLM_MODEL") or DEFAULT_OLLAMA_MODEL


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
# Decay half-lives per representation type
# ──────────────────────────────────────────────
# LLM-generated views age faster than raw content because they encode the
# model's understanding of the world at generation time. A summary written
# 3 months ago may misrepresent today's record; the raw text itself does not.
#
# These knobs are consumed by ``Store._decay_factor`` in ``server.py`` to
# pick a per-tier half-life. Override via env, e.g.
#   MEMORY_DECAY_SUMMARY_DAYS=14   # aggressive summary decay
#   MEMORY_DECAY_RAW_DAYS=365      # slow decay for raw

_REPR_HALF_LIFE_DEFAULTS: dict[str, int] = {
    "raw":        180,
    "keywords":   90,
    "compressed": 60,
    "questions":  45,
    "summary":    30,
}


def get_repr_half_life_days(repr_type: str | None) -> int:
    """Half-life (days) for decay scoring when a record matched via this view.

    ``repr_type`` is one of raw/summary/keywords/questions/compressed (case-
    insensitive). Unknown / empty types fall back to the parent half-life.
    Env override pattern: MEMORY_DECAY_<TYPE>_DAYS.
    """
    if not repr_type:
        return get_parent_half_life_days()
    rt = repr_type.lower()
    default = _REPR_HALF_LIFE_DEFAULTS.get(rt)
    if default is None:
        return get_parent_half_life_days()
    return _get_int_env(f"MEMORY_DECAY_{rt.upper()}_DAYS", default)


def get_parent_half_life_days() -> int:
    """Half-life for records matched through non-representation tiers
    (fts, fuzzy, graph, episode). Default 90d preserves legacy behavior."""
    return _get_int_env("MEMORY_DECAY_PARENT_DAYS", 90)


def get_edge_half_life_days() -> int:
    """Half-life (days) for KG edge freshness in context expansion.

    Used by ``context_expander`` to damp contributions from stale edges so
    that fresh 1-hop links can lift older nodes more effectively than long-
    dormant ones. ``last_reinforced_at`` (falling back to ``created_at``)
    drives the curve.
    """
    return _get_int_env("MEMORY_EDGE_HALF_LIFE_DAYS", 60)


# ──────────────────────────────────────────────
# Recall noise filters
# ──────────────────────────────────────────────
# Records tagged with operational/internal markers (e.g. recovery snapshots
# from session-end, auto-extract harvests from raw transcripts) inflate top-K
# without adding signal. By default they are excluded from ``memory_recall``
# unless the caller explicitly asks for one of these tags. Override via env:
#   MEMORY_RECALL_EXCLUDED_TAGS=recovery,auto-extract,debug,test-data
#   MEMORY_RECALL_EXCLUDE=0   # disable the filter entirely

_RECALL_EXCLUDED_TAGS_DEFAULT = ("recovery", "auto-extract")


def get_recall_excluded_tags() -> tuple[str, ...]:
    """Tag names that are filtered out of recall results by default.

    Comma-separated env override at MEMORY_RECALL_EXCLUDED_TAGS.
    Empty / disabled (``MEMORY_RECALL_EXCLUDE=0``) → no filter applied.
    """
    if os.environ.get("MEMORY_RECALL_EXCLUDE", "1").strip().lower() in (
        "0", "false", "no", "off"
    ):
        return tuple()
    raw = os.environ.get("MEMORY_RECALL_EXCLUDED_TAGS")
    if raw is None:
        return _RECALL_EXCLUDED_TAGS_DEFAULT
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts


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

    # v11 Phase 5 — defensive telemetry: any Ollama probe on the hot path is
    # a fast-mode violation. The bench asserts this stays 0.
    try:
        from memory_core.telemetry import counters as _v11_counters
        _v11_counters.bump("network_calls", 1.0)
    except Exception:
        pass

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
    return model_matches(name, list_ollama_models())


def model_matches(name: str, installed: list[str]) -> bool:
    """True if ``name`` is among ``installed`` Ollama model names, by exact
    name or by base name without the ``:tag``."""
    if not name or not installed:
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


def has_llm(phase: str | None = None) -> bool:
    """One-line check used by LLM-calling code paths.

    Provider-aware: resolves the active provider for ``phase`` (or the global
    MEMORY_LLM_PROVIDER when ``phase`` is None) and asks it whether it is
    usable right now.

    Fast paths:
      - MEMORY_LLM_ENABLED=false      → always False
      - MEMORY_LLM_ENABLED in (true,force) → always True (skip network probe)

    Otherwise:
      - provider == ollama → legacy probe: detect_ollama() + has_model(model)
      - provider != ollama → delegate to the provider's own ``available()``
        (see ``llm_provider.py``); failures are swallowed so a misconfigured
        provider can't blow up the caller — they just get ``False``.
    """
    mode = get_llm_mode()
    if mode == "false":
        return False
    if mode in ("true", "force"):
        return True

    # Resolve active provider — phase override wins over global default.
    try:
        provider_name = get_phase_provider(phase) if phase else get_llm_provider()
    except ValueError:
        # Unknown phase — fall back to global so we never crash callers.
        provider_name = get_llm_provider()

    if provider_name == "ollama":
        if not detect_ollama():
            return False
        return has_model(get_llm_model_for_provider("ollama"))

    # Cloud provider — lazy import to avoid circular deps at module load.
    try:
        from llm_provider import make_provider

        provider = make_provider(provider_name)
        return bool(provider.available())
    except Exception:  # noqa: BLE001 — never let a probe raise.
        return False


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


# ──────────────────────────────────────────────
# Cloud provider configuration
# ──────────────────────────────────────────────
#
# Scaffolding for pluggable LLM / embedding backends. Call-sites still use
# Ollama directly — wiring happens in a separate wave. These helpers expose
# env-driven config in the same style as the Ollama helpers above so the new
# abstraction layer (src/llm_provider.py, src/embed_provider.py) can stay
# dumb and declarative.
#
# Provider names are canonical, lowercase:
#   LLM:   ollama | openai | anthropic | auto
#   EMBED: fastembed | openai | cohere | dashscope


_SUPPORTED_LLM_PROVIDERS = ("ollama", "openai", "openai-compatible", "anthropic", "auto")
_SUPPORTED_EMBED_PROVIDERS = ("fastembed", "openai", "cohere", "dashscope")
_SUPPORTED_PHASES = ("triple", "enrich", "repr", "reason")

# Default model name per provider when MEMORY_LLM_MODEL isn't set.
_DEFAULT_LLM_MODEL_BY_PROVIDER = {
    "ollama": DEFAULT_OLLAMA_MODEL,
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}

_DEFAULT_LLM_API_BASE_BY_PROVIDER = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}

_DEFAULT_EMBED_MODEL_BY_PROVIDER = {
    "fastembed": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "openai": "text-embedding-3-small",
    "cohere": "embed-multilingual-v3.0",
    "dashscope": "text-embedding-v4",
}

_DEFAULT_EMBED_API_BASE_BY_PROVIDER = {
    "fastembed": "",  # local, no HTTP
    "openai": "https://api.openai.com/v1",
    "cohere": "https://api.cohere.com/v2",
    # Alibaba Cloud Model Studio, OpenAI-compatible mode, Singapore region.
    # Keys are region-bound: a Beijing key needs
    # https://dashscope.aliyuncs.com/compatible-mode/v1 via MEMORY_EMBED_API_BASE.
    "dashscope": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}

# Env var that carries the key for a given provider, in fallback order.
_LLM_KEY_ENV_BY_PROVIDER = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "ollama": (),  # no auth
}

_EMBED_KEY_ENV_BY_PROVIDER = {
    "openai": ("OPENAI_API_KEY",),
    "cohere": ("COHERE_API_KEY",),
    "dashscope": ("DASHSCOPE_API_KEY",),
    "fastembed": (),  # local
}

_DEFAULT_EMBED_DIMENSIONS_BY_PROVIDER = {"dashscope": 1024}
_DEFAULT_EMBED_BATCH_BY_PROVIDER = {"openai": 64, "dashscope": 10}
DEFAULT_EMBED_MAX_RETRIES = 6
DEFAULT_EMBED_TIMEOUT_SEC = 60.0
DEFAULT_EMBED_MAX_BACKOFF_SEC = 30.0


def _normalize_provider(name: str, allowed: tuple[str, ...], default: str) -> str:
    raw = (name or "").strip().lower()
    if raw in allowed:
        return raw
    return default


def _auto_resolve_llm_provider() -> str:
    """Pick a provider from env keys when MEMORY_LLM_PROVIDER=auto."""
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("COHERE_API_KEY"):
        # Cohere is embed-only; still fall through to ollama for completion.
        return "ollama"
    return "ollama"


def _configured_llm_provider() -> str:
    """The provider MEMORY_LLM_PROVIDER names (`auto` resolved from API keys)."""
    raw = _normalize_provider(
        os.environ.get("MEMORY_LLM_PROVIDER", "ollama"),
        _SUPPORTED_LLM_PROVIDERS,
        default="ollama",
    )
    if raw == "auto":
        return _auto_resolve_llm_provider()
    return raw


def get_llm_provider() -> str:
    """Canonical LLM provider name for the global phase.

    `auto` resolves by probing well-known API-key env vars. When
    MEMORY_LLM_FALLBACK_PROVIDERS is set, the first provider of the chain
    that answers its availability probe is returned (see resolve_llm_provider).
    """
    return resolve_llm_provider(None)


def get_llm_api_base(provider: str | None = None) -> str:
    """Base URL for the configured LLM provider (or an explicit one)."""
    p = provider or get_llm_provider()
    override = os.environ.get("MEMORY_LLM_API_BASE")
    if override:
        return override.rstrip("/")
    if p == "ollama":
        # Stay in sync with the legacy Ollama URL env.
        return get_ollama_url().rstrip("/")
    return _DEFAULT_LLM_API_BASE_BY_PROVIDER.get(p, "").rstrip("/")


def get_llm_api_key(provider: str | None = None) -> str | None:
    """API key for the LLM provider.

    Lookup order:
      1. MEMORY_LLM_API_KEY  (universal override)
      2. provider-specific env (OPENAI_API_KEY / ANTHROPIC_API_KEY / …)
    Returns None for providers that don't need auth (ollama).
    """
    p = provider or get_llm_provider()
    override = os.environ.get("MEMORY_LLM_API_KEY")
    if override:
        return override
    for env_name in _LLM_KEY_ENV_BY_PROVIDER.get(p, ()):  # type: ignore[arg-type]
        val = os.environ.get(env_name)
        if val:
            return val
    return None


def get_llm_model_for_provider(provider: str | None = None) -> str:
    """Model name respecting provider defaults when MEMORY_LLM_MODEL unset."""
    p = provider or get_llm_provider()
    override = os.environ.get("MEMORY_LLM_MODEL")
    if override:
        return override
    if p == "openai-compatible":
        raise ValueError("MEMORY_LLM_MODEL is required for openai-compatible")
    return _DEFAULT_LLM_MODEL_BY_PROVIDER.get(p, get_llm_model())


def get_embed_provider() -> str:
    return _normalize_provider(
        os.environ.get("MEMORY_EMBED_PROVIDER", "fastembed"),
        _SUPPORTED_EMBED_PROVIDERS,
        default="fastembed",
    )


def get_embed_api_base(provider: str | None = None) -> str:
    p = provider or get_embed_provider()
    override = os.environ.get("MEMORY_EMBED_API_BASE")
    if override:
        return override.rstrip("/")
    return _DEFAULT_EMBED_API_BASE_BY_PROVIDER.get(p, "").rstrip("/")


def get_embed_api_key(provider: str | None = None) -> str | None:
    p = provider or get_embed_provider()
    override = os.environ.get("MEMORY_EMBED_API_KEY")
    if override:
        return override
    for env_name in _EMBED_KEY_ENV_BY_PROVIDER.get(p, ()):  # type: ignore[arg-type]
        val = os.environ.get(env_name)
        if val:
            return val
    return None


def get_embed_model(provider: str | None = None) -> str:
    p = provider or get_embed_provider()
    override = os.environ.get("MEMORY_EMBED_MODEL")
    if override:
        return override
    if p == "fastembed":
        # The local provider embeds the text space, so it follows that space's model.
        return get_text_embed_model()
    return _DEFAULT_EMBED_MODEL_BY_PROVIDER.get(p, "")


def _strict_int_env(name: str, minimum: int) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _strict_positive_float_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def get_embed_dimensions(provider: str | None = None) -> int | None:
    """Requested embedding dimensions, or None for the model's native size.

    Invalid values raise instead of falling back: a silently different
    dimension makes every stored vector incomparable with the next one.
    """
    value = _strict_int_env("MEMORY_EMBED_DIMENSIONS", 1)
    if value is not None:
        return value
    return _DEFAULT_EMBED_DIMENSIONS_BY_PROVIDER.get(provider or get_embed_provider())


def get_embed_batch_size(provider: str | None = None) -> int:
    value = _strict_int_env("MEMORY_EMBED_BATCH_SIZE", 1)
    if value is not None:
        return value
    return _DEFAULT_EMBED_BATCH_BY_PROVIDER.get(provider or get_embed_provider(), 64)


def get_embed_max_retries() -> int:
    value = _strict_int_env("MEMORY_EMBED_MAX_RETRIES", 0)
    return DEFAULT_EMBED_MAX_RETRIES if value is None else value


def get_embed_timeout_sec() -> float:
    value = _strict_positive_float_env("MEMORY_EMBED_TIMEOUT_SEC")
    return DEFAULT_EMBED_TIMEOUT_SEC if value is None else value


def get_embed_max_backoff_sec() -> float:
    value = _strict_positive_float_env("MEMORY_EMBED_MAX_BACKOFF_SEC")
    return DEFAULT_EMBED_MAX_BACKOFF_SEC if value is None else value


def _normalize_phase(phase: str) -> str:
    p = (phase or "").strip().lower()
    if p not in _SUPPORTED_PHASES:
        raise ValueError(
            f"unsupported phase {phase!r}; expected one of {_SUPPORTED_PHASES}"
        )
    return p


def _configured_phase_provider(phase: str) -> str:
    """Per-phase provider override (MEMORY_<PHASE>_PROVIDER), else the global one."""
    p = _normalize_phase(phase)
    env_name = f"MEMORY_{p.upper()}_PROVIDER"
    raw = os.environ.get(env_name)
    if not raw:
        return _configured_llm_provider()
    normalized = _normalize_provider(raw, _SUPPORTED_LLM_PROVIDERS, default=_configured_llm_provider())
    if normalized == "auto":
        return _auto_resolve_llm_provider()
    return normalized


def get_phase_provider(phase: str) -> str:
    """Provider for a phase: the configured one, or with MEMORY_LLM_FALLBACK_PROVIDERS
    the first provider of the phase's chain that is available right now."""
    return resolve_llm_provider(_normalize_phase(phase))


def get_phase_model(phase: str) -> str:
    """Per-phase model override. Falls back to the phase provider default."""
    p = _normalize_phase(phase)
    env_name = f"MEMORY_{p.upper()}_MODEL"
    override = os.environ.get(env_name)
    if override:
        return override
    return get_llm_model_for_provider(get_phase_provider(p))


# ──────────────────────────────────────────────
# Active Context (live-doc markdown projection)
# ──────────────────────────────────────────────


def get_active_context_vault() -> Path:
    """Vault root for activeContext.md markdown projections."""
    raw = os.environ.get(
        "MEMORY_ACTIVECONTEXT_VAULT",
        "~/Documents/project/Projects",
    )
    return Path(raw).expanduser()


def is_active_context_enabled() -> bool:
    """True unless MEMORY_ACTIVECONTEXT_DISABLE is set to a truthy value."""
    return os.environ.get("MEMORY_ACTIVECONTEXT_DISABLE", "").lower() not in (
        "1",
        "true",
        "yes",
    )


# ──────────────────────────────────────────────
# v9.0 Phase 1 feature flags
# ──────────────────────────────────────────────
#
# All default OFF — zero regression on upgrade. Flip per-lane to opt in.
#
#   V9_PARALLEL_RETRIEVAL — A1: run FTS+semantic+fuzzy+graph tiers via
#                           asyncio.gather instead of sequential await.
#   V9_CACHE_L1_ENABLED   — A2: in-memory LRU query→top-K (fast path).
#   V9_CACHE_L2_ENABLED   — A2: SQLite embedding_cache keyed by sha256(text).
#   V9_CACHE_L1_SIZE      — A2: max LRU entries (default 1000).
#   V9_CACHE_L1_TTL_SEC   — A2: LRU TTL seconds (default 300).
#   V9_EMBED_BACKEND      — B1: "fastembed" (default) | "bge-m3" | "e5-large" | "minilm".


def _get_bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def is_v9_parallel_retrieval_enabled() -> bool:
    return _get_bool_env("V9_PARALLEL_RETRIEVAL", default=False)


def is_v9_cache_l1_enabled() -> bool:
    return _get_bool_env("V9_CACHE_L1_ENABLED", default=False)


def is_v9_cache_l2_enabled() -> bool:
    return _get_bool_env("V9_CACHE_L2_ENABLED", default=False)


def get_v9_cache_l1_size() -> int:
    return _get_int_env("V9_CACHE_L1_SIZE", 1000)


def get_v9_cache_l1_ttl_sec() -> float:
    return _get_float_env("V9_CACHE_L1_TTL_SEC", 300.0)


_SUPPORTED_V9_EMBED_BACKENDS = (
    "fastembed",
    "bge-m3",
    "e5-large",
    "minilm",
    # v9 D5: locally-fine-tuned embedding (scripts/finetune_embedding.py).
    # Resolves to a sentence-transformers model dir on disk via choose_embed.
    "locomo-tuned-minilm",
    # v9 D1: OpenAI cloud embeddings — re-uses MEMORY_EMBED_API_KEY plumbing
    # in src/embed_provider.OpenAIEmbedProvider.
    "openai-3-small",   # 1536d, ~5x cheaper than 3-large, ~+2-3pp R@5 vs MiniLM.
    "openai-3-large",   # 3072d, top-tier quality, primary target for v9 push.
)


def get_v9_embed_backend() -> str:
    raw = (os.environ.get("V9_EMBED_BACKEND", "fastembed") or "fastembed").strip().lower()
    if raw in _SUPPORTED_V9_EMBED_BACKENDS:
        return raw
    return "fastembed"


def get_v9_locomo_tuned_path() -> str:
    """Path to the locomo-tuned-minilm sentence-transformers dir.

    Default points at ``./models/locomo-tuned-minilm`` relative to the repo
    root, which is where ``scripts/finetune_embedding.py all`` writes by
    convention. Override with ``V9_LOCOMO_TUNED_PATH`` env.
    """
    return (os.environ.get("V9_LOCOMO_TUNED_PATH") or "./models/locomo-tuned-minilm").strip()


# v9 D4 — Reranker backend selector.
#
#   ce-marco   (default) cross-encoder/ms-marco-MiniLM-L-6-v2 — web-search trained,
#              regresses on conversational data per LoCoMo ablations (-1.2pp).
#   bge-v2-m3  BAAI/bge-reranker-v2-m3 — multilingual, conversation-friendly.
#   bge-large  BAAI/bge-reranker-large — English-only, higher accuracy on long ctx.
#   off        skip reranking entirely.
_SUPPORTED_RERANKER_BACKENDS = ("ce-marco", "bge-v2-m3", "bge-large", "off")


def get_v9_reranker_backend() -> str:
    raw = (os.environ.get("V9_RERANKER_BACKEND", "ce-marco") or "ce-marco").strip().lower()
    if raw in _SUPPORTED_RERANKER_BACKENDS:
        return raw
    return "ce-marco"


def get_v9_reranker_model_override() -> str | None:
    """Optional explicit HF model id override (skips backend → model table)."""
    raw = (os.environ.get("V9_RERANKER_MODEL", "") or "").strip()
    return raw or None


def get_v9_reranker_use_fp16() -> bool:
    """fp16 compute for BGE rerankers — ~2x speed on Apple Silicon/CUDA, no acc loss."""
    raw = (os.environ.get("V9_RERANKER_FP16", "1") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────
# v11.0 — MEMORY_MODE + breaking defaults
# ──────────────────────────────────────────────
#
# v11 splits the system into a deterministic Memory Core (no LLM in the
# hot path) and an asynchronous AI Layer (worker drains enrichment jobs).
# `MEMORY_MODE` selects the runtime profile and derives sane defaults for
# every legacy v10.x knob — so a fresh install is automatically fast.
#
#   ultrafast  FTS-only, no embeddings on save unless cached, save < 20 ms
#   fast       (default) FastEmbed + FTS5 + vector + RRF, zero LLM, no Ollama
#   balanced   fast hot path + async enrichment worker on
#   deep       legacy v10.5 behaviour (sync quality_gate / contradiction /
#              advanced RAG); reranker on
#
# Every derived flag uses `setdefault` so an explicit env override always
# wins (escape hatch for power users).


SUPPORTED_MEMORY_MODES = ("ultrafast", "fast", "balanced", "deep")


def get_memory_mode() -> str:
    """Resolved MEMORY_MODE (default fast). Always one of SUPPORTED_MEMORY_MODES."""
    raw = (os.environ.get("MEMORY_MODE", "fast") or "fast").strip().lower()
    if raw not in SUPPORTED_MEMORY_MODES:
        return "fast"
    return raw


def use_llm_in_hot_path() -> bool:
    raw = (os.environ.get("MEMORY_USE_LLM_IN_HOT_PATH", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def allow_ollama_in_hot_path() -> bool:
    raw = (os.environ.get("MEMORY_ALLOW_OLLAMA_IN_HOT_PATH", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_negative_retrieval_enabled() -> bool:
    """Contradiction-seeking second pass in memory_answer (on by default)."""
    return _get_bool_env("MEMORY_NEGATIVE_RETRIEVAL", default=True)


CONTRADICTION_POLICIES = ("resolve", "abstain")
CONTRADICTION_SCORERS = ("llm", "jev")
TYPESAFE_DEFAULT_BASE_URL = "https://api.typesafe.ai"
TYPESAFE_DEFAULT_MODEL = "jev-latest"


def get_contradiction_scorer() -> str:
    """Who scores contradiction pairs in memory_answer.

    ``llm`` (default) uses the reasoning provider. ``jev`` uses TypeSafe's
    System One model and needs ``TYPESAFE_API_KEY``. Unknown values fall back
    to ``llm``.
    """
    raw = os.environ.get("MEMORY_CONTRADICTION_SCORER", "").strip().lower()
    return raw if raw in CONTRADICTION_SCORERS else CONTRADICTION_SCORERS[0]


def get_typesafe_api_key() -> str | None:
    return os.environ.get("TYPESAFE_API_KEY") or None


def get_typesafe_base_url() -> str:
    return (os.environ.get("TYPESAFE_BASE_URL") or TYPESAFE_DEFAULT_BASE_URL).strip()


def get_typesafe_model() -> str:
    return (os.environ.get("TYPESAFE_DEFAULT_MODEL") or TYPESAFE_DEFAULT_MODEL).strip()


def get_contradiction_policy() -> str:
    """What memory_answer does on a hard contradiction.

    ``resolve`` (default) hands both sides, with their recording dates, to the
    reader, which answers with the current value. ``abstain`` refuses without
    reading. Unknown values fall back to ``resolve``.
    """
    raw = os.environ.get("MEMORY_CONTRADICTION_POLICY", "").strip().lower()
    return raw if raw in CONTRADICTION_POLICIES else CONTRADICTION_POLICIES[0]


# 14.5 — cross-encoder re-ranking of the fused candidate window (fastembed,
# ONNX, no torch). The model scores (query, record) pairs; its rank joins the
# RRF rank with CROSS_RERANK_WEIGHT. English-only models are skipped for
# queries written mostly outside the Latin script.
def context_resolves_dates() -> bool:
    """MEMORY_CONTEXT_RESOLVE_DATES (default on): context mode follows relative date
    phrases ("last Thursday") with the date they denote, counted from the record's time."""
    raw = (os.environ.get("MEMORY_CONTEXT_RESOLVE_DATES", "on") or "on").strip().lower()
    if raw not in ("on", "off"):
        raise ValueError("MEMORY_CONTEXT_RESOLVE_DATES must be on or off")
    return raw == "on"


CROSS_RERANK_MODES = ("auto", "on", "off")
DEFAULT_CROSS_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
MULTILINGUAL_CROSS_RERANK_MODELS = frozenset({"jinaai/jina-reranker-v2-base-multilingual"})
DEFAULT_CROSS_RERANK_WINDOW = 50
DEFAULT_CROSS_RERANK_WEIGHT = 2.0
# Characters of the previous and of the next turn the encoder reads with each candidate.
DEFAULT_CROSS_RERANK_CONTEXT = 400


def get_cross_rerank_mode() -> str:
    """``auto`` (default) re-ranks once the model is loaded; ``on`` waits for it; ``off`` disables."""
    raw = (os.environ.get("MEMORY_CROSS_RERANK", "auto") or "auto").strip().lower()
    if raw in ("1", "true", "yes"):
        return "on"
    if raw in ("0", "false", "no"):
        return "off"
    if raw not in CROSS_RERANK_MODES:
        raise ValueError(f"MEMORY_CROSS_RERANK must be one of {', '.join(CROSS_RERANK_MODES)}")
    return raw


def get_cross_rerank_model() -> str:
    return (os.environ.get("MEMORY_CROSS_RERANK_MODEL", "") or DEFAULT_CROSS_RERANK_MODEL).strip()


def get_cross_rerank_window() -> int:
    value = int(os.environ.get("MEMORY_CROSS_RERANK_WINDOW", str(DEFAULT_CROSS_RERANK_WINDOW)))
    if value < 2:
        raise ValueError("MEMORY_CROSS_RERANK_WINDOW must be at least 2")
    return value


def get_cross_rerank_weight() -> float:
    value = float(os.environ.get("MEMORY_CROSS_RERANK_WEIGHT", str(DEFAULT_CROSS_RERANK_WEIGHT)))
    if value <= 0:
        raise ValueError("MEMORY_CROSS_RERANK_WEIGHT must be positive")
    return value


def get_cross_rerank_context() -> int:
    """MEMORY_CROSS_RERANK_CONTEXT: characters of each neighbouring turn read with a candidate (0 = off)."""
    value = int(os.environ.get("MEMORY_CROSS_RERANK_CONTEXT", str(DEFAULT_CROSS_RERANK_CONTEXT)))
    if value < 0:
        raise ValueError("MEMORY_CROSS_RERANK_CONTEXT must be zero or positive")
    return value


def is_rerank_enabled() -> bool:
    raw = (os.environ.get("MEMORY_RERANK_ENABLED", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_enrichment_enabled() -> bool:
    raw = (os.environ.get("MEMORY_ENRICHMENT_ENABLED", "false") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────
# Per-space embedding model env vars (v11 §J multi-embedding-space)
# ──────────────────────────────────────────────
#
# When a per-space model env var is empty, the row falls back to the TEXT
# model but still records `embedding_space=<space>` so a future model swap
# is a one-line config change, not an architecture migration.

# v11.0 §J defaults: text + code get real per-space models out of the box;
# log + config inherit the text model (still tagged with their own space).
# The user can override any of these to a different FastEmbed model, an ST
# model, or empty (= force fallback to the text model).
_DEFAULT_TEXT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # 384d, 0.22GB
_DEFAULT_CODE_MODEL = "jinaai/jina-embeddings-v2-base-code"                          # 768d, 0.64GB


def get_text_embed_model() -> str:
    return (os.environ.get("MEMORY_TEXT_EMBED_MODEL") or _DEFAULT_TEXT_MODEL).strip()


def get_code_embed_model() -> str:
    """Default = jinaai/jina-embeddings-v2-base-code (FastEmbed-supported,
    code-aware, 768d). Set to empty to force fallback to the text model
    (the row still records `embedding_space=code`)."""
    raw = os.environ.get("MEMORY_CODE_EMBED_MODEL")
    if raw is None:
        return _DEFAULT_CODE_MODEL
    return raw.strip()  # empty string → caller falls back to text model


def get_log_embed_model() -> str:
    """Default empty → falls back to the text model (logs/stacktraces are
    prose-shaped enough that a code embedder is the wrong tool)."""
    return (os.environ.get("MEMORY_LOG_EMBED_MODEL", "") or "").strip()


def get_config_embed_model() -> str:
    """Default empty → falls back to the text model. Override to a code
    model if you save large amounts of SQL / YAML / JSON snippets."""
    return (os.environ.get("MEMORY_CONFIG_EMBED_MODEL", "") or "").strip()


def get_default_embedding_space() -> str:
    raw = (os.environ.get("MEMORY_DEFAULT_EMBEDDING_SPACE", "text") or "text").strip().lower()
    return raw if raw in ("text", "code", "log", "config") else "text"


# ──────────────────────────────────────────────
# Mode → derived defaults
# ──────────────────────────────────────────────


_MODE_DEFAULTS: dict[str, dict[str, str]] = {
    # Hot path = 0 LLM. Embeddings on save are cached or skipped.
    "ultrafast": {
        "MEMORY_QUALITY_GATE_ENABLED": "false",
        "MEMORY_CONTRADICTION_DETECT_ENABLED": "false",
        "MEMORY_ENTITY_DEDUP_ENABLED": "false",
        "MEMORY_COREF_ENABLED": "false",
        "USE_ADVANCED_RAG": "false",
        "MEMORY_QUERY_REWRITE": "0",
        "MEMORY_RERANK_ENABLED": "false",
        "MEMORY_CROSS_ENCODER_ENABLED": "false",
        "MEMORY_USE_LLM_IN_HOT_PATH": "false",
        "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH": "false",
        "MEMORY_ENRICHMENT_ENABLED": "false",
        "MEMORY_ASYNC_ENRICHMENT": "true",
    },
    # Hot path = 0 LLM. FastEmbed only. Async enrichment off by default
    # (user opts in with MEMORY_ENRICHMENT_ENABLED=true to drain queues).
    "fast": {
        "MEMORY_QUALITY_GATE_ENABLED": "false",
        "MEMORY_CONTRADICTION_DETECT_ENABLED": "false",
        "MEMORY_ENTITY_DEDUP_ENABLED": "false",
        "MEMORY_COREF_ENABLED": "false",
        "USE_ADVANCED_RAG": "false",
        "MEMORY_QUERY_REWRITE": "0",
        "MEMORY_RERANK_ENABLED": "false",
        "MEMORY_CROSS_ENCODER_ENABLED": "false",
        "MEMORY_USE_LLM_IN_HOT_PATH": "false",
        "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH": "false",
        "MEMORY_ENRICHMENT_ENABLED": "false",
        "MEMORY_ASYNC_ENRICHMENT": "true",
    },
    # Same fast hot path, but async enrichment worker is on by default.
    "balanced": {
        "MEMORY_QUALITY_GATE_ENABLED": "false",
        "MEMORY_CONTRADICTION_DETECT_ENABLED": "false",
        "MEMORY_ENTITY_DEDUP_ENABLED": "false",
        "MEMORY_COREF_ENABLED": "false",
        "USE_ADVANCED_RAG": "false",
        "MEMORY_QUERY_REWRITE": "0",
        "MEMORY_RERANK_ENABLED": "false",
        "MEMORY_CROSS_ENCODER_ENABLED": "false",
        "MEMORY_USE_LLM_IN_HOT_PATH": "false",
        "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH": "true",
        "MEMORY_ENRICHMENT_ENABLED": "true",
        "MEMORY_ASYNC_ENRICHMENT": "true",
    },
    # Legacy v10.5 behaviour: sync quality gate / contradiction / advanced
    # RAG / reranker. Slow but deepest semantic enrichment.
    "deep": {
        "MEMORY_QUALITY_GATE_ENABLED": "auto",
        "MEMORY_CONTRADICTION_DETECT_ENABLED": "auto",
        "MEMORY_ENTITY_DEDUP_ENABLED": "auto",
        "MEMORY_COREF_ENABLED": "false",  # opt-in even in deep
        "USE_ADVANCED_RAG": "auto",
        "MEMORY_QUERY_REWRITE": "0",       # explicit opt-in (Anthropic API cost)
        "MEMORY_RERANK_ENABLED": "true",
        "MEMORY_CROSS_ENCODER_ENABLED": "true",
        "MEMORY_USE_LLM_IN_HOT_PATH": "true",
        "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH": "true",
        "MEMORY_ENRICHMENT_ENABLED": "true",
        "MEMORY_ASYNC_ENRICHMENT": "false",
    },
}


_mode_resolved = False


def resolve_mode_defaults(force: bool = False) -> str:
    """Apply MEMORY_MODE → derived env-var defaults. Idempotent.

    Each derived value uses `setdefault`, so any flag the user already
    exported wins. Call once at server startup BEFORE any module reads
    `USE_ADVANCED_RAG` or similar at import time.

    Returns the resolved mode name.
    """
    global _mode_resolved
    if _mode_resolved and not force:
        return get_memory_mode()
    mode = get_memory_mode()
    for key, value in _MODE_DEFAULTS[mode].items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("MEMORY_MODE_RESOLVED", mode)
    _mode_resolved = True
    return mode


def reset_mode_resolution() -> None:
    """Test helper — clears the once-only guard so a new env can be applied."""
    global _mode_resolved
    _mode_resolved = False
    os.environ.pop("MEMORY_MODE_RESOLVED", None)


# ──────────────────────────────────────────────
# Recall tier weights (14.8.0)
# ──────────────────────────────────────────────
#
# `memory_recall` fuses the ranked lists of its retrieval tiers with weighted
# Reciprocal Rank Fusion. The weights used to be constants inside the server;
# they are now read from ``MEMORY_RECALL_TIER_WEIGHTS`` so an installation (or
# an ablation run) can retune or switch a tier off without a code change.
#
#   MEMORY_RECALL_TIER_WEIGHTS=fts=1.0,semantic=1.2,fuzzy=0,graph=0.8
#
# Tiers that are not named keep their default. A weight of 0 switches the tier
# off entirely: it is not executed, so it costs no time and adds no candidates.

RECALL_TIERS: tuple[str, ...] = (
    "fts", "semantic", "hyde", "multi_repr", "fuzzy", "graph", "episode", "atomic_facts",
    "directives", "multi_query",
)

DEFAULT_RECALL_TIER_WEIGHTS: dict[str, float] = {
    "fts": 1.0,
    "semantic": 1.2,
    "hyde": 1.0,
    "multi_repr": 1.0,
    "fuzzy": 0.5,
    # Graph candidates are one hop from the seeds, never evidence on their own.
    "graph": 0.3,
    "episode": 0.9,
    # Atomic facts only count when no other tier found anything; see Recall.
    "atomic_facts": 1.0,
    # Conventions and preferences of the project for advice-shaped queries.
    "directives": 1.0,
    # Each side of an ordering / comparison question searched on its own.
    # Off by default: neutral on LoCoMo and LongMemEval-S held-out splits
    # (docs/benchmarks/tier-ablation-v14), costs ~20 ms when it fires.
    "multi_query": 0.0,
}

_tier_weights_cache: dict[str, dict[str, float]] = {}


def parse_tier_weights(raw: str | None) -> dict[str, float]:
    """Parse ``tier=weight,...`` into a full weight table over RECALL_TIERS.

    Unknown tiers, non-numeric or negative weights raise ValueError so a typo
    in the environment is reported instead of silently ignored.
    """
    weights = dict(DEFAULT_RECALL_TIER_WEIGHTS)
    if raw is None or not raw.strip():
        return weights
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, value = part.partition("=")
        name = name.strip().lower()
        if not sep or name not in RECALL_TIERS:
            raise ValueError(
                f"MEMORY_RECALL_TIER_WEIGHTS: unknown tier {name!r}; known tiers: {', '.join(RECALL_TIERS)}"
            )
        try:
            weight = float(value.strip())
        except ValueError as error:
            raise ValueError(f"MEMORY_RECALL_TIER_WEIGHTS: {name} needs a number, got {value.strip()!r}") from error
        if weight < 0 or math.isnan(weight) or math.isinf(weight):
            raise ValueError(f"MEMORY_RECALL_TIER_WEIGHTS: {name} must be a finite number >= 0, got {weight}")
        weights[name] = weight
    return weights


def get_recall_tier_weights() -> dict[str, float]:
    """Tier weights from the environment; a malformed value is logged and ignored.

    The lookup is cached per raw string so the hot recall path parses nothing.
    """
    raw = os.environ.get("MEMORY_RECALL_TIER_WEIGHTS", "")
    cached = _tier_weights_cache.get(raw)
    if cached is None:
        try:
            cached = parse_tier_weights(raw)
        except ValueError as error:
            import logging
            logging.getLogger(__name__).warning(
                "ignoring MEMORY_RECALL_TIER_WEIGHTS, using defaults", extra={"error": str(error)}
            )
            cached = dict(DEFAULT_RECALL_TIER_WEIGHTS)
        _tier_weights_cache[raw] = cached
    return dict(cached)


def tier_weight_signature(weights: dict[str, float]) -> str | None:
    """Compact cache-key fragment; None when the weights are the defaults."""
    if all(float(weights.get(name, DEFAULT_RECALL_TIER_WEIGHTS[name])) == DEFAULT_RECALL_TIER_WEIGHTS[name]
           for name in RECALL_TIERS):
        return None
    return ",".join(f"{name}={float(weights.get(name, DEFAULT_RECALL_TIER_WEIGHTS[name])):g}" for name in RECALL_TIERS)


# ──────────────────────────────────────────────
# Graph tier hub cut-off (14.8.0)
# ──────────────────────────────────────────────

DEFAULT_GRAPH_HUB_DEGREE = 200


def get_graph_hub_degree() -> int:
    """Nodes linked to more records than this are ignored by the recall graph tier.

    MEMORY_GRAPH_HUB_DEGREE; a non-positive or malformed value falls back to the default.
    """
    raw = os.environ.get("MEMORY_GRAPH_HUB_DEGREE", "").strip()
    if not raw:
        return DEFAULT_GRAPH_HUB_DEGREE
    try:
        value = int(raw)
    except ValueError:
        import logging
        logging.getLogger(__name__).warning("ignoring MEMORY_GRAPH_HUB_DEGREE, using default",
                                            extra={"value": raw})
        return DEFAULT_GRAPH_HUB_DEGREE
    return value if value > 0 else DEFAULT_GRAPH_HUB_DEGREE


# ──────────────────────────────────────────────
# LLM provider fallback chain (14.8.0)
# ──────────────────────────────────────────────
#
# Enrichment (triples, representations, deep enrichment, session compression,
# contradiction checks) used to stop silently when the configured provider,
# usually a local Ollama, was down: queue rows stayed pending and nothing said
# so. With
#
#   MEMORY_LLM_FALLBACK_PROVIDERS=anthropic,openai
#
# every phase resolves its provider through a chain: the configured provider
# first, then the fallbacks in order; the first one whose availability probe
# passes is used. Probes are the same cached ones has_llm() uses, so the hot
# path pays nothing extra. Fallback use is capped per process with
# MEMORY_LLM_FALLBACK_MAX_CALLS (resolutions that landed on a fallback; each
# enrichment call resolves once). llm_health() reports the chain, which
# providers answer, the active one and the pending queue rows, and
# memory_stats / the dashboard health endpoint include it.

DEFAULT_LLM_FALLBACK_MAX_CALLS = 500

_fallback_calls = 0


def get_llm_fallback_providers() -> tuple[str, ...]:
    """Providers named in MEMORY_LLM_FALLBACK_PROVIDERS, in order, unknown names dropped with a log line."""
    raw = os.environ.get("MEMORY_LLM_FALLBACK_PROVIDERS", "")
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name or name in names:
            continue
        if name == "auto" or name not in _SUPPORTED_LLM_PROVIDERS:
            import logging
            logging.getLogger(__name__).warning(
                "ignoring unknown entry in MEMORY_LLM_FALLBACK_PROVIDERS", extra={"provider": name})
            continue
        names.append(name)
    return tuple(names)


def get_llm_fallback_max_calls() -> int:
    raw = os.environ.get("MEMORY_LLM_FALLBACK_MAX_CALLS", "").strip()
    if not raw:
        return DEFAULT_LLM_FALLBACK_MAX_CALLS
    try:
        value = int(raw)
    except ValueError:
        import logging
        logging.getLogger(__name__).warning("ignoring MEMORY_LLM_FALLBACK_MAX_CALLS, using default",
                                            extra={"value": raw})
        return DEFAULT_LLM_FALLBACK_MAX_CALLS
    return max(0, value)


def get_llm_provider_chain(phase: str | None = None) -> list[str]:
    """The configured provider for `phase` (or the global one) followed by the fallbacks."""
    primary = _configured_phase_provider(phase) if phase else _configured_llm_provider()
    return [primary, *[name for name in get_llm_fallback_providers() if name != primary]]


def provider_is_available(name: str) -> bool:
    """Cached availability probe for one provider name; a failing probe is False, never an error."""
    if name == "ollama":
        return detect_ollama() and has_model(get_llm_model_for_provider("ollama"))
    try:
        from llm_provider import make_provider
        return bool(make_provider(name).available())
    except Exception as error:  # noqa: BLE001 — a probe must not raise into the caller
        import logging
        logging.getLogger(__name__).debug("provider probe failed", extra={"provider": name, "error": str(error)})
        return False


def resolve_llm_provider(phase: str | None = None) -> str:
    """First available provider of the chain; the configured one when nothing answers or no chain is set."""
    chain = get_llm_provider_chain(phase)
    if len(chain) == 1:
        return chain[0]
    global _fallback_calls
    for name in chain:
        if not provider_is_available(name):
            continue
        if name == chain[0]:
            return name
        if _fallback_calls >= get_llm_fallback_max_calls():
            import logging
            logging.getLogger(__name__).warning(
                "LLM fallback budget exhausted; staying on the configured provider",
                extra={"phase": phase or "global", "configured": chain[0], "fallback": name,
                       "calls": _fallback_calls})
            return chain[0]
        _fallback_calls += 1
        return name
    return chain[0]


def reset_llm_fallback_budget() -> None:
    """Test helper and per-run reset for long-lived processes."""
    global _fallback_calls
    _fallback_calls = 0


def llm_health(phase: str | None = None) -> dict:
    """Operator view of the LLM chain for `phase`: who is configured, who answers, who is active."""
    chain = get_llm_provider_chain(phase)
    mode = get_llm_mode()
    available = {name: provider_is_available(name) for name in chain} if mode != "false" else {}
    active = None
    if mode == "false":
        active = None
    elif mode in ("true", "force"):
        active = chain[0]
    else:
        active = next((name for name in chain if available.get(name)), None)
    return {
        "phase": phase or "global",
        "mode": mode,
        "configured": chain[0],
        "chain": chain,
        "available": available,
        "active": active,
        "fallback_calls": _fallback_calls,
        "fallback_max_calls": get_llm_fallback_max_calls(),
    }


# ──────────────────────────────────────────────
# User-turn boost for advice queries (14.8.0)
# ──────────────────────────────────────────────
#
# "Recommend me X" is answered by what the user said about X earlier, not by
# what the assistant replied; the user's turn rarely shares words with the
# request. For advice-shaped queries the fused score of records that are user
# turns ("[date] user: ...") is multiplied by this factor. 1.0 switches it off.

DEFAULT_USER_TURN_BOOST = 1.3


def get_user_turn_boost() -> float:
    raw = os.environ.get("MEMORY_RECALL_USER_TURN_BOOST", "").strip()
    if not raw:
        return DEFAULT_USER_TURN_BOOST
    try:
        value = float(raw)
    except ValueError:
        import logging
        logging.getLogger(__name__).warning("ignoring MEMORY_RECALL_USER_TURN_BOOST, using default",
                                            extra={"value": raw})
        return DEFAULT_USER_TURN_BOOST
    return value if value > 0 else DEFAULT_USER_TURN_BOOST
