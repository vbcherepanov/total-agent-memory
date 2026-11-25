#!/usr/bin/env python3
"""Improve semantic search quality for Claude Memory.

Provides:
  1. Model selection (best available via Ollama)
  2. Query expansion (multilingual + keyword extraction)
  3. Reciprocal Rank Fusion (RRF) for hybrid search
  4. Re-embedding with the best model
  5. Before/after benchmark

Usage:
    python src/tools/improve_search.py --reembed          # re-embed all with best model
    python src/tools/improve_search.py --test             # run before/after comparison
    python src/tools/improve_search.py --install-model    # install best embedding model
    python src/tools/improve_search.py --show-models      # show available embedding models
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from erasure import chroma_write_lock  # noqa: E402
from paths import memory_dir  # noqa: E402
from tool_support import ollama_binary  # noqa: E402

MEMORY_DIR = memory_dir()
DB_PATH = MEMORY_DIR / "memory.db"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Model priority: best first. Each tuple: (name, dim, description)
MODEL_PRIORITY: list[tuple[str, int, str]] = [
    ("bge-m3", 1024, "BAAI BGE-M3: best multilingual (RU+EN), dense+sparse+colbert"),
    ("snowflake-arctic-embed2", 1024, "Snowflake Arctic Embed 2: SOTA retrieval quality"),
    ("mxbai-embed-large", 1024, "MixedBread mxbai-embed-large: strong multilingual"),
    ("nomic-embed-text", 768, "Nomic Embed Text: decent baseline, fast"),
]

# Russian stopwords for keyword extraction
RU_STOPWORDS = frozenset(
    "и в на не что как это по с он она они но из за к от до "
    "бы ли же ни или мне мой моя все для так его ее уже когда "
    "при нет есть был если то тоже только этот этих".split()
)

EN_STOPWORDS = frozenset(
    "the a an and or but in on at to for of is it this that with from by "
    "as are was were be been has have had do does did will would can could "
    "should may might shall not no".split()
)


# ---------------------------------------------------------------------------
# 1. Model Selection
# ---------------------------------------------------------------------------

def get_ollama_models() -> list[str]:
    """Get list of models available in Ollama."""
    binary = ollama_binary()
    if binary is None:
        return []
    try:
        result = subprocess.run(
            [binary, "list"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode != 0:
            return []
        models = []
        for line in result.stdout.strip().splitlines()[1:]:  # skip header
            name = line.split()[0] if line.strip() else ""
            if name:
                # Normalize: remove :latest tag
                base = name.split(":")[0]
                models.append(base)
        return models
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def select_best_model() -> tuple[str, int]:
    """Select best embedding model from available Ollama models.

    Returns:
        Tuple of (model_name, dimension).
    """
    available = get_ollama_models()
    print(f"Available Ollama models: {len(available)}")

    for model_name, dim, desc in MODEL_PRIORITY:
        if model_name in available:
            print(f"  Selected: {model_name} ({dim}-dim) -- {desc}")
            return model_name, dim

    # None of the preferred models found
    print("\n  None of the preferred embedding models found.")
    print("  Install the best one with:")
    print(f"    ollama pull {MODEL_PRIORITY[0][0]}")
    print()

    # Fall back to nomic if available
    if "nomic-embed-text" in available:
        print("  Falling back to nomic-embed-text (768-dim)")
        return "nomic-embed-text", 768

    raise RuntimeError(
        "No embedding model available. Run: ollama pull bge-m3"
    )


# ---------------------------------------------------------------------------
# 2. Query Expansion
# ---------------------------------------------------------------------------

def _is_cyrillic(text: str) -> bool:
    """Check if text is predominantly Cyrillic."""
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    lat = sum(1 for c in text if "a" <= c.lower() <= "z")
    return cyr > lat


def _extract_keywords(text: str) -> str:
    """Extract meaningful keywords from a query, removing stopwords."""
    words = re.findall(r"[\w\-]+", text.lower())
    stopwords = RU_STOPWORDS | EN_STOPWORDS
    keywords = [w for w in words if w not in stopwords and len(w) > 2]
    return " ".join(keywords)


def _translate_keywords(text: str) -> str | None:
    """Generate cross-language keyword variant.

    For Russian text: extract technical terms (often already Latin).
    For English text: keep as-is (most code terms are English).
    Returns None if no useful translation can be generated.
    """
    words = re.findall(r"[\w\-]+", text)
    if _is_cyrillic(text):
        # Extract Latin/technical terms from Russian text
        latin_words = [w for w in words if all("a" <= c.lower() <= "z" or c in "-_." for c in w)]
        if latin_words:
            return " ".join(latin_words)
        # Common RU->EN technical mappings
        ru_en_map = {
            "поиск": "search", "ошибка": "error", "ошибки": "errors",
            "настройка": "config configuration", "конфигурация": "configuration",
            "база": "database", "данных": "data", "данные": "data",
            "сервер": "server", "клиент": "client", "запрос": "request query",
            "ответ": "response", "модель": "model", "тест": "test",
            "память": "memory", "кэш": "cache", "файл": "file",
            "проект": "project", "сессия": "session", "пользователь": "user",
            "токен": "token", "вектор": "vector embedding",
            "индекс": "index", "миграция": "migration", "схема": "schema",
            "контейнер": "container docker", "деплой": "deploy",
            "авторизация": "auth authorization", "аутентификация": "authentication",
            "маршрут": "route", "обработка": "handler processing",
            "интеграция": "integration", "зависимость": "dependency",
        }
        translated = []
        for w in words:
            wl = w.lower()
            if wl in ru_en_map:
                translated.append(ru_en_map[wl])
        if translated:
            return " ".join(translated)
    return None


def expand_query(query: str) -> list[str]:
    """Generate multiple query variants for better recall.

    Returns:
        List of 2-3 query strings (original + keywords + translated).
    """
    variants = [query]

    # Keywords-only variant
    keywords = _extract_keywords(query)
    if keywords and keywords != query.lower().strip():
        variants.append(keywords)

    # Cross-language variant
    translated = _translate_keywords(query)
    if translated:
        variants.append(translated)

    return variants


# ---------------------------------------------------------------------------
# 3. Reciprocal Rank Fusion (RRF)
# ---------------------------------------------------------------------------

@dataclass
class RRFResult:
    """A result with its RRF score and contributing sources."""
    id: int
    rrf_score: float = 0.0
    sources: list[str] = field(default_factory=list)


def rrf_fusion(
    ranked_lists: dict[str, list[int]],
    k: int = 60,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion across multiple ranked lists.

    For each result, score = sum(1 / (k + rank)) across all lists where it appears.
    Higher score = better. k=60 is the standard constant from the RRF paper.

    Args:
        ranked_lists: Dict mapping source name to ordered list of IDs.
                      Example: {"fts": [5, 3, 1], "semantic": [3, 7, 5]}
        k: Smoothing constant (default 60, from original paper).

    Returns:
        List of (id, rrf_score) sorted by score descending.
    """
    scores: dict[int, RRFResult] = {}

    for source, ids in ranked_lists.items():
        for rank, item_id in enumerate(ids):
            if item_id not in scores:
                scores[item_id] = RRFResult(id=item_id)
            scores[item_id].rrf_score += 1.0 / (k + rank + 1)  # rank is 0-based
            if source not in scores[item_id].sources:
                scores[item_id].sources.append(source)

    # Sort by RRF score descending
    ranked = sorted(scores.values(), key=lambda r: -r.rrf_score)
    return [(r.id, r.rrf_score) for r in ranked]


