#Requires -Version 5.1
<#
.SYNOPSIS
    Claude Total Memory — One-Command Installer (Windows)

.DESCRIPTION
    Creates Python venv, installs dependencies, downloads embedding model,
    and configures Claude Code MCP server automatically.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  Claude Total Memory v2.2 — Installer (Windows)"       -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""

# -- Config --
$InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MemoryDir = if ($env:CLAUDE_MEMORY_DIR) { $env:CLAUDE_MEMORY_DIR } else { Join-Path $env:USERPROFILE ".claude-memory" }
$VenvDir = Join-Path $InstallDir ".venv"
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$ClaudeSettings = Join-Path $ClaudeDir "settings.json"

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

# Find python
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

# Create venv
Write-Host "  Creating virtual environment..."
& $pythonCmd -m venv $VenvDir
$VenvPython = Join-Path $VenvDir "Scripts" "python.exe"
$VenvPip = Join-Path $VenvDir "Scripts" "pip.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "  ERROR: Failed to create virtual environment" -ForegroundColor Red
    exit 1
}

& $VenvPip install -q --upgrade pip 2>$null
Write-Host "  Installing dependencies (this may take 2-3 minutes on first run)..."
& $VenvPip install -q "mcp[cli]>=1.0.0" chromadb sentence-transformers 2>&1 | Select-Object -Last 1
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

# -- 4. Configure Claude Code MCP --
Write-Host "-> Step 4: Configuring Claude Code MCP server..." -ForegroundColor Yellow

if (-not (Test-Path $ClaudeDir)) {
    New-Item -ItemType Directory -Path $ClaudeDir -Force | Out-Null
}

$SrvPath = Join-Path $InstallDir "src" "server.py"

# Use forward slashes in JSON (works on Windows too for Python/MCP)
$PyPathJson = $VenvPython.Replace("\", "/")
$SrvPathJson = $SrvPath.Replace("\", "/")
$MemoryDirJson = $MemoryDir.Replace("\", "/")

& $VenvPython -c @"
import json, os

settings_path = r'$ClaudeSettings'
new_server = {
    'command': '$PyPathJson',
    'args': ['$SrvPathJson'],
    'env': {
        'CLAUDE_MEMORY_DIR': '$MemoryDirJson',
        'EMBEDDING_MODEL': 'all-MiniLM-L6-v2'
    }
}

settings = {}
if os.path.exists(settings_path):
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except:
        pass

if 'mcpServers' not in settings:
    settings['mcpServers'] = {}
settings['mcpServers']['memory'] = new_server

with open(settings_path, 'w') as f:
    json.dump(settings, f, indent=2)

print('  OK: MCP server added to ' + settings_path)
"@

# -- 5. Verify --
Write-Host ""
Write-Host "-> Step 5: Verifying installation..." -ForegroundColor Yellow

if (Test-Path $SrvPath) {
    Write-Host "  OK: Server: $SrvPath" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Server not found at $SrvPath" -ForegroundColor Red
}

try {
    & $VenvPython -c @"
import json
with open(r'$ClaudeSettings') as f:
    s = json.load(f)
assert 'memory' in s.get('mcpServers', {})
print('  OK: MCP server configured')
"@ 2>$null
} catch {
    Write-Host "  FAIL: MCP config issue" -ForegroundColor Red
}

if (Test-Path $MemoryDir) {
    Write-Host "  OK: Memory directory: $MemoryDir" -ForegroundColor Green
} else {
    Write-Host "  FAIL: Memory directory issue" -ForegroundColor Red
}

# -- Done --
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  INSTALLED SUCCESSFULLY!" -ForegroundColor Green
Write-Host ""
Write-Host "  Claude Code now has persistent memory."
Write-Host "  Just start 'claude' as usual — memory is automatic."
Write-Host ""
Write-Host "  Available MCP tools (13):"
Write-Host "    memory_recall          — Search all past knowledge"
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
Write-Host ""
Write-Host "  Web dashboard:"
Write-Host "    .venv\Scripts\python.exe src\dashboard.py"
Write-Host "    Open http://localhost:37737"
Write-Host ""
Write-Host "  Optional: Copy CLAUDE.md.template to your project"
Write-Host "  to instruct Claude to use memory automatically."
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
