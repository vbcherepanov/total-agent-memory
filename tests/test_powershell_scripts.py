"""Windows PowerShell 5.1 compatibility and runtime checks for the hook, installer and diagnose scripts.

Static checks run everywhere. Runtime checks run once per available engine (`powershell` = 5.1 on Windows,
`pwsh` = 7+) and are skipped when neither exists.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks"
SCRIPTS = {
    "inject-provenance": HOOKS / "inject-provenance.ps1",
    "codex-notify": HOOKS / "codex-notify.ps1",
    "common": HOOKS / "lib" / "common.ps1",
    "install-codex": ROOT / "install-codex.ps1",
    "diagnose": ROOT / "scripts" / "diagnose.ps1",
}
ENGINES = [path for path in (shutil.which("powershell"), shutil.which("pwsh")) if path]
needs_engine = pytest.mark.skipif(not ENGINES, reason="no powershell/pwsh on PATH")
TIMEOUT_SEC = 120

# Syntax that Windows PowerShell 5.1 rejects or silently misreads.
PS7_ONLY = {
    "null-coalescing ??": re.compile(r"\?\?"),
    "null-conditional ?. / ?[": re.compile(r"\?\.|\?\["),
    "pipeline chain && / ||": re.compile(r"(?<![\"'])(?:&&|\|\|)(?![\"'])"),
    "ternary": re.compile(r"\)\s*\?\s*[$\"'\w]|\$\w+\s*\?\s*[$\"'\w]"),
    "ForEach-Object -Parallel": re.compile(r"-Parallel\b"),
    "$IsWindows/$IsLinux/$IsMacOS": re.compile(r"\$Is(?:Windows|Linux|MacOS)\b"),
    "ConvertFrom-Json -AsHashtable": re.compile(r"-AsHashtable"),
    "utf8NoBOM encoding": re.compile(r"utf8NoBOM", re.IGNORECASE),
    "-AsByteStream": re.compile(r"-AsByteStream"),
    "Join-Path with more than one child": re.compile(
        r"""Join-Path\s+(?:\([^()]*\)|[^\s()]+)\s+(?!-)(?:"[^"]*"|'[^']*'|[^\s()|;]+)\s+(?:["'$]|[A-Za-z])"""),
}
# Every .ps1 the project ships, not only the ones exercised below.
SHIPPED = sorted([*ROOT.glob("*.ps1"), *(path for directory in ("hooks", "scripts", "tests")
                                     for path in (ROOT / directory).rglob("*.ps1"))])


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """(line number, text) without full-line comments; here-strings and strings stay, they are code."""
    return [(number, line) for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if not line.lstrip().startswith("#")]


