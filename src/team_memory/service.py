import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Callable

from pydantic import JsonValue, ValidationError

from memory_reports.llm_summary import ReportSummarizer
from memory_core.query_shapes import is_historical_value_query
from recall_output import INSTRUCTION_NOTICE
from secret_redaction import redact_value
from team_memory.contracts import (
    Actor,
    Browse,
    Delete,
    DomainError,
    Empty,
    History,
    RecordRequest,
    Save,
    Search,
    Update,
    Work,
)
from team_memory.gateway_llm import GatewayLLM
from team_memory.learning.service import LearningService
from team_memory.learning.sources import Caller
from team_memory.learning.tools import LEARNING_TOOLS
from team_memory.registry import Registry
from team_memory.reports.service import TeamReportService
from team_memory.reports.tools import REPORT_TOOLS
from team_memory.rerank import CONTEXT_FIELD, GatewayReranker
from team_memory.settings import SettingsStore, load_cipher
from team_memory.worker import WorkerPool

LOGGER = logging.getLogger(__name__)
LATENCY_BUCKETS = (0.1, 0.5, 1, 5, 30, 120)
MEMORY_TOOLS = {
    "memory_scopes": (Empty, "List your personal, team and shared workspaces."),
    "memory_save": (Save, "Save memory. Default scope is personal; author comes from your token."),
    "memory_recall": (Search, "Search all accessible workspaces, or one selected scope."),
    "memory_get": (RecordRequest, "Read an exact record with author and revision."),
    "memory_update": (Update, "Replace an exact record with revision checking; returns its new ID."),
    "memory_delete": (Delete, "Soft-delete an exact record with revision checking."),
    "memory_history": (History, "Page through this record and its predecessors; after is the last sequence."),
    "memory_export": (Browse, "Page through workspace records with authorship; pass last ID as after."),
}
TOOLS = {**MEMORY_TOOLS, **LEARNING_TOOLS, **REPORT_TOOLS}
WRITES = frozenset(("memory_save", "memory_update", "memory_delete"))
READ_ONLY_TOOLS = frozenset(("memory_scopes", "memory_recall", "memory_get", "memory_history", "memory_export",
                           "memory_report", "onboarding_overview", "onboarding_quiz", "onboarding_team_report",
                           "onboarding_curriculum_get"))



def _with_notice(merged: dict) -> dict:
    """Workers flag agent-directed records; the merged answer says once what the flag means."""
    if any(item["record"].get("untrusted_instructions") for item in merged["results"]):
        merged["notice"] = INSTRUCTION_NOTICE
    return merged


def _order_values(items: list[dict], query: str) -> list[dict]:
    from memory_core.cross_rerank import order_scoped_value_updates

    return order_scoped_value_updates(
        items, lambda item: item["record"].get("content", ""),
        lambda item: (item["record"].get("created_at") or "", item["record"].get("id", 0)),
        lambda item: (item["scope"]["kind"], item["scope"].get("team_id"), item["record"].get("project")),
        older_first=is_historical_value_query(query),
    )

