#Requires -Version 5.1
<#
.SYNOPSIS
    total-agent-memory - One-Command Installer (Windows, multi-IDE)

.DESCRIPTION
    Creates Python venv, installs dependencies, downloads embedding model,
    registers the MCP server with the chosen IDE, installs v8.0 hooks and
    configures Windows Task Scheduler background tasks for reflection,
    orphan-backfill and check-updates.

.PARAMETER Ide
    Target IDE: claude-code (default), claude-desktop, cursor, gemini-cli, opencode, codex, cline, continue,
    windsurf, aider. Registration is done by src/setup_wizard/register.py, shared with install.sh and the wizard.

.PARAMETER Uninstall
    Remove scheduled tasks and MCP entries (leaves venv and memory.db).

.PARAMETER TestMode
    Skip pip install, embedding model download, Task Scheduler registration
    and dashboard service. Used by test harness.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Ide cursor
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>

param(
    [ValidateSet("claude-code", "claude-desktop", "cursor", "gemini-cli", "opencode", "codex", "cline", "continue", "windsurf", "aider")]
    [string]$Ide = "claude-code",
    [switch]$Uninstall,
    [switch]$TestMode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Utf8File {
    param([string]$Path, [string]$Content)
    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

# TestMode can also be forced via env (parity with INSTALL_TEST_MODE=1 in bash)
if (-not $TestMode -and $env:INSTALL_TEST_MODE -eq "1") {
    $TestMode = $true
}

$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$versionSource = Get-Content ([System.IO.Path]::Combine($InstallDir, "src", "version.py")) -Raw
if ($versionSource -notmatch 'VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"') {
    throw "Cannot read release version from src/version.py"
}
$ReleaseVersion = $Matches[1]

Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  total-agent-memory v$ReleaseVersion - Installer (Windows)" -ForegroundColor Cyan
Write-Host "  IDE: $Ide$(if ($TestMode) {' [TEST MODE]'})"            -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""

# -- Config --
$HomeDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $env:HOME }
# The registration module resolves the home directory with Python's Path.home(): USERPROFILE on
# Windows, HOME elsewhere (pwsh on macOS/Linux). It must see the same home as this script, or the
# entry lands in another user's configs.
$env:HOME = $HomeDir
$MemoryDir = if ($env:TAM_MEMORY_DIR) {
    $env:TAM_MEMORY_DIR
} elseif ($env:CLAUDE_MEMORY_DIR) {
    $env:CLAUDE_MEMORY_DIR
} elseif (Test-Path ([System.IO.Path]::Combine($HomeDir, ".tam"))) {
    [System.IO.Path]::Combine($HomeDir, ".tam")
} elseif (Test-Path ([System.IO.Path]::Combine($HomeDir, ".claude-memory"))) {
    [System.IO.Path]::Combine($HomeDir, ".claude-memory")
} else {
    [System.IO.Path]::Combine($HomeDir, ".tam")
}
$env:TAM_MEMORY_DIR = $MemoryDir
$env:CLAUDE_MEMORY_DIR = $MemoryDir
$VenvDir = [System.IO.Path]::Combine($InstallDir, ".venv")

# Scheduled task names (used in both install + uninstall paths)
$TaskReflection     = "total-agent-memory-reflection"
$TaskOrphanBackfill = "total-agent-memory-orphan-backfill"
$TaskCheckUpdates   = "total-agent-memory-check-updates"
$TaskDashboard      = "ClaudeTotalMemoryDashboard"

# ===================================================================
# Uninstall branch (short-circuits before Python bootstrap)
# ===================================================================
# Stop a running instance before unregistering: Unregister-ScheduledTask alone leaves the
# started process (the dashboard) running, holding the port and the install directory.
function Remove-TamScheduledTask ([string]$Name) {
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { return $false }
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { return $false }
    if ($task.State -eq "Running") { Stop-ScheduledTask -TaskName $Name }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    return $true
}

function Invoke-Uninstall {
    Write-Host "-> Uninstalling total-agent-memory (config + scheduled tasks)..." -ForegroundColor Yellow

    # Scheduled tasks
    foreach ($t in @($TaskReflection, $TaskOrphanBackfill, $TaskCheckUpdates, $TaskDashboard)) {
        try {
            if (Remove-TamScheduledTask $t) {
                Write-Host "  OK: Stopped and removed scheduled task $t" -ForegroundColor Green
            } else {
                Write-Host "  SKIP: $t (not registered)" -ForegroundColor DarkYellow
            }
        } catch {
            Write-Host "  WARN: Could not remove scheduled task ${t}: $($_.Exception.Message)" -ForegroundColor DarkYellow
        }
    }

    # MCP entry + hooks: the registration module removes exactly what it registered.
    $uninstallPython = [System.IO.Path]::Combine($VenvDir, "Scripts", "python.exe")
    if (Test-Path $uninstallPython) {
        $previousPythonPath = $env:PYTHONPATH
        $env:PYTHONPATH = [System.IO.Path]::Combine($InstallDir, "src")
        try {
            & $uninstallPython -m setup_wizard.register --unregister --client $Ide
            if ($LASTEXITCODE -ne 0) {
                Write-Host "  WARN: Could not remove the memory entry for $Ide (see the message above)" -ForegroundColor DarkYellow
            }
        } finally {
            $env:PYTHONPATH = $previousPythonPath
        }
    } else {
        Write-Host "  SKIP: venv not found; remove the 'memory' MCP entry from your $Ide config by hand" -ForegroundColor DarkYellow
    }

    Write-Host ""
    Write-Host "  Uninstall complete. Venv and memory.db were left intact." -ForegroundColor Green
    Write-Host "  Delete manually if desired:" -ForegroundColor DarkGray
    Write-Host "    $VenvDir" -ForegroundColor DarkGray
    Write-Host "    $MemoryDir" -ForegroundColor DarkGray
    Write-Host ""
}

if ($Uninstall) {
    Invoke-Uninstall
    exit 0
}

# ===================================================================
# 1. Memory directories
# ===================================================================
Write-Host "-> Step 1: Creating memory directories..." -ForegroundColor Yellow
$dirs = @("raw", "chroma", "transcripts", "queue", "backups", "extract-queue", "logs")
foreach ($d in $dirs) {
    $path = [System.IO.Path]::Combine($MemoryDir, $d)
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
}
Write-Host "  OK: $MemoryDir" -ForegroundColor Green

# ===================================================================
# 2. Python venv + deps
# ===================================================================
Write-Host "-> Step 2: Setting up Python environment..." -ForegroundColor Yellow

$pythonCmd = $null
# "python" first: on Windows it is the interpreter on PATH (python.org, venv,
# setup-python), while "python3" is often the Microsoft Store alias or another install.
foreach ($cmd in @("python", "python3")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $parts = $ver.Split(".")
            $major = [int]$parts[0]; $minor = [int]$parts[1]
            if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                $pythonCmd = $cmd
                Write-Host "  Python $ver found ($cmd)" -ForegroundColor Green
                break
            }
        }
    } catch {}
}