@pytest.mark.parametrize("path", SHIPPED, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_script_uses_only_windows_powershell_51_syntax(path):
    offenders = [f"{path.name}:{number}: {label}" for number, line in _code_lines(path)
                 for label, pattern in PS7_ONLY.items() if pattern.search(line)]
    assert not offenders, offenders
    assert path.read_text(encoding="utf-8").count("\r\r") == 0


@pytest.mark.parametrize("label,sample", [
    ("null-coalescing ??", "$a = $b ?? 1"), ("null-conditional ?. / ?[", "$a?.Name"),
    ("pipeline chain && / ||", "a && b"), ("ternary", "$x = ($a -eq 1) ? 'y' : 'n'"),
    ("ForEach-Object -Parallel", "1..3 | ForEach-Object -Parallel { $_ }"),
    ("$IsWindows/$IsLinux/$IsMacOS", "if ($IsWindows) { }"),
    ("Join-Path with more than one child", '$p = Join-Path $env:USERPROFILE ".agents" "skills"'),
])
def test_the_ps7_detector_flags_ps7_only_syntax(label, sample):
    assert PS7_ONLY[label].search(sample)


@pytest.mark.parametrize("path", SHIPPED, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_script_is_ascii_because_ps51_reads_bomless_files_as_ansi(path):
    # An em dash is E2 80 94 in UTF-8; cp1252 maps 0x94 to a closing smart quote, which ends a PowerShell string.
    text = path.read_text(encoding="utf-8")
    assert [c for c in text if ord(c) > 127] == []


def _run(engine: str, args: list[str], env: dict[str, str], stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([engine, "-NoProfile", "-ExecutionPolicy", "Bypass", *args], input=stdin.encode(),
                          env=env, capture_output=True, timeout=TIMEOUT_SEC, check=False)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("TAM_", "CLAUDE_MEMORY", "HOOK_"))}
    base.update({"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "HOOK_PYTHON": sys.executable,
                 "TAM_MEMORY_DIR": str(tmp_path / "mem"), **extra})
    return base


PARSE = ("$errors = $null; $tokens = $null; "
         "[void][System.Management.Automation.Language.Parser]::ParseFile($env:PS_UNDER_TEST, [ref]$tokens, [ref]$errors); "
         "$errors | ForEach-Object { Write-Output ('PARSE_ERROR ' + $_.Extent.StartLineNumber + ': ' + $_.Message) }; "
         "Write-Output ('PARSED ' + @($errors).Count)")


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", SCRIPTS)
def test_script_parses_with_the_engine(engine, name, tmp_path):
    proc = _run(engine, ["-Command", PARSE], _env(tmp_path, PS_UNDER_TEST=str(SCRIPTS[name])))
    assert proc.stdout.decode().strip().endswith("PARSED 0"), proc.stdout.decode() + proc.stderr.decode()


def _register_tam_server(home: Path) -> dict:
    entry = {"command": "tam-server", "args": ["--stdio"], "env": {"SECRET": "x"}}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"memory": entry}}), encoding="utf-8")
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "tam-provenance.json").write_text(
        json.dumps({"servers": {"memory": {"command": "tam-server", "args": ["--stdio"]}}}), encoding="utf-8")
    return entry


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
def test_inject_provenance_ps1_injects_for_the_tam_server_only(engine, tmp_path):
    _register_tam_server(tmp_path)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "mcp__memory__memory_save", "session_id": "s",
               "tool_use_id": "toolu_9", "cwd": str(tmp_path), "tool_input": {"content": "c"}}
    script = str(SCRIPTS["inject-provenance"])
    proc = _run(engine, ["-File", script], _env(tmp_path), json.dumps(payload))
    log = tmp_path / "mem" / "hooks.log"
    diagnostics = proc.stderr.decode() + (log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")
    assert proc.returncode == 0 and proc.stdout.strip(), diagnostics
    out = json.loads(proc.stdout.decode())["hookSpecificOutput"]
    assert out["updatedInput"]["content"] == "c" and out["updatedInput"]["_ctx"]["tool_use_id"] == "toolu_9"
    assert "permissionDecision" not in out
    other = {**payload, "tool_name": "Read", "tool_input": {}}
    silent = _run(engine, ["-File", script], _env(tmp_path), json.dumps(other))
    assert silent.returncode == 0 and silent.stdout.strip() == b""


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
def test_inject_provenance_ps1_leaves_a_foreign_memory_server_alone(engine, tmp_path):
    _register_tam_server(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"memory": {"command": "npx", "args": ["foreign-memory"]}}}), encoding="utf-8")
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps({"enabledMcpjsonServers": ["memory"]}),
                                                              encoding="utf-8")
    payload = {"hook_event_name": "PreToolUse", "tool_name": "mcp__memory__memory_save", "session_id": "s",
               "cwd": str(tmp_path), "tool_input": {"content": "c"}}
    proc = _run(engine, ["-File", str(SCRIPTS["inject-provenance"])], _env(tmp_path), json.dumps(payload))
    assert proc.returncode == 0 and proc.stdout.strip() == b""


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
def test_common_ps1_exposes_payload_fields_and_resolves_the_memory_dir(engine, tmp_path):
    probe = tmp_path / "probe.ps1"
    probe.write_text('. "' + str(SCRIPTS["common"]) + '"\n'
                     "Write-Output (Get-HookField 'tool_input.file_path')\n"
                     "Write-Output $HookEvent\n"
                     "Write-Output $env:TAM_MEMORY_DIR\n", encoding="ascii")
    payload = {"hook_event_name": "PreToolUse", "tool_input": {"file_path": "a.py"}}
    proc = _run(engine, ["-File", str(probe)], _env(tmp_path), json.dumps(payload))
    lines = proc.stdout.decode().split()
    assert proc.returncode == 0, proc.stderr.decode()
    assert lines[:2] == ["a.py", "PreToolUse"] and lines[2] == str(tmp_path / "mem")


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
def test_codex_notify_ps1_ignores_an_empty_payload_and_accepts_an_argument(engine, tmp_path):
    script = str(SCRIPTS["codex-notify"])
    env = _env(tmp_path, TAM_HOOK_NOTIFY="0")
    assert _run(engine, ["-File", script], env, "").returncode == 0
    note = json.dumps({"type": "something-else"})
    proc = _run(engine, ["-File", script, note], env)
    assert proc.returncode == 0, proc.stderr.decode()


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
def test_diagnose_ps1_test_mode_and_real_run_report(engine, tmp_path):
    # InstallDir is derived from the script location: run a copy so a repo-level .venv cannot satisfy the checks.
    (tmp_path / "scripts").mkdir()
    copy = tmp_path / "scripts" / "diagnose.ps1"
    shutil.copy(SCRIPTS["diagnose"], copy)
    script = str(copy)
    mock = _run(engine, ["-File", script], _env(tmp_path, DIAG_TEST_MODE="1"))
    assert mock.returncode == 0 and "Result: 7/7 passed" in mock.stdout.decode()
    real = _run(engine, ["-File", script], _env(tmp_path, CLAUDE_MEMORY_DIR=str(tmp_path / "mem"),
                                                DASHBOARD_PORT="1", OLLAMA_HOST="http://127.0.0.1:1"))
    text = real.stdout.decode()
    assert real.returncode == 1 and "FAIL Python venv missing" in text and "FAIL DB missing" in text
    assert "Result: one or more checks FAILED" in text


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
def test_install_codex_ps1_stops_cleanly_without_python(engine, tmp_path):
    memory = tmp_path / "mem"
    env = _env(tmp_path, CLAUDE_MEMORY_DIR=str(memory))
    env["PATH"] = str(Path(engine).resolve().parent)
    proc = _run(engine, ["-File", str(SCRIPTS["install-codex"])], env)
    assert proc.returncode == 1, proc.stdout.decode() + proc.stderr.decode()
    assert "Python 3.11+ not found" in proc.stdout.decode()
    assert (memory / "raw").is_dir() and (memory / "extract-queue").is_dir()


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
def test_diagnose_ps1_finds_the_db_in_a_fresh_tam_dir(engine, tmp_path):
    (tmp_path / "scripts").mkdir()
    copy = tmp_path / "scripts" / "diagnose.ps1"
    shutil.copy(SCRIPTS["diagnose"], copy)
    (tmp_path / ".tam").mkdir()
    (tmp_path / ".tam" / "memory.db").write_bytes(b"")
    env = _env(tmp_path, DASHBOARD_PORT="1", OLLAMA_HOST="http://127.0.0.1:1")
    del env["TAM_MEMORY_DIR"]
    text = _run(engine, ["-File", str(copy)], env).stdout.decode()
    assert "DB missing" not in text


