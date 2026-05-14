"""Behaviour tests for the post-tool-use nudge logic.

The bash hook delegates to an inline Python block. To test it without
spawning shells we reproduce the same logic by importing the embedded
script as a module — but since the hook ships as bash, we instead drive
it by invoking the hook script directly with controlled JSON payloads.
Each test asserts on:
  * counters in the state file
  * stdout (the nudge message Claude would see)

The hook lives in `hooks/post-tool-use.sh`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "hooks" / "post-tool-use.sh"
)
NUDGE_LIB = (
    Path(__file__).resolve().parent.parent / "hooks" / "lib" / "memory-nudge.sh"
)


def run_hook(payload: dict, env_extra: dict | None = None, memory_dir: Path = None):
    """Invoke the bash hook in a clean env and capture stdout."""
    env = os.environ.copy()
    env["CLAUDE_MEMORY_DIR"] = str(memory_dir)
    env["CLAUDE_MEMORY_INSTALL_DIR"] = str(HOOK_PATH.parent.parent)
    env["HOOK_PYTHON"] = sys.executable
    # Disable extractor capture so the test stays self-contained.
    env.pop("MEMORY_POST_TOOL_CAPTURE", None)
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})
    proc = subprocess.run(
        ["bash", str(HOOK_PATH)],
        input=json.dumps(payload).encode(),
        env=env,
        capture_output=True,
        timeout=10,
    )
    return proc


def state_for(memory_dir: Path, session_id: str) -> dict:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in session_id)
    path = memory_dir / "state" / f"nudge-{safe}.json"
    return json.loads(path.read_text())


class TestCounters:
    def test_edit_increments_edits(self, tmp_path):
        sid = "sess-A"
        proc = run_hook(
            {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
            memory_dir=tmp_path,
        )
        assert proc.returncode == 0
        s = state_for(tmp_path, sid)
        assert s["edits"] == 1
        assert s["writes"] == 0
        assert s["memory_saves"] == 0

    def test_write_increments_writes(self, tmp_path):
        sid = "sess-B"
        run_hook(
            {"tool_name": "Write", "session_id": sid, "cwd": "/tmp/x"},
            memory_dir=tmp_path,
        )
        s = state_for(tmp_path, sid)
        assert s["writes"] == 1

    def test_memory_save_resets_pressure(self, tmp_path):
        sid = "sess-C"
        for _ in range(3):
            run_hook(
                {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
                memory_dir=tmp_path,
            )
        run_hook(
            {"tool_name": "mcp__memory__memory_save",
             "session_id": sid, "cwd": "/tmp/x"},
            memory_dir=tmp_path,
        )
        s = state_for(tmp_path, sid)
        assert s["edits"] == 3
        assert s["memory_saves"] == 1

    def test_unrelated_tool_does_not_count(self, tmp_path):
        sid = "sess-D"
        run_hook(
            {"tool_name": "Read", "session_id": sid, "cwd": "/tmp/x"},
            memory_dir=tmp_path,
        )
        # No state file should exist — Read doesn't trigger init.
        path = tmp_path / "state" / "nudge-sess-D.json"
        # nudge code only creates the file when field is classified;
        # Read isn't classified so file may stay absent.
        if path.exists():
            s = json.loads(path.read_text())
            assert s["edits"] == 0
            assert s["writes"] == 0


class TestNudgeStdout:
    def test_no_nudge_below_soft_threshold(self, tmp_path):
        sid = "sess-soft0"
        for _ in range(2):
            proc = run_hook(
                {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
                memory_dir=tmp_path,
            )
            assert b"MEMORY_NUDGE" not in proc.stdout

    def test_soft_nudge_at_threshold(self, tmp_path):
        sid = "sess-soft"
        outs = []
        for _ in range(3):
            proc = run_hook(
                {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
                memory_dir=tmp_path,
            )
            outs.append(proc.stdout)
        # The 3rd Edit reaches SOFT=3, no save → soft nudge fires.
        assert any(b"MEMORY_NUDGE [soft]" in o for o in outs)

    def test_hard_nudge_after_seven_writes(self, tmp_path):
        sid = "sess-hard"
        seen_hard = False
        for _ in range(8):
            proc = run_hook(
                {"tool_name": "Write", "session_id": sid, "cwd": "/tmp/x"},
                memory_dir=tmp_path,
            )
            if b"MEMORY_NUDGE [hard]" in proc.stdout:
                seen_hard = True
        assert seen_hard

    def test_save_silences_nudges_until_divergence(self, tmp_path):
        sid = "sess-silence"
        # 3 edits → soft nudge fires
        for _ in range(3):
            run_hook(
                {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
                memory_dir=tmp_path,
            )
        # Save
        run_hook(
            {"tool_name": "mcp__memory__memory_save",
             "session_id": sid, "cwd": "/tmp/x"},
            memory_dir=tmp_path,
        )
        # Next 3 edits: STEP*2=6 gap not yet reached after save → silent.
        outs = []
        for _ in range(3):
            proc = run_hook(
                {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
                memory_dir=tmp_path,
            )
            outs.append(proc.stdout)
        assert not any(b"MEMORY_NUDGE" in o for o in outs), (
            "save should silence nudges until significant new divergence"
        )

    def test_disable_flag(self, tmp_path):
        sid = "sess-off"
        for _ in range(8):
            proc = run_hook(
                {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
                env_extra={"MEMORY_NUDGE_DISABLE": "1"},
                memory_dir=tmp_path,
            )
            assert b"MEMORY_NUDGE" not in proc.stdout


class TestNudgeSummary:
    """nudge_summary() is sourced from memory-nudge.sh and used by
    on-stop.sh. Drive it via a tiny bash wrapper so we cover the
    bash-side too."""

    def _run_summary(self, tmp_path: Path, session_id: str, project: str) -> str:
        wrapper = f"""
            export CLAUDE_MEMORY_DIR={tmp_path}
            export HOOK_PYTHON={sys.executable}
            source {NUDGE_LIB}
            nudge_summary "{session_id}" "{project}"
        """
        proc = subprocess.run(
            ["bash", "-c", wrapper], capture_output=True, timeout=10
        )
        return proc.stdout.decode().strip()

    def test_summary_silent_when_nothing_happened(self, tmp_path):
        out = self._run_summary(tmp_path, "empty-session", "proj")
        assert out == ""

    def test_summary_final_warning_on_zero_saves(self, tmp_path):
        sid = "sess-final-warn"
        for _ in range(4):
            run_hook(
                {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
                memory_dir=tmp_path,
            )
        out = self._run_summary(tmp_path, sid, "proj")
        assert "MEMORY_FINAL_WARNING" in out

    def test_summary_ok_when_saves_present(self, tmp_path):
        sid = "sess-final-ok"
        for _ in range(3):
            run_hook(
                {"tool_name": "Edit", "session_id": sid, "cwd": "/tmp/x"},
                memory_dir=tmp_path,
            )
        run_hook(
            {"tool_name": "mcp__memory__memory_save",
             "session_id": sid, "cwd": "/tmp/x"},
            memory_dir=tmp_path,
        )
        out = self._run_summary(tmp_path, sid, "proj")
        assert "MEMORY_FINAL_OK" in out or "MEMORY_FINAL_NOTE" in out
