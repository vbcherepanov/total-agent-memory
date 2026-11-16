from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import chain
from typing import Literal, NotRequired, TypedDict

from memory_core.timestamps import utc_now

EXPORT_FORMAT = "tam-records"
EXPORT_SCHEMA_VERSION = 1
ExportState = Literal["active", "archived", "retracted"]
EXPORT_PAGE_DEFAULT_LIMIT = 1000
EXPORT_PAGE_MAX_LIMIT = 5000
CURSOR_KEY_ENV = "TAM_EXPORT_CURSOR_KEY"
CURSOR_VERSION = 1
CURSOR_MAC_SEPARATOR = "."
SNAPSHOT_ID_BYTES = 16
CURSOR_KEY_BYTES = 32
FIRST_PAGE = 1
# Cursors are signed; without a configured key they stay valid for the life of this process.
_CURSOR_KEY = (os.environ.get(CURSOR_KEY_ENV) or "").encode() or secrets.token_bytes(CURSOR_KEY_BYTES)


class ExportHeader(TypedDict):
    format: str
    schema_version: int
    exported_at: str
    total: int
    counts: dict[str, dict[str, int]]
    snapshot: NotRequired[dict[str, object]]


class ExportEnd(TypedDict):
    complete: bool
    next_cursor: str | None


class ExportFormatError(ValueError):
    def __init__(self, line: int, reason: str):
        super().__init__(f"knowledge.jsonl line {line}: {reason}")


class ExportCursorError(ValueError):
    """The cursor is malformed, forged, or does not belong to this export request."""


@dataclass(frozen=True)
class PageCursor:
    """Position of one page inside a snapshot: fixed identity plus where the page starts."""

    snapshot_id: str
    upper_id: int
    last_id: int
    page: int
    total: int
    exported_at: str
    project: str | None
    from_cursor: str | None = None

    def describe(self) -> dict[str, object]:
        return {"id": self.snapshot_id, "upper_id": self.upper_id, "page": self.page,
                "total": self.total, "from_cursor": self.from_cursor}

    def advance(self, last_id: int) -> PageCursor:
        return replace(self, last_id=last_id, page=self.page + 1)


def new_snapshot(*, project: str | None, upper_id: int, total: int) -> PageCursor:
    return PageCursor(secrets.token_hex(SNAPSHOT_ID_BYTES), upper_id, 0, FIRST_PAGE, total, utc_now(), project)


def validate_page_limit(limit: object) -> int:
    if limit is None:
        return EXPORT_PAGE_DEFAULT_LIMIT
    if type(limit) is not int or not 1 <= limit <= EXPORT_PAGE_MAX_LIMIT:
        raise ValueError(f"limit must be an integer from 1 to {EXPORT_PAGE_MAX_LIMIT}")
    return limit


def _sign(body: str) -> str:
    return hmac.new(_CURSOR_KEY, body.encode(), hashlib.sha256).hexdigest()


def encode_cursor(position: PageCursor) -> str:
    payload = {"v": CURSOR_VERSION, "sid": position.snapshot_id, "up": position.upper_id,
               "last": position.last_id, "page": position.page, "total": position.total,
               "at": position.exported_at, "project": position.project}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return body + CURSOR_MAC_SEPARATOR + _sign(body)


def decode_cursor(token: object, project: str | None) -> PageCursor:
    """Validate the signature, shape and project binding of a cursor issued by this server."""
    if not isinstance(token, str) or token.count(CURSOR_MAC_SEPARATOR) != 1:
        raise ExportCursorError("invalid export cursor")
    body, mac = token.split(CURSOR_MAC_SEPARATOR)
    if not hmac.compare_digest(mac, _sign(body)):
        raise ExportCursorError("invalid export cursor: signature mismatch (forged, altered or issued "
                                "before a restart; restart the export without a cursor)")
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
        ints = [payload[key] for key in ("up", "last", "page", "total")]
        if (payload["v"] != CURSOR_VERSION or not isinstance(payload["sid"], str)
                or not isinstance(payload["at"], str) or any(type(item) is not int or item < 0 for item in ints)
                or not (payload["project"] is None or isinstance(payload["project"], str))):
            raise ExportCursorError("invalid export cursor: unexpected content")
    except (binascii.Error, ValueError, KeyError, TypeError) as error:
        raise ExportCursorError("invalid export cursor: unreadable") from error
    if payload["project"] != project:
        raise ExportCursorError("export cursor was issued for a different project")
    return PageCursor(payload["sid"], payload["up"], payload["last"], payload["page"], payload["total"],
                      payload["at"], payload["project"], token)


