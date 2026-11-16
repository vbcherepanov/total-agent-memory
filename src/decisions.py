"""Structured decision schema (v8.0).

Stores architectural decisions with the Creative-phase schema from
cursor-memory-bank: options + criteria matrix + rationale + discarded. The
record lands in `knowledge` as type="decision" so it flows through the
normal recall pipeline (BM25 + semantic + RRF), but:

  * `knowledge.content` is a BM25-searchable human-readable template
  * `knowledge.context`  is a JSON blob with the full structured schema
  * one `knowledge_representations` row per criterion is added so callers
    can recall "what criteria were used when choosing X"

Backward compatible with `store.save_knowledge(type="decision", ...)`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any

LOG = lambda msg: sys.stderr.write(f"[decisions] {msg}\n")


SCHEMA_VERSION = "decision/v1"
STRUCTURED_TAG = "structured"


@dataclass
class DecisionOption:
    """One option considered during a creative/design phase."""

    name: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pros": list(self.pros),
            "cons": list(self.cons),
            "unknowns": list(self.unknowns),
        }


@dataclass
class Decision:
    """Fully-structured architectural decision.

    criteria_matrix: criterion -> {option_name: rating 0-5}
    selected      : option name chosen (must be in `options`)
    discarded     : option names explicitly rejected (subset of
                    options - {selected})
    """

    title: str
    options: list[DecisionOption]
    criteria_matrix: dict[str, dict[str, float]]
    selected: str
    rationale: str
    discarded: list[str] = field(default_factory=list)
    project: str | None = None
    tags: list[str] | None = None

    # ── Validation ───────────────────────────────────────────

    def __post_init__(self) -> None:
        if not self.title or not self.title.strip():
            raise ValueError("Decision.title is required")
        if not self.options:
            raise ValueError("Decision.options must not be empty")
        if not self.selected:
            raise ValueError("Decision.selected is required")
        if not self.rationale or not self.rationale.strip():
            raise ValueError("Decision.rationale is required")

        option_names = {o.name for o in self.options}
        if len(option_names) != len(self.options):
            raise ValueError("Decision.options have duplicate names")
        if self.selected not in option_names:
            raise ValueError(
                f"Decision.selected={self.selected!r} is not among "
                f"options={sorted(option_names)}"
            )
        discarded_set = set(self.discarded)
        if self.selected in discarded_set:
            raise ValueError(
                f"Decision.discarded must not contain the selected "
                f"option ({self.selected!r})"
            )
        unknown = discarded_set - option_names
        if unknown:
            raise ValueError(
                f"Decision.discarded contains unknown options: "
                f"{sorted(unknown)}"
            )

        # Criteria matrix values must be numeric 0-5; every inner dict
        # may reference only known option names.
        for crit, ratings in self.criteria_matrix.items():
            if not isinstance(ratings, dict):
                raise ValueError(
                    f"criteria_matrix[{crit!r}] must be dict, "
                    f"got {type(ratings).__name__}"
                )
            for opt_name, rating in ratings.items():
                if opt_name not in option_names:
                    raise ValueError(
                        f"criteria_matrix[{crit!r}] references unknown "
                        f"option {opt_name!r}"
                    )
                try:
                    r = float(rating)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"criteria_matrix[{crit!r}][{opt_name!r}] must be "
                        f"numeric, got {rating!r}"
                    ) from exc
                if r < 0 or r > 5:
                    raise ValueError(
                        f"criteria_matrix[{crit!r}][{opt_name!r}]={r} "
                        f"outside 0..5"
                    )

    # ── Serialization ────────────────────────────────────────

    def to_context_json(self) -> str:
        """Return canonical JSON blob stored in knowledge.context."""
        payload = {
            "schema": SCHEMA_VERSION,
            "title": self.title,
            "options": [o.to_dict() for o in self.options],
            "criteria_matrix": {
                crit: {k: float(v) for k, v in ratings.items()}
                for crit, ratings in self.criteria_matrix.items()
            },
            "selected": self.selected,
            "rationale": self.rationale,
            "discarded": list(self.discarded),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def to_content(self) -> str:
        """Render BM25-searchable human-readable content."""
        lines: list[str] = [f"DECISION: {self.title}", "", f"SELECTED: {self.selected}", ""]
        lines.append("OPTIONS CONSIDERED:")
        for opt in self.options:
            pros = ", ".join(opt.pros) if opt.pros else "-"
            cons = ", ".join(opt.cons) if opt.cons else "-"
            lines.append(f"- {opt.name}: pros={pros}; cons={cons}")
            if opt.unknowns:
                lines.append(f"  unknowns: {', '.join(opt.unknowns)}")
        lines.append("")
        lines.append(f"RATIONALE: {self.rationale}")
        if self.discarded:
            lines.append("")
            lines.append(f"DISCARDED: {', '.join(self.discarded)}")
        return "\n".join(lines)

    def criterion_repr_content(self) -> str:
        """Content for the `criterion` multi-repr row.

        One line per criterion: `criterion_name: selected_rating`. Missing
        ratings are logged as '?'. This gives recall parity on queries
        like "what criteria were used when choosing X".
        """
        if not self.criteria_matrix:
            return ""
        lines: list[str] = []
        for crit, ratings in self.criteria_matrix.items():
            rating = ratings.get(self.selected)
            if rating is None:
                rendered = "?"
            else:
                rendered = f"{float(rating):g}"
            lines.append(f"{crit}: {rendered}")
        return "\n".join(lines)

    def summary_repr_content(self) -> str:
        """Content for the `summary` multi-repr row (selected + rationale)."""
        return f"Selected {self.selected}. Rationale: {self.rationale}"


# ─────────────────────────────────────────────────────────────
# Save integration
# ─────────────────────────────────────────────────────────────


def save_decision(store: Any, decision: Decision, session_id: str | None = None) -> int:
    """Persist a `Decision` via the shared knowledge pipeline.

    Flow:
      1. Insert into `knowledge` with type="decision" + JSON context.
      2. Ensure the "structured" tag is present.
      3. Add `summary` and `criterion` multi-repr rows (best-effort; skipped
         if the embedder is unavailable, e.g. in fully-offline tests).

    Returns the knowledge record id.
    """
    sid = session_id
    if sid is None:
        # Fall back to the server's module-level SID when invoked from MCP.
        try:
            import server  # type: ignore

            sid = getattr(server, "SID", None)
        except Exception:  # noqa: BLE001
            sid = None
    if not sid:
        raise RuntimeError("save_decision requires an active session id")

    # Compose tags — always include STRUCTURED_TAG.
    tags = list(decision.tags or [])
    if STRUCTURED_TAG not in tags:
        tags.append(STRUCTURED_TAG)

    content = decision.to_content()
    context_json = decision.to_context_json()
    project = decision.project or "general"

    rid, _was_dedup, _was_red, _priv, _qm = store.save_knowledge(
        sid,
        content,
        "decision",
        project=project,
        tags=tags,
        context=context_json,
        skip_dedup=True,
    )

    _add_multi_repr_views(store, rid, decision)
    return int(rid)


def _add_multi_repr_views(store: Any, knowledge_id: int, decision: Decision) -> None:
    """Best-effort: upsert `summary` and `criterion` representations.

    Silently skipped when the embedder is offline (embed() returns None)
    — the knowledge row still lives in FTS5 and gets picked up by the
    async representations worker later.
    """
    try:
        from multi_repr_store import MultiReprStore  # type: ignore
    except ImportError:  # pragma: no cover - package import fallback
        from .multi_repr_store import MultiReprStore  # type: ignore[no-redef]

    embedder = getattr(store, "embed", None)
    if not callable(embedder):
        return

    repr_store = MultiReprStore(store.db)

    texts: list[tuple[str, str]] = []
    summary_text = decision.summary_repr_content()
    if summary_text:
        texts.append(("summary", summary_text))
    criterion_text = decision.criterion_repr_content()
    if criterion_text:
        texts.append(("criterion", criterion_text))

    for repr_type, text in texts:
        try:
            embeddings = store.embed([text])
        except Exception as exc:  # noqa: BLE001
            LOG(f"embed() failed for {repr_type} on kid={knowledge_id}: {exc}")
            continue
        if not embeddings or not embeddings[0]:
            # Embedder unavailable — skip silently.
            continue
        try:
            identity = store.last_embed_identity()
            if identity is None:
                raise ValueError("Decision embedding identity is unavailable")
            repr_store.upsert(knowledge_id, repr_type, text, embeddings[0], identity.model,
                              identity=identity)
        except Exception as exc:  # noqa: BLE001
            LOG(f"multi_repr upsert failed for {repr_type} on kid={knowledge_id}: {exc}")


# ─────────────────────────────────────────────────────────────
# Recall helper
# ─────────────────────────────────────────────────────────────


def parse_stored_decision(context: str) -> dict[str, Any] | None:
    """Parse a knowledge.context blob back into the structured payload.

    Returns None when the blob is not a structured decision (schema
    mismatch or invalid JSON) — callers fall back to treating it as
    legacy free-form text.
    """
    if not context:
        return None
    try:
        data = json.loads(context)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != SCHEMA_VERSION:
        return None
    return data
