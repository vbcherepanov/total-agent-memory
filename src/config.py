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
    MEMORY_EMBED_PROVIDER — fastembed|openai|cohere (default fastembed)
    MEMORY_EMBED_MODEL    — embedding model name (provider-specific default)
    MEMORY_EMBED_API_BASE — override embedding provider base URL
    MEMORY_EMBED_API_KEY  — key for embedding provider
    MEMORY_{TRIPLE|ENRICH|REPR}_PROVIDER — per-phase provider override
    MEMORY_{TRIPLE|ENRICH|REPR}_MODEL    — per-phase model override
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


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
#   EMBED: fastembed | openai | cohere


_SUPPORTED_LLM_PROVIDERS = ("ollama", "openai", "anthropic", "auto")
_SUPPORTED_EMBED_PROVIDERS = ("fastembed", "openai", "cohere")
_SUPPORTED_PHASES = ("triple", "enrich", "repr")

# Default model name per provider when MEMORY_LLM_MODEL isn't set.
_DEFAULT_LLM_MODEL_BY_PROVIDER = {
    "ollama": "qwen2.5-coder:7b",
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
}

_DEFAULT_EMBED_API_BASE_BY_PROVIDER = {
    "fastembed": "",  # local, no HTTP
    "openai": "https://api.openai.com/v1",
    "cohere": "https://api.cohere.com/v2",
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
    "fastembed": (),  # local
}


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


def get_llm_provider() -> str:
    """Canonical LLM provider name.

    `auto` resolves by probing well-known API-key env vars.
    """
    raw = _normalize_provider(
        os.environ.get("MEMORY_LLM_PROVIDER", "ollama"),
        _SUPPORTED_LLM_PROVIDERS,
        default="ollama",
    )
    if raw == "auto":
        return _auto_resolve_llm_provider()
    return raw


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
    return _DEFAULT_EMBED_MODEL_BY_PROVIDER.get(p, "")


def _normalize_phase(phase: str) -> str:
    p = (phase or "").strip().lower()
    if p not in _SUPPORTED_PHASES:
        raise ValueError(
            f"unsupported phase {phase!r}; expected one of {_SUPPORTED_PHASES}"
        )
    return p


def get_phase_provider(phase: str) -> str:
    """Per-phase provider override. Falls back to the global LLM provider."""
    p = _normalize_phase(phase)
    env_name = f"MEMORY_{p.upper()}_PROVIDER"
    raw = os.environ.get(env_name)
    if not raw:
        return get_llm_provider()
    normalized = _normalize_provider(raw, _SUPPORTED_LLM_PROVIDERS, default=get_llm_provider())
    if normalized == "auto":
        return _auto_resolve_llm_provider()
    return normalized


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