def record_state(row: Mapping[str, object]) -> ExportState:
    if row.get("superseded_by") is not None or row.get("status", "active") not in ("active", "archived"):
        return "retracted"
    return "archived" if row.get("status") == "archived" else "active"


def snapshot_header(rows: Iterable[Mapping[str, object]], *, exported_at: str | None = None,
                    snapshot: Mapping[str, object] | None = None) -> ExportHeader:
    counts: dict[str, dict[str, int]] = {}
    total = 0
    for row in rows:
        states = counts.setdefault(str(row["type"]), {})
        state = record_state(row)
        states[state] = states.get(state, 0) + 1
        total += 1
    header: ExportHeader = {"format": EXPORT_FORMAT, "schema_version": EXPORT_SCHEMA_VERSION,
                            "exported_at": exported_at or utc_now(), "total": total, "counts": counts}
    if snapshot is not None:
        header["snapshot"] = dict(snapshot)
    return header


def snapshot_lines(rows: list[dict[str, object]], *, header: ExportHeader | None = None,
                   next_cursor: str | None = None) -> Iterator[str]:
    """Yield the JSONL lines of one export; a page ``header`` carries the snapshot identity."""
    header = header or snapshot_header(rows)
    yield json.dumps({"header": header}, ensure_ascii=False)
    for row in rows:
        yield json.dumps({"record": {**row, "key": str(row["id"]), "state": record_state(row)}},
                         ensure_ascii=False)
    yield json.dumps({"end": {"complete": next_cursor is None, "next_cursor": next_cursor}})


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def read_pages(sources: Sequence[Iterable[bytes]]) -> Iterator[bytes]:
    """Concatenate the lines of several page files so ``read_snapshot`` verifies them as one export."""
    return chain.from_iterable(sources)


@dataclass
class _Paging:
    snapshot: dict[str, object]
    page: int
    next_cursor: str | None
    union: int = 0
    last_id: int = 0


def _parse_header(header: dict[str, object], number: int, expected: Counter[tuple[str, str]]) -> None:
    if header.get("format") != EXPORT_FORMAT or header.get("schema_version") != EXPORT_SCHEMA_VERSION:
        raise ExportFormatError(number, "unsupported snapshot format or schema version")
    from importers.records import parse_timestamp

    try:
        stamp = parse_timestamp(header.get("exported_at"), strict=True)
    except ValueError as error:
        raise ExportFormatError(number, "invalid export time") from error
    if stamp is None:
        raise ExportFormatError(number, "missing export time")
    counts = header.get("counts")
    if type(header.get("total")) is not int or header["total"] < 0 or not isinstance(counts, dict):
        raise ExportFormatError(number, "invalid total or counts")
    for kind, states in counts.items():
        if not isinstance(states, dict):
            raise ExportFormatError(number, "invalid type/state counts")
        for state, count in states.items():
            if state not in ("active", "archived", "retracted") or type(count) is not int or count < 0:
                raise ExportFormatError(number, "invalid type/state count")
            expected[kind, state] = count
    if sum(expected.values()) != header["total"]:
        raise ExportFormatError(number, "counts do not match total")


def _check_page(header: dict[str, object], number: int, paging: _Paging | None) -> _Paging:
    """Check that a page header continues the snapshot of the previous page (or starts a new one)."""
    info = header.get("snapshot")
    if not isinstance(info, dict) or not isinstance(info.get("id"), str) or not info["id"] or not all(
            type(info.get(key)) is int and info[key] >= 0 for key in ("upper_id", "page", "total")):
        raise ExportFormatError(number, "invalid snapshot block")
    identity = {"id": info["id"], "upper_id": info["upper_id"], "total": info["total"],
                "exported_at": header["exported_at"]}
    if paging is None:
        if info["page"] != FIRST_PAGE or info.get("from_cursor") is not None:
            raise ExportFormatError(number, f"export must start at page {FIRST_PAGE} (page {info['page']} "
                                            "found); an earlier page is missing")
        return _Paging(identity, FIRST_PAGE, None)
    if identity != paging.snapshot:
        raise ExportFormatError(number, "page belongs to a different snapshot")
    if info["page"] != paging.page + 1:
        raise ExportFormatError(number, f"page {info['page']} follows page {paging.page}; "
                                        "pages are not contiguous")
    if info.get("from_cursor") != paging.next_cursor:
        raise ExportFormatError(number, "page does not continue from the previous page cursor")
    paging.page = info["page"]
    return paging


