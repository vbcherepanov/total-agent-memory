from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tam_db.prepare_vector_index import main

ROOT = Path(__file__).resolve().parents[1]


def test_source_cli_help_requires_no_database(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_vector_index.py"), "--help"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "--dimension" in result.stdout
    assert "--dsn-env" in result.stdout


@pytest.mark.parametrize("schema", ["public", "ws_bad", "ws_" + "a" * 48 + ";DROP SCHEMA public"])
def test_cli_rejects_non_workspace_schema_before_connection(monkeypatch, capsys, schema):
    monkeypatch.setattr(sys, "argv", [
        "tam-prepare-vector-index", "--dsn-env", "TAM_TEST_EMPTY_DSN",
        "--schema", schema, "--dimension", "384",
    ])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "Expected a TAM workspace schema" in capsys.readouterr().err


def test_cli_reports_missing_dsn_without_connecting(monkeypatch, capsys):
    monkeypatch.delenv("TAM_TEST_EMPTY_DSN", raising=False)
    monkeypatch.setattr(sys, "argv", [
        "tam-prepare-vector-index", "--dsn-env", "TAM_TEST_EMPTY_DSN",
        "--schema", "ws_" + "a" * 48, "--dimension", "384",
    ])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "DSN environment variable is empty" in capsys.readouterr().err
