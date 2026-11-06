"""The deck page and its small API, served to this computer and (optionally)
to phones and tablets on the network.

  GET  /              the deck page
  GET  /api/state     every button, the deck size and the connection
  GET  /api/events    live updates (server-sent events)
  POST /api/press     {"key": n, "pressed": true|false}
"""
import json
import logging
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("rebeldeck.web")
HERE = os.path.dirname(os.path.abspath(__file__))


class Hub:
    """Fans deck changes out to every open page."""

    def __init__(self, deck):
        self.deck = deck
        self.clients = set()
        self.lock = threading.Lock()
        deck.listeners.append(self._on_change)

    def _on_change(self, kind, value):
        if kind == "key":
            msg = ("key", self.deck.key_json(value))
        elif kind == "status":
            msg = ("status", {"status": value, "companion": self.deck.companion_version})
        elif kind == "brightness":
            msg = ("brightness", {"value": value})
        else:
            return
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass                      # a stalled page; it resyncs on reconnect

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)


def make_server(cfg, deck):
    hub = Hub(deck)
    page = open(os.path.join(HERE, "deck.html"), encoding="utf-8").read()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/deck"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(deck.snapshot()))
            elif path == "/api/events":
                self._events()
            else:
                self._send(404, '{"error":"not found"}')

        def do_POST(self):
            if self.path != "/api/press":
                return self._send(404, '{"error":"not found"}')
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                deck.press(int(body["key"]), bool(body["pressed"]))
            except (ValueError, KeyError, TypeError):
                return self._send(400, '{"error":"bad request"}')
            self._send(200, '{"ok":true}')

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe()
            try:
                self._event("state", deck.snapshot())
                while True:
                    try:
                        kind, data = q.get(timeout=15)
                        self._event(kind, data)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)
                self.close_connection = True

        def _event(self, kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

    host = "0.0.0.0" if cfg.get("web_network", True) else "127.0.0.1"
    server = ThreadingHTTPServer((host, int(cfg["web_port"])), Handler)
    server.daemon_threads = True
    return server
