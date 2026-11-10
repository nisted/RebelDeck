"""A Companion Satellite API client for one deck.

Each deck has its own connection, so it shows up in Companion as its own
surface and can be added, resized or removed without disturbing the others.

Connects over TCP, registers the deck, keeps button states and passes taps,
rotary turns and fader moves back. Reconnects on its own if Companion goes
away. Runs in its own thread; listeners are called with ("key", n) when a
button changes, ("status", s) when the connection does, ("fader", (n, value))
when Companion moves a fader and ("brightness", v).

Rotaries send KEY-ROTATE. Faders work like the Intech Grid's: each one sends
KEY-ROTATE steps as it moves, and its position (0-127) as a surface variable
("fader_<n>"); Companion can move it with the output variable "fader_<n>_set".
"""
import json
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
    __slots__ = ("image", "color", "text", "text_color", "pressed", "location")

    def __init__(self):
        self.image = None        # data: URL (PNG) or None
        self.color = "#000000"
        self.text = ""
        self.text_color = "#ffffff"
        self.pressed = False
        self.location = None     # Companion's page/row/column, when it says


FADER_MAX = 127


class Deck:
    def __init__(self, server, deck, picture_px=144):
        self.server = server                # {"host", "port", ...}: the Companion this deck uses
        self.picture_px = int(picture_px)
        self.deck = deck                    # this deck: id, name, columns, rows, device_id, key types…
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
        self._rotary_amount = False
        self._api = (0, 0, 0)
        self.faders = {int(k): 0 for k, t in deck.get("key_types", {}).items() if t == "fader"}
        self.gaps = {int(k) for k, t in deck.get("key_types", {}).items() if t == "gap"}
        self.strips = {int(k) for k, t in deck.get("key_types", {}).items() if t == "strip"}
        self.latched = set()                # sticky keys held down in Companion right now
        self._logged = None                 # last state written to the log (it doesn't repeat itself)
        self._last_err = None
        self.ratio = float(deck.get("strip_ratio", 2) or 2)
        self._advanced = False              # Companion's free layout (strips, gaps), from API 1.9
        self._nonsquare = False             # wide pictures, from API 1.11 (Companion 5)
        self._fader_sent = {}
        self._stop = threading.Event()

    # ---- public -------------------------------------------------------
    def start(self):
        threading.Thread(target=self._run, name="satellite", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._send("QUIT")
        self._close()

    def _where(self, key):
        """How Companion knows a key: KEY=n, or CONTROLID="row/column" in the free layout."""
        if self._advanced:
            return f'CONTROLID="{key // self.cols}/{key % self.cols}"'
        return f"KEY={key}"

    def sticky(self, key):
        return self.deck.get("key_types", {}).get(str(key)) == "sticky"

    def press(self, key, pressed):
        if not (0 <= key < len(self.keys)) or key in self.gaps:
            return
        if self.sticky(key):
            # sticky: one press holds it down in Companion, the next lets go; releases do nothing
            if not pressed:
                return
            pressed = key not in self.latched
            (self.latched.add if pressed else self.latched.discard)(key)
            self._emit("key", key)
        self._send(f"KEY-PRESS DEVICEID={self.device_id} {self._where(key)} PRESSED={'true' if pressed else 'false'}")

    def let_go(self, key):
        """A sticky key: let go, if it's held."""
        if key in self.latched:
            self.press(key, True)

    def rotate(self, key, steps):
        """A rotary turned: steps > 0 clockwise."""
        if not (0 <= key < len(self.keys)) or not steps or key in self.gaps:
            return
        steps = int(steps)
        where = self._where(key)
        if self._rotary_amount:
            self._send(f"KEY-ROTATE DEVICEID={self.device_id} {where} DIRECTION={steps}")
        else:
            one = 1 if steps > 0 else -1
            for _ in range(min(abs(steps), 32)):
                self._send(f"KEY-ROTATE DEVICEID={self.device_id} {where} DIRECTION={one}")

    def fader(self, key, value):
        """A fader moved on screen to value (0-127)."""
        if key not in self.faders:
            return
        value = max(0, min(FADER_MAX, int(value)))
        old = self.faders[key]
        self.faders[key] = value
        if value != old:
            self.rotate(key, value - old)
        if self._fader_sent.get(key) != value:
            self._fader_sent[key] = value
            b64 = base64.b64encode(str(value).encode()).decode()
            self._send(f"SET-VARIABLE-VALUE DEVICEID={self.device_id} VARIABLE=fader_{key} VALUE={b64}")
        self._emit("fader", (key, value))

    def snapshot(self):
        d = self.deck
        return {
            "id": self.id, "slug": d.get("slug", self.id), "name": d["name"],
            "cols": self.cols, "rows": self.rows, "status": self.status,
            "companion": self.companion_version, "brightness": self.brightness,
            "keys": [self.key_json(i) for i in range(len(self.keys))],
            "style": self.style(),
        }

    def style(self):
        """How the deck page should look and behave (changes without reconnecting)."""
        d = self.deck
        return {"name": d["name"], "slug": d.get("slug"), "showing": d.get("showing", True),
                "click_mode": d.get("click_mode", "clickable"),
                "blocked": d.get("blocked", []), "key_types": d.get("key_types", {}),
                "breakout": d.get("breakout", []), "float": d.get("float", True),
                "movable": d.get("movable", True), "transparency": d.get("transparency", 0),
                "strip_ratio": d.get("strip_ratio", 2), "rotary": d.get("rotary", {}),
                "hotkeys": d.get("hotkeys", {}),
                "colour": d.get("colour", "#000000"), "faders": {str(k): v for k, v in self.faders.items()}}

    def key_json(self, i):
        k = self.keys[i]
        return {"i": i, "image": k.image, "color": k.color, "text": k.text,
                "textColor": k.text_color, "pressed": k.pressed, "location": k.location,
                "latched": i in self.latched}

    def locations(self):
        return [k.location for k in self.keys]

    # ---- connection ---------------------------------------------------
    def _set_status(self, s):
        if s != self.status:
            self.status = s
            # while Companion's away, each retry goes connecting -> disconnected: log that once
            if s != self._logged and not (s == "connecting" and self._logged == "disconnected"):
                log.info("%s: companion %s", self.deck.get("name", self.id), s)
                self._logged = s
            if s == "connected":
                self._last_err = None
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
                if str(e) != self._last_err:            # the same error again and again: once is enough
                    log.info("%s: companion at %s:%s: %s", self.deck.get("name", self.id),
                             self.server["host"], self.server["port"], e)
                    self._last_err = str(e)
            self._close()
            if self.latched:                            # Companion's forgotten them
                held, self.latched = self.latched, set()
                for k in held:
                    self._emit("key", k)
            if self._stop.is_set():
                break
            self._set_status("disconnected")
            self.companion_version = ""
            self._stop.wait(delay)
            delay = min(delay * 2, 10)               # 1, 2, 4, 8, 10, 10 … seconds

    def _session(self):
        host, port = self.server["host"], int(self.server["port"])
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

    def strip_px(self):
        """(width, height) of a strip button's picture."""
        if not self._nonsquare:
            return self.picture_px, self.picture_px
        return self.picture_px, max(16, round(self.picture_px / self.ratio))

    def _add_device(self):
        parts = [
            "ADD-DEVICE", f"DEVICEID={self.device_id}", f"PRODUCT_NAME={quote(self.deck['name'])}",
            f"SERIAL={quote('rebeldeck:' + self.id)}",
        ]
        # strips and gaps need Companion's free layout (API 1.9+): every key a
        # control at its row/column, gaps left out, strip buttons with wide pictures
        self._advanced = bool(self.strips or self.gaps) and self._api >= (1, 9, 0)
        if self._advanced:
            px = self.picture_px
            sw, sh = self.strip_px()
            base = {"colors": "hex", "text": True, "textStyle": True}
            manifest = {"stylePresets": {"default": dict(base, bitmap={"w": px, "h": px}),
                                         "strip": dict(base, bitmap={"w": sw, "h": sh})},
                        "controls": {}}
            for k in range(len(self.keys)):
                if k in self.gaps:
                    continue
                r, c = divmod(k, self.cols)
                ctl = {"row": r, "column": c}
                if k in self.strips:
                    ctl["stylePreset"] = "strip"
                manifest["controls"][f"{r}/{c}"] = ctl
            parts.append("LAYOUT_MANIFEST=" + base64.b64encode(json.dumps(manifest).encode()).decode())
        else:
            parts += [f"KEYS_TOTAL={len(self.keys)}", f"KEYS_PER_ROW={self.cols}",
                      f"BITMAPS={self.picture_px}", "COLORS=hex", "TEXT=true", "TEXT_STYLE=true"]
        parts.append("BRIGHTNESS=true")
        if self._png:
            parts.append("BITMAP_FORMAT=png")
        if self.faders and self._api >= (1, 7, 0):
            rc = lambda k: f"{k // self.cols + 1}/{k % self.cols + 1}"
            vs = []
            for k in sorted(self.faders):
                vs.append({"id": f"fader_{k}", "type": "input", "name": f"Fader {rc(k)} position",
                           "description": f"0-{FADER_MAX}, as the fader on key {k} (row/column {rc(k)}) is moved"})
                vs.append({"id": f"fader_{k}_set", "type": "output", "name": f"Fader {rc(k)} move to",
                           "description": f"Set this (0-{FADER_MAX}) to move the on-screen fader on key {k}"})
            parts.append("VARIABLES=" + base64.b64encode(json.dumps(vs).encode()).decode())
        self._send(" ".join(parts))

    def _handle(self, line):
        cmd, a = parse(line)
        if cmd == "BEGIN":
            self.companion_version = a.get("CompanionVersion", "")
            try:
                self._api = tuple(int(x) for x in a.get("ApiVersion", "0.0.0").split(".")[:3])
            except ValueError:
                self._api = (0, 0, 0)
        elif cmd == "CAPS":
            self._png = "png" in a.get("BITMAP_FORMATS", "").split(",")
            self._rotary_amount = "ROTARY_AMOUNT" in a
            self._nonsquare = "NONSQUARE" in a
            return "caps"
        elif cmd == "VARIABLE-VALUE" and a.get("DEVICEID") == self.device_id:
            name = a.get("VARIABLE", "")
            if name.startswith("fader_") and name.endswith("_set"):
                try:
                    k = int(name[6:-4])
                    v = base64.b64decode(a.get("VALUE", "")).decode().strip()
                    v = max(0, min(FADER_MAX, int(float(v))))
                except ValueError:
                    return None
                if k in self.faders:
                    self.faders[k] = v
                    self._fader_sent[k] = v
                    self._emit("fader", (k, v))
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
            if "CONTROLID" in a:
                r, c = (int(x) for x in a["CONTROLID"].split("/"))
                i = r * self.cols + c if 0 <= c < self.cols else -1
            else:
                i = int(a.get("KEY", -1))
        except ValueError:
            return
        if not 0 <= i < len(self.keys):
            return
        k = self.keys[i]
        if a.get("LOCATION"):
            k.location = a["LOCATION"]
        if "BITMAP" in a:
            strip = self._advanced and i in self.strips
            k.image = self._image(a["BITMAP"], self.strip_px() if strip else None)
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

    def _image(self, value, size=None):
        if not value:
            return None
        if value.startswith("data:"):
            return value
        # raw 8-bit RGB: turn it into a PNG the browser can show
        try:
            raw = base64.b64decode(value)
            if size and size[0] * size[1] * 3 == len(raw):
                w, h = size
            else:
                w = h = int((len(raw) // 3) ** 0.5)
                if w * h * 3 != len(raw):
                    return None
            from PIL import Image
            im = Image.frombytes("RGB", (w, h), raw)
            out = io.BytesIO()
            im.save(out, "PNG")
            return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
        except Exception:
            log.exception("couldn't read a button picture")
            return None
