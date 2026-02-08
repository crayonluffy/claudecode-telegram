#!/bin/bash
# Claude Code PostToolUse hook - sends progress updates to Telegram
# Install: add to ~/.claude/settings.json hooks section

SETTINGS_FILE=~/.claude/telegram_settings.json
CHAT_ID_FILE=~/.claude/telegram_chat_id
PENDING_FILE=~/.claude/telegram_pending
ENV_FILE=%BRIDGE_DIR%/.env

# Load token from .env file (Claude processes don't inherit the bridge's env)
if [ -z "$TELEGRAM_BOT_TOKEN" ] && [ -f "$ENV_FILE" ]; then
    TELEGRAM_BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2)
fi

# Only run for Telegram-initiated messages
[ ! -f "$PENDING_FILE" ] && exit 0
[ ! -f "$CHAT_ID_FILE" ] && exit 0
[ -z "$TELEGRAM_BOT_TOKEN" ] && exit 0

# Check if verbose is enabled
if [ -f "$SETTINGS_FILE" ]; then
    VERBOSE=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('verbose', False))" 2>/dev/null)
    [ "$VERBOSE" != "True" ] && exit 0
else
    exit 0
fi

# Read input from Claude Code
INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
TOOL_INPUT=$(echo "$INPUT" | jq -r '.tool_input // empty')

[ -z "$TOOL_NAME" ] && exit 0

CHAT_ID=$(cat "$CHAT_ID_FILE")

# Map tool to emoji and message
case "$TOOL_NAME" in
    Read)
        FILE=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' | xargs basename 2>/dev/null)
        MSG="🔍 Reading ${FILE:-file}..."
        ;;
    Edit)
        FILE=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' | xargs basename 2>/dev/null)
        MSG="🔧 Editing ${FILE:-file}..."
        ;;
    Write)
        FILE=$(echo "$TOOL_INPUT" | jq -r '.file_path // empty' | xargs basename 2>/dev/null)
        MSG="📝 Writing ${FILE:-file}..."
        ;;
    Bash)
        CMD=$(echo "$TOOL_INPUT" | jq -r '.command // empty' | head -c 40)
        MSG="▶️ Running: ${CMD:-command}..."
        ;;
    Glob)
        PATTERN=$(echo "$TOOL_INPUT" | jq -r '.pattern // empty')
        MSG="🔎 Glob: ${PATTERN:-*}..."
        ;;
    Grep)
        PATTERN=$(echo "$TOOL_INPUT" | jq -r '.pattern // empty' | head -c 30)
        MSG="🔎 Grep: ${PATTERN:-pattern}..."
        ;;
    Task)
        MSG="🤖 Spawning agent..."
        ;;
    WebFetch|WebSearch)
        MSG="🌐 Web request..."
        ;;
    *)
        # Skip unknown tools
        exit 0
        ;;
esac

# Send to Telegram (fire and forget)
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"$CHAT_ID\", \"text\": \"$MSG\"}" > /dev/null 2>&1 &

exit 0
