"""
Advanced RAG: HyDE + Pluggable Reranker + MMR Diversity.

- HyDE: generates hypothetical answer, embeds it for semantic search.
- Reranker (v9 D4): pluggable backend selected via V9_RERANKER_BACKEND.
    * ce-marco   (default, legacy) cross-encoder/ms-marco-MiniLM-L-6-v2 via
                 sentence-transformers.CrossEncoder. Web-search trained —
                 known to regress on LoCoMo conversational data (-1.2pp).
    * bge-v2-m3  BAAI/bge-reranker-v2-m3 via FlagEmbedding.FlagReranker.
                 Multilingual, conversation-friendly. Recommended for v9.
    * bge-large  BAAI/bge-reranker-large via FlagReranker. English-only,
                 higher accuracy on long context, slower.
    * off        skip reranking entirely.
  All backends return scores normalized to [0,1] via sigmoid; downstream
  blending logic is identical (CE-boost-only: never demote originals).
  Falls back to Ollama LLM reranker if the configured backend fails to load.
- MMR: Maximal Marginal Relevance for result diversity (λ=0.7).
"""

import json
import os
import sys
import urllib.request
from typing import Optional

import numpy as np

import config
from llm_provider import make_provider
from memory_core.evidence_excerpt import excerpt
from memory_core.retrieval import SearchScope

MODEL_RERANK_EXCERPT_CHARS = 1024
LLM_RERANK_EXCERPT_CHARS = 1024

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Use smaller model for speed — 7b is ~3x faster than 32b for reranking
RERANKER_MODEL = os.environ.get("RERANKER_MODEL") or config.get_llm_model()
HYDE_MODEL = os.environ.get("HYDE_MODEL") or config.get_llm_model()
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")

LOG = lambda msg: sys.stderr.write(f"[reranker] {msg}\n")


def _ollama_generate(prompt: str, model: str, max_tokens: int = 200, temperature: float = 0.3) -> Optional[str]:
    """Generate through the configured provider, preserving legacy Ollama model overrides."""
    if not config.has_llm():
        return None
    try:
        name = config.get_llm_provider()
        return make_provider(name).complete(
            prompt, model=model if name == "ollama" else config.get_llm_model_for_provider(name),
            max_tokens=max_tokens, temperature=temperature, timeout=config.get_llm_timeout_sec(),
        )
    except Exception as e:
        LOG(f"generate error ({model}): {e}")
        return None


