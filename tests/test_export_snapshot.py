import json
import asyncio

import pytest

from memory_core.export_snapshot import ExportFormatError, read_snapshot, snapshot_lines
from tests.pg_store_support import store_backend  # noqa: F401


def records():
    return [{"id": 1, "type": "fact", "status": "deleted", "content": "old", "superseded_by": 2},
            {"id": 2, "type": "fact", "status": "active", "content": "new"},
            {"id": 3, "type": "lesson", "status": "archived", "content": "archived"}]


def parse(lines):
    return list(read_snapshot((line.encode() for line in lines), max_line_bytes=65536))


def test_snapshot_envelope_has_exact_state_counts():
    lines = list(snapshot_lines(records()))
    header = json.loads(lines[0])["header"]
    assert header["total"] == 3
    assert header["counts"] == {"fact": {"active": 1, "retracted": 1}, "lesson": {"archived": 1}}
    assert [row["state"] for row in parse(lines)] == ["retracted", "active", "archived"]


@pytest.mark.parametrize("damage,line,reason", [
    ("truncate", 5, "missing end"), ("duplicate", 3, "duplicate record key"),
    ("omit", 4, "counts do not match"), ("extra-key", 2, "one key per line"),
    ("repeat-json-key", 2, "duplicate JSON key"), ("incomplete", 5, "incomplete"),
    ("after-end", 6, "after end"), ("wrong-state", 2, "does not match"),
])
def test_snapshot_rejects_first_offending_line(damage, line, reason):
    lines = list(snapshot_lines(records()))
    if damage == "truncate":
        lines.pop()
    elif damage == "duplicate":
        lines.insert(2, lines[1])
    elif damage == "omit":
        lines.pop(1)
    elif damage == "extra-key":
        lines[1] = lines[1][:-1] + ', "extra": 1}'
    elif damage == "repeat-json-key":
        lines[1] = '{"record": {}, "record": {}}'
    elif damage == "incomplete":
        lines[-1] = '{"end": {"complete": false, "next_cursor": "3"}}'
    elif damage == "after-end":
        lines.append(lines[1])
    elif damage == "wrong-state":
        lines[1] = lines[1].replace('"retracted"', '"active"')
    else:
        raise AssertionError(damage)
    with pytest.raises(ExportFormatError, match=f"line {line}: .*{reason}"):
        parse(lines)


def test_headerless_exports_remain_readable():
    assert parse([json.dumps(row) for row in records()]) == records()


def test_mcp_export_includes_inactive_records_and_complete_jsonl(store_backend, monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "MEMORY_DIR", tmp_path)
    instance = server.Store(database=store_backend, background_queues=False)
    instance._embed_mode = "none"
    monkeypatch.setattr(server, "store", instance)
    try:
        ids = []
        for index in range(3):
            ids.append(instance.save_knowledge("export", f"Export state sample number {index}", "fact",
                                               "export", skip_dedup=True, skip_quality=True)[0])
        instance.db.execute("UPDATE knowledge SET status='deleted',superseded_by=? WHERE id=?", (ids[1], ids[0]))
        instance.db.execute("UPDATE knowledge SET status='archived' WHERE id=?", (ids[2],))
        instance.db.commit()
        data = json.loads(asyncio.run(server._do("memory_export", {"project": "export", "save_to_file": False})))
        assert data["header"]["total"] == 3 and data["end"]["complete"] is True
        assert [row["state"] for row in data["knowledge"]] == ["retracted", "active", "archived"]
        raw = asyncio.run(server._do("memory_export", {"project": "export", "save_to_file": False, "format": "jsonl"}))
        assert [row["id"] for row in parse(raw.splitlines())] == ids
    finally:
        instance.db.close()