@needs_engine
@pytest.mark.parametrize("engine", ENGINES)
def test_diagnose_ps1_warns_when_the_installed_dir_has_no_db_yet(engine, tmp_path):
    (tmp_path / "scripts").mkdir()
    copy = tmp_path / "scripts" / "diagnose.ps1"
    shutil.copy(SCRIPTS["diagnose"], copy)
    (tmp_path / ".tam").mkdir()
    env = _env(tmp_path, DASHBOARD_PORT="1", OLLAMA_HOST="http://127.0.0.1:1")
    del env["TAM_MEMORY_DIR"]
    text = _run(engine, ["-File", str(copy)], env).stdout.decode()
    assert "WARN DB not created yet" in text and "DB missing" not in text


@pytest.mark.parametrize("sample", [
    'Join-Path (Join-Path $root "src") "server.py"', 'Join-Path $root -ChildPath "src"',
    'Join-Path $root "src" -Resolve', 'Join-Path $root "src" | Out-Null', "$wrapper = [System.Text.UTF8Encoding]::new($false)",
])
def test_the_ps7_detector_accepts_windows_powershell_51_forms(sample):
    assert not [label for label, pattern in PS7_ONLY.items() if pattern.search(sample)]


INSTALL_PS1 = ROOT / "install.ps1"


def test_install_ps1_stops_a_task_before_unregistering_it():
    # Unregister-ScheduledTask alone leaves a started dashboard running after -Uninstall or a reinstall.
    text = INSTALL_PS1.read_text(encoding="utf-8")
    start = text.index("function Remove-TamScheduledTask")
    body = text[start:text.index("\n}\n", start)]
    assert body.index("Stop-ScheduledTask") < body.index("Unregister-ScheduledTask")
    outside = text[:start] + text[start + len(body):]
    assert "Unregister-ScheduledTask" not in "\n".join(line for _, line in _code_lines_of(outside))


def _code_lines_of(text: str) -> list[tuple[int, str]]:
    return [(number, line) for number, line in enumerate(text.splitlines(), 1) if not line.lstrip().startswith("#")]


EXTRACT_AND_CALL = r"""
$errors = $null; $tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:INSTALL_PS1, [ref]$tokens, [ref]$errors)
$fn = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Remove-TamScheduledTask' }, $true) | Select-Object -First 1
Invoke-Expression $fn.Extent.Text
$name = $env:TASK_NAME
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -Command Start-Sleep -Seconds 300; '$name'"
Register-ScheduledTask -TaskName $name -Action $action -RunLevel Limited | Out-Null
Start-ScheduledTask -TaskName $name
for ($i = 0; $i -lt 50 -and (Get-ScheduledTask -TaskName $name).State -ne 'Running'; $i++) { Start-Sleep -Milliseconds 200 }
$before = (Get-ScheduledTask -TaskName $name).State
$removed = Remove-TamScheduledTask $name
Start-Sleep -Seconds 2
$left = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$name*" -and $_.Name -eq 'powershell.exe' -and $_.ProcessId -ne $PID }).Count
$task = @(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue).Count
$again = Remove-TamScheduledTask $name
"before=$before removed=$removed left=$left task=$task again=$again"
"""


@pytest.mark.skipif(sys.platform != "win32" or not ENGINES, reason="Windows Task Scheduler")
def test_remove_tam_scheduled_task_stops_the_running_instance(tmp_path):
    name = f"TamTaskStopCheck{os.getpid()}"
    script = tmp_path / "check.ps1"
    script.write_text(EXTRACT_AND_CALL, encoding="utf-8")
    env = {**os.environ, "INSTALL_PS1": str(INSTALL_PS1), "TASK_NAME": name}
    proc = _run(ENGINES[0], ["-File", str(script)], env)
    out = proc.stdout.decode(errors="replace")
    assert "before=Running removed=True left=0 task=0 again=False" in out, out + proc.stderr.decode(errors="replace")
