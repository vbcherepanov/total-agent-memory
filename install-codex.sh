#!/usr/bin/env bash
# Backward-compat shim. Use: install.sh --ide codex
exec "$(dirname "$0")/install.sh" --ide codex "$@"
