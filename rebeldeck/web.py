"""Deck pages, the web GUI and RebelDeck's API, served to this computer and
(optionally) to phones and tablets on the network.

Pages
  GET  /                       pick a deck (phone-friendly)
  GET  /deck/<id>              a deck
  GET  /gui                    the web GUI

API (JSON)
  GET  /api/status             app, Companion connection and decks
  GET  /api/presets            deck size presets
  GET  /api/decks              decks
  POST /api/decks              add a deck            {name, columns, rows}
  PUT  /api/decks/<id>         change a deck         {name, columns, rows, showing}
  DELETE /api/decks/<id>       remove a deck
  POST /api/decks/sync         decks from another rig (Companion Sync, this computer only)
  GET  /api/decks/<id>/state   every button
  GET  /api/decks/<id>/events  live updates (server-sent events)
  POST /api/decks/<id>/press   {"key": n, "pressed": true|false}
  GET  /api/settings           app settings
  PUT  /api/settings           change app settings
  GET  /api/update             is there a newer version?
  POST /api/update             install it
  GET  /api/log                the last lines of the log
  POST /api/quit               quit RebelDeck (this computer only)

Other devices can open decks and press buttons. If a web password is set,
they need it for the web GUI and for changing anything.
"""
import base64
import hmac
import json
import logging
import os
import queue
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, settings

log = logging.getLogger("rebeldeck.web")
HERE = os.path.dirname(os.path.abspath(__file__))


