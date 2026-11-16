"""Paged memory_export: one snapshot identity, signed cursors, contiguous verified import."""
import asyncio
import json
import sqlite3

import pytest

import portable_export
from memory_core.export_snapshot import (
    EXPORT_PAGE_MAX_LIMIT, ExportCursorError, ExportFormatError, decode_cursor, read_pages, read_snapshot)
from portable_export import BundleError
from tests.pg_store_support import store_backend  # noqa: F401

PROJECT = "paging"
RECORD_COUNT = 7
PAGE_LIMIT = 3
MAX_LINE = 1 << 20


@pytest.fixture
def source(store_backend, monkeypatch, tmp_path):  # noqa: F811
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(database=store_backend, background_queues=False)
    instance._embed_mode = "none"
    monkeypatch.setattr(server, "store", instance)
    ids = [instance.save_knowledge("paging", f"Paging sample record number {index} about topic {index * 17}",
                                   "fact", PROJECT, skip_dedup=True, skip_quality=True)[0]
           for index in range(RECORD_COUNT)]
    yield server, instance, ids
    instance.db.close()


def export(server, **arguments) -> str:
    return asyncio.run(server._do("memory_export", {"project": PROJECT, "save_to_file": False,
                                                    "format": "jsonl", **arguments}))


def pages(server, limit=PAGE_LIMIT, between=None) -> list[str]:
    result, cursor = [], None
    while True:
        page = export(server, limit=limit, **({"cursor": cursor} if cursor else {}))
        result.append(page)
        if between is not None:
            between(len(result))
        cursor = json.loads(page.splitlines()[-1])["end"]["next_cursor"]
        if cursor is None:
            return result


def parse(*texts: str) -> list[dict]:
    return list(read_snapshot(read_pages([[line.encode() for line in text.splitlines()] for text in texts]),
                              max_line_bytes=MAX_LINE))


def test_unpaged_export_is_unchanged(source):
    server, _, ids = source
    lines = export(server).splitlines()
    assert "snapshot" not in json.loads(lines[0])["header"]
    assert json.loads(lines[-1]) == {"end": {"complete": True, "next_cursor": None}}
    assert [row["id"] for row in parse(export(server))] == ids


def test_paged_export_round_trips_to_the_unpaged_export(source):
    server, _, ids = source
    texts = pages(server)
    assert len(texts) == 3
    ends = [json.loads(text.splitlines()[-1])["end"] for text in texts]
    assert [end["complete"] for end in ends] == [False, False, True]
    assert [end["next_cursor"] is None for end in ends] == [False, False, True]
    headers = [json.loads(text.splitlines()[0])["header"] for text in texts]
    assert len({(h["snapshot"]["id"], h["exported_at"], h["snapshot"]["upper_id"]) for h in headers}) == 1
    assert [h["snapshot"]["page"] for h in headers] == [1, 2, 3]
    paged = parse(*texts)
    assert [row["id"] for row in paged] == ids
    assert paged == parse(export(server))
    assert parse("\n".join(texts)) == paged


def test_record_inserted_between_pages_is_not_in_the_snapshot(source):
    server, instance, ids = source

    def insert_and_change(page_number):
        if page_number == 1:
            instance.save_knowledge("paging", "A record written while the export was running",
                                    "fact", PROJECT, skip_dedup=True, skip_quality=True)
            instance.db.execute("UPDATE knowledge SET status='archived' WHERE id=?", (ids[-1],))
            instance.db.commit()

    texts = pages(server, between=insert_and_change)
    rows = parse(*texts)
    assert [row["id"] for row in rows] == ids
    assert rows[-1]["state"] == "archived"
    assert [row["id"] for row in parse(export(server))] == [*ids, max(ids) + 1]


