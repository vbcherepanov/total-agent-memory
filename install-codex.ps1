#Requires -Version 5.1
<#
.SYNOPSIS
    Claude Total Memory — Codex CLI Installer (Windows)

.DESCRIPTION
    Creates Python venv, installs dependencies, downloads embedding model,
    and configures Codex CLI MCP server automatically.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-codex.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  Claude Total Memory v4.0 — Codex CLI Installer"       -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""

# -- Config --
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MemoryDir = if ($env:CLAUDE_MEMORY_DIR) { $env:CLAUDE_MEMORY_DIR } else { Join-Path $env:USERPROFILE ".claude-memory" }
$VenvDir = Join-Path $InstallDir ".venv"
$CodexDir = Join-Path $env:USERPROFILE ".codex"
$CodexConfig = Join-Path $CodexDir "config.toml"
$SkillTarget = Join-Path $env:USERPROFILE ".agents" "skills" "memory"

# -- 1. Create memory directories --
Write-Host "-> Step 1: Creating memory directories..." -ForegroundColor Yellow
$dirs = @("raw", "chroma", "transcripts", "queue", "backups", "extract-queue")
foreach ($d in $dirs) {
    $path = Join-Path $MemoryDir $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
}
Write-Host "  OK: $MemoryDir" -ForegroundColor Green

# -- 2. Python venv + deps --
Write-Host "-> Step 2: Setting up Python environment..." -ForegroundColor Yellow

