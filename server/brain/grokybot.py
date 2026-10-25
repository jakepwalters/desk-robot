"""Hand real work to Groky Bot, the human's main agent in the Grok Bot app, and speak its answer when it comes back.

Grok Bot has no chat API, so this is a one-way pipe each direction:

    out: the `ask_groky_bot` tool POSTs {"text", "id", "sent_at", "reply_url", "reply_secret"} to the Grok Bot
         routine's webhook (GROKYBOT_WEBHOOK_URL, "Authorization: Bearer <GROKYBOT_WEBHOOK_KEY>")
    back: Groky Bot's run POSTs {"reply", "id", "took_s", "needs_confirm"} to GROKYBOT_REPLY_URL, a tunnel to
          /grokybot-reply on a small server of its own (GROKYBOT_REPLY_PORT, default 8768), with the secret in
          "X-Grokybot-Secret" (the older "X-Chief-Secret" works too). The tunnel (cloudflared) points at that port
          only; the brain's own control server stays this-computer-only.

Groky Bot only does read-only work from a webhook: anything that sends, deletes or changes something waits for the human
to confirm in the Grok Bot app.

Off unless GROKYBOT_WEBHOOK_URL is set in server/.env: no tool, no server.
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

WEBHOOK_URL = os.environ.get("GROKYBOT_WEBHOOK_URL", "")
WEBHOOK_KEY = os.environ.get("GROKYBOT_WEBHOOK_KEY", "")
REPLY_URL = os.environ.get("GROKYBOT_REPLY_URL", "")   # public https URL ending in /grokybot-reply
REPLY_SECRET = os.environ.get("GROKYBOT_REPLY_SECRET", "")
REPLY_PORT = int(os.environ.get("GROKYBOT_REPLY_PORT") or 8768)
MAX_REPLY = 64 * 1024   # bytes: a spoken answer plus Groky Bot's testing fields (raw), not a report

ENABLED = bool(WEBHOOK_URL)
_pending: dict[str, tuple[float, str]] = {}   # request id -> (when it was sent, the task), until the answer comes


class Update:
    """An answer from Groky Bot, waiting to be worked into the conversation."""

    def __init__(self, text: str, task: str = "", needs_confirm: bool = False) -> None:
        self.text, self.task, self.needs_confirm = text, task, needs_confirm
        self.logged = False   # printed to the log yet

    def note(self) -> str:
        about = f' to "{self.task}"' if self.task else ""
        ok = " It needs your human's OK in the Grok Bot app before anything happens: say so clearly." \
            if self.needs_confirm else ""
        return f"Answer{about}: {self.text}{ok}"


def tools() -> list[dict]:
    if not ENABLED:
        return []
    return [{"type": "function", "function": {
        "name": "ask_groky_bot",
        "description": (
            "Hand a real task to your full self in the Grok Bot app (Groky Bot). It has your human's email, "
            "calendar, accounts, files and memory, and can research and get things done. Use it for anything you "
            "can't do here: email, calendar, messages, research, plans, anything about their work or life you don't "
            "know. The answer comes back later, often in a minute or more, as an update you then tell your human "
            "about. Keep talking with them normally meanwhile. From here it only looks things up: anything that sends, deletes or changes something waits for "
            "your human's OK in the Grok Bot app. Say one short line as you call it (like \"Checking on that.\"); "
            "that's all. You and Groky Bot are the same assistant to your human: don't talk about handing off."),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "the request in full, in your human's words where you can"},
        }, "required": ["task"]},
    }}]


def ask_groky_bot(args: dict) -> str:
    """The `ask_groky_bot` ability: start a Groky Bot run. Returns the line for the model; raises if it didn't go."""
    task = str(args.get("task") or "").strip()
    if not task:
        raise ValueError("no task given: ask your human what Groky Bot should do")
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
        raise RuntimeError(f"Groky Bot's webhook refused the task (HTTP {e.code}); it didn't go through") from None
    except Exception as e:
        raise RuntimeError(f"couldn't reach Groky Bot ({type(e).__name__}); it didn't go through") from None
    _pending[rid] = (time.time(), task)
    print(f"  (sent to Groky Bot [{rid}]: {task})")
    return "Sent. The answer comes back later as an update; keep talking normally until then."


def serve(on_reply: Callable[[Update], None]) -> None:
    """Listen for Groky Bot's answers on 127.0.0.1:REPLY_PORT. on_reply(update) runs on the server's thread."""
    if not ENABLED:
        return
    if not REPLY_SECRET:
        print("(groky bot: GROKYBOT_REPLY_SECRET is not set in server/.env — replies from Groky Bot will be refused)")

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
            if self.path != "/grokybot-reply":
                self._reply(404, {"error": "not found"})
                return
            given = (self.headers.get("X-Grokybot-Secret") or self.headers.get("X-Chief-Secret")
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
                # Only "reply" is spoken. "raw" (Groky Bot's echo of the request) isn't logged: it holds reply_secret.
                text = str(data.get("reply") or data.get("text") or "").strip()
                if not text:
                    raise ValueError("no reply")
            except ValueError as e:
                self._reply(400, {"error": f"bad request: {e}"})
                return
            rid = str(data.get("id") or "")
            sent, task = _pending.pop(rid, (None, ""))
            facts = [f"id {rid}" if rid else "no id"]
            if data.get("took_s") is not None:
                facts.append(f"Groky Bot took {data['took_s']}s")
            if sent is not None:
                facts.append(f"round trip {time.time() - sent:.0f}s")
            if data.get("needs_confirm"):
                facts.append("needs your OK in Grok Bot")
            print(f"  (from Groky Bot: {' · '.join(facts)})")
            on_reply(Update(text, task, bool(data.get("needs_confirm"))))
            self._reply(200, {"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", REPLY_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="grokybot-reply").start()
    print(f"groky bot: tasks go to the Grok Bot webhook; answers come back on http://127.0.0.1:{REPLY_PORT}/grokybot-reply")
