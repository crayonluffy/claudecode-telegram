#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge"""

import os
import json
import re
import subprocess
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

DEFAULT_TMUX_SESSION = os.environ.get("TMUX_SESSION", "claude")
PROJECTS_BASE = os.path.expanduser(os.environ.get("PROJECTS_BASE", "~/claude"))
CHAT_ID_FILE = os.path.expanduser("~/.claude/telegram_chat_id")
PENDING_FILE = os.path.expanduser("~/.claude/telegram_pending")
HISTORY_FILE = os.path.expanduser("~/.claude/history.jsonl")
SETTINGS_FILE = os.path.expanduser("~/.claude/telegram_settings.json")
SESSION_FILE = os.path.expanduser("~/.claude/telegram_tmux_session")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
UPLOAD_DIR = os.path.expanduser(os.environ.get("UPLOAD_DIR", "~/uploads"))

# Media group (album) buffering for multiple photos
_media_group_lock = threading.Lock()
_media_group_buffer = {}  # media_group_id -> {msgs: [...], timer: Timer, chat_id, msg_id}

# Interactive prompt monitoring state
_prompt_lock = threading.Lock()
_prompt_last_fingerprint = None
_prompt_keyboard_message_id = None
_prompt_keyboard_chat_id = None
_prompt_current_options = []
_prompt_highlighted_index = 0
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][AB012]')


def get_current_session():
    """Get the current tmux session name."""
    try:
        with open(SESSION_FILE) as f:
            return f.read().strip() or DEFAULT_TMUX_SESSION
    except:
        return DEFAULT_TMUX_SESSION


def set_current_session(name):
    """Set the current tmux session name."""
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            f.write(name)
        return True
    except:
        return False


def list_tmux_sessions():
    """List all available tmux sessions."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]

DEFAULT_SETTINGS = {
    "verbose": False,
    "coauthor": True,
    "signature": True,
}


def load_settings():
    """Load user settings from file."""
    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
            return {**DEFAULT_SETTINGS, **settings}
    except:
        return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    """Save user settings to file."""
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    except:
        return False

BOT_COMMANDS = [
    {"command": "stop", "description": "Interrupt Claude (Escape)"},
    {"command": "screenshot", "description": "Capture tmux screen"},
    {"command": "status", "description": "Show project, branch, state"},
    {"command": "projects", "description": "List available projects"},
    {"command": "sessions", "description": "List all tmux sessions"},
    {"command": "attach", "description": "Switch to tmux session"},
    {"command": "start", "description": "Create tmux + start Claude"},
    {"command": "restart", "description": "Restart Claude Code"},
    {"command": "new", "description": "Start new Claude session"},
    {"command": "resume", "description": "Resume session (picker)"},
    {"command": "continue_", "description": "Continue last session"},
    {"command": "scroll", "description": "Show last N lines"},
    {"command": "usage", "description": "Show plan usage limits"},
    {"command": "help", "description": "Show all commands"},
]

HELP_TEXT = """Commands:
  /stop - Interrupt Claude (Escape)
  /screenshot - Capture tmux screen
  /status - Project, branch, cwd, state
  /start [name] [dir] - Create tmux + Claude
  /restart - Restart Claude
  /new [dir] - Fresh Claude session
  /resume - Pick from recent sessions
  /continue_ - Continue last session
  /scroll [n] - Last n lines of output

More:
  /projects /sessions /attach /kill /clear
  /usage /commit /undo /diff /pwd /loop
  /pick N /y /n /ok /retry
  /verbose /coauthor /signature

Just type normally to chat with Claude.
Interactive prompts appear as buttons automatically."""

BLOCKED_COMMANDS = [
    "/mcp", "/settings", "/config", "/model", "/compact", "/cost",
    "/doctor", "/init", "/login", "/logout", "/memory", "/permissions",
    "/pr", "/review", "/terminal", "/vim", "/approved-tools", "/listen"
]


def telegram_api(method, data):
    if not BOT_TOKEN:
        return None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"Telegram API error: {e}")
        return None


def download_telegram_file(file_id):
    """Download a file from Telegram and save to dated folder. Returns local path."""
    result = telegram_api("getFile", {"file_id": file_id})
    if not result or not result.get("ok"):
        return None

    file_path = result.get("result", {}).get("file_path", "")
    if not file_path:
        return None

    # Create dated folder
    from datetime import datetime
    date_folder = datetime.now().strftime("%Y-%m-%d")
    save_dir = Path(UPLOAD_DIR) / date_folder
    save_dir.mkdir(parents=True, exist_ok=True)

    # Download file
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    file_name = f"{file_id}_{Path(file_path).name}"
    local_path = save_dir / file_name

    try:
        urllib.request.urlretrieve(file_url, local_path)
        return str(local_path)
    except Exception as e:
        print(f"Download error: {e}")
        return None


def setup_bot_commands():
    result = telegram_api("setMyCommands", {"commands": BOT_COMMANDS})
    if result and result.get("ok"):
        print("Bot commands registered")


def send_typing_loop(chat_id):
    while os.path.exists(PENDING_FILE):
        telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        time.sleep(4)


def tmux_exists(session=None):
    """Check if a tmux session exists."""
    session = session or get_current_session()
    return subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0


def tmux_create(session=None, start_dir=None, start_claude=True):
    """Create a new tmux session and optionally start Claude Code."""
    session = session or get_current_session()

    if tmux_exists(session):
        return True, "Session already exists"

    # Create new detached session
    cmd = ["tmux", "new-session", "-d", "-s", session]
    if start_dir:
        cmd.extend(["-c", start_dir])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, f"Failed to create session: {result.stderr}"

    # Update current session
    set_current_session(session)

    if start_claude:
        time.sleep(0.5)
        tmux_send("claude --dangerously-skip-permissions", session=session)
        tmux_send_enter(session=session)

    return True, "Session created"


def _wait_for_shell_prompt(session, timeout=5):
    """Wait until the tmux pane shows a shell prompt (Claude has exited)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pane = capture_tmux_pane(session)
        if not pane:
            time.sleep(0.3)
            continue
        # Check last non-empty line for shell prompt characters
        lines = [l for l in pane.rstrip().split('\n') if l.strip()]
        if lines:
            last = strip_ansi(lines[-1]).rstrip()
            if last.endswith('$') or last.endswith('#') or last.endswith('%'):
                return True
        time.sleep(0.3)
    return False


