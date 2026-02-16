#!/usr/bin/env bash
#
# PostToolUse:Bash hook — suggest memory_save after significant operations
#
# Triggers on: git commit, docker compose, migrations, make setup
#
# Add to ~/.claude/settings.json:
#   "hooks": {
#     "PostToolUse": [{
#       "type": "command",
#       "command": "/path/to/claude-memory/hooks/memory-trigger.sh",
#       "matcher": "Bash"
#     }]
#   }

# Read tool input from stdin (JSON)
INPUT=$(cat)

# Extract the command that was run
COMMAND=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('input',{}).get('command',''))" 2>/dev/null)

if [ -z "$COMMAND" ]; then
    exit 0
fi

# Check for significant operations
case "$COMMAND" in
    *"git commit"*)
        echo "MEMORY_HINT: Git commit detected. Consider saving the commit scope and key changes with memory_save(type='fact')."
        ;;
    *"docker compose up"*|*"docker-compose up"*)
        echo "MEMORY_HINT: Docker environment started. Consider saving infrastructure config with memory_save(type='fact')."
        ;;
    *"migrate"*|*"migration"*)
        echo "MEMORY_HINT: Database migration detected. Consider saving schema changes with memory_save(type='fact')."
        ;;
    *"make setup"*|*"make init"*)
        echo "MEMORY_HINT: Project setup detected. Consider saving infrastructure facts with memory_save(type='fact')."
        ;;
esac
