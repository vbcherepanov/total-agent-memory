#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  dashboard-service.sh install <py_path> <install_dir> <memory_dir>
  dashboard-service.sh print-management

Environment overrides for testing:
  CTM_UNAME=Darwin|Linux
  CTM_FORCE_WSL=1
EOF
}

platform_name() {
    if [ -n "${CTM_UNAME:-}" ]; then
        printf '%s\n' "$CTM_UNAME"
    else
        uname
    fi
}

is_wsl() {
    [ "${CTM_FORCE_WSL:-0}" = "1" ] || \
    grep -qi microsoft /proc/version 2>/dev/null || \
    [ -n "${WSL_DISTRO_NAME:-}" ]
}

systemd_user_available() {
    command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

install_service() {
    if [ "$#" -ne 3 ]; then
        usage
        exit 1
    fi

    py_path="$1"
    install_dir="$2"
    memory_dir="$3"
    dashboard_path="$install_dir/src/dashboard.py"
    dashboard_port="${DASHBOARD_PORT:-37737}"
    log_dir="$memory_dir/logs"
    plist_name="com.claude-total-memory.dashboard"
    plist_path="$HOME/Library/LaunchAgents/$plist_name.plist"
    systemd_name="claude-total-memory-dashboard.service"
    systemd_dir="$HOME/.config/systemd/user"
    systemd_path="$systemd_dir/$systemd_name"
    autostart_script="$memory_dir/dashboard-autostart.sh"
    profile_path="$HOME/.profile"

    mkdir -p "$log_dir"

    if [ "$(platform_name)" = "Darwin" ]; then
        mkdir -p "$(dirname "$plist_path")"
        launchctl bootout "gui/$(id -u)/$plist_name" 2>/dev/null || true

        cat > "$plist_path" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$plist_name</string>
    <key>ProgramArguments</key>
    <array>
        <string>$py_path</string>
        <string>$dashboard_path</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLAUDE_MEMORY_DIR</key>
        <string>$memory_dir</string>
        <key>DASHBOARD_PORT</key>
        <string>$dashboard_port</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$log_dir/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>$log_dir/dashboard.err</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST

        launchctl bootstrap "gui/$(id -u)" "$plist_path" 2>/dev/null || \
        launchctl load "$plist_path" 2>/dev/null || true
        echo "  OK: Dashboard service installed (auto-starts on login)"
        echo "  OK: http://localhost:$dashboard_port"
    elif systemd_user_available; then
        mkdir -p "$systemd_dir"
        cat > "$systemd_path" <<UNIT
[Unit]
Description=Claude Total Memory Dashboard
After=default.target

[Service]
Type=simple
ExecStart=$py_path $dashboard_path
Restart=on-failure
RestartSec=5
Environment=CLAUDE_MEMORY_DIR=$memory_dir
Environment=DASHBOARD_PORT=$dashboard_port
WorkingDirectory=$install_dir
StandardOutput=append:$log_dir/dashboard.log
StandardError=append:$log_dir/dashboard.err

[Install]
WantedBy=default.target
UNIT

        systemctl --user daemon-reload
        systemctl --user enable --now "$systemd_name" >/dev/null 2>&1 || true
        echo "  OK: Dashboard service installed with systemd --user"
        echo "  OK: http://localhost:$dashboard_port"
    else
        cat > "$autostart_script" <<EOF
#!/usr/bin/env bash
if ! "$py_path" - <<'PY' >/dev/null 2>&1
import socket
s = socket.socket()
try:
    s.connect(("127.0.0.1", $dashboard_port))
    raise SystemExit(0)
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY
then
  CLAUDE_MEMORY_DIR="$memory_dir" DASHBOARD_PORT="$dashboard_port" \
    nohup "$py_path" "$dashboard_path" >>"$log_dir/dashboard.log" 2>>"$log_dir/dashboard.err" </dev/null &
fi
EOF
        chmod +x "$autostart_script"

        if ! grep -Fq "$autostart_script" "$profile_path" 2>/dev/null; then
            cat >> "$profile_path" <<EOF

# Claude Total Memory dashboard auto-start
if [ -x "$autostart_script" ]; then
    "$autostart_script"
fi
EOF
        fi

        "$autostart_script" || true

        if is_wsl; then
            echo "  OK: WSL fallback auto-start installed via ~/.profile"
        else
            echo "  OK: Shell-login auto-start installed via ~/.profile"
        fi
        echo "  OK: http://localhost:$dashboard_port"
    fi
}

print_management() {
    if [ "$(platform_name)" = "Darwin" ]; then
        echo "  Dashboard management:"
        echo "    Stop:    launchctl bootout gui/\$(id -u)/com.claude-total-memory.dashboard"
        echo "    Start:   launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.claude-total-memory.dashboard.plist"
        echo "    Logs:    tail -f ~/.claude-memory/logs/dashboard.log"
    elif systemd_user_available; then
        echo "  Dashboard management:"
        echo "    Status:  systemctl --user status claude-total-memory-dashboard"
        echo "    Stop:    systemctl --user stop claude-total-memory-dashboard"
        echo "    Start:   systemctl --user start claude-total-memory-dashboard"
        echo "    Logs:    tail -f ~/.claude-memory/logs/dashboard.log"
    else
        echo "  Dashboard management:"
        echo "    Start:   ~/.claude-memory/dashboard-autostart.sh"
        echo "    Logs:    tail -f ~/.claude-memory/logs/dashboard.log"
    fi
}

cmd="${1:-}"
case "$cmd" in
    install)
        shift
        install_service "$@"
        ;;
    print-management)
        print_management
        ;;
    *)
        usage
        exit 1
        ;;
esac