@pytest.mark.parametrize("damage", ["flip-body", "flip-signature", "unsigned", "empty", "other-project"])
def test_tampered_or_forged_cursor_is_rejected(source, damage):
    server, _, _ = source
    cursor = json.loads(export(server, limit=PAGE_LIMIT).splitlines()[-1])["end"]["next_cursor"]
    body, mac = cursor.split(".")
    forged = {"flip-body": ("A" if body[0] != "A" else "B") + body[1:] + "." + mac,
              "flip-signature": body + "." + ("0" if mac[0] != "0" else "1") + mac[1:],
              "unsigned": body, "empty": "", "other-project": cursor}[damage]
    arguments = {"limit": PAGE_LIMIT, "cursor": forged}
    if damage == "other-project":
        with pytest.raises(ExportCursorError, match="different project"):
            asyncio.run(server._do("memory_export", {"project": "elsewhere", "save_to_file": False,
                                                     "format": "jsonl", **arguments}))
        return
    with pytest.raises(ExportCursorError, match="invalid export cursor"):
        export(server, **arguments)
    with pytest.raises(ExportCursorError):
        decode_cursor(forged, PROJECT)


@pytest.mark.parametrize("limit", [0, -1, EXPORT_PAGE_MAX_LIMIT + 1, "3", True])
def test_page_limit_is_bounded(source, limit):
    server, _, _ = source
    with pytest.raises(ValueError, match="limit must be an integer"):
        export(server, limit=limit)


def test_pages_of_two_exports_cannot_be_mixed(source):
    server, _, _ = source
    first, second = pages(server), pages(server)
    with pytest.raises(ExportFormatError, match="different snapshot"):
        parse(first[0], second[1], second[2])
    with pytest.raises(ExportFormatError, match="different snapshot"):
        parse(first[0], first[1], second[2])


@pytest.mark.parametrize("keep,reason", [
    ([0, 1], "final page missing"), ([0], "final page missing"),
    ([0, 2], "not contiguous"), ([1, 2], "must start at page 1"),
    ([0, 1, 1, 2], "not contiguous"), ([0, 2, 1], "not contiguous"),
])
def test_missing_duplicated_or_reordered_pages_are_rejected(source, keep, reason):
    server, _, _ = source
    texts = pages(server)
    with pytest.raises(ExportFormatError, match=reason):
        parse(*[texts[index] for index in keep])


def test_missing_final_page_is_a_domain_error_in_portable_import(source, tmp_path):
    server, _, _ = source
    texts = pages(server)
    staging = tmp_path / "staged"
    staging.mkdir()
    (staging / portable_export.RECORDS_MEMBER).write_text("".join(t + "\n" for t in texts[:-1]), encoding="utf-8")
    staged = portable_export.StagedBundle(staging, {"kind": "project"})
    with pytest.raises(BundleError, match="final page missing"):
        portable_export._merge(tmp_path / "destination", staged)
    assert not (tmp_path / "destination").exists()


def test_every_record_state_survives_paged_export_and_import(source, tmp_path):
    server, instance, ids = source
    instance.db.execute("UPDATE knowledge SET status='superseded', superseded_by=? WHERE id=?", (ids[1], ids[0]))
    instance.db.execute("UPDATE knowledge SET status='archived' WHERE id=?", (ids[2],))
    instance.db.execute("UPDATE knowledge SET status='deleted' WHERE id=?", (ids[3],))
    instance.db.commit()
    texts = pages(server, limit=2)
    staging = tmp_path / "staged"
    staging.mkdir()
    (staging / portable_export.RECORDS_MEMBER).write_text("".join(t + "\n" for t in texts), encoding="utf-8")
    target = tmp_path / "destination"
    result = portable_export._merge(target, portable_export.StagedBundle(staging, {"kind": "project"}))
    assert result["imported"] == RECORD_COUNT
    with sqlite3.connect(target / "memory.db") as db:
        rows = db.execute("SELECT id,status,superseded_by FROM knowledge ORDER BY id").fetchall()
    assert [row[1] for row in rows] == ["superseded", "active", "archived", "deleted", "active", "active", "active"]
    assert rows[0][2] == rows[1][0]
    assert [row["state"] for row in parse(*texts)][:4] == ["retracted", "active", "archived", "retracted"]
