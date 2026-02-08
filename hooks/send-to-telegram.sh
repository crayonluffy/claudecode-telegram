#!/bin/bash
# Claude Code Stop hook - sends response back to Telegram
# Install: copy to ~/.claude/hooks/ and add to ~/.claude/settings.json

ENV_FILE=%BRIDGE_DIR%/.env
# Load token from .env file (Claude processes don't inherit the bridge's env)
if [ -z "$TELEGRAM_BOT_TOKEN" ] && [ -f "$ENV_FILE" ]; then
    TELEGRAM_BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2)
fi
[ -z "$TELEGRAM_BOT_TOKEN" ] && exit 0

INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')
CHAT_ID_FILE=~/.claude/telegram_chat_id
PENDING_FILE=~/.claude/telegram_pending
SESSION_FILE=~/.claude/telegram_tmux_session
TMUX_SESSION=$(cat "$SESSION_FILE" 2>/dev/null)
TMUX_SESSION="${TMUX_SESSION:-claude}"

# Only respond to Telegram-initiated messages
[ ! -f "$PENDING_FILE" ] && exit 0

PENDING_TIME=$(cat "$PENDING_FILE" 2>/dev/null)
NOW=$(date +%s)
[ -z "$PENDING_TIME" ] || [ $((NOW - PENDING_TIME)) -gt 600 ] && rm -f "$PENDING_FILE" && exit 0
[ ! -f "$CHAT_ID_FILE" ] || [ ! -f "$TRANSCRIPT_PATH" ] && rm -f "$PENDING_FILE" && exit 0

# Only allow the Claude instance running in the target tmux session to respond.
# This prevents other Claude instances (e.g. MRC, SI) from leaking responses to Telegram.
# Quick check: compare our own tmux session name with the target session.
MY_SESSION=$(tmux display-message -p '#{session_name}' 2>/dev/null)
[ -n "$MY_SESSION" ] && [ "$MY_SESSION" != "$TMUX_SESSION" ] && exit 0
# Also verify via project directory encoding (belt-and-suspenders).
TMUX_CWD=$(tmux display-message -t "$TMUX_SESSION" -p '#{pane_current_path}' 2>/dev/null)
if [ -n "$TMUX_CWD" ]; then
    PROJ_DIR=$(echo "$TRANSCRIPT_PATH" | sed 's|.*/projects/||; s|/[^/]*$||')
    TMUX_ENCODED=$(echo "$TMUX_CWD" | sed 's|/|-|g')
    [ "$PROJ_DIR" != "$TMUX_ENCODED" ] && exit 0
fi

# Check if Claude Code is waiting for user input by inspecting the tmux pane.
# Only match patterns that appear when Claude is IDLE (waiting for input).
# Avoids "esc to interrupt" which appears during active streaming.
# Also skip AskUserQuestion prompts (they show "Esc to cancel" / "Enter to select").
sleep 0.3
PANE_BOTTOM=$(tmux capture-pane -t "$TMUX_SESSION" -p 2>/dev/null | tail -8)
echo "$PANE_BOTTOM" | grep -qE 'to navigate|ctrl-g to edit|tab to cycle' || exit 0
echo "$PANE_BOTTOM" | grep -q 'Esc to cancel' && exit 0

CHAT_ID=$(cat "$CHAT_ID_FILE")
LAST_USER_LINE=$(grep -n '"type":"user"' "$TRANSCRIPT_PATH" | grep -v '"tool_result"' | tail -1 | cut -d: -f1)
[ -z "$LAST_USER_LINE" ] && rm -f "$PENDING_FILE" && exit 0

TMPFILE=$(mktemp)
tail -n "+$LAST_USER_LINE" "$TRANSCRIPT_PATH" | \
  grep '"type":"assistant"' | \
  jq -rs '[.[].message.content[] | select(.type == "text") | .text] | join("\n\n")' > "$TMPFILE" 2>/dev/null

[ ! -s "$TMPFILE" ] && rm -f "$TMPFILE" "$PENDING_FILE" && exit 0

python3 - "$TMPFILE" "$CHAT_ID" "$TELEGRAM_BOT_TOKEN" << 'PYEOF'
import sys, re, json, urllib.request

tmpfile, chat_id, token = sys.argv[1], sys.argv[2], sys.argv[3]
with open(tmpfile) as f:
    text = f.read().strip()

if not text or text == "null":
    sys.exit(0)

if len(text) > 4000:
    text = text[:4000] + "\n..."

def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

blocks, inlines = [], []
text = re.sub(r'```(\w*)\n?(.*?)```', lambda m: (blocks.append((m.group(1) or '', m.group(2))), f"\x00B{len(blocks)-1}\x00")[1], text, flags=re.DOTALL)
text = re.sub(r'`([^`\n]+)`', lambda m: (inlines.append(m.group(1)), f"\x00I{len(inlines)-1}\x00")[1], text)
text = esc(text)
text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)

for i, (lang, code) in enumerate(blocks):
    text = text.replace(f"\x00B{i}\x00", f'<pre><code class="language-{lang}">{esc(code.strip())}</code></pre>' if lang else f'<pre>{esc(code.strip())}</pre>')
for i, code in enumerate(inlines):
    text = text.replace(f"\x00I{i}\x00", f'<code>{esc(code)}</code>')

def send(txt, mode=None):
    data = {"chat_id": chat_id, "text": txt}
    if mode:
        data["parse_mode"] = mode
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", json.dumps(data).encode(), {"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read()).get("ok")
    except:
        return False

if not send(text, "HTML"):
    with open(tmpfile) as f:
        send(f.read()[:4096])
PYEOF

rm -f "$TMPFILE" "$PENDING_FILE"
exit 0
