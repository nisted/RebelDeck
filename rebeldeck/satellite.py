"""A Companion Satellite API client for one deck.

Each deck has its own connection, so it shows up in Companion as its own
surface and can be added, resized or removed without disturbing the others.

Connects over TCP, registers the deck, keeps button states and passes taps
back. Reconnects on its own if Companion goes away. Runs in its own thread;
listeners are called with ("key", n) when a button changes and ("status", s)
when the connection does.
"""
import base64
import io
import logging
import re
import socket
import threading
import time

log = logging.getLogger("rebeldeck.satellite")

_ARG = re.compile(r'(\w+)=("([^"]*)"|\S*)')


def parse(line):
    """'CMD A=1 B="x y"' -> ('CMD', {'A': '1', 'B': 'x y'})"""
    cmd, _, rest = line.partition(" ")
    args = {}
    for m in _ARG.finditer(rest):
        args[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return cmd, args


def quote(v):
    v = str(v)
    return f'"{v}"' if (" " in v or not v) else v


class Key:
    __slots__ = ("image", "color", "text", "text_color", "pressed")

    def __init__(self):
        self.image = None        # data: URL (PNG) or None
        self.color = "#000000"
        self.text = ""
        self.text_color = "#ffffff"
        self.pressed = False


class Deck:
    def __init__(self, cfg, deck):
        self.cfg = cfg                      # app settings: Companion's address, picture size
        self.deck = deck                    # this deck: id, name, columns, rows, device_id
        self.id = deck["id"]
        self.device_id = deck["device_id"]
        self.cols, self.rows = int(deck["columns"]), int(deck["rows"])
        self.keys = [Key() for _ in range(self.cols * self.rows)]
        self.status = "connecting"
        self.companion_version = ""
        self.brightness = 100
        self.listeners = []
        self._sock = None
        self._lock = threading.Lock()
        self._png = False
        self._stop = threading.Event()

    # ---- public -------------------------------------------------------
    def start(self):
        threading.Thread(target=self._run, name="satellite", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._send("QUIT")
        self._close()

    def press(self, key, pressed):
        if 0 <= key < len(self.keys):
            self._send(f"KEY-PRESS DEVICEID={self.device_id} KEY={key} PRESSED={'true' if pressed else 'false'}")

    def snapshot(self):
        return {
            "id": self.id, "name": self.deck["name"],
            "cols": self.cols, "rows": self.rows, "status": self.status,
            "companion": self.companion_version, "brightness": self.brightness,
            "keys": [self.key_json(i) for i in range(len(self.keys))],
        }

    def key_json(self, i):
        k = self.keys[i]
        return {"i": i, "image": k.image, "color": k.color, "text": k.text,
                "textColor": k.text_color, "pressed": k.pressed}

    # ---- connection ---------------------------------------------------
    def _set_status(self, s):
        if s != self.status:
            self.status = s
            log.info("companion: %s", s)
            self._emit("status", s)

    def _emit(self, kind, value):
        for fn in list(self.listeners):
            try:
                fn(kind, value)
            except Exception:
                log.exception("listener failed")

    def _send(self, line):
        with self._lock:
            s = self._sock
            if not s:
                return
            try:
                s.sendall((line + "\n").encode())
            except OSError:
                pass

    def _close(self):
        with self._lock:
            s, self._sock = self._sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass

    def _run(self):
        delay = 1
        while not self._stop.is_set():
            try:
                self._session()
                delay = 1
            except OSError as e:
                log.info("companion: %s", e)
            self._close()
            if self._stop.is_set():
                break
            self._set_status("disconnected")
            self.companion_version = ""
            self._stop.wait(delay)
            delay = min(delay * 2, 10)               # 1, 2, 4, 8, 10, 10 … seconds

    def _session(self):
        host, port = self.cfg["companion_host"], int(self.cfg["companion_port"])
        self._set_status("connecting")
        s = socket.create_connection((host, port), timeout=5)
        s.settimeout(1)
        with self._lock:
            self._sock = s
        buf = b""
        last_ping = last_heard = time.monotonic()
        added = False
        begun = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_ping > 2:
                self._send("PING rebeldeck")
                last_ping = now
            if now - last_heard > 10:
                raise OSError("no reply from Companion for 10 s")
            # CAPS arrives straight after BEGIN on newer Companions; older ones
            # never send it, so register anyway after a short wait
            if not added and self.companion_version and now - begun > 0.5:
                self._add_device()
                added = True
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                raise OSError("Companion closed the connection")
            last_heard = time.monotonic()
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode(errors="replace").strip()
                if not line:
                    continue
                if self._handle(line) == "caps" and not added:
                    self._add_device()
                    added = True

    def _add_device(self):
        c = self.cfg
        parts = [
            "ADD-DEVICE", f"DEVICEID={self.device_id}", f"PRODUCT_NAME={quote(self.deck['name'])}",
            f"SERIAL={quote('rebeldeck:' + self.id)}",
            f"KEYS_TOTAL={len(self.keys)}", f"KEYS_PER_ROW={self.cols}",
            f"BITMAPS={int(c['button_px'])}", "COLORS=hex", "TEXT=true", "TEXT_STYLE=true",
            "BRIGHTNESS=true",
        ]
        if self._png:
            parts.append("BITMAP_FORMAT=png")
        self._send(" ".join(parts))

    def _handle(self, line):
        cmd, a = parse(line)
        if cmd == "BEGIN":
            self.companion_version = a.get("CompanionVersion", "")
        elif cmd == "CAPS":
            self._png = "png" in a.get("BITMAP_FORMATS", "").split(",")
            return "caps"
        elif cmd == "PING":
            self._send("PONG " + line[5:])
        elif cmd == "ADD-DEVICE":
            if line.startswith("ADD-DEVICE OK"):
                self._set_status("connected")
            else:
                log.error("Companion refused the deck: %s", a.get("MESSAGE", line))
                self._set_status("refused")
        elif cmd == "KEY-STATE" and a.get("DEVICEID") == self.device_id:
            self._key_state(a)
        elif cmd == "KEYS-CLEAR" and a.get("DEVICEID") == self.device_id:
            for i, k in enumerate(self.keys):
                k.__init__()
                self._emit("key", i)
        elif cmd == "BRIGHTNESS" and a.get("DEVICEID") == self.device_id:
            try:
                self.brightness = int(a.get("VALUE", 100))
            except ValueError:
                pass
            self._emit("brightness", self.brightness)
        elif cmd == "ERROR":
            log.warning("Companion: %s", a.get("MESSAGE", line))
        return None

    def _key_state(self, a):
        try:
            i = int(a.get("KEY", -1))
        except ValueError:
            return
        if not 0 <= i < len(self.keys):
            return
        k = self.keys[i]
        if "BITMAP" in a:
            k.image = self._image(a["BITMAP"])
        if "COLOR" in a:
            k.color = a["COLOR"]
        if "TEXTCOLOR" in a:
            k.text_color = a["TEXTCOLOR"]
        if "TEXT" in a:
            try:
                k.text = base64.b64decode(a["TEXT"]).decode(errors="replace")
            except ValueError:
                k.text = ""
        if "PRESSED" in a:
            k.pressed = a["PRESSED"] in ("1", "true")
        self._emit("key", i)

    def _image(self, value):
        if not value:
            return None
        if value.startswith("data:"):
            return value
        # raw 8-bit RGB: turn it into a PNG the browser can show
        try:
            raw = base64.b64decode(value)
            side = int((len(raw) // 3) ** 0.5)
            if side * side * 3 != len(raw):
                return None
            from PIL import Image
            im = Image.frombytes("RGB", (side, side), raw)
            out = io.BytesIO()
            im.save(out, "PNG")
            return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
        except Exception:
            log.exception("couldn't read a button picture")
            return None