def _page(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


class Hub:
    """Fans changes out to every open page."""

    def __init__(self, manager):
        self.m = manager
        self.clients = {}           # queue -> deck id (None: every deck)
        self.lock = threading.Lock()
        manager.listeners.append(self._on_change)

    def _on_change(self, kind, deck_id, value):
        deck = self.m.deck(deck_id) if deck_id else None
        if kind == "key" and deck:
            msg = ("key", deck.key_json(value))
        elif kind == "status" and deck:
            msg = ("status", {"status": value, "companion": deck.companion_version})
        elif kind == "brightness":
            msg = ("brightness", {"value": value})
        elif kind == "decks":
            msg = ("reload", {})
        elif kind == "settings":
            msg = ("settings", {})
        else:
            return
        with self.lock:
            for q, want in list(self.clients.items()):
                if want is None or want == deck_id or kind == "decks" and deck_id is None:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass                  # a stalled page; it catches up when it reconnects

    def subscribe(self, deck_id):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients[q] = deck_id
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.pop(q, None)


DECK_URL = re.compile(r"^/(?:api/)?decks?/([0-9a-z]+)(/[a-z]+)?$")


def make_server(manager, app=None):
    """app: optional hooks from the desktop app (update, quit, log, windows)."""
    hub = Hub(manager)
    pages = {"deck": _page("deck.html"), "gui": _page("gui.html"), "home": _page("home.html")}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "RebelDeck/" + __version__

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---- helpers ---------------------------------------------------------
        def local(self):
            return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

        def allowed(self):
            """The web GUI and changes: this computer always; others need the
            password, if there is one."""
            pw = manager.cfg.get("web_password") or ""
            if self.local() or not pw:
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    given = base64.b64decode(auth[6:]).decode().split(":", 1)[-1]
                except ValueError:
                    return False
                return hmac.compare_digest(given, pw)
            return False

        def ask_password(self):
            body = b'{"error":"password needed"}'
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="RebelDeck"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send(self, code, body, ctype="application/json"):
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body)
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > 1_000_000:
                raise ValueError("too big")
            data = self.rfile.read(n) if n else b"{}"
            out = json.loads(data or b"{}")
            if not isinstance(out, dict):
                raise ValueError("not an object")
            return out

        def deck_page(self, deck_id):
            d = next((x for x in manager.cfg["decks"] if x["id"] == deck_id), None)
            if not d:
                return self.send(404, "<h1>No such deck</h1><p><a href='/'>Back to the decks</a></p>",
                                 "text/html; charset=utf-8")
            html = (pages["deck"].replace("{{DECK}}", d["id"]).replace("{{COLS}}", str(d["columns"]))
                    .replace("{{ROWS}}", str(d["rows"])).replace("{{KEYPX}}", str(int(manager.cfg["key_px"])))
                    .replace("{{NAME}}", json.dumps(d["name"])[1:-1].replace("<", "&lt;")))
            self.send(200, html, "text/html; charset=utf-8")

        # ---- GET -------------------------------------------------------------
        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path == "/":
                return self.send(200, pages["home"], "text/html; charset=utf-8")
            if path == "/gui":
                if not self.allowed():
                    return self.ask_password()
                return self.send(200, pages["gui"].replace("{{VERSION}}", __version__), "text/html; charset=utf-8")
            m = DECK_URL.match(path)
            if m and path.startswith("/deck/") and not m.group(2):
                return self.deck_page(m.group(1))
            if path == "/api/status":
                st = manager.status()
                st["version"] = __version__
                st["label"] = app.label() if app else f"v{__version__}"
                st["local"] = self.local()
                st["restart_needed"] = manager.restart_needed
                if app:
                    st["update"] = app.update_cached()
                    st["windows"] = app.windows_showing()
                    st["lan"] = app.lan_url()
                return self.send(200, st)
            if path == "/api/presets":
                return self.send(200, [{"name": n, "columns": c, "rows": r} for n, c, r in settings.PRESETS])
            if path == "/api/decks":
                return self.send(200, manager.deck_list())
            if path == "/api/settings":
                if not self.allowed():
                    return self.ask_password()
                return self.send(200, manager.app_settings())
            if path == "/api/update":
                force = "force=1" in self.path
                return self.send(200, app.check_update(force) if app else {"newer": False, "current": __version__})
            if path == "/api/log":
                if not self.allowed():
                    return self.ask_password()
                return self.send(200, {"lines": app.log_tail(200) if app else []})
            if m and path.startswith("/api/decks/"):
                deck = manager.deck(m.group(1))
                if m.group(2) == "/state":
                    return self.send(200, deck.snapshot() if deck else {"gone": True})
                if m.group(2) == "/events":
                    return self.events(m.group(1))
            self.send(404, {"error": "not found"})

        def events(self, deck_id):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe(deck_id)
            try:
                deck = manager.deck(deck_id)
                self.event("state", deck.snapshot() if deck else {"gone": True})
                while True:
                    try:
                        kind, data = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    if kind == "reload":
                        # the deck was resized, renamed or removed: send the new state
                        deck = manager.deck(deck_id)
                        self.event("state", deck.snapshot() if deck else {"gone": True})
                        continue
                    self.event(kind, data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)
                self.close_connection = True

        def event(self, kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        # ---- POST / PUT / DELETE ------------------------------------------------
        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            m = DECK_URL.match(path)
            try:
                if m and m.group(2) == "/press":
                    # pressing buttons is what decks are for: no password
                    deck = manager.deck(m.group(1))
                    b = self.body()
                    if not deck:
                        return self.send(404, {"error": "no such deck"})
                    deck.press(int(b["key"]), bool(b["pressed"]))
                    return self.send(200, {"ok": True})
                if not self.allowed():
                    return self.ask_password()
                if path == "/api/decks":
                    b = self.body()
                    return self.send(200, manager.add_deck(b.get("name"), b.get("columns"), b.get("rows")))
                if path == "/api/decks/sync":
                    # deck layouts from another rig, via Companion Sync on this computer
                    if not self.local():
                        return self.send(403, {"error": "only from this computer"})
                    decks = self.body().get("decks")
                    if not isinstance(decks, list) or not all(isinstance(d, dict) for d in decks):
                        raise ValueError("decks: a list")
                    manager.apply_synced_decks(decks)
                    return self.send(200, {"ok": True, "decks": len(decks)})
                if path == "/api/update":
                    if not app:
                        return self.send(400, {"error": "updates come from the app"})
                    threading.Thread(target=app.update_now, daemon=True).start()
                    return self.send(200, {"ok": True})
                if path == "/api/quit":
                    if not (app and self.local()):
                        return self.send(403, {"error": "only from this computer"})
                    threading.Timer(0.3, app.quit).start()
                    return self.send(200, {"ok": True})
            except (ValueError, KeyError, TypeError) as e:
                return self.send(400, {"error": f"bad request: {e}"})
            self.send(404, {"error": "not found"})

        def do_PUT(self):
            if not self.allowed():
                return self.ask_password()
            path = self.path.split("?", 1)[0].rstrip("/")
            m = DECK_URL.match(path)
            try:
                if path == "/api/settings":
                    return self.send(200, manager.set_app_settings(self.body()))
                if m and path.startswith("/api/decks/") and not m.group(2):
                    return self.send(200, manager.update_deck(m.group(1), self.body()))
            except KeyError:
                return self.send(404, {"error": "no such deck"})
            except (ValueError, TypeError) as e:
                return self.send(400, {"error": f"bad request: {e}"})
            self.send(404, {"error": "not found"})

        def do_DELETE(self):
            if not self.allowed():
                return self.ask_password()
            path = self.path.split("?", 1)[0].rstrip("/")
            m = DECK_URL.match(path)
            if m and path.startswith("/api/decks/") and not m.group(2):
                try:
                    manager.remove_deck(m.group(1))
                except KeyError:
                    return self.send(404, {"error": "no such deck"})
                return self.send(200, {"ok": True})
            self.send(404, {"error": "not found"})

    host = "0.0.0.0" if manager.cfg.get("web_network", True) else "127.0.0.1"
    server = ThreadingHTTPServer((host, int(manager.cfg["web_port"])), Handler)
    server.daemon_threads = True
    return server
