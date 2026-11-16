"""The ambient hook commands, one entry point for every way a hook reaches them.

``hooks/lib/tam_hook.py`` normalises the client payload into a :class:`HookCall`
(project, session, exit status already derived) and runs :func:`run` either
in-process (native install) or inside the TAM container through
``docker exec -i <container> python -m ambient <command>`` (Docker install,
where the store lives in a volume the host cannot open). Each command returns
the text the hook hands on: model context for ``prompt-context`` and
``rule-reminder``, a user notice for ``turn-end``, nothing for the others.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from ambient.actions import ActionLog, build_action, log_event, parse_exit_code, status_for, table_exists
from ambient.settings import AmbientSettings
from ambient.state import claim_tool_use

MEMORY_DB_NAME: Final = "memory.db"
SQLITE_TIMEOUT_SEC: Final = 2.0
RECORD_ACTION: Final = "record-action"
PROMPT_CONTEXT: Final = "prompt-context"
RULE_REMINDER: Final = "rule-reminder"
TURN_END: Final = "turn-end"
CODEX_NOTIFY: Final = "codex-notify"


class HookCallError(ValueError):
    """The normalised hook input is malformed."""


@dataclass(frozen=True)
class HookCall:
    payload: dict[str, Any]
    project: str
    session_id: str
    cwd: str = ""
    exit_code: str = ""
    failed: bool = False
    interrupted: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> HookCall:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HookCallError(f"hook call is not JSON: {exc}") from None
        if not isinstance(data, Mapping) or not isinstance(data.get("payload"), Mapping):
            raise HookCallError("hook call must be an object with a 'payload' object")
        return cls(payload=dict(data["payload"]), project=str(data.get("project") or "general"),
                   session_id=str(data.get("session_id") or "unknown"), cwd=str(data.get("cwd") or ""),
                   exit_code=str(data.get("exit_code") or ""), failed=bool(data.get("failed")),
                   interrupted=bool(data.get("interrupted")))

    @property
    def prompt(self) -> str:
        # Claude Code sends {"prompt": ...}; older builds used user_message.content.
        prompt = self.payload.get("prompt")
        if not prompt:
            message = self.payload.get("user_message")
            prompt = message.get("content") if isinstance(message, Mapping) else ""
        return str(prompt or "").strip()

    @property
    def tool(self) -> str:
        return str(self.payload.get("tool_name") or "")

    @property
    def tool_input(self) -> dict[str, Any]:
        value = self.payload.get("tool_input")
        return value if isinstance(value, dict) else {}


def record_action(call: HookCall, memory_dir: Path) -> str:
    settings = AmbientSettings.load(memory_dir)
    db_path = memory_dir / MEMORY_DB_NAME
    if not settings.action_log or not db_path.is_file():
        return ""
    action = build_action(tool=call.tool, tool_input=call.tool_input, session_id=call.session_id,
                          project=call.project, status=status_for(call.failed, call.interrupted),
                          exit_code=parse_exit_code(call.exit_code))
    if action is None:
        return ""
    tool_use_id = call.payload.get("tool_use_id")
    if isinstance(tool_use_id, str) and tool_use_id and not claim_tool_use(memory_dir, call.session_id, tool_use_id):
        log_event("action_duplicate", tool_use_id=tool_use_id, tool=call.tool)
        return ""
    db = sqlite3.connect(str(db_path), timeout=SQLITE_TIMEOUT_SEC)
    try:
        if not table_exists(db, "agent_actions"):
            log_event("action_not_logged", reason="agent_actions missing; start the TAM server once to migrate")
            return ""
        ActionLog(db).record(action, retention_days=settings.action_log_retention_days)
    finally:
        db.close()
    return ""


def prompt_context(call: HookCall, memory_dir: Path) -> str:
    from ambient.prompt_context import build_prompt_context

    result = build_prompt_context(db_path=memory_dir / MEMORY_DB_NAME, memory_dir=memory_dir,
                                  prompt=call.prompt, project=call.project,
                                  session_id=call.session_id, settings=AmbientSettings.load(memory_dir))
    log_event("ambient_prompt_context", background=result.background, rules=len(result.rule_ids),
              records=len(result.record_ids), correction_id=result.correction_id,
              dropped_floor=result.dropped_floor,
              elapsed_ms=result.elapsed_ms)
    return result.text


def rule_reminder(call: HookCall, memory_dir: Path) -> str:
    from ambient.reminders import build_reminder
    from ambient.state import SessionState

    settings = AmbientSettings.load(memory_dir)
    db_path = memory_dir / MEMORY_DB_NAME
    if not settings.rule_reminders or not db_path.is_file():
        return ""
    state = SessionState.load(memory_dir, call.session_id)
    db = sqlite3.connect(str(db_path), timeout=SQLITE_TIMEOUT_SEC)
    try:
        if not table_exists(db, "rule_surfaces"):
            return ""
        reminder = build_reminder(db, tool=call.tool, tool_input=call.tool_input, project=call.project,
                                  session_id=call.session_id, state=state, now=time.time())
    finally:
        db.close()
    if reminder.rule_ids:
        state.save()
    return reminder.text


def turn_end(call: HookCall, memory_dir: Path) -> str:
    from ambient.turn_end import run_turn_end

    message = call.payload.get("last_assistant_message")
    result = run_turn_end(db_path=memory_dir / MEMORY_DB_NAME, memory_dir=memory_dir, session_id=call.session_id,
                          project=call.project, cwd=call.cwd or os.getcwd(),
                          assistant_message=message if isinstance(message, str) else "",
                          settings=AmbientSettings.load(memory_dir))
    compliance = result.compliance
    log_event("ambient_turn_end", actions=result.actions, saved_id=result.saved_id,
              followed=len(compliance.followed) if compliance else 0,
              violated=len(compliance.violations) if compliance else 0)
    return result.notice


def codex_notify(call: HookCall, memory_dir: Path) -> str:
    from ambient.codex import capture_turn, turn_from

    turn = turn_from(call.payload, fallback_cwd=call.cwd)
    if turn is None:
        return ""
    result = capture_turn(memory_dir / MEMORY_DB_NAME, turn, AmbientSettings.load(memory_dir))
    log_event("ambient_codex_turn", project=turn.project, recorded=result.recorded, saved_id=result.saved_id)
    return ""


COMMANDS: Final[dict[str, Callable[[HookCall, Path], str]]] = {
    RECORD_ACTION: record_action,
    PROMPT_CONTEXT: prompt_context,
    RULE_REMINDER: rule_reminder,
    TURN_END: turn_end,
    CODEX_NOTIFY: codex_notify,
}


def run(command: str, call: HookCall, memory_dir: Path) -> str:
    try:
        handler = COMMANDS[command]
    except KeyError:
        raise HookCallError(f"unknown ambient command {command!r}") from None
    return handler(call, Path(memory_dir))