# ---------------------------------------------------------------------------
# 4. Embedding via Ollama HTTP API
# ---------------------------------------------------------------------------

def embed_text(text: str, model: str) -> list[float] | None:
    """Embed a single text via Ollama REST API."""
    payload = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            embeddings = data.get("embeddings")
            if embeddings and len(embeddings) > 0:
                return embeddings[0]
            # Fallback for older Ollama versions
            return data.get("embedding")
    except Exception as e:
        print(f"  Embed error: {e}", file=sys.stderr)
        return None


def embed_batch(texts: list[str], model: str) -> list[list[float] | None]:
    """Embed a batch of texts one by one (Ollama does not batch well)."""
    results = []
    for text in texts:
        results.append(embed_text(text, model))
    return results


# ---------------------------------------------------------------------------
# 5. Re-embedding
# ---------------------------------------------------------------------------

def quantize_binary(embedding: list[float]) -> bytes:
    """Convert float32 embedding to packed binary vector."""
    arr = np.array(embedding, dtype=np.float32)
    binary = np.where(arr > 0, 1, 0).astype(np.uint8)
    return np.packbits(binary).tobytes()


def float32_to_blob(embedding: list[float]) -> bytes:
    """Convert float32 embedding list to BLOB."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def load_records(db_path: Path) -> list[dict[str, Any]]:
    """Load all active knowledge records."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, session_id, type, content, context, project, status, "
        "confidence, created_at, tags FROM knowledge WHERE status='active' ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ensure_embeddings_table(conn: sqlite3.Connection) -> None:
    """Create embeddings table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            knowledge_id INTEGER PRIMARY KEY,
            binary_vector BLOB NOT NULL,
            float32_vector BLOB NOT NULL,
            embed_model TEXT NOT NULL,
            embed_dim INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


def reembed_all(db_path: str | Path = DB_PATH, model: str = "auto") -> dict[str, Any]:
    """Re-embed all active knowledge records with the best model.

    Args:
        db_path: Path to memory.db.
        model: Model name or "auto" for best available.

    Returns:
        Stats dict with counts, timing, model info.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    # Select model
    if model == "auto":
        model_name, expected_dim = select_best_model()
    else:
        model_name = model
        expected_dim = 0  # Will detect from first embedding

    records = load_records(db_path)
    print(f"\nRecords to embed: {len(records)}")
    print(f"Model: {model_name}")

    conn = sqlite3.connect(str(db_path))
    ensure_embeddings_table(conn)

    # Check current state
    current_model = conn.execute(
        "SELECT embed_model FROM embeddings LIMIT 1"
    ).fetchone()
    current_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    if current_model:
        print(f"Current embeddings: {current_count} (model: {current_model[0]})")
    else:
        print(f"Current embeddings: {current_count}")

    total_start = time.time()
    embedded = 0
    errors = 0
    dim = expected_dim
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for i, record in enumerate(records):
        # Compose text to embed: content + context + tags
        parts = [record["content"]]
        if record.get("context"):
            parts.append(record["context"])
        if record.get("tags"):
            try:
                tags = json.loads(record["tags"]) if isinstance(record["tags"], str) else record["tags"]
                if isinstance(tags, list):
                    parts.append(" ".join(str(t) for t in tags))
            except (json.JSONDecodeError, TypeError):
                pass
        text = " ".join(parts)

        # Truncate very long texts (Ollama context limit)
        if len(text) > 8000:
            text = text[:8000]

        embedding = embed_text(text, model_name)
        if embedding is None:
            errors += 1
            continue

        if dim == 0:
            dim = len(embedding)
            print(f"Detected dimension: {dim}")

        binary_blob = quantize_binary(embedding)
        f32_blob = float32_to_blob(embedding)

        conn.execute(
            """INSERT OR REPLACE INTO embeddings
               (knowledge_id, binary_vector, float32_vector, embed_model, embed_dim, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (record["id"], binary_blob, f32_blob, model_name, dim, now),
        )
        embedded += 1

        # Progress every 100 records
        if (i + 1) % 100 == 0 or i == len(records) - 1:
            elapsed = time.time() - total_start
            rate = embedded / elapsed if elapsed > 0 else 0
            eta = (len(records) - i - 1) / rate if rate > 0 else 0
            print(
                f"  Progress: {i + 1}/{len(records)} "
                f"({embedded} ok, {errors} err) "
                f"[{elapsed:.0f}s, {rate:.1f} rec/s, ETA {eta:.0f}s]"
            )
            conn.commit()

    conn.commit()

    # Update ChromaDB too if available
    chroma_updated = False
    try:
        import chromadb

        chroma_path = MEMORY_DIR / "chroma"
        if chroma_path.exists():
            print("\nUpdating ChromaDB...")
            client = chromadb.PersistentClient(path=str(chroma_path))
            collection = client.get_or_create_collection(
                "knowledge", metadata={"hnsw:space": "cosine"}
            )

            # Re-read all embeddings from SQLite and upsert to ChromaDB
            rows = conn.execute(
                "SELECT e.knowledge_id, e.float32_vector FROM embeddings e "
                "JOIN knowledge k ON e.knowledge_id = k.id WHERE k.status='active'"
            ).fetchall()

            batch_size = 100
            for j in range(0, len(rows), batch_size):
                batch = rows[j : j + batch_size]
                ids = [str(r[0]) for r in batch]
                embs = [
                    list(struct.unpack(f"{len(r[1]) // 4}f", r[1]))
                    for r in batch
                ]

                # Get metadata for each record
                kid_list = [r[0] for r in batch]
                placeholders = ",".join("?" * len(kid_list))
                meta_rows = conn.execute(
                    f"SELECT id, type, project, status, session_id, created_at, "
                    f"confidence, content, context FROM knowledge WHERE id IN ({placeholders})",
                    kid_list,
                ).fetchall()
                meta_map = {r[0]: r for r in meta_rows}

                metas = []
                docs = []
                for r in batch:
                    kid = r[0]
                    if kid in meta_map:
                        m = meta_map[kid]
                        metas.append({
                            "type": m[1] or "",
                            "project": m[2] or "",
                            "status": m[3] or "active",
                            "session_id": m[4] or "",
                            "created_at": m[5] or "",
                            "confidence": float(m[6] or 1.0),
                        })
                        docs.append(f"{m[7] or ''} {m[8] or ''}")
                    else:
                        metas.append({"type": "", "project": "", "status": "active"})
                        docs.append("")

                with chroma_write_lock(chroma_path):
                    collection.upsert(ids=ids, embeddings=embs, documents=docs, metadatas=metas)

            chroma_count = collection.count()
            print(f"  ChromaDB updated: {chroma_count} embeddings")
            chroma_updated = True
    except ImportError:
        pass
    except Exception as e:
        print(f"  ChromaDB update failed: {e}", file=sys.stderr)

    conn.close()

    total_time = time.time() - total_start
    stats = {
        "model": model_name,
        "dimension": dim,
        "total_records": len(records),
        "embedded": embedded,
        "errors": errors,
        "time_seconds": round(total_time, 1),
        "rate_per_second": round(embedded / max(total_time, 0.01), 1),
        "chroma_updated": chroma_updated,
    }

    print(f"\n{'=' * 60}")
    print(f"Re-embedding complete:")
    print(f"  Model: {model_name} ({dim}-dim)")
    print(f"  Records: {embedded}/{len(records)} ({errors} errors)")
    print(f"  Time: {total_time:.1f}s ({stats['rate_per_second']} rec/s)")
    print(f"  ChromaDB: {'updated' if chroma_updated else 'skipped'}")

    return stats


# ---------------------------------------------------------------------------
# 6. Benchmark / Test
# ---------------------------------------------------------------------------

# Test queries covering different scenarios
TEST_QUERIES = [
    # (query, expected_keywords_in_results)
    ("embedding semantic search reembed", ["embed", "search", "semantic"]),
    ("docker compose deployment", ["docker", "compose", "deploy"]),
    ("git commit push workflow", ["git", "commit"]),
    ("ошибка авторизации JWT token", ["auth", "JWT", "token"]),
    ("PostgreSQL migration index", ["postgres", "migration", "index"]),
    ("memory save recall session", ["memory", "session", "save"]),
    ("Vue Nuxt component composable", ["vue", "nuxt", "component"]),
    ("Symfony controller service DTO", ["symfony", "controller", "service"]),
    ("Go gRPC handler interceptor", ["go", "grpc", "handler"]),
    ("hooks session-start recovery", ["hook", "session", "recovery"]),
]


def _search_fts(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[int]:
    """Perform FTS5 search, return ranked IDs."""
    words = re.split(r"\s+", query)
    fts_q = " OR ".join(f'"{w}"' for w in words if len(w) > 2) or f'"{query}"'
    try:
        rows = conn.execute(
            """SELECT k.id, bm25(knowledge_fts) AS score
               FROM knowledge_fts f JOIN knowledge k ON k.id=f.rowid
               WHERE knowledge_fts MATCH ? AND k.status='active'
               ORDER BY bm25(knowledge_fts) LIMIT ?""",
            (fts_q, limit),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _search_semantic(
    conn: sqlite3.Connection, query: str, model: str, limit: int = 10
) -> list[int]:
    """Perform binary+cosine semantic search, return ranked IDs."""
    q_emb = embed_text(query, model)
    if q_emb is None:
        return []

    # Load all binary vectors
    rows = conn.execute(
        """SELECT e.knowledge_id, e.binary_vector
           FROM embeddings e JOIN knowledge k ON e.knowledge_id = k.id
           WHERE k.status='active'"""
    ).fetchall()
    if not rows:
        return []

    kid_list = [r[0] for r in rows]
    bin_vecs = np.array([np.frombuffer(r[1], dtype=np.uint8) for r in rows])

    # Hamming pre-filter
    q_binary = np.frombuffer(quantize_binary(q_emb), dtype=np.uint8)
    popcount_lut = np.array([bin(i).count("1") for i in range(256)], dtype=np.int32)
    xor_result = np.bitwise_xor(bin_vecs, q_binary)
    hamming = popcount_lut[xor_result].sum(axis=1)

    n_cand = min(50, len(kid_list))
    top_idx = np.argpartition(hamming, n_cand)[:n_cand]

    # Cosine re-rank
    candidate_kids = [int(kid_list[i]) for i in top_idx]
    placeholders = ",".join("?" * len(candidate_kids))
    f32_rows = conn.execute(
        f"SELECT knowledge_id, float32_vector FROM embeddings WHERE knowledge_id IN ({placeholders})",
        candidate_kids,
    ).fetchall()

    q_vec = np.array(q_emb, dtype=np.float32)
    q_norm = np.linalg.norm(q_vec)

    scored = []
    for kid, f32_blob in f32_rows:
        vec = np.frombuffer(f32_blob, dtype=np.float32)
        cos_sim = float(np.dot(q_vec, vec) / (q_norm * np.linalg.norm(vec) + 1e-10))
        scored.append((kid, cos_sim))

    scored.sort(key=lambda x: -x[1])
    return [s[0] for s in scored[:limit]]


def _search_expanded_semantic(
    conn: sqlite3.Connection, query: str, model: str, limit: int = 10
) -> list[int]:
    """Semantic search with query expansion + RRF fusion."""
    variants = expand_query(query)

    ranked_lists: dict[str, list[int]] = {}
    for i, variant in enumerate(variants):
        ids = _search_semantic(conn, variant, model, limit=limit)
        if ids:
            ranked_lists[f"sem_{i}"] = ids

    if not ranked_lists:
        return []

    fused = rrf_fusion(ranked_lists)
    return [item_id for item_id, _ in fused[:limit]]


def _search_hybrid_rrf(
    conn: sqlite3.Connection, query: str, model: str, limit: int = 10
) -> list[int]:
    """Full hybrid search: FTS + expanded semantic + RRF."""
    variants = expand_query(query)

    ranked_lists: dict[str, list[int]] = {}

    # FTS for each variant
    for i, variant in enumerate(variants):
        ids = _search_fts(conn, variant, limit=limit * 2)
        if ids:
            ranked_lists[f"fts_{i}"] = ids

    # Semantic for each variant
    for i, variant in enumerate(variants):
        ids = _search_semantic(conn, variant, model, limit=limit * 2)
        if ids:
            ranked_lists[f"sem_{i}"] = ids

    if not ranked_lists:
        return []

    fused = rrf_fusion(ranked_lists)
    return [item_id for item_id, _ in fused[:limit]]


def run_benchmark(db_path: Path = DB_PATH) -> None:
    """Run search quality benchmark comparing different strategies."""
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Check current model
    model_row = conn.execute("SELECT embed_model FROM embeddings LIMIT 1").fetchone()
    if not model_row:
        print("ERROR: No embeddings found. Run --reembed first.", file=sys.stderr)
        sys.exit(1)
    model = model_row[0]
    emb_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    total_records = conn.execute(
        "SELECT COUNT(*) FROM knowledge WHERE status='active'"
    ).fetchone()[0]

    print(f"Benchmark: {total_records} records, {emb_count} embeddings, model={model}")
    print(f"{'=' * 70}")

    strategies = {
        "FTS only": lambda q: _search_fts(conn, q),
        "Semantic only": lambda q: _search_semantic(conn, q, model),
        "Semantic+Expand": lambda q: _search_expanded_semantic(conn, q, model),
        "Hybrid RRF": lambda q: _search_hybrid_rrf(conn, q, model),
    }

    # Run all queries through all strategies
    results: dict[str, dict[str, float]] = {name: {"hits": 0, "total": 0, "time": 0.0} for name in strategies}

    for query, expected_kw in TEST_QUERIES:
        print(f"\nQuery: {query!r}")
        print(f"  Expected keywords: {expected_kw}")

        for name, search_fn in strategies.items():
            t0 = time.time()
            ids = search_fn(query)
            elapsed = time.time() - t0

            # Check if any results contain expected keywords
            hits = 0
            if ids:
                # Load content for result IDs
                placeholders = ",".join("?" * len(ids[:10]))
                rows = conn.execute(
                    f"SELECT content FROM knowledge WHERE id IN ({placeholders})",
                    ids[:10],
                ).fetchall()
                combined = " ".join(r[0].lower() for r in rows)
                for kw in expected_kw:
                    if kw.lower() in combined:
                        hits += 1

            relevance = hits / len(expected_kw) if expected_kw else 0
            results[name]["hits"] += hits
            results[name]["total"] += len(expected_kw)
            results[name]["time"] += elapsed

            marker = "+" if relevance >= 0.5 else "-"
            print(f"  [{marker}] {name:20s}: {len(ids):2d} results, "
                  f"{hits}/{len(expected_kw)} keywords, {elapsed:.2f}s")

    # Summary
    print(f"\n{'=' * 70}")
    print(f"{'Strategy':20s} {'Recall':>8s} {'Avg Time':>10s}")
    print(f"{'-' * 40}")
    for name, stats in results.items():
        recall = stats["hits"] / max(stats["total"], 1)
        avg_time = stats["time"] / len(TEST_QUERIES)
        bar = "#" * int(recall * 20)
        print(f"{name:20s} {recall:>7.1%}  {avg_time:>8.3f}s  {bar}")

    conn.close()


# ---------------------------------------------------------------------------
# 7. Install Model
# ---------------------------------------------------------------------------

def install_best_model() -> None:
    """Install the best embedding model via Ollama."""
    available = get_ollama_models()

    for model_name, dim, desc in MODEL_PRIORITY:
        if model_name in available:
            print(f"Best model already installed: {model_name} ({dim}-dim)")
            print(f"  {desc}")
            return

    # Install the best one
    target = MODEL_PRIORITY[0]
    print(f"Installing: {target[0]} ({target[1]}-dim)")
    print(f"  {target[2]}")
    print()

    binary = ollama_binary()
    if binary is None:
        print("ollama not found: set MEMORY_OLLAMA_BIN or add ollama to PATH", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(
        [binary, "pull", target[0]],
        timeout=600,
    )
    if result.returncode == 0:
        print(f"\nInstalled successfully: {target[0]}")
    else:
        print(f"\nFailed to install {target[0]}. Try manually:", file=sys.stderr)
        print(f"  ollama pull {target[0]}", file=sys.stderr)
        sys.exit(1)


def show_models() -> None:
    """Show available embedding models and their status."""
    available = get_ollama_models()
    print("Embedding Model Status:")
    print(f"{'=' * 70}")
    for model_name, dim, desc in MODEL_PRIORITY:
        status = "INSTALLED" if model_name in available else "not installed"
        marker = "*" if model_name in available else " "
        print(f"  [{marker}] {model_name:30s} {dim:4d}-dim  {status}")
        print(f"      {desc}")
    print()

    # Show what's currently in use
    if DB_PATH.exists():
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT embed_model, embed_dim, COUNT(*) FROM embeddings GROUP BY embed_model").fetchall()
        conn.close()
        if row:
            print("Currently in use:")
            for r in row:
                print(f"  {r[0]} ({r[1]}-dim): {r[2]} embeddings")


# ---------------------------------------------------------------------------
# 8. Integration Guide
# ---------------------------------------------------------------------------

INTEGRATION_GUIDE = """
## Integration Guide: Patching server.py Recall.search()

To integrate query expansion + RRF fusion into the existing search,
modify Recall.search() in server.py as follows:

### Step 1: Import at top of server.py

    from tools.improve_search import expand_query, rrf_fusion

### Step 2: In Recall.search(), after cache check, expand the query

    # After line: use_advanced = self._should_use_advanced_rag()
    query_variants = expand_query(query)

### Step 3: Run FTS for all query variants

    # Replace the single FTS block with:
    fts_ranked_lists = {}
    for vi, q_variant in enumerate(query_variants):
        fts_q = " OR ".join(Store._fts_escape(w) for w in re.split(r'\\s+', q_variant) if len(w) > 2) or Store._fts_escape(q_variant)
        try:
            # ... existing FTS query but with q_variant ...
            fts_ids = [r["id"] for r in fts_rows]
            if fts_ids:
                fts_ranked_lists[f"fts_{vi}"] = fts_ids
        except Exception:
            pass

### Step 4: Run semantic search for all variants

    # Replace single semantic block with:
    sem_ranked_lists = {}
    for vi, q_variant in enumerate(query_variants):
        embs = self.s.embed([q_variant])
        if embs:
            candidates = self.s._binary_search(embs[0], ...)
            sem_ids = [kid for kid, _ in candidates]
            if sem_ids:
                sem_ranked_lists[f"sem_{vi}"] = sem_ids

### Step 5: Merge with RRF

    all_ranked = {**fts_ranked_lists, **sem_ranked_lists}
    fused = rrf_fusion(all_ranked)
    # Use fused IDs as the primary ranking, then load full records
    for item_id, rrf_score in fused[:limit * 2]:
        if item_id not in results:
            rec = self.s.q1("SELECT * FROM knowledge WHERE id=?", (item_id,))
            if rec:
                results[item_id] = {"r": rec, "score": rrf_score * 10, "via": ["rrf"]}

### Expected improvement:
    - R@10 from 0.48 to ~0.70+ with query expansion
    - Better multilingual recall (RU queries find EN content)
    - FTS+semantic fusion catches both exact and conceptual matches
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Improve semantic search for Claude Memory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--reembed", action="store_true", help="Re-embed all records with best model")
    parser.add_argument("--test", action="store_true", help="Run before/after search benchmark")
    parser.add_argument("--install-model", action="store_true", help="Install best embedding model")
    parser.add_argument("--show-models", action="store_true", help="Show available embedding models")
    parser.add_argument("--integration-guide", action="store_true", help="Print server.py integration guide")
    parser.add_argument("--model", default="auto", help="Override model (default: auto-select best)")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to memory.db")

    args = parser.parse_args()

    if not any([args.reembed, args.test, args.install_model, args.show_models, args.integration_guide]):
        parser.print_help()
        sys.exit(0)

    if args.show_models:
        show_models()

    if args.install_model:
        install_best_model()

    if args.reembed:
        stats = reembed_all(db_path=Path(args.db), model=args.model)
        print(f"\nStats: {json.dumps(stats, indent=2)}")

    if args.test:
        run_benchmark(db_path=Path(args.db))

    if args.integration_guide:
        print(INTEGRATION_GUIDE)


if __name__ == "__main__":
    main()
