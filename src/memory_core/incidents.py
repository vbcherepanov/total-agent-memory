from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memory_core.snapshot import Snapshot
from memory_core.telemetry import counters, op_timer

OPEN_ITEM = re.compile(r"^\s*[-*+]\s+\[ \]\s+", re.MULTILINE)
INCIDENT_FIELDS = ("detection", "affected", "resolution", "incident_status")


class IncidentError(ValueError):
    code = "invalid_incident"


class Detection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    check: str = Field(min_length=1)
    healthy_result: str = Field(min_length=1)


class AffectedWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)
    scope: str = Field(min_length=1)
    starts_at: str = Field(alias="from")
    ends_at: str | None = Field(default=None, alias="to")

    @field_validator("starts_at", "ends_at")
    @classmethod
    def timestamp(cls, value: str | None) -> str | None:
        return Snapshot.at(value).instant if value is not None else None

    @model_validator(mode="after")
    def interval(self):
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("affected.to must be later than affected.from")
        return self


class IncidentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    detection: Detection | None = None
    affected: list[AffectedWindow] = Field(default_factory=list)
    resolution: str | None = None
    incident_status: Literal["open", "closed"] = "open"

    @model_validator(mode="after")
    def closure(self):
        if self.incident_status == "closed" and not self.resolution:
            raise IncidentError("resolution is required to close an incident")
        identities = [(window.scope, window.starts_at) for window in self.affected]
        if len(set(identities)) != len(identities):
            raise IncidentError("affected windows must not repeat scope and start time")
        return self

    def hints(self) -> list[str]:
        result = []
        if self.detection is None:
            result.append("Add detection.check and detection.healthy_result so the incident can be checked again.")
        if not self.affected:
            result.append("Add affected windows with scope, from and optional to.")
        return result


class IncidentFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True, str_strip_whitespace=True)
    status: Literal["open", "closed"] | None = None
    scope: str | None = Field(default=None, min_length=1)
    starts_at: str | None = Field(default=None, alias="from")
    ends_at: str | None = Field(default=None, alias="to")

    @field_validator("starts_at", "ends_at")
    @classmethod
    def timestamp(cls, value: str | None) -> str | None:
        return Snapshot.at(value).instant if value is not None else None

    @model_validator(mode="after")
    def interval(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("incident.to must be later than incident.from")
        return self

    def sql(self) -> tuple[list[str], list[str]]:
        predicates = ["k.type='incident'"]
        params: list[str] = []
        if self.status:
            predicates.append("EXISTS (SELECT 1 FROM knowledge_incidents i WHERE i.knowledge_id=k.id AND i.incident_status=?)")
            params.append(self.status)
        if self.scope or self.starts_at or self.ends_at:
            conditions = ["w.knowledge_id=k.id"]
            if self.scope:
                conditions.append("w.scope=?")
                params.append(self.scope)
            if self.starts_at:
                conditions.append("(w.ends_at IS NULL OR w.ends_at>?)")
                params.append(self.starts_at)
            if self.ends_at:
                conditions.append("w.starts_at<?")
                params.append(self.ends_at)
            predicates.append("EXISTS (SELECT 1 FROM knowledge_incident_windows w WHERE " + " AND ".join(conditions) + ")")
        return predicates, params


class IncidentService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, identity: int) -> IncidentSpec:
        row = self.db.execute("SELECT * FROM knowledge_incidents WHERE knowledge_id=?", (identity,)).fetchone()
        if row is None:
            return IncidentSpec()
        detection = (Detection(check=row["detection_check"], healthy_result=row["healthy_result"])
                     if row["detection_check"] else None)
        windows = self.db.execute("SELECT scope,starts_at,ends_at FROM knowledge_incident_windows "
                                  "WHERE knowledge_id=? ORDER BY scope,starts_at", (identity,)).fetchall()
        return IncidentSpec(detection=detection, affected=[AffectedWindow(**dict(window)) for window in windows],
                            resolution=row["resolution"], incident_status=row["incident_status"])

    def prepare(self, kind: str, arguments: Mapping[str, object], previous_id: int | None = None) -> IncidentSpec | None:
        from privacy_filter import redact_private_value
        from secret_redaction import redact_value

        fields = {key: arguments[key] for key in INCIDENT_FIELDS if key in arguments}
        if kind != "incident":
            if fields:
                raise IncidentError("Incident fields require type=incident")
            return None
        previous = self.get(previous_id).model_dump() if previous_id is not None else {}
        fields, _redacted = redact_value(redact_private_value(fields))
        try:
            return IncidentSpec.model_validate({**previous, **fields})
        except ValueError as error:
            counters.bump("incident_validation_failures")
            raise IncidentError(str(error)) from error

    @op_timer("incident_save_ms")
    def write(self, identity: int, spec: IncidentSpec, content: str | None = None) -> int:
        """Write the incident rows without committing; the caller owns the transaction.

        Without ``content`` the stored text of the record is read, so the open-item
        count matches what is persisted. Returns the open-item count.
        """
        if content is None:
            row = self.db.execute("SELECT content FROM knowledge WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise IncidentError(f"Record {identity} does not exist")
            content = row["content"]
        open_items = len(OPEN_ITEM.findall(content))
        detection = spec.detection
        self.db.execute(
            "INSERT INTO knowledge_incidents(knowledge_id,detection_check,healthy_result,resolution,incident_status,open_items) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(knowledge_id) DO UPDATE SET detection_check=excluded.detection_check, "
            "healthy_result=excluded.healthy_result,resolution=excluded.resolution,incident_status=excluded.incident_status, "
            "open_items=excluded.open_items",
            (identity, detection.check if detection else None, detection.healthy_result if detection else None,
             spec.resolution, spec.incident_status, open_items))
        self.db.execute("DELETE FROM knowledge_incident_windows WHERE knowledge_id=?", (identity,))
        for window in spec.affected:
            self.db.execute("INSERT INTO knowledge_incident_windows(knowledge_id,scope,starts_at,ends_at) VALUES (?,?,?,?)",
                             (identity, window.scope, window.starts_at, window.ends_at))
        return open_items

    def open_items(self, identity: int) -> int:
        row = self.db.execute("SELECT open_items FROM knowledge_incidents WHERE knowledge_id=?", (identity,)).fetchone()
        return row["open_items"] if row is not None else 0

    @staticmethod
    def payload(spec: IncidentSpec, open_items: int) -> dict[str, object]:
        counters.bump("incident_saves")
        return {"incident": spec.model_dump(by_alias=True), "open_items": open_items, "hints": spec.hints()}

    def save(self, identity: int, spec: IncidentSpec, content: str) -> dict[str, object]:
        open_items = self.write(identity, spec, content)
        self.db.commit()
        return self.payload(spec, open_items)

    @op_timer("incident_search_ms")
    def search(self, query: str, scope, limit: int, *, postgres: bool = False) -> dict[str, object]:
        from memory_core.query_terms import fts_match_query

        conditions, params = scope.sql()
        match = fts_match_query(query)
        source = "knowledge k"
        ordering = "k.created_at DESC,k.id DESC"
        if match:
            if postgres:
                from memory_core.pg_fts import match_source

                matched, match_params = match_source("knowledge_fts", match)
                source = f"({matched}) f JOIN knowledge k ON k.id=f.id"
                params = [*match_params, *params]
                ordering = "f.rank,k.id"
            else:
                source = "knowledge_fts JOIN knowledge k ON k.id=knowledge_fts.rowid"
                conditions.insert(0, "knowledge_fts MATCH ?")
                params.insert(0, match)
                ordering = "bm25(knowledge_fts),k.id"
        rows = self.db.execute("SELECT k.* FROM " + source + " WHERE " + " AND ".join(conditions)
                               + " ORDER BY " + ordering + " LIMIT ?", [*params, limit]).fetchall()
        results = []
        for row in rows:
            record = dict(row)
            record["incident"] = self.get(row["id"]).model_dump(by_alias=True)
            record["open_items"] = len(OPEN_ITEM.findall(row["content"]))
            record["score"] = 1.0 / (len(results) + 1)
            record["via"] = ["incident_fts" if match else "incident_filter"]
            results.append(record)
        counters.bump("incident_searches")
        return {"query": query, "total": len(results), "results": {"incident": results}}


def input_properties() -> dict[str, object]:
    schema = IncidentSpec.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    return expand(schema["properties"])
