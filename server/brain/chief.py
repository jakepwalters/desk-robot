"""Hand real work to Chief, the human's main Grok Bot, and read Chief's answer out when it comes back.

Grok Bot has no chat API, so this is a one-way pipe each direction:

    out: the `ask_chief` tool POSTs {"text", "sent_at", "reply_url", "reply_secret"} to the Grok Bot routine's
         webhook (CHIEF_WEBHOOK_URL, "Authorization: Bearer <CHIEF_WEBHOOK_KEY>")
    back: Chief's run POSTs {"reply": ...} to CHIEF_REPLY_URL (a tunnel to /chief-reply on a small server of its own,
          CHIEF_REPLY_PORT, default 8768) with the secret in "X-Chief-Secret". The tunnel (cloudflared) points at that
          port only; the brain's own control server stays this-computer-only.

Chief only does read-only work from a webhook: anything that sends, deletes or changes something waits for the human
to confirm in the Grok Bot app.

Off unless CHIEF_WEBHOOK_URL is set in server/.env: no tool, no server.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import config  # noqa: F401  (loads server/.env into os.environ first)

WEBHOOK_URL = os.environ.get("CHIEF_WEBHOOK_URL", "")
WEBHOOK_KEY = os.environ.get("CHIEF_WEBHOOK_KEY", "")
REPLY_URL = os.environ.get("CHIEF_REPLY_URL", "")   # public https URL ending in /chief-reply
REPLY_SECRET = os.environ.get("CHIEF_REPLY_SECRET", "")
REPLY_PORT = int(os.environ.get("CHIEF_REPLY_PORT") or 8768)
MAX_REPLY = 64 * 1024   # bytes: a spoken answer plus Chief's testing fields (raw), not a report

ENABLED = bool(WEBHOOK_URL)
_pending: dict[str, float] = {}   # request id -> when it was sent, for the round trip in the log


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
            "answer out when it arrives. From you Chief only looks things up: anything that sends, deletes or "
            "changes something, Chief asks your human to confirm in the Grok Bot app. Say one short line as you "
            "call it (like \"Sent to Chief.\"); that's all."),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "the request in full, in your human's words where you can"},
        }, "required": ["task"]},
    }}]


def ask_chief(args: dict) -> str:
    """The `ask_chief` ability: start a Chief run. Returns the line for the model; raises if it didn't go."""
    task = str(args.get("task") or "").strip()
    if not task:
        raise ValueError("no task given: ask your human what Chief should do")
    rid = uuid.uuid4().hex[:12]
    msg = {"text": task, "id": rid, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if REPLY_URL and REPLY_SECRET:
        msg |= {"reply_url": REPLY_URL, "reply_secret": REPLY_SECRET}
    body = json.dumps(msg).encode()
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
    _pending[rid] = time.time()
    print(f"  (sent to Chief [{rid}]: {task})")
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
            given = (self.headers.get("X-Chief-Secret")
                     or (self.headers.get("Authorization") or "").removeprefix("Bearer ")).strip()
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
                except json.JSONDecodeError:
                    data = raw   # plain text is fine too
                if not isinstance(data, dict):
                    data = {"reply": str(data)}
                # Only "reply" is spoken. "raw" (Chief's echo of the request) isn't logged: it holds reply_secret.
                text = str(data.get("reply") or data.get("text") or "").strip()
                if not text:
                    raise ValueError("no reply")
            except ValueError as e:
                self._reply(400, {"error": f"bad request: {e}"})
                return
            rid = str(data.get("id") or "")
            sent = _pending.pop(rid, None)
            facts = [f"id {rid}" if rid else "no id"]
            if data.get("took_s") is not None:
                facts.append(f"Chief took {data['took_s']}s")
            if sent is not None:
                facts.append(f"round trip {time.time() - sent:.0f}s")
            if data.get("needs_confirm"):
                facts.append("needs your OK in Grok Bot")
            print(f"  (from Chief: {' · '.join(facts)})")
            on_reply(text)
            self._reply(200, {"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", REPLY_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="chief-reply").start()
    print(f"chief: tasks go to the Grok Bot webhook; answers come back on http://127.0.0.1:{REPLY_PORT}/chief-reply")
