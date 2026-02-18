#!/usr/bin/env python3
"""
Claude Total Memory — MCP Server v3.0 (Self-Improving Agent)

Tools (19): memory_recall, memory_save, memory_update, memory_timeline,
            memory_stats, memory_consolidate, memory_export, memory_forget,
            memory_history, memory_delete, memory_relate, memory_search_by_tag,
            memory_extract_session,
            self_error_log, self_insight, self_rules, self_patterns,
            self_reflect, self_rules_context
Storage: SQLite FTS5 + ChromaDB (semantic) + relations (graph)
Features: BM25 scoring, progressive disclosure, decay scoring, fuzzy search,
          deduplication, retention zones, consolidation, version history, graph relations,
          self-improvement pipeline (errors → insights → rules/SOUL)
"""

import asyncio
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

try:
    import chromadb
    HAS_CHROMA = True
except ImportError:
    HAS_CHROMA = False

try:
    from sentence_transformers import SentenceTransformer
    HAS_ST = True
except ImportError:
    HAS_ST = False

MEMORY_DIR = Path(os.environ.get("CLAUDE_MEMORY_DIR", os.path.expanduser("~/.claude-memory")))
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
DECAY_HALF_LIFE = int(os.environ.get("DECAY_HALF_LIFE", "90"))  # days
ARCHIVE_AFTER_DAYS = int(os.environ.get("ARCHIVE_AFTER_DAYS", "180"))
PURGE_AFTER_DAYS = int(os.environ.get("PURGE_AFTER_DAYS", "365"))
LOG = lambda msg: sys.stderr.write(f"[memory-mcp] {msg}\n")


# ═══════════════════════════════════════════════════════════
# Storage
# ═══════════════════════════════════════════════════════════

