#!/usr/bin/env python3
"""
Claude Total Memory — MCP Server v4.0 (Ultimate)

Tools (20): memory_recall, memory_save, memory_update, memory_timeline,
            memory_stats, memory_consolidate, memory_export, memory_forget,
            memory_history, memory_delete, memory_relate, memory_search_by_tag,
            memory_extract_session, memory_observe,
            self_error_log, self_insight, self_rules, self_patterns,
            self_reflect, self_rules_context
Storage: SQLite FTS5 + ChromaDB (semantic) + relations (graph)
Features: BM25 scoring, 3-level progressive disclosure, decay scoring, fuzzy search,
          deduplication, retention zones, consolidation, version history, graph relations,
          self-improvement pipeline (errors → insights → rules/SOUL),
          privacy stripping, branch-aware context, token estimation, observations
"""

import asyncio
import json
import logging
import math
import os
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

# v11.0 — Apply MEMORY_MODE → derived env defaults BEFORE any downstream
# module reads `USE_ADVANCED_RAG`, `MEMORY_QUALITY_GATE_ENABLED`, etc. at
# import time. Default mode = `fast` (zero LLM in hot path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import config as _v11_config  # noqa: E402
    _v11_config.resolve_mode_defaults()
except Exception as _v11_exc:  # pragma: no cover — never block startup
    sys.stderr.write(f"[memory-mcp] v11 mode resolver skipped: {_v11_exc}\n")

import importlib.util

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from memory_core.timestamps import format_utc, utc_now

# chromadb (~70 MB RSS) and sentence_transformers (which pulls in torch,
# ~450 MB) are *fallback* paths — when fastembed is healthy neither is ever
# used. Importing them eagerly cost every user that memory anyway: a bare
# `import server` measured 548 MB RSS with all three loaded. find_spec answers
# "is it installed" without importing; the real import happens at first use.
# Reported by d.snezhinskiy, who saw a 1.5 GB MCP server on a client machine.
#
# Since 13.0.2 sentence_transformers is not even installed by default — it
# moved to the `rerank` extra — so HAS_ST is normally False and the fallback
# chain ends at fastembed / Ollama. That is intentional: MEMORY_MODE=fast
# already forbade the ST branch via MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=false.
HAS_CHROMA = importlib.util.find_spec("chromadb") is not None
HAS_ST = importlib.util.find_spec("sentence_transformers") is not None

try:
    from fastembed import TextEmbedding
    HAS_FASTEMBED = True
except ImportError:
    HAS_FASTEMBED = False

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

try:
    from reranker import (
        analyze_query,
        hyde_expand,
        mmr_diversify,
        multi_hop_expand,
        rerank_results,
    )
    HAS_RERANKER = True
except ImportError:
    HAS_RERANKER = False

try:
    from temporal_filter import has_temporal_intent, temporal_rerank
    HAS_TEMPORAL_FILTER = True
except ImportError:
    HAS_TEMPORAL_FILTER = False

try:
    from temporal_index import (
        ensure_schema as _temporal_index_ensure_schema,
    )
    from temporal_index import (
        filter_by_query_date as _temporal_index_filter,
    )
    HAS_TEMPORAL_INDEX = True
except ImportError:
    HAS_TEMPORAL_INDEX = False

try:
    from graph_expander import expand as _graph_expand
    from graph_expander import fetch_records as _graph_fetch
    HAS_GRAPH_EXPAND = True
except ImportError:
    HAS_GRAPH_EXPAND = False

try:
    from query_rewriter import has_decomposable_intent as _qr_decomposable
    from query_rewriter import is_enabled as _qr_is_enabled
    from query_rewriter import rewrite as _qr_rewrite
    HAS_QUERY_REWRITER = True
except ImportError:
    HAS_QUERY_REWRITER = False

try:
    # Import cache early so it's available regardless of __file__ path tricks
    _src_dir = str(Path(__file__).resolve().parent)
    if _src_dir not in sys.path:
        sys.path.insert(0, _src_dir)
    from cache import QueryCache
    HAS_CACHE = True
except ImportError:
    HAS_CACHE = False

from paths import memory_dir as _resolve_memory_dir  # noqa: E402

MEMORY_DIR = _resolve_memory_dir()
# Records a supersede=true save compares against (most recent first).
SUPERSEDE_CANDIDATES = 500
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
# The text space is embedded by the store itself; MEMORY_TEXT_EMBED_MODEL names its model
# (FASTEMBED_MODEL is the older name and still wins when set).
FASTEMBED_MODEL = os.environ.get("FASTEMBED_MODEL") or _v11_config.get_text_embed_model()
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
USE_OLLAMA_EMBED = os.environ.get("USE_OLLAMA_EMBED", "auto")  # auto|true|false
DECAY_HALF_LIFE = int(os.environ.get("DECAY_HALF_LIFE", "90"))  # days
ARCHIVE_AFTER_DAYS = int(os.environ.get("ARCHIVE_AFTER_DAYS", "180"))
PURGE_AFTER_DAYS = int(os.environ.get("PURGE_AFTER_DAYS", "365"))
OBSERVATION_RETENTION_DAYS = int(os.environ.get("OBSERVATION_RETENTION_DAYS", "30"))
# Bounds SQLite WAL file growth: without a limit, checkpoints reuse space in
# place but never shrink the file back down, so it sits at its high-water
# mark indefinitely under sustained write load (measured: a 334MB WAL next
# to a 171MB db). 48MB keeps the file well under the db size while staying
# large enough that ordinary checkpoints don't thrash.
DB_JOURNAL_SIZE_LIMIT_BYTES = 48 * 1024 * 1024
# 8-10 concurrent MCP sessions plus the dashboard service contend for the
# single writer lock; 5000ms was too tight and surfaced as "database is
# locked" from memory_save. 15000ms absorbs realistic contention without
# reading as a hang to the caller, so do not raise further.
DB_BUSY_TIMEOUT_MS = 15000
# Short timeout for the opportunistic startup checkpoint attempt only: fail
# fast if another reader holds a snapshot rather than stalling startup.
DB_STARTUP_CHECKPOINT_TIMEOUT_MS = 200

# v10 — importance multipliers applied to the final recall score so a
# `critical` decision outranks ten `medium` observations at the same RRF
# rank. Override individual values via env (e.g. MEMORY_IMPORTANCE_BOOST_CRITICAL=2.0).
def _imp_boost(level: str, default: float) -> float:
    raw = os.environ.get(f"MEMORY_IMPORTANCE_BOOST_{level.upper()}")
    if raw is None:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default

_IMPORTANCE_BOOST = {
    "critical": _imp_boost("critical", 1.5),
    "high":     _imp_boost("high",     1.2),
    "medium":   _imp_boost("medium",   1.0),
    "low":      _imp_boost("low",      0.8),
}
USE_ADVANCED_RAG = os.environ.get("USE_ADVANCED_RAG", "auto")  # auto|true|false — HyDE + reranker
USE_BINARY_SEARCH = os.environ.get("USE_BINARY_SEARCH", "auto")  # auto|true|false — binary quantization
LOG = lambda msg: sys.stderr.write(f"[memory-mcp] {msg}\n")

# ── Super Memory v5 modules (lazy init) ──
_v5_modules = {}


def _get_v5(name, db):
    """Lazy-init v5 modules to avoid import overhead on startup."""
    if name not in _v5_modules:
        _src = str(Path(__file__).parent)
        if _src not in sys.path:
            sys.path.insert(0, _src)
        if name == "graph_store":
            from graph.store import GraphStore
            _v5_modules[name] = GraphStore(db)
        elif name == "graph_query":
            from graph.query import GraphQuery
            _v5_modules[name] = GraphQuery(_get_v5("graph_store", db))
        elif name == "graph_indexer":
            from graph.indexer import GraphIndexer
            _v5_modules[name] = GraphIndexer(db)
        elif name == "graph_enricher":
            from graph.enricher import GraphEnricher
            _v5_modules[name] = GraphEnricher(db)
        elif name == "activation":
            from associative.activation import SpreadingActivation
            _v5_modules[name] = SpreadingActivation(db)
        elif name == "composition":
            from associative.composition import CompositionEngine
            _v5_modules[name] = CompositionEngine(db)
        elif name == "assoc_recall":
            from associative.recall import AssociativeRecall
            _v5_modules[name] = AssociativeRecall(db, _get_v5("activation", db), _get_v5("composition", db))
        elif name == "episodes":
            from memory_systems.episode_store import EpisodeStore
            _v5_modules[name] = EpisodeStore(db)
        elif name == "skills":
            from memory_systems.skill_store import SkillStore
            _v5_modules[name] = SkillStore(db)
        elif name == "self_model":
            from memory_systems.self_model import SelfModel
            _v5_modules[name] = SelfModel(db)
        elif name == "reflection":
            from reflection.agent import ReflectionAgent
            _v5_modules[name] = ReflectionAgent(db)
        elif name == "cognitive":
            from cognitive.engine import CognitiveEngine
            _v5_modules[name] = CognitiveEngine(db)
        elif name == "ingestion":
            from ingestion.gateway import IngestGateway
            _v5_modules[name] = IngestGateway(db)
        elif name == "extractor":
            from ingestion.extractor import ConceptExtractor
            _v5_modules[name] = ConceptExtractor(db)
    return _v5_modules[name]


# Privacy: patterns to redact before storing
SENSITIVE_PATTERNS = [
    re.compile(r'(?:sk|pk|api[_-]?key)[_-]?[a-zA-Z0-9]{20,}', re.I),
    re.compile(r'(?:password|passwd|pwd|secret|token)\s*[:=]\s*\S+', re.I),
    re.compile(r'(?:AKIA|ASIA)[A-Z0-9]{16}'),  # AWS keys
    re.compile(r'ghp_[a-zA-Z0-9]{36}'),  # GitHub PAT
    re.compile(r'eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}'),  # JWT
    re.compile(r'(?:bearer|authorization)\s+\S+', re.I),
    re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),  # emails
    re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),  # credit cards
]
PRIVACY_TAG_RE = re.compile(r'<private>.*?</private>', re.DOTALL)


# ═══════════════════════════════════════════════════════════
# Storage
# ═══════════════════════════════════════════════════════════

