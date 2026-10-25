"""Hand real work to Chief, the human's main Grok Bot, and read Chief's answer out when it comes back.

Grok Bot has no chat API, so this is a one-way pipe each direction:

    out: the `ask_chief` tool POSTs the task to a Grok Bot routine's webhook (CHIEF_WEBHOOK_URL / CHIEF_WEBHOOK_KEY)
    back: Chief's run POSTs its answer to /chief-reply on a small server of its own (CHIEF_REPLY_PORT, default 8768),
          with "Authorization: Bearer <CHIEF_REPLY_SECRET>". A tunnel (cloudflared) points at that port only; the
          brain's own control server stays this-computer-only.

Off unless CHIEF_WEBHOOK_URL is set in server/.env: no tool, no server.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import config  # noqa: F401  (loads server/.env into os.environ first)

WEBHOOK_URL = os.environ.get("CHIEF_WEBHOOK_URL", "")
WEBHOOK_KEY = os.environ.get("CHIEF_WEBHOOK_KEY", "")
REPLY_SECRET = os.environ.get("CHIEF_REPLY_SECRET", "")
REPLY_PORT = int(os.environ.get("CHIEF_REPLY_PORT") or 8768)
MAX_REPLY = 16 * 1024   # bytes: a spoken answer, not a report

ENABLED = bool(WEBHOOK_URL)


def tools() -> list[dict]:
    if not ENABLED:
        return []
    return [{"type": "function", "function": {
        "name": "ask_chief",
        "description": (
            "Hand a real task to Chief, your human's main assistant (Grok Bot). Chief has their email, calendar, "
            "accounts, files and memory, and can research and get things done. Use it when they ask for Chief, or "
            "for anything you can't do yourself: email, calendar, messages, research, plans, anything about their "
            "work or life you don't know. Chief answers later, often in a minute or more, and you will read the "
            "answer out when it arrives. Say one short line as you call it (like \"Sent to Chief.\"); that's all."),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "the request in full, in your human's words where you can"},
        }, "required": ["task"]},
    }}]


def ask_chief(args: dict) -> str:
    """The `ask_chief` ability: start a Chief run. Returns the line for the model; raises if it didn't go."""
    task = str(args.get("task") or "").strip()
    if not task:
        raise ValueError("no task given: ask your human what Chief should do")
    body = json.dumps({
        "task": task,
        "reply": "Answer in one to three short spoken sentences, then POST it as JSON {\"text\": \"...\"} "
                 "to the Groky reply endpoint from your routine instructions.",
    }).encode()
    req = urllib.request.Request(WEBHOOK_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    if WEBHOOK_KEY:
        req.add_header("Authorization", f"Bearer {WEBHOOK_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(1024)
    # A failure raises, so the brain doesn't count it as done and he gets to say it didn't go through.
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Chief's webhook refused the task (HTTP {e.code}); it didn't go through") from None
    except Exception as e:
        raise RuntimeError(f"couldn't reach Chief ({type(e).__name__}); it didn't go through") from None
    print(f"  (sent to Chief: {task})")
    return "Sent. Chief will answer later; you'll read it out when it arrives."


def serve(on_reply: Callable[[str], None]) -> None:
    """Listen for Chief's answers on 127.0.0.1:REPLY_PORT. on_reply(text) runs on the server's thread."""
    if not ENABLED:
        return
    if not REPLY_SECRET:
        print("(chief: CHIEF_REPLY_SECRET is not set in server/.env — replies from Chief will be refused)")

    class Handler(BaseHTTPRequestHandler):
        server_version = "desk-robot"
        sys_version = ""

        def log_message(self, *args) -> None:
            pass

        def _reply(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/chief-reply":
                self._reply(404, {"error": "not found"})
                return
            given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not REPLY_SECRET or not hmac.compare_digest(given.encode(), REPLY_SECRET.encode()):
                self._reply(401, {"error": "bad secret"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= MAX_REPLY:
                    raise ValueError("empty or too long")
                raw = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    data = json.loads(raw)
                    text = str(data.get("text") or "") if isinstance(data, dict) else str(data)
                except json.JSONDecodeError:
                    text = raw   # plain text is fine too
                text = text.strip()
                if not text:
                    raise ValueError("no text")
            except ValueError as e:
                self._reply(400, {"error": f"bad request: {e}"})
                return
            on_reply(text)
            self._reply(200, {"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", REPLY_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="chief-reply").start()
    print(f"chief: tasks go to the Grok Bot webhook; answers come back on http://127.0.0.1:{REPLY_PORT}/chief-reply")