if (-not $pythonCmd) {
    Write-Host "  ERROR: Python 3.11+ not found. Install from https://python.org" -ForegroundColor Red
    exit 1
}

$VenvPython = [System.IO.Path]::Combine($VenvDir, "Scripts", "python.exe")

if ($TestMode) {
    Write-Host "  SKIP (test mode): venv creation and pip install" -ForegroundColor DarkYellow
    # Use system python so downstream config steps still resolve a path
    try {
        $VenvPython = (Get-Command $pythonCmd -ErrorAction Stop).Source
    } catch {
        $VenvPython = $pythonCmd
    }
} else {
    if (-not (Test-Path $VenvPython)) {
        Write-Host "  Creating virtual environment..."
        & $pythonCmd -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed" }
    }
    if (-not (Test-Path $VenvPython)) {
        Write-Host "  ERROR: Failed to create virtual environment" -ForegroundColor Red
        exit 1
    }
    & $VenvPython -m pip install -q --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
    Write-Host "  Installing dependencies (this may take 2-3 minutes on first run)..."
    $req = [System.IO.Path]::Combine($InstallDir, "requirements.txt")
    & $VenvPython -m pip install -q -r $req -e $InstallDir
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
    Write-Host "  OK: Dependencies installed" -ForegroundColor Green
}