def read_snapshot(lines: Iterable[bytes], *, max_line_bytes: int) -> Iterator[dict[str, object]]:
    header: dict[str, object] | None = None
    expected: Counter[tuple[str, str]] = Counter()
    actual: Counter[tuple[str, str]] = Counter()
    keys: set[str] = set()
    paging: _Paging | None = None
    ended = False
    awaiting_page = False
    started = False
    number = 0
    for number, line in enumerate(lines, 1):
        if len(line) > max_line_bytes:
            raise ExportFormatError(number, f"exceeds {max_line_bytes} bytes")
        if not line.strip():
            continue
        try:
            value = json.loads(line, object_pairs_hook=_unique_object)
        except RecursionError as error:
            raise ExportFormatError(number, "nested too deeply") from error
        except ValueError as error:
            raise ExportFormatError(number, f"invalid JSON: {error}") from error
        if not isinstance(value, dict):
            raise ExportFormatError(number, "expected an object")
        if (not started or awaiting_page) and "header" in value:
            if len(value) != 1 or not isinstance(value["header"], dict):
                raise ExportFormatError(number, "expected one header key")
            header = value["header"]
            expected.clear()
            actual.clear()
            _parse_header(header, number, expected)
            if "snapshot" in header or awaiting_page:
                if "snapshot" not in header:
                    raise ExportFormatError(number, "page belongs to a different snapshot")
                paging = _check_page(header, number, paging)
            started = True
            awaiting_page = False
            continue
        if not started:
            started = True
        if header is None:
            yield value
            continue
        if awaiting_page:
            raise ExportFormatError(number, "expected the header of the next page")
        if ended:
            raise ExportFormatError(number, "data after end line")
        if len(value) != 1:
            raise ExportFormatError(number, "expected exactly one key per line")
        if "end" in value:
            end = value["end"]
            if not isinstance(end, dict) or not isinstance(end.get("complete"), bool):
                raise ExportFormatError(number, "incomplete snapshot cannot be imported")
            more = end.get("next_cursor")
            if paging is None:
                if end["complete"] is not True or more is not None:
                    raise ExportFormatError(number, "incomplete snapshot cannot be imported")
            elif end["complete"] != (more is None) or not (more is None or (isinstance(more, str) and more)):
                raise ExportFormatError(number, "end line complete flag and next_cursor disagree")
            if actual != expected:
                raise ExportFormatError(number, "record counts do not match header")
            if paging is None:
                ended = True
            else:
                paging.union += sum(actual.values())
                paging.next_cursor = more
                if more is None:
                    if paging.union != paging.snapshot["total"]:
                        raise ExportFormatError(number, f"pages hold {paging.union} records but the "
                                                        f"snapshot declares {paging.snapshot['total']}")
                    ended = True
                else:
                    awaiting_page = True
            continue
        row = value.get("record")
        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or not row["key"]:
            raise ExportFormatError(number, "expected a record with a nonempty key")
        key = row["key"]
        if type(row.get("id")) is not int or row["id"] < 1 or key != str(row["id"]):
            raise ExportFormatError(number, "record key must match its positive integer id")
        if key in keys:
            raise ExportFormatError(number, f"duplicate record key {key!r}")
        if paging is not None:
            if row["id"] <= paging.last_id:
                raise ExportFormatError(number, "records must be in ascending id order across pages")
            if row["id"] > paging.snapshot["upper_id"]:
                raise ExportFormatError(number, "record is newer than the snapshot")
            paging.last_id = row["id"]
        keys.add(key)
        if row.get("state") != record_state(row):
            raise ExportFormatError(number, "record state does not match status/supersession")
        bucket = (str(row.get("type")), str(row.get("state")))
        actual[bucket] += 1
        if actual[bucket] > expected[bucket]:
            raise ExportFormatError(number, "record count exceeds header")
        yield row
    if awaiting_page and paging is not None:
        raise ExportFormatError(number + 1, f"final page missing: export ended after page {paging.page} "
                                            "with more pages pending")
    if header is not None and not ended:
        raise ExportFormatError(number + 1, "missing end line")
