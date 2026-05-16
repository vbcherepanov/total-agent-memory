"""
Deep Graph Enrichment — dramatically improve knowledge graph connectivity.

Addresses the core problem: 0.31% density, 63% weak mentioned_with edges,
29% unlinked knowledge records. This module creates meaningful semantic,
hierarchical, cross-project, and temporal edges.

Usage:
    python src/tools/graph_enrichment.py            # run all enrichments
    python src/tools/graph_enrichment.py --stats     # show current graph stats
    python src/tools/graph_enrichment.py --dry-run   # preview changes without writing
    python src/tools/graph_enrichment.py --orphans   # only link orphan records
    python src/tools/graph_enrichment.py --semantic   # only add semantic edges
    python src/tools/graph_enrichment.py --hierarchy  # only add hierarchy edges
    python src/tools/graph_enrichment.py --cross      # only add cross-project edges
    python src/tools/graph_enrichment.py --temporal   # only add temporal edges
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paths import memory_dir

LOG = lambda msg: sys.stderr.write(f"[graph-enrichment] {msg}\n")

DEFAULT_DB = str(memory_dir() / "memory.db")


def _now() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    """Generate a new UUID hex string."""
    return uuid.uuid4().hex


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity between two sets."""
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _tokenize_content(content: str) -> set[str]:
    """Extract meaningful tokens from content for matching.

    Splits on whitespace and punctuation, lowercases, filters short tokens
    and common stopwords.
    """
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "must", "need", "ought",
        "and", "but", "or", "nor", "not", "no", "so", "if", "then", "else",
        "when", "where", "how", "what", "which", "who", "whom", "whose",
        "this", "that", "these", "those", "it", "its", "of", "in", "on",
        "at", "to", "for", "with", "by", "from", "as", "into", "about",
        "after", "before", "between", "under", "over", "up", "down", "out",
        "all", "each", "every", "both", "few", "more", "most", "some", "any",
        "such", "only", "own", "same", "than", "too", "very",
        # Russian stopwords
        "и", "в", "на", "с", "по", "для", "из", "не", "что", "это",
        "как", "все", "или", "но", "да", "нет", "при", "от", "до",
        "уже", "он", "она", "оно", "они", "мы", "вы", "его", "её",
        "их", "мой", "наш", "ваш", "тот", "этот", "тут", "там",
    }
    tokens = set(re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9_]+", content.lower()))
    return {t for t in tokens if len(t) >= 3 and t not in stopwords}


