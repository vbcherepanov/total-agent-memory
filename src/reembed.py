#!/usr/bin/env python3
"""Re-embed all active knowledge records into ChromaDB or SQLite binary vectors.

Uses urllib.request for Ollama API (no extra dependencies needed).

Usage:
    python reembed.py --dry-run                # Just show counts
    python reembed.py --fastembed              # FastEmbed → SQLite (default, fastest)
    python reembed.py --fastembed --keep-chroma # FastEmbed → SQLite + ChromaDB
    python reembed.py --ollama                 # Ollama → ChromaDB
    python reembed.py --binary --ollama        # Ollama → SQLite (binary quantization)
    python reembed.py --binary                 # SentenceTransformers → SQLite
    python reembed.py --model nomic-embed-text # Override model
    python reembed.py --batch-size 100         # Larger batches
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import get_embed_threads
from erasure import chroma_write_lock
from paths import memory_dir

MEMORY_DIR = memory_dir()
DB_PATH = MEMORY_DIR / "memory.db"
CHROMA_PATH = MEMORY_DIR / "chroma"


def load_records(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, session_id, type, content, context, project, status, "
        "confidence, created_at FROM knowledge WHERE status='active' ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def embed_ollama(texts: list[str], model: str,
                 url: str = "http://localhost:11434") -> list[list[float]]:
    results = []
    for text in texts:
        payload = json.dumps({"model": model, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{url}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            results.append(data["embedding"])
    return results


def embed_st(texts: list[str], model_name: str) -> list[list[float]]:
    from cpu_budget import configure_torch_threads
    configure_torch_threads()
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_name)
    return model.encode(texts, show_progress_bar=False).tolist()


# Singleton to avoid re-loading the model per batch
_fastembed_instance = None


def embed_fastembed(texts: list[str], model_name: str) -> list[list[float]]:
    """Get embeddings via FastEmbed (local, fast, multilingual)."""
    global _fastembed_instance
    if _fastembed_instance is None:
        from fastembed import TextEmbedding
        from memory_core.fastembed_loader import load_model
        _fastembed_instance = load_model(TextEmbedding, model_name, threads=get_embed_threads())
    embeddings = list(_fastembed_instance.embed(texts))
    return [emb.tolist() if hasattr(emb, 'tolist') else list(emb) for emb in embeddings]


def quantize_binary(embedding: list[float]) -> bytes:
    """Convert float32 embedding to packed binary vector."""
    arr = np.array(embedding, dtype=np.float32)
    binary = np.where(arr > 0, 1, 0).astype(np.uint8)
    return np.packbits(binary).tobytes()


def float32_to_blob(embedding: list[float]) -> bytes:
    """Convert float32 embedding list to BLOB."""
    return struct.pack(f'{len(embedding)}f', *embedding)


DEFAULT_SPACE = "text"
PROVIDER_LABELS = {"fastembed": "fastembed", "ollama": "ollama", "st": "st"}


def ensure_embeddings_table(conn: sqlite3.Connection):
    """Create embeddings table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            knowledge_id INTEGER PRIMARY KEY,
            binary_vector BLOB NOT NULL,
            float32_vector BLOB NOT NULL,
            embed_model TEXT NOT NULL,
            embed_dim INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            embedding_provider TEXT,
            embedding_space TEXT,
            content_type TEXT,
            language TEXT
        )
    """)
    conn.commit()


def upsert_embedding(conn: sqlite3.Connection, knowledge_id: int, embedding: list[float], model: str,
                     provider: str, created_at: str) -> None:
    """Write a record's vector columns; the row's space, content type and language stay as they are.

    INSERT OR REPLACE would delete the row and drop every column it does not
    list, so a record's embedding_space went back to NULL on each re-embed.
    """
    conn.execute(
        """
        INSERT INTO embeddings
            (knowledge_id, binary_vector, float32_vector, embed_model, embed_dim, created_at,
             embedding_provider, embedding_space)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(knowledge_id) DO UPDATE SET
            binary_vector = excluded.binary_vector,
            float32_vector = excluded.float32_vector,
            embed_model = excluded.embed_model,
            embed_dim = excluded.embed_dim,
            created_at = excluded.created_at,
            embedding_provider = excluded.embedding_provider
        """,
        (knowledge_id, quantize_binary(embedding), float32_to_blob(embedding), model, len(embedding),
         created_at, provider, DEFAULT_SPACE),
    )


def main():
    parser = argparse.ArgumentParser(description="Re-embed all knowledge records")
    parser.add_argument("--fastembed", action="store_true",
                        help="Use FastEmbed (fastest, multilingual, default choice)")
    parser.add_argument("--ollama", action="store_true", help="Use Ollama HTTP API")
    parser.add_argument("--model", default=None, help="Embedding model name")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--binary", action="store_true",
                        help="Store in SQLite embeddings table (binary quantization)")
    parser.add_argument("--keep-chroma", action="store_true",
                        help="Also update ChromaDB when using --binary")
    args = parser.parse_args()

    # --fastembed implies --binary (SQLite storage)
    if args.fastembed:
        args.binary = True

    if args.model is None:
        if args.fastembed:
            from memory_core.embedding_spaces import resolve_text_model
            args.model = resolve_text_model()
        elif args.ollama:
            args.model = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        else:
            args.model = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found", file=sys.stderr)
        sys.exit(1)

    records = load_records(DB_PATH)
    target = "SQLite binary" if args.binary else "ChromaDB"
    provider = "FastEmbed" if args.fastembed else ("Ollama" if args.ollama else "SentenceTransformers")
    print(f"Found {len(records)} active knowledge records")
    print(f"Model: {args.model} ({provider})")
    print(f"Target: {target}")

    if args.dry_run:
        projects: dict[str, int] = {}
        types: dict[str, int] = {}
        for r in records:
            projects[r["project"]] = projects.get(r["project"], 0) + 1
            types[r["type"]] = types.get(r["type"], 0) + 1
        print(f"\nBy type:")
        for t, c in sorted(types.items(), key=lambda x: -x[1]):
            print(f"  {t}: {c}")
        print(f"\nBy project:")
        for p, c in sorted(projects.items(), key=lambda x: -x[1]):
            print(f"  {p}: {c}")
        print(f"\nTotal records to re-embed: {len(records)}")
        print(f"Batches ({args.batch_size} per batch): {(len(records) + args.batch_size - 1) // args.batch_size}")

        if args.binary:
            # Estimate storage
            dim = 384 if (args.fastembed or not args.ollama) else 768
            binary_size = len(records) * (dim // 8)
            float32_size = len(records) * dim * 4
            print(f"\nEstimated storage:")
            print(f"  Binary vectors: {binary_size / 1024:.1f} KB ({dim // 8} bytes/record)")
            print(f"  Float32 vectors: {float32_size / 1048576:.1f} MB ({dim * 4} bytes/record)")
            print(f"  Total: {(binary_size + float32_size) / 1048576:.1f} MB")
        return

    # ChromaDB setup (if needed)
    collection = None
    if not args.binary or args.keep_chroma:
        import chromadb
        client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        collection = client.get_or_create_collection("knowledge", metadata={"hnsw:space": "cosine"})
        print(f"ChromaDB: {collection.count()} existing embeddings")

    # SQLite setup for binary mode
    conn = None
    if args.binary:
        conn = sqlite3.connect(str(DB_PATH))
        ensure_embeddings_table(conn)
        existing = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        print(f"SQLite embeddings: {existing} existing")

    backend = PROVIDER_LABELS["fastembed" if args.fastembed else ("ollama" if args.ollama else "st")]
    texts = [f"{r['content']} {r.get('context', '')}" for r in records]
    total_start = time.time()
    all_embeddings: list[list[float]] = []
    errors = 0

    for i in range(0, len(texts), args.batch_size):
        batch = texts[i : i + args.batch_size]
        batch_num = i // args.batch_size + 1
        total_batches = (len(texts) + args.batch_size - 1) // args.batch_size
        t0 = time.time()

        try:
            if args.fastembed:
                embs = embed_fastembed(batch, args.model)
            elif args.ollama:
                embs = embed_ollama(batch, args.model, args.ollama_url)
            else:
                embs = embed_st(batch, args.model)
            all_embeddings.extend(embs)
            elapsed = time.time() - t0
            ms_per = elapsed / len(batch) * 1000
            print(f"  Batch {batch_num}/{total_batches}: {len(batch)} records in {elapsed:.1f}s ({ms_per:.0f}ms/rec)")
        except Exception as e:
            print(f"  ERROR batch {batch_num}: {e}", file=sys.stderr)
            errors += len(batch)
            all_embeddings.extend([[] for _ in batch])

    embed_time = time.time() - total_start
    good = [i for i, e in enumerate(all_embeddings) if e]
    dim = len(all_embeddings[good[0]]) if good else 0
    print(f"\nEmbedded: {len(good)}/{len(records)} ({dim}-dim), {embed_time:.1f}s total")

    # Upsert
    upsert_start = time.time()
    upserted = 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    for i in range(0, len(records), args.batch_size):
        batch_records = records[i : i + args.batch_size]
        batch_embs = all_embeddings[i : i + args.batch_size]
        batch_texts_sub = texts[i : i + args.batch_size]

        valid = [(r, e, t) for r, e, t in zip(batch_records, batch_embs, batch_texts_sub) if e]
        if not valid:
            continue

        # Binary quantization → SQLite
        if args.binary and conn:
            for r, emb, _ in valid:
                upsert_embedding(conn, r["id"], emb, args.model, backend, now)
            conn.commit()

        # ChromaDB (default or --keep-chroma)
        if collection is not None:
            ids = [str(r["id"]) for r, _, _ in valid]
            embs_list = [e for _, e, _ in valid]
            docs = [t for _, _, t in valid]
            metas = [
                {
                    "type": r["type"],
                    "project": r["project"],
                    "status": "active",
                    "session_id": r["session_id"],
                    "created_at": r["created_at"],
                    "confidence": r.get("confidence", 1.0),
                }
                for r, _, _ in valid
            ]
            with chroma_write_lock(CHROMA_PATH):
                collection.upsert(ids=ids, embeddings=embs_list, documents=docs, metadatas=metas)

        upserted += len(valid)

    upsert_time = time.time() - upsert_start
    total_time = time.time() - total_start

    print(f"\n{'=' * 50}")
    print(f"DONE: {upserted}/{len(records)} records re-embedded")
    print(f"  Model: {args.model} ({dim}-dim)")
    print(f"  Target: {target}")
    print(f"  Embed: {embed_time:.1f}s | Upsert: {upsert_time:.1f}s | Total: {total_time:.1f}s")
    print(f"  Avg: {total_time / max(upserted, 1) * 1000:.0f}ms/record")

    if args.binary and conn:
        final_count = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        binary_bytes = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(binary_vector)), 0) FROM embeddings"
        ).fetchone()[0]
        f32_bytes = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(float32_vector)), 0) FROM embeddings"
        ).fetchone()[0]
        print(f"  SQLite embeddings: {final_count}")
        print(f"  Binary vectors: {binary_bytes / 1024:.1f} KB")
        print(f"  Float32 vectors: {f32_bytes / 1048576:.1f} MB")
        print(f"  Total: {(binary_bytes + f32_bytes) / 1048576:.1f} MB")
        conn.close()

    if collection is not None:
        print(f"  ChromaDB now: {collection.count()} embeddings")

    if errors:
        print(f"  Errors: {errors}")


if __name__ == "__main__":
    main()
