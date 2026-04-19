"""Deep (LLM-based) metadata enricher.

Complements `ingestion.enricher.MetadataEnricher` (fast heuristics) with
LLM-extracted semantic metadata:
  - entities: [{"name", "type"}, ...]  — Go/PostgreSQL/Bob/ImPatient
  - intent:   str                       — question | procedural | fact | decision | ...
  - topics:   list[str]                 — high-level themes

Stored alongside base metadata; used for filtering at retrieval time
(`memory_recall(topics=["auth"])` etc.).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

import os as _os

import config
from config import get_enrich_timeout_sec

OLLAMA_URL = _os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = _os.environ.get("MEMORY_LLM_MODEL", "qwen2.5-coder:7b")
MIN_CHARS_FOR_LLM = 120  # below this, skip LLM calls
MAX_LLM_INPUT_CHARS = 6000

LOG = lambda msg: sys.stderr.write(f"[deep-enricher] {msg}\n")

# Provider cache — keyed by phase. Rebuilt only on process restart.
_provider_cache: dict[str, Any] = {}


def _get_phase_provider(phase: str):
    """Resolve LLMProvider for the enrichment phase, caching on first use."""
    cached = _provider_cache.get(phase)
    if cached is not None:
        return cached
    from llm_provider import make_provider
    provider = make_provider(config.get_phase_provider(phase))
    _provider_cache[phase] = provider
    return provider


# ──────────────────────────────────────────────
# LLM adapter (monkeypatched in tests)
# ──────────────────────────────────────────────


def _llm_complete(
    prompt: str, model: str = OLLAMA_MODEL, num_predict: int = 200
) -> str:
    """Run enrichment completion through the configured LLM provider.

    Default phase provider is Ollama; non-Ollama phases route through the
    `llm_provider` abstraction (OpenAI / Anthropic / compatible endpoints).
    On `ollama` we keep the inlined urllib path so existing test
    monkeypatches on `deep_enricher.urllib.request.urlopen` still work.
    """
    phase_provider = config.get_phase_provider("enrich")

    if phase_provider != "ollama":
        provider = _get_phase_provider("enrich")
        if not provider.available():
            raise RuntimeError(
                f"LLM provider '{getattr(provider, 'name', '?')}' unavailable"
            )
        return provider.complete(
            prompt,
            model=config.get_phase_model("enrich"),
            max_tokens=num_predict,
            temperature=0.1,
            timeout=get_enrich_timeout_sec(),
        )

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0.1},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=get_enrich_timeout_sec()) as resp:
        data = json.loads(resp.read())
    return str(data.get("response", "")).strip()


# ──────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────


_ENTITIES_PROMPT = """Extract named entities from the text. For each: name and type.
Valid types: technology, person, project, company, product, location.
Return strict JSON: {{"entities": [{{"name": "...", "type": "..."}}]}}

TEXT:
{content}

JSON:"""


_INTENT_PROMPT = """Classify the intent of this text in ONE word (snake_case):
question, procedural, fact, decision, problem, solution, incident, plan.
Reply with only the intent word.

TEXT:
{content}

INTENT:"""


_TOPICS_PROMPT = """List 3-5 high-level topics covered by the text.
Return a JSON array of short lowercase strings (1-3 words each).

TEXT:
{content}