$SrvPath = [System.IO.Path]::Combine($InstallDir, "src", "server.py")

# ===================================================================
# 3. Pre-download embedding model
# ===================================================================
Write-Host "-> Step 3: Loading embedding model (first time only)..." -ForegroundColor Yellow
if ($TestMode) {
    Write-Host "  SKIP (test mode): embedding model pre-download" -ForegroundColor DarkYellow
} else {
    try {
        & $VenvPython -c @"
import os
from fastembed import TextEmbedding
name = os.environ.get('FASTEMBED_MODEL', 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
TextEmbedding(name)
print(f'  OK: Model ready ({name})')
"@ 2>$null
    } catch {
        Write-Host "  WARNING: Will download on first use" -ForegroundColor DarkYellow
    }
}

# ===================================================================
# 4. Register the MCP server (src/setup_wizard/register.py is the single
#    implementation shared with install.sh, the wizard and the npm wrapper)
# ===================================================================
function Register-Mcp {
    param([Parameter(Mandatory=$true)][string]$Client)
    Write-Host "-> Step 4: Registering the MCP server with $Client..." -ForegroundColor Yellow
    $registerArgs = @("-m", "setup_wizard.register", "--client", $Client, "--memory-dir", $MemoryDir,
                      "--command", $VenvPython, "--arg", $SrvPath, "--env", "CLAUDE_MEMORY_DIR=$MemoryDir")
    if ($Client -eq "codex") {
        $registerArgs += @("--env", "MEMORY_TRIPLE_TIMEOUT_SEC=120", "--env", "MEMORY_ENRICH_TIMEOUT_SEC=90",
                           "--env", "MEMORY_REPR_TIMEOUT_SEC=120", "--env", "MEMORY_TRIPLE_MAX_PREDICT=512")
    }
    if ($Client -eq "claude-code") { $registerArgs += "--hooks" } else { $registerArgs += "--no-hooks" }
    if ($env:INSTALL_OVERWRITE_HOOKS -eq "1") { $registerArgs += "--overwrite-hooks" }
    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = [System.IO.Path]::Combine($InstallDir, "src")
    try {
        & $VenvPython @registerArgs
        if ($LASTEXITCODE -ne 0) { throw "MCP registration for $Client failed (see the message above)" }
    } finally {
        $env:PYTHONPATH = $previousPythonPath
    }
}

Register-Mcp -Client $Ide

# ===================================================================
# 5. Background scheduled tasks (Task Scheduler, Windows analogue
#    of macOS LaunchAgents / Linux systemd)
# ===================================================================
function Register-BackgroundTask {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Description,
        [Parameter(Mandatory=$true)][string]$ScriptPath,
        [string[]]$ScriptArgs = @(),
        [Parameter(Mandatory=$true)]$Trigger
    )

    if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
        Write-Host "  WARN: ScheduledTasks module unavailable, skipping $Name" -ForegroundColor DarkYellow
        return
    }

    Remove-TamScheduledTask $Name | Out-Null

    $launcher = Write-PythonLauncher -FileName "$Name.py" -ScriptPath $ScriptPath -ScriptArgs $ScriptArgs
    $action = New-ScheduledTaskAction -Execute $VenvPython -Argument "`"$launcher`"" -WorkingDirectory $InstallDir
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    Register-ScheduledTask `
        -TaskName $Name `
        -Description $Description `
        -Action $action `
        -Trigger $Trigger `
        -Settings $settings `
        -RunLevel Limited | Out-Null
    Write-Host "  OK: Scheduled task $Name registered" -ForegroundColor Green
}

function Install-BackgroundTasks {
    Write-Host "-> Step 5: Registering background scheduled tasks..." -ForegroundColor Yellow

    $reflectionScript = [System.IO.Path]::Combine($InstallDir, "src", "tools", "run_reflection.py")
    $orphanScript     = [System.IO.Path]::Combine($InstallDir, "src", "tools", "backfill_orphan_edges.py")
    $updateScript     = [System.IO.Path]::Combine($InstallDir, "src", "tools", "check_updates.py")

    # Reflection: every 5 minutes, repeated, start when available.
    # (Windows has no native file-watch trigger like launchd's WatchPaths; a
    #  companion watch-reflect.ps1 daemon could upgrade this later. For now
    #  periodic polling - run_reflection.py has its own debounce.)
    $tReflection = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 365)
    Register-BackgroundTask -Name $TaskReflection `
        -Description "total-agent-memory reflection runner (periodic)" `
        -ScriptPath $reflectionScript `
        -ScriptArgs @("--scope=auto") `
        -Trigger $tReflection

    # Orphan-backfill: daily at 00:00, repeat every 6h (4 fires/day)
    $tOrphan = New-ScheduledTaskTrigger -Daily -At "00:00"
    $tOrphan.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Hours 6) `
        -RepetitionDuration (New-TimeSpan -Days 365)).Repetition
    Register-BackgroundTask -Name $TaskOrphanBackfill `
        -Description "total-agent-memory orphan-edge backfill (4x daily)" `
        -ScriptPath $orphanScript `
        -ScriptArgs @("--min-mentions=1", "--limit=500", "--trigger-now") `
        -Trigger $tOrphan

    # Check-updates: weekly Monday 09:00
    $tUpdates = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "09:00"
    Register-BackgroundTask -Name $TaskCheckUpdates `
        -Description "total-agent-memory weekly update check" `
        -ScriptPath $updateScript `
        -Trigger $tUpdates
}

function Write-PythonLauncher {
    param(
        [Parameter(Mandatory=$true)][string]$FileName,
        [Parameter(Mandatory=$true)][string]$ScriptPath,
        [string[]]$ScriptArgs = @()
    )
    $wrapperPath = [System.IO.Path]::Combine($MemoryDir, $FileName)
    $scriptLiteral = $ScriptPath | ConvertTo-Json -Compress
    $argumentsLiteral = ConvertTo-Json -InputObject @($ScriptArgs) -Compress
    $sourceLiteral = [System.IO.Path]::Combine($InstallDir, "src") | ConvertTo-Json -Compress
    $memoryPath = $MemoryDir | ConvertTo-Json -Compress
    $dashboardPort = if ($env:DASHBOARD_PORT) { $env:DASHBOARD_PORT } else { "37737" }
    $portLiteral = $dashboardPort | ConvertTo-Json -Compress
    $wrapperContent = @"
import os
import runpy
import sys

os.environ.update(TAM_MEMORY_DIR=$memoryPath, CLAUDE_MEMORY_DIR=$memoryPath, DASHBOARD_PORT=$portLiteral)
sys.path.insert(0, $sourceLiteral)
sys.argv = [$scriptLiteral] + $argumentsLiteral
runpy.run_path($scriptLiteral, run_name="__main__")
"@
    Write-Utf8File -Path $wrapperPath -Content $wrapperContent
    return $wrapperPath
}

function Write-DashboardLauncher {
    return Write-PythonLauncher -FileName "start-dashboard.py" `
        -ScriptPath ([System.IO.Path]::Combine($InstallDir, "src", "dashboard.py"))
}

