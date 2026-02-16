# PostToolUse:Bash hook — suggest memory_save after significant operations
#
# Triggers on: git commit, docker compose, migrations, make setup
#
# Add to %USERPROFILE%\.claude\settings.json:
#   "hooks": {
#     "PostToolUse": [{
#       "type": "command",
#       "command": "powershell -ExecutionPolicy Bypass -File C:\\path\\to\\claude-total-memory\\hooks\\memory-trigger.ps1",
#       "matcher": "Bash"
#     }]
#   }

# Read tool input from stdin (JSON)
$input_json = $input | Out-String

try {
    $data = $input_json | ConvertFrom-Json
    $command = $data.input.command
} catch {
    exit 0
}

if (-not $command) { exit 0 }

if ($command -match "git commit") {
    Write-Output "MEMORY_HINT: Git commit detected. Consider saving the commit scope and key changes with memory_save(type='fact')."
}
elseif ($command -match "docker.compose up|docker-compose up") {
    Write-Output "MEMORY_HINT: Docker environment started. Consider saving infrastructure config with memory_save(type='fact')."
}
elseif ($command -match "migrate|migration") {
    Write-Output "MEMORY_HINT: Database migration detected. Consider saving schema changes with memory_save(type='fact')."
}
elseif ($command -match "make setup|make init") {
    Write-Output "MEMORY_HINT: Project setup detected. Consider saving infrastructure facts with memory_save(type='fact')."
}