def _stop_claude(session=None):
    """Reliably stop Claude Code and return to shell prompt. Returns True on success."""
    session = session or get_current_session()

    # Already at shell prompt?
    if _wait_for_shell_prompt(session, timeout=0.5):
        return True

    # Double Ctrl-C reliably exits Claude from any state (idle or working)
    subprocess.run(["tmux", "send-keys", "-t", session, "C-c"])
    time.sleep(0.5)
    subprocess.run(["tmux", "send-keys", "-t", session, "C-c"])
    time.sleep(0.5)

    if _wait_for_shell_prompt(session, timeout=5):
        return True

    # Last resort: /exit
    tmux_send("/exit", session=session)
    tmux_send_enter(session)
    time.sleep(1)
    return _wait_for_shell_prompt(session, timeout=5)


def tmux_restart_claude(session=None, start_dir=None):
    """Restart Claude Code in existing or new tmux session.
    Returns (success, message, previous_session_id)."""
    session = session or get_current_session()
    prev_session_id = None

    if not tmux_exists(session):
        ok, msg = tmux_create(session, start_dir, start_claude=True)
        return ok, msg, None

    # Remember current working directory before restart
    if not start_dir:
        result = subprocess.run(
            ["tmux", "display-message", "-t", session, "-p", "#{pane_current_path}"],
            capture_output=True, text=True
        )
        start_dir = result.stdout.strip() or None

    # Capture session ID before stopping so we can offer resume
    if start_dir:
        prev_session_id = get_session_id(start_dir)

    if not _stop_claude(session):
        return False, "Failed to stop previous Claude instance", None

    # Clean up stale pending file so old monitor threads stop
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)

    # Start Claude in the remembered directory
    if start_dir:
        tmux_send(f"cd {start_dir} && claude --dangerously-skip-permissions", session=session)
    else:
        tmux_send("claude --dangerously-skip-permissions", session=session)
    tmux_send_enter(session)

    return True, "Claude restarted", prev_session_id


def ensure_tmux_session(session=None):
    """Ensure tmux session exists, create if not."""
    session = session or get_current_session()
    if tmux_exists(session):
        return True
    success, _ = tmux_create(session, start_claude=True)
    return success


def tmux_send(text, literal=True, session=None):
    """Send text to tmux session."""
    session = session or get_current_session()
    cmd = ["tmux", "send-keys", "-t", session]
    if literal:
        cmd.append("-l")
    cmd.append(text)
    subprocess.run(cmd)


def tmux_send_enter(session=None):
    """Send Enter key to tmux session."""
    session = session or get_current_session()
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"])


def tmux_send_escape(session=None):
    """Send Escape key to tmux session."""
    session = session or get_current_session()
    subprocess.run(["tmux", "send-keys", "-t", session, "Escape"])


def strip_ansi(text):
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub('', text)


def capture_tmux_pane(session=None):
    """Capture current tmux pane content as plain text."""
    session = session or get_current_session()
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except Exception:
        return ""


def _is_horizontal_rule(line):
    """Check if a line is a horizontal rule (───────)."""
    stripped = line.strip()
    return len(stripped) > 10 and stripped.count('\u2500') > len(stripped) * 0.7


def _is_cursor_char(ch):
    """Check if a character is a selection cursor used by Claude Code."""
    return ch in ('\u276f', '\u203a', '>')  # ❯, ›, >


def parse_interactive_prompt(pane_text):
    """Parse an interactive selection prompt from tmux pane content.

    Claude Code renders AskUserQuestion prompts like:
        [header]
        Question text here

        › 1. Option A
            Description of A
          2. Option B
            Description of B

        Enter to select · ↑/↓ to navigate · Esc to cancel

    Detection uses the footer "to navigate" as a reliable signal,
    combined with cursor character (›, ❯, or >) on an option line.
    Returns (question, [options_as_[label,desc]], highlighted_index) or None.
    """
    text = strip_ansi(pane_text)
    lines = text.split('\n')

    # Look for the navigation footer at the BOTTOM of the pane.
    # The actual footer is always one of the last few visible lines:
    #   "Enter to select · ↑/↓ to navigate · Esc to cancel"
    # Must check bottom lines only to avoid matching conversation text.
    footer_line = None
    for i in range(len(lines) - 1, max(len(lines) - 6, -1), -1):
        if i < 0:
            break
        s = lines[i].strip()
        if s.startswith('Enter') and 'to navigate' in s:
            footer_line = i
            break

    if footer_line is None:
        return None

    # Find the cursor line (›, ❯, or >) above the footer
    cursor_line = None
    for i in range(footer_line - 1, max(footer_line - 40, -1), -1):
        s = lines[i].strip()
        if len(s) > 1 and _is_cursor_char(s[0]) and s[1] == ' ':
            cursor_line = i
            break

    if cursor_line is None:
        return None

    cursor_char = lines[cursor_line].strip()[0]
    cursor_col = len(lines[cursor_line]) - len(lines[cursor_line].lstrip())
    label_indent = cursor_col + 2  # cursor + space = 2 chars

    # Find the first option line (scan up from cursor to find start of options)
    first_option = cursor_line
    for i in range(cursor_line - 1, max(cursor_line - 30, -1), -1):
        s = lines[i].strip()
        if not s:
            break
        leading = len(lines[i]) - len(lines[i].lstrip())
        if leading <= label_indent and not _is_cursor_char(s[0]):
            # Same indent as option labels — this is an option above cursor
            first_option = i
        elif leading > label_indent:
            # Description line — continue scanning up
            continue
        elif _is_cursor_char(s[0]):
            first_option = i
        else:
            break

    # Find question text above the options
    question_text = ""
    for i in range(first_option - 1, max(first_option - 10, -1), -1):
        s = lines[i].strip()
        if not s:
            continue
        if _is_horizontal_rule(lines[i]):
            break
        if any(c in s for c in '\u256d\u256e\u2570\u256f'):
            break
        if s and len(s) > 5:
            question_text = s
            break

    # Collect options and descriptions from first_option to footer
    options = []
    highlighted_index = 0

    for i in range(first_option, footer_line):
        line = lines[i]
        s = line.strip()
        if not s:
            continue  # skip blank lines between option groups
        if 'to navigate' in s:
            break
        # Skip horizontal rules and em-dash separators
        if _is_horizontal_rule(line) or (len(s) <= 3 and all(c in '\u2500\u2014\u2013-' for c in s)):
            continue

        # Check if this is a cursor line
        is_cursor = len(s) > 1 and _is_cursor_char(s[0]) and s[1] == ' '

        if is_cursor:
            highlighted_index = len(options)
            options.append([s[2:], None])
        else:
            leading = len(line) - len(line.lstrip())
            if leading <= label_indent:
                # Option label
                options.append([s, None])
            elif options:
                # Description — attach to last option
                if options[-1][1]:
                    options[-1][1] += ' ' + s
                else:
                    options[-1][1] = s

    if len(options) < 2:
        return None

    return (question_text or "Select an option:", options, highlighted_index)