function Install-DashboardService {
    Write-Host "-> Step 5b: Setting up dashboard service..." -ForegroundColor Yellow

    try {
        Remove-TamScheduledTask $TaskDashboard | Out-Null
        $WrapperPath = Write-DashboardLauncher

        $Action = New-ScheduledTaskAction -Execute $VenvPython `
            -Argument "`"$WrapperPath`"" -WorkingDirectory $InstallDir
        $Trigger = New-ScheduledTaskTrigger -AtLogon -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
        $Settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Days 365)

        Register-ScheduledTask -TaskName $TaskDashboard -Action $Action `
            -Trigger $Trigger -Settings $Settings `
            -Description "total-agent-memory web dashboard" `
            -RunLevel Limited | Out-Null

        Start-ScheduledTask -TaskName $TaskDashboard -ErrorAction SilentlyContinue
        Write-Host "  OK: Dashboard scheduled task created (auto-starts on login)" -ForegroundColor Green
    } catch {
        Write-Host "  WARN: Dashboard task failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
        Write-Host "  Run manually: .venv\Scripts\python.exe src\dashboard.py" -ForegroundColor DarkYellow
    }
}

if ($TestMode) {
    if ($Ide -eq "claude-code") { $null = Write-DashboardLauncher }
    Write-Host "-> Step 5: SKIP (test mode) scheduled tasks + dashboard service" -ForegroundColor DarkYellow
} else {
    try {
        Install-BackgroundTasks
    } catch {
        Write-Host "  WARN: Background task registration failed ($($_.Exception.Message))" -ForegroundColor DarkYellow
    }
    if ($Ide -eq "claude-code") {
        try { Install-DashboardService } catch {
            Write-Host "  WARN: Dashboard service install failed ($($_.Exception.Message))" -ForegroundColor DarkYellow
        }
    }
}

# ===================================================================
# 6. Verify
# ===================================================================
Write-Host ""
Write-Host "-> Step 6: Verifying installation..." -ForegroundColor Yellow

if (Test-Path $SrvPath) {
    Write-Host "  OK: Server: $SrvPath" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Server not found at $SrvPath" -ForegroundColor Red
}

if (Test-Path $MemoryDir) {
    Write-Host "  OK: Memory directory: $MemoryDir" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Memory directory issue" -ForegroundColor Red
}

# ===================================================================
# Done
# ===================================================================
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  INSTALLED SUCCESSFULLY (IDE: $Ide)" -ForegroundColor Green
Write-Host ""
switch ($Ide) {
    "claude-code"    { Write-Host "  Claude Code now has persistent memory + v8.0 hooks." }
    "claude-desktop" { Write-Host "  Claude Desktop now has persistent memory. Quit and reopen it." }
    "cline"          { Write-Host "  Cline now has persistent memory. Reload the VS Code window." }
    "continue"       { Write-Host "  Continue now has persistent memory. Reload your IDE." }
    "windsurf"       { Write-Host "  Windsurf now has persistent memory. Restart Windsurf." }
    "aider"          { Write-Host "  Aider now reads the memory-protocol skill (no MCP)." }
    "cursor"      { Write-Host "  Cursor now has persistent memory. Restart Cursor." }
    "gemini-cli"  { Write-Host "  Gemini CLI now has persistent memory. Restart 'gemini'." }
    "opencode"    { Write-Host "  OpenCode now has persistent memory. Restart 'opencode'." }
    "codex"       { Write-Host "  Codex CLI now has persistent memory. Type /mcp to verify." }
}
Write-Host ""
# The dashboard task is registered with Claude Code only; other clients get the manual command.
if ($Ide -eq "claude-code" -and -not $TestMode) {
    Write-Host "  Web dashboard: http://localhost:37737"
} else {
    Write-Host "  Web dashboard (not started by this install; start it when you need it):"
    Write-Host "    .venv\Scripts\python.exe src\dashboard.py   then open http://localhost:37737"
}
Write-Host ""
Write-Host "  Scheduled tasks (PowerShell, as current user):"
Write-Host "    Get-ScheduledTask -TaskName $TaskReflection"
Write-Host "    Get-ScheduledTask -TaskName $TaskOrphanBackfill"
Write-Host "    Get-ScheduledTask -TaskName $TaskCheckUpdates"
Write-Host ""
Write-Host "  Uninstall (config + tasks, leaves venv/memory.db):"
Write-Host "    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall"
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
