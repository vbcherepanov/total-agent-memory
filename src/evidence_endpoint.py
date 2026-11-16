from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Protocol, TypedDict

from config import get_recall_excluded_tags
from memory_core.embeddings import EmbeddingProvider
from memory_core.evidence_search import EvidenceSearch
from memory_core.evidence_window import ANSWER_GUIDANCE
from memory_core.grounding import MissingRelation
from memory_core.passage_index import PassageIndex
from memory_core.retrieval import MemoryHit, SearchScope, flatten_results
from memory_core.incidents import IncidentFilter, IncidentService


class MissingRelationOptions(TypedDict):
    subject: str
    relation: str
    time: str


class EvidenceOptions(TypedDict, total=False):
    query: str
    as_of: str
    project: str
    type: str
    branch: str
    limit: int
    context_max_chars: int
    evidence_followup: bool
    missing_relation: MissingRelationOptions
    topics: list[str]
    entities: list[str]
    intent: str
    decisions_only: bool


class SearchEnvelope(TypedDict):
    results: dict[str, list[MemoryHit]]


class EvidenceStore(Protocol):
    db: sqlite3.Connection
    evidence_embedder: EmbeddingProvider


class EvidenceRecall(Protocol):
    def search(self, query: str, project: str | None = None, ktype: str = 'all',
               limit: int = 10, detail: str = 'full', branch: str | None = None,
               *, record_usage: bool = True,
               as_of: datetime | str | None = None) -> SearchEnvelope: ...


class FollowupDetails(TypedDict):
    query: str
    missing_terms: list[str]
    reason: str


class EvidenceResponse(TypedDict):
    query: str
    mode: str
    results: list[MemoryHit]
    total: int
    context: str
    answer_guidance: str
    followup: FollowupDetails | None
    search_calls: int


class IndexOptions(TypedDict, total=False):
    project: str
    limit: int
    after_id: int


class IndexResponse(TypedDict):
    processed: int
    next_after_id: int
    remaining: int
    model: str


def index_passages(store: EvidenceStore, options: IndexOptions) -> IndexResponse:
    project = options.get('project', '')
    limit = options.get('limit', 20)
    after_id = options.get('after_id', 0)
    if not project.strip() or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Passage indexing requires a project and a limit between 1 and 100')
    if type(after_id) is not int or after_id < 0:
        raise ValueError('Index cursor must be a non-negative integer')
    rows = store.db.execute("SELECT * FROM knowledge WHERE project=? AND status='active' AND id>? ORDER BY id LIMIT ?",
                           (project, after_id, limit)).fetchall()
    model = store.evidence_embedder.active_model()
    PassageIndex(store.db, store.evidence_embedder.embed_texts, model).ensure([dict(row) for row in rows])
    cursor = rows[-1]['id'] if rows else after_id
    remaining = store.db.execute("SELECT count(*) FROM knowledge WHERE project=? AND status='active' AND id>?", (project, cursor)).fetchone()[0]
    return {'processed': len(rows), 'next_after_id': cursor, 'remaining': remaining, 'model': model}


def evidence_response(store: EvidenceStore, recall: EvidenceRecall, options: EvidenceOptions,
                      initial: SearchEnvelope) -> EvidenceResponse:
    scope = SearchScope(options.get('project'), options.get('type', 'all'), options.get('branch'),
                        as_of=options.get('as_of') or None,
                        incident=IncidentFilter.model_validate(options['incident']) if 'incident' in options else None)
    def search(query: str, limit: int) -> list[MemoryHit]:
        if scope.incident is not None:
            return flatten_results(IncidentService(store.db).search(
                query, scope, limit, postgres=getattr(store, 'is_postgres', False)))
        if scope.as_of is not None:
            return flatten_results(recall.search(query, project=scope.project, ktype=scope.kind,
                limit=limit, detail='full', branch=scope.branch, record_usage=False, as_of=scope.as_of))
        return flatten_results(recall.search(query, project=scope.project, ktype=scope.kind,
            limit=limit, detail='full', branch=scope.branch, record_usage=False))
    filtered = any(options.get(key) for key in ('topics', 'entities', 'intent', 'decisions_only'))
    service = EvidenceSearch(store.db, search, store.evidence_embedder.embed_texts,
        store.evidence_embedder.active_model(), get_recall_excluded_tags(),
        query_embed=store.evidence_embedder.embed_query)
    missing = options.get('missing_relation')
    result = service.run(options['query'], scope, limit=options.get('limit', 10),
        max_bytes=options.get('context_max_chars', 24000),
        followup=options.get('evidence_followup', True) and not filtered,
        initial=flatten_results(initial),
        missing_relation=MissingRelation(missing.get('subject', ''), missing.get('relation', ''), missing.get('time', '')) if missing else None)
    plan = result.followup
    return {'query': options['query'], 'mode': 'evidence', 'results': result.evidence,
        'total': len(result.evidence), 'context': result.context, 'answer_guidance': ANSWER_GUIDANCE,
        'followup': {'query': plan.query, 'missing_terms': list(plan.missing_terms), 'reason': plan.reason} if plan else None,
        'search_calls': result.search_calls}