JSON:"""


# ──────────────────────────────────────────────
# Parsers (pure, testable)
# ──────────────────────────────────────────────


_JSON_IN_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _find_json(text: str) -> str | None:
    """Extract the first JSON object/array from a possibly markdown-fenced response."""
    if not text:
        return None
    m = _JSON_IN_FENCE.search(text)
    if m:
        return m.group(1).strip()
    # Otherwise look for the first { ... } or [ ... ] span
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = text.find(open_ch)
        j = text.rfind(close_ch)
        if 0 <= i < j:
            return text[i : j + 1]
    return None


def parse_entities(raw: str) -> list[dict[str, str]]:
    snippet = _find_json(raw or "")
    if not snippet:
        return []
    try:
        data = json.loads(snippet)
    except (json.JSONDecodeError, TypeError):
        return []
    candidate = data.get("entities") if isinstance(data, dict) else data
    if not isinstance(candidate, list):
        return []
    out: list[dict[str, str]] = []
    for item in candidate:
        if isinstance(item, dict) and item.get("name"):
            out.append(
                {
                    "name": str(item["name"]).strip(),
                    "type": str(item.get("type", "concept")).strip(),
                }
            )
    return out


def parse_intent(raw: str | None) -> str:
    if not raw:
        return "unknown"
    s = raw.strip().strip('."\'')
    # Maybe JSON: {"intent": "..."}
    snippet = _find_json(s)
    if snippet:
        try:
            data = json.loads(snippet)
            if isinstance(data, dict) and "intent" in data:
                return str(data["intent"]).strip().lower().replace(" ", "_") or "unknown"
        except (json.JSONDecodeError, TypeError):
            pass
    # Maybe "intent: xyz"
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    # First token, lowercased
    token = s.split()[0] if s.split() else ""
    token = token.strip('."\'` ').lower().replace("-", "_")
    return token or "unknown"


def parse_topics(raw: str, max_topics: int = 10) -> list[str]:
    if not raw:
        return []
    snippet = _find_json(raw)
    values: list[str] = []
    if snippet:
        try:
            data = json.loads(snippet)
            if isinstance(data, list):
                values = [str(x) for x in data]
        except (json.JSONDecodeError, TypeError):
            pass
    if not values:
        # Fallback: comma-separated string
        values = [p.strip() for p in raw.split(",")]
    cleaned: list[str] = []
    for v in values:
        v2 = v.strip(" `'\"[]()").lower()
        if v2 and v2 not in cleaned:
            cleaned.append(v2)
    return cleaned[:max_topics]


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_LLM_INPUT_CHARS else text[:MAX_LLM_INPUT_CHARS]


def deep_enrich(
    content: str,
    base_metadata: dict[str, Any] | None = None,
    skip: set[str] | None = None,
) -> dict[str, Any]:
    """Enrich metadata with LLM-extracted entities/intent/topics.

    Short content (< MIN_CHARS_FOR_LLM) gets stub enrichment values (no LLM
    calls). LLM failures are swallowed so the caller always gets a dict.

    `skip` lets callers disable specific keys, e.g. skip={"intent"}.
    """
    skip = skip or set()
    out: dict[str, Any] = dict(base_metadata or {})

    # Always provide the keys so downstream filtering doesn't KeyError
    out.setdefault("entities", [])
    out.setdefault("intent", "unknown")
    out.setdefault("topics", [])

    content = content or ""
    if len(content) < MIN_CHARS_FOR_LLM:
        return out

    # Degrade gracefully when Ollama / model unavailable
    try:
        from config import has_llm
        if not has_llm("enrich"):
            return out
    except Exception:
        pass

    truncated = _truncate(content)

    if "entities" not in skip:
        try:
            resp = _llm_complete(_ENTITIES_PROMPT.format(content=truncated), num_predict=250)
            out["entities"] = parse_entities(resp)
        except Exception as e:  # noqa: BLE001
            LOG(f"entities LLM failed: {e}")

    if "intent" not in skip:
        try:
            resp = _llm_complete(_INTENT_PROMPT.format(content=truncated), num_predict=30)
            out["intent"] = parse_intent(resp)
        except Exception as e:  # noqa: BLE001
            LOG(f"intent LLM failed: {e}")

    if "topics" not in skip:
        try:
            resp = _llm_complete(_TOPICS_PROMPT.format(content=truncated), num_predict=100)
            out["topics"] = parse_topics(resp)
        except Exception as e:  # noqa: BLE001
            LOG(f"topics LLM failed: {e}")

    return out
