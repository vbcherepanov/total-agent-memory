"""LLM-generated representations: summary, keywords, utility questions.

Each knowledge record can have several "views":
- summary   — short abstract of the content
- keywords  — salient terms/entities
- questions — questions the record answers (GEM-RAG utility questions)

Matching against any of these (fused with RRF) boosts retrieval recall.
Generation happens async/offline to avoid slowing memory_save; this module
only exposes pure functions. Ollama is the default LLM.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

import os as _os

import config
from config import get_repr_timeout_sec

OLLAMA_URL = _os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Default picks the first available locally-installed qwen/vitalii model.
OLLAMA_MODEL = _os.environ.get("MEMORY_LLM_MODEL", "qwen2.5-coder:7b")
# Skip summary for very short content (not worth the LLM hop)
MIN_TOKENS_FOR_SUMMARY = 50
# Compression only kicks in for large content — below this we keep raw
MIN_CHARS_FOR_COMPRESSION = 1500
# Hard cap on content we send to the LLM (avoid runaway tokens)
MAX_LLM_INPUT_CHARS = 8000

LOG = lambda msg: sys.stderr.write(f"[memory-representations] {msg}\n")

# Provider cache — keyed by phase. Rebuilt only on process restart.
_provider_cache: dict[str, Any] = {}


def _get_phase_provider(phase: str):
    """Resolve LLMProvider for the representations phase, caching on first use."""
    cached = _provider_cache.get(phase)
    if cached is not None:
        return cached
    from llm_provider import make_provider
    provider = make_provider(config.get_phase_provider(phase))
    _provider_cache[phase] = provider
    return provider


# ──────────────────────────────────────────────
# LLM adapter (override in tests via monkeypatch)
# ──────────────────────────────────────────────


def _llm_complete(
    prompt: str, model: str = OLLAMA_MODEL, num_predict: int = 200
) -> str:
    """Run representation completion through the configured LLM provider.

    Default phase provider is Ollama (original behavior preserved inline so
    `representations.urllib.request.urlopen` monkeypatches keep working).
    When MEMORY_REPR_PROVIDER / MEMORY_LLM_PROVIDER picks a cloud backend,
    requests route via `llm_provider`. Network errors propagate; callers
    already wrap this in try/except.
    """
    phase_provider = config.get_phase_provider("repr")

    if phase_provider != "ollama":
        provider = _get_phase_provider("repr")
        if not provider.available():
            raise RuntimeError(
                f"LLM provider '{getattr(provider, 'name', '?')}' unavailable"
            )
        return provider.complete(
            prompt,
            model=config.get_phase_model("repr"),
            max_tokens=num_predict,
            temperature=0.2,
            timeout=get_repr_timeout_sec(),
        )

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0.2},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=get_repr_timeout_sec()) as resp:
        data = json.loads(resp.read())
    return str(data.get("response", "")).strip()


# ──────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────


_SUMMARY_PROMPT = """Summarize the following text in ONE sentence (max 25 words).
Keep key entities, technologies, and project names. Output the summary only, no preamble.

TEXT:
{content}

SUMMARY:"""


_KEYWORDS_PROMPT = """Extract 5-10 salient keywords or short phrases from the text.
Focus on: technologies, entities, actions, domain concepts. Comma-separated, lowercase.
Output the list only.

TEXT:
{content}

KEYWORDS:"""


_QUESTIONS_PROMPT = """Generate 3 concrete questions this text answers.
Each question should be specific and self-contained (no "this" or "it" pronouns).
Output one question per line, no numbering.

TEXT:
{content}

QUESTIONS:"""


_COMPRESSED_PROMPT = """Rewrite the text to be as short as possible while preserving:
- EVERY code block, URL, and file path EXACTLY (byte-for-byte)
- every inline `code` token
- the same number of headings (# ## ###)
- at least 85% of the bullet points
- all facts, numbers, names, dates

Remove only filler words, repetitions, transitions, and redundant explanations.
Output the compressed text only, no preamble.

TEXT:
{content}

COMPRESSED:"""


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_LLM_INPUT_CHARS else text[:MAX_LLM_INPUT_CHARS]


def generate_representations(
    content: str,
    project: str | None = None,
    skip: set[str] | None = None,
) -> dict[str, str]:
    """Generate summary/keywords/questions/compressed for a piece of content.

    Returns a dict with keys "summary", "keywords", "questions", "compressed".
    Values are strings (possibly empty on short content, missing LLM, or
    LLM failure — the caller can decide to embed or skip).

    `skip` lets callers disable specific representations (e.g. skip=["summary"]).
    Gracefully no-ops if Ollama / configured model is unavailable.
    """
    skip = skip or set()
    content = content or ""
    truncated = _truncate(content)
    tokens = _estimate_tokens(content)

    out: dict[str, str] = {"summary": "", "keywords": "", "questions": "", "compressed": ""}

    # Skip everything if no LLM available (degraded mode)
    try:
        from config import has_llm
        if not has_llm("repr"):
            return out
    except Exception:
        pass  # config module not importable — proceed and let LLM calls fail-soft

    # Summary — skip on very short inputs
    if "summary" not in skip and tokens >= MIN_TOKENS_FOR_SUMMARY:
        try:
            out["summary"] = _llm_complete(
                _SUMMARY_PROMPT.format(content=truncated), num_predict=80
            )
        except Exception as e:  # network / ollama errors — swallow
            LOG(f"summary generation failed: {e}")

    # Keywords
    if "keywords" not in skip:
        try:
            out["keywords"] = _llm_complete(
                _KEYWORDS_PROMPT.format(content=truncated), num_predict=120
            )
        except Exception as e:
            LOG(f"keywords generation failed: {e}")

    # Utility questions
    if "questions" not in skip:
        try:
            out["questions"] = _llm_complete(
                _QUESTIONS_PROMPT.format(content=truncated), num_predict=200
            )
        except Exception as e:
            LOG(f"questions generation failed: {e}")

    # Compressed — only for sufficiently long content; validator-guarded at
    # the queue worker level (representations_queue drops invalid output).
    if "compressed" not in skip and len(content) >= MIN_CHARS_FOR_COMPRESSION:
        try:
            out["compressed"] = _llm_complete(
                _COMPRESSED_PROMPT.format(content=truncated),
                num_predict=max(400, len(content) // 8),
            )
        except Exception as e:
            LOG(f"compressed generation failed: {e}")

    return out