def _get_db(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with proper settings."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def _ensure_edge(
    db: sqlite3.Connection,
    source_id: str,
    target_id: str,
    relation_type: str,
    weight: float = 1.0,
    context: str | None = None,
) -> bool:
    """Create an edge if it doesn't exist. Returns True if created.

    Avoids self-loops. If edge exists, reinforces it instead.
    """
    if source_id == target_id:
        return False

    existing = db.execute(
        """SELECT id, weight FROM graph_edges
           WHERE source_id = ? AND target_id = ? AND relation_type = ?""",
        (source_id, target_id, relation_type),
    ).fetchone()

    if existing:
        # Reinforce existing edge
        db.execute(
            """UPDATE graph_edges
               SET weight = MAX(weight, ?),
                   last_reinforced_at = ?,
                   reinforcement_count = reinforcement_count + 1,
                   context = COALESCE(?, context)
               WHERE id = ?""",
            (weight, _now(), context, existing["id"]),
        )
        return False

    db.execute(
        """INSERT OR IGNORE INTO graph_edges
           (id, source_id, target_id, relation_type, weight, context, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_new_id(), source_id, target_id, relation_type, weight, context, _now()),
    )
    return True


def _get_or_create_node(
    db: sqlite3.Connection,
    name: str,
    node_type: str,
    content: str | None = None,
    source: str = "enrichment",
) -> str:
    """Get an existing node by name+type or create it. Returns node_id."""
    row = db.execute(
        "SELECT id FROM graph_nodes WHERE name = ? AND type = ?",
        (name, node_type),
    ).fetchone()

    if row:
        return row["id"]

    node_id = _new_id()
    now = _now()
    db.execute(
        """INSERT INTO graph_nodes
           (id, type, name, content, source, importance, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, 0.5, ?, ?)""",
        (node_id, node_type, name, content, source, now, now),
    )
    return node_id


# ──────────────────────────────────────────────────────────
# 1. Link Orphan Knowledge Records
# ──────────────────────────────────────────────────────────

def link_orphan_records(db_path: str) -> int:
    """Find knowledge records not linked to any graph node and link them.

    Strategy:
    1. Get all active node names for fast matching
    2. For each orphan: tokenize content, match against node names
    3. Also link to project node and tag-based concept nodes
    4. Fallback: extract key phrases via regex patterns

    Returns count of new knowledge_nodes links created.
    """
    db = _get_db(db_path)
    new_links = 0

    # Get orphan knowledge records
    orphans = db.execute(
        """SELECT k.id, k.content, k.project, k.tags, k.type, k.context
           FROM knowledge k
           WHERE k.status = 'active'
             AND k.id NOT IN (SELECT knowledge_id FROM knowledge_nodes)
           ORDER BY k.id"""
    ).fetchall()

    if not orphans:
        LOG("No orphan knowledge records found")
        db.close()
        return 0

    LOG(f"Processing {len(orphans)} orphan knowledge records...")

    # Build node name lookup: name -> (id, type)
    all_nodes = db.execute(
        "SELECT id, name, type FROM graph_nodes WHERE status = 'active'"
    ).fetchall()

    # Index by lowercase name for matching
    node_by_name: dict[str, tuple[str, str]] = {}
    for node in all_nodes:
        node_by_name[node["name"].lower()] = (node["id"], node["type"])

    # Also build multi-word name index for compound matches
    multiword_names: dict[str, tuple[str, str]] = {}
    for name, (nid, ntype) in node_by_name.items():
        if " " in name or "_" in name or "-" in name:
            multiword_names[name] = (nid, ntype)

    # Technology keywords for extraction
    tech_patterns = {
        "php", "symfony", "go", "golang", "vue", "nuxt", "docker",
        "postgresql", "postgres", "redis", "rabbitmq", "grpc", "protobuf",
        "git", "bitrix", "bitrix24", "phpstan", "phpunit", "eslint",
        "tailwind", "pinia", "typescript", "javascript", "python",
        "makefile", "nginx", "linux", "macos", "sqlite", "chromadb",
        "ollama", "prometheus", "grafana", "sentry", "celery",
        "fastapi", "django", "flask", "sqlalchemy", "pydantic",
    }

    for orphan in orphans:
        kid = orphan["id"]
        content = orphan["content"] or ""
        project = orphan["project"] or "general"
        context_text = orphan["context"] or ""
        full_text = f"{content} {context_text}".lower()

        try:
            tags = json.loads(orphan["tags"]) if orphan["tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []

        linked_nodes: set[str] = set()

        # 1. Match against existing node names (single word)
        tokens = _tokenize_content(full_text)
        for token in tokens:
            if token in node_by_name:
                nid, ntype = node_by_name[token]
                if nid not in linked_nodes:
                    db.execute(
                        """INSERT OR IGNORE INTO knowledge_nodes
                           (knowledge_id, node_id, role, strength)
                           VALUES (?, ?, 'mentions', 0.7)""",
                        (kid, nid),
                    )
                    linked_nodes.add(nid)
                    new_links += 1

        # 2. Match multi-word node names in full text
        for mw_name, (nid, ntype) in multiword_names.items():
            if mw_name in full_text and nid not in linked_nodes:
                db.execute(
                    """INSERT OR IGNORE INTO knowledge_nodes
                       (knowledge_id, node_id, role, strength)
                       VALUES (?, ?, 'mentions', 0.8)""",
                    (kid, nid),
                )
                linked_nodes.add(nid)
                new_links += 1

        # 3. Match technology keywords
        for tech in tech_patterns:
            if tech in tokens and tech in node_by_name:
                nid, _ = node_by_name[tech]
                if nid not in linked_nodes:
                    db.execute(
                        """INSERT OR IGNORE INTO knowledge_nodes
                           (knowledge_id, node_id, role, strength)
                           VALUES (?, ?, 'uses', 0.9)""",
                        (kid, nid),
                    )
                    linked_nodes.add(nid)
                    new_links += 1

        # 4. Link to project node
        if project and project != "general":
            proj_key = project.lower()
            if proj_key in node_by_name:
                nid, _ = node_by_name[proj_key]
            else:
                nid = _get_or_create_node(db, project, "project")
                node_by_name[proj_key] = (nid, "project")

            if nid not in linked_nodes:
                db.execute(
                    """INSERT OR IGNORE INTO knowledge_nodes
                       (knowledge_id, node_id, role, strength)
                       VALUES (?, ?, 'belongs_to', 1.0)""",
                    (kid, nid),
                )
                linked_nodes.add(nid)
                new_links += 1

        # 5. Link to tag concept nodes
        for tag in tags:
            if not isinstance(tag, str) or len(tag) < 3:
                continue
            tag_key = tag.lower().replace("-", "_").replace(" ", "_")
            if tag_key in node_by_name:
                nid, _ = node_by_name[tag_key]
            else:
                nid = _get_or_create_node(db, tag_key, "concept")
                node_by_name[tag_key] = (nid, "concept")

            if nid not in linked_nodes:
                db.execute(
                    """INSERT OR IGNORE INTO knowledge_nodes
                       (knowledge_id, node_id, role, strength)
                       VALUES (?, ?, 'tagged', 0.8)""",
                    (kid, nid),
                )
                linked_nodes.add(nid)
                new_links += 1

        # 6. Link to knowledge type node (fact, solution, lesson, etc.)
        k_type = orphan["type"]
        if k_type:
            type_key = f"knowledge_type_{k_type}"
            if type_key in node_by_name:
                nid, _ = node_by_name[type_key]
            else:
                nid = _get_or_create_node(
                    db, type_key, "concept",
                    content=f"Knowledge type: {k_type}",
                )
                node_by_name[type_key] = (nid, "concept")

            if nid not in linked_nodes:
                db.execute(
                    """INSERT OR IGNORE INTO knowledge_nodes
                       (knowledge_id, node_id, role, strength)
                       VALUES (?, ?, 'is_type', 0.5)""",
                    (kid, nid),
                )
                linked_nodes.add(nid)
                new_links += 1

    db.commit()
    db.close()
    LOG(f"Linked {new_links} new knowledge->node connections for {len(orphans)} orphans")
    return new_links


# ──────────────────────────────────────────────────────────
# 2. Semantic Edges
# ──────────────────────────────────────────────────────────

def add_semantic_edges(db_path: str) -> int:
    """Create semantic edges between nodes whose knowledge records are related.

    Two strategies:
    A) Same project + overlapping tags -> semantic_similarity edges
    B) Shared technology references -> uses_together edges

    Returns count of new edges created.
    """
    db = _get_db(db_path)
    new_edges = 0

    # ── Strategy A: Project+tag overlap ──
    # Get all active knowledge with their tags, grouped by project
    records = db.execute(
        """SELECT k.id, k.project, k.tags
           FROM knowledge k
           WHERE k.status = 'active'
             AND k.project != 'general'
             AND k.tags != '[]'
             AND k.tags IS NOT NULL"""
    ).fetchall()

    # Group by project
    by_project: dict[str, list[tuple[int, set[str]]]] = defaultdict(list)
    for rec in records:
        try:
            tags = set(json.loads(rec["tags"])) if rec["tags"] else set()
        except (json.JSONDecodeError, TypeError):
            continue
        if tags:
            by_project[rec["project"]].append((rec["id"], tags))

    # For each project, compare records with overlapping tags
    for project, recs in by_project.items():
        if len(recs) < 2:
            continue

        # Get nodes linked to each knowledge record
        kid_to_nodes: dict[int, list[str]] = defaultdict(list)
        kid_list = [r[0] for r in recs]

        # Batch query for efficiency
        placeholders = ",".join("?" * len(kid_list))
        links = db.execute(
            f"""SELECT knowledge_id, node_id FROM knowledge_nodes
                WHERE knowledge_id IN ({placeholders})""",
            kid_list,
        ).fetchall()

        for link in links:
            kid_to_nodes[link["knowledge_id"]].append(link["node_id"])

        # Compare pairs
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                kid_a, tags_a = recs[i]
                kid_b, tags_b = recs[j]
                similarity = _jaccard(tags_a, tags_b)

                if similarity < 0.3:
                    continue

                nodes_a = kid_to_nodes.get(kid_a, [])
                nodes_b = kid_to_nodes.get(kid_b, [])

                if not nodes_a or not nodes_b:
                    continue

                # Create edges between their most important nodes (limit pairs)
                pairs_created = 0
                for na in nodes_a[:3]:
                    for nb in nodes_b[:3]:
                        if na == nb:
                            continue
                        # Canonical order to avoid duplicates
                        src, tgt = (na, nb) if na < nb else (nb, na)
                        weight = round(similarity * 2.0, 2)
                        created = _ensure_edge(
                            db, src, tgt, "semantic_similarity",
                            weight=weight,
                            context=f"shared tags in {project} (jaccard={similarity:.2f})",
                        )
                        if created:
                            new_edges += 1
                            pairs_created += 1
                        if pairs_created >= 5:
                            break
                    if pairs_created >= 5:
                        break

    # ── Strategy B: Shared technology references ──
    # Find knowledge records that reference the same technology nodes
    tech_knowledge = db.execute(
        """SELECT kn.knowledge_id, kn.node_id, gn.name
           FROM knowledge_nodes kn
           JOIN graph_nodes gn ON kn.node_id = gn.id
           WHERE gn.type = 'technology' AND gn.status = 'active'"""
    ).fetchall()

    # Group: technology -> list of knowledge IDs
    tech_to_kids: dict[str, list[int]] = defaultdict(list)
    kid_to_techs: dict[int, set[str]] = defaultdict(set)
    for row in tech_knowledge:
        tech_to_kids[row["node_id"]].append(row["knowledge_id"])
        kid_to_techs[row["knowledge_id"]].add(row["node_id"])

    # Find technology pairs used together in same knowledge records
    tech_pairs_seen: set[tuple[str, str]] = set()
    for kid, tech_nodes in kid_to_techs.items():
        tech_list = sorted(tech_nodes)
        for i in range(len(tech_list)):
            for j in range(i + 1, len(tech_list)):
                pair = (tech_list[i], tech_list[j])
                if pair not in tech_pairs_seen:
                    tech_pairs_seen.add(pair)

    # Count co-occurrences for each tech pair
    for src, tgt in tech_pairs_seen:
        # Count knowledge records that mention both
        count = db.execute(
            """SELECT COUNT(*) FROM knowledge_nodes kn1
               JOIN knowledge_nodes kn2
                 ON kn1.knowledge_id = kn2.knowledge_id
               WHERE kn1.node_id = ? AND kn2.node_id = ?""",
            (src, tgt),
        ).fetchone()[0]

        if count >= 2:
            weight = min(0.5 + count * 0.15, 3.0)
            created = _ensure_edge(
                db, src, tgt, "uses_together",
                weight=weight,
                context=f"co-referenced in {count} knowledge records",
            )
            if created:
                new_edges += 1

    db.commit()
    db.close()
    LOG(f"Created {new_edges} semantic edges")
    return new_edges


# ──────────────────────────────────────────────────────────
# 3. Hierarchical Edges
# ──────────────────────────────────────────────────────────

# Technology hierarchy definition
TECH_HIERARCHY: dict[str, list[tuple[str, str, str]]] = {
    # category_name: [(child_name, child_type, relation)]
    "backend": [
        ("php", "technology", "part_of"),
        ("symfony", "technology", "part_of"),
        ("go", "technology", "part_of"),
        ("golang", "technology", "part_of"),
        ("python", "technology", "part_of"),
        ("fastapi", "technology", "part_of"),
        ("django", "technology", "part_of"),
        ("flask", "technology", "part_of"),
        ("grpc", "technology", "part_of"),
        ("protobuf", "technology", "part_of"),
    ],
    "frontend": [
        ("vue", "technology", "part_of"),
        ("nuxt", "technology", "part_of"),
        ("typescript", "technology", "part_of"),
        ("javascript", "technology", "part_of"),
        ("tailwind", "technology", "part_of"),
        ("pinia", "technology", "part_of"),
        ("eslint", "technology", "part_of"),
    ],
    "database": [
        ("postgresql", "technology", "part_of"),
        ("postgres", "technology", "part_of"),
        ("sqlite", "technology", "part_of"),
        ("redis", "technology", "part_of"),
        ("chromadb", "technology", "part_of"),
    ],
    "devops": [
        ("docker", "technology", "part_of"),
        ("makefile", "technology", "part_of"),
        ("nginx", "technology", "part_of"),
        ("git", "technology", "part_of"),
    ],
    "messaging": [
        ("rabbitmq", "technology", "part_of"),
        ("grpc", "technology", "part_of"),
    ],
    "testing": [
        ("phpunit", "technology", "part_of"),
        ("phpstan", "technology", "part_of"),
    ],
    "monitoring": [
        ("prometheus", "technology", "part_of"),
        ("grafana", "technology", "part_of"),
        ("sentry", "technology", "part_of"),
    ],
    "ai_ml": [
        ("ollama", "technology", "part_of"),
        ("chromadb", "technology", "part_of"),
    ],
    "crm": [
        ("bitrix", "technology", "part_of"),
        ("bitrix24", "technology", "part_of"),
    ],
}

# Framework dependency relationships
TECH_DEPENDENCIES: list[tuple[str, str]] = [
    ("symfony", "php"),
    ("phpstan", "php"),
    ("phpunit", "php"),
    ("nuxt", "vue"),
    ("pinia", "vue"),
    ("vue", "typescript"),
    ("vue", "javascript"),
    ("tailwind", "css"),
    ("fastapi", "python"),
    ("django", "python"),
    ("flask", "python"),
    ("sqlalchemy", "python"),
    ("pydantic", "python"),
    ("celery", "python"),
    ("bitrix24", "bitrix"),
    ("protobuf", "grpc"),
    ("eslint", "javascript"),
    ("chromadb", "python"),
]


def add_hierarchy_edges(db_path: str) -> int:
    """Create hierarchical category edges and dependency edges for technologies.

    1. Creates category nodes (backend, frontend, database, etc.) if missing
    2. Links technology nodes to their categories via part_of
    3. Creates depends_on edges between frameworks and their languages

    Returns count of new edges created.
    """
    db = _get_db(db_path)
    new_edges = 0

    # Create category -> technology part_of edges
    for category, children in TECH_HIERARCHY.items():
        cat_id = _get_or_create_node(
            db, category, "concept",
            content=f"Technology category: {category}",
        )

        for child_name, child_type, relation in children:
            # Only create edge if child node exists
            child = db.execute(
                "SELECT id FROM graph_nodes WHERE name = ? AND status = 'active'",
                (child_name,),
            ).fetchone()

            if child:
                created = _ensure_edge(
                    db, child["id"], cat_id, relation,
                    weight=2.0,
                    context=f"technology hierarchy: {child_name} is part of {category}",
                )
                if created:
                    new_edges += 1

    # Create framework -> language depends_on edges
    for framework, language in TECH_DEPENDENCIES:
        fw_node = db.execute(
            "SELECT id FROM graph_nodes WHERE name = ? AND status = 'active'",
            (framework,),
        ).fetchone()
        lang_node = db.execute(
            "SELECT id FROM graph_nodes WHERE name = ? AND status = 'active'",
            (language,),
        ).fetchone()

        if fw_node and lang_node:
            created = _ensure_edge(
                db, fw_node["id"], lang_node["id"], "depends_on",
                weight=3.0,
                context=f"{framework} depends on {language}",
            )
            if created:
                new_edges += 1

    db.commit()
    db.close()
    LOG(f"Created {new_edges} hierarchy edges")
    return new_edges


# ──────────────────────────────────────────────────────────
# 4. Cross-Project Edges
# ──────────────────────────────────────────────────────────

def add_cross_project_edges(db_path: str) -> int:
    """Find concepts shared across projects and create shared_across edges.

    Strategy:
    1. For each concept node, find which projects reference it
    2. If a concept appears in 2+ projects, create edges between project nodes
    3. Weight edges by the number of shared concepts

    Returns count of new edges created.
    """
    db = _get_db(db_path)
    new_edges = 0

    # Find concept -> project mappings via knowledge records
    concept_projects = db.execute(
        """SELECT gn.id AS node_id, gn.name AS node_name,
                  k.project, COUNT(*) AS cnt
           FROM knowledge_nodes kn
           JOIN graph_nodes gn ON kn.node_id = gn.id
           JOIN knowledge k ON kn.knowledge_id = k.id
           WHERE gn.type IN ('concept', 'technology', 'rule', 'convention')
             AND gn.status = 'active'
             AND k.status = 'active'
             AND k.project != 'general'
             AND k.project != ''
           GROUP BY gn.id, k.project
           HAVING cnt >= 1
           ORDER BY gn.id, cnt DESC"""
    ).fetchall()

    # Build: node_id -> set of projects
    node_to_projects: dict[str, set[str]] = defaultdict(set)
    for row in concept_projects:
        node_to_projects[row["node_id"]].add(row["project"])

    # Find project pairs that share concepts
    project_shared_count: dict[tuple[str, str], int] = defaultdict(int)
    project_shared_concepts: dict[tuple[str, str], list[str]] = defaultdict(list)

    for node_id, projects in node_to_projects.items():
        if len(projects) < 2:
            continue
        proj_list = sorted(projects)
        for i in range(len(proj_list)):
            for j in range(i + 1, len(proj_list)):
                pair = (proj_list[i], proj_list[j])
                project_shared_count[pair] += 1
                if len(project_shared_concepts[pair]) < 10:
                    # Get node name for context
                    name_row = db.execute(
                        "SELECT name FROM graph_nodes WHERE id = ?",
                        (node_id,),
                    ).fetchone()
                    if name_row:
                        project_shared_concepts[pair].append(name_row["name"])

    # Create shared_across edges between project nodes
    for (proj_a, proj_b), shared_count in project_shared_count.items():
        if shared_count < 2:
            continue

        # Get or create project nodes
        proj_a_id = _get_or_create_node(db, proj_a, "project")
        proj_b_id = _get_or_create_node(db, proj_b, "project")

        concepts = project_shared_concepts.get((proj_a, proj_b), [])
        concepts_str = ", ".join(concepts[:5])
        weight = min(1.0 + shared_count * 0.2, 5.0)

        created = _ensure_edge(
            db, proj_a_id, proj_b_id, "shared_across",
            weight=weight,
            context=f"{shared_count} shared concepts: {concepts_str}",
        )
        if created:
            new_edges += 1

    db.commit()
    db.close()
    LOG(f"Created {new_edges} cross-project edges")
    return new_edges


# ──────────────────────────────────────────────────────────
# 5. Temporal Edges
# ──────────────────────────────────────────────────────────

def add_temporal_edges(db_path: str) -> int:
    """Create temporal edges based on session co-occurrence and error->fix pairs.

    1. co_occurred: nodes from knowledge records in the same session
    2. solves: solution-type records linked to error-type records in same session

    Returns count of new edges created.
    """
    db = _get_db(db_path)
    new_edges = 0

    # ── Strategy 1: Session co-occurrence ──
    # Find sessions with multiple knowledge records that have graph links
    sessions = db.execute(
        """SELECT session_id, COUNT(*) AS cnt
           FROM knowledge
           WHERE status = 'active'
             AND session_id IS NOT NULL
             AND session_id != ''
           GROUP BY session_id
           HAVING cnt >= 2 AND cnt <= 50
           ORDER BY cnt DESC"""
    ).fetchall()

    LOG(f"Processing {len(sessions)} sessions for temporal edges...")

    for session in sessions:
        sid = session["session_id"]

        # Get all nodes linked to knowledge in this session
        session_nodes = db.execute(
            """SELECT DISTINCT kn.node_id, gn.type AS node_type
               FROM knowledge k
               JOIN knowledge_nodes kn ON k.id = kn.knowledge_id
               JOIN graph_nodes gn ON kn.node_id = gn.id
               WHERE k.session_id = ?
                 AND k.status = 'active'
                 AND gn.status = 'active'
                 AND gn.type IN ('concept', 'technology', 'rule')""",
            (sid,),
        ).fetchall()

        node_ids = [r["node_id"] for r in session_nodes]

        if len(node_ids) < 2 or len(node_ids) > 30:
            continue

        # Create co_occurred edges between significant node pairs
        # Limit to avoid quadratic explosion
        pairs_created = 0
        max_pairs = min(len(node_ids) * 2, 20)

        for i in range(len(node_ids)):
            if pairs_created >= max_pairs:
                break
            for j in range(i + 1, len(node_ids)):
                if pairs_created >= max_pairs:
                    break
                src, tgt = node_ids[i], node_ids[j]
                if src == tgt:
                    continue
                # Canonical order
                if src > tgt:
                    src, tgt = tgt, src

                created = _ensure_edge(
                    db, src, tgt, "co_occurred",
                    weight=0.5,
                    context=f"same session: {sid[:20]}",
                )
                if created:
                    new_edges += 1
                    pairs_created += 1

    # ── Strategy 2: Solution -> Error links ──
    # Find solution records and link their nodes to nearby error-related nodes
    solutions = db.execute(
        """SELECT k.id, k.session_id, k.content, k.project
           FROM knowledge k
           WHERE k.type = 'solution'
             AND k.status = 'active'
             AND k.session_id IS NOT NULL"""
    ).fetchall()

    for sol in solutions:
        sol_nodes = db.execute(
            """SELECT kn.node_id FROM knowledge_nodes kn
               WHERE kn.knowledge_id = ?""",
            (sol["id"],),
        ).fetchall()

        if not sol_nodes:
            continue

        # Find error/lesson records in the same session
        related = db.execute(
            """SELECT k.id FROM knowledge k
               WHERE k.session_id = ?
                 AND k.type IN ('lesson', 'fact')
                 AND k.status = 'active'
                 AND k.id != ?
                 AND (k.content LIKE '%error%' OR k.content LIKE '%fix%'
                      OR k.content LIKE '%bug%' OR k.content LIKE '%issue%'
                      OR k.content LIKE '%problem%' OR k.content LIKE '%ошибк%')""",
            (sol["session_id"], sol["id"]),
        ).fetchall()

        for rel in related:
            rel_nodes = db.execute(
                """SELECT kn.node_id FROM knowledge_nodes kn
                   WHERE kn.knowledge_id = ?""",
                (rel["id"],),
            ).fetchall()

            # Link solution nodes to error-related nodes
            pairs_created = 0
            for sn in sol_nodes[:3]:
                for rn in rel_nodes[:3]:
                    if sn["node_id"] == rn["node_id"]:
                        continue
                    created = _ensure_edge(
                        db, sn["node_id"], rn["node_id"], "solves",
                        weight=1.5,
                        context=f"solution in session {sol['session_id'][:20]}",
                    )
                    if created:
                        new_edges += 1
                        pairs_created += 1
                    if pairs_created >= 5:
                        break
                if pairs_created >= 5:
                    break

    db.commit()
    db.close()
    LOG(f"Created {new_edges} temporal edges")
    return new_edges


# ──────────────────────────────────────────────────────────
# 6. Main Entry Point
# ──────────────────────────────────────────────────────────

def get_graph_stats(db_path: str) -> dict[str, Any]:
    """Get comprehensive graph statistics."""
    db = _get_db(db_path)

    total_nodes = db.execute(
        "SELECT COUNT(*) FROM graph_nodes WHERE status = 'active'"
    ).fetchone()[0]

    total_edges = db.execute(
        "SELECT COUNT(*) FROM graph_edges"
    ).fetchone()[0]

    # Density: edges / (nodes * (nodes-1) / 2) for undirected
    max_edges = total_nodes * (total_nodes - 1) / 2 if total_nodes > 1 else 1
    density = round(total_edges / max_edges * 100, 4)

    nodes_by_type: dict[str, int] = {}
    for row in db.execute(
        """SELECT type, COUNT(*) AS cnt
           FROM graph_nodes WHERE status = 'active'
           GROUP BY type ORDER BY cnt DESC"""
    ).fetchall():
        nodes_by_type[row["type"]] = row["cnt"]

    edges_by_type: dict[str, int] = {}
    for row in db.execute(
        """SELECT relation_type, COUNT(*) AS cnt
           FROM graph_edges GROUP BY relation_type ORDER BY cnt DESC"""
    ).fetchall():
        edges_by_type[row["relation_type"]] = row["cnt"]

    total_knowledge = db.execute(
        "SELECT COUNT(*) FROM knowledge WHERE status = 'active'"
    ).fetchone()[0]

    linked_knowledge = db.execute(
        """SELECT COUNT(DISTINCT knowledge_id) FROM knowledge_nodes
           WHERE knowledge_id IN (SELECT id FROM knowledge WHERE status = 'active')"""
    ).fetchone()[0]

    unlinked = total_knowledge - linked_knowledge

    # Orphan nodes (no edges at all)
    orphan_nodes = db.execute(
        """SELECT COUNT(*) FROM graph_nodes
           WHERE status = 'active'
             AND id NOT IN (SELECT source_id FROM graph_edges)
             AND id NOT IN (SELECT target_id FROM graph_edges)"""
    ).fetchone()[0]

    avg_edges_per_node = round(total_edges * 2 / total_nodes, 2) if total_nodes > 0 else 0

    # Edge weight distribution
    weight_dist = db.execute(
        """SELECT
             COUNT(CASE WHEN weight < 0.5 THEN 1 END) AS weak,
             COUNT(CASE WHEN weight >= 0.5 AND weight < 1.5 THEN 1 END) AS medium,
             COUNT(CASE WHEN weight >= 1.5 AND weight < 3.0 THEN 1 END) AS strong,
             COUNT(CASE WHEN weight >= 3.0 THEN 1 END) AS very_strong
           FROM graph_edges"""
    ).fetchone()

    db.close()

    return {
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "density_pct": density,
        "avg_edges_per_node": avg_edges_per_node,
        "nodes_by_type": nodes_by_type,
        "edges_by_type": edges_by_type,
        "total_knowledge": total_knowledge,
        "linked_knowledge": linked_knowledge,
        "unlinked_knowledge": unlinked,
        "unlinked_pct": round(unlinked / total_knowledge * 100, 1) if total_knowledge > 0 else 0,
        "orphan_nodes": orphan_nodes,
        "edge_weight_distribution": {
            "weak (<0.5)": weight_dist["weak"],
            "medium (0.5-1.5)": weight_dist["medium"],
            "strong (1.5-3.0)": weight_dist["strong"],
            "very_strong (3.0+)": weight_dist["very_strong"],
        },
    }


def run_deep_enrichment(db_path: str) -> dict[str, Any]:
    """Full graph enrichment pipeline. Returns stats dict.

    Runs all five enrichment strategies in order:
    1. Link orphan knowledge records to graph nodes
    2. Add semantic similarity and uses_together edges
    3. Add technology hierarchy edges
    4. Add cross-project shared_across edges
    5. Add temporal co_occurred and solves edges

    After all enrichment, recomputes PageRank for updated importance scores.
    """
    LOG("=" * 60)
    LOG("Starting deep graph enrichment...")
    LOG("=" * 60)

    stats_before = get_graph_stats(db_path)
    LOG(f"BEFORE: {stats_before['total_nodes']} nodes, {stats_before['total_edges']} edges, "
        f"{stats_before['density_pct']}% density, {stats_before['unlinked_knowledge']} unlinked knowledge")

    stats: dict[str, Any] = {}

    # Phase 1: Link orphans first (other phases depend on links existing)
    LOG("\n--- Phase 1: Linking orphan knowledge records ---")
    stats["orphans_linked"] = link_orphan_records(db_path)

    # Phase 2: Semantic edges (needs knowledge links)
    LOG("\n--- Phase 2: Adding semantic edges ---")
    stats["semantic_edges"] = add_semantic_edges(db_path)

    # Phase 3: Hierarchy edges (independent)
    LOG("\n--- Phase 3: Adding hierarchy edges ---")
    stats["hierarchy_edges"] = add_hierarchy_edges(db_path)

    # Phase 4: Cross-project edges (needs knowledge links)
    LOG("\n--- Phase 4: Adding cross-project edges ---")
    stats["cross_project_edges"] = add_cross_project_edges(db_path)

    # Phase 5: Temporal edges (needs knowledge links)
    LOG("\n--- Phase 5: Adding temporal edges ---")
    stats["temporal_edges"] = add_temporal_edges(db_path)

    # Phase 6: Recompute PageRank with enriched graph
    LOG("\n--- Phase 6: Recomputing PageRank ---")
    try:
        db = _get_db(db_path)
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from graph.enricher import GraphEnricher
        enricher = GraphEnricher(db)
        scores = enricher.compute_pagerank()
        stats["pagerank_nodes"] = len(scores)
        db.close()
    except Exception as e:
        LOG(f"PageRank computation failed: {e}")
        stats["pagerank_nodes"] = 0

    stats_after = get_graph_stats(db_path)
    stats["before"] = {
        "nodes": stats_before["total_nodes"],
        "edges": stats_before["total_edges"],
        "density_pct": stats_before["density_pct"],
        "unlinked_knowledge": stats_before["unlinked_knowledge"],
    }
    stats["after"] = {
        "nodes": stats_after["total_nodes"],
        "edges": stats_after["total_edges"],
        "density_pct": stats_after["density_pct"],
        "unlinked_knowledge": stats_after["unlinked_knowledge"],
    }

    LOG("\n" + "=" * 60)
    LOG(f"AFTER: {stats_after['total_nodes']} nodes, {stats_after['total_edges']} edges, "
        f"{stats_after['density_pct']}% density, {stats_after['unlinked_knowledge']} unlinked knowledge")
    LOG(f"Improvement: +{stats_after['total_edges'] - stats_before['total_edges']} edges, "
        f"density {stats_before['density_pct']}% -> {stats_after['density_pct']}%")
    LOG("=" * 60)

    return stats


# ──────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point for graph enrichment."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Deep graph enrichment for knowledge graph",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help="Path to SQLite database",
    )
    parser.add_argument("--stats", action="store_true", help="Show current graph stats")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--orphans", action="store_true", help="Only link orphan records")
    parser.add_argument("--semantic", action="store_true", help="Only add semantic edges")
    parser.add_argument("--hierarchy", action="store_true", help="Only add hierarchy edges")
    parser.add_argument("--cross", action="store_true", help="Only add cross-project edges")
    parser.add_argument("--temporal", action="store_true", help="Only add temporal edges")
    args = parser.parse_args()

    db_path = args.db

    if args.stats:
        result = get_graph_stats(db_path)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.dry_run:
        print("DRY RUN: showing current stats only")
        result = get_graph_stats(db_path)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # Run specific enrichment or all
    specific = args.orphans or args.semantic or args.hierarchy or args.cross or args.temporal

    if specific:
        result: dict[str, Any] = {}
        if args.orphans:
            result["orphans_linked"] = link_orphan_records(db_path)
        if args.semantic:
            result["semantic_edges"] = add_semantic_edges(db_path)
        if args.hierarchy:
            result["hierarchy_edges"] = add_hierarchy_edges(db_path)
        if args.cross:
            result["cross_project_edges"] = add_cross_project_edges(db_path)
        if args.temporal:
            result["temporal_edges"] = add_temporal_edges(db_path)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        result = run_deep_enrichment(db_path)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
