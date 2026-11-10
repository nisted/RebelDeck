"""Deck pages, the web GUI and RebelDeck's API, served to this computer and
(optionally) to phones and tablets on the network.

A deck can be named in a URL by its address name ("foh-mini") or its id.

Pages
  GET  /                         pick a deck (any device), from this rig and others
  GET  /deck/<deck>              a deck            ?window=1: in RebelDeck's own window
  GET  /deck/<deck>?key=<n>      one broken-out key
  GET  /gui                      the web GUI

API (JSON)
  GET  /api/status               app, Companion servers and decks
  GET  /api/info                 addresses phones can use
  GET  /api/rigs                 decks on other RebelDecks on the network
  GET  /api/events               every deck's buttons, live (server-sent events)
  GET  /api/presets              deck size presets
  GET  /api/decks                decks
  POST /api/decks                add a deck            {name, columns, rows}
  PUT  /api/decks/<deck>         change a deck         (any deck setting)
  DELETE /api/decks/<deck>       remove a deck
  POST /api/decks/sync           decks from another rig (Rebel Sync, this computer only)
  GET  /api/decks/<deck>/state   every button
  GET  /api/decks/<deck>/events  live updates (server-sent events)
  GET  /api/decks/<deck>/qr.png  QR code of the deck's address
  POST /api/decks/<deck>/press   {"key": n, "pressed": true|false}
  POST /api/decks/<deck>/rotate  {"key": n, "steps": ±n}
  POST /api/decks/<deck>/fader   {"key": n, "value": 0-127}
  POST /api/decks/<deck>/let-go  {"key": n}: a held sticky key lets go
  POST /api/decks/<deck>/tap     {"key": n}: press and release (hotkeys; this computer only)
  POST /api/decks/<deck>/identify   flash its name wherever it's open
  GET  /api/qr.png               QR code of the pick-a-deck page
  GET  /<n>                      short address: deck n (in the Decks page's order)
  GET  /api/companions/scan      Companions on this computer and the local network
  GET/POST /api/servers          Companion servers   {name, host, port, default}
  PUT/DELETE /api/servers/<id>
  GET/POST /api/profiles         saved sets of decks  {name}
  POST /api/profiles/<name>/load
  DELETE /api/profiles/<name>
  GET  /api/settings             app settings
  PUT  /api/settings             change app settings
  GET  /api/update               is there a newer version?
  POST /api/update               install it
  GET  /api/log                  the last lines of the log
  POST /api/quit                 quit RebelDeck (this computer only)
  POST /api/popout               show the pop-out (this computer only)

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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, qr, settings
from .manager import NameInUse

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
            msg = ("key", dict(deck.key_json(value), deck=deck_id))
        elif kind == "status" and deck:
            msg = ("status", {"status": value, "companion": deck.companion_version, "deck": deck_id})
        elif kind == "brightness":
            msg = ("brightness", {"value": value, "deck": deck_id})
        elif kind == "fader":
            msg = ("fader", {"i": value[0], "value": value[1], "deck": deck_id})
        elif kind == "identify":
            msg = ("identify", {})
        elif kind == "decks":
            msg = ("reload", {})
        elif kind in ("settings", "servers", "profiles"):
            msg = (kind, {})
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


DECK_URL = re.compile(r"^/(?:api/)?decks?/([0-9a-z-]+)(/[a-z.-]+)?$")
SERVER_URL = re.compile(r"^/api/servers/([0-9a-zA-Z_-]+)$")
PROFILE_URL = re.compile(r"^/api/profiles/([^/]+)(/load)?$")


def make_server(manager, app=None, rigs=None):
    """app: optional hooks from the desktop app (update, quit, log, windows).
    rigs: optional rigs.Rigs, to list other RebelDecks."""
    hub = Hub(manager)
    pages = {"deck": _page("deck.html"), "gui": _page("gui.html"), "home": _page("home.html")}

    def ip_base(handler=None):
        """http://<IP address>:<port> for other devices (or the address this page came in on)."""
        if app and app.lan_url():
            return app.lan_url().rstrip("/")
        if handler is not None:
            host = handler.headers.get("Host")
            if host:
                return "http://" + host
        return f"http://127.0.0.1:{manager.cfg['web_port']}"

    def lan_base(handler=None):
        """The address links and QR codes use, from the link_address setting:
        <computer>.local (default), the bare computer name, or the IP address."""
        from .rigs import hostname
        how = manager.cfg.get("link_address", "local")
        port = manager.cfg["web_port"]
        if how == "local":
            return f"http://{hostname().lower()}.local:{port}"
        if how == "name":
            return f"http://{hostname().lower()}:{port}"
        return ip_base(handler)

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

        def send(self, code, body, ctype="application/json", cache=False):
            if not isinstance(body, (str, bytes)):
                body = json.dumps(body)
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
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

        def query(self):
            return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        def deck_page(self, ref):
            d = manager.deck_cfg(ref)
            if not d:
                return self.send(404, "<!doctype html><meta name=viewport content='width=device-width'>"
                                      "<body style='background:#06070b;color:#e8d6ff;font:16px monospace;padding:20px'>"
                                      "<h1>No such deck</h1><p>It may have been renamed (that changes its address). "
                                      "<a style='color:#b973ff' href='/'>Pick a deck</a></p>",
                                 "text/html; charset=utf-8")
            q = self.query()
            key = q.get("key", [""])[0]
            html = (pages["deck"].replace("{{DECK}}", d["id"]).replace("{{COLS}}", str(d["columns"]))
                    .replace("{{ROWS}}", str(d["rows"])).replace("{{KEY}}", key if key.isdigit() else "")
                    .replace("{{NAME}}", json.dumps(d["name"])[1:-1].replace("<", "&lt;")))
            self.send(200, html, "text/html; charset=utf-8")

        # ---- GET -------------------------------------------------------------
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/":
                return self.send(200, pages["home"], "text/html; charset=utf-8")
            if path == "/gui":
                if not self.allowed():
                    return self.ask_password()
                return self.send(200, pages["gui"].replace("{{VERSION}}", __version__), "text/html; charset=utf-8")
            m = DECK_URL.match(path)
            if m and path.startswith("/deck/") and not m.group(2):
                return self.deck_page(m.group(1))
            if re.fullmatch(r"/\d{1,3}", path):
                # short addresses: /1 is the first deck on the Decks page, /2 the second…
                n = int(path[1:])
                decks = manager.cfg["decks"]
                if not 1 <= n <= len(decks):
                    return self.send(404, f"<!doctype html><meta name=viewport content='width=device-width'>"
                                          f"<body style='background:#06070b;color:#e8d6ff;font:16px monospace;padding:20px'>"
                                          f"<h1>No deck {n}</h1><p>There are {len(decks)}. "
                                          f"<a style='color:#b973ff' href='/'>Pick a deck</a></p>",
                                     "text/html; charset=utf-8")
                self.send_response(302)
                self.send_header("Location", f"/deck/{decks[n - 1]['slug']}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/status":
                st = manager.status()
                st["version"] = __version__
                st["label"] = app.label() if app else f"v{__version__}"
                st["local"] = self.local()
                st["restart_needed"] = manager.restart_needed
                st["info"] = self.info()
                if app:
                    st["update"] = app.update_cached()
                    st["windows"] = app.windows_showing()
                return self.send(200, st)
            if path == "/api/events":
                return self.all_events()
            if path == "/api/info":
                return self.send(200, self.info())
            if path == "/api/rigs":
                here = {"name": self.info()["computer"], "url": lan_base(self), "ip_url": ip_base(self), "here": True,
                        "decks": [{"name": d["name"], "slug": d["slug"], "n": d["n"], "columns": d["columns"],
                                   "rows": d["rows"], "status": d["status"]} for d in manager.deck_list()]}
                others = rigs.others() if rigs and manager.cfg.get("find_other_rigs", True) else []
                return self.send(200, [here] + others)
            if path == "/api/qr.png":
                return self.send(200, qr.png(lan_base(self) + "/"), "image/png", cache=True)
            if path == "/api/companions/scan":
                if not self.allowed():
                    return self.ask_password()
                from .rigs import scan_companions
                known = {(s["host"], s["port"]) for s in manager.cfg["servers"]}
                found = scan_companions()
                for f in found:
                    f["added"] = (f["host"], f["port"]) in known or (
                        f["host"] == "127.0.0.1" and ("localhost", f["port"]) in known)
                return self.send(200, found)
            if path == "/api/presets":
                return self.send(200, [{"name": n, "columns": c, "rows": r, "key_types": lay.get("key_types", {})}
                                       for n, c, r, lay in settings.PRESETS])
            if path == "/api/decks":
                return self.send(200, manager.deck_list())
            if path == "/api/servers":
                return self.send(200, manager.server_list())
            if path == "/api/profiles":
                return self.send(200, manager.profile_list())
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
                d = manager.deck_cfg(m.group(1))
                deck = manager.deck(m.group(1))
                if m.group(2) == "/state":
                    return self.send(200, deck.snapshot() if deck else {"gone": True})
                if m.group(2) == "/events":
                    return self.events(d["id"] if d else m.group(1))
                if m.group(2) == "/qr.png":
                    if not d:
                        return self.send(404, {"error": "no such deck"})
                    return self.send(200, qr.png(f"{lan_base(self)}/deck/{d['slug']}"), "image/png")
            self.send(404, {"error": "not found"})

        def info(self):
            from .rigs import hostname
            port = manager.cfg["web_port"]
            on = manager.cfg.get("web_network", True)
            return {"computer": hostname(),
                    "link": lan_base(self) + "/" if on else None,          # what links and QR codes use
                    "lan": ip_base(self) + "/" if on else None,            # the IP address: the backup
                    "local_name": f"http://{hostname().lower()}.local:{port}/" if on else None,
                    "link_address": manager.cfg.get("link_address", "local"),
                    "here": f"http://127.0.0.1:{port}/",
                    "build": app.label() if app else __version__}     # pages reload themselves when this changes

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
                        # the deck was changed or removed: send the new state
                        deck = manager.deck(deck_id)
                        self.event("state", deck.snapshot() if deck else {"gone": True})
                        continue
                    self.event(kind, data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)
                self.close_connection = True

        def all_events(self):
            """Every deck's buttons, live, in one stream (the web GUI's previews)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe(None)

            def states():
                self.event("states", {"decks": [manager.deck(d["id"]).snapshot() for d in manager.cfg["decks"]
                                                if manager.deck(d["id"])]})
            try:
                states()
                while True:
                    try:
                        kind, data = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    if kind == "reload":
                        states()
                    elif kind in ("key", "status"):
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
        def deck_input(self, ref, what):
            """Presses, turns and fader moves: what decks are for, so no password.
            Keys a deck's settings make unclickable are ignored."""
            d = manager.deck_cfg(ref)
            deck = manager.deck(ref)
            b = self.body()
            if not d or not deck:
                return self.send(404, {"error": "no such deck"})
            if "key" not in b or (what == "/press" and "pressed" not in b) or \
                    (what == "/rotate" and "steps" not in b) or (what == "/fader" and "value" not in b):
                raise ValueError("missing key, pressed, steps or value")
            key = int(b["key"])
            if d["click_mode"] == "unclickable" or (d["click_mode"] == "hybrid" and key in d["blocked"]):
                return self.send(200, {"ok": False, "why": "not clickable"})
            if what == "/press":
                deck.press(key, bool(b["pressed"]))
            elif what == "/rotate":
                deck.rotate(key, int(b["steps"]))
            elif what == "/let-go":
                deck.let_go(key)
            else:
                deck.fader(key, int(b["value"]))
            return self.send(200, {"ok": True})

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            m = DECK_URL.match(path)
            try:
                if m and m.group(2) in ("/press", "/rotate", "/fader", "/let-go"):
                    return self.deck_input(m.group(1), m.group(2))
                if m and m.group(2) == "/tap":
                    # a hotkey (the Pi's keybinds run "RebelDeck --hotkey", which comes here):
                    # this computer only; hotkeys work whatever the deck's click mode
                    if not self.local():
                        return self.send(403, {"error": "hotkeys come from this computer only"})
                    manager.tap(m.group(1), int(self.body()["key"]))
                    return self.send(200, {"ok": True})
                if m and m.group(2) == "/identify":
                    manager.identify(m.group(1))
                    if app:
                        app.identify(manager.deck_cfg(m.group(1))["id"])
                    return self.send(200, {"ok": True})
                if not self.allowed():
                    return self.ask_password()
                if path == "/api/decks":
                    b = self.body()
                    return self.send(200, manager.add_deck(b.get("name"), b.get("columns"), b.get("rows")))
                if path == "/api/decks/sync":
                    # deck layouts from another rig, via Rebel Sync on this computer
                    if not self.local():
                        return self.send(403, {"error": "only from this computer"})
                    decks = self.body().get("decks")
                    if not isinstance(decks, list) or not all(isinstance(d, dict) for d in decks):
                        raise ValueError("decks: a list")
                    manager.apply_synced_decks(decks)
                    return self.send(200, {"ok": True, "decks": len(decks)})
                if path == "/api/servers":
                    b = self.body()
                    if not str(b.get("host", "")).strip():
                        raise ValueError("a Companion server needs an address")
                    return self.send(200, manager.add_server(b.get("name"), b.get("host"), b.get("port", 16622),
                                                             bool(b.get("default"))))
                if path == "/api/profiles":
                    return self.send(200, manager.save_profile(self.body().get("name")))
                pm = PROFILE_URL.match(path)
                if pm and pm.group(2):
                    return self.send(200, manager.load_profile(urllib.parse.unquote(pm.group(1))))
                if path == "/api/update":
                    if not app:
                        return self.send(400, {"error": "updates come from the app"})
                    threading.Thread(target=app.update_now, daemon=True).start()
                    return self.send(200, {"ok": True})
                if path in ("/api/quit", "/api/popout"):
                    if not (app and self.local()):
                        return self.send(403, {"error": "only from this computer"})
                    if path == "/api/quit":
                        threading.Timer(0.3, app.quit).start()
                    else:
                        app.popup_show()
                    return self.send(200, {"ok": True})
            except KeyError as e:
                return self.send(404, {"error": f"not found: {e}"})
            except (ValueError, TypeError) as e:
                return self.send(400, {"error": str(e)})
            self.send(404, {"error": "not found"})

        def do_PUT(self):
            if not self.allowed():
                return self.ask_password()
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            m = DECK_URL.match(path)
            sm = SERVER_URL.match(path)
            try:
                if path == "/api/settings":
                    return self.send(200, manager.set_app_settings(self.body()))
                if m and path.startswith("/api/decks/") and not m.group(2):
                    return self.send(200, manager.update_deck(m.group(1), self.body()))
                if sm:
                    return self.send(200, manager.update_server(sm.group(1), self.body()))
            except KeyError:
                return self.send(404, {"error": "not found"})
            except NameInUse as e:
                return self.send(409, {"error": str(e)})
            except (ValueError, TypeError) as e:
                return self.send(400, {"error": str(e)})
            self.send(404, {"error": "not found"})

        def do_DELETE(self):
            if not self.allowed():
                return self.ask_password()
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            m = DECK_URL.match(path)
            sm = SERVER_URL.match(path)
            pm = PROFILE_URL.match(path)
            try:
                if m and path.startswith("/api/decks/") and not m.group(2):
                    manager.remove_deck(m.group(1))
                    return self.send(200, {"ok": True})
                if sm:
                    manager.remove_server(sm.group(1))
                    return self.send(200, {"ok": True})
                if pm and not pm.group(2):
                    manager.remove_profile(urllib.parse.unquote(pm.group(1)))
                    return self.send(200, {"ok": True})
            except KeyError:
                return self.send(404, {"error": "not found"})
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            self.send(404, {"error": "not found"})

    host = "0.0.0.0" if manager.cfg.get("web_network", True) else "127.0.0.1"
    server = ThreadingHTTPServer((host, int(manager.cfg["web_port"])), Handler)
    server.daemon_threads = True
    return server
