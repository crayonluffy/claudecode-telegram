#!/bin/bash
# Setup systemd services for Telegram Claude Bridge
# Usage: ./setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "=== Telegram Bridge Setup ==="

# Check .env exists
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env file not found. Create it with:"
    echo "  TELEGRAM_BOT_TOKEN=your_token_here"
    echo "  PORT=8090"
    exit 1
fi

# Kill any stray bridge.py processes (nohup leftovers)
STRAY_PIDS=$(pgrep -f "python3.*bridge.py" 2>/dev/null || true)
if [ -n "$STRAY_PIDS" ]; then
    echo "Killing stray bridge.py processes: $STRAY_PIDS"
    kill $STRAY_PIDS 2>/dev/null || true
    sleep 1
fi

# Install hooks, replacing %BRIDGE_DIR% placeholder with actual path
mkdir -p "$HOME/.claude/hooks"
echo "Installing hooks..."
for f in "$SCRIPT_DIR/hooks/"*.sh; do
    sed "s|%BRIDGE_DIR%|$SCRIPT_DIR|g" "$f" > "$HOME/.claude/hooks/$(basename "$f")"
done
chmod +x "$HOME/.claude/hooks/"*.sh

# Register hooks in Claude settings.json automatically
SETTINGS="$HOME/.claude/settings.json"
echo "Registering hooks in $SETTINGS..."
python3 - "$SETTINGS" << 'PYEOF'
import json, sys, os

settings_path = sys.argv[1]

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

hooks = settings.setdefault("hooks", {})

TG_HOOKS = {
    "Stop": {
        "hooks": [{"type": "command", "command": "~/.claude/hooks/send-to-telegram.sh"}]
    },
    "PostToolUse": {
        "hooks": [{"type": "command", "command": "~/.claude/hooks/notify-tool-use.sh"}]
    },
}

for event, hook_entry in TG_HOOKS.items():
    event_hooks = hooks.setdefault(event, [])
    cmd = hook_entry["hooks"][0]["command"]
    already = any(cmd in str(h.get("hooks", [])) for h in event_hooks)
    if not already:
        event_hooks.append(hook_entry)
        print(f"  + Registered {event}: {cmd}")
    else:
        print(f"  = Already registered {event}: {cmd}")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
PYEOF

# Copy service files, replacing %BRIDGE_DIR% placeholder with actual path
mkdir -p "$SYSTEMD_DIR"
echo "Installing systemd units..."
for f in "$SCRIPT_DIR/systemd/"*.service "$SCRIPT_DIR/systemd/"*.path; do
    sed "s|%BRIDGE_DIR%|$SCRIPT_DIR|g" "$f" > "$SYSTEMD_DIR/$(basename "$f")"
done

# Reload systemd
systemctl --user daemon-reload
echo "Reloaded systemd"

# Enable and start services
echo "Enabling services..."
systemctl --user enable telegram-bridge.service
systemctl --user enable telegram-bridge-watcher.path
systemctl --user enable cloudflared.service

echo "Starting services..."
systemctl --user restart telegram-bridge.service
systemctl --user restart telegram-bridge-watcher.path
systemctl --user restart cloudflared.service

# Ensure lingering is enabled (services survive logout)
loginctl enable-linger "$(whoami)" 2>/dev/null || true

echo ""
echo "=== Status ==="
systemctl --user status telegram-bridge.service --no-pager -l 2>/dev/null | head -5
echo "---"
systemctl --user status telegram-bridge-watcher.path --no-pager -l 2>/dev/null | head -5
echo "---"
systemctl --user status cloudflared.service --no-pager -l 2>/dev/null | head -5

echo ""
echo "Done! Hot reload is active - bridge restarts automatically when bridge.py or hooks/ change."
echo ""
echo "Useful commands:"
echo "  systemctl --user status telegram-bridge     # Check bridge status"
echo "  systemctl --user restart telegram-bridge     # Manual restart"
echo "  journalctl --user -u telegram-bridge -f      # Live logs"
echo "  systemctl --user stop telegram-bridge-watcher.path  # Disable hot reload"