def _ollama_embed(text: str, model: str = None) -> Optional[list]:
    """Get embedding via Ollama. Retained for backward compat; new code
    should use _provider_embed() which dispatches to the configured
    EmbeddingProvider (fastembed|openai|cohere)."""
    model = model or OLLAMA_EMBED_MODEL
    try:
        payload = json.dumps({"model": model, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("embedding")
    except Exception as e:
        LOG(f"embed error: {e}")
        return None


# Module-level cache for the configured provider. Rebuilt on first call;
# tests that mutate MEMORY_EMBED_PROVIDER between invocations should call
# _reset_embed_provider() (e.g. via monkeypatch with raising=False).
_embed_provider_cache = None


def _reset_embed_provider() -> None:
    """Clear the cached embed provider — used by tests after env changes."""
    global _embed_provider_cache
    _embed_provider_cache = None


def _provider_embed(text: str) -> Optional[list]:
    """Embed a single text via the configured EmbeddingProvider.

    Returns None when the provider init or call fails: a vector from another
    model (the old Ollama fallback) would be compared with stored vectors it
    has nothing in common with.
    """
    global _embed_provider_cache
    try:
        if _embed_provider_cache is None:
            import config as _cfg
            from embed_provider import make_embed_provider
            _embed_provider_cache = make_embed_provider(_cfg.get_embed_provider())
        vecs = _embed_provider_cache.embed([text])
        if vecs:
            return list(vecs[0])
    except Exception as e:  # noqa: BLE001
        LOG(f"provider embed error: {e}")
    return None


# =============================================================================
# HyDE — Hypothetical Document Embeddings
# =============================================================================

def hyde_expand(query: str, project: str = None, embed_fn=None) -> Optional[list]:
    """
    Generate a hypothetical answer to the query, then embed it.
    Returns the embedding of the hypothetical answer (not the answer itself).

    `embed_fn(texts) -> vectors | None` embeds with the model the store searches
    with. Without it the configured EmbeddingProvider is used, which can name a
    different model than the stored vectors (callers that search must pass it).

    Why: "auth JWT" as query might not match "Используем Bearer токен с RS256 подписью".
         But a hypothetical answer about auth WILL be semantically close to that record.

    Returns None if no LLM is configured — caller falls back to plain semantic search.
    """
    try:
        from config import has_llm
        if not has_llm():
            return None
    except Exception:
        pass

    context = f" in project {project}" if project else ""
    prompt = f"""You are a developer knowledge base. Write a short factual answer (2-3 sentences) to this query{context}.
If you don't know, write a plausible answer based on common patterns.

Query: {query}

Answer:"""

    hypothetical = _ollama_generate(prompt, HYDE_MODEL, max_tokens=150, temperature=0.5)
    if not hypothetical:
        return None

    # Embed the hypothetical answer (not the original query)
    if embed_fn is not None:
        vectors = embed_fn([hypothetical])
        return list(vectors[0]) if vectors and vectors[0] else None
    return _provider_embed(hypothetical)


# =============================================================================
# Reranker — pluggable backend (CE / BGE-v2-m3 / BGE-large) with LLM fallback
# =============================================================================

# Default kept on the legacy ce-marco for backward compatibility with v8 callers
# that import this constant directly. Active backend is resolved per-call from
# config.get_v9_reranker_backend() so tests can flip env vars.
CROSS_ENCODER_MODEL = os.environ.get("CROSS_ENCODER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")

_BACKEND_TO_MODEL = {
    "ce-marco": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "bge-v2-m3": "BAAI/bge-reranker-v2-m3",
    "bge-large": "BAAI/bge-reranker-large",
}

# {backend_key: (model_obj_or_False, kind)}; kind ∈ {"ce", "flag"}
_reranker_cache: dict = {}


def _resolve_reranker_backend() -> str:
    """Read V9_RERANKER_BACKEND each call so tests can monkeypatch env."""
    try:
        import config as _cfg  # local import: avoids hard dep at module load
        return _cfg.get_v9_reranker_backend()
    except Exception:
        return "ce-marco"


def _resolve_reranker_model(backend: str) -> str:
    """Honor V9_RERANKER_MODEL override; else fall back to backend table."""
    try:
        import config as _cfg
        override = _cfg.get_v9_reranker_model_override()
        if override:
            return override
    except Exception:
        pass
    return _BACKEND_TO_MODEL.get(backend, CROSS_ENCODER_MODEL)


def _reset_reranker_cache() -> None:
    """Clear loaded reranker models — used by tests after env changes."""
    global _reranker_cache
    _reranker_cache = {}


# The reranker backends live in the ``rerank`` extra, not the base install:
# they resolve torch and the nvidia-cu* stack (~3 GB of wheels) for a path the
# default MEMORY_MODE=fast switches off. So "not installed" is the expected
# state for most users and the message has to say what to do about it, not just
# that an import failed.
_RERANK_EXTRA_HINT = (
    'reranker backend not installed — pip install "total-agent-memory[rerank]" '
    "(or pip install -r requirements-rerank.txt). Retrieval continues without "
    "reranking."
)


def _load_ce_reranker(model_name: str):
    """sentence-transformers CrossEncoder — works for ms-marco-* family."""
    try:
        from cpu_budget import configure_torch_threads
        configure_torch_threads()
        from sentence_transformers import CrossEncoder
        ce = CrossEncoder(model_name)
        LOG(f"CrossEncoder loaded: {model_name}")
        return ce
    except ImportError:
        LOG(f"CrossEncoder unavailable ({model_name}): {_RERANK_EXTRA_HINT}")
        return None
    except Exception as e:  # noqa: BLE001
        LOG(f"CrossEncoder load failed ({model_name}): {e}")
        return None


def _load_flag_reranker(model_name: str):
    """Load a BAAI/bge-reranker-* model.

    Strategy: prefer sentence-transformers CrossEncoder first — it works
    with bge-reranker-v2-m3 and bge-reranker-large under any modern
    transformers version. FlagEmbedding's custom tokenizer wrapper is
    fragile across transformers releases (e.g. transformers>=4.40 removes
    XLMRobertaTokenizer.prepare_for_model that FlagEmbedding 1.2.x relies on).

    Fall back to FlagReranker only if CrossEncoder cannot load the model
    (rare — usually means HF download failed).

    Both loaders live in the ``rerank`` extra; without it this returns None,
    and ``rerank_results`` drops to its documented Ollama fallback — which in
    turn returns the RRF order untouched when Ollama is unreachable.
    """
    ce = _load_ce_reranker(model_name)
    if ce is not None:
        LOG(f"Reranker loaded via CrossEncoder API: {model_name}")
        return ce

    use_fp16 = True
    try:
        import config as _cfg
        use_fp16 = _cfg.get_v9_reranker_use_fp16()
    except Exception:
        pass

    try:
        from cpu_budget import configure_torch_threads
        configure_torch_threads()
        from FlagEmbedding import FlagReranker  # type: ignore[import-not-found]
        rr = FlagReranker(model_name, use_fp16=use_fp16)
        LOG(f"FlagReranker loaded: {model_name} (fp16={use_fp16})")
        return rr
    except ImportError:
        LOG(f"FlagReranker unavailable ({model_name}): {_RERANK_EXTRA_HINT}")
        return None
    except Exception as e:  # noqa: BLE001
        LOG(f"FlagReranker load failed ({model_name}): {e}")
        return None


# =============================================================================
# v11 D4 — BGE-v2-m3 named-function entrypoints
# =============================================================================
# These named helpers are part of the public API contract for v11 LoCoMo D4.
# The dispatch layer (`rerank_results` → `_get_reranker`) already routes via
# `V9_RERANKER_BACKEND`; the helpers below give explicit, test-friendly
# handles for the multilingual BGE-v2-m3 path so callers (notebooks, ad-hoc
# scripts, benchmark code) can pin to it without touching env vars.

_bge_v2_m3_singleton = None


def _get_bge_v2_m3():
    """Lazy singleton for BAAI/bge-reranker-v2-m3.

    Tries sentence-transformers CrossEncoder first (works with any modern
    transformers release), then FlagEmbedding.FlagReranker. fp16 honored
    via V9_RERANKER_FP16 (default on — ~2x speed on Apple Silicon/CUDA).

    Returns the loaded model object, or None if neither backend is
    importable. Cached for the process lifetime.
    """
    global _bge_v2_m3_singleton
    if _bge_v2_m3_singleton is not None:
        # `False` sentinel marks a previous failed load — don't retry.
        return _bge_v2_m3_singleton if _bge_v2_m3_singleton is not False else None
    obj = _load_flag_reranker("BAAI/bge-reranker-v2-m3")
    _bge_v2_m3_singleton = obj if obj is not None else False
    return obj


def _reset_bge_v2_m3_singleton() -> None:
    """Test helper — drop cached singleton so monkeypatched loaders re-run."""
    global _bge_v2_m3_singleton
    _bge_v2_m3_singleton = None


def _bge_rerank_scores(query: str, passages: list[str]) -> list[float]:
    """Score (query, passage) pairs with BGE-v2-m3, normalised to [0,1].

    Used by tests and external callers that want raw scores without going
    through the full `rerank_results` dispatch / boost-blend pipeline.

    Returns a list of floats with len == len(passages). If the model fails
    to load, returns an all-zeros list of the right length so callers can
    treat the reranker as a no-op without branching on None.
    """
    if not passages:
        return []
    model = _get_bge_v2_m3()
    if model is None:
        return [0.0] * len(passages)

    pairs = [[query, p] for p in passages]
    try:
        if hasattr(model, "compute_score"):
            # FlagReranker path — `normalize=True` applies sigmoid → [0,1].
            scores = model.compute_score(pairs, normalize=True)
        else:
            # CrossEncoder path — predict returns logits, sigmoid manually.
            raw = model.predict(pairs)
            scores = (1.0 / (1.0 + np.exp(-np.array(raw, dtype=np.float32)))).tolist()
    except Exception as e:  # noqa: BLE001
        LOG(f"BGE-v2-m3 scoring failed: {e}")
        return [0.0] * len(passages)

    if isinstance(scores, (int, float)):
        scores = [float(scores)]
    return [float(s) for s in scores]


def _get_reranker(backend: str | None = None):
    """Lazy-load the configured reranker. Returns (model, kind) or (None, None)."""
    backend = backend or _resolve_reranker_backend()
    if backend == "off":
        return None, None
    cached = _reranker_cache.get(backend)
    if cached is not None:
        return cached if cached[0] is not False else (None, None)

    model_name = _resolve_reranker_model(backend)
    if backend == "ce-marco":
        obj = _load_ce_reranker(model_name)
        kind = "ce"
    else:  # bge-v2-m3, bge-large
        obj = _load_flag_reranker(model_name)
        # FlagReranker exposes .compute_score; CrossEncoder exposes .predict —
        # we sniff at score-time, so kind tracks the *requested* backend for
        # logging only.
        kind = "flag" if obj is not None and hasattr(obj, "compute_score") else "ce"

    _reranker_cache[backend] = (obj if obj is not None else False, kind)
    return (obj, kind) if obj is not None else (None, None)


def _get_cross_encoder():
    """Backward-compat shim — returns the legacy ce-marco CrossEncoder."""
    obj, _ = _get_reranker("ce-marco")
    return obj


def rerank_results(query: str, results: list, top_k: int = 10) -> list:
    """
    Rerank search results using the configured backend (V9_RERANKER_BACKEND).

    Backends:
      ce-marco   ms-marco-MiniLM-L-6-v2  ~5ms /20 pairs, MS-MARCO web search.
      bge-v2-m3  BAAI/bge-reranker-v2-m3 multilingual, conversation-friendly.
      bge-large  BAAI/bge-reranker-large higher acc, English-only, slower.
      off        no-op, returns top_k as-is.
    LLM fallback (Ollama) used only if the configured backend fails to load.
    """
    if not results:
        return results

    candidates = results[:min(len(results), top_k * 2)]

    backend = _resolve_reranker_backend()
    if backend == "off":
        return results[:top_k]

    model, kind = _get_reranker(backend)
    if model is not None:
        ranked = _rerank_with_model(model, kind, query, candidates, top_k)
    else:
        ranked = _rerank_llm(query, candidates, top_k)

    # Combine reranked + remaining
    reranked_ids = {id(item) for item in ranked}
    remaining = [item for item in results if id(item) not in reranked_ids]
    return ranked[:top_k] + remaining[:max(0, top_k - len(ranked))]


def _rerank_with_model(model, kind: str, query: str, candidates: list, top_k: int) -> list:
    """Score candidates with the loaded model; CE-boost-only blend with original."""
    pairs = []
    for item in candidates:
        content = excerpt(item["r"].get("content", ""), query, MODEL_RERANK_EXCERPT_CHARS, len)
        project = item["r"].get("project", "")
        tags = item["r"].get("tags", "")
        doc = f"[{project}] {content}"
        if tags:
            doc += f" tags:{tags}"
        pairs.append([query, doc])

    raw_scores = _score_pairs(model, kind, pairs)
    if raw_scores is None:
        return candidates[:top_k]

    # All paths return logits/raw; sigmoid → [0,1] for stable blending.
    scores_norm = 1.0 / (1.0 + np.exp(-np.array(raw_scores, dtype=np.float32)))

    # CE boost-only: reranker can promote results, never demote them.
    # Conservative because rerankers (esp. ce-marco trained on web search)
    # may misjudge conversational LoCoMo data. Keep original ordering as
    # a soft prior; reranker contributes 40% weight.
    max_orig = max(item["score"] for item in candidates) or 1.0
    for i, item in enumerate(candidates):
        if i < len(scores_norm):
            ce_score = float(scores_norm[i])
            orig_norm = item["score"] / max_orig
            ce_blend = ce_score * 0.4 + orig_norm * 0.6
            item["score"] = max(orig_norm, ce_blend) * max_orig  # restore scale
            item["reranked"] = True
            item["ce_score"] = round(ce_score, 4)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def _score_pairs(model, kind: str, pairs: list[list[str]]):
    """Backend-specific scoring. Returns list[float] of raw scores or None."""
    try:
        if kind == "flag" and hasattr(model, "compute_score"):
            # FlagReranker accepts list[[q,p]]; returns list[float] of logits.
            scores = model.compute_score(pairs)
            if isinstance(scores, (int, float)):
                return [float(scores)]
            return [float(s) for s in scores]
        # sentence-transformers CrossEncoder.predict
        return model.predict(pairs)
    except Exception as e:  # noqa: BLE001
        LOG(f"reranker scoring failed ({kind}): {e}")
        return None


def _rerank_cross_encoder(ce, query: str, candidates: list, top_k: int) -> list:
    """Legacy entrypoint kept for tests that imported it by name."""
    return _rerank_with_model(ce, "ce", query, candidates, top_k)


def _rerank_llm(query: str, candidates: list, top_k: int) -> list:
    """Fallback: rerank using Ollama LLM as cross-encoder."""
    entries = []
    for i, item in enumerate(candidates):
        content = excerpt(item["r"].get("content", ""), query, LLM_RERANK_EXCERPT_CHARS, len)
        project = item["r"].get("project", "")
        rtype = item["r"].get("type", "")
        entries.append(f"{i}. [{rtype}|{project}] {content}")

    prompt = (
        "Rate how relevant each knowledge entry is to the query. Score 0-10.\n"
        "0 = completely irrelevant, 10 = perfect match.\n"
        "Return ONLY a JSON array of scores, e.g. [8, 3, 7, 1, 9]\n\n"
        f"Query: {query}\n\n"
        f"Entries:\n{chr(10).join(entries)}\n\n"
        "Scores (JSON array):"
    )

    response = _ollama_generate(prompt, RERANKER_MODEL, max_tokens=100, temperature=0.1)
    if not response:
        return candidates[:top_k]

    scores = _parse_scores(response, len(candidates))
    if not scores:
        return candidates[:top_k]

    max_orig = max(item["score"] for item in candidates) or 1.0
    for i, item in enumerate(candidates):
        if i < len(scores):
            rerank_score = scores[i] / 10.0
            orig_norm = item["score"] / max_orig
            item["score"] = rerank_score * 0.6 + orig_norm * 0.4
            item["reranked"] = True

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def _parse_scores(response: str, expected_count: int) -> Optional[list]:
    """Parse JSON array of scores from LLM response."""
    response = response.strip()

    for attempt in [response, f"[{response}]"]:
        try:
            scores = json.loads(attempt)
            if isinstance(scores, list):
                result = []
                for s in scores[:expected_count]:
                    try:
                        val = float(s)
                        result.append(max(0, min(10, val)))
                    except (TypeError, ValueError):
                        result.append(5.0)
                return result
        except json.JSONDecodeError:
            continue

    import re
    numbers = re.findall(r'\b(\d+(?:\.\d+)?)\b', response)
    if numbers:
        return [max(0, min(10, float(n))) for n in numbers[:expected_count]]

    LOG(f"Failed to parse reranker scores: {response[:100]}")
    return None


# =============================================================================
# MMR — Maximal Marginal Relevance (diversity in search results)
# =============================================================================

def mmr_diversify(results: list, embeddings: list, lambda_param: float = 0.7,
                  top_k: int = 10, redundancy_threshold: float = 0.85) -> list:
    """
    Adaptive MMR: only activates when top results are truly redundant.

    Checks avg pairwise similarity of top-k results first.
    If below threshold — returns as-is (no diversity penalty).
    If above — applies MMR with high λ (0.9) to gently remove near-duplicates.

    Args:
        results: list of dicts with "r" (record) and "score" (relevance score)
        embeddings: list of numpy arrays, same length as results
        lambda_param: base λ (overridden to 0.9 when redundancy detected)
        top_k: number of results to return
        redundancy_threshold: avg pairwise sim above which MMR activates

    Returns:
        reordered list (only reordered if redundancy detected)
    """
    if len(results) <= 2 or not embeddings:
        return results[:top_k]

    n = len(results)
    if len(embeddings) != n:
        LOG(f"MMR: embeddings count ({len(embeddings)}) != results count ({n}), skipping")
        return results[:top_k]

    # Normalize embeddings
    emb_matrix = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(emb_matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    emb_matrix = emb_matrix / norms

    # Pairwise similarity
    sim_matrix = emb_matrix @ emb_matrix.T

    # Check if top results are actually redundant
    check_n = min(top_k, n)
    if check_n > 1:
        top_sims = []
        for i in range(check_n):
            for j in range(i + 1, check_n):
                top_sims.append(float(sim_matrix[i][j]))
        avg_sim = sum(top_sims) / len(top_sims) if top_sims else 0

        if avg_sim < redundancy_threshold:
            # Results are diverse enough — don't apply MMR
            return results[:top_k]

        LOG(f"MMR activated: avg_sim={avg_sim:.3f} > {redundancy_threshold} (top-{check_n} redundant)")

    # Apply MMR with high λ (gentle: mostly relevance, slight diversity push)
    effective_lambda = 0.9

    scores = np.array([item["score"] for item in results], dtype=np.float32)
    max_score = scores.max()
    if max_score > 0:
        scores = scores / max_score

    selected = []
    remaining = list(range(n))

    for _ in range(min(top_k, n)):
        if not remaining:
            break

        if not selected:
            best_idx = remaining[int(np.argmax(scores[remaining]))]
        else:
            best_score = -float("inf")
            best_idx = remaining[0]
            for idx in remaining:
                relevance = scores[idx]
                max_sim = max(sim_matrix[idx][s] for s in selected)
                mmr_score = effective_lambda * relevance - (1 - effective_lambda) * max_sim
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = idx

        selected.append(best_idx)
        remaining.remove(best_idx)

    return [results[i] for i in selected]


# =============================================================================
# Query Analyzer — classify query to choose strategy
# =============================================================================

def analyze_query(query: str) -> dict:
    """
    Classify query type to optimize search strategy.
    Fast heuristic-based (no LLM call).

    Returns:
        {
            "type": "factual"|"solution"|"debug"|"architecture"|"search",
            "expand": True/False (should use HyDE),
            "deep_graph": True/False (should do multi-hop)
        }
    """
    ql = query.lower()

    # Debug/error patterns
    if any(w in ql for w in ["error", "ошибка", "bug", "fix", "broken", "fail", "не работает", "crash"]):
        return {"type": "debug", "expand": True, "deep_graph": False}

    # Factual lookup (check BEFORE solution — "какой стек" is factual, not "как")
    if any(w in ql for w in ["что", "what", "where", "где", "какой", "какая", "какое", "стек",
                              "порт", "url", "пароль", "домен", "версия", "кто", "сколько"]):
        return {"type": "factual", "expand": False, "deep_graph": False}

    # Architecture patterns
    if any(w in ql for w in ["architect", "архитектур", "структур", "pattern", "design", "подход", "выбор", "сравн"]):
        return {"type": "architecture", "expand": True, "deep_graph": True}

    # Solution/how-to patterns
    if any(w in ql for w in ["как", "how", "implement", "реализ", "сделать", "настро", "создать"]):
        return {"type": "solution", "expand": True, "deep_graph": False}

    # Default: try HyDE for better recall
    return {"type": "search", "expand": len(query.split()) >= 3, "deep_graph": False}


# =============================================================================
# Multi-hop graph traversal
# =============================================================================

def multi_hop_expand(store, seed_ids: list, results: dict, depth: int = 2,
                     *, scope: SearchScope | None = None) -> dict:
    """
    Expand graph relations to depth N (default 2 hops).
    Current system does 1 hop — this does 2+ for architecture queries.

    A → B → C: if A is relevant, B is likely relevant, C might be.
    Score decays: hop1 = parent * 0.4, hop2 = parent * 0.2
    """
    visited = set(results.keys())
    current_layer = seed_ids
    predicates, params = (scope or SearchScope()).sql()

    for hop in range(depth):
        next_layer = []
        decay = 0.4 / (hop + 1)  # 0.4, 0.2, 0.13...

        for kid in current_layer:
            if kid not in results:
                continue
            parent_score = results[kid]["score"]

            for r in store.q(f"""
                SELECT k.* FROM relations rel
                JOIN knowledge k ON k.id = CASE WHEN rel.from_id=? THEN rel.to_id ELSE rel.from_id END
                WHERE (rel.from_id=? OR rel.to_id=?) AND {' AND '.join(predicates)}
            """, [kid, kid, kid, *params]):
                rid = r["id"]
                if rid not in visited:
                    visited.add(rid)
                    results[rid] = {"r": r, "score": parent_score * decay, "via": ["graph"]}
                    next_layer.append(rid)

        current_layer = next_layer
        if not current_layer:
            break

    return results