class Store:
    def __init__(self):
        for d in ["raw", "chroma", "transcripts", "queue", "backups", "extract-queue"]:
            (MEMORY_DIR / d).mkdir(parents=True, exist_ok=True)

        self.db = sqlite3.connect(str(MEMORY_DIR / "memory.db"))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._schema()
        self._migrate()
        self._check_fts()

        self.chroma = None
        if HAS_CHROMA:
            try:
                c = chromadb.PersistentClient(path=str(MEMORY_DIR / "chroma"))
                self.chroma = c.get_or_create_collection("knowledge", metadata={"hnsw:space": "cosine"})
            except Exception as e:
                LOG(f"ChromaDB init: {e}")

        self._embedder = None

    @property
    def embedder(self):
        if self._embedder is None and HAS_ST:
            try:
                self._embedder = SentenceTransformer(EMBEDDING_MODEL)
            except Exception:
                pass
        return self._embedder

    def embed(self, texts):
        if not self.embedder:
            return None
        try:
            return self.embedder.encode(texts).tolist()
        except Exception:
            return None

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
        """)
        self.db.commit()

    def _migrate(self):
        """Add columns/tables that may not exist in older databases."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(knowledge)").fetchall()}
        if "recall_count" not in cols:
            self.db.execute("ALTER TABLE knowledge ADD COLUMN recall_count INTEGER DEFAULT 0")
        if "last_recalled" not in cols:
            self.db.execute("ALTER TABLE knowledge ADD COLUMN last_recalled TEXT")

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
                self.db.execute("DROP TABLE IF EXISTS knowledge_fts")
                self.db.execute("DROP TRIGGER IF EXISTS k_fts_i")
                self.db.execute("DROP TRIGGER IF EXISTS k_fts_u")
                self.db.executescript("""
                    CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                        content, context, tags, content='knowledge', content_rowid='id'
                    );
                    CREATE TRIGGER k_fts_i AFTER INSERT ON knowledge BEGIN
                        INSERT INTO knowledge_fts(rowid,content,context,tags)
                        VALUES (new.id,new.content,new.context,new.tags);
                    END;
                    CREATE TRIGGER k_fts_u AFTER UPDATE ON knowledge BEGIN
                        INSERT INTO knowledge_fts(knowledge_fts,rowid,content,context,tags)
                        VALUES ('delete',old.id,old.content,old.context,old.tags);
                        INSERT INTO knowledge_fts(rowid,content,context,tags)
                        VALUES (new.id,new.content,new.context,new.tags);
                    END;
                """)
                self.db.execute(
                    "INSERT INTO knowledge_fts(rowid,content,context,tags) "
                    "SELECT id,content,context,tags FROM knowledge WHERE status='active'"
                )
                self.db.commit()
                LOG("FTS5 recreated from scratch: OK")

    def q(self, sql, params=()):
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def q1(self, sql, params=()):
        r = self.db.execute(sql, params).fetchone()
        return dict(r) if r else None

    def raw_append(self, sid, entry):
        entry["_ts"] = datetime.utcnow().isoformat() + "Z"
        p = MEMORY_DIR / "raw" / f"{sid}.jsonl"
        with open(p, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def session_start(self, sid, project="general"):
        now = datetime.utcnow().isoformat() + "Z"
        self.db.execute("INSERT OR IGNORE INTO sessions (id,started_at,project) VALUES (?,?,?)",
                        (sid, now, project))
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
        """Check if very similar knowledge already exists."""
        try:
            words = [w for w in content.split()[:12] if len(w) > 2]
            if not words:
                return None
            fts_q = " OR ".join(self._fts_escape(w) for w in words)
            rows = self.q("""
                SELECT k.id, k.content FROM knowledge_fts f
                JOIN knowledge k ON k.id=f.rowid
                WHERE f.content MATCH ? AND k.status='active' AND k.project=? AND k.type=?
                ORDER BY rank LIMIT 5
            """, (fts_q, project, ktype))
            for row in rows:
                if self._jaccard(content, row["content"]) > 0.85:
                    return row["id"]
                if self._fuzzy_ratio(content, row["content"]) > 0.90:
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
            now = datetime.now(lc.tzinfo) if lc.tzinfo else datetime.utcnow()
            days = (now - lc.replace(tzinfo=None)).days if not lc.tzinfo else (now - lc).days
            return max(0.01, math.exp(-days * math.log(2) / half_life_days))
        except Exception:
            return 0.5

    # ── CRUD ──

    def save_knowledge(self, sid, content, ktype, project="general", tags=None, context=""):
        now = datetime.utcnow().isoformat() + "Z"

        dup_id = self._find_duplicate(content, ktype, project)
        if dup_id:
            self.db.execute("UPDATE knowledge SET last_confirmed=? WHERE id=?", (now, dup_id))
            self.db.commit()
            LOG(f"Dedup: updated last_confirmed for id={dup_id}")
            return dup_id

        cur = self.db.execute("""
            INSERT INTO knowledge (session_id,type,content,context,project,tags,source,confidence,created_at,last_confirmed,recall_count)
            VALUES (?,?,?,?,?,?,'explicit',1.0,?,?,0)
        """, (sid, ktype, content, context, project, json.dumps(tags or []), now, now))
        self.db.commit()
        rid = cur.lastrowid

        if self.chroma:
            embs = self.embed([f"{content} {context}"])
            if embs:
                try:
                    self.chroma.upsert(
                        ids=[str(rid)], embeddings=embs, documents=[content],
                        metadatas=[{"type": ktype, "project": project, "status": "active",
                                    "session_id": sid, "created_at": now, "confidence": 1.0}])
                except Exception:
                    pass
        return rid

    def bump_recall(self, ids):
        """Strengthen memories that are recalled (spaced repetition effect)."""
        now = datetime.utcnow().isoformat() + "Z"
        for kid in ids:
            self.db.execute(
                "UPDATE knowledge SET recall_count=recall_count+1, last_recalled=?, last_confirmed=? WHERE id=?",
                (now, now, kid))
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
        now = datetime.utcnow().isoformat() + "Z"
        self.db.execute("UPDATE knowledge SET last_confirmed=? WHERE id=?", (now, longest["id"]))
        merged_ids = []
        for r in group:
            if r["id"] != longest["id"]:
                self.db.execute(
                    "UPDATE knowledge SET status='consolidated', superseded_by=? WHERE id=?",
                    (longest["id"], r["id"]))
                merged_ids.append(r["id"])
                if self.chroma:
                    try:
                        self.chroma.delete(ids=[str(r["id"])])
                    except Exception:
                        pass
        self.db.commit()
        return {"kept": longest["id"], "merged": merged_ids}

    # ── Retention Zones ──

    def apply_retention(self):
        """Move old unconfirmed records: active→archived→purged."""
        now = datetime.utcnow()
        archive_cutoff = (now - __import__('datetime').timedelta(days=ARCHIVE_AFTER_DAYS)).isoformat() + "Z"
        purge_cutoff = (now - __import__('datetime').timedelta(days=PURGE_AFTER_DAYS)).isoformat() + "Z"

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

        if self.chroma and (archived or purged):
            for r in self.q("SELECT id FROM knowledge WHERE status IN ('archived','purged')"):
                try:
                    self.chroma.delete(ids=[str(r["id"])])
                except Exception:
                    pass

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
        sessions = self.q("SELECT * FROM sessions ORDER BY started_at")
        return {
            "version": "2.1",
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "knowledge": rows,
            "sessions": sessions,
            "relations": self.q("SELECT * FROM relations"),
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
        self.db.execute("UPDATE knowledge SET status='deleted' WHERE id=?", (kid,))
        self.db.commit()
        if self.chroma:
            try:
                self.chroma.delete(ids=[str(kid)])
            except Exception:
                pass
        return rec

    # ── Relations ──

    def add_relation(self, from_id, to_id, rel_type):
        """Create a relation between two knowledge records."""
        now = datetime.utcnow().isoformat() + "Z"
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
        """Find all active knowledge with a matching tag."""
        conds = ["status='active'"]
        params = []
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

    # ═══════════════════════════════════════════════════════════
    # Self-Improvement: Errors / Insights / Rules
    # ═══════════════════════════════════════════════════════════

    def log_error(self, sid, description, category, severity="medium",
                  fix="", context="", project="general", tags=None):
        """Log a structured error and check for patterns."""
        now = datetime.utcnow().isoformat() + "Z"
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
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat() + "Z"
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
        now = datetime.utcnow().isoformat() + "Z"

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
        now = datetime.utcnow().isoformat() + "Z"
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
        now = datetime.utcnow().isoformat() + "Z"

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

    def get_rules_for_context(self, project="general", categories=None):
        """Get active rules relevant to current context."""
        scopes = ["'global'", f"'project:{project}'"]
        if categories:
            scopes.extend(f"'category:{c}'" for c in categories)
        rows = self.q(f"""
            SELECT * FROM rules
            WHERE status='active' AND scope IN ({','.join(scopes)})
            ORDER BY priority DESC, success_rate DESC LIMIT 20
        """)
        now = datetime.utcnow().isoformat() + "Z"
        for r in rows:
            self.db.execute(
                "UPDATE rules SET fire_count=fire_count+1, last_fired=?, updated_at=? WHERE id=?",
                (now, now, r["id"]))
        self.db.commit()
        return {"rules_count": len(rows), "rules": rows}

    def analyze_patterns(self, view="full_report", project=None, days=30):
        """Analyze error patterns and self-improvement metrics."""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
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
            from datetime import timedelta
            weeks = []
            for w in range(4):
                start = (datetime.utcnow() - timedelta(days=(w+1)*7)).isoformat() + "Z"
                end = (datetime.utcnow() - timedelta(days=w*7)).isoformat() + "Z"
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

    def search(self, query, project=None, ktype="all", limit=10, detail="full"):
        results = {}

        # Tier 1: FTS5 keyword search with BM25 scoring
        fts_q = " OR ".join(Store._fts_escape(w) for w in re.split(r'\s+', query) if len(w) > 2) or Store._fts_escape(query)
        try:
            conds = ["knowledge_fts MATCH ?", "k.status='active'"]
            params = [fts_q]
            if project:
                conds.append("k.project=?")
                params.append(project)
            if ktype != "all":
                conds.append("k.type=?")
                params.append(ktype)
            params.append(limit * 3)
            for r in self.s.db.execute(f"""
                SELECT k.*, bm25(knowledge_fts) AS _bm25
                FROM knowledge_fts f JOIN knowledge k ON k.id=f.rowid
                WHERE {' AND '.join(conds)} ORDER BY bm25(knowledge_fts) LIMIT ?
            """, params).fetchall():
                row = dict(r)
                bm25_raw = abs(row.pop("_bm25", 0))
                bm25_score = min(2.0, bm25_raw / max(bm25_raw, 1.0))  # normalize to ~0-2 range
                results[r["id"]] = {"r": row, "score": max(0.5, bm25_score), "via": ["fts"]}
        except Exception:
            pass

        # Tier 2: Semantic search via ChromaDB
        if self.s.chroma and self.s.embedder:
            embs = self.s.embed([query])
            if embs:
                where = {"status": "active"}
                if project:
                    where = {"$and": [{"status": "active"}, {"project": project}]}
                try:
                    cr = self.s.chroma.query(
                        query_embeddings=embs, where=where,
                        n_results=limit * 3, include=["distances", "documents", "metadatas"])
                    for i, rid_s in enumerate(cr["ids"][0]):
                        rid = int(rid_s)
                        score = max(0, 1.0 - cr["distances"][0][i])
                        if rid in results:
                            results[rid]["score"] += score
                            results[rid]["via"].append("semantic")
                        else:
                            rec = self.s.q1("SELECT * FROM knowledge WHERE id=?", (rid,))
                            if rec:
                                results[rid] = {"r": rec, "score": score, "via": ["semantic"]}
                except Exception:
                    pass

        # Tier 3: Fuzzy search (catches typos and partial matches)
        if len(results) < limit:
            try:
                conds2 = ["k.status='active'"]
                params2 = []
                if project:
                    conds2.append("k.project=?")
                    params2.append(project)
                if ktype != "all":
                    conds2.append("k.type=?")
                    params2.append(ktype)
                params2.append(limit * 5)
                candidates = self.s.q(f"""
                    SELECT * FROM knowledge k WHERE {' AND '.join(conds2)}
                    ORDER BY last_confirmed DESC LIMIT ?
                """, params2)
                ql = query.lower()
                for r in candidates:
                    if r["id"] in results:
                        continue
                    ratio = SequenceMatcher(None, ql, r["content"][:200].lower()).ratio()
                    if ratio > 0.35:
                        results[r["id"]] = {"r": r, "score": ratio * 0.6, "via": ["fuzzy"]}
            except Exception:
                pass

        # Tier 4: Graph expansion (1 hop from top 5)
        top5 = sorted(results, key=lambda x: results[x]["score"], reverse=True)[:5]
        for kid in top5:
            for r in self.s.q("""
                SELECT k.* FROM relations rel
                JOIN knowledge k ON k.id = CASE WHEN rel.from_id=? THEN rel.to_id ELSE rel.from_id END
                WHERE (rel.from_id=? OR rel.to_id=?) AND k.status='active'
            """, (kid, kid, kid)):
                if r["id"] not in results:
                    results[r["id"]] = {"r": r, "score": results[kid]["score"] * 0.4, "via": ["graph"]}

        # Apply decay scoring
        for item in results.values():
            lc = item["r"].get("last_confirmed", "")
            decay = Store._decay_factor(lc, DECAY_HALF_LIFE)
            recall_boost = min(0.3, (item["r"].get("recall_count", 0) or 0) * 0.05)
            item["score"] *= (decay + recall_boost)

        # Rank, group, and bump recall counts
        ranked = sorted(results.values(), key=lambda x: x["score"], reverse=True)[:limit]
        returned_ids = [item["r"]["id"] for item in ranked]
        if returned_ids:
            self.s.bump_recall(returned_ids)

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
            if detail == "summary":
                content = content[:150] + ("..." if len(content) > 150 else "")
                context = ""

            grouped[t].append({
                "id": r["id"], "content": content, "context": context,
                "project": r.get("project", ""), "tags": tags,
                "confidence": r.get("confidence", 1.0),
                "created_at": r.get("created_at", ""), "session_id": r.get("session_id", ""),
                "score": round(item["score"], 3), "via": item["via"],
                "recall_count": r.get("recall_count", 0),
                "decay": round(Store._decay_factor(r.get("last_confirmed", ""), DECAY_HALF_LIFE), 3),
            })
        return {"query": query, "total": len(ranked), "detail": detail, "results": grouped}

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
            WHERE status='active' AND last_confirmed < datetime('now', '-90 days')
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
            "storage_mb": {
                "transcripts": round(trans_mb, 1),
                "raw_logs": round(raw_mb, 1),
                "sqlite": round(db_mb, 1),
                "chroma": round(chroma_mb, 1),
                "total": round(raw_mb + trans_mb + db_mb + chroma_mb, 1),
            },
            "config": {
                "decay_half_life_days": DECAY_HALF_LIFE,
                "archive_after_days": ARCHIVE_AFTER_DAYS,
                "purge_after_days": PURGE_AFTER_DAYS,
                "embedding_model": EMBEDDING_MODEL,
                "has_chromadb": HAS_CHROMA,
                "has_sentence_transformers": HAS_ST,
            },
            "self_improvement": self._si_stats(s),
        }

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

app = Server("claude-total-memory")
store: Store = None
recall: Recall = None
SID: str = None


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="memory_recall",
            description="Search ALL memory: decisions, solutions, facts, lessons from ALL past sessions. "
                        "Uses 4-tier search: FTS5 keyword → semantic (ChromaDB) → fuzzy → graph expansion. "
                        "Results include decay scoring (recent = higher rank). Use BEFORE starting any task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for"},
                    "project": {"type": "string", "description": "Filter by project name"},
                    "type": {"type": "string", "enum": ["decision", "fact", "solution", "lesson", "convention", "all"],
                             "default": "all"},
                    "limit": {"type": "integer", "default": 10},
                    "detail": {"type": "string", "enum": ["summary", "full"], "default": "full",
                               "description": "Level of detail: 'summary' truncates content to 150 chars (saves tokens), 'full' returns everything"},
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
            name="memory_save",
            description="Save knowledge explicitly. Types: decision (MUST include WHY in context), "
                        "solution, lesson, fact, convention. Auto-dedup via Jaccard + fuzzy similarity.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The knowledge to save"},
                    "type": {"type": "string", "enum": ["decision", "fact", "solution", "lesson", "convention"]},
                    "project": {"type": "string", "default": "general"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "context": {"type": "string", "description": "Additional context, WHY for decisions"},
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
                    "save_to_file": {"type": "boolean", "description": "Save to ~/.claude-memory/backups/ (default true)", "default": True},
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
                        "After task completion, rate rules: self_rules(action='rate', id=X, success=true/false).",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "default": "general"},
                    "categories": {"type": "array", "items": {"type": "string"},
                                   "description": "Error categories relevant to current task"},
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name, args):
    store.raw_append(SID, {"type": "tool_call", "tool": name, "args": args})
    try:
        r = await _do(name, args)
        return [TextContent(type="text", text=r)]
    except Exception as e:
        LOG(f"Error in {name}: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


async def _do(name, a):
    J = lambda x: json.dumps(x, ensure_ascii=False, indent=2, default=str)

    if name == "memory_recall":
        return J(recall.search(a["query"], a.get("project"), a.get("type", "all"),
                               a.get("limit", 10), a.get("detail", "full")))

    elif name == "memory_timeline":
        kwargs = {k: a.get(k) for k in
                  ["query", "session_number", "sessions_ago", "date_from", "date_to", "project", "limit"]}
        return J(recall.timeline(**kwargs))

    elif name == "memory_save":
        dup_id = store._find_duplicate(a["content"], a["type"], a.get("project", "general"))
        rid = store.save_knowledge(
            SID, a["content"], a["type"],
            a.get("project", "general"), a.get("tags", []), a.get("context", ""))
        return J({"saved": True, "id": rid, "deduplicated": dup_id is not None})

    elif name == "memory_update":
        res = recall.search(a["find"], limit=3)
        items = [i for g in res.get("results", {}).values() for i in g]
        if not items:
            return J({"error": "Not found", "query": a["find"]})
        old = items[0]
        old_rec = store.q1("SELECT * FROM knowledge WHERE id=?", (old["id"],))
        if not old_rec:
            return J({"error": "Record not found in DB"})
        new_id = store.save_knowledge(
            SID, a["new_content"], old_rec["type"], old_rec["project"],
            json.loads(old_rec.get("tags", "[]")),
            f"Updated: {a.get('reason', '')}. Was: {old_rec['content'][:200]}")
        store.db.execute(
            "UPDATE knowledge SET status='superseded',superseded_by=? WHERE id=?",
            (new_id, old["id"]))
        store.db.commit()
        if store.chroma:
            try:
                store.chroma.delete(ids=[str(old["id"])])
            except Exception:
                pass
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
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            proj = a.get("project", "all")
            path = MEMORY_DIR / "backups" / f"export_{proj}_{ts}.json"
            with open(path, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            return J({"exported": True, "file": str(path),
                       "knowledge_count": len(data["knowledge"]), "sessions_count": len(data["sessions"])})
        else:
            return J(data)

    elif name == "memory_forget":
        dry_run = a.get("dry_run", True)
        if dry_run:
            archive_cutoff = (datetime.utcnow() - __import__('datetime').timedelta(days=ARCHIVE_AFTER_DAYS)).isoformat() + "Z"
            purge_cutoff = (datetime.utcnow() - __import__('datetime').timedelta(days=PURGE_AFTER_DAYS)).isoformat() + "Z"
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
        rid = store.save_knowledge(
            SID, a["reflection"], "reflection",
            a.get("project", "general"),
            (a.get("tags") or []) + ["self-reflection", a.get("outcome", "success")],
            f"Task: {a['task_summary']}. Outcome: {a.get('outcome', 'success')}")
        return J({"saved": True, "id": rid, "type": "reflection"})

    elif name == "self_rules_context":
        return J(store.get_rules_for_context(
            a.get("project", "general"), a.get("categories")))

    return J({"error": "Unknown tool"})


async def main():
    global store, recall, SID
    store = Store()
    recall = Recall(store)
    SID = f"mcp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    store.session_start(SID)
    LOG(f"Session: {SID} | Memory: {MEMORY_DIR} | Sessions: {store.total_sessions()}")
    LOG(f"Config: decay={DECAY_HALF_LIFE}d archive={ARCHIVE_AFTER_DAYS}d purge={PURGE_AFTER_DAYS}d")
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