def prompt_fingerprint(question, options):
    """Generate a fingerprint for a prompt to avoid sending duplicates.
    options is a list of [label, description_or_None].
    """
    return f"{question}|{'|'.join(o[0] for o in options)}"


def send_prompt_keyboard(chat_id, question, options):
    """Send an interactive prompt to Telegram as inline keyboard buttons.

    options is a list of [label, description_or_None].
    Message text shows full details (label + description), buttons show labels.
    Returns message_id of the sent message, or None on failure.
    """
    # Build message text with full option details
    text_lines = [f"Interactive prompt:\n\n{question}\n"]
    for label, desc in options:
        if desc:
            text_lines.append(f"  {label}\n    {desc}")
        else:
            text_lines.append(f"  {label}")
    msg_text = "\n".join(text_lines)

    # Build inline keyboard with label-only buttons
    # Hide "Type something"/"Other" — selecting them in Claude Code just declines
    keyboard = []
    for i, (label, _desc) in enumerate(options):
        clean = re.sub(r'^\d+\.\s*', '', label).strip().lower().rstrip('.')
        if clean in ("other", "type something"):
            continue
        display = label[:60] + "..." if len(label) > 60 else label
        keyboard.append([{"text": display, "callback_data": f"pick:{i}"}])
    keyboard.append([{"text": "--- Dismiss (Escape) ---", "callback_data": "pick:dismiss"}])
    msg_text += "\n\nOr type a message to skip this prompt."

    result = telegram_api("sendMessage", {
        "chat_id": chat_id,
        "text": msg_text,
        "reply_markup": {"inline_keyboard": keyboard}
    })
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def dismiss_prompt_keyboard(chat_id, message_id, reason="Prompt dismissed"):
    """Edit a previously sent inline keyboard to remove buttons and show status."""
    telegram_api("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": reason,
        "reply_markup": {"inline_keyboard": []}
    })


def select_prompt_option(target_index, current_index, total_options, session=None):
    """Send arrow key presses + Enter to select an option in the TUI."""
    session = session or get_current_session()
    moves = target_index - current_index

    if moves > 0:
        for _ in range(moves):
            subprocess.run(["tmux", "send-keys", "-t", session, "Down"], timeout=5)
            time.sleep(0.05)
    elif moves < 0:
        for _ in range(abs(moves)):
            subprocess.run(["tmux", "send-keys", "-t", session, "Up"], timeout=5)
            time.sleep(0.05)

    time.sleep(0.1)
    tmux_send_enter(session)


def check_and_show_prompt(chat_id):
    """Synchronously check for an active prompt and show keyboard if found.

    Returns True if a prompt was detected and keyboard was sent.
    """
    global _prompt_last_fingerprint, _prompt_keyboard_message_id
    global _prompt_keyboard_chat_id, _prompt_current_options, _prompt_highlighted_index

    pane_text = capture_tmux_pane()
    if not pane_text:
        return False

    parsed = parse_interactive_prompt(pane_text)
    if parsed is None:
        return False

    question, options, highlighted_idx = parsed
    fp = prompt_fingerprint(question, options)

    with _prompt_lock:
        if fp == _prompt_last_fingerprint:
            return True  # Already showing this keyboard

        if _prompt_keyboard_message_id and _prompt_keyboard_chat_id:
            dismiss_prompt_keyboard(
                _prompt_keyboard_chat_id, _prompt_keyboard_message_id,
                "Previous prompt superseded"
            )

        msg_id = send_prompt_keyboard(chat_id, question, options)
        _prompt_last_fingerprint = fp
        _prompt_keyboard_message_id = msg_id
        _prompt_keyboard_chat_id = chat_id
        _prompt_current_options = options
        _prompt_highlighted_index = highlighted_idx

    return True