class MemoryService:
    def __init__(self, registry: Registry, pool: WorkerPool, learning: LearningService | None = None,
                 reports: TeamReportService | None = None, llm: GatewayLLM | None = None,
                 reranker: GatewayReranker | None = None):
        self.registry, self.pool = registry, pool
        if learning is None or reports is None:
            # Onboarding and reports run in the gateway: they resolve dashboard provider settings per call.
            llm = llm or GatewayLLM(SettingsStore(registry, load_cipher(registry.root)))
        self.learning = learning or LearningService.default(registry, pool, llm)
        self.reports = reports or TeamReportService(registry, ReportSummarizer(llm))
        self.reranker = reranker or GatewayReranker()
        self.counts = Counter()
        self.histogram = Counter()

    async def call(self, credential: str | Callable[[], Actor], name: str,
                   arguments: dict[str, JsonValue], *, readonly: bool = False) -> JsonValue:
        started = time.monotonic()
        status = "ok"
        try:
            if readonly and name not in READ_ONLY_TOOLS:
                from team_memory.contracts import ReadOnly
                self._authenticate(credential)
                raise ReadOnly("This MCP endpoint permits read-only tools")
            return await self._execute(credential, name, arguments, readonly=readonly)
        except (DomainError, ValidationError):
            status = "rejected"
            raise
        except Exception:
            status = "error"
            LOGGER.exception('gateway_call_failed')
            raise
        finally:
            elapsed = time.monotonic() - started
            operation = name if name in TOOLS else "unknown"
            self.counts[(operation, status)] += 1
            for bound in LATENCY_BUCKETS:
                if elapsed <= bound:
                    self.histogram[(operation, str(bound))] += 1
            self.histogram[(operation, "+Inf")] += 1
            LOGGER.info(json.dumps({"event": "memory_call", "tool": operation,
                                    "status": status, "duration_seconds": elapsed}))

    def _authenticate(self, credential: str | Callable[[], Actor]) -> Actor:
        return credential() if callable(credential) else self.registry.authenticate(credential)

    async def _execute(self, credential: str | Callable[[], Actor], name: str,
                       arguments: dict[str, JsonValue], *, readonly: bool = False) -> JsonValue:
        actor = self._authenticate(credential)
        if name not in TOOLS:
            raise DomainError("Unknown tool")
        # Credentials never reach a workspace store, learning.db or the audit trail.
        arguments, _ = redact_value(arguments)
        if name in LEARNING_TOOLS:
            request = LEARNING_TOOLS[name][0].model_validate(arguments)
            return await self.learning.call(Caller(actor, credential), name, request,
                                            **({"readonly": True} if readonly else {}))
        if name in REPORT_TOOLS:
            return await asyncio.to_thread(self.reports.call, actor, REPORT_TOOLS[name][0].model_validate(arguments))
        request = TOOLS[name][0].model_validate(arguments)
        if name == "memory_scopes":
            return {"actor": actor.model_dump(), "workspaces": [w.model_dump(mode="json")
                    for w in self.registry.workspaces(actor)]}
        scopes = (self.registry.workspaces(actor) if isinstance(request, Search) and request.scope is None
                  else [self.registry.authorize(actor, request.scope, name in WRITES)])
        execution_order = (await asyncio.to_thread(self.pool.search_order, scopes)
                           if isinstance(request, Search) and len(scopes) > 1 else scopes)
        arguments = request.model_dump(mode="json", exclude={"scope"})
        window = self.reranker.window_for(request.query) if isinstance(request, Search) else 0
        if window:
            arguments["defer_cross_rerank"] = True
        output = []
        for workspace in execution_order:
            work = Work(actor=actor, workspace=workspace, operation=name, arguments=arguments)
            data = await asyncio.to_thread(self.pool.invoke, work, credential)
            if name not in WRITES:
                current_actor = self._authenticate(credential)
                self.registry.authorize(current_actor, workspace.scope, False)
            output.append({"scope": workspace.scope.model_dump(mode="json"),
                           "scope_tags": workspace.scope.system_tags(), "data": data})
        if name in WRITES:
            # The pool authorised the write under its lock and the worker committed it; a revocation
            # that lands afterwards must not turn a committed write into a reported failure.
            return output[0]
        current_actor = self._authenticate(credential)
        for workspace in scopes:
            self.registry.authorize(current_actor, workspace.scope, False)
        if isinstance(request, Search):
            return await self._merge(credential, request, scopes, output, window)
        return output[0]

    async def _merge(self, credential: str | Callable[[], Actor], request: Search, scopes: list,
                     output: list[dict], window: int) -> dict:
        """Merge per-workspace results; with a cross-encoder, re-rank the merged window once.

        `output` holds only workspaces authorised for this caller, so the merged window cannot contain
        anything else. Without the cross-encoder the order is the previous one: rank within the
        workspace, then score. With it, the merged fused window is re-ranked, `limit` records are kept,
        and those are ordered by score, which is exactly what one workspace did on its own before.
        Value updates are ordered within their scope and project using the query's temporal intent.
        """
        scope_order = {(workspace.scope.kind.value, workspace.scope.team_id): index
                       for index, workspace in enumerate(scopes)}
        ranked = []
        for group in output:
            ranked.extend({"scope": group["scope"], "scope_tags": group["scope_tags"], "record": record, "scope_rank": rank}
                          for rank, record in enumerate(group["data"], 1))

        def position(item):
            return scope_order[(item["scope"]["kind"], item["scope"]["team_id"])]

        if not window:
            ranked.sort(key=lambda item: (item["scope_rank"], -float(item["record"].get("score", 0)), position(item)))
            return _with_notice({"results": _order_values(ranked, request.query)[:request.limit],
                                 "ordering": "scope_rank_then_score"})
        ranked.sort(key=lambda item: (item["scope_rank"], -float(item["record"].get("rrf_score", 0)), position(item)))
        reranked = await self.reranker.rerank(request.query, ranked[:window], self.pool.timeout)
        current_actor = self._authenticate(credential)
        for workspace in scopes:
            self.registry.authorize(current_actor, workspace.scope, False)
        kept = _order_values(reranked.items + ranked[window:], request.query)[:request.limit]
        kept.sort(key=lambda item: -float(item["record"].get("score", 0)))
        kept = _order_values(kept, request.query)
        for item in kept:
            item["record"] = {key: value for key, value in item["record"].items()
                              if key not in (CONTEXT_FIELD, "fused_rank")}
        return _with_notice({"results": kept, "ordering": "cross_rerank_then_score", "rerank": reranked.status})