$pythonCmd = $null
foreach ($cmd in @("python3", "python")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $major, $minor = $ver.Split(".")
            if ([int]$major -ge 3 -and [int]$minor -ge 10) {
                $pythonCmd = $cmd
                Write-Host "  Python $ver found ($cmd)" -ForegroundColor Green
                break
            }
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Host "  ERROR: Python 3.10+ not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}

$VenvPython = Join-Path $VenvDir "Scripts" "python.exe"
$VenvPip = Join-Path $VenvDir "Scripts" "pip.exe"

if (Test-Path $VenvPython) {
    Write-Host "  Existing venv found, updating dependencies..."
    & $VenvPip install -q --upgrade "mcp[cli]>=1.0.0" chromadb sentence-transformers 2>&1 | Select-Object -Last 1
} else {
    Write-Host "  Creating virtual environment..."
    & $pythonCmd -m venv $VenvDir
    if (-not (Test-Path $VenvPython)) {
        Write-Host "  ERROR: Failed to create virtual environment" -ForegroundColor Red
        exit 1
    }
    & $VenvPip install -q --upgrade pip 2>$null
    Write-Host "  Installing dependencies (this may take 2-3 minutes on first run)..."
    & $VenvPip install -q "mcp[cli]>=1.0.0" chromadb sentence-transformers 2>&1 | Select-Object -Last 1
}
Write-Host "  OK: Dependencies installed" -ForegroundColor Green

# -- 3. Pre-download embedding model --
Write-Host "-> Step 3: Loading embedding model (first time only)..." -ForegroundColor Yellow
try {
    & $VenvPython -c @"
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('all-MiniLM-L6-v2')
print(f'  OK: Model ready ({m.get_sentence_embedding_dimension()}d embeddings)')
"@ 2>$null
} catch {
    Write-Host "  WARNING: Will download on first use" -ForegroundColor DarkYellow
}

# -- 4. Configure Codex CLI MCP --
Write-Host "-> Step 4: Configuring Codex CLI MCP server..." -ForegroundColor Yellow

if (-not (Test-Path $CodexDir)) {
    New-Item -ItemType Directory -Path $CodexDir -Force | Out-Null
}

$SrvPath = Join-Path $InstallDir "src" "server.py"

# Use forward slashes for TOML (works on Windows for Python/MCP)
$PyPathToml = $VenvPython.Replace("\", "/")
$SrvPathToml = $SrvPath.Replace("\", "/")
$MemoryDirToml = $MemoryDir.Replace("\", "/")

$env:_CTM_CONFIG = $CodexConfig
$env:_CTM_PY = $PyPathToml
$env:_CTM_SRV = $SrvPathToml
$env:_CTM_MEM = $MemoryDirToml

& $VenvPython -c @"
import os, re

config_path = os.environ['_CTM_CONFIG']
# Escape backslashes and double quotes for safe TOML embedding
def toml_escape(s):
    return s.replace('\\', '/').replace('"', '\\"')
py_path = toml_escape(os.environ['_CTM_PY'])
srv_path = toml_escape(os.environ['_CTM_SRV'])
memory_dir = toml_escape(os.environ['_CTM_MEM'])

toml_block = f'''
# --- Claude Total Memory MCP Server ---
[mcp_servers.memory]
command = "{py_path}"
args = ["{srv_path}"]
required = true
startup_timeout_sec = 15.0
tool_timeout_sec = 120.0

[mcp_servers.memory.env]
CLAUDE_MEMORY_DIR = "{memory_dir}"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
# --- End Claude Total Memory ---
'''

content = ''
if os.path.exists(config_path):
    with open(config_path, 'r') as f:
        content = f.read()

if '[mcp_servers.memory]' in content:
    pattern = r'# --- Claude Total Memory MCP Server ---.*?# --- End Claude Total Memory ---'
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, toml_block.strip(), content, flags=re.DOTALL)
    else:
        content = re.sub(r'\[mcp_servers\.memory\].*?(?=\n\[|\Z)', toml_block.strip(), content, flags=re.DOTALL)
    print('  OK: Updated existing memory config in ' + config_path)
else:
    content = content.rstrip() + '\n' + toml_block
    print('  OK: Added memory config to ' + config_path)

content = content.lstrip('\n')
with open(config_path, 'w') as f:
    f.write(content)
"@

# Clean up temp env vars
Remove-Item Env:\_CTM_CONFIG -ErrorAction SilentlyContinue
Remove-Item Env:\_CTM_PY -ErrorAction SilentlyContinue
Remove-Item Env:\_CTM_SRV -ErrorAction SilentlyContinue
Remove-Item Env:\_CTM_MEM -ErrorAction SilentlyContinue

# -- 5. Install Codex Skill --
Write-Host "-> Step 5: Installing memory skill..." -ForegroundColor Yellow
$SkillSrc = Join-Path $InstallDir "codex-skill"

if (Test-Path $SkillSrc) {
    if (-not (Test-Path $SkillTarget)) {
        New-Item -ItemType Directory -Path $SkillTarget -Force | Out-Null
    }
    Copy-Item -Path "$SkillSrc\*" -Destination $SkillTarget -Recurse -Force
    Write-Host "  OK: Skill installed to $SkillTarget" -ForegroundColor Green
} else {
    Write-Host "  SKIP: codex-skill/ directory not found" -ForegroundColor DarkYellow
}

# -- 6. Dashboard service (Windows Scheduled Task) --
Write-Host "-> Step 6: Setting up dashboard service..." -ForegroundColor Yellow
$DashboardPath = Join-Path $InstallDir "src" "dashboard.py"
$LogDir = Join-Path $MemoryDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$TaskName = "ClaudeTotalMemoryDashboard"

try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}

try {
    # Create wrapper script to pass environment variables to dashboard
    $WrapperPath = Join-Path $InstallDir "start-dashboard.cmd"
    @"
@echo off
set CLAUDE_MEMORY_DIR=$MemoryDir
set DASHBOARD_PORT=37737
"$VenvPython" "$DashboardPath"
"@ | Set-Content -Path $WrapperPath -Encoding ASCII

    $Action = New-ScheduledTaskAction `
        -Execute "cmd.exe" `
        -Argument "/c `"$WrapperPath`"" `
        -WorkingDirectory $InstallDir

    $Trigger = New-ScheduledTaskTrigger -AtLogon
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Days 365)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Claude Total Memory web dashboard on port 37737" `
        -RunLevel Limited | Out-Null

    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    Write-Host "  OK: Dashboard scheduled task created (auto-starts on login)" -ForegroundColor Green
    Write-Host "  OK: http://localhost:37737" -ForegroundColor Green
} catch {
    Write-Host "  INFO: Could not create scheduled task (run as admin for auto-start)" -ForegroundColor DarkYellow
    Write-Host "  Run manually: .venv\Scripts\python.exe src\dashboard.py" -ForegroundColor DarkYellow
}

# -- 7. Verify --
Write-Host ""
Write-Host "-> Step 7: Verifying installation..." -ForegroundColor Yellow

if (Test-Path $SrvPath) {
    Write-Host "  OK: Server: $SrvPath" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Server not found at $SrvPath" -ForegroundColor Red
}

if (Test-Path $CodexConfig) {
    $configContent = Get-Content $CodexConfig -Raw
    if ($configContent -match "mcp_servers\.memory") {
        Write-Host "  OK: MCP server configured in $CodexConfig" -ForegroundColor Green
    } else {
        Write-Host "  FAIL: MCP config missing in $CodexConfig" -ForegroundColor Red
    }
} else {
    Write-Host "  FAIL: Config file not created: $CodexConfig" -ForegroundColor Red
}

if (Test-Path $MemoryDir) {
    Write-Host "  OK: Memory directory: $MemoryDir" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Memory directory issue" -ForegroundColor Red
}

if (Test-Path $SkillTarget) {
    Write-Host "  OK: Skill installed: $SkillTarget" -ForegroundColor Green
} else {
    Write-Host "  WARN: Skill not installed" -ForegroundColor DarkYellow
}

# -- Done --
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  INSTALLED SUCCESSFULLY!" -ForegroundColor Green
Write-Host ""
Write-Host "  Codex CLI now has persistent memory."
Write-Host "  Just start 'codex' as usual — memory tools are available."
Write-Host ""
Write-Host "  Available MCP tools (20):"
Write-Host "    memory_recall          — Search all past knowledge (3-level detail)"
Write-Host "    memory_save            — Save decisions, solutions, lessons"
Write-Host "    memory_update          — Update existing knowledge"
Write-Host "    memory_timeline        — Browse session history"
Write-Host "    memory_stats           — View statistics & health"
Write-Host "    memory_consolidate     — Merge similar records"
Write-Host "    memory_export          — Backup to JSON"
Write-Host "    memory_forget          — Archive stale records"
Write-Host "    memory_history         — View version history"
Write-Host "    memory_delete          — Soft-delete a record"
Write-Host "    memory_relate          — Link related records"
Write-Host "    memory_search_by_tag   — Browse by tag"
Write-Host "    memory_extract_session — Process session transcripts"
Write-Host "    memory_observe         — Lightweight file change tracking"
Write-Host "    self_error_log         — Log errors for pattern analysis"
Write-Host "    self_insight           — Manage insights from error patterns"
Write-Host "    self_rules             — Manage behavioral rules (SOUL)"
Write-Host "    self_patterns          — Analyze error patterns & trends"
Write-Host "    self_reflect           — Save session reflections"
Write-Host "    self_rules_context     — Load rules at session start"
Write-Host ""
Write-Host "  Web dashboard: http://localhost:37737"
Write-Host ""
Write-Host "  Verify in Codex: type /mcp to check memory server"
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    1. Copy AGENTS.md.template to your project as AGENTS.md"
Write-Host "    2. Copy codex-global-rules.md.template to ~\.codex\AGENTS.md"
Write-Host "    3. Restart Codex CLI"
Write-Host ""
Write-Host "  Note: If you also use Claude Code, both share the same"
Write-Host "  memory database. Don't run them simultaneously."
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