def prompt_monitor_loop(chat_id):
    """Periodically check tmux pane for interactive prompts while request is pending."""
    global _prompt_last_fingerprint, _prompt_keyboard_message_id
    global _prompt_keyboard_chat_id, _prompt_current_options, _prompt_highlighted_index

    MONITOR_LOG = os.path.expanduser("~/.claude/prompt_monitor_debug.log")
    def mlog(msg):
        try:
            with open(MONITOR_LOG, "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    time.sleep(0.5)
    mlog("Monitor started")

    try:
        while os.path.exists(PENDING_FILE):
            try:
                pane_text = capture_tmux_pane()
                if not pane_text:
                    time.sleep(0.5)
                    continue

                parsed = parse_interactive_prompt(pane_text)
                if parsed:
                    labels = [o[0] for o in parsed[1]]
                    mlog(f"Parsed: True q={parsed[0]!r} opts={len(parsed[1])} labels={labels} hi={parsed[2]}")
                # Dump full pane to file for debugging when footer is found
                elif 'to navigate' in pane_text and 'to select' in pane_text:
                    dump_path = os.path.expanduser("~/.claude/pane_dump.txt")
                    with open(dump_path, "w") as f:
                        f.write(pane_text)
                    mlog(f"Footer found but parse failed, pane dumped to {dump_path}")

                with _prompt_lock:
                    if parsed is not None:
                        question, options, highlighted_idx = parsed
                        fp = prompt_fingerprint(question, options)

                        if fp != _prompt_last_fingerprint:
                            # Dismiss old keyboard if exists
                            if _prompt_keyboard_message_id and _prompt_keyboard_chat_id:
                                dismiss_prompt_keyboard(
                                    _prompt_keyboard_chat_id,
                                    _prompt_keyboard_message_id,
                                    "Previous prompt superseded"
                                )

                            msg_id = send_prompt_keyboard(chat_id, question, options)
                            _prompt_last_fingerprint = fp
                            _prompt_keyboard_message_id = msg_id
                            _prompt_keyboard_chat_id = chat_id
                            _prompt_current_options = options
                            _prompt_highlighted_index = highlighted_idx
                    else:
                        # No prompt on screen - dismiss keyboard if one was sent
                        if _prompt_keyboard_message_id and _prompt_keyboard_chat_id:
                            dismiss_prompt_keyboard(
                                _prompt_keyboard_chat_id,
                                _prompt_keyboard_message_id,
                                "Prompt was resolved"
                            )
                            _prompt_keyboard_message_id = None
                            _prompt_keyboard_chat_id = None
                            _prompt_last_fingerprint = None
                            _prompt_current_options = []

            except Exception as e:
                mlog(f"Error: {e}")
                print(f"Prompt monitor error: {e}")

            time.sleep(0.5)

    finally:
        with _prompt_lock:
            if _prompt_keyboard_message_id and _prompt_keyboard_chat_id:
                dismiss_prompt_keyboard(
                    _prompt_keyboard_chat_id,
                    _prompt_keyboard_message_id,
                    "Request completed"
                )
            _prompt_last_fingerprint = None
            _prompt_keyboard_message_id = None
            _prompt_keyboard_chat_id = None
            _prompt_current_options = []


def get_recent_sessions(limit=5):
    if not os.path.exists(HISTORY_FILE):
        return []
    sessions = []
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                try:
                    sessions.append(json.loads(line.strip()))
                except:
                    continue
    except:
        return []
    sessions.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return sessions[:limit]


def get_session_id(project_path):
    encoded = project_path.replace("/", "-").lstrip("-")
    for prefix in [f"-{encoded}", encoded]:
        project_dir = Path.home() / ".claude" / "projects" / prefix
        if project_dir.exists():
            jsonls = list(project_dir.glob("*.jsonl"))
            if jsonls:
                return max(jsonls, key=lambda p: p.stat().st_mtime).stem
    return None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            update = json.loads(body)
            if "callback_query" in update:
                print(f"[webhook] callback_query data={update['callback_query'].get('data')}")
                self.handle_callback(update["callback_query"])
            elif "message" in update:
                self.handle_message(update)
        except Exception as e:
            import traceback
            print(f"Error: {e}")
            traceback.print_exc()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Claude-Telegram Bridge")

    def handle_callback(self, cb):
        global _prompt_keyboard_message_id, _prompt_last_fingerprint, _prompt_current_options
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        telegram_api("answerCallbackQuery", {"callback_query_id": cb.get("id")})

        if not tmux_exists():
            self.reply(chat_id, "tmux session not found")
            return

        if data.startswith("resume:"):
            session_id = data.split(":", 1)[1]
            if not _stop_claude():
                self.reply(chat_id, "Failed to stop Claude")
                return
            tmux_send(f"claude --resume {session_id} --dangerously-skip-permissions")
            tmux_send_enter()
            self.reply(chat_id, f"Resuming: {session_id[:8]}...")

        elif data == "continue_recent":
            if not _stop_claude():
                self.reply(chat_id, "Failed to stop Claude")
                return
            tmux_send("claude --continue --dangerously-skip-permissions")
            tmux_send_enter()
            self.reply(chat_id, "Continuing most recent...")

        elif data == "dismiss_msg":
            msg_id = cb.get("message", {}).get("message_id")
            telegram_api("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
            return

        elif data.startswith("attach:"):
            session_name = data.split(":", 1)[1]
            if not tmux_exists(session_name):
                self.reply(chat_id, f"❌ Session '{session_name}' not found")
                return
            set_current_session(session_name)
            # Update the button message to show the new selection
            msg_id = cb.get("message", {}).get("message_id")
            sessions = list_tmux_sessions()
            kb = []
            for s in sessions:
                label = f"{'▶ ' if s == session_name else ''}{s}"
                kb.append([{"text": label, "callback_data": f"attach:{s}"}])
            telegram_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": f"📺 Sessions ({len(sessions)}) — current: {session_name}",
                "reply_markup": {"inline_keyboard": kb}
            })

        elif data.startswith("pick:"):
            pick_value = data.split(":", 1)[1]
            print(f"[pick] pick_value={pick_value}")

            if pick_value == "dismiss":
                tmux_send_escape()
                with _prompt_lock:
                    if _prompt_keyboard_message_id:
                        dismiss_prompt_keyboard(
                            chat_id, _prompt_keyboard_message_id,
                            "Dismissed (Escape sent)"
                        )
                        _prompt_keyboard_message_id = None
                        _prompt_last_fingerprint = None
                        _prompt_current_options = []
                return

            try:
                target_idx = int(pick_value)
            except ValueError:
                self.reply(chat_id, "Invalid selection")
                return

            with _prompt_lock:
                options = _prompt_current_options[:]
                highlighted_idx = _prompt_highlighted_index

            print(f"[pick] target={target_idx} highlighted={highlighted_idx} options_count={len(options)}")

            if not options or target_idx < 0 or target_idx >= len(options):
                self.reply(chat_id, f"Prompt may have changed (options={len(options)}, target={target_idx}). Use /screenshot to check.")
                return

            selected_text = options[target_idx][0] if isinstance(options[target_idx], list) else options[target_idx]
            print(f"[pick] Selecting: {selected_text}, sending {target_idx - highlighted_idx} moves")

            # Grab and clear the keyboard message ID BEFORE sending keys,
            # so the monitor loop can't race and override with "Prompt was resolved"
            with _prompt_lock:
                saved_msg_id = _prompt_keyboard_message_id
                _prompt_keyboard_message_id = None
                # Keep _prompt_last_fingerprint so monitor won't re-send keyboard
                _prompt_current_options = []

            select_prompt_option(target_idx, highlighted_idx, len(options))

            if saved_msg_id:
                dismiss_prompt_keyboard(
                    chat_id, saved_msg_id,
                    f"Selected: {selected_text}"
                )

    def handle_message(self, update):
        global _prompt_last_fingerprint, _prompt_keyboard_message_id
        global _prompt_keyboard_chat_id, _prompt_current_options
        msg = update.get("message", {})
        chat_id, msg_id = msg.get("chat", {}).get("id"), msg.get("message_id")
        if not chat_id:
            return

        # Handle photo uploads (with media group / album support)
        if "photo" in msg:
            media_group_id = msg.get("media_group_id")
            if media_group_id:
                # Part of an album — buffer and wait for all photos
                with _media_group_lock:
                    if media_group_id not in _media_group_buffer:
                        _media_group_buffer[media_group_id] = {
                            "msgs": [], "chat_id": chat_id, "msg_id": msg_id
                        }
                    _media_group_buffer[media_group_id]["msgs"].append(msg)
                    # Cancel existing timer and set a new one (1.5s debounce)
                    existing = _media_group_buffer[media_group_id].get("timer")
                    if existing:
                        existing.cancel()
                    t = threading.Timer(1.5, self._process_media_group, args=(media_group_id,))
                    _media_group_buffer[media_group_id]["timer"] = t
                    t.start()
            else:
                self.handle_photo(msg, chat_id, msg_id)
            return

        text = msg.get("text", "")
        if not text:
            return

        with open(CHAT_ID_FILE, "w") as f:
            f.write(str(chat_id))

        if text.startswith("/"):
            cmd = text.split()[0].lower()

            if cmd == "/help":
                self.reply(chat_id, HELP_TEXT)
                return

            if cmd == "/status":
                current_session = get_current_session()
                if not tmux_exists():
                    self.reply(chat_id, f"❌ tmux '{current_session}' not found\n\n💡 Use /start to create or /sessions to list available")
                    return
                # Get current directory
                result = subprocess.run(
                    ["tmux", "display-message", "-t", current_session, "-p", "#{pane_current_path}"],
                    capture_output=True, text=True
                )
                pwd = result.stdout.strip() or "unknown"
                # Get git branch
                branch_result = subprocess.run(
                    ["git", "-C", pwd, "branch", "--show-current"],
                    capture_output=True, text=True
                )
                branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "n/a"
                # Get session ID
                sessions = get_recent_sessions(1)
                sid = ""
                if sessions:
                    sid = get_session_id(sessions[0].get("project", "")) or ""
                # Check if Claude is running by looking at the pane
                pane_result = subprocess.run(
                    ["tmux", "capture-pane", "-t", current_session, "-p"],
                    capture_output=True, text=True
                )
                pane_content = pane_result.stdout.strip()
                # Check if Claude is running
                claude_running = "claude" in pane_content.lower() or ">" in pane_content or "╭" in pane_content
                shell_prompt = pane_content.endswith("$") or pane_content.endswith("#") or pane_content.endswith("%")

                if os.path.exists(PENDING_FILE):
                    state = "⏳ Working..."
                elif shell_prompt and not claude_running:
                    state = "🔴 Shell only (Claude not running)\n💡 Use /restart to start Claude"
                else:
                    state = "✅ Ready"
                # Build status message
                lines = [
                    f"🖥️ tmux: {current_session}",
                    f"📍 {pwd}",
                    f"🔀 Branch: {branch}",
                    f"📋 Claude: {sid[:8]}..." if sid else "📋 Claude: unknown",
                    f"⏱️ {state}",
                ]
                self.reply(chat_id, "\n".join(lines))
                return

            # List available project directories
            if cmd == "/projects":
                if not os.path.isdir(PROJECTS_BASE):
                    self.reply(chat_id, f"❌ Projects base not found: {PROJECTS_BASE}")
                    return
                dirs = sorted(
                    d for d in os.listdir(PROJECTS_BASE)
                    if os.path.isdir(os.path.join(PROJECTS_BASE, d)) and not d.startswith(".")
                )
                if not dirs:
                    self.reply(chat_id, f"No projects found in {PROJECTS_BASE}")
                    return
                sessions = list_tmux_sessions()
                lines = [f"📂 Projects in `{PROJECTS_BASE}`:\n"]
                for d in dirs:
                    marker = " ✅" if d in sessions else ""
                    lines.append(f"  • {d}{marker}")
                lines.append(f"\n💡 /start <name> to create a session")
                self.reply(chat_id, "\n".join(lines))
                return

            # List tmux sessions
            if cmd == "/sessions":
                sessions = list_tmux_sessions()
                current = get_current_session()
                if not sessions:
                    self.reply(chat_id, "No tmux sessions found.\n\n💡 Use /start <name> to create one")
                    return
                kb = []
                for s in sessions:
                    label = f"{'▶ ' if s == current else ''}{s}"
                    kb.append([{"text": label, "callback_data": f"attach:{s}"}])
                kb.append([{"text": "✕ Dismiss", "callback_data": "dismiss_msg"}])
                telegram_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"📺 Sessions ({len(sessions)}) — current: {current}",
                    "reply_markup": {"inline_keyboard": kb}
                })
                return

            # Attach to tmux session
            if cmd == "/attach":
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    self.reply(chat_id, "Usage: /attach <session_name>\n\n💡 Use /sessions to list available")
                    return
                session_name = parts[1].strip()
                if not tmux_exists(session_name):
                    self.reply(chat_id, f"❌ Session '{session_name}' not found\n\n💡 Use /sessions to list available or /start {session_name} to create")
                    return
                set_current_session(session_name)
                self.reply(chat_id, f"✅ Switched to tmux session: {session_name}")
                return

            # Quick responses
            if cmd == "/y":
                if tmux_exists():
                    tmux_send("yes")
                    tmux_send_enter()
                    self.reply(chat_id, "Sent: yes")
                return

            if cmd == "/n":
                if tmux_exists():
                    tmux_send("no")
                    tmux_send_enter()
                    self.reply(chat_id, "Sent: no")
                return

            if cmd == "/ok":
                if tmux_exists():
                    tmux_send("ok, continue")
                    tmux_send_enter()
                    self.reply(chat_id, "Sent: ok, continue")
                return

            if cmd == "/retry":
                if tmux_exists():
                    tmux_send("try again")
                    tmux_send_enter()
                    self.reply(chat_id, "Sent: try again")
                return

            # Info commands
            if cmd == "/pwd":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                result = subprocess.run(
                    ["tmux", "display-message", "-t", get_current_session(), "-p", "#{pane_current_path}"],
                    capture_output=True, text=True
                )
                pwd = result.stdout.strip() or "unknown"
                self.reply(chat_id, f"📂 {pwd}")
                return

            if cmd == "/project":
                current_session = get_current_session()
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                result = subprocess.run(
                    ["tmux", "display-message", "-t", current_session, "-p", "#{pane_current_path}"],
                    capture_output=True, text=True
                )
                pwd = result.stdout.strip() or "unknown"
                # Get session ID
                sessions = get_recent_sessions(1)
                sid = ""
                if sessions:
                    sid = get_session_id(sessions[0].get("project", "")) or ""
                self.reply(chat_id, f"🖥️ tmux: {current_session}\n📂 {pwd}\n📋 Claude: {sid[:8] if sid else 'unknown'}...")
                return

            # Session management
            if cmd == "/new":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found. Use /start to create a session first.")
                    return
                parts = text.split(maxsplit=1)
                if not _stop_claude():
                    self.reply(chat_id, "Failed to stop Claude")
                    return
                if len(parts) > 1:
                    # Start in specified directory
                    target_dir = parts[1].strip()
                    tmux_send(f"cd {target_dir} && claude --dangerously-skip-permissions")
                else:
                    tmux_send("claude --dangerously-skip-permissions")
                tmux_send_enter()
                self.reply(chat_id, "New Claude session started")
                return

            if cmd == "/kill":
                current_session = get_current_session()
                subprocess.run(["tmux", "kill-session", "-t", current_session], capture_output=True)
                self.reply(chat_id, f"Killed tmux session '{current_session}'")
                return

            if cmd == "/start":
                # /start [session_name] [directory]
                parts = text.split()
                session_name = None
                start_dir = None

                if len(parts) >= 2:
                    # Check if second arg is a path or session name
                    arg = parts[1]
                    if arg.startswith("/") or arg.startswith("~") or arg.startswith("."):
                        start_dir = arg
                    else:
                        session_name = arg
                        if len(parts) >= 3:
                            start_dir = parts[2]

                session_name = session_name or get_current_session()

                # Auto-resolve working directory from session name if not specified
                if not start_dir and session_name:
                    candidate = os.path.join(PROJECTS_BASE, session_name)
                    if os.path.isdir(candidate):
                        start_dir = candidate
                    elif os.path.isdir(PROJECTS_BASE):
                        start_dir = PROJECTS_BASE

                if tmux_exists(session_name):
                    self.reply(chat_id, f"⚠️ tmux '{session_name}' already exists.\n\n💡 Use /attach {session_name} to switch or /restart to restart Claude")
                    return

                success, msg = tmux_create(session_name, start_dir, start_claude=True)
                if success:
                    dir_info = f" in `{start_dir}`" if start_dir else ""
                    self.reply(chat_id, f"✅ Created tmux session '{session_name}'{dir_info} and started Claude Code")
                else:
                    self.reply(chat_id, f"❌ {msg}")
                return

            if cmd == "/restart":
                parts = text.split(maxsplit=1)
                start_dir = parts[1].strip() if len(parts) > 1 else None
                success, msg, prev_sid = tmux_restart_claude(session=get_current_session(), start_dir=start_dir)
                if success:
                    if prev_sid:
                        kb = [[{"text": "Resume previous session", "callback_data": f"resume:{prev_sid}"}]]
                        telegram_api("sendMessage", {
                            "chat_id": chat_id,
                            "text": f"✅ Claude Code restarted\n\nPrevious session: {prev_sid[:8]}...",
                            "reply_markup": {"inline_keyboard": kb}
                        })
                    else:
                        self.reply(chat_id, "✅ Claude Code restarted")
                else:
                    self.reply(chat_id, f"❌ {msg}")
                return

            if cmd == "/screenshot":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                result = subprocess.run(
                    ["tmux", "capture-pane", "-t", get_current_session(), "-p"],
                    capture_output=True, text=True
                )
                content = result.stdout.strip()
                if not content:
                    self.reply(chat_id, "(empty screen)")
                    return
                # Truncate if too long
                if len(content) > 3500:
                    content = content[-3500:]
                    content = "...\n" + content
                self.reply(chat_id, f"```\n{content}\n```")
                return

            if cmd == "/scroll":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                parts = text.split()
                lines = 50  # default
                if len(parts) > 1:
                    try:
                        lines = int(parts[1])
                    except:
                        pass
                lines = min(lines, 200)  # cap at 200
                result = subprocess.run(
                    ["tmux", "capture-pane", "-t", get_current_session(), "-p", "-S", f"-{lines}"],
                    capture_output=True, text=True
                )
                content = result.stdout.strip()
                if not content:
                    self.reply(chat_id, "(empty)")
                    return
                # Truncate if too long
                if len(content) > 3500:
                    content = content[-3500:]
                    content = "...\n" + content
                self.reply(chat_id, f"```\n{content}\n```")
                return

            # Claude shortcuts
            if cmd == "/usage":
                if tmux_exists():
                    tmux_send("/usage")
                    tmux_send_enter()
                    time.sleep(1.5)
                    content = strip_ansi(capture_tmux_pane() or "")
                    # Close the usage panel
                    tmux_send_escape()
                    # Find the last "Current session" block (avoid duplicates from prior captures)
                    lines = content.split('\n')
                    start_idx = -1
                    for i, l in enumerate(lines):
                        if 'Current session' in l:
                            start_idx = i
                    if start_idx >= 0:
                        usage_lines = []
                        for l in lines[start_idx:]:
                            s = l.strip()
                            if 'Esc to cancel' in s or 'to cycle' in s:
                                break
                            if not s:
                                continue
                            usage_lines.append(s)
                        if usage_lines:
                            self.reply(chat_id, '\n'.join(usage_lines))
                            return
                    self.reply(chat_id, "Could not capture usage output")
                return

            if cmd == "/commit":
                if tmux_exists():
                    tmux_send("/commit")
                    tmux_send_enter()
                    self.reply(chat_id, "Sent: /commit")
                return

            if cmd == "/undo":
                if tmux_exists():
                    tmux_send("/undo")
                    tmux_send_enter()
                    self.reply(chat_id, "Sent: /undo")
                return

            if cmd == "/diff":
                if tmux_exists():
                    tmux_send("show me the git diff")
                    tmux_send_enter()
                    self.reply(chat_id, "Sent: show me the git diff")
                return

            if cmd == "/stop":
                if tmux_exists():
                    tmux_send_escape()
                if os.path.exists(PENDING_FILE):
                    os.remove(PENDING_FILE)
                with _prompt_lock:
                    if _prompt_keyboard_message_id and _prompt_keyboard_chat_id:
                        dismiss_prompt_keyboard(
                            _prompt_keyboard_chat_id,
                            _prompt_keyboard_message_id,
                            "Interrupted by /stop"
                        )
                    _prompt_last_fingerprint = None
                    _prompt_keyboard_message_id = None
                    _prompt_keyboard_chat_id = None
                    _prompt_current_options = []
                self.reply(chat_id, "Interrupted")
                return

            if cmd == "/pick":
                parts = text.split()
                if len(parts) < 2:
                    self.reply(chat_id, "Usage: /pick <number>\n\nSelects option N from the current interactive prompt.\nUse /screenshot to see available options.")
                    return
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                pick_val = parts[1].strip()
                if pick_val.lower() in ("dismiss", "esc", "escape"):
                    tmux_send_escape()
                    self.reply(chat_id, "Sent Escape")
                    return
                try:
                    target_idx = int(pick_val)
                except ValueError:
                    self.reply(chat_id, "Usage: /pick <number> (0-based index)")
                    return
                with _prompt_lock:
                    options = _prompt_current_options[:]
                    highlighted_idx = _prompt_highlighted_index
                if options:
                    if target_idx < 0 or target_idx >= len(options):
                        self.reply(chat_id, f"Index out of range. Valid: 0-{len(options)-1}")
                        return
                    select_prompt_option(target_idx, highlighted_idx, len(options))
                    label = options[target_idx][0] if isinstance(options[target_idx], list) else options[target_idx]
                    self.reply(chat_id, f"Selected option {target_idx}: {label}")
                else:
                    select_prompt_option(target_idx, 0, target_idx + 1)
                    self.reply(chat_id, f"Sent {target_idx} Down arrow(s) + Enter (no active prompt tracked)")
                return

            if cmd == "/clear":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                tmux_send_escape()
                time.sleep(0.2)
                tmux_send("/clear")
                tmux_send_enter()
                self.reply(chat_id, "Cleared")
                return

            if cmd == "/continue_":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                if not _stop_claude():
                    self.reply(chat_id, "Failed to stop Claude")
                    return
                tmux_send("claude --continue --dangerously-skip-permissions")
                tmux_send_enter()
                self.reply(chat_id, "Continuing...")
                return

            if cmd == "/loop":
                if not tmux_exists():
                    self.reply(chat_id, "tmux not found")
                    return
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    self.reply(chat_id, "Usage: /loop <prompt>")
                    return
                prompt = parts[1].replace('"', '\\"')
                full = f'{prompt} Output <promise>DONE</promise> when complete.'
                with open(PENDING_FILE, "w") as f:
                    f.write(str(int(time.time())))
                threading.Thread(target=send_typing_loop, args=(chat_id,), daemon=True).start()
                threading.Thread(target=prompt_monitor_loop, args=(chat_id,), daemon=True).start()
                tmux_send(f'/ralph-loop:ralph-loop "{full}" --max-iterations 5 --completion-promise "DONE"')
                time.sleep(0.3)
                tmux_send_enter()
                self.reply(chat_id, "Ralph Loop started (max 5 iterations)")
                return

            if cmd == "/resume":
                sessions = get_recent_sessions()
                if not sessions:
                    self.reply(chat_id, "No sessions")
                    return
                kb = [[{"text": "Continue most recent", "callback_data": "continue_recent"}]]
                for s in sessions:
                    sid = get_session_id(s.get("project", ""))
                    if sid:
                        kb.append([{"text": s.get("display", "?")[:40] + "...", "callback_data": f"resume:{sid}"}])
                telegram_api("sendMessage", {"chat_id": chat_id, "text": "Select session:", "reply_markup": {"inline_keyboard": kb}})
                return

            if cmd in BLOCKED_COMMANDS:
                self.reply(chat_id, f"'{cmd}' not supported (interactive)")
                return

            # Settings toggles
            if cmd in ["/verbose", "/coauthor", "/signature"]:
                setting_name = cmd[1:]  # Remove leading /
                parts = text.split()
                settings = load_settings()
                if len(parts) < 2:
                    # Show current value
                    val = "on" if settings.get(setting_name) else "off"
                    self.reply(chat_id, f"{setting_name}: {val}")
                    return
                new_val = parts[1].lower() in ["on", "true", "1", "yes"]
                settings[setting_name] = new_val
                save_settings(settings)
                self.reply(chat_id, f"{setting_name}: {'on' if new_val else 'off'}")
                return

        # Regular message
        print(f"[{chat_id}] {text[:50]}...")

        # If there's an active interactive prompt and user typed free text,
        # navigate to "Type something" option, select it, and type the text.
        if tmux_exists() and check_and_show_prompt(chat_id):
            with _prompt_lock:
                options = _prompt_current_options[:]
                highlighted_idx = _prompt_highlighted_index
                saved_msg_id = _prompt_keyboard_message_id
                saved_chat_id = _prompt_keyboard_chat_id
                _prompt_keyboard_message_id = None
                _prompt_keyboard_chat_id = None
                # Keep _prompt_last_fingerprint so monitor won't re-send keyboard
                _prompt_current_options = []

            # Find the "Type something" / "Other" option
            type_idx = None
            for i, opt in enumerate(options):
                lbl = opt[0] if isinstance(opt, list) else opt
                clean = re.sub(r'^\d+\.\s*', '', lbl).strip().lower().rstrip('.')
                if clean in ("other", "type something"):
                    type_idx = i
                    break

            session = get_current_session()

            if type_idx is not None:
                # Navigate to "Type something" with arrow keys (NO Enter!)
                # Then just start typing — the TUI switches to text input mode
                moves = type_idx - highlighted_idx
                for _ in range(abs(moves)):
                    key = "Down" if moves > 0 else "Up"
                    subprocess.run(["tmux", "send-keys", "-t", session, key], timeout=5)
                    time.sleep(0.05)
                time.sleep(0.2)
                # Type the custom text directly and submit
                tmux_send(text, literal=True, session=session)
                tmux_send_enter(session)
            else:
                # No "Type something" option — escape and send as regular message
                tmux_send_escape(session)
                time.sleep(0.5)
                tmux_send(text, literal=True, session=session)
                tmux_send_enter(session)

            if saved_msg_id and saved_chat_id:
                dismiss_prompt_keyboard(
                    saved_chat_id, saved_msg_id,
                    f"Custom answer: {text[:40]}"
                )
            return

        with open(PENDING_FILE, "w") as f:
            f.write(str(int(time.time())))

        if msg_id:
            telegram_api("setMessageReaction", {"chat_id": chat_id, "message_id": msg_id, "reaction": [{"type": "emoji", "emoji": "\u2705"}]})

        # Auto-create tmux session if it doesn't exist
        if not tmux_exists():
            self.reply(chat_id, "🔄 tmux session not found, creating...")
            success, msg = tmux_create(start_claude=True)
            if not success:
                self.reply(chat_id, f"❌ Failed to create session: {msg}")
                os.remove(PENDING_FILE)
                return
            self.reply(chat_id, "✅ Created tmux session and started Claude Code. Waiting for Claude to initialize...")
            time.sleep(3)  # Wait for Claude to start

        # Build message with settings prefix
        settings = load_settings()
        prefix_parts = []
        if not settings.get("coauthor", True):
            prefix_parts.append("no Co-Authored-By")
        if not settings.get("signature", True):
            prefix_parts.append("no 'Generated with Claude' signatures")

        message = text
        if prefix_parts:
            prefix = f"[Note: {', '.join(prefix_parts)} in commits/PRs] "
            message = prefix + text

        threading.Thread(target=send_typing_loop, args=(chat_id,), daemon=True).start()
        threading.Thread(target=prompt_monitor_loop, args=(chat_id,), daemon=True).start()
        tmux_send(message)
        # Long messages trigger bracketed paste in Claude Code's TUI;
        # Enter sent too quickly gets lost, so add a delay proportional to length
        if len(message) > 200:
            time.sleep(0.5)
        tmux_send_enter()

    def _process_media_group(self, media_group_id):
        """Process a buffered media group (album) after debounce timeout."""
        with _media_group_lock:
            group = _media_group_buffer.pop(media_group_id, None)
        if not group or not group["msgs"]:
            return

        chat_id = group["chat_id"]
        msg_id = group["msg_id"]
        msgs = group["msgs"]

        with open(CHAT_ID_FILE, "w") as f:
            f.write(str(chat_id))

        # Download all photos
        paths = []
        for m in msgs:
            photos = m.get("photo", [])
            if not photos:
                continue
            file_id = photos[-1].get("file_id")
            local_path = download_telegram_file(file_id)
            if local_path:
                paths.append(local_path)

        if not paths:
            self.reply(chat_id, "Failed to download photos")
            return

        # React to first message
        if msg_id:
            telegram_api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "reaction": [{"type": "emoji", "emoji": "\U0001F4F7"}]
            })

        # Auto-create tmux session if needed
        if not tmux_exists():
            self.reply(chat_id, "tmux session not found, creating...")
            success, err = tmux_create(start_claude=True)
            if not success:
                self.reply(chat_id, f"Photos saved but failed to create session: {err}")
                return
            self.reply(chat_id, "Created tmux session. Waiting for Claude to initialize...")
            time.sleep(3)

        # Build prompt with all image paths
        caption = msgs[0].get("caption", "").strip()
        image_refs = " ".join(f"[Image: {p}]" for p in paths)
        if caption:
            prompt = f"{caption} {image_refs}"
        else:
            prompt = f"Please analyze these {len(paths)} images: {image_refs}"

        print(f"[{chat_id}] Album: {len(paths)} photos")
        with open(PENDING_FILE, "w") as f:
            f.write(str(int(time.time())))

        threading.Thread(target=send_typing_loop, args=(chat_id,), daemon=True).start()
        threading.Thread(target=prompt_monitor_loop, args=(chat_id,), daemon=True).start()
        tmux_send(prompt)
        tmux_send_enter()

    def handle_photo(self, msg, chat_id, msg_id):
        """Handle single photo uploads - download and send path to Claude."""
        with open(CHAT_ID_FILE, "w") as f:
            f.write(str(chat_id))

        # Get largest photo (last in array)
        photos = msg.get("photo", [])
        if not photos:
            return
        file_id = photos[-1].get("file_id")

        # Download photo
        local_path = download_telegram_file(file_id)
        if not local_path:
            self.reply(chat_id, "Failed to download photo")
            return

        # React to confirm receipt
        if msg_id:
            telegram_api("setMessageReaction", {
                "chat_id": chat_id,
                "message_id": msg_id,
                "reaction": [{"type": "emoji", "emoji": "\U0001F4F7"}]  # camera emoji
            })

        # Auto-create tmux session if it doesn't exist
        if not tmux_exists():
            self.reply(chat_id, "🔄 tmux session not found, creating...")
            success, msg = tmux_create(start_claude=True)
            if not success:
                self.reply(chat_id, f"Photo saved: {local_path}\n❌ Failed to create session: {msg}")
                return
            self.reply(chat_id, "✅ Created tmux session and started Claude Code. Waiting for Claude to initialize...")
            time.sleep(3)

        # Build message with caption if provided
        caption = msg.get("caption", "").strip()
        if caption:
            # Use single line - newlines in tmux send-keys are interpreted as Enter
            prompt = f"{caption} [Image: {local_path}]"
        else:
            prompt = f"Please analyze this image: {local_path}"

        print(f"[{chat_id}] Photo: {local_path}")
        with open(PENDING_FILE, "w") as f:
            f.write(str(int(time.time())))

        threading.Thread(target=send_typing_loop, args=(chat_id,), daemon=True).start()
        threading.Thread(target=prompt_monitor_loop, args=(chat_id,), daemon=True).start()
        tmux_send(prompt)
        if len(prompt) > 200:
            time.sleep(0.5)
        tmux_send_enter()

    def reply(self, chat_id, text):
        telegram_api("sendMessage", {"chat_id": chat_id, "text": text})

    def log_message(self, *args):
        pass


def main():
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        return
    setup_bot_commands()
    print(f"Bridge on :{PORT} | tmux: {get_current_session()}")

    # If a pending request exists from before restart, start a monitor thread
    if os.path.exists(PENDING_FILE) and os.path.exists(CHAT_ID_FILE):
        try:
            with open(CHAT_ID_FILE) as f:
                chat_id = int(f.read().strip())
            print(f"Resuming prompt monitor for chat_id={chat_id}")
            threading.Thread(target=prompt_monitor_loop, args=(chat_id,), daemon=True).start()
        except Exception as e:
            print(f"Failed to resume monitor: {e}")

    try:
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