class Store:
    def __init__(self, connection_factory: Callable[..., sqlite3.Connection] = sqlite3.Connection):
        for d in ["raw", "chroma", "transcripts", "queue", "backups", "extract-queue"]:
            (MEMORY_DIR / d).mkdir(parents=True, exist_ok=True)

        self.db_path = MEMORY_DIR / "memory.db"
        # check_same_thread=False lets background threads *read* through this
        # Connection (the enrichment worker calls Store._binary_search, and
        # SELECT does not open an implicit transaction).
        #
        # It does NOT make writes from another thread safe: sqlite3's legacy
        # isolation mode keeps the implicit BEGIN/COMMIT on the Connection, so
        # two threads issuing DML interleave into "cannot start a transaction
        # within a transaction" / "no transaction active". WAL and
        # busy_timeout solve contention *between* connections, not this. The
        # enrichment worker therefore opens its own connection to db_path.
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False, factory=connection_factory)
        self.db.row_factory = sqlite3.Row
        from memory_core.embeddings import EmbeddingProvider
        self.evidence_embedder = EmbeddingProvider()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # Avoid SQLITE_BUSY when reflection runner / dashboard hold reader locks.
        self.db.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        # Larger negative value = larger page cache (SQLite uses kibibytes when
        # negative). 20MB cache cuts disk I/O for repeat reads in hot path.
        self.db.execute("PRAGMA cache_size=-20000")
        self.db.execute(f"PRAGMA journal_size_limit={DB_JOURNAL_SIZE_LIMIT_BYTES}")

        # Opportunistic WAL reclaim: a full TRUNCATE checkpoint only succeeds
        # with zero other readers holding a snapshot, which realistically
        # only happens when the first session of the day opens the db. Use a
        # short timeout so a busy result (the common case) fails fast instead
        # of stalling startup.
        # A successful TRUNCATE reports (0, 0, 0), so the size to log is read
        # from the file before the checkpoint.
        wal_path = self.db_path.with_name(self.db_path.name + "-wal")
        wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        try:
            self.db.execute(f"PRAGMA busy_timeout={DB_STARTUP_CHECKPOINT_TIMEOUT_MS}")
            row = self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row and row[0] != 0:
                LOG(f"WAL checkpoint busy: {row[1]} log frames, {row[2]} checkpointed")
            elif wal_bytes:
                LOG(f"WAL checkpoint: truncated {wal_bytes} bytes")
        except sqlite3.OperationalError as exc:
            LOG(f"WAL checkpoint skipped: {exc}")
        finally:
            self.db.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")

        self._schema()
        self._migrate()
        self._apply_sql_migrations()
        self._check_fts()

        from memory_core.vector_search import DEFAULT_VECTOR_CACHE_BYTES, VectorSearch
        self._vector_search = VectorSearch(
            self.db, max_cache_bytes=int(os.environ.get("MEMORY_VECTOR_CACHE_BYTES", DEFAULT_VECTOR_CACHE_BYTES)),
        )

        self.chroma = None
        # v11 §J — per-space Chroma collections so different embedding-space
        # models (text 384d, code 768d, ...) can coexist without the HNSW
        # index blowing up on a dimension mismatch. Backwards-compat: the
        # default `knowledge` collection IS the text collection, so v10.x
        # rows keep working unchanged.
        self.chroma_per_space: dict[str, object] = {}
        self._chroma_client = None
        if HAS_CHROMA and USE_BINARY_SEARCH != "true":
            try:
                import chromadb  # noqa: PLC0415 — deferred: see HAS_CHROMA above

                self._chroma_client = chromadb.PersistentClient(path=str(MEMORY_DIR / "chroma"))
                # Default collection name `knowledge` == text space (legacy alias).
                self.chroma = self._chroma_client.get_or_create_collection(
                    "knowledge", metadata={"hnsw:space": "cosine"}
                )
                self.chroma_per_space["text"] = self.chroma
            except Exception as e:
                LOG(f"ChromaDB init: {e}")

        self._embedder = None
        self._fastembed_model = None  # lazy init
        self._ollama_available = None  # lazy check
        self._binary_search_ready = None  # lazy check
        self._embed_provider = None  # lazy init (pluggable provider)

        # Query cache (LRU with TTL) — imported at top level
        if HAS_CACHE:
            self.cache = QueryCache(maxsize=200, default_ttl=300)
            LOG("Query cache: enabled (200 entries, 5min TTL)")
        else:
            self.cache = None
            LOG("Query cache: disabled (cache module not found)")

        # v9.0 two-level cache (lane A2) — gated by V9_CACHE_L1/L2_ENABLED flags.
        # Instantiation is cheap + idempotent even when flags are OFF
        # (public API turns into no-op), so we always construct it.
        try:
            from cache_layer import TwoLevelCache as _V9TwoLevelCache
            self.v9_cache = _V9TwoLevelCache(db_path=str(MEMORY_DIR / "memory.db"))
        except Exception as _e:  # pragma: no cover — never hit in CI
            LOG(f"v9 cache init failed: {_e}")
            self.v9_cache = None

        # Eagerly initialize embedding mode (not lazy)
        self._embed_mode = self._init_embed_mode()
        LOG(f"Embedding mode: {self._embed_mode}")

        # Safety gate: refuse to run if configured provider's dim() differs
        # from the dim already stored in the embeddings table. Prevents
        # silent corruption when MEMORY_EMBED_PROVIDER is swapped on a
        # live DB without re-embedding.
        self._check_embed_dim_compat()

        # v10 — replay any save_knowledge intents that were created but
        # never committed (process crash mid-save). Idempotent: dedup
        # path swallows re-inserts of the same content.
        self._reconcile_outbox_at_startup()

        # v10.1 — Async enrichment worker (inbox/outbox style). When
        # MEMORY_ASYNC_ENRICHMENT=true, the heavy LLM-bound stages of
        # save_knowledge (quality gate, entity dedup audit, contradiction
        # detector, episodic event linking, wiki refresh) run in this
        # background thread instead of synchronously. Default OFF so
        # existing deployments keep their current latency profile.
        try:
            import enrichment_worker as _ew
            self._enrich_worker = _ew.start_worker(self)
        except Exception as e:
            LOG(f"enrichment worker init skipped: {e}")
            self._enrich_worker = None

    @property
    def embedder(self):
        if self._embedder is None and HAS_ST:
            try:
                from cpu_budget import configure_torch_threads
                configure_torch_threads()
                from sentence_transformers import (  # noqa: PLC0415 — deferred
                    SentenceTransformer,
                )

                self._embedder = SentenceTransformer(EMBEDDING_MODEL)
            except Exception as error:
                logging.getLogger(__name__).warning('SentenceTransformer initialization failed',
                                                    extra={'error_type': type(error).__name__})
        return self._embedder

    @property
    def fastembed(self):
        """Lazy-init FastEmbed model."""
        if self._fastembed_model is None and HAS_FASTEMBED:
            try:
                from memory_core.fastembed_loader import load_model
                self._fastembed_model = load_model(TextEmbedding, FASTEMBED_MODEL,
                                                   threads=_v11_config.get_embed_threads())
                LOG(f"FastEmbed: loaded {FASTEMBED_MODEL}")
            except Exception as e:
                LOG(f"FastEmbed: init failed ({e})")
                self._fastembed_model = False  # sentinel to avoid retries
        return self._fastembed_model if self._fastembed_model is not False else None

    @property
    def embed_provider(self):
        """Lazy-init configured EmbeddingProvider (fastembed|openai|cohere).

        Cached on the Store. Returns None when the configured provider is
        unavailable at runtime (e.g. missing API key, fastembed import
        failure) — callers must fall back to the legacy embed() paths.
        """
        if self._embed_provider is not None:
            return self._embed_provider if self._embed_provider is not False else None
        try:
            import config as _cfg
            from embed_provider import make_embed_provider
            provider_name = _cfg.get_embed_provider()
            self._embed_provider = make_embed_provider(provider_name)
            LOG(f"Embed provider: {provider_name} ({self._embed_provider.name})")
        except Exception as e:  # noqa: BLE001
            LOG(f"Embed provider init failed: {e}")
            self._embed_provider = False
            return None
        return self._embed_provider

    def _provider_embed(self, texts):
        """Embed via configured provider. Returns None on failure."""
        provider = self.embed_provider
        if provider is None:
            return None
        try:
            return provider.embed(list(texts))
        except Exception as e:  # noqa: BLE001
            LOG(f"Embed provider error: {e}")
            return None

    def _active_embed_model_name(self):
        """Name of the model currently writing into the embeddings table."""
        mode = self._embed_mode
        if mode in ("openai", "cohere"):
            provider = self.embed_provider
            if provider is not None:
                return getattr(provider, "model", None) or getattr(provider, "model_name", mode)
            return mode
        if mode == "fastembed":
            return FASTEMBED_MODEL
        if mode == "ollama":
            return OLLAMA_EMBED_MODEL
        return EMBEDDING_MODEL

    def _init_embed_mode(self):
        """Eagerly determine embedding mode at startup.

        Priority: configured provider → FastEmbed (legacy) → Ollama → ST.
        Default MEMORY_EMBED_PROVIDER=fastembed keeps behaviour identical
        to pre-provider code — we still report "fastembed" mode and the
        legacy self.fastembed property continues to back self.embed().
        """
        # Try configured provider first. If it's fastembed (default),
        # preserve legacy "fastembed" mode label so downstream code that
        # branches on self._embed_mode (e.g. ChromaDB fallback, model_name
        # tagging in _upsert_embedding) keeps working unchanged.
        try:
            import config as _cfg
            configured = _cfg.get_embed_provider()
        except Exception:
            configured = "fastembed"

        if configured == "fastembed":
            if HAS_FASTEMBED and self.fastembed:
                return "fastembed"
        elif configured in ("openai", "cohere"):
            provider = self.embed_provider
            if provider is not None and provider.available():
                return configured
            LOG(f"Embed provider {configured} unavailable — falling back to legacy chain")

        # Legacy fallback chain — kept so tests that run without any
        # provider-specific env vars still light up local fastembed.
        if HAS_FASTEMBED and self.fastembed:
            return "fastembed"

        if USE_OLLAMA_EMBED == "false":
            self._ollama_available = False
            if self.embedder:
                return "st"
            return "none"

        # Check Ollama availability
        if USE_OLLAMA_EMBED == "true" or USE_OLLAMA_EMBED == "auto":
            if self._check_ollama():
                return "ollama"

        # Fallback to SentenceTransformers
        if self.embedder:
            return "st"
        return "none"

    def _check_embed_dim_compat(self):
        """Refuse to boot if provider dim() mismatches stored embed_dim.

        Reads one row from the embeddings table. If the configured
        EmbeddingProvider reports a different dim than what's already
        stored, raises RuntimeError with a re-embed hint. An empty table
        (fresh DB) or an unavailable provider (no dim known yet) silently
        accept whatever comes next.
        """
        provider = self.embed_provider
        if provider is None:
            return
        provider_dim = 0
        try:
            provider_dim = provider.dim()
        except Exception:  # noqa: BLE001
            provider_dim = 0
        if not provider_dim:
            return  # provider dim unknown — skip gate

        try:
            row = self.db.execute(
                "SELECT embed_dim FROM embeddings WHERE COALESCE(embedding_space, 'text')='text' LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return  # table not ready — nothing to guard
        if row is None:
            return  # fresh DB — new dim is fine

        stored_dim = int(row[0])
        if stored_dim != provider_dim:
            raise RuntimeError(
                f"Embedding dimension mismatch: stored={stored_dim}, "
                f"provider={provider_dim}. Re-embed via "
                f"tools/reembed.py --dim {provider_dim} or revert "
                f"MEMORY_EMBED_PROVIDER."
            )

    def _chroma_collection_for(self, space: str):
        """v11 §J — lazily get-or-create the Chroma collection for an
        embedding space. Returns None when ChromaDB is disabled.

        Collections are named:
          text   → "knowledge"          (legacy default; v10.x rows live here)
          code   → "knowledge_code"
          log    → "knowledge_log"
          config → "knowledge_config"

        Different spaces can use different embedding dimensions (e.g. text
        384d MiniLM, code 768d jina-code) without breaking HNSW.
        """
        if not self._chroma_client:
            return None
        space = (space or "text").strip().lower()
        if space in self.chroma_per_space:
            return self.chroma_per_space[space]
        coll_name = "knowledge" if space == "text" else f"knowledge_{space}"
        try:
            coll = self._chroma_client.get_or_create_collection(
                coll_name, metadata={"hnsw:space": "cosine", "embedding_space": space},
            )
            self.chroma_per_space[space] = coll
            LOG(f"ChromaDB: collection '{coll_name}' ready for space={space}")
            return coll
        except Exception as e:
            LOG(f"ChromaDB collection init for space={space} failed: {e}")
            return None

    def _check_ollama(self):
        """Check if Ollama is running and has the embedding model."""
        if self._ollama_available is not None:
            return self._ollama_available
        if USE_OLLAMA_EMBED == "false":
            self._ollama_available = False
            return False
        try:
            import urllib.request
            req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
                models = [m.get("name", "").split(":")[0] for m in data.get("models", [])]
                self._ollama_available = OLLAMA_EMBED_MODEL in models
                if self._ollama_available:
                    LOG(f"Ollama embed: available ({OLLAMA_EMBED_MODEL})")
                else:
                    LOG(f"Ollama embed: model '{OLLAMA_EMBED_MODEL}' not found (available: {models[:5]})")
        except Exception as e:
            self._ollama_available = False
            LOG(f"Ollama embed: not available ({e})")
        return self._ollama_available

    def _ollama_embed(self, texts):
        """Get embeddings via Ollama API."""
        # v11 Phase 5 — defensive telemetry: any Ollama traffic on the hot
        # path is a fast-mode violation. The bench script asserts this stays 0.
        try:
            from memory_core.telemetry import counters as _v11_counters
            _v11_counters.bump("network_calls", float(len(texts) or 1))
        except Exception:
            pass
        try:
            import urllib.request
            results = []
            for text in texts:
                payload = json.dumps({"model": OLLAMA_EMBED_MODEL, "prompt": text}).encode()
                req = urllib.request.Request(
                    f"{OLLAMA_URL}/api/embeddings",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                    results.append(data["embedding"])
            return results
        except Exception as e:
            LOG(f"Ollama embed error: {e}")
            return None

    def _fastembed_embed(self, texts):
        """Get embeddings via FastEmbed (local, no server needed)."""
        try:
            # fastembed returns a generator, convert to list of lists
            embeddings = list(self.fastembed.embed(texts))
            return [emb.tolist() if hasattr(emb, 'tolist') else list(emb) for emb in embeddings]
        except Exception as e:
            LOG(f"FastEmbed error: {e}")
            return None

    def embed(self, texts):
        """Get embeddings.

        Dispatch order:
          1. Cloud providers (openai / cohere) via EmbeddingProvider when selected.
          2. Legacy FastEmbed → Ollama → SentenceTransformers chain.

        v9 A2: when ``V9_CACHE_L2_ENABLED`` is set, individual texts are
        looked up in the persistent ``embedding_cache`` table first; misses
        go through the provider and their vectors are stored back. Keyed
        by sha256(text) so identical inputs short-circuit the embedder.
        """
        # ── v9 L2 pre-lookup ───────────────────────────────
        l2 = getattr(self, "v9_cache", None)
        model_name = self._active_embed_model_name() if l2 is not None else ""
        cached: list[list[float] | None] = [None] * len(texts)
        missing_idx: list[int] = []
        if l2 is not None and l2.l2.enabled:
            for i, t in enumerate(texts):
                hit = l2.embed_get(t, expected_model=model_name or None)
                if hit is not None:
                    cached[i] = hit
                else:
                    missing_idx.append(i)
        else:
            missing_idx = list(range(len(texts)))

        if not missing_idx:
            return cached  # full L2 hit

        missing_texts = [texts[i] for i in missing_idx]

        # ── upstream embedding ─────────────────────────────
        def _compute(batch):
            if self._embed_mode in ("openai", "cohere"):
                r = self._provider_embed(batch)
                if r:
                    return r
            if self._embed_mode == "fastembed":
                r = self._fastembed_embed(batch)
                if r:
                    return r
                # v11: silent fallback to Ollama is forbidden in fast mode.
                # Power users opt back in with MEMORY_ALLOW_OLLAMA_IN_HOT_PATH=true.
                allow_ollama = os.environ.get(
                    "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH", "false"
                ).strip().lower() in ("1", "true", "yes", "on")
                if allow_ollama and self._check_ollama():
                    return self._ollama_embed(batch)
            if self._embed_mode == "ollama":
                r = self._ollama_embed(batch)
                if r:
                    return r
            # v11: SentenceTransformer fallback is also gated. Allowed when
            # the user explicitly opts in OR the mode is not "fastembed"
            # (i.e. they configured embed_mode=st on purpose).
            allow_st = (
                self._embed_mode != "fastembed"
                or os.environ.get(
                    "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH", "false"
                ).strip().lower() in ("1", "true", "yes", "on")
            )
            if allow_st and self._embed_mode in ("st", "ollama", "fastembed", "openai", "cohere") and self.embedder:
                try:
                    return self.embedder.encode(batch).tolist()
                except Exception:
                    pass
            return None

        fresh = _compute(missing_texts)
        if fresh is None:
            # Upstream failed — honour legacy contract and return None
            # only when nothing at all can be produced.
            return None if all(v is None for v in cached) else cached

        # ── merge + persist back into L2 ───────────────────
        for local_i, global_i in enumerate(missing_idx):
            cached[global_i] = fresh[local_i]
            if l2 is not None and l2.l2.enabled and fresh[local_i] is not None:
                try:
                    l2.embed_set(texts[global_i], fresh[local_i], model_name)
                except Exception:
                    pass

        return cached

    # ── Binary Quantization ──

    def _check_binary_search(self):
        """Check if binary search is ready (embeddings table populated)."""
        if self._binary_search_ready is not None:
            return self._binary_search_ready
        if USE_BINARY_SEARCH == "false":
            self._binary_search_ready = False
            return False
        try:
            count = self.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            active = self.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0]
        except Exception:
            self._binary_search_ready = False
            return False
        if USE_BINARY_SEARCH == "true":
            self._binary_search_ready = count > 0
        else:  # auto
            self._binary_search_ready = active > 0 and count >= active * 0.8
        if self._binary_search_ready:
            LOG(f"Binary search: enabled ({count} embeddings for {active} active records)")
        return self._binary_search_ready

    @staticmethod
    def _quantize_binary(embedding):
        """Convert float32 embedding to packed binary vector (N-dim → N/8 bytes)."""
        import numpy as np
        arr = np.array(embedding, dtype=np.float32)
        binary = np.where(arr > 0, 1, 0).astype(np.uint8)
        return np.packbits(binary).tobytes()

    @staticmethod
    def _float32_to_blob(embedding):
        """Convert float32 embedding list to BLOB."""
        return struct.pack(f'{len(embedding)}f', *embedding)

    @staticmethod
    def _blob_to_float32(blob):
        """Convert BLOB back to float32 list."""
        n = len(blob) // 4
        return list(struct.unpack(f'{n}f', blob))

    def _upsert_embedding(
        self,
        knowledge_id,
        embedding,
        model_name,
        *,
        provider: str = "fastembed",
        embedding_space: str = "text",
        content_type: str = "text",
        language: str | None = None,
    ):
        """Store binary + float32 vectors for a knowledge record.

        v11.0 §J — every row now carries `embedding_provider`, `embedding_space`,
        `content_type` and `language`. New callers pass them as keyword args;
        legacy callers that omit them get the safe defaults (text space,
        fastembed provider) so v10.x code paths keep working.
        """
        binary_blob = self._quantize_binary(embedding)
        float32_blob = self._float32_to_blob(embedding)
        now = utc_now()
        self.db.execute("""
            INSERT OR REPLACE INTO embeddings (
                knowledge_id, binary_vector, float32_vector,
                embed_model, embed_dim, created_at,
                embedding_provider, embedding_space, content_type, language
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            knowledge_id, binary_blob, float32_blob,
            model_name, len(embedding), now,
            provider, embedding_space, content_type, language,
        ))

    def _delete_embedding(self, knowledge_id):
        """Remove embedding for a knowledge record."""
        self.db.execute("DELETE FROM embeddings WHERE knowledge_id=?", (knowledge_id,))

    @staticmethod
    def _perf_snapshot() -> dict[str, float]:
        """v11 Phase 5 — return the in-process telemetry counter snapshot.

        Used by `bin/memory-bench` to assert `llm_calls == 0` and
        `network_calls == 0` over a benchmark run, and to read the
        accumulated `*_ms` timers for p50/p95/p99 reporting.
        """
        try:
            from memory_core.telemetry import counters
            return counters.snapshot()
        except Exception:
            return {}

    @staticmethod
    def _perf_reset() -> None:
        """v11 Phase 5 — reset every telemetry counter (for benchmarks)."""
        try:
            from memory_core.telemetry import counters
            counters.reset()
        except Exception:
            pass

    def _reconcile_outbox_at_startup(self):
        """v10 — replay any save_knowledge intents from a previous crash.

        Runs on every Store.__init__ after migrations apply. Idempotent
        thanks to the dedup path (`_find_duplicate`): a replayed payload
        whose record was already inserted returns the existing id (replays
        never replace) and the intent is closed as 'superseded'.
        """
        try:
            import outbox
            if not outbox.is_enabled():
                return
            # Confirm the table exists — bail silently otherwise so older
            # databases that haven't applied migration 017 yet don't crash.
            try:
                self.db.execute("SELECT 1 FROM write_intents LIMIT 1").fetchone()
            except Exception:
                return

            def _replay(payload):
                # `_from_outbox=True` short-circuits create_intent so the
                # replayed save doesn't create another intent for the
                # same payload.
                rid, was_dedup, *_ = self.save_knowledge(
                    payload.get("sid", ""),
                    payload.get("content", ""),
                    payload.get("ktype", "fact"),
                    project=payload.get("project", "general"),
                    tags=payload.get("tags") or [],
                    context=payload.get("context", "") or "",
                    branch=payload.get("branch", "") or "",
                    skip_dedup=False,
                    filter_name=payload.get("filter_name"),
                    source_format=payload.get("source_format", "auto"),
                    importance=payload.get("importance", "medium"),
                    skip_quality=True,           # don't re-score on replay
                    coref=False,                 # don't re-rewrite on replay
                    _from_outbox=True,
                )
                # On dedup, return None so reconcile records 'dedup' status.
                return None if was_dedup else rid

            counts = outbox.reconcile_pending(self.db, replay_fn=_replay)
            if any(counts.values()):
                LOG(
                    f"Outbox reconcile at startup: "
                    f"replayed={counts['replayed']} dedup={counts['dedup']} "
                    f"failed={counts['failed']} skipped={counts['skipped']}"
                )
        except Exception as exc:
            LOG(f"outbox reconcile skipped: {exc}")

    def _search_spaces(self, query, *, project=None, spaces=None, limit=10, kind="all", branch=None):
        from memory_core.embeddings import EmbeddingProvider
        from memory_core.telemetry import op_timer
        from memory_core.vector_math import (
            BINARY_CANDIDATE_MULTIPLIER,
            MAX_BINARY_CANDIDATES,
            MIN_BINARY_CANDIDATES,
        )

        groups = self._vector_search.groups(project)
        rankings = {}
        query_vectors = {}
        self._semantic_diagnostics = []
        for space, model, dimension in groups:
            if spaces and space not in spaces:
                continue
            try:
                with op_timer("search_space_ms"):
                    if space == "text":
                        actual_model = self._active_embed_model_name()
                    else:
                        provider = getattr(self, "_v11_embed_provider", None)
                        if provider is None:
                            provider = EmbeddingProvider()
                            self._v11_embed_provider = provider
                        actual_model = provider.active_model(space)
                    if actual_model != model:
                        raise ValueError(f"Embedding model changed for {space}; re-embed required")
                    uses_store = space == "text" or (
                        self._embed_mode == "fastembed" and actual_model == self._active_embed_model_name()
                    )
                    vector_key = ("store" if uses_store else space, actual_model)
                    if vector_key not in query_vectors:
                        if uses_store:
                            vectors = self.embed([query])
                            query_vectors[vector_key] = vectors[0] if vectors else []
                        else:
                            query_vectors[vector_key] = provider.embed_query(query, space=space)
                    vector = query_vectors[vector_key]
                    if len(vector) != dimension:
                        raise ValueError(f"Embedding model changed for {space}; re-embed required")
                    rankings[space] = self._binary_search(
                        vector, n_candidates=max(limit, MIN_BINARY_CANDIDATES, min(MAX_BINARY_CANDIDATES, limit * BINARY_CANDIDATE_MULTIPLIER)), project=project,
                        n_results=limit, embedding_spaces=[space], embedding_model=model,
                        exact_small_pool=True, kind=kind, branch=branch,
                    )
            except Exception as error:  # noqa: BLE001 — report failed spaces while other tiers remain usable
                self._semantic_diagnostics.append({"space": space, "error": str(error)})
                LOG(f"semantic space {space} unavailable: {error}")
        if len(rankings) == 1:
            return next(iter(rankings.values()))
        scores = {}
        for hits in rankings.values():
            for rank, (kid, _) in enumerate(hits, 1):
                scores[kid] = scores.get(kid, 0.0) + 1.0 / (60 + rank)
        return sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]

    def _binary_search(self, query_embedding, n_candidates=50, project=None, n_results=10,
                       embedding_spaces=None, embedding_model=None, exact_small_pool=False,
                       kind="all", branch=None):
        from memory_core.vector_math import EXACT_VECTOR_SCAN_LIMIT
        from memory_core.vector_search import VectorScope

        scope = VectorScope(
            dimension=len(query_embedding), project=project,
            spaces=tuple(sorted(embedding_spaces or ())), model=embedding_model,
            kind=kind, branch=branch,
        )
        return self._vector_search.search(
            query_embedding, scope, candidates=n_candidates, limit=n_results,
            exact_limit=int(os.environ.get("MEMORY_VECTOR_EXACT_LIMIT", EXACT_VECTOR_SCAN_LIMIT)) if exact_small_pool else 0,
        )

    def _schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                project TEXT DEFAULT 'general', status TEXT DEFAULT 'open',
                summary TEXT, log_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, type TEXT NOT NULL,
                content TEXT NOT NULL, context TEXT DEFAULT '',
                project TEXT DEFAULT 'general', tags TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active', superseded_by INTEGER,
                confidence REAL DEFAULT 1.0, source TEXT DEFAULT 'explicit',
                created_at TEXT NOT NULL, last_confirmed TEXT,
                recall_count INTEGER DEFAULT 0, last_recalled TEXT
            );
            CREATE TABLE IF NOT EXISTS relations (
                from_id INTEGER, to_id INTEGER, type TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS timeline (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, ts TEXT NOT NULL,
                event TEXT NOT NULL, summary TEXT NOT NULL,
                details TEXT DEFAULT '', project TEXT DEFAULT 'general', files TEXT DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_k_status ON knowledge(status);
            CREATE INDEX IF NOT EXISTS idx_k_type ON knowledge(type);
            CREATE INDEX IF NOT EXISTS idx_k_project ON knowledge(project);
            CREATE INDEX IF NOT EXISTS idx_k_session ON knowledge(session_id);
            CREATE INDEX IF NOT EXISTS idx_k_last_confirmed ON knowledge(last_confirmed);
            CREATE INDEX IF NOT EXISTS idx_rel_from ON relations(from_id);
            CREATE INDEX IF NOT EXISTS idx_rel_to ON relations(to_id);
            CREATE INDEX IF NOT EXISTS idx_t_session ON timeline(session_id);
            CREATE INDEX IF NOT EXISTS idx_s_started ON sessions(started_at);
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                content, context, tags, content='knowledge', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS k_fts_i AFTER INSERT ON knowledge BEGIN
                INSERT INTO knowledge_fts(rowid,content,context,tags)
                VALUES (new.id,new.content,new.context,new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS k_fts_u AFTER UPDATE ON knowledge BEGIN
                INSERT INTO knowledge_fts(knowledge_fts,rowid,content,context,tags)
                VALUES ('delete',old.id,old.content,old.context,old.tags);
                INSERT INTO knowledge_fts(rowid,content,context,tags)
                VALUES (new.id,new.content,new.context,new.tags);
            END;
            CREATE TABLE IF NOT EXISTS embeddings (
                knowledge_id INTEGER PRIMARY KEY,
                binary_vector BLOB NOT NULL,
                float32_vector BLOB NOT NULL,
                embed_model TEXT NOT NULL,
                embed_dim INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        self.db.commit()

    def _migrate(self):
        """Add columns/tables that may not exist in older databases."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(knowledge)").fetchall()}
        if "recall_count" not in cols:
            self.db.execute("ALTER TABLE knowledge ADD COLUMN recall_count INTEGER DEFAULT 0")
        if "last_recalled" not in cols:
            self.db.execute("ALTER TABLE knowledge ADD COLUMN last_recalled TEXT")
        # v4.0: branch-aware context
        if "branch" not in cols:
            self.db.execute("ALTER TABLE knowledge ADD COLUMN branch TEXT DEFAULT ''")
            LOG("Migration: added branch to knowledge table")
        # Claude Code subagent lineage (agent_id / parent_agent_id) is owned by
        # migrations/028_agent_lineage.sql and deliberately NOT duplicated here.
        # It used to be in both places, and because _migrate() runs first, 028
        # then hit "duplicate column name: agent_id" on every fresh database —
        # aborting before its CREATE INDEX statements and never recording
        # itself, so it retried on every single startup. Reported by @juicetin
        # in #12. One schema change, one owner.

        sess_cols = {r[1] for r in self.db.execute("PRAGMA table_info(sessions)").fetchall()}
        if "branch" not in sess_cols:
            self.db.execute("ALTER TABLE sessions ADD COLUMN branch TEXT DEFAULT ''")
            LOG("Migration: added branch to sessions table")

        # Self-Improvement tables (v3.0)
        tables = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "errors" not in tables:
            self._create_self_improvement_tables()
            LOG("Migration: created self-improvement tables (errors, insights, rules)")
        else:
            # Migrate existing errors table if missing columns
            ecols = {r[1] for r in self.db.execute("PRAGMA table_info(errors)").fetchall()}
            if "resolved_at" not in ecols:
                self.db.execute("ALTER TABLE errors ADD COLUMN resolved_at TEXT")
                LOG("Migration: added resolved_at to errors table")
            # Ensure session index exists
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_e_session ON errors(session_id)")

        # v4.0: Observations table (lightweight auto-capture)
        if "observations" not in tables:
            self._create_observations_table()
            LOG("Migration: created observations table")

        # v4.1: Embeddings table for binary quantization
        if "embeddings" not in tables:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    knowledge_id INTEGER PRIMARY KEY,
                    binary_vector BLOB NOT NULL,
                    float32_vector BLOB NOT NULL,
                    embed_model TEXT NOT NULL,
                    embed_dim INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            LOG("Migration: created embeddings table for binary quantization")

        self.db.commit()

    def _create_self_improvement_tables(self):
        """Create errors, insights, rules tables for Self-Improving Agent."""
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                category TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',
                description TEXT NOT NULL,
                context TEXT DEFAULT '',
                fix TEXT DEFAULT '',
                project TEXT DEFAULT 'general',
                tags TEXT DEFAULT '[]',
                status TEXT DEFAULT 'open',
                resolved_at TEXT,
                insight_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_e_category ON errors(category);
            CREATE INDEX IF NOT EXISTS idx_e_project ON errors(project);
            CREATE INDEX IF NOT EXISTS idx_e_status ON errors(status);
            CREATE INDEX IF NOT EXISTS idx_e_session ON errors(session_id);
            CREATE INDEX IF NOT EXISTS idx_e_created ON errors(created_at DESC);

            CREATE VIRTUAL TABLE IF NOT EXISTS errors_fts USING fts5(
                description, context, fix, tags,
                content='errors', content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS e_fts_i AFTER INSERT ON errors BEGIN
                INSERT INTO errors_fts(rowid, description, context, fix, tags)
                VALUES (new.id, new.description, new.context, new.fix, new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS e_fts_u AFTER UPDATE ON errors BEGIN
                INSERT INTO errors_fts(errors_fts, rowid, description, context, fix, tags)
                VALUES ('delete', old.id, old.description, old.context, old.fix, old.tags);
                INSERT INTO errors_fts(rowid, description, context, fix, tags)
                VALUES (new.id, new.description, new.context, new.fix, new.tags);
            END;
            CREATE TRIGGER IF NOT EXISTS e_fts_d AFTER DELETE ON errors BEGIN
                INSERT INTO errors_fts(errors_fts, rowid, description, context, fix, tags)
                VALUES ('delete', old.id, old.description, old.context, old.fix, old.tags);
            END;

            CREATE TABLE IF NOT EXISTS insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                context TEXT DEFAULT '',
                category TEXT NOT NULL,
                importance INTEGER NOT NULL DEFAULT 2,
                confidence REAL NOT NULL DEFAULT 0.5,
                source_error_ids TEXT DEFAULT '[]',
                project TEXT DEFAULT 'general',
                tags TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                promoted_to_rule_id INTEGER,
                fire_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_i_status ON insights(status);
            CREATE INDEX IF NOT EXISTS idx_i_category ON insights(category);
            CREATE INDEX IF NOT EXISTS idx_i_project ON insights(project);
            CREATE INDEX IF NOT EXISTS idx_i_importance ON insights(importance DESC);

            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                context TEXT DEFAULT '',
                category TEXT NOT NULL,
                scope TEXT DEFAULT 'global',
                priority INTEGER NOT NULL DEFAULT 5,
                source_insight_id INTEGER,
                project TEXT DEFAULT 'general',
                tags TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                fire_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                success_rate REAL DEFAULT 0.0,
                last_fired TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_r_status ON rules(status);
            CREATE INDEX IF NOT EXISTS idx_r_scope ON rules(scope);
            CREATE INDEX IF NOT EXISTS idx_r_priority ON rules(priority DESC);
            CREATE INDEX IF NOT EXISTS idx_r_project ON rules(project);
        """)

    def _check_fts(self):
        """Verify FTS5 index integrity on startup, rebuild if corrupted."""
        try:
            self.db.execute(
                "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH '\"test\"'"
            ).fetchone()
        except Exception as e:
            LOG(f"FTS5 index corrupted: {e} — rebuilding...")
            try:
                self.db.execute(
                    "INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')"
                )
                self.db.commit()
                LOG("FTS5 rebuild: OK")
            except Exception as e2:
                LOG(f"FTS5 rebuild failed: {e2} — recreating from scratch...")
                from memory_core.fts_schema import recreate_knowledge_fts
                recreate_knowledge_fts(self.db)
                LOG("FTS5 recreated from scratch: OK")

    def _apply_sql_migrations(self):
        """Idempotently apply all migrations/*.sql in sorted order.

        Tracks applied migrations in a `migrations(version, description, applied_at)`
        table. Each file's basename prefix before the first underscore (e.g. "001"
        from "001_v5_schema.sql") is used as the version key. Safe to run at
        every startup — already-applied migrations are skipped.
        """
        import datetime as _dt
        from pathlib import Path as _Path

        migrations_dir = _Path(__file__).resolve().parent.parent / "migrations"
        if not migrations_dir.is_dir():
            raise RuntimeError(
                f"Bundled SQL migrations are missing: {migrations_dir}. "
                "The installation artifact is incomplete; reinstall "
                "total-agent-memory from a distribution that includes migrations/*.sql."
            )

        # Ensure tracker table
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS migrations (
                version TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            """
        )
        self.db.commit()

        applied = {
            r[0] for r in self.db.execute("SELECT version FROM migrations").fetchall()
        }

        for sql_path in sorted(migrations_dir.glob("*.sql")):
            # Version = digits before the first underscore (e.g. "001", "002")
            stem = sql_path.stem
            version = stem.split("_", 1)[0]
            if version in applied:
                continue
            description = stem[len(version) + 1 :].replace("_", " ") or stem
            script = sql_path.read_text(encoding="utf-8")
            from memory_core.schema_migration import (
                TRANSACTIONAL_SCHEMA_VERSION,
                Migration,
                MigrationRunner,
            )
            if int(version) >= TRANSACTIONAL_SCHEMA_VERSION:
                MigrationRunner(self.db).apply(Migration(version, description, script))
                LOG(f"Applied migration {version}: {description}")
                continue
            try:
                self.db.executescript(script)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    LOG(f"Migration {version} failed: {e}")
                    continue  # don't mark applied — will retry next startup
                # SQLite has no ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so a
                # DB where the column already exists (restored backup, column
                # added out-of-band) fails here and, because executescript is
                # all-or-nothing, never reaches the CREATE INDEX statements —
                # the migration then re-fails on every startup forever.
                # Replay statement by statement, skipping only the redundant
                # ALTERs, then record it as applied.
                if not self._replay_migration_skipping_existing(script, version):
                    continue
            except Exception as e:
                LOG(f"Migration {version} failed: {e}")
                continue  # don't mark applied — will retry next startup

            self.db.execute(
                "INSERT OR IGNORE INTO migrations (version, description, applied_at) "
                "VALUES (?, ?, ?)",
                (version, description, _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            self.db.commit()
            LOG(f"Applied migration {version}: {description}")

    def _replay_migration_skipping_existing(self, script: str, version: str) -> bool:
        """Re-run `script` one statement at a time, tolerating existing columns.

        Returns True when every statement either succeeded or was a redundant
        ``ADD COLUMN``; False (with a log line) on any other error, so the
        caller leaves the migration unrecorded and retries next startup.
        """
        for statement in script.split(";"):
            if not statement.strip():
                continue
            try:
                self.db.execute(statement)
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    continue
                LOG(f"Migration {version} failed on replay: {e}")
                self.db.rollback()
                return False
        return True

    def _create_observations_table(self):
        """Create lightweight observations table for auto-capture."""
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                observation_type TEXT NOT NULL DEFAULT 'change',
                summary TEXT NOT NULL,
                files_affected TEXT DEFAULT '[]',
                project TEXT DEFAULT 'general',
                branch TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_obs_session ON observations(session_id);
            CREATE INDEX IF NOT EXISTS idx_obs_project ON observations(project);
            CREATE INDEX IF NOT EXISTS idx_obs_type ON observations(observation_type);
            CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at DESC);
        """)

    @staticmethod
    def _sanitize_content(text):
        """Strip sensitive data: <private> tags and common secret patterns."""
        if not text:
            return text, False
        redacted = False
        # Strip <private>...</private> blocks
        cleaned = PRIVACY_TAG_RE.sub("[REDACTED]", text)
        if cleaned != text:
            redacted = True
            text = cleaned
        # Strip known sensitive patterns
        for pat in SENSITIVE_PATTERNS:
            new_text = pat.sub("[REDACTED]", text)
            if new_text != text:
                redacted = True
                text = new_text
        return text, redacted

    @staticmethod
    def _estimate_tokens(text):
        """Rough token estimate: ~4 chars per token for English."""
        return len(text) // 4 if text else 0

    def q(self, sql, params=()):
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def q1(self, sql, params=()):
        r = self.db.execute(sql, params).fetchone()
        return dict(r) if r else None

    def raw_append(self, sid, entry):
        entry["_ts"] = utc_now()
        p = MEMORY_DIR / "raw" / f"{sid}.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def session_start(self, sid, project="general", branch=""):
        now = utc_now()
        self.db.execute(
            "INSERT OR IGNORE INTO sessions (id,started_at,project,branch) VALUES (?,?,?,?)",
            (sid, now, project, branch))
        self.db.commit()

    def total_sessions(self):
        return self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    # ── Similarity & Dedup ──

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        """Word-level Jaccard similarity."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    @staticmethod
    def _fuzzy_ratio(a: str, b: str) -> float:
        """Sequence-based fuzzy similarity (SequenceMatcher)."""
        return SequenceMatcher(None, a.lower(), b.lower()).ratio()

    @staticmethod
    def _fts_escape(word: str) -> str:
        """Escape a word for FTS5 by wrapping in double quotes."""
        return '"' + word.replace('"', '""') + '"'

    def _find_duplicate(self, content, ktype, project):
        """Return the id of an active record that states exactly what `content` states.

        Near-duplicates are not duplicates: a record that differs in one value
        ("... is Argentina" -> "... is Armenia") is an update and must be stored.
        """
        from memory_core.dedup import candidate_match_query, repeats
        from memory_core.fts_schema import project_token

        try:
            terms = candidate_match_query(content)
            if not terms:
                return None
            fts_q = f"content : ({terms}) AND fts_project : {project_token(project)}"
            # Materialized so SQLite runs MATCH once instead of once per
            # project row.
            rows = self.q("""
                WITH f AS MATERIALIZED (
                    SELECT rowid AS id FROM knowledge_fts WHERE knowledge_fts MATCH ?
                )
                SELECT k.id, k.content FROM f JOIN knowledge k ON k.id=f.id
                WHERE k.status='active' AND k.project=? AND k.type=?
                ORDER BY k.id
            """, (fts_q, project, ktype))
            for row in rows:
                if repeats(content, row["content"]):
                    return row["id"]
        except Exception as e:
            LOG(f"Dedup FTS error: {e}")
        return None

    # ── Decay Scoring ──

    @staticmethod
    def _decay_factor(last_confirmed_str: str, half_life_days: int = 90) -> float:
        """Exponential decay: score *= e^(-days / half_life). Range [0.01, 1.0]."""
        if not last_confirmed_str:
            return 0.5
        try:
            lc = datetime.fromisoformat(last_confirmed_str.replace("Z", "+00:00"))
            now = datetime.now(lc.tzinfo) if lc.tzinfo else datetime.now(timezone.utc).replace(tzinfo=None)
            days = (now - lc.replace(tzinfo=None)).days if not lc.tzinfo else (now - lc).days
            return max(0.01, math.exp(-days * math.log(2) / half_life_days))
        except Exception:
            return 0.5

    # ── CRUD ──

    def save_knowledge(self, sid, content, ktype, project="general", tags=None,
                        context="", branch="", skip_dedup=False, filter_name=None,
                        importance="medium", skip_quality=False, coref=None,
                        agent_id=None, parent_agent_id=None,
                        _from_outbox=False, source_format: Literal['auto', 'conversation'] = 'auto',
                        repeat: Literal['replace', 'confirm'] = 'replace'):
        """Save knowledge. Returns
        ``(record_id, was_deduplicated, was_redacted, private_sections, quality_meta)``.

        Optional `filter_name` runs the content through a TOML-defined
        rtk-style pipeline BEFORE dedup/save — shrinks noisy CLI output
        (pytest, cargo, etc.) while a hard whitelist keeps URLs/paths/code.

        v10: a quality gate (`src/quality_gate.py`) scores the record
        synchronously before dedup. Records below the configured threshold
        are dropped; in that case ``record_id`` is ``None`` and
        ``quality_meta['decision'] == 'drop'``. ``skip_quality=True`` bypasses
        the gate (used by `memory_update` / `self_reflect` where the content
        is already validated). The optional ``importance`` field
        (``critical|high|medium|low``, default ``medium``) is persisted on
        the row and consumed by `fusion.py` to boost recall ranking.
        """
        # v11 Phase 5 — total wall-clock for this save. Recorded into
        # `memory_core.telemetry.counters['save_total_ms']` so the bench
        # script can report p50/p95/p99 without re-instrumenting.
        try:
            from memory_core.telemetry import op_timer as _v11_op_timer
        except Exception:
            from contextlib import (
                nullcontext as _v11_op_timer,  # type: ignore[assignment]
            )

            def _v11_op_timer(_name):  # type: ignore[no-redef]
                from contextlib import nullcontext
                return nullcontext()

        if repeat not in ("replace", "confirm"):
            raise ValueError(f"repeat must be 'replace' or 'confirm', got {repeat!r}")
        with _v11_op_timer("save_total_ms"):
            return self._save_knowledge_impl(
                sid, content, ktype, project=project, tags=tags,
                context=context, branch=branch, skip_dedup=skip_dedup,
                filter_name=filter_name, importance=importance,
                skip_quality=skip_quality, coref=coref,
                agent_id=agent_id, parent_agent_id=parent_agent_id,
                _from_outbox=_from_outbox, source_format=source_format,
                repeat=repeat,
            )

    def _save_knowledge_impl(self, sid, content, ktype, project="general", tags=None,
                              context="", branch="", skip_dedup=False, filter_name=None,
                              importance="medium", skip_quality=False, coref=None,
                              agent_id=None, parent_agent_id=None,
                              _from_outbox=False, source_format: Literal['auto', 'conversation'] = 'auto',
                              repeat: Literal['replace', 'confirm'] = 'replace'):
        """Underlying implementation; wrapped by `save_knowledge` for telemetry."""
        if source_format not in ('auto', 'conversation'):
            raise ValueError('Unsupported source format')
        if source_format == 'conversation' and filter_name:
            raise ValueError('Conversation sources cannot use a destructive content filter')
        now = utc_now()

        # Inline <private>...</private> tag redaction (P0.1) — BEFORE autofilter/dedup/sanitize
        private_sections = 0
        try:
            from privacy_filter import redact_private_sections as _rps
            content, _pc = _rps(content)
            context, _px = _rps(context)
            private_sections = _pc + _px
            if private_sections > 0:
                sys.stderr.write(f"[private-tag] redacted {private_sections} sections\n")
                try:
                    self.db.execute(
                        "UPDATE privacy_counters SET value = value + ?, updated_at = ? WHERE key = ?",
                        (private_sections, now, "private_redactions_total"),
                    )
                    self.db.commit()
                except Exception as e:
                    LOG(f"privacy_counters update error: {e}")
        except Exception as e:
            raise ValueError("Private content redaction failed; nothing was persisted") from e

        # Optional content filter (token-saving preprocessor)
        # Auto-detect filter when caller didn't specify one.
        if not filter_name and source_format == 'auto':
            try:
                from autofilter import detect_filter
                filter_name = detect_filter(content)
                if filter_name:
                    LOG(f"autofilter detected: {filter_name}")
            except Exception as e:
                LOG(f"autofilter error: {e}")
        filter_stats = None
        if filter_name:
            try:
                from pathlib import Path as _Path

                from content_filter import filter_with_stats as _fws
                from content_filter import load_filter_config
                cfg_path = _Path(__file__).resolve().parent.parent / "filters" / f"{filter_name}.toml"
                if cfg_path.exists():
                    cfg = load_filter_config(cfg_path)
                    safety = cfg.get("safety", "strict")
                    content, filter_stats = _fws(content, cfg.get("stages", {}), safety=safety)
                    filter_stats["filter_name"] = filter_name
                    LOG(
                        f"filter '{filter_name}' applied: "
                        f"{filter_stats['input_chars']} -> {filter_stats['output_chars']} "
                        f"chars (-{filter_stats['reduction_pct']}%)"
                    )
                else:
                    LOG(f"filter '{filter_name}' not found at {cfg_path}")
            except Exception as e:
                LOG(f"filter '{filter_name}' failed: {e}")

        # Privacy stripping
        content, redacted_c = self._sanitize_content(content)
        context, redacted_x = self._sanitize_content(context)
        was_redacted = redacted_c or redacted_x

        # Persist only the sanitized replay payload. _from_outbox=True
        # signals a replay from the reconciler — skip intent creation
        # to avoid an infinite intent → replay → intent loop.
        intent = None
        if not _from_outbox:
            try:
                import outbox as _ob
                intent = _ob.create_intent(
                    self.db,
                    payload={
                        "sid": sid, "content": content, "ktype": ktype,
                        "project": project, "tags": list(tags or []),
                        "context": context or "", "branch": branch or "",
                        "filter_name": filter_name, "importance": importance,
                        "source_format": source_format,
                        "agent_id": agent_id, "parent_agent_id": parent_agent_id,
                    },
                    session_id=sid, content=content, ktype=ktype, project=project,
                )
            except (sqlite3.Error, ValueError, TypeError) as e:
                LOG(f"outbox create_intent skipped: {e}")
                intent = None

        # v10 — Coreference rewrite (opt-in). Expands pronouns/deictics in
        # the content using the last N records from the same session as
        # context, so semantic search later returns a self-contained
        # fragment instead of "after this it broke" mystery prose. Off by
        # default — caller must opt in (e.g. extract_transcript) or set
        # MEMORY_COREF_ENABLED=true. Always falls through on error.
        try:
            from coref_resolver import resolve as _coref_resolve
            cr = _coref_resolve(content, db=self.db, session_id=sid, coref=coref,
                               project=project)
            if cr.decision == "rewritten":
                LOG(f"coref rewritten record (latency_ms={cr.latency_ms})")
                content = cr.content
        except Exception as e:
            LOG(f"coref resolver skipped: {e}")

        # v10 — Canonical-vocabulary tag normalisation. Free-form input tags
        # are mapped to their canonical form (with the original kept alongside
        # so legacy synonym recall still works). Failures are non-fatal: the
        # gate must never block a save just because the vocabulary file moved.
        try:
            from canonical_tags import normalise_tags as _norm_tags
            tags = _norm_tags(tags or [])
        except Exception as e:
            LOG(f"canonical_tags normalisation skipped: {e}")
            tags = list(tags or [])

        # v10 — Pre-write entity dedup. Tags that are NOT canonical topics
        # get a second-chance lookup against existing graph_nodes via
        # embedding cosine; matches above MEMORY_ENTITY_DEDUP_THRESHOLD
        # (default 0.85) are rewritten to the canonical entity name. We
        # batch this AFTER canonical_tags so we don't waste embed calls on
        # tags the cheap path already resolved.
        entity_dedup_decisions: list = []
        try:
            import entity_dedup as _ed
            if _ed._enabled():
                # Lazily fetch candidates only if there are non-canonical tags
                # left to consider.
                cand_pool = _ed.production_candidates_query(self.db, project=project)
                if cand_pool and tags:
                    embed_fn = lambda texts: self.embed(texts) or None
                    new_tags, entity_dedup_decisions = _ed.canonicalize_entity_tags(
                        tags, candidates=cand_pool, embed_fn=embed_fn,
                    )
                    if new_tags != tags:
                        tags = new_tags
        except Exception as e:
            LOG(f"entity_dedup skipped: {e}")

        # v10.1 — Async enrichment switch. When MEMORY_ASYNC_ENRICHMENT=true,
        # we skip the synchronous quality gate (and the contradiction
        # detector / entity dedup audit / episodic event / wiki refresh
        # blocks further down). The expensive stages run in a background
        # worker thread that consumes `enrichment_queue`. Tradeoff: a
        # quality-gate 'drop' verdict marks the row as `quality_dropped`
        # *after* the INSERT, instead of preventing the INSERT outright.
        try:
            import enrichment_worker as _ew
            _async_enrich = _ew._enabled()
        except Exception:
            _async_enrich = False

        # v10 — Quality gate (Beever-style "6-Month Test"). Synchronous,
        # runs before dedup so dropped records never pollute storage. Gate
        # fails open: any LLM error or unavailability lets the save proceed.
        quality_meta: dict | None = None
        if not skip_quality and not _async_enrich:
            try:
                from quality_gate import log_decision, score_quality
                score = score_quality(content, ktype=ktype, project=project)
                quality_meta = {
                    "decision": score.decision,
                    "total": score.total,
                    "specificity": score.specificity,
                    "actionability": score.actionability,
                    "verifiability": score.verifiability,
                    "reason": score.reason,
                    "threshold": score.threshold,
                    "provider": score.provider,
                    "model": score.model,
                    "latency_ms": score.latency_ms,
                }
                if score.decision == "drop":
                    # Journal the rejection and bail before dedup/insert.
                    log_decision(
                        self.db, score, project=project, ktype=ktype,
                        content=content, knowledge_id=None,
                    )
                    LOG(
                        f"quality-gate dropped record (score={score.total:.2f}<"
                        f"{score.threshold}, reason={score.reason!r})"
                    )
                    # Outbox: a quality-gate drop is a *committed* outcome
                    # (the record will not be saved by any future replay
                    # either) — mark superseded with knowledge_id=None.
                    if intent is not None:
                        try:
                            import outbox as _ob
                            _ob.mark_superseded(self.db, intent, None)
                        except Exception:
                            pass
                    return None, False, was_redacted, private_sections, quality_meta
                # Non-drop decisions ('skip'/'error'/'pass') only journal
                # when MEMORY_QUALITY_LOG_ALL=1; helper handles the gating.
                log_decision(
                    self.db, score, project=project, ktype=ktype,
                    content=content, knowledge_id=None,
                )
            except Exception as e:
                # Quality gate must never break the underlying save.
                LOG(f"quality_gate error (continuing): {e}")

        # repeat='replace': a repeat of a stored statement replaces it, so the
        # statement carries the time it was last made: after "A", "B", "A" the
        # current value is A again, and readers that order by created_at must
        # see that. repeat='confirm' keeps the stored row and its authorship
        # (team stores record a confirmation). An outbox replay is the same
        # write, not a new statement, and always confirms.
        replaces = None
        if not skip_dedup:
            dup_id = self._find_duplicate(content, ktype, project)
            if dup_id and repeat == "replace" and not _from_outbox:
                replaces = dup_id
            elif dup_id:
                if source_format == 'conversation':
                    self.db.execute("UPDATE knowledge SET source_format='conversation' WHERE id=?", (dup_id,))
                self.db.execute("UPDATE knowledge SET last_confirmed=? WHERE id=?", (now, dup_id))
                self.db.commit()
                LOG(f"Dedup: updated last_confirmed for id={dup_id}")
                if intent is not None:
                    try:
                        import outbox as _ob
                        _ob.mark_superseded(self.db, intent, dup_id)
                    except Exception:
                        pass
                return dup_id, True, was_redacted, private_sections, quality_meta

        # Validate importance enum; fall back to 'medium' on bad input.
        importance_value = (importance or "medium").lower()
        if importance_value not in ("critical", "high", "medium", "low"):
            importance_value = "medium"

        cur = self.db.execute("""
            INSERT INTO knowledge (session_id,type,content,context,project,tags,source,confidence,
                                   created_at,last_confirmed,recall_count,branch,importance,
                                   agent_id,parent_agent_id,source_format)
            VALUES (?,?,?,?,?,?,'explicit',1.0,?,?,0,?,?,?,?,?)
        """, (sid, ktype, content, context, project, json.dumps(tags or []), now, now,
              branch or "", importance_value, agent_id, parent_agent_id, source_format))
        self.db.commit()
        rid = cur.lastrowid

        if replaces:
            self.db.execute(
                "UPDATE knowledge SET status='superseded', superseded_by=? WHERE id=? AND status='active'",
                (rid, replaces),
            )
            self._delete_embedding(replaces)
            self.db.commit()
            if self.chroma and not self._check_binary_search():
                try:
                    self.chroma.delete(ids=[str(replaces)])
                except Exception as e:  # noqa: BLE001 — chroma raises its own types; cleanup must not fail the save
                    LOG(f"Chroma delete of replaced id={replaces} failed: {e}")
            LOG(f"Repeat: id={rid} replaces id={replaces}")

        # v2.1.145 agent lineage — when both agent and parent ids are present
        # record a `spawned_by` assertion in the temporal KG so recall can
        # answer "which subagent ran for which parent". add_fact is idempotent
        # on (subject, predicate, object) so repeat saves are no-ops.
        if agent_id and parent_agent_id:
            try:
                from temporal_kg import TemporalKG
                TemporalKG(self.db).add_fact(
                    subject=str(agent_id),
                    predicate="spawned_by",
                    object=str(parent_agent_id),
                    confidence=1.0,
                    context=f"Observed during memory_save (record id={rid})",
                    source="agent-lineage",
                    project=project,
                    invalidate_previous=False,  # subagent lineage is immutable
                )
            except Exception as _kg_err:
                LOG(f"agent-lineage kg_add_fact skipped: {_kg_err}")

        # v11 §J Phase 5b — classify FIRST, then embed via the
        # per-space provider so code chunks really go through the code
        # model (jina-embeddings-v2-base-code, 768d) and text/log/config
        # go through the text model (MiniLM, 384d). Different sizes ⇒
        # different Chroma collections (handled below).
        try:
            from memory_core.classifier import classify as _v11_classify
            from memory_core.embedding_spaces import (
                model_for_space as _v11_model_for_space,
            )
            from memory_core.embedding_spaces import (
                resolve_space as _v11_resolve_space,
            )
            _cls = _v11_classify(content, source_format=source_format)
            _v11_content_type = _cls.type
            _v11_language = _cls.language
            _v11_space = _v11_resolve_space(_cls.type, _cls.language)
        except Exception as _cls_err:
            LOG(f"classifier failed, using text defaults: {_cls_err}")
            _v11_content_type = "text"
            _v11_language = None
            _v11_space = "text"

        # Per-space embedding via memory_core.EmbeddingProvider when the
        # space is anything but "text" — that lets jina-code (or whatever
        # the user configured) actually fire. The text path keeps using
        # the legacy `Store.embed` so the embedding cache, binary
        # quantization and ST fallback stay wired.
        embs: list[list[float]] | None = None
        model_name = self._active_embed_model_name()
        _v11_provider_name = self._embed_mode or "fastembed"
        if _v11_space != "text":
            try:
                from memory_core.embeddings import EmbeddingProvider as _V11Embed
                if not hasattr(self, "_v11_embed_provider") or self._v11_embed_provider is None:
                    self._v11_embed_provider = _V11Embed()
                v = self._v11_embed_provider.embed_query(
                    f"{content} {context}", space=_v11_space,
                )
                if v:
                    embs = [v]
                    model_name = self._v11_embed_provider.active_model(_v11_space)
                    _v11_provider_name = "fastembed"  # EmbeddingProvider is FastEmbed-first
            except Exception as _emb_err:
                LOG(f"per-space embed for space={_v11_space} fell back to text: {_emb_err}")
                embs = None  # fall through to legacy text path
        if embs is None:
            embs = self.embed([f"{content} {context}"])
            if embs and _v11_space != "text":
                # Honest record-keeping: the row IS in `text` model space
                # because per-space encoder failed — degrade gracefully.
                _v11_space = "text"
        if embs:
            self._upsert_embedding(
                rid, embs[0], model_name,
                provider=_v11_provider_name,
                embedding_space=_v11_space,
                content_type=_v11_content_type,
                language=_v11_language,
            )
            self.db.commit()
            if self._chroma_client and not self._check_binary_search():
                try:
                    coll = self._chroma_collection_for(_v11_space)
                    if coll is not None:
                        coll.upsert(
                            ids=[str(rid)], embeddings=embs, documents=[content],
                            metadatas=[{
                                "type": ktype, "project": project, "status": "active",
                                "session_id": sid, "created_at": now, "confidence": 1.0,
                                # v11 §J multi-embedding-space metadata
                                "embedding_provider": _v11_provider_name,
                                "embedding_model": model_name,
                                "embedding_dimension": len(embs[0]),
                                "embedding_space": _v11_space,
                                "content_type": _v11_content_type,
                                "language": _v11_language or "",
                            }])
                except Exception as _ce:
                    LOG(f"chroma per-space upsert failed: {_ce}")
        # Auto-link to knowledge graph
        try:
            from graph.auto_link import auto_link_knowledge
            auto_link_knowledge(self.db, rid, content, project,
                                tags if isinstance(tags, list) else json.loads(tags or "[]"))
        except Exception as e:
            LOG(f"Auto-link error: {e}")

        # v10 — Episodic link: spawn an Event node and connect every
        # entity-typed graph node linked to this record via MENTIONED_IN.
        # Lets future queries answer "show me saves where Bob and
        # Postgres were mentioned together" in one graph traversal.
        # Skipped in async mode — the worker handles it.
        if not _async_enrich:
            try:
                import episodic as _ep
                _ep.record_save_event(self.db, knowledge_id=rid,
                                      project=project, session_id=sid)
            except Exception as e:
                LOG(f"episodic event creation skipped: {e}")

        # Enqueue for async deep triple extraction (processed by reflection agent)
        try:
            from triple_extraction_queue import TripleExtractionQueue
            TripleExtractionQueue(self.db).enqueue(rid)
        except Exception as e:
            LOG(f"Triple-enqueue error: {e}")

        # Enqueue for async deep metadata enrichment (entities/intent/topics)
        try:
            from deep_enrichment_queue import DeepEnrichmentQueue
            DeepEnrichmentQueue(self.db).enqueue(rid)
        except Exception as e:
            LOG(f"Deep-enrich-enqueue error: {e}")

        # Enqueue for async multi-representation embedding generation (GEM-RAG)
        try:
            from representations_queue import RepresentationsQueue
            RepresentationsQueue(self.db).enqueue(rid)
        except Exception as e:
            LOG(f"Repr-enqueue error: {e}")

        # Ping the reflection runner (watched by LaunchAgent). The runner
        # debounces: within the debounce window, multiple saves coalesce into
        # one drain run. If no agent is watching, this is a cheap no-op.
        try:
            trigger_path = MEMORY_DIR / ".reflect-pending"
            trigger_path.touch()
        except Exception as e:
            LOG(f"Reflect-trigger touch failed: {e}")

        # Persist filter savings metric if a filter ran
        if filter_stats:
            try:
                self.db.execute(
                    """INSERT INTO filter_savings
                         (knowledge_id, filter_name, input_chars, output_chars,
                          reduction_pct, safety, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        rid,
                        filter_stats.get("filter_name", "unknown"),
                        filter_stats.get("input_chars", 0),
                        filter_stats.get("output_chars", 0),
                        filter_stats.get("reduction_pct", 0.0),
                        filter_stats.get("safety", "strict"),
                        now,
                    ),
                )
                self.db.commit()
            except Exception as e:
                LOG(f"filter_savings log error: {e}")

        # Backfill the audit log row with the freshly minted knowledge_id so
        # 'pass' rows in quality_gate_log can be joined back to records.
        if quality_meta and quality_meta.get("decision") == "pass":
            try:
                self.db.execute(
                    "UPDATE quality_gate_log SET knowledge_id=? "
                    "WHERE id = (SELECT MAX(id) FROM quality_gate_log "
                    "            WHERE knowledge_id IS NULL AND decision='pass')",
                    (rid,),
                )
                self.db.commit()
            except Exception as e:
                LOG(f"quality_gate_log backfill error: {e}")

        # v10 — Persist entity-dedup decisions to audit log now that the
        # knowledge_id is known. Skipped in async mode — the worker
        # re-walks the canonicalisation and writes decisions itself.
        if entity_dedup_decisions and not _async_enrich:
            try:
                import entity_dedup as _ed
                _ed.log_decisions(
                    self.db, entity_dedup_decisions,
                    knowledge_id=rid, project=project,
                )
            except Exception as e:
                LOG(f"entity_dedup audit log skipped: {e}")

        # v10 — Auto-contradiction detection. For decision/solution saves,
        # run a semantic search against existing same-type records in the
        # same project and ask the LLM whether the new record supersedes
        # any of them. ≥0.8 confidence → automatic supersession; 0.5-0.8 →
        # 'flagged' for human review. Fail-open: any error simply lets the
        # save complete without supersession.
        # Skipped in async mode — the worker runs the same sweep without
        # blocking the caller.
        if not _async_enrich:
            try:
                from contradiction_detector import (
                    apply_and_log as _cd_apply,
                )
                from contradiction_detector import (
                    detect_contradictions as _cd_detect,
                )
                from contradiction_detector import (
                    production_candidates_query as _cd_fetch,
                )
                from contradiction_detector import (
                    production_llm_call as _cd_llm,
                )
                from contradiction_detector import (
                    should_run as _cd_should_run,
                )
                ok, reason = _cd_should_run(ktype)
                if ok and embs:
                    cand_pool = self._binary_search(embs[0], n_candidates=50,
                                                    project=project, n_results=10)
                    # Drop self from the candidate pool — embedding search may
                    # surface the row we just inserted.
                    cand_pool = [(cid, cos) for cid, cos in cand_pool if cid != rid]
                    if cand_pool:
                        verdicts = _cd_detect(
                            content,
                            ktype=ktype, project=project,
                            candidate_pool=cand_pool,
                            fetch_candidates=lambda ids: _cd_fetch(
                                self.db, project=project, ktype=ktype, candidate_ids=ids
                            ),
                            llm_fn=_cd_llm,
                        )
                        if verdicts:
                            counts = _cd_apply(self.db, verdicts, new_id=rid)
                            if counts.get("superseded"):
                                LOG(
                                    f"contradiction-detector superseded "
                                    f"{counts['superseded']} record(s) on save id={rid}"
                                )
            except Exception as e:
                LOG(f"contradiction_detector skipped: {e}")

        # v10 — Outbox: mark intent as committed now that all the side
        # effects (insert, embed, queues, contradiction sweep) are done.
        if intent is not None:
            try:
                import outbox as _ob
                _ob.mark_committed(self.db, intent, rid)
            except Exception as e:
                LOG(f"outbox mark_committed skipped: {e}")

        # v10 — Project wiki auto-refresh. Off by default; opt in via
        # `MEMORY_WIKI_AUTO_REFRESH_EVERY_N=10` to regenerate the
        # per-project markdown digest after every 10th save_knowledge
        # commit. Wikis live at <MEMORY_DIR>/wikis/<project>.md.
        # Skipped in async mode — the worker handles it (still cheap when
        # auto-refresh is off, but keeps the sync path strictly minimal).
        if not _async_enrich:
            try:
                import project_wiki as _pw
                _pw.maybe_auto_refresh(
                    self.db, project=project, save_count=rid,
                    output_dir=str(MEMORY_DIR / "wikis"),
                )
            except Exception as e:
                LOG(f"project_wiki auto-refresh skipped: {e}")

        # v10.1 — In async mode, hand off the heavy work to the worker.
        # Quality meta is reported as 'pending' so the MCP client knows
        # the gate verdict will arrive later (visible via memory_history
        # / quality_gate_log).
        if _async_enrich:
            try:
                import enrichment_worker as _ew
                _ew.enqueue(
                    self.db,
                    knowledge_id=rid,
                    session_id=sid,
                    project=project,
                    ktype=ktype,
                    content_snapshot=content,
                    tags_snapshot=tags or [],
                    importance=importance_value,
                    skip_quality=skip_quality,
                )
                quality_meta = {
                    "decision": "pending",
                    "reason": "queued for async enrichment",
                }
            except Exception as e:
                LOG(f"enrichment enqueue failed (sync fallback would re-run heavy stages): {e}")

        return rid, replaces is not None, was_redacted, private_sections, quality_meta

    def supersede_values(self, rid, content, ktype, project):
        """Retire active records that `content` (record `rid`) states a new value for.

        Opt-in per save (`memory_save(supersede=true)`); see
        memory_core.dedup.updates_value for the rule. Returns the retired ids.
        """
        from memory_core.dedup import prefix_match_query, updates_value
        from memory_core.fts_schema import project_token

        terms = prefix_match_query(content)
        if not terms:
            return []
        rows = self.db.execute("""
            WITH f AS MATERIALIZED (
                SELECT rowid AS id FROM knowledge_fts WHERE knowledge_fts MATCH ?
            )
            SELECT k.id, k.content FROM f JOIN knowledge k ON k.id=f.id
            WHERE k.status='active' AND k.project=? AND k.type=? AND k.id<>?
            ORDER BY k.id DESC LIMIT ?
        """, (f"content : ({terms}) AND fts_project : {project_token(project)}",
              project, ktype, rid, SUPERSEDE_CANDIDATES)).fetchall()
        retired = [row[0] for row in rows if updates_value(content, row[1])]
        for old_id in retired:
            self.db.execute(
                "UPDATE knowledge SET status='superseded', superseded_by=? WHERE id=? AND status='active'",
                (rid, old_id))
            self._delete_embedding(old_id)
        self.db.commit()
        if retired and self.chroma and not self._check_binary_search():
            try:
                self.chroma.delete(ids=[str(i) for i in retired])
            except Exception as e:  # noqa: BLE001 — chroma raises its own types; cleanup must not fail the save
                LOG(f"Chroma delete of superseded ids={retired} failed: {e}")
        return retired

    def bump_recall(self, ids):
        """Strengthen memories that are recalled (spaced repetition effect)."""
        now = utc_now()
        for kid in ids:
            self.db.execute(
                "UPDATE knowledge SET recall_count=recall_count+1, last_recalled=? WHERE id=?",
                (now, kid))
        self.db.commit()

    # ── Consolidation ──

    def find_similar_groups(self, project=None, threshold=0.75):
        """Find groups of similar active knowledge for consolidation."""
        conds = ["status='active'"]
        params = []
        if project:
            conds.append("project=?")
            params.append(project)
        rows = self.q(f"SELECT id, content, type, project FROM knowledge WHERE {' AND '.join(conds)} ORDER BY id", params)

        groups = []
        used = set()
        for i, a in enumerate(rows):
            if a["id"] in used:
                continue
            group = [a]
            for b in rows[i+1:]:
                if b["id"] in used or b["type"] != a["type"] or b["project"] != a["project"]:
                    continue
                if self._jaccard(a["content"], b["content"]) > threshold:
                    group.append(b)
                    used.add(b["id"])
            if len(group) > 1:
                used.add(a["id"])
                groups.append(group)
        return groups

    def consolidate_group(self, sid, group):
        """Merge a group of similar records: keep longest, supersede rest."""
        longest = max(group, key=lambda r: len(r["content"]))
        now = utc_now()
        self.db.execute("UPDATE knowledge SET last_confirmed=? WHERE id=?", (now, longest["id"]))
        merged_ids = []
        for r in group:
            if r["id"] != longest["id"]:
                self.db.execute(
                    "UPDATE knowledge SET status='consolidated', superseded_by=? WHERE id=?",
                    (longest["id"], r["id"]))
                merged_ids.append(r["id"])
                self._delete_embedding(r["id"])
                if self.chroma and not self._check_binary_search():
                    try:
                        self.chroma.delete(ids=[str(r["id"])])
                    except Exception:
                        pass
        self.db.commit()
        return {"kept": longest["id"], "merged": merged_ids}

    # ── Retention Zones ──

    def apply_retention(self):
        """Move old unconfirmed records: active→archived→purged."""
        now = datetime.now(timezone.utc)
        archive_cutoff = format_utc(now - timedelta(days=ARCHIVE_AFTER_DAYS))
        purge_cutoff = format_utc(now - timedelta(days=PURGE_AFTER_DAYS))

        archived = self.db.execute("""
            UPDATE knowledge SET status='archived'
            WHERE status='active' AND last_confirmed < ? AND recall_count = 0
            AND confidence < 0.8
        """, (archive_cutoff,)).rowcount

        purged = self.db.execute("""
            UPDATE knowledge SET status='purged'
            WHERE status='archived' AND last_confirmed < ?
        """, (purge_cutoff,)).rowcount

        self.db.commit()

        if archived or purged:
            for r in self.q("SELECT id FROM knowledge WHERE status IN ('archived','purged')"):
                self._delete_embedding(r["id"])
                if self.chroma and not self._check_binary_search():
                    try:
                        self.chroma.delete(ids=[str(r["id"])])
                    except Exception:
                        pass
            self.db.commit()

        return {"archived": archived, "purged": purged}

    # ── Export ──

    def export_all(self, project=None):
        """Export all active knowledge as JSON."""
        conds = ["status='active'"]
        params = []
        if project:
            conds.append("project=?")
            params.append(project)
        rows = self.q(f"SELECT * FROM knowledge WHERE {' AND '.join(conds)} ORDER BY id", params)
        for r in rows:
            if isinstance(r.get("tags"), str):
                try:
                    r["tags"] = json.loads(r["tags"])
                except Exception:
                    pass
        sessions = self.q("SELECT * FROM sessions WHERE (? IS NULL OR project=?) ORDER BY started_at",
                          (project, project))
        relations = self.q("""SELECT rel.* FROM relations rel
            JOIN knowledge a ON a.id=rel.from_id JOIN knowledge b ON b.id=rel.to_id
            WHERE a.status='active' AND b.status='active'
            AND (? IS NULL OR (a.project=? AND b.project=?))""", (project, project, project))
        return {
            "version": "2.1",
            "exported_at": utc_now(),
            "knowledge": rows,
            "sessions": sessions,
            "relations": relations,
        }

    # ── Version History ──

    def get_version_history(self, kid):
        """Walk the superseded_by chain to build version history."""
        chain = []
        current = self.q1("SELECT * FROM knowledge WHERE id=?", (kid,))
        if not current:
            return chain
        chain.append(current)
        # Walk backwards: find records that were superseded by this one
        visited = {kid}
        while True:
            prev = self.q1("SELECT * FROM knowledge WHERE superseded_by=? AND id NOT IN ({})".format(
                ",".join("?" * len(visited))), (kid, *visited))
            if not prev:
                break
            chain.append(prev)
            visited.add(prev["id"])
            kid = prev["id"]
        # Walk forward: follow superseded_by from original record
        fwd_id = current.get("superseded_by")
        while fwd_id and fwd_id not in visited:
            visited.add(fwd_id)
            nxt = self.q1("SELECT * FROM knowledge WHERE id=?", (fwd_id,))
            if not nxt:
                break
            chain.insert(0, nxt)
            fwd_id = nxt.get("superseded_by")
        return chain

    # ── Delete ──

    def delete_knowledge(self, kid):
        """Soft-delete a knowledge record."""
        rec = self.q1("SELECT * FROM knowledge WHERE id=?", (kid,))
        if not rec:
            return None
        spaces = [r[0] for r in self.db.execute(
            "SELECT DISTINCT embedding_space FROM embeddings WHERE knowledge_id=?", (kid,))]
        self.db.execute("UPDATE knowledge SET status='deleted' WHERE id=?", (kid,))
        self._delete_embedding(kid)
        self.db.commit()
        for space in {"text", *spaces}:
            collection = self._chroma_collection_for(space)
            if collection is not None:
                try:
                    collection.delete(ids=[str(kid)])
                except Exception as error:  # noqa: BLE001 — SQL soft deletion still excludes this record
                    LOG(f"Chroma delete failed for knowledge_id={kid} space={space}: {error}")
        return rec

    # ── Relations ──

    def add_relation(self, from_id, to_id, rel_type):
        """Create a relation between two knowledge records."""
        now = utc_now()
        # Verify both records exist
        if not self.q1("SELECT id FROM knowledge WHERE id=?", (from_id,)):
            return {"error": f"Record {from_id} not found"}
        if not self.q1("SELECT id FROM knowledge WHERE id=?", (to_id,)):
            return {"error": f"Record {to_id} not found"}
        # Check for duplicate
        existing = self.q1(
            "SELECT rowid FROM relations WHERE from_id=? AND to_id=? AND type=?",
            (from_id, to_id, rel_type))
        if existing:
            return {"exists": True, "from_id": from_id, "to_id": to_id, "type": rel_type}
        self.db.execute("INSERT INTO relations (from_id, to_id, type, created_at) VALUES (?,?,?,?)",
                        (from_id, to_id, rel_type, now))
        self.db.commit()
        return {"created": True, "from_id": from_id, "to_id": to_id, "type": rel_type}

    # ── Search by Tag ──

    def search_by_tag(self, tag, project=None):
        """Find all active knowledge with a matching tag (SQL pre-filter + Python refine)."""
        conds = ["status='active'", "tags LIKE ?"]
        params = [f"%{tag}%"]
        if project:
            conds.append("project=?")
            params.append(project)
        rows = self.q(
            f"SELECT * FROM knowledge WHERE {' AND '.join(conds)} ORDER BY created_at DESC",
            params)
        matched = []
        tag_lower = tag.lower()
        for r in rows:
            tags_raw = r.get("tags", "[]")
            if isinstance(tags_raw, str):
                try:
                    tags_list = json.loads(tags_raw)
                except Exception:
                    tags_list = []
            else:
                tags_list = tags_raw
            if any(tag_lower in t.lower() for t in tags_list):
                r["tags"] = tags_list
                matched.append(r)
        return matched

    # ── Observations (lightweight auto-capture) ──

    def save_observation(self, sid, tool_name, summary, observation_type="change",
                         files_affected=None, project="general", branch=""):
        """Save a lightweight observation (no dedup, no ChromaDB)."""
        now = utc_now()
        summary, _ = self._sanitize_content(summary)
        cur = self.db.execute("""
            INSERT INTO observations (session_id, tool_name, observation_type, summary,
                                      files_affected, project, branch, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (sid, tool_name, observation_type, summary,
              json.dumps(files_affected or []), project, branch or "", now))
        self.db.commit()
        return cur.lastrowid

    def cleanup_old_observations(self):
        """Remove observations older than OBSERVATION_RETENTION_DAYS."""
        cutoff = format_utc(datetime.now(timezone.utc) - timedelta(days=OBSERVATION_RETENTION_DAYS))
        deleted = self.db.execute(
            "DELETE FROM observations WHERE created_at < ?", (cutoff,)).rowcount
        self.db.commit()
        return deleted

    # ═══════════════════════════════════════════════════════════
    # Self-Improvement: Errors / Insights / Rules
    # ═══════════════════════════════════════════════════════════

    def log_error(self, sid, description, category, severity="medium",
                  fix="", context="", project="general", tags=None):
        """Log a structured error and check for patterns."""
        now = utc_now()
        status = "resolved" if fix else "open"
        cur = self.db.execute("""
            INSERT INTO errors (session_id, category, severity, description, context,
                               fix, project, tags, status, resolved_at, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (sid, category, severity, description, context, fix, project,
              json.dumps(tags or []), status, now if fix else None, now))
        self.db.commit()
        error_id = cur.lastrowid
        pattern = self.detect_error_pattern(category, project)
        return error_id, pattern

    def detect_error_pattern(self, category, project="general"):
        """Detect repeating error patterns (3+ same category in 30 days)."""
        cutoff = format_utc(datetime.now(timezone.utc) - timedelta(days=30))
        row = self.db.execute("""
            SELECT COUNT(*) as cnt, GROUP_CONCAT(id) as ids
            FROM errors
            WHERE category=? AND project=? AND status != 'insight_extracted'
            AND created_at > ?
        """, (category, project, cutoff)).fetchone()
        count = row[0] if row else 0
        if count < 3:
            return None

        error_ids = [int(x) for x in (row[1] or "").split(",") if x]

        existing = self.q1(
            "SELECT id, content, importance FROM insights "
            "WHERE category=? AND project=? AND status='active'",
            (category, project))

        if existing:
            return {
                "pattern_detected": True, "category": category, "count": count,
                "error_ids": error_ids[:10],
                "existing_insight_id": existing["id"],
                "suggestion": f"UPVOTE existing insight #{existing['id']}: "
                             f"{existing['content'][:100]}"
            }

        descriptions = self.q(
            "SELECT id, description, fix FROM errors WHERE id IN ({}) "
            "ORDER BY created_at DESC".format(",".join("?" * len(error_ids[:10]))),
            error_ids[:10])

        return {
            "pattern_detected": True, "category": category, "count": count,
            "error_ids": error_ids[:10],
            "descriptions": [{"id": d["id"], "desc": d["description"][:200],
                              "fix": (d["fix"] or "")[:200]} for d in descriptions],
            "suggestion": "Extract an insight from these repeated errors using "
                          "self_insight(action='add', ...)"
        }

    def _find_similar_insight(self, content, category, project):
        """Find existing insight with similar content via fuzzy match."""
        rows = self.q(
            "SELECT * FROM insights WHERE category=? AND project=? AND status='active'",
            (category, project))
        for r in rows:
            if self._fuzzy_ratio(content, r["content"]) > 0.70:
                return r
        return None

    def manage_insight(self, sid, action, **kw):
        """ExpeL-style insight management: add/upvote/downvote/edit/list/promote."""
        now = utc_now()

        if action == "add":
            content = kw["content"]
            category = kw["category"]
            project = kw.get("project", "general")
            existing = self._find_similar_insight(content, category, project)
            if existing:
                self.db.execute(
                    "UPDATE insights SET importance=importance+1, "
                    "confidence=MIN(1.0, confidence+0.05), updated_at=? WHERE id=?",
                    (now, existing["id"]))
                self.db.commit()
                return {"action": "auto_upvoted", "id": existing["id"],
                        "importance": existing["importance"] + 1}

            source_ids = kw.get("source_error_ids", [])
            cur = self.db.execute("""
                INSERT INTO insights (session_id, content, context, category, importance,
                                     confidence, source_error_ids, project, tags,
                                     status, created_at, updated_at)
                VALUES (?,?,?,?,2,0.5,?,?,?,'active',?,?)
            """, (sid, content, kw.get("context", ""), category,
                  json.dumps(source_ids), project,
                  json.dumps(kw.get("tags", [])), now, now))
            self.db.commit()
            insight_id = cur.lastrowid
            for eid in source_ids:
                self.db.execute(
                    "UPDATE errors SET status='insight_extracted', insight_id=? WHERE id=?",
                    (insight_id, eid))
            self.db.commit()
            return {"action": "added", "id": insight_id, "importance": 2}

        elif action == "upvote":
            self.db.execute(
                "UPDATE insights SET importance=importance+1, "
                "confidence=MIN(1.0, confidence+0.05), updated_at=? "
                "WHERE id=? AND status='active'", (now, kw["id"]))
            self.db.commit()
            rec = self.q1("SELECT id, importance, confidence FROM insights WHERE id=?", (kw["id"],))
            eligible = rec and rec["importance"] >= 5 and rec["confidence"] >= 0.8
            return {"action": "upvoted", "id": kw["id"],
                    "importance": rec["importance"] if rec else None,
                    "promotion_eligible": eligible}

        elif action == "downvote":
            self.db.execute(
                "UPDATE insights SET importance=importance-1, updated_at=? "
                "WHERE id=? AND status='active'", (now, kw["id"]))
            self.db.commit()
            rec = self.q1("SELECT id, importance FROM insights WHERE id=?", (kw["id"],))
            if rec and rec["importance"] <= 0:
                self.db.execute(
                    "UPDATE insights SET status='archived', updated_at=? WHERE id=?",
                    (now, kw["id"]))
                self.db.commit()
                return {"action": "archived", "id": kw["id"],
                        "reason": "importance reached 0"}
            return {"action": "downvoted", "id": kw["id"],
                    "importance": rec["importance"] if rec else None}

        elif action == "edit":
            self.db.execute(
                "UPDATE insights SET content=?, updated_at=? WHERE id=? AND status='active'",
                (kw["content"], now, kw["id"]))
            self.db.commit()
            return {"action": "edited", "id": kw["id"]}

        elif action == "list":
            project = kw.get("project")
            category = kw.get("category")
            conds, params = ["status='active'"], []
            if project:
                conds.append("project=?"); params.append(project)
            if category:
                conds.append("category=?"); params.append(category)
            rows = self.q(
                f"SELECT * FROM insights WHERE {' AND '.join(conds)} "
                "ORDER BY importance DESC, confidence DESC LIMIT 50", params)
            for r in rows:
                r["promotion_eligible"] = (r["importance"] >= 5 and r["confidence"] >= 0.8)
            return {"insights": rows, "total": len(rows)}

        elif action == "promote":
            return self.promote_insight_to_rule(sid, kw["id"])

        return {"error": f"Unknown action: {action}"}

    def promote_insight_to_rule(self, sid, insight_id):
        """Promote a high-value insight to a behavioral rule."""
        now = utc_now()
        insight = self.q1("SELECT * FROM insights WHERE id=? AND status='active'", (insight_id,))
        if not insight:
            return {"error": "Insight not found or not active"}
        if insight["importance"] < 5 or insight["confidence"] < 0.8:
            return {"error": "Not eligible", "importance": insight["importance"],
                    "confidence": insight["confidence"],
                    "required": "importance >= 5 AND confidence >= 0.8"}

        scope = "global" if insight["project"] == "general" else f"project:{insight['project']}"
        priority = min(10, max(1, insight["importance"]))

        cur = self.db.execute("""
            INSERT INTO rules (session_id, content, context, category, scope, priority,
                              source_insight_id, project, tags, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,'active',?,?)
        """, (sid, insight["content"],
              f"Promoted from insight #{insight_id}. {insight.get('context', '')}",
              insight["category"], scope, priority, insight_id,
              insight["project"], insight.get("tags", "[]"), now, now))
        self.db.commit()
        rule_id = cur.lastrowid

        self.db.execute(
            "UPDATE insights SET status='promoted', promoted_to_rule_id=?, updated_at=? WHERE id=?",
            (rule_id, now, insight_id))
        self.db.commit()

        return {"promoted": True, "insight_id": insight_id, "rule_id": rule_id,
                "scope": scope, "priority": priority}

    def manage_rule(self, sid, action, **kw):
        """Manage behavioral rules (SOUL)."""
        now = utc_now()

        if action == "list":
            conds, params = ["status='active'"], []
            if kw.get("project"):
                conds.append("(project=? OR scope='global')")
                params.append(kw["project"])
            if kw.get("scope"):
                conds.append("scope=?"); params.append(kw["scope"])
            rows = self.q(
                f"SELECT * FROM rules WHERE {' AND '.join(conds)} "
                "ORDER BY priority DESC, success_rate DESC LIMIT 30", params)
            return {"rules": rows, "total": len(rows)}

        elif action == "fire":
            self.db.execute(
                "UPDATE rules SET fire_count=fire_count+1, last_fired=?, updated_at=? "
                "WHERE id=? AND status='active'", (now, now, kw["id"]))
            self.db.commit()
            return {"fired": True, "id": kw["id"]}

        elif action == "rate":
            rid = kw["id"]
            if kw.get("success"):
                self.db.execute(
                    "UPDATE rules SET success_count=success_count+1, updated_at=? WHERE id=?",
                    (now, rid))
            else:
                self.db.execute(
                    "UPDATE rules SET fail_count=fail_count+1, updated_at=? WHERE id=?",
                    (now, rid))
            self.db.commit()
            # Recalculate success_rate
            self.db.execute(
                "UPDATE rules SET success_rate=CASE WHEN fire_count>0 "
                "THEN CAST(success_count AS REAL)/CAST(fire_count AS REAL) "
                "ELSE 0.0 END WHERE id=?", (rid,))
            self.db.commit()
            rec = self.q1("SELECT * FROM rules WHERE id=?", (rid,))
            # Auto-suspend ineffective rules
            if rec and rec["fire_count"] >= 10 and rec["success_rate"] < 0.2:
                self.db.execute(
                    "UPDATE rules SET status='suspended', updated_at=? WHERE id=?",
                    (now, rid))
                self.db.commit()
                return {"rated": True, "auto_suspended": True,
                        "reason": "success_rate < 0.2 after 10+ fires"}
            return {"rated": True, "id": rid,
                    "success_rate": rec["success_rate"] if rec else None}

        elif action == "suspend":
            self.db.execute("UPDATE rules SET status='suspended', updated_at=? WHERE id=?",
                           (now, kw["id"]))
            self.db.commit()
            return {"suspended": True, "id": kw["id"]}

        elif action == "activate":
            self.db.execute("UPDATE rules SET status='active', updated_at=? WHERE id=?",
                           (now, kw["id"]))
            self.db.commit()
            return {"activated": True, "id": kw["id"]}

        elif action == "retire":
            self.db.execute("UPDATE rules SET status='retired', updated_at=? WHERE id=?",
                           (now, kw["id"]))
            self.db.commit()
            return {"retired": True, "id": kw["id"]}

        elif action == "add_manual":
            cur = self.db.execute("""
                INSERT INTO rules (session_id, content, context, category, scope, priority,
                                  project, tags, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,'active',?,?)
            """, (sid, kw["content"], kw.get("context", ""),
                  kw["category"], kw.get("scope", "global"),
                  kw.get("priority", 5), kw.get("project", "general"),
                  json.dumps(kw.get("tags", [])), now, now))
            self.db.commit()
            return {"added": True, "id": cur.lastrowid}

        return {"error": f"Unknown action: {action}"}

    def get_rules_for_context(self, project="general", categories=None, phase=None):
        """Get active rules relevant to current context.

        Args:
            project: filter by project scope (plus 'global' rules).
            categories: extra scope filters (category:<name>).
            phase: optional phase filter (v8.0 lazy rule loading). When set, returns
                core rules (no 'phase:*' tag) plus rules tagged 'phase:<phase>'.
                Expected values: van|plan|creative|build|reflect|archive.
        """
        VALID_PHASES = {"van", "plan", "creative", "build", "reflect", "archive"}
        if phase is not None and phase not in VALID_PHASES:
            return {
                "error": f"Unknown phase '{phase}'. "
                         f"Expected one of: {sorted(VALID_PHASES)}",
            }

        scopes = ["'global'", f"'project:{project}'"]
        if categories:
            scopes.extend(f"'category:{c}'" for c in categories)
        rows = self.q(f"""
            SELECT * FROM rules
            WHERE status='active' AND scope IN ({','.join(scopes)})
            ORDER BY priority DESC, success_rate DESC LIMIT 20
        """)

        if phase is not None:
            # Tag-based routing: a rule is phase-specific iff it has a tag
            # matching "phase:<X>". Rules without any "phase:*" tag are core
            # and apply to every phase.
            filtered = []
            for r in rows:
                try:
                    tags = json.loads(r.get("tags") or "[]")
                except (json.JSONDecodeError, TypeError):
                    tags = []
                phase_tags = [t for t in tags if isinstance(t, str) and t.startswith("phase:")]
                if not phase_tags:
                    filtered.append(r)  # core rule — always included
                elif f"phase:{phase}" in phase_tags:
                    filtered.append(r)  # matches current phase
                # else: rule is scoped to a different phase — skip
            rows = filtered

        now = utc_now()
        for r in rows:
            self.db.execute(
                "UPDATE rules SET fire_count=fire_count+1, last_fired=?, updated_at=? WHERE id=?",
                (now, now, r["id"]))
        self.db.commit()
        result = {"rules_count": len(rows), "rules": rows}
        if phase is not None:
            result["phase_filter"] = phase
        return result

    def set_rule_phase(self, rule_id, phase):
        """Set or clear the phase of a rule (v8.0 lazy rule loading).

        Tag-based: manages the "phase:<X>" tag on the rule's tags JSON list.
        - phase=None   → remove any "phase:*" tag (rule becomes core).
        - phase="build"→ replace any existing "phase:*" tag with "phase:build".

        Returns:
            dict with rule_id, phase (or None), updated flag.
        """
        VALID_PHASES = {"van", "plan", "creative", "build", "reflect", "archive"}
        if phase is not None and phase not in VALID_PHASES:
            return {
                "error": f"Unknown phase '{phase}'. "
                         f"Expected one of: {sorted(VALID_PHASES)} or null",
            }

        row = self.q1("SELECT id, tags FROM rules WHERE id=?", (rule_id,))
        if not row:
            return {"error": f"Rule {rule_id} not found", "rule_id": rule_id}

        try:
            tags = json.loads(row.get("tags") or "[]")
            if not isinstance(tags, list):
                tags = []
        except (json.JSONDecodeError, TypeError):
            tags = []

        # Strip any existing phase:* tags
        tags = [t for t in tags if not (isinstance(t, str) and t.startswith("phase:"))]
        if phase is not None:
            tags.append(f"phase:{phase}")

        now = utc_now()
        self.db.execute(
            "UPDATE rules SET tags=?, updated_at=? WHERE id=?",
            (json.dumps(tags), now, rule_id),
        )
        self.db.commit()
        return {"rule_id": rule_id, "phase": phase, "updated": True}

    def analyze_patterns(self, view="full_report", project=None, days=30):
        """Analyze error patterns and self-improvement metrics."""
        cutoff = format_utc(datetime.now(timezone.utc) - timedelta(days=days))
        pf = "AND project=?" if project else ""
        pp = (project,) if project else ()
        result = {}

        if view in ("error_patterns", "full_report"):
            freq = self.q(f"""
                SELECT category, severity, COUNT(*) as count,
                       SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) as unresolved
                FROM errors WHERE created_at > ? {pf}
                GROUP BY category, severity ORDER BY count DESC
            """, (cutoff, *pp))
            patterns = self.q(f"""
                SELECT category, COUNT(*) as count, GROUP_CONCAT(id) as error_ids,
                       MIN(created_at) as first_seen, MAX(created_at) as last_seen
                FROM errors WHERE created_at > ? AND status != 'insight_extracted' {pf}
                GROUP BY category HAVING count >= 3 ORDER BY count DESC
            """, (cutoff, *pp))
            result["error_patterns"] = {"frequency": freq, "repeating_patterns": patterns}

        if view in ("insight_candidates", "full_report"):
            candidates = self.q(f"""
                SELECT * FROM insights
                WHERE status='active' AND importance >= 5 AND confidence >= 0.8
                {pf} ORDER BY importance DESC
            """, pp)
            result["promotion_candidates"] = {"count": len(candidates), "insights": candidates}

        if view in ("rule_effectiveness", "full_report"):
            stats = self.q(f"""
                SELECT id, content, scope, priority, fire_count,
                       success_count, fail_count, success_rate, status
                FROM rules WHERE fire_count > 0 {pf} ORDER BY success_rate DESC
            """, pp)
            stale = self.q("""
                SELECT id, content, last_fired FROM rules
                WHERE status='active'
                AND (last_fired IS NULL OR last_fired < datetime('now', '-60 days'))
            """)
            result["rule_effectiveness"] = {"rules": stats, "stale_rules": stale}

        if view in ("improvement_trend", "full_report"):
            weeks = []
            for w in range(4):
                start = format_utc(datetime.now(timezone.utc) - timedelta(days=(w+1)*7))
                end = format_utc(datetime.now(timezone.utc) - timedelta(days=w*7))
                cnt = self.db.execute(f"""
                    SELECT COUNT(*) FROM errors WHERE created_at BETWEEN ? AND ? {pf}
                """, (start, end, *pp)).fetchone()[0]
                weeks.append({"week_ago": w, "errors": cnt})
            result["improvement_trend"] = {
                "weekly_errors": weeks,
                "direction": "improving" if weeks and weeks[0]["errors"] <= weeks[-1]["errors"]
                            else "degrading"
            }

        if view == "full_report":
            result["summary"] = {
                "total_errors": self.db.execute(
                    f"SELECT COUNT(*) FROM errors WHERE 1=1 {pf}", pp).fetchone()[0],
                "active_insights": self.db.execute(
                    f"SELECT COUNT(*) FROM insights WHERE status='active' {pf}", pp).fetchone()[0],
                "active_rules": self.db.execute(
                    f"SELECT COUNT(*) FROM rules WHERE status='active' {pf}", pp).fetchone()[0],
            }
        return result


# ═══════════════════════════════════════════════════════════
# Retrieval
# ═══════════════════════════════════════════════════════════

class Recall:
    def __init__(self, store: Store):
        self.s = store
        self._bounded_reranker = None

    # ── RRF: Reciprocal Rank Fusion ──────────────────────────
    # Default tier weights: semantic gets slight boost, fuzzy is penalized
    RRF_K = 60  # standard RRF constant
    RRF_WEIGHTS = {
        "fts": 1.0,
        "semantic": 1.2,
        "hyde": 1.0,
        "fuzzy": 0.5,
        "graph": 0.8,
        "episode": 0.9,
    }

    @staticmethod
    def _rrf_fuse(tier_rankings, weights, k=60, *, score_weight=None):
        """Reciprocal Rank Fusion across multiple ranked lists.

        Args:
            tier_rankings: dict mapping tier name to list of doc IDs (ordered by tier score desc).
            weights: dict mapping tier name to weight multiplier.
            k: RRF smoothing constant (default 60).
            score_weight: optional callable ``(doc_id, tier_name) -> float`` that
                returns a *per-tier* multiplier (e.g. staleness decay scoped to
                that tier). Defaults to 1.0 — identical to legacy behavior.

        Returns:
            dict mapping doc_id to fused RRF score.
        """
        scores = {}
        for source, ranked_ids in tier_rankings.items():
            w = weights.get(source, 1.0)
            for rank, doc_id in enumerate(ranked_ids):
                mult = 1.0
                if score_weight is not None:
                    try:
                        mult = float(score_weight(doc_id, source))
                    except Exception:
                        mult = 1.0
                scores[doc_id] = scores.get(doc_id, 0.0) + (
                    w * mult * (1.0 / (k + rank + 1))
                )
        return scores

    def _should_use_advanced_rag(self):
        """Check if advanced RAG (HyDE + reranker) is available and enabled."""
        if not HAS_RERANKER:
            return False
        # v11: respect MEMORY_USE_LLM_IN_HOT_PATH=false. Fast/balanced modes
        # set this off via resolve_mode_defaults — ignoring whatever the
        # legacy USE_ADVANCED_RAG env says. Read at runtime so tests that
        # twiddle env vars per-test still work.
        if os.environ.get(
            "MEMORY_USE_LLM_IN_HOT_PATH", "false"
        ).strip().lower() not in ("1", "true", "yes", "on"):
            return False
        # Re-read USE_ADVANCED_RAG at runtime too (the module-level constant
        # is fixed at import; tests use monkeypatch.setenv after import).
        rag = os.environ.get("USE_ADVANCED_RAG", USE_ADVANCED_RAG).strip().lower()
        if rag == "false":
            return False
        if rag == "true":
            return True
        # auto: check if Ollama is available
        return self.s._check_ollama()

    def search(self, query, project=None, ktype="all", limit=10, detail="full", branch=None, fusion="rrf",
               rerank=False, diverse=False, embedding_space=None, _explain=False,
               record_usage=True):
        # v11 Phase 5 — total wall-clock for this search; recorded into
        # `memory_core.telemetry.counters['search_total_ms']`.
        try:
            from memory_core.telemetry import op_timer as _v11_op_timer
        except Exception:
            from contextlib import nullcontext

            def _v11_op_timer(_name):  # type: ignore[no-redef]
                return nullcontext()

        with _v11_op_timer("search_total_ms"):
            return self._search_impl(
                query, project=project, ktype=ktype, limit=limit, detail=detail,
                branch=branch, fusion=fusion, rerank=rerank, diverse=diverse,
                embedding_space=embedding_space, _explain=_explain,
                record_usage=record_usage,
            )

    def _search_impl(self, query, project=None, ktype="all", limit=10, detail="full",
                     branch=None, fusion="rrf", rerank=False, diverse=False,
                     embedding_space=None, _explain=False, record_usage=True):
        """Underlying implementation; wrapped by `search` for telemetry.

        v11 Phase 6b — `embedding_space` (str | list[str] | None) filters
        vector candidates to rows tagged with one of the listed spaces.
        FTS / fuzzy / graph tiers are unaffected (those don't carry a
        space). detail="full" exposes the per-row `embedding_space` so
        clients can verify the filter took effect.

        v11 Phase 6 — `_explain=True` returns an `_explain` payload with
        per-tier breakdowns; this is the data path used by the
        `memory_explain_search` MCP tool.
        """
        from memory_core.cross_rerank import shared_reranker
        from memory_core.retrieval import SearchScope
        scope = SearchScope(project=project, kind=ktype, branch=branch,
                            spaces=embedding_space)
        # Candidates gathered per tier. A cross-encoder needs a wider window
        # than the caller's limit to have anything to re-order.
        cross = shared_reranker()
        cross_active = cross is not None and cross.applies_to(query)
        pool = max(limit, (cross.window + 1) // 2) if cross_active else limit
        cacheable = not self.s.db.in_transaction
        revision = (self.s.db.total_changes,
                    self.s.db.execute("PRAGMA data_version").fetchone()[0])
        if not cacheable or getattr(self, "_cache_revision", None) != revision:
            if self.s.cache is not None:
                self.s.cache.invalidate()
            if getattr(self.s, "v9_cache", None) is not None:
                self.s.v9_cache.invalidate_all()
            self._cache_revision = revision
        # Normalize embedding_space param to a sorted list[str] (or None).
        if embedding_space is None or embedding_space == "":
            _v11_spaces: list[str] | None = None
        elif isinstance(embedding_space, str):
            _v11_spaces = [embedding_space.strip().lower()]
        else:
            _v11_spaces = sorted({
                str(s).strip().lower() for s in embedding_space if str(s).strip()
            }) or None
        # v9 A2 L1: fast-path query cache. Keyed by full filter set so that
        # different projects / ktypes / branches / spaces don't collide.
        _v9 = getattr(self.s, "v9_cache", None)
        _v9_filters = {
            "project": project, "ktype": ktype, "detail": detail, "branch": branch,
            "fusion": fusion, "rerank": rerank, "diverse": diverse,
            # v11 Phase 6b — embedding_space affects candidate pool, must be in cache key.
            "embedding_space": ",".join(_v11_spaces) if _v11_spaces else None,
            "cross_rerank": f"{cross.model}/{cross.window}/{cross.weight}/{cross.context_chars}" if cross_active else None,
        }
        # _explain bypasses both caches: the payload includes ephemeral
        # tier rankings that are not part of the cached representation.
        if cacheable and not _explain and _v9 is not None and _v9.l1.enabled:
            hit = _v9.recall_get(query, mode="search", k=limit, filters=_v9_filters)
            if hit is not None:
                if record_usage:
                    from memory_core.retrieval import flatten_results
                    self.s.bump_recall([item["id"] for item in flatten_results(hit)])
                self._cache_revision = (self.s.db.total_changes, revision[1])
                return hit

        # Check cache first (include fusion param in cache key)
        if cacheable and not _explain and self.s.cache is not None:
            cache_key = self.s.cache.make_key(query=query, project=project, ktype=ktype,
                                               limit=limit, detail=detail, branch=branch,
                                               fusion=fusion, rerank=rerank, diverse=diverse,
                                               embedding_space=",".join(_v11_spaces) if _v11_spaces else None)
            cached = self.s.cache.get(cache_key)
            if cached is not None:
                if record_usage:
                    from memory_core.retrieval import flatten_results
                    self.s.bump_recall([item["id"] for item in flatten_results(cached)])
                self._cache_revision = (self.s.db.total_changes, revision[1])
                return cached

        # Stage 0 (optional): Query rewriting via Haiku.
        # For multi-hop / temporal queries, ask a cheap LLM to produce a
        # canonical fact-lookup form. Gated on MEMORY_QUERY_REWRITE=1 and
        # heuristic intent detector to avoid paying LLM for every call.
        original_query = query
        if (HAS_QUERY_REWRITER and _qr_is_enabled()
                and os.environ.get("MEMORY_USE_LLM_IN_HOT_PATH", "false").strip().lower() in ("1", "true", "yes", "on")
                and _qr_decomposable(query)):
            try:
                from ai_layer.planner_client import PlannerClient
                rw = _qr_rewrite(query, PlannerClient(), model="configured")
                canonical = (rw or {}).get("canonical", "").strip()
                if canonical and len(canonical) > 3:
                    query = canonical
            except Exception as e:
                LOG(f"query_rewriter failed, using original query: {e}")

        use_advanced = self._should_use_advanced_rag()
        query_info = None
        if use_advanced:
            query_info = analyze_query(query)

        use_rrf = (fusion == "rrf")

        # Collect results per doc_id and per-tier ranked lists for RRF
        results = {}          # doc_id -> {"r": row, "score": legacy_score, "via": [tiers]}
        tier_rankings = {}    # tier_name -> [doc_id, ...] ordered by tier-specific score desc
        # v11 Phase 6 — when _explain=True we also keep the raw per-tier
        # scores (BM25, cosine, fuzzy ratio, …) so the explain payload
        # can show them separately from the merged additive `score`.
        tier_scores: dict[str, dict[int, float]] = {} if _explain else {}

        # Tier 1: FTS5 keyword search with BM25 scoring
        from memory_core.query_terms import fts_match_query
        fts_q = fts_match_query(query)
        try:
            conds = ["knowledge_fts MATCH ?", "k.status='active'"]
            params = [fts_q]
            joins = ""
            if project:
                conds.append("k.project=?")
                params.append(project)
            if ktype != "all":
                conds.append("k.type=?")
                params.append(ktype)
            if branch:
                conds.append("(k.branch=? OR k.branch='')")
                params.append(branch)
            # v11 Phase 6b — restrict FTS results to records whose
            # embedding row carries one of the requested spaces. Records
            # without an embedding row are admitted only when no space
            # filter was set (otherwise we'd leak unclassified rows).
            if _v11_spaces:
                joins += " JOIN embeddings e ON e.knowledge_id=k.id"
                ph = ",".join("?" * len(_v11_spaces))
                conds.append(f"COALESCE(e.embedding_space, 'text') IN ({ph})")
                params.extend(_v11_spaces)
            params.append(pool * 3)
            from memory_core.telemetry import op_timer
            with op_timer("retrieval_fts_ms"):
                if project:
                    # The project's token is ANDed into MATCH so FTS5 ranks
                    # only that project's matches; materialized so the planner
                    # does not re-run MATCH per project row instead.
                    from memory_core.fts_schema import SCOPED_BM25_WEIGHTS, scoped_match
                    params[0] = scoped_match(fts_q, project)
                    fts_rows = self.s.db.execute(f"""
                        WITH f AS MATERIALIZED (
                            SELECT rowid AS id, bm25(knowledge_fts, {SCOPED_BM25_WEIGHTS}) AS _bm25
                            FROM knowledge_fts WHERE {conds[0]}
                        )
                        SELECT k.*, f._bm25 AS _bm25
                        FROM f JOIN knowledge k ON k.id=f.id{joins}
                        WHERE {' AND '.join(conds[1:])} ORDER BY f._bm25, k.id LIMIT ?
                    """, params).fetchall()
                else:
                    fts_rows = self.s.db.execute(f"""
                        SELECT k.*, bm25(knowledge_fts) AS _bm25
                        FROM knowledge_fts f JOIN knowledge k ON k.id=f.rowid{joins}
                        WHERE {' AND '.join(conds)} ORDER BY bm25(knowledge_fts) LIMIT ?
                    """, params).fetchall()
            # Proper BM25 normalization: relative to max in batch
            raw_scores = [abs(dict(r).get("_bm25", 0)) for r in fts_rows]
            max_bm25 = max(raw_scores) if raw_scores else 1.0
            fts_tier = []  # collect (doc_id, score) for RRF ranking
            for r in fts_rows:
                row = dict(r)
                bm25_raw = abs(row.pop("_bm25", 0))
                bm25_score = (bm25_raw / max(max_bm25, 0.01)) * 2.0
                results[r["id"]] = {"r": row, "score": max(0.5, bm25_score), "via": ["fts"]}
                fts_tier.append(r["id"])  # already sorted by BM25 from SQL ORDER BY
                if _explain:
                    tier_scores.setdefault("fts", {})[int(r["id"])] = float(bm25_raw)
            if fts_tier:
                tier_rankings["fts"] = fts_tier
        except Exception:
            from memory_core.telemetry import counters
            counters.bump("retrieval_fts_errors")
            logging.getLogger(__name__).exception("FTS retrieval failed", extra={"tier": "fts"})

        from memory_core.atomic_facts import FactRepository
        try:
            atomic_hits = FactRepository(self.s.db).search(query, scope, pool * 3)
            if atomic_hits:
                tier_rankings["atomic_facts"] = [hit["id"] for hit in atomic_hits]
                for hit in atomic_hits:
                    kid = hit["id"]
                    if kid in results:
                        results[kid]["via"].append("atomic_facts")
                    else:
                        results[kid] = {"r": hit, "score": 0.0, "via": ["atomic_facts"]}
        except sqlite3.Error as error:
            from memory_core.telemetry import counters
            counters.bump("retrieval_atomic_facts_errors")
            logging.getLogger(__name__).warning("Atomic fact search unavailable",
                                                extra={"tier": "atomic_facts", "error": str(error)})

        # Search each embedding model in its own vector space.
        can_embed = self.s._embed_mode != "none"
        self.s._semantic_diagnostics = []
        semantic_tier = self.s._search_spaces(
            query, project=project, spaces=_v11_spaces, limit=pool * 3, kind=ktype, branch=branch,
        ) if can_embed else []
        from memory_core.retrieval import fetch_active_records
        semantic_records = fetch_active_records(
            self.s.db, [kid for kid, _ in semantic_tier if kid not in results],
        )
        hyde_tier = []
        for kid, similarity in semantic_tier:
            if kid in results:
                results[kid]["score"] += max(0, similarity)
                results[kid]["via"].append("semantic")
            else:
                row = semantic_records.get(kid)
                if row:
                    results[kid] = {"r": row, "score": max(0, similarity), "via": ["semantic"]}
            if _explain:
                tier_scores.setdefault("semantic", {})[kid] = float(similarity)

        if (can_embed and use_advanced and query_info and query_info.get("expand")
                and (not _v11_spaces or "text" in _v11_spaces)):
            try:
                hyde_vector = hyde_expand(query, project)
                if hyde_vector:
                    hyde_tier = self.s._binary_search(
                        hyde_vector, project=project, n_results=pool * 2,
                        embedding_spaces=["text"],
                        embedding_model=self.s._active_embed_model_name(),
                        kind=ktype, branch=branch,
                    )
                    hyde_records = fetch_active_records(
                        self.s.db, [kid for kid, _ in hyde_tier if kid not in results],
                    )
                    for kid, similarity in hyde_tier:
                        if kid in results:
                            results[kid]["via"].append("hyde")
                        else:
                            row = hyde_records.get(kid)
                            if row:
                                results[kid] = {"r": row, "score": max(0, similarity), "via": ["hyde"]}
                        if _explain:
                            tier_scores.setdefault("hyde", {})[kid] = float(similarity)
            except Exception as error:  # noqa: BLE001 — preserve ordinary retrieval when optional expansion fails
                LOG(f"HyDE search failed: {error}")

        if semantic_tier:
            semantic_tier.sort(key=lambda x: x[1], reverse=True)
            tier_rankings["semantic"] = [doc_id for doc_id, _ in semantic_tier]
        if hyde_tier:
            hyde_tier.sort(key=lambda x: x[1], reverse=True)
            tier_rankings["hyde"] = [doc_id for doc_id, _ in hyde_tier]

        # ── Tier 2c: Multi-representation search (summary/keywords/questions) ──
        # Safe no-op when knowledge_representations is empty or no embedder.
        try:
            from multi_repr_search import has_representations, search_with_winners

            if can_embed and has_representations(self.s.db):
                # Reuse query embedding if already computed above; else compute now
                try:
                    q_emb = embs[0]  # noqa: F821 — defined in upstream can_embed branch
                except (NameError, UnboundLocalError):
                    q_emb_list = self.s.embed([query])
                    q_emb = q_emb_list[0] if q_emb_list else None
                if q_emb:
                    repr_hits, repr_winners = search_with_winners(
                        self.s.db, q_emb, project=project, n_candidates=100, top_n=pool * 3
                    )
                    if repr_hits:
                        repr_records = fetch_active_records(
                            self.s.db, [kid for kid, _ in repr_hits if kid not in results],
                        )
                        repr_tier: list[tuple[int, float]] = []
                        for kid, score in repr_hits:
                            repr_tier.append((kid, score))
                            winner = repr_winners.get(kid)
                            if kid in results:
                                # RRF scores are small (~0.016) — scale to align with cosine tiers
                                results[kid]["score"] += score * 20.0
                                if "multi_repr" not in results[kid]["via"]:
                                    results[kid]["via"].append("multi_repr")
                                # Remember the freshest repr-type winner across passes
                                prev = results[kid].get("matched_repr")
                                if winner and not prev:
                                    results[kid]["matched_repr"] = winner
                            else:
                                rec = repr_records.get(kid)
                                if rec:
                                    results[kid] = {
                                        "r": rec,
                                        "score": score * 20.0,
                                        "via": ["multi_repr"],
                                        "matched_repr": winner,
                                    }
                        repr_tier.sort(key=lambda x: x[1], reverse=True)
                        tier_rankings["multi_repr"] = [doc_id for doc_id, _ in repr_tier]
        except Exception as e:
            LOG(f"multi_repr tier error: {e}")

        # ── Tier 3: Fuzzy search (catches typos and partial matches) ──
        if len(results) < pool:
            from memory_core.telemetry import op_timer
            with op_timer("retrieval_fuzzy_ms"):
                try:
                    conds2 = ["k.status='active'"]
                    params2 = []
                    if project:
                        conds2.append("k.project=?")
                        params2.append(project)
                    if ktype != "all":
                        conds2.append("k.type=?")
                        params2.append(ktype)
                    if branch:
                        conds2.append("(k.branch=? OR k.branch='')")
                        params2.append(branch)
                    params2.append(pool * 5)
                    candidates = self.s.q(f"""
                        SELECT * FROM knowledge k WHERE {' AND '.join(conds2)}
                        ORDER BY last_confirmed DESC LIMIT ?
                    """, params2)
                    ql = query.lower()
                    fuzzy_tier = []
                    for r in candidates:
                        if r["id"] in results:
                            continue
                        ratio = SequenceMatcher(None, ql, r["content"][:200].lower()).ratio()
                        if ratio > 0.35:
                            results[r["id"]] = {"r": r, "score": ratio * 0.6, "via": ["fuzzy"]}
                            fuzzy_tier.append((r["id"], ratio))
                            if _explain:
                                tier_scores.setdefault("fuzzy", {})[int(r["id"])] = float(ratio)
                    if fuzzy_tier:
                        fuzzy_tier.sort(key=lambda x: x[1], reverse=True)
                        tier_rankings["fuzzy"] = [doc_id for doc_id, _ in fuzzy_tier]
                except Exception:
                    from memory_core.telemetry import counters
                    counters.bump("retrieval_fuzzy_errors")
                    logging.getLogger(__name__).exception("Fuzzy retrieval failed", extra={"tier": "fuzzy"})


        # ── Tier 4: Graph expansion ──
        graph_candidates = [kid for kid in results if results[kid]["via"] != ["atomic_facts"]]
        top5 = sorted(graph_candidates or results, key=lambda x: results[x]["score"], reverse=True)[:5]
        graph_tier = []
        if use_advanced and query_info and query_info.get("deep_graph"):
            # Multi-hop graph traversal (2 hops for architecture queries)
            before_ids = set(results.keys())
            multi_hop_expand(self.s, top5, results, depth=2)
            # Collect newly added graph results for RRF tier ranking
            for did in results:
                if did not in before_ids:
                    sc = results[did]["score"]
                    graph_tier.append((did, sc))
                    if _explain:
                        tier_scores.setdefault("graph", {})[int(did)] = float(sc)
        else:
            # Standard 1-hop expansion
            for kid in top5:
                for r in self.s.q("""
                    SELECT k.* FROM relations rel
                    JOIN knowledge k ON k.id = CASE WHEN rel.from_id=? THEN rel.to_id ELSE rel.from_id END
                    WHERE (rel.from_id=? OR rel.to_id=?) AND k.status='active'
                """, (kid, kid, kid)):
                    if r["id"] not in results:
                        graph_score = results[kid]["score"] * 0.4
                        results[r["id"]] = {"r": r, "score": graph_score, "via": ["graph"]}
                        graph_tier.append((r["id"], graph_score))
                        if _explain:
                            tier_scores.setdefault("graph", {})[int(r["id"])] = float(graph_score)

        if graph_tier:
            graph_tier.sort(key=lambda x: x[1], reverse=True)
            tier_rankings["graph"] = [doc_id for doc_id, _ in graph_tier]

        # Tier 6: Episode retrieval (v11 W1-A) — coherent (when/who/where/what)
        # windows over fact rows. Each EpisodeHit expands to its constituent
        # knowledge_ids weighted by the episode's fused score.
        if os.environ.get("MEMORY_EPISODE_TIER", "true").strip().lower() in ("1", "true", "yes", "on"):
            try:
                from memory_core.episodes.retriever import retrieve_episodes
                episode_tier: list[tuple[int, float]] = []
                def ep_embed(txt):
                    vecs = self.s.embed([txt])
                    if not vecs:
                        return None
                    v = vecs[0]
                    return v if v is not None else None
                ep_hits = retrieve_episodes(
                    self.s.db, query, project,
                    k=max(5, limit // 2), embed_fn=ep_embed,
                )
                episode_records = fetch_active_records(self.s.db, [
                    fid for hit in ep_hits for fid in (getattr(hit, "fact_ids", ()) or ())[:5]
                    if fid not in results
                ])
                for hit in ep_hits:
                    fact_ids = list(getattr(hit, "fact_ids", ()) or ())
                    if not fact_ids:
                        continue
                    base = float(getattr(hit, "score", 0.0))
                    for rank, fid in enumerate(fact_ids[:5]):
                        if fid not in results:
                            row = episode_records.get(fid)
                            if row is None:
                                continue
                            ep_score = base * (1.0 / (1 + rank))
                            results[fid] = {"r": dict(row), "score": ep_score, "via": ["episode"]}
                            episode_tier.append((fid, ep_score))
                            if _explain:
                                tier_scores.setdefault("episode", {})[int(fid)] = float(ep_score)
                        else:
                            if "episode" not in results[fid]["via"]:
                                results[fid]["via"].append("episode")
                            episode_tier.append((fid, results[fid]["score"]))
                if episode_tier:
                    seen: set = set()
                    deduped: list[int] = []
                    for fid, _ in sorted(episode_tier, key=lambda x: x[1], reverse=True):
                        if fid not in seen:
                            seen.add(fid)
                            deduped.append(fid)
                    tier_rankings["episode"] = deduped
            except sqlite3.OperationalError:
                # Migration 023 not yet applied (e.g. legacy DB) — skip silently.
                pass
            except Exception as e:
                LOG(f"episode tier error: {e}")

        for kid in list(results):
            if not scope.allows(results[kid]["r"], self.s.db):
                del results[kid]
        from memory_core.telemetry import counters
        for tier, ids in tier_rankings.items():
            counters.bump(f"retrieval_{tier}_candidates", len(ids))
            tier_rankings[tier] = [kid for kid in ids if kid in results]

        # ── Noise filter: drop records carrying excluded tags ──
        # Operational tags like ``recovery`` or ``auto-extract`` mark records
        # that hold value for forensics but inflate recall noise. They remain
        # accessible via the dedicated ``memory_search_by_tag`` path.
        try:
            from config import get_recall_excluded_tags
            _ex = get_recall_excluded_tags()
        except Exception:
            _ex = tuple()
        if _ex:
            def _has_excluded(rec_tags) -> bool:
                if not rec_tags:
                    return False
                if isinstance(rec_tags, str):
                    raw = rec_tags
                else:
                    try:
                        raw = json.dumps(rec_tags)
                    except Exception:
                        raw = str(rec_tags)
                low = raw.lower()
                return any(t.lower() in low for t in _ex)

            dropped: set[int] = set()
            for kid, item in list(results.items()):
                if _has_excluded(item["r"].get("tags", "")):
                    dropped.add(kid)
                    results.pop(kid, None)
            if dropped:
                for tier_name, ranked_ids in list(tier_rankings.items()):
                    tier_rankings[tier_name] = [
                        d for d in ranked_ids if d not in dropped
                    ]

        # ── Drift detection (Wave B — B1) ──
        # When a hit came through the multi_repr tier, the matched view
        # (summary/keywords/questions/compressed) may have been generated
        # against an older version of the parent content. We compare the
        # stored ``parent_content_hash`` with sha256 of the *current* parent
        # content; mismatch → score penalty AND re-enqueue for regeneration.
        # Records that fall through this check are surfaced with a clean
        # ``"drift"`` flag in the result so the dashboard can warn.
        try:
            from multi_repr_store import content_hash as _content_hash
            drift_candidates = [
                (kid, item)
                for kid, item in results.items()
                if "multi_repr" in (item.get("via") or [])
                and item.get("matched_repr")
            ]
            if drift_candidates:
                kids = [c[0] for c in drift_candidates]
                ph = ",".join("?" * len(kids))
                rows = self.s.db.execute(
                    f"SELECT knowledge_id, representation, parent_content_hash "
                    f"FROM knowledge_representations "
                    f"WHERE knowledge_id IN ({ph})",
                    kids,
                ).fetchall()
                stored_hash = {
                    (r["knowledge_id"], r["representation"]): r["parent_content_hash"]
                    for r in rows
                }
                drifted: list[int] = []
                for kid, item in drift_candidates:
                    rep = item["matched_repr"]
                    stored = stored_hash.get((kid, rep))
                    if not stored:
                        # Legacy view (no hash yet) — leave alone.
                        continue
                    actual = _content_hash(item["r"].get("content", "") or "")
                    if stored != actual:
                        item["drift"] = True
                        item["score"] *= 0.3
                        drifted.append(kid)
                if drifted:
                    # Asynchronously re-enqueue for regeneration so the next
                    # recall sees a fresh summary. Best-effort: silent on
                    # error (queue table may not exist on legacy DB).
                    try:
                        from representations_queue import RepresentationsQueue
                        rq = RepresentationsQueue(self.s.db)
                        for kid in drifted:
                            rq.enqueue(kid)
                    except Exception as _e:
                        LOG(f"drift re-enqueue failed: {_e}")
        except Exception as e:
            LOG(f"drift detection skipped: {e}")

        # ── Apply decay scoring ──
        # Per-tier half-life: hits via multi_repr decay according to the
        # specific view (summary/keywords/questions/compressed) that won
        # cosine; other tiers (fts/semantic/fuzzy/graph/episode) use the
        # parent half-life. LLM-generated summaries age fastest.
        try:
            from config import get_parent_half_life_days, get_repr_half_life_days
        except Exception:
            get_repr_half_life_days = lambda _t: DECAY_HALF_LIFE  # type: ignore[assignment]
            get_parent_half_life_days = lambda: DECAY_HALF_LIFE   # type: ignore[assignment]

        for item in results.values():
            lc = item["r"].get("last_confirmed", "")
            via = item.get("via") or []
            if "multi_repr" in via and item.get("matched_repr"):
                hl = get_repr_half_life_days(item["matched_repr"])
                item["half_life_days"] = hl
            else:
                hl = get_parent_half_life_days()
                item["half_life_days"] = hl
            # recall_count is not a ranking signal: it counts how often a record
            # was returned, not whether it helped, so boosting by it promoted
            # whatever generic rows matched common words (-4.7 pts R@10 on
            # LoCoMo within one pass of an agent's own queries).
            item["decay_factor"] = Store._decay_factor(lc, hl)
            # v10 — importance boost (defaults to neutral 1.0 for legacy rows
            # without the column or with NULL/unknown values).
            imp = (item["r"].get("importance") or "medium").lower()
            item["importance_boost"] = _IMPORTANCE_BOOST.get(imp, 1.0)

        # B2 — per-tier decay closure for RRF.
        # For each (doc, tier) contribution we apply the half-life appropriate
        # to that tier: multi_repr uses the per-view half-life (summary fastest,
        # raw slowest), every other tier uses the parent half-life. Importance
        # is *outside* the closure — multiplied
        # against the final fused score so it scales the document as a whole.
        def _tier_score_weight(doc_id, tier_name):
            item = results.get(doc_id)
            if not item:
                return 1.0
            lc = item["r"].get("last_confirmed", "")
            if tier_name == "multi_repr" and item.get("matched_repr"):
                hl = get_repr_half_life_days(item["matched_repr"])
            else:
                hl = get_parent_half_life_days()
            return Store._decay_factor(lc, hl)

        # ── Score fusion: RRF or legacy ──
        if use_rrf and tier_rankings:
            # Compute RRF scores with per-tier decay folded in
            rrf_scores = self._rrf_fuse(
                tier_rankings, {**self.RRF_WEIGHTS, "atomic_facts":
                    0.0 if any(ids for tier, ids in tier_rankings.items() if tier != "atomic_facts") else 1.0},
                self.RRF_K,
                score_weight=_tier_score_weight,
            )

            # Apply importance boost on the fused score (per-tier decay is
            # already inside rrf_scores). Drift penalty already lowered
            # `item["score"]` upstream — keep it in sync for additive paths.
            for doc_id, rrf_sc in rrf_scores.items():
                if doc_id in results:
                    item = results[doc_id]
                    boost = item["importance_boost"]
                    item["rrf_score"] = rrf_sc * boost * (0.3 if item.get("drift") else 1.0)
                    item["score"] *= item["decay_factor"] * boost

            # Documents not in any tier ranking (shouldn't happen, but safety net)
            for doc_id, item in results.items():
                if "rrf_score" not in item:
                    multiplier = item["decay_factor"] * item["importance_boost"]
                    item["score"] *= multiplier
                    item["rrf_score"] = item["score"] * 0.5  # penalized fallback

            # Sort by fused RRF score
            ranked = sorted(results.values(), key=lambda x: x.get("rrf_score", 0), reverse=True)[:pool * 2]
        else:
            # Legacy: apply decay + importance to additive scores
            for item in results.values():
                item["score"] *= item["decay_factor"] * item["importance_boost"]
            ranked = sorted(results.values(), key=lambda x: x["score"], reverse=True)[:pool * 2]

        # Stage 4.3 (optional): Temporal-index hard filter.
        # When query has explicit dates and index is populated, drop candidates
        # whose timestamps fall outside the query window. Different from 4.5
        # (proximity re-rank) — this is a binary admit/reject.
        # Toggle: MEMORY_TEMPORAL_INDEX=1.
        if (HAS_TEMPORAL_INDEX
                and os.environ.get("MEMORY_TEMPORAL_INDEX", "0") == "1"
                and len(ranked) > 1):
            try:
                allowed_ids = _temporal_index_filter(self.s.db, original_query)
                if allowed_ids:
                    filtered = [it for it in ranked if it["r"]["id"] in allowed_ids]
                    if filtered:
                        ranked = filtered
            except Exception as e:
                LOG(f"temporal_index filter failed, keeping full ranked list: {e}")

        # Stage 4.5 (optional): Temporal-aware re-rank.
        # Detects date entities in the query, re-orders over-fetched candidates
        # by timestamp proximity. No LLM calls. +11pp Acc on LoCoMo temporal,
        # +5pp on multi-hop. Neutral on non-temporal queries (skip-path).
        # Toggle: MEMORY_TEMPORAL_FILTER (default "1", set "0" to disable).
        if (HAS_TEMPORAL_FILTER
                and os.environ.get("MEMORY_TEMPORAL_FILTER", "1") != "0"
                and len(ranked) > 1):
            try:
                if has_temporal_intent(query):
                    adapted = [{"content": item["r"].get("content", ""),
                                "score": item.get("rrf_score", item.get("score", 0)),
                                "_orig": item}
                               for item in ranked]
                    reordered = temporal_rerank(query, adapted)
                    ranked = [e["_orig"] for e in reordered]
            except Exception as e:
                LOG(f"Temporal filter failed, keeping RRF order: {e}")

        # Stage 4.8: cross-encoder over the fused window (fastembed, local).
        # Its rank joins the RRF rank; see memory_core/cross_rerank.py.
        if cross_active and len(ranked) > 1:
            from config import get_cross_rerank_mode
            from memory_core.cross_rerank import session_window_texts
            try:
                db = self.s.db

                def with_neighbours(window):
                    texts = session_window_texts(db, [item["r"] for item in window], side_chars=cross.context_chars)
                    return [texts.get(item["r"]["id"], item["r"].get("content", "")) for item in window]

                def recency(item):
                    return item["r"].get("created_at") or "", item["r"]["id"]

                ranked = cross.rerank(query, ranked, lambda item: item["r"].get("content", ""),
                                      wait=get_cross_rerank_mode() == "on",
                                      context_of=with_neighbours if cross.context_chars else None,
                                      recency_of=recency)
            except Exception as e:  # noqa: BLE001 — onnxruntime raises its own types; keep the fused order
                from memory_core.telemetry import counters as _cross_counters
                LOG(f"cross-rerank failed, keeping fused order: {e}")
                _cross_counters.bump("cross_rerank_errors")

        # Stage 5 (optional): CrossEncoder re-ranking
        # CE is trained on MS-MARCO (web search) — helps for precision in large bases,
        # but can hurt recall on conversational data. Off by default.
        if rerank and HAS_RERANKER and len(ranked) > 1:
            try:
                if os.environ.get("MEMORY_BOUNDED_RERANK", "false").lower() in ("true", "1", "yes"):
                    from ai_layer.bounded_reranker import BoundedReranker
                    from memory_core.query_terms import lexical_terms
                    if len(lexical_terms(query)) >= 3:
                        if self._bounded_reranker is None:
                            self._bounded_reranker = BoundedReranker(
                                os.environ.get("MEMORY_RERANK_LOCAL_MODEL", ""),
                                deadline_ms=int(os.environ.get("MEMORY_RERANK_DEADLINE_MS", "250")),
                                candidates=int(os.environ.get("MEMORY_RERANK_CANDIDATES", "20")),
                                startup_seconds=float(os.environ.get("MEMORY_RERANK_STARTUP_SECONDS", "60")))
                        ordered = self._bounded_reranker.rank(query, [item["r"] for item in ranked])
                        by_id = {item["r"]["id"]: item for item in ranked}
                        ranked = [by_id[hit["id"]] for hit in ordered]
                    ranked = ranked[:limit]
                else:
                    ranked = rerank_results(query, ranked, top_k=limit)
            except Exception as e:
                LOG(f"Reranker failed, using original ranking: {e}")
                ranked = ranked[:limit]
        else:
            ranked = ranked[:limit]

        # Stage 6 (optional): MMR diversity
        # Useful for broad queries ("what do I know about X") to get different aspects.
        # Off by default — hurts recall when similar docs contain the answer.
        if diverse and HAS_RERANKER and len(ranked) > 1:
            try:
                contents = [item["r"].get("content", "")[:300] for item in ranked]
                embs = self.s.embed(contents)
                if embs and len(embs) == len(ranked):
                    ranked = mmr_diversify(ranked, embs, top_k=limit)
            except Exception as e:
                LOG(f"MMR diversify failed, using original order: {e}")

        # v10 — Smart query router. For relational questions ("who worked
        # on X with Y", "связь между X и Y"), inject knowledge rows reached
        # via the episodic graph alongside the regular hybrid hits. The
        # classifier is heuristic (no LLM call) so this is cheap on every
        # query. The classification metadata propagates into the response
        # so callers can see which path produced the result.
        router_classification = None
        try:
            import query_router as _qr_router
            router_classification = _qr_router.classify_query(query)
            if (router_classification.kind == "relational"
                    and router_classification.entities):
                graph_rows = _qr_router.graph_search(
                    self.s.db,
                    entities=router_classification.entities,
                    project=project,
                    limit=max(5, limit),
                )
                if graph_rows:
                    existing_ids = {item["r"]["id"] for item in ranked}
                    base_score = (
                        max((it.get("rrf_score", it.get("score", 0))
                             for it in ranked), default=0.0)
                        if ranked else 1.0
                    )
                    boost = float(
                        os.environ.get("MEMORY_RELATIONAL_BOOST", "1.3")
                    )
                    for row in graph_rows:
                        rid_g = row.get("id")
                        if rid_g in existing_ids:
                            continue
                        ranked.append({
                            "r": row,
                            "score": base_score * 0.6,
                            "via": ["relational_router"],
                            "rrf_score": base_score * boost * 0.6,
                            "decay_factor": 1.0,
                            "importance_boost": 1.0,
                        })
                    # Re-sort so the boosted relational rows compete fairly.
                    ranked.sort(
                        key=lambda x: x.get("rrf_score", x.get("score", 0)),
                        reverse=True,
                    )
                    ranked = ranked[:limit]
        except Exception as e:
            LOG(f"smart router skipped: {e}")

        # Stage 6.5 (optional): Graph expansion — add 1-hop neighbours
        # of the top-K to help multi-hop questions. Neighbours get a
        # penalised score (0.5× min of the top score) so they never
        # outrank primary hits.
        if (HAS_GRAPH_EXPAND
                and os.environ.get("MEMORY_GRAPH_EXPAND", "0") == "1"
                and len(ranked) > 0):
            try:
                seed_ids = [item["r"]["id"] for item in ranked[:3]]
                existing_ids = {item["r"]["id"] for item in ranked}
                neighbours = _graph_expand(self.s.db, seed_ids, budget=10)
                new_kids = [kid for kid, _ in neighbours if kid not in existing_ids][:5]
                if new_kids:
                    rows = _graph_fetch(self.s.db, new_kids)
                    if rows and ranked:
                        base_score = min(it["score"] for it in ranked) * 0.5
                        for row in rows:
                            ranked.append({
                                "r": row,
                                "score": base_score,
                                "via": ["graph_expand"],
                                "rrf_score": 0.0,
                            })
            except Exception as e:
                LOG(f"graph_expand failed, keeping original ranked: {e}")

        ranked = [item for item in ranked if scope.allows(item["r"], self.s.db)][:limit]

        # Usage statistics (recall_count, last_recalled) for the dashboard and
        # consolidation; they do not affect ranking. Callers that measure
        # retrieval — benchmarks, evals, memory_explain_search — pass
        # record_usage=False so their runs leave no trace in those statistics.
        if record_usage:
            returned_ids = [item["r"]["id"] for item in ranked]
            if returned_ids:
                self.s.bump_recall(returned_ids)

        total_tokens = 0
        grouped = {}
        for item in ranked:
            r = item["r"]
            t = r["type"]
            if t not in grouped:
                grouped[t] = []
            tags = r.get("tags", "[]")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []

            content = r["content"]
            context = r.get("context", "")

            importance = (r.get("importance") or "medium").lower()

            if detail == "compact":
                # ~50 tokens per result
                entry = {
                    "id": r["id"], "type": t,
                    "title": content[:80] + ("..." if len(content) > 80 else ""),
                    "project": r.get("project", ""),
                    "score": round(item["score"], 3),
                    "importance": importance,
                    "created_at": r.get("created_at", ""),
                }
                if "rrf_score" in item:
                    entry["rrf_score"] = round(item["rrf_score"], 6)
                est = Store._estimate_tokens(json.dumps(entry))
                entry["_tokens"] = est
                total_tokens += est
                grouped[t].append(entry)
            elif detail == "summary":
                content = content[:150] + ("..." if len(content) > 150 else "")
                context = ""
                entry = {
                    "id": r["id"], "content": content, "context": context,
                    "project": r.get("project", ""), "tags": tags,
                    "confidence": r.get("confidence", 1.0),
                    "importance": importance,
                    "created_at": r.get("created_at", ""), "session_id": r.get("session_id", ""),
                    "score": round(item["score"], 3), "via": item["via"],
                    "recall_count": r.get("recall_count", 0),
                    "decay": round(Store._decay_factor(r.get("last_confirmed", ""), DECAY_HALF_LIFE), 3),
                }
                if "rrf_score" in item:
                    entry["rrf_score"] = round(item["rrf_score"], 6)
                est = Store._estimate_tokens(json.dumps(entry))
                entry["_tokens"] = est
                total_tokens += est
                grouped[t].append(entry)
            else:  # full
                # v11 Phase 6b — surface the embedding_space so callers can
                # verify a space filter took effect. We pull it from the
                # row first (FTS/fuzzy/graph rows include it implicitly via
                # SELECT k.*), and fall back to a cheap lookup against the
                # embeddings table for tiers that started from a kid only.
                _v11_es = r.get("embedding_space")
                if _v11_es is None:
                    try:
                        _v11_es_row = self.s.db.execute(
                            "SELECT embedding_space FROM embeddings WHERE knowledge_id=?",
                            (r["id"],),
                        ).fetchone()
                        if _v11_es_row is not None:
                            _v11_es = _v11_es_row[0]
                    except Exception:
                        _v11_es = None
                entry = {
                    "id": r["id"], "content": content, "context": context,
                    "project": r.get("project", ""), "tags": tags,
                    "confidence": r.get("confidence", 1.0),
                    "importance": importance,
                    "created_at": r.get("created_at", ""), "session_id": r.get("session_id", ""),
                    "score": round(item["score"], 3), "via": item["via"],
                    "recall_count": r.get("recall_count", 0),
                    "decay": round(Store._decay_factor(r.get("last_confirmed", ""), DECAY_HALF_LIFE), 3),
                    "branch": r.get("branch", ""),
                    "embedding_space": _v11_es,
                }
                if "rrf_score" in item:
                    entry["rrf_score"] = round(item["rrf_score"], 6)
                est = Store._estimate_tokens(json.dumps(entry))
                entry["_tokens"] = est
                total_tokens += est
                grouped[t].append(entry)

        result = {"query": query, "total": len(ranked), "detail": detail,
                  "fusion": fusion, "total_tokens": total_tokens, "results": grouped}
        # v11 Phase 6b — record the requested embedding_space at the top level.
        if _v11_spaces:
            result["embedding_space"] = (
                _v11_spaces[0] if len(_v11_spaces) == 1 else list(_v11_spaces)
            )
        if use_rrf and tier_rankings:
            result["tiers_used"] = list(tier_rankings.keys())
        if router_classification is not None:
            result["routed_via"] = router_classification.kind
            result["routed_signals"] = router_classification.signals
            if router_classification.entities:
                result["routed_entities"] = router_classification.entities

        cacheable = cacheable and not self.s.db.in_transaction
        self._cache_revision = (self.s.db.total_changes, revision[1]) if cacheable else None

        result["semantic_diagnostics"] = getattr(self.s, "_semantic_diagnostics", [])

        # Cache the result. _explain payloads are NOT cached — they include
        # ephemeral tier rankings tied to a single execution.
        if cacheable and not _explain and self.s.cache is not None:
            cache_key = self.s.cache.make_key(query=query, project=project, ktype=ktype,
                                               limit=limit, detail=detail, branch=branch,
                                               fusion=fusion, rerank=rerank, diverse=diverse,
                                               embedding_space=",".join(_v11_spaces) if _v11_spaces else None)
            self.s.cache.put(cache_key, result, project=project)

        # v9 A2 L1: mirror into fast LRU tagged with the ids this result touched.
        if cacheable and not _explain and _v9 is not None and _v9.l1.enabled:
            try:
                _ids: list[int] = []
                for _tier in result.get("results", {}).values():
                    for _item in _tier:
                        _id = _item.get("id")
                        if isinstance(_id, int):
                            _ids.append(_id)
                _v9.recall_set(query, result, mode="search", k=limit,
                               filters=_v9_filters, memory_ids=_ids)
            except Exception:
                pass

        # v11 Phase 6 — explain payload for `memory_explain_search`.
        if _explain:
            def _tier_pairs(tier_name: str, score_label: str) -> list[dict]:
                """Build [{id, <score_label>}] from `tier_scores` keeping
                the ranking order from `tier_rankings`."""
                scores_for_tier = tier_scores.get(tier_name, {})
                pairs: list[dict] = []
                for did in tier_rankings.get(tier_name, []):
                    sc = scores_for_tier.get(int(did))
                    pairs.append({
                        "id": int(did),
                        score_label: round(float(sc), 4) if sc is not None else None,
                    })
                return pairs

            merged = []
            for item in ranked:
                merged.append({
                    "id": int(item["r"]["id"]),
                    "score": round(float(item.get("rrf_score", item.get("score", 0))), 4),
                    "via": list(item.get("via", [])),
                })

            result["_explain"] = {
                "fts": _tier_pairs("fts", "bm25"),
                "semantic": _tier_pairs("semantic", "cos"),
                "graph": _tier_pairs("graph", "score"),
                "fuzzy": _tier_pairs("fuzzy", "ratio"),
                "hyde": _tier_pairs("hyde", "score"),
                "merged": merged,
                "rerank_applied": bool(rerank and HAS_RERANKER),
                "embedding_space": (
                    _v11_spaces[0] if (_v11_spaces and len(_v11_spaces) == 1)
                    else (list(_v11_spaces) if _v11_spaces else None)
                ),
                "tiers_used": list(tier_rankings.keys()),
            }

        return result

    def timeline(self, query=None, session_number=None, sessions_ago=None,
                 date_from=None, date_to=None, project=None, limit=5):
        total = self.s.total_sessions()

        if sessions_ago is not None:
            offset = max(0, total - sessions_ago - 1)
            sessions = self.s.q("SELECT * FROM sessions ORDER BY started_at ASC LIMIT ? OFFSET ?", (limit, offset))
        elif session_number is not None:
            offset = max(0, (total + session_number) if session_number < 0 else (session_number - 1))
            sessions = self.s.q("SELECT * FROM sessions ORDER BY started_at ASC LIMIT ? OFFSET ?", (limit, offset))
        elif date_from or date_to:
            c, p = ["1=1"], []
            if date_from:
                c.append("started_at>=?"); p.append(date_from)
            if date_to:
                c.append("started_at<=?"); p.append(date_to + "T23:59:59Z")
            if project:
                c.append("project=?"); p.append(project)
            p.append(limit)
            sessions = self.s.q(f"SELECT * FROM sessions WHERE {' AND '.join(c)} ORDER BY started_at DESC LIMIT ?", p)
        elif query:
            fts_q = " OR ".join(Store._fts_escape(w) for w in query.split() if len(w) > 2) or Store._fts_escape(query)
            sids = set()
            try:
                for r in self.s.q(
                    "SELECT DISTINCT k.session_id as sid FROM knowledge_fts f "
                    "JOIN knowledge k ON k.id=f.rowid WHERE f.content MATCH ? LIMIT ?",
                    (fts_q, limit * 3)):
                    sids.add(r["sid"])
            except Exception:
                pass
            for r in self.s.q("SELECT id FROM sessions WHERE summary LIKE ? LIMIT ?", (f"%{query}%", limit * 2)):
                sids.add(r["id"])
            sids = list(sids)[:limit]
            if sids:
                ph = ",".join("?" * len(sids))
                sessions = self.s.q(f"SELECT * FROM sessions WHERE id IN ({ph}) ORDER BY started_at DESC", sids)
            else:
                sessions = []
        else:
            offset = max(0, total - limit)
            sessions = self.s.q("SELECT * FROM sessions ORDER BY started_at ASC LIMIT ? OFFSET ?", (limit, offset))

        result = []
        for sess in sessions:
            num = self.s.db.execute(
                "SELECT COUNT(*) FROM sessions WHERE started_at<=?",
                (sess["started_at"],)).fetchone()[0]
            events = self.s.q("SELECT * FROM timeline WHERE session_id=? ORDER BY ts LIMIT 30", (sess["id"],))
            knowledge = self.s.q("SELECT * FROM knowledge WHERE session_id=? AND status='active'", (sess["id"],))
            result.append({**sess, "session_number": num, "events": events, "knowledge": knowledge})

        return {"total_sessions": total, "returned": len(result), "sessions": result}

    def stats(self):
        s = self.s
        active = s.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='active'").fetchone()[0]
        archived = s.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='archived'").fetchone()[0]
        consolidated = s.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='consolidated'").fetchone()[0]
        superseded = s.db.execute("SELECT COUNT(*) FROM knowledge WHERE status='superseded'").fetchone()[0]
        by_type = dict(s.db.execute(
            "SELECT type,COUNT(*) FROM knowledge WHERE status='active' GROUP BY type").fetchall())
        by_project = dict(s.db.execute(
            "SELECT project,COUNT(*) FROM knowledge WHERE status='active' GROUP BY project").fetchall())

        # Health metrics
        stale = s.db.execute("""
            SELECT COUNT(*) FROM knowledge
            WHERE status='active' AND last_confirmed < strftime('%Y-%m-%dT%H:%M:%f000Z', 'now', '-90 days')
        """).fetchone()[0]
        never_recalled = s.db.execute("""
            SELECT COUNT(*) FROM knowledge WHERE status='active' AND (recall_count=0 OR recall_count IS NULL)
        """).fetchone()[0]

        raw_mb = sum(f.stat().st_size for f in (MEMORY_DIR / "raw").iterdir() if f.is_file()) / 1048576
        trans_mb = sum(f.stat().st_size for f in (MEMORY_DIR / "transcripts").iterdir() if f.is_file()) / 1048576
        db_mb = (MEMORY_DIR / "memory.db").stat().st_size / 1048576 if (MEMORY_DIR / "memory.db").exists() else 0
        chroma_mb = 0
        chroma_dir = MEMORY_DIR / "chroma"
        if chroma_dir.exists():
            chroma_mb = sum(f.stat().st_size for f in chroma_dir.rglob("*") if f.is_file()) / 1048576

        # Binary quantization stats
        try:
            embed_count = s.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            embed_bytes = s.db.execute(
                "SELECT COALESCE(SUM(LENGTH(binary_vector) + LENGTH(float32_vector)), 0) FROM embeddings"
            ).fetchone()[0]
            embed_mb = embed_bytes / 1048576
        except Exception:
            embed_count = 0
            embed_mb = 0

        # Filter savings — total tokens approx saved by content_filter
        try:
            fs_row = s.db.execute(
                "SELECT COUNT(*) AS n, "
                "       COALESCE(SUM(input_chars), 0) AS inp, "
                "       COALESCE(SUM(output_chars), 0) AS outp "
                "FROM filter_savings"
            ).fetchone()
            fs_inp = int(fs_row["inp"]) if fs_row else 0
            fs_out = int(fs_row["outp"]) if fs_row else 0
            filter_savings = {
                "applied_count": int(fs_row["n"]) if fs_row else 0,
                "chars_saved": fs_inp - fs_out,
                "tokens_saved_estimate": (fs_inp - fs_out) // 4,
                "total_reduction_pct": (
                    round((1 - fs_out / fs_inp) * 100, 1) if fs_inp else 0.0
                ),
            }
        except Exception:
            filter_savings = {"applied_count": 0, "chars_saved": 0, "tokens_saved_estimate": 0, "total_reduction_pct": 0.0}

        # v6.0 async queues — visibility for operators
        def _queue_counts(table: str) -> dict:
            try:
                rows = s.db.execute(
                    f"SELECT status, COUNT(*) AS c FROM {table} GROUP BY status"
                ).fetchall()
                out = {"pending": 0, "processing": 0, "done": 0, "failed": 0}
                for r in rows:
                    out[r[0]] = r[1]
                return out
            except Exception:
                return {"pending": 0, "processing": 0, "done": 0, "failed": 0, "error": "table missing"}

        queues = {
            "triple_extraction": _queue_counts("triple_extraction_queue"),
            "deep_enrichment": _queue_counts("deep_enrichment_queue"),
            "representations": _queue_counts("representations_queue"),
        }
        # v6.0 storage
        try:
            repr_count = s.db.execute(
                "SELECT COUNT(*) FROM knowledge_representations"
            ).fetchone()[0]
        except Exception:
            repr_count = 0
        try:
            enrich_count = s.db.execute(
                "SELECT COUNT(*) FROM knowledge_enrichment"
            ).fetchone()[0]
        except Exception:
            enrich_count = 0

        return {
            "sessions": s.total_sessions(),
            "knowledge": {
                "active": active,
                "archived": archived,
                "consolidated": consolidated,
                "superseded": superseded,
            },
            "by_type": by_type,
            "by_project": by_project,
            "health": {
                "stale_90d": stale,
                "never_recalled": never_recalled,
                "health_score": round(max(0, 1.0 - (stale / max(active, 1)) * 0.5 - (never_recalled / max(active, 1)) * 0.3), 2),
            },
            "timeline_events": s.db.execute("SELECT COUNT(*) FROM timeline").fetchone()[0],
            "v6_queues": queues,
            "v6_storage": {
                "representations_rows": repr_count,
                "enrichment_rows": enrich_count,
            },
            "v6_filter_savings": filter_savings,
            "v6_llm": (lambda: __import__("config").get_status())(),
            "storage_mb": {
                "transcripts": round(trans_mb, 1),
                "raw_logs": round(raw_mb, 1),
                "sqlite": round(db_mb, 1),
                "chroma": round(chroma_mb, 1),
                "embeddings": round(embed_mb, 1),
                "total": round(raw_mb + trans_mb + db_mb + chroma_mb, 1),
            },
            "config": {
                "decay_half_life_days": DECAY_HALF_LIFE,
                "archive_after_days": ARCHIVE_AFTER_DAYS,
                "purge_after_days": PURGE_AFTER_DAYS,
                "embedding_model": EMBEDDING_MODEL,
                "fastembed_model": FASTEMBED_MODEL,
                "ollama_embed_model": OLLAMA_EMBED_MODEL,
                "embed_mode": s._embed_mode or "not_initialized",
                "has_chromadb": HAS_CHROMA,
                "has_fastembed": HAS_FASTEMBED,
                "has_sentence_transformers": HAS_ST,
                "binary_search": USE_BINARY_SEARCH,
                "binary_search_active": s._check_binary_search(),
                "embeddings_count": embed_count,
            },
            "self_improvement": self._si_stats(s),
            "observations": self._obs_stats(s),
            "cache": s.cache.stats() if s.cache is not None else {"enabled": False},
        }

    @staticmethod
    def _obs_stats(s):
        """Observations stats (safe: returns empty if table missing)."""
        try:
            total = s.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            by_type = dict(s.db.execute(
                "SELECT observation_type, COUNT(*) FROM observations GROUP BY observation_type"
            ).fetchall())
            return {"total": total, "by_type": by_type}
        except Exception:
            return {}

    @staticmethod
    def _si_stats(s):
        """Self-improvement stats (safe: returns empty if tables missing)."""
        try:
            return {
                "errors": {
                    "total": s.db.execute("SELECT COUNT(*) FROM errors").fetchone()[0],
                    "open": s.db.execute("SELECT COUNT(*) FROM errors WHERE status='open'").fetchone()[0],
                    "by_category": dict(s.db.execute(
                        "SELECT category, COUNT(*) FROM errors GROUP BY category").fetchall()),
                },
                "insights": {
                    "active": s.db.execute("SELECT COUNT(*) FROM insights WHERE status='active'").fetchone()[0],
                    "promoted": s.db.execute("SELECT COUNT(*) FROM insights WHERE status='promoted'").fetchone()[0],
                    "avg_importance": round(
                        s.db.execute("SELECT AVG(importance) FROM insights WHERE status='active'").fetchone()[0] or 0, 1),
                },
                "rules": {
                    "active": s.db.execute("SELECT COUNT(*) FROM rules WHERE status='active'").fetchone()[0],
                    "suspended": s.db.execute("SELECT COUNT(*) FROM rules WHERE status='suspended'").fetchone()[0],
                    "avg_success_rate": round(
                        s.db.execute("SELECT AVG(success_rate) FROM rules WHERE status='active' AND fire_count>0").fetchone()[0] or 0, 2),
                },
            }
        except Exception:
            return {}


# ═══════════════════════════════════════════════════════════
# MCP Server
# ═══════════════════════════════════════════════════════════

try:
    from version import VERSION as _SERVER_VERSION
except Exception:
    _SERVER_VERSION = ""

# serverInfo the client displays. The name tracked the pre-v7 package name
# until 13.0.4, and the version was never passed at all, so every client
# rendered an empty string.
app = Server("total-agent-memory", version=_SERVER_VERSION)
store: Store = None
recall: Recall = None
SID: str = None
BRANCH: str = ""

# mcp SDK 1.x exposed @Server.list_tools() / @Server.call_tool() decorators.
# The 2.x line (protocol revision 2026-07-28) dropped them for
# add_request_handler(method, params_model, handler). We support both so the
# same source runs against an old pinned install and a fresh one.
MCP_SDK_ERA = "1.x" if hasattr(app, "list_tools") else "2.x"


# ── Tool behaviour hints (MCP `annotations`) ──────────────────────────
# Clients use these to decide what may run without a confirmation prompt and
# what to retry. They are hints, not guarantees — the classification below is
# the source of truth for this server and is covered by a test that fails when
# a new tool is added without being classified.
#
# Read-only: answers a question, writes nothing a later read can observe.
# Counters like `recall_count` do not count as environment mutation.
_READ_ONLY_TOOLS = frozenset({
    "analogize", "benchmark", "classify_task", "file_context",
    "kg_at", "kg_timeline", "list_intents",
    "memory_concepts", "memory_consolidate_status", "memory_context_build",
    "memory_episode_recall", "memory_eval_contradictions",
    "memory_eval_entity_consistency", "memory_eval_locomo",
    "memory_eval_long_context", "memory_eval_recall", "memory_eval_temporal",
    "memory_explain_search", "memory_export", "memory_get", "memory_graph",
    "memory_graph_stats", "memory_history", "memory_perf_report",
    "memory_recall", "memory_recall_iterative", "memory_search_by_tag",
    "memory_search_fast", "memory_self_assess", "memory_skill_get",
    "memory_stats", "memory_temporal_query", "memory_timeline",
    "search_intents", "self_patterns", "self_rules_context",
    "task_phases_list", "workflow_predict",
})

# Destructive: removes or overwrites knowledge a user cannot trivially restore.
_DESTRUCTIVE_TOOLS = frozenset({
    "memory_delete", "memory_forget", "memory_update",
    "kg_invalidate_fact", "memory_rebuild_embeddings", "memory_rebuild_fts",
})

# Idempotent writes: running twice with the same arguments changes nothing
# beyond the first run.
_IDEMPOTENT_TOOLS = frozenset({
    "memory_delete", "memory_forget", "memory_rebuild_embeddings",
    "memory_rebuild_fts", "memory_warmup", "memory_graph_index",
    "memory_wiki_generate", "rule_set_phase", "memory_entity_resolve",
})


def _annotate(tool: Tool) -> Tool:
    """Attach MCP behaviour hints to a tool definition."""
    read_only = tool.name in _READ_ONLY_TOOLS
    tool.annotations = ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=False if read_only else tool.name in _DESTRUCTIVE_TOOLS,
        idempotentHint=read_only or tool.name in _IDEMPOTENT_TOOLS,
        # Memory is the canonical closed world: every tool acts on this
        # machine's own store, never on an open set of external entities.
        openWorldHint=False,
    )
    return tool


async def list_tools():
    return [_annotate(t) for t in await _tool_catalogue()]


async def _tool_catalogue():
    return [
        Tool(
            name="memory_recall",
            description="Search ALL memory: decisions, solutions, facts, lessons from ALL past sessions. "
                        "6-stage pipeline: FTS5+BM25 → semantic → fuzzy → graph → (optional) CrossEncoder → (optional) MMR. "
                        "Default: hybrid mode (BM25 + semantic + RRF). "
                        "Use BEFORE starting any task. "
                        "v11.0: routes to fast hot path when MEMORY_MODE=fast (default). "
                        "Use memory_search_fast / memory_explain_search for explicit fast routing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"},
                    "project": {"type": "string", "description": "Filter by project name"},
                    "type": {"type": "string", "enum": ["decision", "fact", "solution", "lesson", "convention", "all"],
                             "default": "all"},
                    "limit": {"type": "integer", "default": 10},
                    "mode": {"type": "string", "enum": ["search", "index", "timeline", "context", "evidence"], "default": "search",
                             "description": "Progressive-disclosure mode: 'search' (default) = normal results, "
                                            "'index' = ultra-compact metadata only (id+title+score+type+project+created_at, "
                                            "~40-60 tok/hit, no cognitive expansion, use memory_get(ids=...) to fetch full content), "
                                            "'timeline' = chronological compact view; "
                                            "'context' = source excerpts; 'evidence' = indexed passage search with bounded follow-up retrieval"},
                    "context_max_chars": {"type": "integer", "minimum": 512, "default": 24000,
                                          "description": "Context character budget; evidence mode uses the same value as a stricter UTF-8 byte budget."},
                    "evidence_followup": {"type": "boolean", "default": True,
                                          "description": "Evidence mode: allow one search for an explicitly supplied missing_relation."},
                    "missing_relation": {"type": "object", "properties": {
                        "subject": {"type": "string"}, "relation": {"type": "string"}, "time": {"type": "string"}},
                        "required": ["subject", "relation"], "additionalProperties": False,
                        "description": "Evidence mode: missing relation; subject must occur in the original question."},
                    "neighbors": {"type": "integer", "default": 2,
                                  "description": "Timeline/context modes: records before/after each hit; context accepts 0–3."},
                    "detail": {"type": "string", "enum": ["compact", "summary", "full", "auto"], "default": "full",
                               "description": "Level of detail: 'compact' ~50 tokens/result (id+title+score), "
                                              "'summary' truncates content to 150 chars, 'full' returns everything, "
                                              "'auto' picks based on query complexity (paths/urls/code → full, short → compact). "
                                              "Ignored when mode!='search'."},
                    "branch": {"type": "string", "description": "Filter by git branch (also includes branch-agnostic records)"},
                    "fusion": {"type": "string", "enum": ["rrf", "legacy"], "default": "rrf",
                               "description": "Score fusion method: 'rrf' = Reciprocal Rank Fusion (better multi-tier ranking), "
                                              "'legacy' = original additive scoring"},
                    "rerank": {"type": "boolean", "default": False,
                               "description": "Enable CrossEncoder re-ranking for higher precision (adds ~30ms latency)"},
                    "diverse": {"type": "boolean", "default": False,
                                "description": "Enable MMR diversity to reduce redundant results (useful for broad queries)"},
                    "expand_context": {"type": "boolean", "default": False,
                                       "description": "Add graph-related records (1-hop neighbors via knowledge graph) as 'expansion' results"},
                    "expand_budget": {"type": "integer", "default": 5,
                                      "description": "Max number of additional records to include via graph expansion"},
                    "topics": {"type": "array", "items": {"type": "string"},
                               "description": "Filter results to records tagged with any of these topics (from deep enrichment)"},
                    "entities": {"type": "array", "items": {"type": "string"},
                                 "description": "Filter by extracted entity names (technology/person/project, case-insensitive)"},
                    "intent": {"type": "string",
                               "description": "Filter by classified intent (question|procedural|fact|decision|problem|solution|incident|plan)"},
                    "decisions_only": {"type": "boolean", "default": False,
                                       "description": "Return only structured decisions (v8.0): type=decision AND tags contain 'structured'. "
                                                      "Results include parsed schema payload under 'decision'."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_timeline",
            description="Browse session history. sessions_ago=N for 'N sessions ago', "
                        "session_number=1 for first session, date_from/date_to for date ranges.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "session_number": {"type": "integer"},
                    "sessions_ago": {"type": "integer"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
            },
        ),
        Tool(
            name="memory_index_passages",
            description="Build local passage indexes before evidence searches. Repeat using next_after_id until remaining=0.",
            inputSchema={"type": "object", "properties": {
                "project": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "after_id": {"type": "integer", "minimum": 0, "default": 0}}, "required": ["project"]},
        ),
        Tool(
            name="memory_answer",
            description="Generate and verify a cited answer using the configured reasoning LLM. "
                        "Evidence carries recording dates; when records about the same subject disagree, the latest one gives the "
                        "current value and the answer names the value it replaced. First runs negative retrieval: a contradiction-seeking "
                        "second search; a score >= 0.60 hands both sides to the reader and answers with a caveat "
                        "(MEMORY_CONTRADICTION_POLICY=abstain refuses instead), 0.30-0.60 answers with a caveat (see `negative`); "
                        "MEMORY_CONTRADICTION_SCORER=jev scores the pairs with TypeSafe's Jev instead of the LLM. "
                        "Up to one missing-relation retrieval and eight LLM calls including inversion retry and bounded quote repair. Explicit project required. "
                        "Citation offsets refer to returned evidence content. Ordinary recall remains local.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string", "minLength": 1}, "project": {"type": "string", "minLength": 1},
                "branch": {"type": "string"}, "type": {"type": "string", "enum": ["all", "fact", "decision", "solution", "lesson", "convention"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                "max_bytes": {"type": "integer", "minimum": 512, "maximum": 24000, "default": 24000},
                "followup": {"type": "boolean", "default": True}}, "required": ["query", "project"], "additionalProperties": False},
        ),
        Tool(
            name="memory_save",
            description="Save knowledge explicitly. Types: decision (MUST include WHY in context), "
                        "solution, lesson, fact, convention. Saving the same words again (case and punctuation "
                        "aside) replaces the stored record, so it carries the latest date; any other text, "
                        "including a changed value, is stored as a new record. "
                        "v10: a quality gate scores the record before save; below-threshold records are "
                        "rejected with a `rejected_by_quality_gate: true` response (override with "
                        "MEMORY_QUALITY_GATE_ENABLED=false). Use `importance` to surface critical "
                        "decisions at recall time (boosts the final RRF score). "
                        "v11.0: routes to fast hot path when MEMORY_MODE=fast (default). "
                        "Use memory_save_fast for explicit fast routing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The knowledge to save"},
                    "source_format": {"type": "string", "enum": ["auto", "conversation"], "default": "auto",
                                      "description": "Conversation preserves dialogue structure and bypasses automatic CLI filters."},
                    "type": {"type": "string", "enum": ["decision", "fact", "solution", "lesson", "convention"]},
                    "project": {"type": "string", "default": "general"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "context": {"type": "string", "description": "Additional context, WHY for decisions"},
                    "branch": {"type": "string", "description": "Git branch this knowledge relates to"},
                    "filter": {"type": "string",
                               "description": "Optional content filter (pytest|cargo|git_status|docker_ps|generic_logs). Trims noisy CLI output while preserving URLs/paths/code."},
                    "importance": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                        "default": "medium",
                        "description": "Recall-time boost: critical x1.5, high x1.2, medium x1.0, low x0.8. Reserve `critical` for migration-blocking decisions and security incidents.",
                    },
                    "coref": {
                        "type": "boolean",
                        "description": "Opt into v10 coreference rewrite — expand pronouns ('after this it broke') into self-contained text using recent session history. Costs ~1s LLM round-trip; default off.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional Claude Code subagent ID (x-claude-code-agent-id header / OTEL agent_id attribute, v2.1.139+). Lets recall trace which subagent produced this knowledge.",
                    },
                    "parent_agent_id": {
                        "type": "string",
                        "description": "Optional parent agent ID (the dispatching Agent tool / parent span). Together with agent_id forms the subagent lineage tree.",
                    },
                    "supersede": {
                        "type": "boolean",
                        "default": False,
                        "description": "Retire active records of the same project and type that this one gives a new value for: same opening words, different trailing value (\"X's citizenship is Argentina\" -> \"... is Armenia\"). Use for single-valued facts only; \"likes jazz\" would retire \"likes rock\". The retired ids are returned as `superseded`.",
                    },
                },
                "required": ["content", "type"],
            },
        ),
        Tool(
            name="memory_update",
            description="Update existing knowledge. Finds old by search query, supersedes it, creates new version.",
            inputSchema={
                "type": "object",
                "properties": {
                    "find": {"type": "string", "description": "Search query to find the old knowledge"},
                    "new_content": {"type": "string", "description": "New content to replace with"},
                    "reason": {"type": "string", "description": "Why updating"},
                },
                "required": ["find", "new_content"],
            },
        ),
        Tool(
            name="memory_stats",
            description="Memory statistics with health metrics: sessions, knowledge by type/project, "
                        "retention zones (active/archived/consolidated), stale records, storage size, config.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="memory_consolidate",
            description="Find and merge duplicate/similar knowledge records. Keeps the longest version, "
                        "supersedes shorter duplicates. Reduces noise in recall results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Consolidate only this project (optional)"},
                    "threshold": {"type": "number", "description": "Similarity threshold 0.0-1.0 (default 0.75)", "default": 0.75},
                    "dry_run": {"type": "boolean", "description": "If true, only show what would be merged", "default": True},
                },
            },
        ),
        Tool(
            name="memory_export",
            description="Export all knowledge as JSON for backup or migration. "
                        "Includes knowledge, sessions, and relations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Export only this project (optional)"},
                    "save_to_file": {"type": "boolean", "description": "Save to <memory-dir>/backups/ (default true)", "default": True},
                },
            },
        ),
        Tool(
            name="memory_forget",
            description="Apply retention policy: archive stale records (>180d, never recalled, low confidence), "
                        "purge very old archived records (>365d). Keeps memory clean.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "description": "If true, only show what would be affected", "default": True},
                },
            },
        ),
        Tool(
            name="memory_wiki_generate",
            description="v10 — Render the per-project wiki digest (top decisions, "
                        "active solutions, conventions, recent changes) as Markdown. "
                        "Pass `project` to refresh one wiki, omit it to refresh all "
                        "active projects. Files land in <MEMORY_DIR>/wikis/<project>.md "
                        "and are deterministic (no LLM call).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project to refresh (omit for all)"},
                },
            },
        ),
        Tool(
            name="memory_get",
            description="Batched fetch by ID — complement to memory_recall(mode='index'). "
                        "Returns full content for ONLY the IDs the caller chose after inspecting an index. "
                        "Typical 3-layer flow: recall(mode='index') → pick IDs → memory_get(ids=[...]).",
            inputSchema={
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "integer"},
                            "description": "Knowledge record IDs (max 50 per call; extras are silently dropped)"},
                    "detail": {"type": "string", "enum": ["summary", "full"], "default": "full",
                               "description": "'summary' truncates content to 150 chars, 'full' returns everything"},
                },
                "required": ["ids"],
            },
        ),
        Tool(
            name="memory_history",
            description="View version history for a knowledge record. Shows the chain of superseded versions "
                        "(newest → oldest), enabling time-travel through knowledge evolution.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Knowledge record ID to get history for"},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="memory_delete",
            description="Delete a knowledge record (soft-delete). Removes from search results and ChromaDB. "
                        "Use when knowledge is wrong or no longer relevant.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "Knowledge record ID to delete"},
                },
                "required": ["id"],
            },
        ),
        Tool(
            name="memory_relate",
            description="Create a typed relation between two knowledge records. Enriches graph expansion in Tier 4 search. "
                        "Types: causal, solution, context, related, contradicts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_id": {"type": "integer", "description": "Source knowledge record ID"},
                    "to_id": {"type": "integer", "description": "Target knowledge record ID"},
                    "type": {"type": "string", "enum": ["causal", "solution", "context", "related", "contradicts"],
                             "description": "Relation type"},
                },
                "required": ["from_id", "to_id", "type"],
            },
        ),
        Tool(
            name="memory_search_by_tag",
            description="Search knowledge by tag. Returns all active records with matching tag (partial match). "
                        "Useful for categorical browsing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Tag to search for (partial match)"},
                    "project": {"type": "string", "description": "Filter by project (optional)"},
                },
                "required": ["tag"],
            },
        ),
        Tool(
            name="memory_extract_session",
            description="Get pending session transcripts for knowledge extraction. "
                        "Previous sessions are auto-captured on exit. Use action='list' to see pending, "
                        "'get' to read transcript, then save knowledge via memory_save, "
                        "then 'complete' to mark as processed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "complete"],
                        "description": "list: show pending sessions. get: return transcript data. complete: mark as done.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session ID (required for 'get' and 'complete')",
                    },
                    "chunk": {
                        "type": "integer",
                        "description": "Chunk number for large transcripts (0-based)",
                        "default": 0,
                    },
                },
                "required": ["action"],
            },
        ),
        # ── Self-Improvement Tools ──
        Tool(
            name="self_error_log",
            description="Log an error/failure for pattern analysis. Call AUTOMATICALLY when: "
                        "bash command fails, wrong assumption discovered, API returns error, "
                        "config issue found, loop detected, or any mistake occurs. "
                        "System detects patterns (3+ same category) and suggests insights.",
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {"type": "string",
                                    "description": "What went wrong: symptom, expectation vs reality"},
                    "category": {"type": "string",
                                 "enum": ["code_error", "logic_error", "config_error", "api_error",
                                          "timeout", "loop_detected", "wrong_assumption", "missing_context"],
                                 "description": "Error category for pattern grouping"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"],
                                 "default": "medium"},
                    "fix": {"type": "string", "description": "How it was fixed (empty if unresolved)",
                            "default": ""},
                    "context": {"type": "string", "description": "What was being done when error occurred",
                                "default": ""},
                    "project": {"type": "string", "default": "general"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["description", "category"],
            },
        ),
        Tool(
            name="self_insight",
            description="Manage insights from error patterns (ExpeL-style). Actions: "
                        "add (create, importance=2), upvote (+1), downvote (-1, auto-archive at 0), "
                        "edit, list, promote (to rule when importance>=5 AND confidence>=0.8). "
                        "Call 'add' when pattern detected. Call 'upvote' when insight confirmed again.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["add", "upvote", "downvote", "edit", "list", "promote"]},
                    "id": {"type": "integer", "description": "Insight ID (for upvote/downvote/edit/promote)"},
                    "content": {"type": "string", "description": "Insight text (for add/edit)"},
                    "category": {"type": "string", "description": "Error category (for add)"},
                    "context": {"type": "string", "default": ""},
                    "source_error_ids": {"type": "array", "items": {"type": "integer"},
                                         "description": "Error IDs that spawned this (for add)"},
                    "project": {"type": "string", "default": "general"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="self_rules",
            description="Manage behavioral rules (SOUL). Rules are promoted insights that shape agent behavior. "
                        "Actions: list, fire (record relevance), rate (success=true/false), "
                        "suspend, activate, retire, add_manual. "
                        "Auto-suspend: success_rate < 0.2 after 10+ fires.",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "fire", "rate", "suspend", "activate", "retire", "add_manual"]},
                    "id": {"type": "integer", "description": "Rule ID (for fire/rate/suspend/activate/retire)"},
                    "success": {"type": "boolean", "description": "For rate: was rule helpful?"},
                    "content": {"type": "string", "description": "Rule text (for add_manual)"},
                    "category": {"type": "string", "description": "Category (for add_manual)"},
                    "scope": {"type": "string", "default": "global",
                              "description": "global | project:<name> | category:<name>"},
                    "priority": {"type": "integer", "default": 5, "description": "1-10"},
                    "project": {"type": "string", "default": "general"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action"],
            },
        ),
        Tool(
            name="self_patterns",
            description="Analyze error patterns and self-improvement stats. Views: "
                        "error_patterns (frequency, repeating 3+), insight_candidates (ready for promotion), "
                        "rule_effectiveness (success rates, stale rules), improvement_trend (weekly errors), "
                        "full_report (all). Call periodically to track improvement.",
            inputSchema={
                "type": "object",
                "properties": {
                    "view": {"type": "string",
                             "enum": ["error_patterns", "insight_candidates", "rule_effectiveness",
                                      "improvement_trend", "full_report"],
                             "default": "full_report"},
                    "project": {"type": "string"},
                    "days": {"type": "integer", "default": 30},
                },
            },
        ),
        Tool(
            name="self_reflect",
            description="Save a verbal self-reflection (Reflexion pattern). "
                        "Call after completing a task or encountering difficulty. "
                        "NOT for errors (use self_error_log). For meta-observations about strategy, "
                        "approach effectiveness, process improvements.",
            inputSchema={
                "type": "object",
                "properties": {
                    "reflection": {"type": "string",
                                   "description": "What went well, what to improve, what to do differently"},
                    "task_summary": {"type": "string", "description": "Brief description of what was done"},
                    "outcome": {"type": "string", "enum": ["success", "partial", "failure", "ongoing"],
                                "default": "success"},
                    "project": {"type": "string", "default": "general"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["reflection", "task_summary"],
            },
        ),
        Tool(
            name="self_rules_context",
            description="Get active behavioral rules for current session. "
                        "Call at SESSION START to load rules. Returns rules filtered by project and scope. "
                        "v8.0: pass `phase` to lazy-load rules relevant to current task phase — core "
                        "rules (no phase tag) + rules tagged phase:<X>. Cuts prompt tokens ~70%. "
                        "After task completion, rate rules: self_rules(action='rate', id=X, success=true/false).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "general"},
                    "categories": {"type": "array", "items": {"type": "string"},
                                   "description": "Error categories relevant to current task"},
                    "phase": {"type": "string",
                              "enum": ["van", "plan", "creative", "build", "reflect", "archive"],
                              "description": "Optional: lazy-load only rules relevant to this phase "
                                             "(core + phase-specific). Omit to get all rules."},
                },
            },
        ),
        Tool(
            name="rule_set_phase",
            description="Attach or remove a phase scope on a rule (v8.0 lazy rule loading). "
                        "Tag-based: manages 'phase:<X>' on the rule's tags. "
                        "phase=null clears the phase tag (rule becomes core — applies to every phase). "
                        "Valid phases: van, plan, creative, build, reflect, archive.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "integer"},
                    "phase": {"type": ["string", "null"],
                              "enum": ["van", "plan", "creative", "build",
                                       "reflect", "archive", None],
                              "description": "Phase name or null to clear."},
                },
                "required": ["rule_id"],
            },
        ),
        # ── Observations ──
        Tool(
            name="memory_observe",
            description="Save a lightweight observation (auto-capture). No dedup, no ChromaDB — fast and cheap. "
                        "Use for tracking file changes, tool usage, and session activity. "
                        "Observations auto-cleanup after 30 days.",
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "Which tool triggered this (Write, Edit, Bash, etc.)"},
                    "summary": {"type": "string", "description": "What happened (e.g. 'Modified auth controller')"},
                    "observation_type": {
                        "type": "string",
                        "enum": ["bugfix", "feature", "refactor", "change", "discovery", "decision"],
                        "default": "change",
                        "description": "Type of observation",
                    },
                    "files_affected": {"type": "array", "items": {"type": "string"},
                                       "description": "List of affected file paths"},
                    "project": {"type": "string", "default": "general"},
                },
                "required": ["tool_name", "summary"],
            },
        ),
        # ═══ Super Memory v5.0 Tools ═══
        Tool(
            name="memory_associate",
            description="Associative recall — brain-like spreading activation through knowledge graph. "
                        "Finds memories through concept resonance, not keyword search. "
                        "In 'composition' mode, finds minimum set of memories covering all needed concepts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query"},
                    "mode": {"type": "string", "enum": ["recall", "composition"], "default": "recall",
                             "description": "recall=find related, composition=build solution from parts"},
                    "project": {"type": "string", "description": "Filter by project"},
                    "max_results": {"type": "integer", "default": 10},
                    "min_coverage": {"type": "number", "default": 0.7,
                                     "description": "Min coverage for composition mode (0.0-1.0)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_graph",
            description="Query the unified knowledge graph. Returns neighborhood of a node: "
                        "connected rules, skills, memories, concepts, entities.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Node name or ID to explore"},
                    "depth": {"type": "integer", "default": 2, "description": "Traversal depth (1-3)"},
                    "types": {"type": "array", "items": {"type": "string"},
                              "description": "Filter by node types (rule, skill, concept, etc.)"},
                },
                "required": ["node"],
            },
        ),
        Tool(
            name="memory_concepts",
            description="List or search concepts in the knowledge graph.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search concepts by name"},
                    "type": {"type": "string", "description": "Filter by node type"},
                    "limit": {"type": "integer", "default": 20},
                    "include_memories": {"type": "boolean", "default": False,
                                         "description": "Include linked knowledge records"},
                },
            },
        ),
        Tool(
            name="memory_episode_save",
            description="Save an episode — narrative of WHAT HAPPENED and HOW. "
                        "Not just facts, but the journey: what was tried, what failed, what worked.",
            inputSchema={
                "type": "object",
                "properties": {
                    "narrative": {"type": "string", "description": "2-3 sentence narrative of what happened"},
                    "outcome": {"type": "string", "enum": ["breakthrough", "failure", "routine", "discovery"]},
                    "project": {"type": "string", "default": "general"},
                    "impact_score": {"type": "number", "default": 0.5, "description": "0.0-1.0, how significant"},
                    "concepts": {"type": "array", "items": {"type": "string"}, "description": "Key concepts involved"},
                    "approaches_tried": {"type": "array", "items": {"type": "string"}},
                    "key_insight": {"type": "string", "description": "The aha moment, if any"},
                    "frustration_signals": {"type": "integer", "default": 0},
                },
                "required": ["narrative", "outcome"],
            },
        ),
        Tool(
            name="memory_episode_recall",
            description="Find past episodes (experiences). Search by concepts, outcome, project, or impact.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search narrative text"},
                    "project": {"type": "string"},
                    "outcome": {"type": "string", "enum": ["breakthrough", "failure", "routine", "discovery"]},
                    "min_impact": {"type": "number", "default": 0.0},
                    "concepts": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 10},
                },
            },
        ),
        Tool(
            name="memory_skill_get",
            description="Find skills matching a trigger. Skills are learned procedures — HOW to do things.",
            inputSchema={
                "type": "object",
                "properties": {
                    "trigger": {"type": "string", "description": "Natural language trigger to match"},
                    "name": {"type": "string", "description": "Get skill by exact name"},
                    "list_all": {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="memory_skill_update",
            description="Record skill usage or refine a skill. Updates success rate and metrics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Skill ID"},
                    "success": {"type": "boolean", "description": "Was the skill application successful?"},
                    "notes": {"type": "string"},
                    "new_steps": {"type": "array", "items": {"type": "string"}, "description": "Additional steps to add"},
                    "new_anti_pattern": {"type": "string", "description": "Anti-pattern learned from failure"},
                },
                "required": ["skill_id", "success"],
            },
        ),
        Tool(
            name="memory_self_assess",
            description="Self-assessment: how competent am I in given domains? Shows level, confidence, blind spots.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concepts": {"type": "array", "items": {"type": "string"},
                                 "description": "Domains/concepts to assess competency for"},
                    "full_report": {"type": "boolean", "default": False,
                                    "description": "Return full self-model report"},
                },
            },
        ),
        Tool(
            name="memory_context_build",
            description="Build optimal context for a query. Combines: spreading activation + knowledge graph "
                        "+ episodes + skills + self-model. The 'brain thinking' tool.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What you need context for"},
                    "project": {"type": "string"},
                    "max_tokens": {"type": "integer", "default": 4000},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_reflect_now",
            description="Run reflection (the 'sleep' process). Consolidates knowledge, finds patterns, "
                        "generates skill proposals, updates self-model.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["quick", "full", "weekly"], "default": "full",
                              "description": "quick=dedup only, full=digest+synthesize, weekly=deep analysis"},
                },
            },
        ),
        Tool(
            name="memory_graph_index",
            description="Reindex CLAUDE.md rules and skills into the knowledge graph. "
                        "Run after modifying CLAUDE.md or adding new skills.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["all", "claude_md", "skills", "rules"], "default": "all"},
                },
            },
        ),
        Tool(
            name="memory_graph_stats",
            description="Knowledge graph statistics: nodes, edges, communities, top concepts, health metrics.",
            inputSchema={"type": "object", "properties": {}},
        ),
        # ── v7.0 Temporal KG ──
        Tool(
            name="kg_add_fact",
            description="Record a temporal fact assertion (subject, predicate, object). "
                        "Supersedes any prior assertion with same (s,p) and different object — "
                        "full history is preserved. Use for evolving architectural decisions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "confidence": {"type": "number", "default": 1.0},
                    "context": {"type": "string"},
                    "project": {"type": "string", "default": "general"},
                    "invalidate_previous": {"type": "boolean", "default": True},
                },
                "required": ["subject", "predicate", "object"],
            },
        ),
        Tool(
            name="kg_invalidate_fact",
            description="Close a currently-valid fact assertion. History is retained.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "reason": {"type": "string", "default": "manually_invalidated"},
                    "project": {"type": "string", "default": "general"},
                },
                "required": ["subject", "predicate", "object"],
            },
        ),
        Tool(
            name="kg_at",
            description="Point-in-time query: return fact assertions valid at `timestamp` "
                        "(ISO 8601). Omit timestamp for currently-valid facts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string",
                                    "description": "ISO 8601 or omit for now"},
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        ),
        Tool(
            name="kg_timeline",
            description="Full chronological history of assertions for a subject.",
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "default": 500},
                },
                "required": ["subject"],
            },
        ),
        # ── v7.0 Procedural memory ──
        Tool(
            name="workflow_learn",
            description="Record a learned workflow (named sequence of steps) for future reuse.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "description": {"type": "string"},
                    "trigger_pattern": {"type": "string"},
                    "context": {"type": "object"},
                    "project": {"type": "string", "default": "general"},
                },
                "required": ["name", "steps"],
            },
        ),
        Tool(
            name="workflow_predict",
            description="Predict outcome (success probability, avg duration) for a workflow "
                        "by id OR by trigger keyword. Uses Laplace-smoothed success rate.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "trigger": {"type": "string"},
                    "project": {"type": "string"},
                },
            },
        ),
        Tool(
            name="workflow_track",
            description="Record a workflow execution outcome. Outcome ∈ "
                        "{success|failure|partial|aborted}. Aggregates update automatically.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "outcome": {"type": "string",
                                "enum": ["success", "failure", "partial", "aborted"]},
                    "duration_ms": {"type": "integer"},
                    "error_details": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["workflow_id", "outcome"],
            },
        ),
        # ── v7.0 File-context guard ──
        Tool(
            name="file_context",
            description="BEFORE editing a file, call this to surface past errors, lessons, "
                        "and related rules for that file path. Returns risk_score ∈ [0, 1].",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["path"],
            },
        ),
        # ── v7.0 Structured error capture ──
        Tool(
            name="learn_error",
            description="Structured error capture: file, error, root_cause, fix, pattern. "
                        "After N (default 3) errors share the same pattern, a prevention "
                        "rule is auto-synthesized into the rules table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "error": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "fix": {"type": "string"},
                    "pattern": {"type": "string"},
                    "severity": {"type": "string",
                                 "enum": ["low", "medium", "high", "critical"],
                                 "default": "medium"},
                    "category": {"type": "string", "default": "bug"},
                    "project": {"type": "string", "default": "general"},
                },
                "required": ["file", "error", "root_cause", "fix", "pattern"],
            },
        ),
        # ── v7.0 Session continuity ──
        Tool(
            name="session_init",
            description="At session start: return the most recent unconsumed end-of-session "
                        "summary with highlights / pitfalls / next_steps.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "general"},
                    "mark_consumed": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="session_end",
            description="End-of-session capture: summary + highlights + pitfalls + next_steps "
                        "so the next session can resume cleanly. "
                        "Set auto_compress=true to have the LLM generate the missing "
                        "summary/next_steps/pitfalls from stored session artifacts "
                        "(or from an optional `transcript`).",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "summary": {"type": "string"},
                    "highlights": {"type": "array", "items": {"type": "string"}},
                    "pitfalls": {"type": "array", "items": {"type": "string"}},
                    "next_steps": {"type": "array", "items": {"type": "string"}},
                    "open_questions": {"type": "array", "items": {"type": "string"}},
                    "project": {"type": "string", "default": "general"},
                    "auto_compress": {"type": "boolean", "default": False},
                    "transcript": {"type": "string"},
                },
                "required": ["session_id"],
            },
        ),
        # ── v7.0 AST ingest ──
        Tool(
            name="ingest_codebase",
            description="Parse a file or directory into semantic AST chunks "
                        "(functions, classes, methods) across 8 languages. "
                        "Returns chunk count + sample.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "include": {"type": "array", "items": {"type": "string"},
                                "description": "Extension allowlist e.g. ['.py','.go']"},
                    "sample_limit": {"type": "integer", "default": 5},
                },
                "required": ["path"],
            },
        ),
        # ── v7.0 Analogy + benchmark ──
        Tool(
            name="analogize",
            description="Find past solutions/lessons from OTHER projects whose feature set "
                        "overlaps with the given problem text (Jaccard similarity).",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "exclude_project": {"type": "string"},
                    "only_types": {"type": "array", "items": {"type": "string"},
                                   "default": ["solution", "lesson", "decision"]},
                    "limit": {"type": "integer", "default": 10},
                    "min_score": {"type": "number", "default": 0.1},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="benchmark",
            description="Run the eval harness: recall_at_k, prevention_rate, latency percentiles. "
                        "Loads scenarios from evals/scenarios/*.json by default.",
            inputSchema={
                "type": "object",
                "properties": {
                    "scenarios_path": {"type": "string",
                                       "description": "Custom scenarios dir or file"},
                },
            },
        ),
        # ── v11.0 Phase 6 — fast-path & introspection tools ──────────
        Tool(
            name="memory_save_fast",
            description="v11.0: same as memory_save but routes through the fast hot path "
                        "(skip_quality=True, no LLM, no async-blocking). Use when you want "
                        "to bypass the v10 quality gate without flipping the env flag.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "source_format": {"type": "string", "enum": ["auto", "conversation"], "default": "auto"},
                    "type": {"type": "string", "enum": ["decision", "fact", "solution", "lesson", "convention"]},
                    "project": {"type": "string", "default": "general"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "context": {"type": "string"},
                    "branch": {"type": "string"},
                    "filter": {"type": "string"},
                    "importance": {"type": "string", "enum": ["critical", "high", "medium", "low"], "default": "medium"},
                    "agent_id": {"type": "string", "description": "Optional Claude Code subagent ID (v2.1.139+)"},
                    "parent_agent_id": {"type": "string", "description": "Optional parent agent ID (the dispatching span)"},
                    "supersede": {"type": "boolean", "default": False,
                                  "description": "Same as memory_save.supersede: retire records this one gives a new value for."},
                },
                "required": ["content", "type"],
            },
        ),
        Tool(
            name="memory_search_fast",
            description="v11.0: like memory_recall but with rerank=False, diverse=False forced. "
                        "Deterministic fast path — zero LLM, FastEmbed-only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string"},
                    "type": {"type": "string", "enum": ["decision", "fact", "solution", "lesson", "convention", "all"], "default": "all"},
                    "limit": {"type": "integer", "default": 10},
                    "detail": {"type": "string", "enum": ["compact", "summary", "full"], "default": "full"},
                    "branch": {"type": "string"},
                    "fusion": {"type": "string", "enum": ["rrf", "legacy"], "default": "rrf"},
                    "embedding_space": {
                        "type": ["string", "array"],
                        "description": "Filter to one or more embedding spaces (text|code|log|config).",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_explain_search",
            description="v11.0: same as memory_search_fast but returns a per-tier breakdown "
                        "(fts/semantic/graph/fuzzy/hyde with raw scores, the merged RRF list, "
                        "rerank_applied flag, embedding_space). Use to debug why a record did "
                        "or didn't surface for a query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string"},
                    "type": {"type": "string", "enum": ["decision", "fact", "solution", "lesson", "convention", "all"], "default": "all"},
                    "limit": {"type": "integer", "default": 10},
                    "embedding_space": {"type": ["string", "array"]},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_warmup",
            description="v11.0: pre-load FastEmbed model and open the vector store, so the "
                        "first save/search after process start doesn't pay model-load latency.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="memory_perf_report",
            description="v11.0: dump in-process telemetry counters (search_total_ms, embed_ms, "
                        "fts_ms, vector_ms, llm_calls, network_calls) plus persistent "
                        "embedding_cache stats. Use to verify the fast hot path stays clean.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="memory_rebuild_fts",
            description="v11.0: drop and rebuild the SQLite FTS5 virtual table from `knowledge` "
                        "rows. Useful after migrations or content_type column changes that the "
                        "FTS triggers didn't see. Returns {rebuilt: int}.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="memory_rebuild_embeddings",
            description="v11.0: re-encode every record (or every record in a given embedding "
                        "space) and update the binary + float32 vectors. Idempotent. Pass "
                        "embedding_space='code' to refresh only code rows after switching the "
                        "code embedder. Returns {rebuilt: int, skipped: int}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "embedding_space": {
                        "type": ["string", "array"],
                        "description": "Optional: only re-encode rows in these spaces.",
                    },
                    "project": {"type": "string"},
                    "batch_size": {"type": "integer", "default": 32},
                    "limit": {"type": "integer"},
                },
            },
        ),
        # ── v11.0 Phase 8 — evaluation harness MCP tools ─────────────
        Tool(
            name="memory_eval_locomo",
            description="v11.0 Phase 8: run the LongMemEval-style recall+prevention "
                        "scenario suite (loaded from evals/scenarios/) against the live "
                        "store. Forces MEMORY_MODE=fast by default. Returns "
                        "{scenarios_total, scenarios_passed, recall_at_5, recall_at_10, "
                        "latency_ms, mode, llm_calls_during_eval, network_calls_during_eval}.",
            inputSchema={
                "type": "object",
                "properties": {
                    "top_k": {"type": "integer", "default": 5},
                    "limit": {"type": "integer", "description": "Cap how many scenarios to run."},
                    "mode": {"type": "string", "enum": ["fast", "balanced", "deep"], "default": "fast"},
                    "scenarios_path": {"type": "string", "description": "Optional override path."},
                },
            },
        ),
        Tool(
            name="memory_eval_recall",
            description="v11.0 Phase 8: generic recall benchmark on a dataset path or a "
                        "small built-in fixture. Same payload shape as memory_eval_locomo.",
            inputSchema={
                "type": "object",
                "properties": {
                    "dataset_path": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                    "limit": {"type": "integer"},
                    "mode": {"type": "string", "enum": ["fast", "balanced", "deep"], "default": "fast"},
                },
            },
        ),
        Tool(
            name="memory_eval_temporal",
            description="v11.0 Phase 8: temporal recall using temporal_kg + temporal_filter. "
                        "Returns {status: 'not_implemented', ...} when modules are missing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "mode": {"type": "string", "enum": ["fast", "balanced", "deep"], "default": "fast"},
                },
            },
        ),
        Tool(
            name="memory_eval_entity_consistency",
            description="v11.0 Phase 8: verifies entity_dedup canonicalization is stable "
                        "across repeated saves of variant tag spellings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["fast", "balanced", "deep"], "default": "fast"},
                },
            },
        ),
        Tool(
            name="memory_eval_contradictions",
            description="v11.0 Phase 8: runs contradiction_detector against a labelled "
                        "fixture. Requires balanced/deep mode (LLM). Returns "
                        "{status: 'not_implemented', ...} if module is unavailable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["fast", "balanced", "deep"], "default": "fast"},
                    "fixture_path": {"type": "string"},
                },
            },
        ),
        Tool(
            name="memory_eval_long_context",
            description="v11.0 Phase 8: large-context recall scenario. Saves N records "
                        "and queries them at the tail. Reuses eval_harness scenarios "
                        "tagged 'long_context' if present.",
            inputSchema={
                "type": "object",
                "properties": {
                    "n_records": {"type": "integer", "default": 200},
                    "top_k": {"type": "integer", "default": 5},
                    "mode": {"type": "string", "enum": ["fast", "balanced", "deep"], "default": "fast"},
                },
            },
        ),
        Tool(name="classify_task", description="v8.0: classify task into L1-L4 complexity + suggested phases.", inputSchema={"type": "object", "properties": {"description": {"type": "string"}, "project": {"type": "string"}}, "required": ["description"]}),
        Tool(name="task_create", description="v8.0: start a task in `van` phase (auto-classifies level if missing).", inputSchema={"type": "object", "properties": {"task_id": {"type": "string"}, "description": {"type": "string"}, "level": {"type": "integer"}}, "required": ["task_id", "description"]}),
        Tool(name="phase_transition", description="v8.0: advance a task to the next phase.", inputSchema={"type": "object", "properties": {"task_id": {"type": "string"}, "new_phase": {"type": "string"}, "artifacts": {"type": "object"}, "notes": {"type": "string"}}, "required": ["task_id", "new_phase"]}),
        Tool(name="task_phases_list", description="v8.0: list all phases of a task in chronological order.", inputSchema={"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}),
        Tool(
            name="save_intent",
            description="Persist one user prompt into the `intents` table (same source as the "
                        "UserPromptSubmit hook). Use when programmatically seeding intents — the "
                        "hook covers normal interactive usage. Dedupes same prompt within 5 min per session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "User prompt text as submitted"},
                    "session_id": {"type": "string", "description": "Session id (defaults to current MCP session)"},
                    "project": {"type": "string", "description": "Project slug"},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="list_intents",
            description="List recent user prompts from the intents table, newest first. "
                        "Filter by project and/or session. Max 500 rows.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="search_intents",
            description="Substring search over user prompts (LIKE). Returns newest match first. "
                        "Useful for 'what did I ask about X' without mining transcripts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match in prompt text"},
                    "project": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="save_decision",
            description="v8.0: save a structured architectural decision (options + criteria matrix + "
                        "rationale + discarded). Adds `structured` tag and a JSON blob in context. "
                        "Use for Creative-phase outputs; plain type=decision memory_save still works.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short decision title"},
                    "options": {
                        "type": "array",
                        "description": "Options considered: [{name, pros[], cons[], unknowns[]}, ...]",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "pros": {"type": "array", "items": {"type": "string"}},
                                "cons": {"type": "array", "items": {"type": "string"}},
                                "unknowns": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["name"],
                        },
                    },
                    "criteria_matrix": {
                        "type": "object",
                        "description": "criterion -> {option_name: rating 0-5}",
                    },
                    "selected": {"type": "string", "description": "Chosen option name (must be in options)"},
                    "rationale": {"type": "string", "description": "Why this option was chosen"},
                    "discarded": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Option names rejected (subset of options - {selected})",
                    },
                    "project": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "options", "criteria_matrix", "selected", "rationale"],
            },
        ),
        # ── v11.0 W3 — new MCP tools: iterative recall, temporal, entity, consolidate ──
        Tool(
            name="memory_recall_iterative",
            description="v11.0 W1-B: IRCoT-style iterative retrieval. Decomposes the query into "
                        "sub-questions, retrieves per sub-question, and asks a planner LLM whether "
                        "more retrieval is needed. Best for multi-hop questions. Returns unified "
                        "evidence + provenance per iteration.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "project": {"type": "string"},
                    "max_iters": {"type": "integer", "default": 4, "minimum": 1, "maximum": 12},
                    "k_per_iter": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    "llm_model": {"type": "string", "default": "configured"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memory_temporal_query",
            description="v11.0 W1-C: deterministic temporal reasoning — Allen interval relations, "
                        "duration arithmetic (days/weeks/months/years), and natural-language date "
                        "normalization (en + ru). Pass op=relation|duration_between|normalize.",
            inputSchema={
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["relation", "duration_between", "normalize"]},
                    "a_start": {"type": "string", "description": "ISO datetime — relation only"},
                    "a_end": {"type": "string"},
                    "b_start": {"type": "string"},
                    "b_end": {"type": "string"},
                    "a": {"type": "string", "description": "ISO datetime — duration_between"},
                    "b": {"type": "string"},
                    "phrase": {"type": "string", "description": "Natural-language date — normalize"},
                    "anchor": {"type": "string", "description": "ISO datetime anchor for relative phrases"},
                    "lang": {"type": "string", "enum": ["auto", "en", "ru"], "default": "auto"},
                },
                "required": ["op"],
            },
        ),
        Tool(
            name="memory_entity_resolve",
            description="v11.0 W1-F: resolve a mention to its canonical entity within a project+type. "
                        "Cross-session coreference via name/alias index + embedding cosine. Returns "
                        "canonical_id, matched_via, and is_new flag. Pronouns return -1.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mention": {"type": "string"},
                    "project": {"type": "string", "default": "general"},
                    "type": {"type": "string", "default": "person",
                             "description": "Entity type: person, technology, project, company, ..."},
                    "threshold": {"type": "number", "default": 0.85,
                                  "description": "Cosine similarity threshold for embedding match."},
                    "create_if_missing": {"type": "boolean", "default": True},
                },
                "required": ["mention"],
            },
        ),
        Tool(
            name="memory_consolidate_status",
            description="v11.0 W2-G: report the consolidation daemon state — per-project last-run, "
                        "active locks, recent activity. Use to verify the idle-project worker is "
                        "making progress without interfering with active work.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


async def _call_tool_impl(name, args) -> tuple[list[TextContent], bool]:
    """Run a tool and report whether it failed.

    The bool is the MCP ``isError`` flag: a tool that raised is reported
    inside the result so the model can see and self-correct, rather than as a
    protocol-level error.
    """
    try:
        # Guarded: a client can call a tool before _bootstrap_session() has
        # run (or after a failed bootstrap). Losing the audit line is fine;
        # crashing the transport is not.
        if store is not None:
            store.raw_append(SID, {"type": "tool_call", "tool": name, "args": args})
        r = await _do(name, args)
        return [TextContent(type="text", text=r)], False
    except Exception as e:
        LOG(f"Error in {name}: {e}")
        return [TextContent(type="text", text=f"Error: {e}")], True


async def call_tool(name, args):
    content, _is_error = await _call_tool_impl(name, args)
    return content


def _structured_content(content: list[TextContent], is_error: bool):
    """Parsed payload for MCP `structuredContent`, or None when there isn't one.

    Nearly every tool here answers with a JSON document rendered as text. On
    protocol revision 2026-07-28 `structuredContent` accepts any JSON value, so
    we hand clients the parsed object as well and they stop re-parsing strings.
    Tools that answer in prose, and error results, carry text only.

    No `outputSchema` is declared, so this stays additive: the spec only
    constrains structuredContent against a schema when one is advertised.
    """
    if is_error or len(content) != 1:
        return None
    text = (content[0].text or "").lstrip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _register_mcp_handlers(server) -> str:
    """Wire tools/list + tools/call onto `server` for either SDK era."""
    if hasattr(server, "list_tools"):
        server.list_tools()(list_tools)
        server.call_tool()(call_tool)
        return "1.x"

    from mcp.types import (  # noqa: PLC0415 — 2.x-only symbols
        CallToolRequestParams,
        CallToolResult,
        ListToolsResult,
        PaginatedRequestParams,
    )

    async def _handle_list_tools(_ctx, _params):
        return ListToolsResult(tools=await list_tools())

    async def _handle_call_tool(_ctx, params):
        content, is_error = await _call_tool_impl(params.name, params.arguments or {})
        return CallToolResult(
            content=list(content),
            structured_content=_structured_content(content, is_error),
            is_error=is_error,
        )

    server.add_request_handler("tools/list", PaginatedRequestParams, _handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, _handle_call_tool)
    return "2.x"


_register_mcp_handlers(app)


# ──────────────────────────────────────────────
# v11.0 Phase 8 — evaluation harness helpers
# ──────────────────────────────────────────────


class _EvalModeContext:
    """Force MEMORY_MODE for the duration of an eval call.

    Saves and restores the relevant env vars so a tool invocation cannot leak
    its mode into the long-running server process. Idempotent: nested usage
    is safe because each instance snapshots its own slice of env.
    """

    _KEYS = (
        "MEMORY_MODE",
        "MEMORY_USE_LLM_IN_HOT_PATH",
        "MEMORY_ALLOW_OLLAMA_IN_HOT_PATH",
        "MEMORY_RERANK_ENABLED",
        "MEMORY_ENRICHMENT_ENABLED",
        "MEMORY_LLM_ENABLED",
    )

    def __init__(self, mode: str) -> None:
        self.mode = (mode or "fast").strip().lower()
        if self.mode not in ("fast", "balanced", "deep"):
            self.mode = "fast"
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> "_EvalModeContext":
        for k in self._KEYS:
            self._saved[k] = os.environ.get(k)
        os.environ["MEMORY_MODE"] = self.mode
        if self.mode == "fast":
            # Lock the hot path: zero LLM, zero network, zero rerank.
            os.environ["MEMORY_USE_LLM_IN_HOT_PATH"] = "false"
            os.environ["MEMORY_ALLOW_OLLAMA_IN_HOT_PATH"] = "false"
            os.environ["MEMORY_RERANK_ENABLED"] = "false"
            os.environ["MEMORY_ENRICHMENT_ENABLED"] = "false"
            os.environ["MEMORY_LLM_ENABLED"] = "false"
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _eval_perf_snapshot_delta(before: dict[str, float]) -> tuple[int, int]:
    """Diff llm_calls / network_calls against `before`. Counters can only
    rise, so a negative diff means the caller reset between snapshots —
    in that case we trust the post-reset value."""
    try:
        from memory_core.telemetry import counters as _c
        snap = _c.snapshot()
    except Exception:
        return 0, 0
    llm_now = int(snap.get("llm_calls", 0))
    net_now = int(snap.get("network_calls", 0))
    llm_before = int(before.get("llm_calls", 0))
    net_before = int(before.get("network_calls", 0))
    return max(0, llm_now - llm_before), max(0, net_now - net_before)


def _eval_run_locomo(
    *,
    store_obj,
    recall_obj,
    top_k: int,
    limit: int | None,
    scenarios_path: str | None,
) -> dict:
    """Run eval_harness.EvalHarness against `evals/scenarios/` (or override).

    Wires `recall_fn` and `file_warnings_fn` to the live Store/Recall.
    """
    import time as _t

    from eval_harness import EvalHarness

    def _recall_fn(query: str, params: dict):
        result = recall_obj.search(
            query, params.get("project"), "all",
            params.get("limit", 10), "full",
            None, "rrf", False, False,
        )
        out: list[dict] = []
        results = result.get("results", {}) if isinstance(result, dict) else {}
        for grp in results.values():
            if isinstance(grp, list):
                out.extend(grp)
        return out

    def _file_warnings_fn(file: str, params: dict):
        try:
            from file_context import FileContextGuard
            guard = FileContextGuard(store_obj.db)
            return guard.get_file_warnings(file, project=params.get("project"))
        except Exception as exc:
            return {"warnings": [], "related_rules": [], "error": str(exc)}

    harness = EvalHarness(recall_fn=_recall_fn, file_warnings_fn=_file_warnings_fn)
    scenarios = harness.load_scenarios(scenarios_path)
    if limit is not None and limit > 0:
        scenarios = scenarios[: int(limit)]
    # Normalise k for recall scenarios so the tool's top_k applies.
    for s in scenarios:
        if s.get("type", "recall") == "recall":
            s.setdefault("k", top_k)

    t0 = _t.perf_counter()
    report = harness.run_suite(scenarios)
    elapsed_ms = (_t.perf_counter() - t0) * 1000.0
    return {"report": report, "elapsed_ms": elapsed_ms, "scenarios": scenarios}


async def _do(name, a):
    J = lambda x: json.dumps(x, ensure_ascii=False, indent=2, default=str)

    if name == "memory_recall":
        mode_param = a.get("mode", "search")
        if mode_param not in ("search", "index", "timeline", "context", "evidence"):
            mode_param = "search"

        detail_param = a.get("detail", "full")
        if detail_param == "auto":
            try:
                from verbosity import analyze_query_complexity
                detail_param = analyze_query_complexity(a["query"])
            except Exception as e:
                LOG(f"auto-verbosity failed: {e}")
                detail_param = "full"

        # For index/timeline we need full records to build compact views / fetch
        # neighbours. Underlying search runs at "full" detail regardless of the
        # caller's detail= for those modes.
        search_detail = "full" if mode_param != "search" else detail_param
        search_limit = min(50, a.get("limit", 10) * 2) if mode_param == "evidence" else a.get("limit", 10)
        result = recall.search(a["query"], a.get("project"), a.get("type", "all"),
                               search_limit, search_detail,
                               a.get("branch"), a.get("fusion", "rrf"),
                               a.get("rerank", False), a.get("diverse", False))
        if a.get("detail") == "auto":
            result["auto_detail"] = detail_param

        # Optional enrichment filter (topics / entities / intent from knowledge_enrichment)
        if a.get("topics") or a.get("entities") or a.get("intent"):
            try:
                from enrichment_filter import filter_by_enrichment

                topics_f = a.get("topics") or None
                entities_f = a.get("entities") or None
                intent_f = a.get("intent") or None

                for group_name in list(result.get("results", {}).keys()):
                    group = result["results"][group_name]
                    candidate_ids = [
                        item["id"] for item in group
                        if isinstance(item.get("id"), int)
                    ]
                    kept_ids = set(
                        filter_by_enrichment(
                            store.db, candidate_ids,
                            topics=topics_f, entities=entities_f, intent=intent_f,
                        )
                    )
                    filtered = [
                        item for item in group
                        if isinstance(item.get("id"), int) and item["id"] in kept_ids
                    ]
                    result["results"][group_name] = filtered
                # Recompute total
                result["total"] = sum(len(g) for g in result["results"].values())
                result["filtered_by"] = {
                    "topics": topics_f, "entities": entities_f, "intent": intent_f,
                }
            except Exception as e:
                LOG(f"Enrichment filter error: {e}")

        # Enrich with CognitiveEngine associative activation.
        # Skip for index/timeline modes — those are intentionally minimal.
        if mode_param == "search":
            try:
                ce = _get_v5("cognitive", store.db)
                cognitive_ctx = ce.on_query(a["query"], a.get("project"))
                # Append cognitive enrichment to result
                enrichment = {}
                if cognitive_ctx.get("activated_concepts"):
                    enrichment["activated_concepts"] = cognitive_ctx["activated_concepts"][:10]
                if cognitive_ctx.get("relevant_rules"):
                    enrichment["relevant_rules"] = cognitive_ctx["relevant_rules"]
                if cognitive_ctx.get("past_failures"):
                    enrichment["past_failures"] = cognitive_ctx["past_failures"]
                if cognitive_ctx.get("available_solutions"):
                    # Deduplicate against already-returned results
                    existing_ids = set()
                    for group in result.get("results", {}).values():
                        for item in group:
                            existing_ids.add(item.get("id"))
                    enrichment["additional_solutions"] = [
                        s for s in cognitive_ctx["available_solutions"]
                        if s.get("id") not in existing_ids
                    ][:5]
                if cognitive_ctx.get("applicable_skills"):
                    enrichment["applicable_skills"] = cognitive_ctx["applicable_skills"]
                if cognitive_ctx.get("competency"):
                    enrichment["competency"] = cognitive_ctx["competency"]
                if enrichment:
                    result["cognitive"] = enrichment
            except Exception as e:
                LOG(f"CognitiveEngine enrichment error: {e}")

        # Optional graph-based context expansion (1-hop neighbors).
        # Also skipped for index/timeline modes (callers opt-in to lightweight output).
        if mode_param == "search" and a.get("expand_context"):
            try:
                from context_expander import ContextExpander

                seed_ids: list[int] = []
                for group in result.get("results", {}).values():
                    for item in group:
                        kid = item.get("id")
                        if isinstance(kid, int):
                            seed_ids.append(kid)
                if seed_ids:
                    expander = ContextExpander(store.db)
                    extra_ids = expander.expand(
                        seed_ids=seed_ids,
                        budget=int(a.get("expand_budget", 5)),
                        depth=1,
                    )
                    if extra_ids:
                        placeholders = ",".join("?" * len(extra_ids))
                        rows = store.db.execute(
                            f"SELECT id, type, content, project, tags, created_at "
                            f"FROM knowledge WHERE id IN ({placeholders})",
                            extra_ids,
                        ).fetchall()
                        expansion: list[dict] = []
                        for r in rows:
                            expansion.append(
                                {
                                    "id": r["id"],
                                    "type": r["type"],
                                    "content": r["content"],
                                    "project": r["project"],
                                    "tags": r["tags"],
                                    "created_at": r["created_at"],
                                    "via": ["graph_expansion"],
                                }
                            )
                        if expansion:
                            result["expansion"] = expansion
            except Exception as e:
                LOG(f"Context expansion error: {e}")

        # Structured-decisions-only filter (v8.0): narrow to type=decision
        # with the 'structured' tag and attach parsed schema payload.
        if a.get("decisions_only"):
            try:
                from decisions import STRUCTURED_TAG, parse_stored_decision

                # Rebuild result on a shallow copy so we never mutate the
                # cached value (cache key does not include decisions_only).
                result = {**result}
                filtered_groups: dict[str, list[dict]] = {}
                for group_name, group in result.get("results", {}).items():
                    # `results` is grouped by type — drop non-decision buckets.
                    if group_name != "decision":
                        continue
                    kept: list[dict] = []
                    for item in group:
                        raw_tags = item.get("tags", [])
                        if isinstance(raw_tags, str):
                            try:
                                raw_tags = json.loads(raw_tags)
                            except Exception:
                                raw_tags = []
                        if STRUCTURED_TAG not in (raw_tags or []):
                            continue
                        kid = item.get("id")
                        ctx = item.get("context") or ""
                        if (not ctx) and isinstance(kid, int):
                            ctx_row = store.db.execute(
                                "SELECT context FROM knowledge WHERE id=?", (kid,)
                            ).fetchone()
                            if ctx_row is not None:
                                ctx = ctx_row["context"] or ""
                        parsed = parse_stored_decision(ctx)
                        if parsed is None:
                            continue
                        enriched = dict(item)
                        enriched["decision"] = parsed
                        kept.append(enriched)
                    filtered_groups[group_name] = kept
                result["results"] = filtered_groups
                result["total"] = sum(len(g) for g in filtered_groups.values())
                result["decisions_only"] = True
            except Exception as e:
                LOG(f"decisions_only filter error: {e}")

        # Progressive-disclosure mode transforms — applied last so they observe
        # all filters (enrichment, decisions_only) but strip heavy fields.
        if mode_param == "index":
            try:
                from recall_modes import index_response
                result = index_response(result)
            except Exception as e:
                LOG(f"index_response error: {e}")
        elif mode_param == "context":
            from config import get_recall_excluded_tags
            from memory_core.evidence_context import EvidenceContext
            from memory_core.evidence_pack import DEFAULT_EVIDENCE_CHARS
            from memory_core.evidence_window import ANSWER_GUIDANCE
            from memory_core.retrieval import SearchScope, flatten_results
            evidence = EvidenceContext(store.db, get_recall_excluded_tags()).build(
                flatten_results(result), query=a["query"],
                scope=SearchScope(project=a.get("project"), kind=a.get("type", "all"), branch=a.get("branch")),
                radius=int(a.get("neighbors", 1)),
                max_chars=a.get("context_max_chars", DEFAULT_EVIDENCE_CHARS),
            )
            result = {**result, "mode": "context", "results": evidence, "total": len(evidence),
                      "answer_guidance": ANSWER_GUIDANCE,
                      "total_tokens": Store._estimate_tokens(json.dumps(evidence, ensure_ascii=False) + ANSWER_GUIDANCE)}
        elif mode_param == "timeline":
            try:
                from memory_core.retrieval import SearchScope
                from recall_modes import timeline_response
                result = timeline_response(
                    result, store,
                    neighbors=int(a.get("neighbors", 2)),
                    limit=int(a.get("limit", 5)),
                    scope=SearchScope(project=a.get("project"), kind=a.get("type", "all"), branch=a.get("branch")),
                )
            except Exception as e:
                LOG(f"timeline_response error: {e}")

        if mode_param == "evidence":
            from evidence_endpoint import evidence_response
            result = evidence_response(store, recall, a, result)
        return J(result)

    elif name == "memory_timeline":
        kwargs = {k: a.get(k) for k in
                  ["query", "session_number", "sessions_ago", "date_from", "date_to", "project", "limit"]}
        return J(recall.timeline(**kwargs))

    elif name == "memory_index_passages":
        from evidence_endpoint import index_passages
        return J(index_passages(store, a))

    elif name == "memory_answer":
        from dataclasses import asdict

        from answer_endpoint import answer_response
        return J(asdict(answer_response(store, recall, a)))

    elif name == "memory_save":
        rid, was_dedup, was_redacted, private_sections, quality_meta = store.save_knowledge(
            SID, a["content"], a["type"],
            a.get("project", "general"), a.get("tags", []), a.get("context", ""),
            branch=a.get("branch", BRANCH), filter_name=a.get("filter"),
            source_format=a.get("source_format", "auto"),
            importance=a.get("importance", "medium"),
            coref=a.get("coref"),
            agent_id=a.get("agent_id"),
            parent_agent_id=a.get("parent_agent_id"))
        # v10 — quality gate dropped the record; surface the verdict so the
        # caller can iterate on the content rather than silently losing it.
        if rid is None:
            verdict = quality_meta or {}
            return J({
                "saved": False,
                "rejected_by_quality_gate": True,
                "score": verdict.get("total"),
                "threshold": verdict.get("threshold"),
                "reason": verdict.get("reason"),
                "axes": {
                    "specificity": verdict.get("specificity"),
                    "actionability": verdict.get("actionability"),
                    "verifiability": verdict.get("verifiability"),
                },
            })
        superseded = (store.supersede_values(rid, a["content"], a["type"], a.get("project", "general"))
                      if a.get("supersede") and not was_dedup else [])
        # Invalidate cache on write
        if store.cache is not None:
            store.cache.invalidate(project=a.get("project"))
        # v9 A2: drop L1 query cache wholesale on write — cheap + correct.
        if getattr(store, "v9_cache", None) is not None:
            store.v9_cache.invalidate_all()
        result = {"saved": True, "id": rid, "deduplicated": was_dedup}
        if superseded:
            result["superseded"] = superseded
        if was_redacted:
            result["privacy_redacted"] = True
        if private_sections:
            result["privacy_redacted_sections"] = private_sections
        if quality_meta and quality_meta.get("decision") == "pass":
            result["quality_score"] = quality_meta.get("total")

        # Auto-update SelfModel competencies for solution/lesson saves
        if a["type"] in ("solution", "lesson") and not was_dedup:
            try:
                sm = _get_v5("self_model", store.db)
                tags = a.get("tags", [])
                # Extract domain concepts from tags (skip meta-tags)
                meta_tags = frozenset({
                    "reusable", "session-autosave", "context-recovery",
                    "self-reflection", "auto", "manual",
                })
                domains = [t for t in tags if t not in meta_tags and len(t) >= 3]
                outcome = "discovery" if a["type"] == "solution" else "routine"
                competency_updates = []
                for domain in domains[:5]:
                    sm.update_competency(domain, outcome)
                    competency_updates.append(domain)
                if competency_updates:
                    result["competency_updated"] = competency_updates
            except Exception as e:
                LOG(f"SelfModel competency update error: {e}")

        return J(result)

    elif name == "memory_update":
        res = recall.search(a["find"], limit=3)
        items = [i for g in res.get("results", {}).values() for i in g]
        if not items:
            return J({"error": "Not found", "query": a["find"]})
        old = items[0]
        old_rec = store.q1("SELECT * FROM knowledge WHERE id=?", (old["id"],))
        if not old_rec:
            return J({"error": "Record not found in DB"})
        new_id, _, _, _, _ = store.save_knowledge(
            SID, a["new_content"], old_rec["type"], old_rec["project"],
            json.loads(old_rec.get("tags", "[]")),
            f"Updated: {a.get('reason', '')}. Was: {old_rec['content'][:200]}",
            branch=old_rec.get("branch", ""), skip_dedup=True, skip_quality=True,
            source_format=old_rec.get("source_format", "auto"))
        store.db.execute(
            "UPDATE knowledge SET status='superseded',superseded_by=? WHERE id=?",
            (new_id, old["id"]))
        store._delete_embedding(old["id"])
        store.db.commit()
        # B1 — content changed, so any cached representations on the OLD
        # record are now drift-stale; the NEW record gets its own views
        # generated by the async repr queue. Without this enqueue the new
        # record relies on the raw-only path and recall loses the summary/
        # keywords/questions boost until the next batch sweep picks it up.
        try:
            from representations_queue import RepresentationsQueue
            RepresentationsQueue(store.db).enqueue(new_id)
        except Exception as _e:
            LOG(f"memory_update: repr-queue enqueue failed for new_id={new_id}: {_e}")
        if store.chroma and not store._check_binary_search():
            try:
                store.chroma.delete(ids=[str(old["id"])])
            except Exception:
                pass
        # Invalidate cache on update
        if store.cache is not None:
            store.cache.invalidate(project=old_rec.get("project"))
        if getattr(store, "v9_cache", None) is not None:
            store.v9_cache.invalidate_by_id(old["id"])
        return J({"updated": True, "old_id": old["id"], "new_id": new_id})

    elif name == "memory_stats":
        return J(recall.stats())

    elif name == "memory_consolidate":
        threshold = a.get("threshold", 0.75)
        dry_run = a.get("dry_run", True)
        groups = store.find_similar_groups(a.get("project"), threshold)
        if dry_run:
            preview = []
            for g in groups:
                preview.append({
                    "group_size": len(g),
                    "type": g[0]["type"],
                    "project": g[0]["project"],
                    "records": [{"id": r["id"], "content": r["content"][:100]} for r in g],
                })
            return J({"dry_run": True, "groups_found": len(groups),
                       "total_mergeable": sum(len(g) - 1 for g in groups), "groups": preview})
        else:
            results = []
            for g in groups:
                r = store.consolidate_group(SID, g)
                results.append(r)
            return J({"consolidated": True, "groups_merged": len(results),
                       "total_removed": sum(len(r["merged"]) for r in results), "details": results})

    elif name == "memory_export":
        data = store.export_all(a.get("project"))
        save = a.get("save_to_file", True)
        if save:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            proj = a.get("project", "all")
            path = MEMORY_DIR / "backups" / f"export_{proj}_{ts}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            return J({"exported": True, "file": str(path),
                       "knowledge_count": len(data["knowledge"]), "sessions_count": len(data["sessions"])})
        else:
            return J(data)

    elif name == "memory_wiki_generate":
        try:
            import project_wiki as _pw
        except Exception as e:
            return J({"error": f"project_wiki module unavailable: {e}"})
        out_dir = str(MEMORY_DIR / "wikis")
        target = a.get("project")
        if target:
            res = _pw.generate_wiki(store.db, target, output_dir=out_dir)
            if res is None:
                return J({"saved": False, "project": target,
                          "reason": "wiki disabled or no active records"})
            return J({"saved": True, "project": res.project,
                      "path": res.path, "chars": res.chars})
        results = _pw.generate_all(store.db, output_dir=out_dir)
        return J({
            "saved": True,
            "wiki_count": len(results),
            "wikis": [
                {"project": r.project, "path": r.path, "chars": r.chars}
                for r in results
            ],
        })

    elif name == "memory_forget":
        dry_run = a.get("dry_run", True)
        if dry_run:
            archive_cutoff = format_utc(datetime.now(timezone.utc) - timedelta(days=ARCHIVE_AFTER_DAYS))
            purge_cutoff = format_utc(datetime.now(timezone.utc) - timedelta(days=PURGE_AFTER_DAYS))
            would_archive = store.db.execute("""
                SELECT COUNT(*) FROM knowledge
                WHERE status='active' AND last_confirmed < ? AND recall_count = 0 AND confidence < 0.8
            """, (archive_cutoff,)).fetchone()[0]
            would_purge = store.db.execute("""
                SELECT COUNT(*) FROM knowledge WHERE status='archived' AND last_confirmed < ?
            """, (purge_cutoff,)).fetchone()[0]
            return J({"dry_run": True, "would_archive": would_archive, "would_purge": would_purge,
                       "archive_after_days": ARCHIVE_AFTER_DAYS, "purge_after_days": PURGE_AFTER_DAYS})
        else:
            result = store.apply_retention()
            return J({"applied": True, **result})

    elif name == "memory_get":
        raw_ids = a.get("ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = []
        # Normalize + cap at 50 to bound response size / SQL IN() expansion.
        ids: list[int] = []
        seen_ids: set[int] = set()
        for v in raw_ids:
            try:
                iv = int(v)
            except Exception:
                continue
            if iv in seen_ids:
                continue
            seen_ids.add(iv)
            ids.append(iv)
            if len(ids) >= 50:
                break
        detail = a.get("detail", "full")
        if detail not in ("summary", "full"):
            detail = "full"
        if not ids:
            return J({"total": 0, "detail": detail, "results": []})
        placeholders = ",".join("?" * len(ids))
        rows = store.db.execute(
            f"SELECT id, session_id, type, content, context, project, tags, "
            f"status, confidence, created_at, last_confirmed, recall_count, branch "
            f"FROM knowledge WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        # Preserve caller order; silently skip missing IDs.
        by_id = {r["id"]: r for r in rows}
        out: list[dict] = []
        for kid in ids:
            r = by_id.get(kid)
            if r is None:
                continue
            tags = r["tags"] if "tags" in r.keys() else "[]"
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            content = r["content"] or ""
            if detail == "summary":
                content = content[:150] + ("..." if len(r["content"] or "") > 150 else "")
            entry = {
                "id": r["id"], "type": r["type"], "content": content,
                "project": r["project"] or "", "tags": tags,
                "created_at": r["created_at"] or "",
            }
            if detail == "full":
                entry.update({
                    "context": r["context"] or "",
                    "session_id": r["session_id"] or "",
                    "status": r["status"] or "",
                    "confidence": r["confidence"] if "confidence" in r.keys() else 1.0,
                    "last_confirmed": r["last_confirmed"] or "",
                    "recall_count": r["recall_count"] or 0,
                    "branch": r["branch"] or "",
                })
            out.append(entry)
        return J({"total": len(out), "detail": detail, "results": out})

    elif name == "memory_history":
        chain = store.get_version_history(a["id"])
        if not chain:
            return J({"error": "Record not found", "id": a["id"]})
        versions = []
        for i, rec in enumerate(chain):
            tags = rec.get("tags", "[]")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            versions.append({
                "id": rec["id"], "content": rec["content"][:300],
                "status": rec["status"], "created_at": rec.get("created_at", ""),
                "superseded_by": rec.get("superseded_by"),
                "tags": tags, "version": i + 1,
            })
        return J({"record_id": a["id"], "total_versions": len(versions), "versions": versions})

    elif name == "memory_delete":
        rec = store.delete_knowledge(a["id"])
        if not rec:
            return J({"error": "Record not found", "id": a["id"]})
        # Invalidate cache on delete
        if store.cache is not None:
            store.cache.invalidate(project=rec.get("project"))
        if getattr(store, "v9_cache", None) is not None:
            store.v9_cache.invalidate_by_id(a["id"])
        return J({"deleted": True, "id": a["id"], "content_preview": rec["content"][:100]})

    elif name == "memory_relate":
        result = store.add_relation(a["from_id"], a["to_id"], a["type"])
        return J(result)

    elif name == "memory_search_by_tag":
        records = store.search_by_tag(a["tag"], a.get("project"))
        items = []
        for r in records:
            items.append({
                "id": r["id"], "content": r["content"][:200],
                "type": r["type"], "project": r.get("project", ""),
                "tags": r.get("tags", []), "created_at": r.get("created_at", ""),
            })
        return J({"tag": a["tag"], "total": len(items), "records": items})

    elif name == "memory_extract_session":
        action = a.get("action", "list")
        eq_dir = MEMORY_DIR / "extract-queue"

        if action == "list":
            pending = []
            for f in sorted(eq_dir.glob("pending-*.json"), reverse=True):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    pending.append({
                        "session_id": data.get("session_id", f.stem.replace("pending-", "")),
                        "project_name": data.get("project_name", "unknown"),
                        "started_at": data.get("started_at", ""),
                        "ended_at": data.get("ended_at", ""),
                        "stats": data.get("stats", {}),
                        "file_size_kb": round(f.stat().st_size / 1024, 1),
                    })
                except Exception as e:
                    LOG(f"Extract list error for {f.name}: {e}")
            return J({"pending": len(pending), "sessions": pending})

        elif action == "get":
            sid = a.get("session_id", "")
            if not sid:
                return J({"error": "session_id required"})
            fpath = eq_dir / f"pending-{sid}.json"
            if not fpath.exists():
                return J({"error": f"No pending extraction for {sid}"})

            content = fpath.read_text(encoding="utf-8")
            chunk = a.get("chunk", 0)
            chunk_size = 100_000  # ~100 KB per chunk

            if len(content) <= chunk_size:
                data = json.loads(content)
                data["_hint"] = (
                    "Analyze this conversation and save important knowledge via memory_save. "
                    "Focus on: decisions (with WHY), solutions (problem→fix), lessons (gotchas), "
                    "facts (configs, architecture). Skip items already in memory_saves_in_session."
                )
                data["_total_chunks"] = 1
                data["_chunk"] = 0
                return J(data)
            else:
                total_chunks = (len(content) + chunk_size - 1) // chunk_size
                start = chunk * chunk_size
                end = min(start + chunk_size, len(content))
                return J({
                    "_total_chunks": total_chunks,
                    "_chunk": chunk,
                    "_hint": "Chunked response. Request next chunk with chunk=N if needed.",
                    "partial_content": content[start:end],
                })

        elif action == "complete":
            sid = a.get("session_id", "")
            if not sid:
                return J({"error": "session_id required"})
            src = eq_dir / f"pending-{sid}.json"
            dst = eq_dir / f"done-{sid}.json"
            if not src.exists():
                return J({"error": f"No pending extraction for {sid}"})
            src.rename(dst)

            # Cleanup old done files (>7 days)
            import time
            cutoff = time.time() - 7 * 86400
            for f in eq_dir.glob("done-*.json"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except Exception:
                    pass

            return J({"completed": True, "session_id": sid})

        return J({"error": f"Unknown action: {action}"})

    # ── Self-Improvement Handlers ──

    elif name == "self_error_log":
        error_id, pattern = store.log_error(
            SID, a["description"], a["category"],
            a.get("severity", "medium"), a.get("fix", ""),
            a.get("context", ""), a.get("project", "general"),
            a.get("tags", []))
        result = {"logged": True, "error_id": error_id}
        if pattern:
            result["pattern"] = pattern
        return J(result)

    elif name == "self_insight":
        return J(store.manage_insight(SID, a["action"], **{
            k: v for k, v in a.items() if k != "action"}))

    elif name == "self_rules":
        return J(store.manage_rule(SID, a["action"], **{
            k: v for k, v in a.items() if k != "action"}))

    elif name == "self_patterns":
        return J(store.analyze_patterns(
            a.get("view", "full_report"), a.get("project"), a.get("days", 30)))

    elif name == "self_reflect":
        rid, _, _, _, _ = store.save_knowledge(
            SID, a["reflection"], "reflection",
            a.get("project", "general"),
            (a.get("tags") or []) + ["self-reflection", a.get("outcome", "success")],
            f"Task: {a['task_summary']}. Outcome: {a.get('outcome', 'success')}",
            branch=BRANCH, skip_quality=True)
        return J({"saved": True, "id": rid, "type": "reflection"})

    elif name == "self_rules_context":
        return J(store.get_rules_for_context(
            a.get("project", "general"), a.get("categories"), a.get("phase")))

    elif name == "rule_set_phase":
        return J(store.set_rule_phase(a["rule_id"], a.get("phase")))

    elif name == "memory_observe":
        obs_id = store.save_observation(
            SID, a["tool_name"], a["summary"],
            a.get("observation_type", "change"),
            a.get("files_affected", []),
            a.get("project", "general"),
            branch=BRANCH)
        return J({"observed": True, "id": obs_id})

    # ═══ Super Memory v5.0 Handlers ═══

    elif name == "memory_associate":
        ar = _get_v5("assoc_recall", store.db)
        return J(ar.recall(
            query=a["query"],
            project=a.get("project"),
            mode=a.get("mode", "recall"),
            max_results=a.get("max_results", 10),
            min_coverage=a.get("min_coverage", 0.7),
        ))

    elif name == "memory_graph":
        gs = _get_v5("graph_store", store.db)
        gq = _get_v5("graph_query", store.db)
        node_input = a["node"]
        # Try find by name first, then by ID
        node = gs.get_node_by_name(node_input)
        if not node:
            node = gs.get_node(node_input)
        if not node:
            return J({"error": f"Node not found: {node_input}"})
        result = gq.neighborhood(node["id"], depth=a.get("depth", 2),
                                  types=a.get("types"))
        result["center"] = node
        return J(result)

    elif name == "memory_concepts":
        gs = _get_v5("graph_store", store.db)
        query = a.get("query")
        node_type = a.get("type")
        limit = a.get("limit", 20)
        if query:
            nodes = gs.search_nodes(query, type=node_type, limit=limit)
        else:
            nodes = gs.get_nodes(type=node_type or "concept", limit=limit)
        if a.get("include_memories"):
            for n in nodes:
                n["memories"] = gs.get_node_knowledge(n["id"])
        return J({"concepts": nodes, "total": len(nodes)})

    elif name == "memory_episode_save":
        es = _get_v5("episodes", store.db)
        eid = es.save(
            session_id=SID,
            narrative=a["narrative"],
            outcome=a["outcome"],
            project=a.get("project", "general"),
            impact_score=a.get("impact_score", 0.5),
            concepts=a.get("concepts"),
            approaches_tried=a.get("approaches_tried"),
            key_insight=a.get("key_insight"),
            frustration_signals=a.get("frustration_signals", 0),
        )
        # Auto-extract and link concepts to graph
        if a.get("concepts"):
            ex = _get_v5("extractor", store.db)
            ex.extract_and_link(a["narrative"], deep=False)
        return J({"saved": True, "episode_id": eid})

    elif name == "memory_episode_recall":
        es = _get_v5("episodes", store.db)
        return J({"episodes": es.find_similar(
            query=a.get("query"),
            project=a.get("project"),
            outcome=a.get("outcome"),
            min_impact=a.get("min_impact", 0.0),
            concepts=a.get("concepts"),
            limit=a.get("limit", 10),
        )})

    elif name == "memory_skill_get":
        ss = _get_v5("skills", store.db)
        if a.get("name"):
            skill = ss.get_by_name(a["name"])
            return J({"skill": skill} if skill else {"error": "Skill not found"})
        if a.get("list_all"):
            return J({"skills": ss.get_all()})
        if a.get("trigger"):
            matches = ss.match_trigger(a["trigger"])
            return J({"skills": matches, "total": len(matches)})
        return J({"skills": ss.get_all()})

    elif name == "memory_skill_update":
        ss = _get_v5("skills", store.db)
        use_id = ss.record_use(a["skill_id"], a["success"], notes=a.get("notes"))
        if a.get("new_steps") or a.get("new_anti_pattern"):
            ss.refine(a["skill_id"], new_steps=a.get("new_steps"),
                      new_anti_pattern=a.get("new_anti_pattern"))
        skill = ss.get(a["skill_id"])
        return J({"updated": True, "use_id": use_id, "skill": skill})

    elif name == "memory_self_assess":
        sm = _get_v5("self_model", store.db)
        if a.get("full_report"):
            return J(sm.full_report())
        concepts = a.get("concepts", [])
        return J(sm.assess(concepts))

    elif name == "memory_context_build":
        ce = _get_v5("cognitive", store.db)
        return J(ce.build_context(
            query=a["query"],
            project=a.get("project"),
            max_tokens=a.get("max_tokens", 4000),
        ))

    elif name == "memory_reflect_now":
        ra = _get_v5("reflection", store.db)
        scope = a.get("scope", "full")
        if scope == "quick":
            result = await ra.run_quick()
        elif scope == "weekly":
            result = await ra.run_weekly()
        else:
            result = await ra.run_full()
        return J(result)

    elif name == "memory_graph_index":
        gi = _get_v5("graph_indexer", store.db)
        target = a.get("target", "all")
        if target == "all":
            result = gi.reindex_all()
        elif target == "claude_md":
            result = gi.index_claude_md()
        elif target == "skills":
            result = gi.index_skills()
        elif target == "rules":
            result = gi.index_rules_dir()
        else:
            return J({"error": f"Unknown target: {target}"})
        return J(result)

    elif name == "memory_graph_stats":
        ge = _get_v5("graph_enricher", store.db)
        return J(ge.stats())

    # ══════════════════════════════════════════════════════════
    # v7.0 Tool dispatchers
    # ══════════════════════════════════════════════════════════
    elif name == "kg_add_fact":
        from temporal_kg import TemporalKG
        tkg = TemporalKG(store.db)
        fid = tkg.add_fact(
            a["subject"], a["predicate"], a["object"],
            confidence=a.get("confidence", 1.0),
            context=a.get("context"),
            project=a.get("project", "general"),
            invalidate_previous=a.get("invalidate_previous", True),
        )
        return J({"assertion_id": fid})

    elif name == "kg_invalidate_fact":
        from temporal_kg import TemporalKG
        tkg = TemporalKG(store.db)
        closed = tkg.invalidate_fact(
            a["subject"], a["predicate"], a["object"],
            reason=a.get("reason", "manually_invalidated"),
            project=a.get("project", "general"),
        )
        return J({"closed": closed})

    elif name == "kg_at":
        from temporal_kg import TemporalKG
        tkg = TemporalKG(store.db)
        rows = tkg.query_at(
            a.get("timestamp"),
            subject=a.get("subject"),
            predicate=a.get("predicate"),
            object=a.get("object"),
            project=a.get("project"),
            limit=a.get("limit", 100),
        )
        return J({"count": len(rows), "assertions": rows})

    elif name == "kg_timeline":
        from temporal_kg import TemporalKG
        tkg = TemporalKG(store.db)
        rows = tkg.timeline(
            a["subject"],
            predicate=a.get("predicate"),
            project=a.get("project"),
            limit=a.get("limit", 500),
        )
        return J({"count": len(rows), "timeline": rows})

    elif name == "workflow_learn":
        from procedural import ProceduralMemory
        pm = ProceduralMemory(store.db)
        wf_id = pm.learn_workflow(
            a["name"], a["steps"],
            description=a.get("description"),
            trigger_pattern=a.get("trigger_pattern"),
            context=a.get("context"),
            project=a.get("project", "general"),
        )
        return J({"workflow_id": wf_id})

    elif name == "workflow_predict":
        from procedural import ProceduralMemory
        pm = ProceduralMemory(store.db)
        return J(pm.predict_outcome(
            workflow_id=a.get("workflow_id"),
            trigger=a.get("trigger"),
            project=a.get("project"),
        ))

    elif name == "workflow_track":
        from procedural import ProceduralMemory
        pm = ProceduralMemory(store.db)
        run_id = pm.track_outcome(
            a["workflow_id"], a["outcome"],
            duration_ms=a.get("duration_ms"),
            error_details=a.get("error_details"),
            notes=a.get("notes"),
            session_id=SID,
        )
        return J({"run_id": run_id})

    elif name == "file_context":
        from file_context import FileContextGuard
        g = FileContextGuard(store.db)
        return J(g.get_file_warnings(
            a["path"], project=a.get("project"),
            limit=a.get("limit", 20),
        ))

    elif name == "learn_error":
        from error_capture import ErrorCapture
        ec = ErrorCapture(store.db)
        return J(ec.learn_error(
            file=a["file"], error=a["error"],
            root_cause=a["root_cause"], fix=a["fix"],
            pattern=a["pattern"],
            severity=a.get("severity", "medium"),
            category=a.get("category", "bug"),
            project=a.get("project", "general"),
            session_id=SID,
        ))

    elif name == "session_init":
        from session_continuity import SessionContinuity
        sc = SessionContinuity(store.db)
        result = sc.session_init(
            project=a.get("project", "general"),
            mark_consumed=a.get("mark_consumed", True),
        )
        return J(result or {"message": "no pending summary"})

    elif name == "session_end":
        from session_continuity import SessionContinuity
        sc = SessionContinuity(store.db)
        return J(sc.session_end(
            a["session_id"], a.get("summary"),
            highlights=a.get("highlights"),
            pitfalls=a.get("pitfalls"),
            next_steps=a.get("next_steps"),
            open_questions=a.get("open_questions"),
            project=a.get("project", "general"),
            auto_compress=a.get("auto_compress", False),
            transcript=a.get("transcript"),
        ))

    elif name == "ingest_codebase":
        from ast_ingest import ASTIngester
        ing = ASTIngester()
        p = Path(a["path"]).expanduser()
        include = set(a.get("include", [])) or None
        if p.is_dir():
            chunks = ing.parse_directory(p, include=include)
        else:
            chunks = ing.parse_file(p)
        sample = [c.to_dict() for c in chunks[: a.get("sample_limit", 5)]]
        return J({
            "path": str(p),
            "total_chunks": len(chunks),
            "by_kind": {k: sum(1 for c in chunks if c.kind == k)
                         for k in {c.kind for c in chunks}},
            "sample": sample,
        })

    elif name == "analogize":
        from analogy import AnalogyEngine
        ae = AnalogyEngine(store.db)
        results = ae.find_analogies(
            text=a["text"],
            exclude_project=a.get("exclude_project"),
            only_types=a.get("only_types"),
            limit=a.get("limit", 10),
            min_score=a.get("min_score", 0.1),
        )
        return J({"count": len(results), "analogies": results})

    elif name == "benchmark":
        from eval_harness import EvalHarness
        def _recall_fn(q, params):
            res = recall.search(q, params.get("project"), "all",
                                 params.get("limit", 10), "full")
            # Flatten typed buckets to {id, content, score}
            flat = []
            for bucket in (res.get("results") or {}).values():
                flat.extend(bucket)
            return flat
        def _warn_fn(path, params):
            from file_context import FileContextGuard
            return FileContextGuard(store.db).get_file_warnings(
                path, project=params.get("project")
            )
        h = EvalHarness(recall_fn=_recall_fn, file_warnings_fn=_warn_fn)
        report = h.run_suite(a.get("scenarios_path"))
        return J(report)

    elif name in ("classify_task", "task_create", "phase_transition", "task_phases_list"):
        from task_classifier import classify_task
        from task_phases import TaskPhases
        tp = TaskPhases(store.db)
        return J({
            "classify_task": lambda: classify_task(a["description"], a.get("project"), db=store.db),
            "task_create": lambda: tp.create_task(a["task_id"], a["description"], level=a.get("level")),
            "phase_transition": lambda: tp.phase_transition(a["task_id"], a["new_phase"], artifacts=a.get("artifacts"), notes=a.get("notes")),
            "task_phases_list": lambda: {"phases": tp.list_phases(a["task_id"])},
        }[name]())

    elif name == "save_intent":
        from intents import save_intent as _save_intent
        db_path = str(MEMORY_DIR / "memory.db")
        rid = _save_intent(
            db_path,
            a.get("prompt", ""),
            a.get("session_id") or SID,
            a.get("project"),
        )
        return J({"saved": bool(rid), "id": rid})

    elif name == "list_intents":
        from intents import list_intents as _list_intents
        db_path = str(MEMORY_DIR / "memory.db")
        rows = _list_intents(
            db_path,
            project=a.get("project"),
            session_id=a.get("session_id"),
            limit=int(a.get("limit", 50)),
        )
        return J({"items": rows, "count": len(rows)})

    elif name == "search_intents":
        from intents import search_intents as _search_intents
        db_path = str(MEMORY_DIR / "memory.db")
        rows = _search_intents(
            db_path,
            query=a.get("query", ""),
            project=a.get("project"),
            limit=int(a.get("limit", 20)),
        )
        return J({"items": rows, "count": len(rows)})

    elif name == "save_decision":
        from decisions import Decision, DecisionOption, save_decision
        try:
            options = [
                DecisionOption(
                    name=o["name"],
                    pros=list(o.get("pros", []) or []),
                    cons=list(o.get("cons", []) or []),
                    unknowns=list(o.get("unknowns", []) or []),
                )
                for o in a["options"]
            ]
            decision = Decision(
                title=a["title"],
                options=options,
                criteria_matrix=dict(a.get("criteria_matrix", {}) or {}),
                selected=a["selected"],
                rationale=a["rationale"],
                discarded=list(a.get("discarded", []) or []),
                project=a.get("project"),
                tags=list(a.get("tags", []) or []),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return J({"saved": False, "error": f"invalid decision: {exc}"})

        rid = save_decision(store, decision, session_id=SID)
        if store.cache is not None:
            store.cache.invalidate(project=decision.project)
        return J({"saved": True, "id": rid, "structured": True})

    # ── v11.0 Phase 6 — fast-path & introspection tool dispatch ──────
    elif name == "memory_save_fast":
        rid, was_dedup, was_redacted, private_sections, quality_meta = store.save_knowledge(
            SID, a["content"], a["type"],
            a.get("project", "general"), a.get("tags", []), a.get("context", ""),
            branch=a.get("branch", BRANCH), filter_name=a.get("filter"),
            source_format=a.get("source_format", "auto"),
            importance=a.get("importance", "medium"),
            skip_quality=True,  # the explicit fast contract: no quality gate
            coref=False,        # never spend an LLM round-trip on the fast path
            agent_id=a.get("agent_id"),
            parent_agent_id=a.get("parent_agent_id"),
        )
        if rid is None:
            return J({"saved": False, "rejected_by_quality_gate": False,
                      "reason": "save_knowledge returned no id"})
        superseded = (store.supersede_values(rid, a["content"], a["type"], a.get("project", "general"))
                      if a.get("supersede") and not was_dedup else [])
        if store.cache is not None:
            store.cache.invalidate(project=a.get("project"))
        if getattr(store, "v9_cache", None) is not None:
            store.v9_cache.invalidate_all()
        out = {"saved": True, "id": rid, "deduplicated": was_dedup, "mode": "fast"}
        if superseded:
            out["superseded"] = superseded
        if was_redacted:
            out["privacy_redacted"] = True
        if private_sections:
            out["privacy_redacted_sections"] = private_sections
        return J(out)

    elif name == "memory_search_fast":
        result = recall.search(
            a["query"], a.get("project"), a.get("type", "all"),
            a.get("limit", 10), a.get("detail", "full"),
            a.get("branch"), a.get("fusion", "rrf"),
            rerank=False, diverse=False,
            embedding_space=a.get("embedding_space"),
        )
        result["mode"] = "fast"
        return J(result)

    elif name == "memory_explain_search":
        result = recall.search(
            a["query"], a.get("project"), a.get("type", "all"),
            a.get("limit", 10), detail="full",
            branch=a.get("branch"), fusion="rrf",
            rerank=False, diverse=False,
            embedding_space=a.get("embedding_space"),
            _explain=True,
            # Diagnostics must not change what they diagnose: explaining a
            # search would otherwise strengthen every record it reports on.
            record_usage=False,
        )
        return J(result)

    elif name == "memory_warmup":
        import time as _t
        t0 = _t.perf_counter()
        fastembed_loaded = False
        vector_backend = "none"
        try:
            store.embed(["warmup"])
            fastembed_loaded = bool(store.fastembed) or bool(store.embedder)
        except Exception as e:
            LOG(f"warmup embed failed: {e}")
        if store._check_binary_search():
            vector_backend = "sqlite_binary"
        elif store.chroma is not None:
            vector_backend = "chroma"
            try:
                store.chroma.heartbeat()
            except Exception:
                pass
        elapsed_ms = int((_t.perf_counter() - t0) * 1000)
        return J({
            "fastembed_loaded": fastembed_loaded,
            "vector_backend": vector_backend,
            "ms": elapsed_ms,
        })

    elif name == "memory_perf_report":
        report: dict = {}
        try:
            from memory_core.telemetry import counters as _v11_counters
            report["counters"] = _v11_counters.snapshot()
        except Exception as e:
            report["counters_error"] = str(e)
        try:
            from memory_core import embedding_cache as _v11_ec
            report["embedding_cache_v11"] = _v11_ec.stats(store.db)
        except Exception as e:
            report["embedding_cache_v11_error"] = str(e)
        # Optional benchmark snapshot from docs/v11/benchmark.md.
        try:
            from pathlib import Path as _P
            bench = _P(__file__).resolve().parent.parent / "docs" / "v11" / "benchmark.md"
            if bench.is_file():
                report["benchmark_md_bytes"] = bench.stat().st_size
                report["benchmark_md_path"] = str(bench)
        except Exception:
            pass
        return J(report)

    elif name == "memory_rebuild_fts":
        # Drop and rebuild knowledge_fts from current knowledge rows. Idempotent.
        n_before = 0
        try:
            n_before = store.db.execute(
                "SELECT COUNT(*) FROM knowledge_fts"
            ).fetchone()[0]
        except Exception:
            n_before = 0
        try:
            from memory_core.fts_schema import recreate_knowledge_fts
            recreate_knowledge_fts(store.db)
        except Exception as e:
            return J({"rebuilt": False, "error": str(e)})
        n_after = store.db.execute(
            "SELECT COUNT(*) FROM knowledge_fts"
        ).fetchone()[0]
        return J({
            "rebuilt": True,
            "rows_before": int(n_before),
            "rows_after": int(n_after),
        })

    elif name == "memory_rebuild_embeddings":
        # Re-encode every record in the listed spaces (default: all).
        es = a.get("embedding_space")
        if es is None:
            spaces: list[str] | None = None
        elif isinstance(es, str):
            spaces = [es.strip().lower()]
        else:
            spaces = sorted({str(x).strip().lower() for x in es if str(x).strip()}) or None

        proj = a.get("project")
        batch = max(1, int(a.get("batch_size", 32)))
        cap = a.get("limit")

        # Build the candidate set: every active knowledge row whose existing
        # embedding row matches the requested space (or every active row
        # when no space is set).
        sql = (
            "SELECT k.id, k.content, k.context, k.source_format "
            "FROM knowledge k "
            "LEFT JOIN embeddings e ON e.knowledge_id=k.id "
            "WHERE k.status='active'"
        )
        params: list = []
        if proj:
            sql += " AND k.project=?"
            params.append(proj)
        if spaces:
            ph = ",".join("?" * len(spaces))
            sql += f" AND COALESCE(e.embedding_space,'text') IN ({ph})"
            params.extend(spaces)
        if cap:
            sql += " LIMIT ?"
            params.append(int(cap))
        rows = store.db.execute(sql, params).fetchall()

        from memory_core.classifier import classify as _v11_classify
        from memory_core.embedding_spaces import resolve_space as _v11_resolve_space

        rebuilt = 0
        skipped = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            texts = [
                f"{r['content']} {(r['context'] if 'context' in r.keys() else '') or ''}"
                for r in chunk
            ]
            embs = store.embed(texts)
            if not embs or len(embs) != len(chunk):
                skipped += len(chunk)
                continue
            model_name = store._active_embed_model_name()
            provider = store._embed_mode or "fastembed"
            for r, vec in zip(chunk, embs):
                try:
                    cls = _v11_classify(r["content"], source_format=r["source_format"])
                    space = _v11_resolve_space(cls.type, cls.language)
                    store._upsert_embedding(
                        r["id"], vec, model_name,
                        provider=provider,
                        embedding_space=space,
                        content_type=cls.type,
                        language=cls.language,
                    )
                    rebuilt += 1
                except Exception as e:
                    LOG(f"rebuild_embeddings failed for id={r['id']}: {e}")
                    skipped += 1
            store.db.commit()
        return J({
            "rebuilt": rebuilt,
            "skipped": skipped,
            "embedding_space_filter": spaces,
            "project_filter": proj,
        })

    # ── v11.0 Phase 8 — evaluation tools dispatch ────────────────────
    elif name == "memory_eval_locomo":
        mode = a.get("mode", "fast")
        top_k = int(a.get("top_k", 5))
        limit = a.get("limit")
        scenarios_path = a.get("scenarios_path")

        with _EvalModeContext(mode):
            try:
                from memory_core.telemetry import counters as _c
                _c.reset()
                before = _c.snapshot()
            except Exception:
                before = {}

            try:
                run = _eval_run_locomo(
                    store_obj=store, recall_obj=recall,
                    top_k=top_k, limit=limit,
                    scenarios_path=scenarios_path,
                )
            except Exception as exc:
                return J({
                    "status": "error",
                    "reason": f"eval failed: {type(exc).__name__}: {exc}",
                    "mode": mode,
                })

            llm_delta, net_delta = _eval_perf_snapshot_delta(before)

        report = run["report"]
        recall_block = report.get("recall", {})
        return J({
            "scenarios_total": int(report.get("total", 0)),
            "scenarios_passed": int(report.get("passed", 0)),
            "recall_at_5": float(recall_block.get("r_at_5", 0.0)),
            "recall_at_10": float(recall_block.get("r_at_10", 0.0)),
            "latency_ms": float(report.get("latency", {}).get("p95_ms", 0.0)),
            "mode": mode,
            "llm_calls_during_eval": int(llm_delta),
            "network_calls_during_eval": int(net_delta),
            "details": {
                "recall": recall_block,
                "prevention": report.get("prevention", {}),
                "latency": report.get("latency", {}),
            },
        })

    elif name == "memory_eval_recall":
        mode = a.get("mode", "fast")
        top_k = int(a.get("top_k", 5))
        limit = a.get("limit")
        dataset_path = a.get("dataset_path")

        # Built-in fixture when no dataset_path: a tiny self-test that seeds
        # two records and asks for them back. Deterministic; no LLM.
        builtin: list[dict] | None = None
        if not dataset_path:
            seed_pairs = [
                ("eval-recall fixture postgres autovacuum tuning notes",
                 "fact", "postgres autovacuum"),
                ("eval-recall fixture redis cluster failover playbook",
                 "fact", "redis cluster"),
            ]
            for content, ktype, _q in seed_pairs:
                try:
                    store.save_knowledge(
                        sid=getattr(__import__("server"), "SID", "eval-session"),
                        content=content, ktype=ktype, project="memory_eval_recall",
                    )
                except Exception:
                    pass
            builtin = [
                {"name": f"recall_{i}", "type": "recall", "query": q,
                 "project": "memory_eval_recall", "must_contain": [q.split()[0]],
                 "k": top_k}
                for i, (_, _, q) in enumerate(seed_pairs)
            ]

        with _EvalModeContext(mode):
            try:
                from memory_core.telemetry import counters as _c
                _c.reset()
                before = _c.snapshot()
            except Exception:
                before = {}

            try:
                if builtin is not None:
                    from eval_harness import EvalHarness
                    h = EvalHarness(
                        recall_fn=lambda q, p: [
                            it
                            for grp in (recall.search(
                                q, p.get("project"), "all",
                                p.get("limit", 10), "full",
                                None, "rrf", False, False,
                            ).get("results", {}) or {}).values()
                            for it in grp
                        ],
                    )
                    import time as _t
                    t0 = _t.perf_counter()
                    report = h.run_suite(builtin)
                    elapsed = (_t.perf_counter() - t0) * 1000.0
                    run = {"report": report, "elapsed_ms": elapsed, "scenarios": builtin}
                else:
                    run = _eval_run_locomo(
                        store_obj=store, recall_obj=recall,
                        top_k=top_k, limit=limit,
                        scenarios_path=dataset_path,
                    )
            except Exception as exc:
                return J({
                    "status": "error",
                    "reason": f"eval failed: {type(exc).__name__}: {exc}",
                    "mode": mode,
                })

            llm_delta, net_delta = _eval_perf_snapshot_delta(before)

        report = run["report"]
        recall_block = report.get("recall", {})
        return J({
            "scenarios_total": int(report.get("total", 0)),
            "scenarios_passed": int(report.get("passed", 0)),
            "recall_at_5": float(recall_block.get("r_at_5", 0.0)),
            "recall_at_10": float(recall_block.get("r_at_10", 0.0)),
            "latency_ms": float(report.get("latency", {}).get("p95_ms", 0.0)),
            "mode": mode,
            "llm_calls_during_eval": int(llm_delta),
            "network_calls_during_eval": int(net_delta),
            "dataset": "builtin" if builtin is not None else dataset_path,
        })

    elif name == "memory_eval_temporal":
        mode = a.get("mode", "fast")
        try:
            from temporal_filter import parse_query_dates  # type: ignore  # noqa: F401
            from temporal_kg import TemporalKG
        except Exception as exc:
            return J({
                "status": "not_implemented",
                "reason": f"temporal modules unavailable: {type(exc).__name__}: {exc}",
                "mode": mode,
            })

        # Verify the underlying schema is present. Without it the TemporalKG
        # writes would error out — that's a not_implemented scenario.
        try:
            store.db.execute("SELECT 1 FROM fact_assertions LIMIT 1").fetchone()
        except Exception as exc:
            return J({
                "status": "not_implemented",
                "reason": f"fact_assertions table missing: {exc}",
                "mode": mode,
            })

        with _EvalModeContext(mode):
            try:
                from memory_core.telemetry import counters as _c
                _c.reset()
                before = _c.snapshot()
            except Exception:
                before = {}

            t_kg = TemporalKG(store.db)
            project = "memory_eval_temporal"
            # Two-fact timeline: stack=postgres at t1, then stack=mysql at t2.
            t1 = "2026-01-01T00:00:00Z"
            t2 = "2026-04-01T00:00:00Z"
            try:
                t_kg.add_fact("project_x", "uses", "postgres",
                              project=project, valid_from=t1)
                t_kg.add_fact("project_x", "uses", "mysql",
                              project=project, valid_from=t2)
            except Exception as exc:
                return J({
                    "status": "error",
                    "reason": f"temporal write failed: {exc}",
                    "mode": mode,
                })

            scenarios_total = 2
            scenarios_passed = 0
            details = {}

            at_t1 = t_kg.query_at(t1, subject="project_x",
                                  predicate="uses", project=project)
            if any(r.get("object") == "postgres" for r in at_t1):
                scenarios_passed += 1
            details["query_at_t1"] = [r.get("object") for r in at_t1]

            now_rows = t_kg.get_current(subject="project_x",
                                        predicate="uses", project=project)
            if any(r.get("object") == "mysql" for r in now_rows):
                scenarios_passed += 1
            details["current"] = [r.get("object") for r in now_rows]

            llm_delta, net_delta = _eval_perf_snapshot_delta(before)

        return J({
            "scenarios_total": scenarios_total,
            "scenarios_passed": scenarios_passed,
            "recall_at_5": round(scenarios_passed / scenarios_total, 4),
            "recall_at_10": round(scenarios_passed / scenarios_total, 4),
            "latency_ms": 0.0,
            "mode": mode,
            "llm_calls_during_eval": int(llm_delta),
            "network_calls_during_eval": int(net_delta),
            "details": details,
        })

    elif name == "memory_eval_entity_consistency":
        mode = a.get("mode", "fast")
        try:
            from entity_dedup import EntityCandidate, canonicalize_entity_tags
        except Exception as exc:
            return J({
                "status": "not_implemented",
                "reason": f"entity_dedup unavailable: {type(exc).__name__}: {exc}",
                "mode": mode,
            })

        with _EvalModeContext(mode):
            try:
                from memory_core.telemetry import counters as _c
                _c.reset()
                before = _c.snapshot()
            except Exception:
                before = {}

            # Deterministic embed_fn: hash text → 8-d vector. Same text
            # → same vector → cosine=1.0 for exact matches, low otherwise.
            import hashlib as _hl

            def _det_embed(texts):
                out = []
                for t in texts:
                    h = _hl.sha256(t.lower().strip().encode("utf-8")).digest()
                    vec = [b / 255.0 for b in h[:8]]
                    out.append(vec)
                return out

            canonical = "Anthropic Claude"
            candidates = [EntityCandidate(
                node_id="n1", name=canonical, type="company",
                embedding=_det_embed([canonical])[0],
            )]

            scenarios_total = 3
            scenarios_passed = 0
            attempts = []
            for variant in (canonical, canonical, canonical):
                try:
                    rewritten, decisions = canonicalize_entity_tags(
                        [variant], candidates=candidates,
                        embed_fn=_det_embed, threshold=0.99,
                    )
                    matched = canonical.lower() in [t.lower() for t in rewritten]
                    if matched:
                        scenarios_passed += 1
                    attempts.append({
                        "input": variant,
                        "rewritten": rewritten,
                        "decision": decisions[0].decision if decisions else None,
                    })
                except Exception as exc:
                    attempts.append({"input": variant, "error": str(exc)})

            llm_delta, net_delta = _eval_perf_snapshot_delta(before)

        return J({
            "scenarios_total": scenarios_total,
            "scenarios_passed": scenarios_passed,
            "recall_at_5": round(scenarios_passed / scenarios_total, 4),
            "recall_at_10": round(scenarios_passed / scenarios_total, 4),
            "latency_ms": 0.0,
            "mode": mode,
            "llm_calls_during_eval": int(llm_delta),
            "network_calls_during_eval": int(net_delta),
            "attempts": attempts,
        })

    elif name == "memory_eval_contradictions":
        mode = a.get("mode", "fast")
        try:
            from contradiction_detector import detect_contradictions
        except Exception as exc:
            return J({
                "status": "not_implemented",
                "reason": f"contradiction_detector unavailable: {type(exc).__name__}: {exc}",
                "mode": mode,
            })

        # The detector requires LLM — fast mode cannot exercise it. Honour
        # the contract: refuse without crashing, instruct caller.
        if mode == "fast":
            return J({
                "status": "not_implemented",
                "reason": "contradiction_detector requires LLM — call with mode='balanced' or 'deep'.",
                "mode": mode,
            })

        with _EvalModeContext(mode):
            try:
                from memory_core.telemetry import counters as _c
                _c.reset()
                before = _c.snapshot()
            except Exception:
                before = {}

            # Labelled fixture: one pair where new clearly supersedes old,
            # one pair where they are unrelated. We use injected fns so we
            # don't depend on a configured Ollama/Anthropic provider.
            fixture = [
                {
                    "label": "supersedes",
                    "old": "Use ChromaDB as the vector backend.",
                    "new": "Switch to SQLite-vec as the vector backend; ChromaDB is removed.",
                    "expected": "superseded",
                },
                {
                    "label": "unrelated",
                    "old": "Use ChromaDB as the vector backend.",
                    "new": "Bump pgbouncer pool_size from 25 to 100.",
                    "expected": "rejected",
                },
            ]

            def _fake_embed(texts):
                # Deterministic length-only vector; cosine ~ 1 for same text,
                # ~ length-correlated otherwise. Good enough for the fixture.
                out = []
                for t in texts:
                    n = float(len(t))
                    out.append([n, n / 2.0, n / 3.0])
                return out

            def _fake_llm_for(case_label: str):
                def _llm(prompt: str) -> str:
                    # The fixture pipes the new content into the prompt verbatim.
                    if case_label == "supersedes":
                        return ('{"contradicts": true, "confidence": 0.95, '
                                '"reason": "new switches the vector backend"}')
                    return ('{"contradicts": false, "confidence": 0.1, '
                            '"reason": "unrelated topics"}')
                return _llm

            scenarios_total = len(fixture)
            scenarios_passed = 0
            details = []
            for case in fixture:
                old_row = {
                    "id": 1, "type": "solution",
                    "content": case["old"], "project": "memory_eval_contradictions",
                }

                def _fetch(_ids, _row=old_row):
                    return [_row]

                try:
                    verdicts = detect_contradictions(
                        content=case["new"],
                        ktype="solution",
                        project="memory_eval_contradictions",
                        candidate_pool=[(1, 0.85)],
                        fetch_candidates=_fetch,
                        llm_fn=_fake_llm_for(case["label"]),
                    )
                    decisions = [v.decision for v in verdicts]
                    ok = case["expected"] in decisions
                    if ok:
                        scenarios_passed += 1
                    details.append({"label": case["label"],
                                    "expected": case["expected"],
                                    "decisions": decisions, "passed": ok})
                except Exception as exc:
                    details.append({"label": case["label"], "error": str(exc)})

            llm_delta, net_delta = _eval_perf_snapshot_delta(before)

        return J({
            "scenarios_total": scenarios_total,
            "scenarios_passed": scenarios_passed,
            "recall_at_5": round(scenarios_passed / max(1, scenarios_total), 4),
            "recall_at_10": round(scenarios_passed / max(1, scenarios_total), 4),
            "latency_ms": 0.0,
            "mode": mode,
            "llm_calls_during_eval": int(llm_delta),
            "network_calls_during_eval": int(net_delta),
            "details": details,
        })

    elif name == "memory_eval_long_context":
        mode = a.get("mode", "fast")
        n_records = max(10, int(a.get("n_records", 200)))
        top_k = int(a.get("top_k", 5))

        with _EvalModeContext(mode):
            try:
                from memory_core.telemetry import counters as _c
                _c.reset()
                before = _c.snapshot()
            except Exception:
                before = {}

            project = "memory_eval_long_context"
            # Seed a haystack of N filler records + 1 needle at the tail.
            for i in range(n_records):
                try:
                    store.save_knowledge(
                        sid=getattr(__import__("server"), "SID", "eval-session"),
                        content=f"long-context filler {i} — generic noise about caches and queues",
                        ktype="fact", project=project,
                    )
                except Exception:
                    pass
            needle_phrase = "tachyon-aluminium hyperspecific marker"
            try:
                store.save_knowledge(
                    sid=getattr(__import__("server"), "SID", "eval-session"),
                    content=f"NEEDLE — {needle_phrase}",
                    ktype="fact", project=project,
                )
            except Exception:
                pass

            scenarios = [{
                "name": "long_context_needle",
                "type": "recall",
                "query": needle_phrase,
                "project": project,
                "must_contain": ["tachyon"],
                "k": top_k,
            }]
            from eval_harness import EvalHarness
            h = EvalHarness(
                recall_fn=lambda q, p: [
                    it
                    for grp in (recall.search(
                        q, p.get("project"), "all",
                        p.get("limit", 10), "full",
                        None, "rrf", False, False,
                    ).get("results", {}) or {}).values()
                    for it in grp
                ],
            )
            import time as _t
            t0 = _t.perf_counter()
            report = h.run_suite(scenarios)
            elapsed_ms = (_t.perf_counter() - t0) * 1000.0

            llm_delta, net_delta = _eval_perf_snapshot_delta(before)

        recall_block = report.get("recall", {})
        return J({
            "scenarios_total": int(report.get("total", 0)),
            "scenarios_passed": int(report.get("passed", 0)),
            "recall_at_5": float(recall_block.get("r_at_5", 0.0)),
            "recall_at_10": float(recall_block.get("r_at_10", 0.0)),
            "latency_ms": float(report.get("latency", {}).get("p95_ms", elapsed_ms)),
            "mode": mode,
            "llm_calls_during_eval": int(llm_delta),
            "network_calls_during_eval": int(net_delta),
            "n_records": n_records,
        })

    # ── v11.0 W3 dispatch ──────────────────────────────────────────────
    elif name == "memory_recall_iterative":
        from v11_handlers import handle_recall_iterative
        def _search(q, project, ktype, k):
            from config import get_recall_excluded_tags
            from memory_core.evidence_window import EvidenceWindow
            from memory_core.retrieval import SearchScope, flatten_results
            hits = flatten_results(recall.search(q, project, ktype, limit=k, detail="full"))
            evidence = EvidenceWindow(store.db, get_recall_excluded_tags()).expand(
                hits, scope=SearchScope(project=project, kind=ktype), radius=1,
            )
            from memory_core.evidence_chains import EvidenceChains
            return EvidenceChains(store.db, get_recall_excluded_tags()).expand(
                evidence, SearchScope(project=project, kind=ktype))
        return J(handle_recall_iterative(a, search_fn=_search))

    elif name == "memory_temporal_query":
        from v11_handlers import handle_temporal_query
        return J(handle_temporal_query(a))

    elif name == "memory_entity_resolve":
        from v11_handlers import handle_entity_resolve
        def _emb(t):
            vs = store.embed([t])
            return vs[0] if vs else None
        return J(handle_entity_resolve(a, conn=store.db, embed_fn=_emb))

    elif name == "memory_consolidate_status":
        from v11_handlers import handle_consolidate_status
        return J(handle_consolidate_status(a, conn=store.db))

    # MCP: a name the server does not implement is an error, not a result.
    # _call_tool_impl turns this into an isError=true CallToolResult so the
    # model sees it and can self-correct.
    raise ValueError(f"Unknown tool: {name}")


def _detect_git_branch():
    """Auto-detect current git branch (safe: returns '' on failure)."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
    except Exception:
        return ""


async def _bootstrap_session():
    """Common setup: build Store/Recall, start session, cleanup. Called
    by every transport. Populates module globals."""
    global store, recall, SID, BRANCH
    store = Store()
    recall = Recall(store)
    SID = f"mcp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    BRANCH = _detect_git_branch()
    store.session_start(SID, branch=BRANCH)
    cleaned = store.cleanup_old_observations()
    if cleaned:
        LOG(f"Cleaned {cleaned} old observations (>{OBSERVATION_RETENTION_DAYS}d)")
    LOG(f"Session: {SID} | Branch: {BRANCH or '(none)'} | Memory: {MEMORY_DIR} | Sessions: {store.total_sessions()}")
    LOG(f"Config: decay={DECAY_HALF_LIFE}d archive={ARCHIVE_AFTER_DAYS}d purge={PURGE_AFTER_DAYS}d")
    from memory_core.cross_rerank import shared_reranker
    cross = shared_reranker()
    if cross is not None:
        cross.start()  # load in the background; searches keep the fused order until it is ready
        LOG(f"Cross-rerank: {cross.model} window={cross.window} weight={cross.weight}")


async def _run_stdio():
    """Default transport: read MCP frames from stdin/stdout. Used when
    Claude Code / Codex / Cursor / any local MCP client spawns this
    process via their config."""
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


async def _run_streamable_http(host: str, port: int, sock: socket.socket | None = None,
                               stateless: bool = False):
    """HTTP transport (MCP Streamable HTTP, spec 2025-03-26).

    Exposes:
      POST/GET/DELETE /mcp     — Streamable HTTP MCP frames
      GET             /healthz — DB-independent liveness probe

    Use case: long-lived container deployments (Docker, k8s) where the
    MCP server runs as a daemon and clients (or sidecars) talk to it
    over the network. Lives at /mcp so the same image can host the
    dashboard on 37737 and MCP on 3737 without path conflicts.

    `sock` is a listening socket shared with sibling worker processes
    (MCP_HTTP_WORKERS); those run `stateless`, because consecutive
    requests of one client can reach different workers.
    """
    from contextlib import asynccontextmanager

    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    # One manager per process. A single process keeps short-lived sessions
    # in memory; workers sharing a socket cannot, so they run stateless.
    manager = StreamableHTTPSessionManager(
        app=app,
        event_store=None,
        json_response=False,
        stateless=stateless,
    )

    async def handle_mcp(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    async def healthz(request: Request) -> Response:
        return JSONResponse({
            "status": "ok",
            "transport": "streamable-http",
            "session_id": SID,
            "memory_dir": str(MEMORY_DIR),
            "pid": os.getpid(),
        })

    @asynccontextmanager
    async def lifespan(starlette_app):
        # Start the session manager when uvicorn boots, stop it cleanly
        # on SIGTERM — otherwise in-flight SSE streams leak.
        async with manager.run():
            yield

    starlette_app = Starlette(
        debug=False,
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Mount("/mcp", app=handle_mcp),
        ],
        lifespan=lifespan,
    )

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        # Drain in-flight requests for up to 5s before kill — matches
        # Docker stop-grace defaults.
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    LOG(f"MCP HTTP transport listening on http://{host}:{port}/mcp (healthz: /healthz, pid {os.getpid()})")
    await server.serve(sockets=[sock] if sock is not None else None)


HTTP_TRANSPORTS = ("http", "streamable-http", "streamable_http")
HTTP_LISTEN_BACKLOG = 2048


def _transport() -> str:
    return (os.environ.get("MCP_TRANSPORT", "stdio") or "stdio").lower().strip()


def _http_address() -> tuple[str, int]:
    return os.environ.get("MCP_HTTP_HOST", "127.0.0.1"), int(os.environ.get("MCP_HTTP_PORT", "3737"))


def _http_workers() -> int:
    raw = os.environ.get("MCP_HTTP_WORKERS", "1").strip() or "1"
    try:
        workers = int(raw)
    except ValueError:
        raise SystemExit(f"MCP_HTTP_WORKERS must be a positive integer, got {raw!r}") from None
    if workers < 1:
        raise SystemExit(f"MCP_HTTP_WORKERS must be a positive integer, got {raw!r}")
    return workers


HTTP_LISTEN_FD_ENV = "MCP_HTTP_LISTEN_FD"


def _hold_init_lock():
    """Exclusive lock that serialises worker start-up.

    Workers start together; on a fresh store they would race to apply the
    schema migrations. The first one to hold the lock applies them.
    """
    import fcntl

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(MEMORY_DIR / ".http-workers.lock", "w")  # noqa: SIM115 — released in _http_worker
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


async def _http_worker(host: str, port: int, sock: socket.socket, init_lock):
    import fcntl

    try:
        await _bootstrap_session()
    finally:
        fcntl.flock(init_lock, fcntl.LOCK_UN)
        init_lock.close()
    await _run_streamable_http(host, port, sock=sock, stateless=True)


def _serve_http_workers(host: str, port: int, workers: int) -> None:
    """Run `workers` HTTP server processes on one listening socket.

    Tool calls run synchronously inside a process's event loop, so one
    process serves one call at a time. Each worker is a fresh interpreter
    with its own Store and SQLite connection, handed the listening socket
    by file descriptor; SQLite (WAL) serialises their writes. If any worker
    exits, the others are stopped and its exit code is passed on, so the
    service manager (systemd, Docker, k8s) restarts the whole group.
    """
    if os.name != "posix":
        raise SystemExit("MCP_HTTP_WORKERS > 1 needs a POSIX system (Linux, macOS); use one worker.")
    sock = socket.create_server((host, port), backlog=HTTP_LISTEN_BACKLOG)
    fd = sock.fileno()
    env = dict(os.environ, MCP_HTTP_WORKERS="1", **{HTTP_LISTEN_FD_ENV: str(fd)})
    children = [
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], env=env, pass_fds=(fd,))
        for _ in range(workers)
    ]
    sock.close()
    LOG(f"MCP HTTP: {workers} workers on http://{host}:{port}/mcp, pids {[c.pid for c in children]}")

    def stop_children(signum=None, frame=None):
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)
    by_pid = {child.pid: child for child in children}
    exit_code: int | None = None
    while by_pid:
        pid, status = os.wait()
        if by_pid.pop(pid, None) is None:
            continue
        if exit_code is None:
            exit_code = os.waitstatus_to_exitcode(status)
            if by_pid:
                LOG(f"HTTP worker {pid} exited with {exit_code}; stopping the others")
                stop_children()
    sys.exit(exit_code or 0)


async def main():
    await _bootstrap_session()
    transport = _transport()
    if transport in HTTP_TRANSPORTS:
        await _run_streamable_http(*_http_address())
    elif transport == "stdio":
        await _run_stdio()
    else:
        raise SystemExit(
            f"Unknown MCP_TRANSPORT={transport!r}. "
            f"Expected one of: stdio, http, streamable-http."
        )


def run() -> None:
    """Process entry point: one server, a worker handed a socket, or a group of workers."""
    listen_fd = os.environ.get(HTTP_LISTEN_FD_ENV)
    if listen_fd:
        asyncio.run(_http_worker(*_http_address(), socket.socket(fileno=int(listen_fd)), _hold_init_lock()))
    elif _transport() in HTTP_TRANSPORTS and _http_workers() > 1:
        _serve_http_workers(*_http_address(), _http_workers())
    else:
        asyncio.run(main())


if __name__ == "__main__":
    run()
