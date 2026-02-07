#!/usr/bin/env python3
"""Claude Code <-> Telegram Bridge"""

import os
import json
import subprocess
import threading
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

TMUX_SESSION = os.environ.get("TMUX_SESSION", "claude")
CHAT_ID_FILE = os.path.expanduser("~/.claude/telegram_chat_id")
PENDING_FILE = os.path.expanduser("~/.claude/telegram_pending")
HISTORY_FILE = os.path.expanduser("~/.claude/history.jsonl")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
UPLOAD_DIR = os.path.expanduser(os.environ.get("UPLOAD_DIR", "~/uploads"))

BOT_COMMANDS = [
    {"command": "clear", "description": "Clear conversation"},
    {"command": "resume", "description": "Resume session (shows picker)"},
    {"command": "continue_", "description": "Continue most recent session"},
    {"command": "loop", "description": "Ralph Loop: /loop <prompt>"},
    {"command": "stop", "description": "Interrupt Claude (Escape)"},
    {"command": "status", "description": "Check tmux status"},
]

BLOCKED_COMMANDS = [
    "/mcp", "/help", "/settings", "/config", "/model", "/compact", "/cost",
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


def tmux_exists():
    return subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True).returncode == 0


def tmux_send(text, literal=True):
    cmd = ["tmux", "send-keys", "-t", TMUX_SESSION]
    if literal:
        cmd.append("-l")
    cmd.append(text)
    subprocess.run(cmd)


def tmux_send_enter():
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Enter"])


def tmux_send_escape():
    subprocess.run(["tmux", "send-keys", "-t", TMUX_SESSION, "Escape"])


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
                self.handle_callback(update["callback_query"])
            elif "message" in update:
                self.handle_message(update)
        except Exception as e:
            print(f"Error: {e}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Claude-Telegram Bridge")

    def handle_callback(self, cb):
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        telegram_api("answerCallbackQuery", {"callback_query_id": cb.get("id")})

        if not tmux_exists():
            self.reply(chat_id, "tmux session not found")
            return

        if data.startswith("resume:"):
            session_id = data.split(":", 1)[1]
            tmux_send_escape()
            time.sleep(0.2)
            tmux_send("/exit")
            tmux_send_enter()
            time.sleep(0.5)
            tmux_send(f"claude --resume {session_id} --dangerously-skip-permissions")
            tmux_send_enter()
            self.reply(chat_id, f"Resuming: {session_id[:8]}...")

        elif data == "continue_recent":
            tmux_send_escape()
            time.sleep(0.2)
            tmux_send("/exit")
            tmux_send_enter()
            time.sleep(0.5)
            tmux_send("claude --continue --dangerously-skip-permissions")
            tmux_send_enter()
            self.reply(chat_id, "Continuing most recent...")

    def handle_message(self, update):
        msg = update.get("message", {})
        chat_id, msg_id = msg.get("chat", {}).get("id"), msg.get("message_id")
        if not chat_id:
            return

        # Handle photo uploads
        if "photo" in msg:
            self.handle_photo(msg, chat_id, msg_id)
            return

        text = msg.get("text", "")
        if not text:
            return

        with open(CHAT_ID_FILE, "w") as f:
            f.write(str(chat_id))

        if text.startswith("/"):
            cmd = text.split()[0].lower()

            if cmd == "/status":
                status = "running" if tmux_exists() else "not found"
                self.reply(chat_id, f"tmux '{TMUX_SESSION}': {status}")
                return

            if cmd == "/stop":
                if tmux_exists():
                    tmux_send_escape()
                if os.path.exists(PENDING_FILE):
                    os.remove(PENDING_FILE)
                self.reply(chat_id, "Interrupted")
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
                tmux_send_escape()
                time.sleep(0.2)
                tmux_send("/exit")
                tmux_send_enter()
                time.sleep(0.5)
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

        # Regular message
        print(f"[{chat_id}] {text[:50]}...")
        with open(PENDING_FILE, "w") as f:
            f.write(str(int(time.time())))

        if msg_id:
            telegram_api("setMessageReaction", {"chat_id": chat_id, "message_id": msg_id, "reaction": [{"type": "emoji", "emoji": "\u2705"}]})

        if not tmux_exists():
            self.reply(chat_id, "tmux not found")
            os.remove(PENDING_FILE)
            return

        threading.Thread(target=send_typing_loop, args=(chat_id,), daemon=True).start()
        tmux_send(text)
        tmux_send_enter()

    def handle_photo(self, msg, chat_id, msg_id):
        """Handle photo uploads - download and send path to Claude."""
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

        if not tmux_exists():
            self.reply(chat_id, f"Photo saved: {local_path}\n(tmux not found)")
            return

        # Build message with caption if provided
        caption = msg.get("caption", "").strip()
        if caption:
            prompt = f"{caption}\n\nImage: {local_path}"
        else:
            prompt = f"Please analyze this image: {local_path}"

        print(f"[{chat_id}] Photo: {local_path}")
        with open(PENDING_FILE, "w") as f:
            f.write(str(int(time.time())))

        threading.Thread(target=send_typing_loop, args=(chat_id,), daemon=True).start()
        tmux_send(prompt)
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
    print(f"Bridge on :{PORT} | tmux: {TMUX_SESSION}")
    try:
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
