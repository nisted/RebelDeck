"""A stand-in for Companion's Satellite API, for tests."""
import base64
import socket
import threading


class FakeCompanion:
    def __init__(self, png=True, caps=True, rotary_amount=False, nonsquare=True):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        self.png, self.caps, self.rotary_amount, self.nonsquare = png, caps, rotary_amount, nonsquare
        self.lines = []
        self.conns = []          # every connection
        self.by_dev = {}         # device id -> its connection
        self.added = threading.Event()
        self.pressed = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def send(self, line, dev=None):
        c = self.by_dev.get(dev) or self.conns[-1]
        c.sendall((line + "\n").encode())

    def _serve(self, c):
        c.sendall(b"BEGIN CompanionVersion=5.1.2 ApiVersion=1.16.0\n")
        if self.caps:
            fm = "rgb,png,webp" if self.png else "rgb"
            extra = (" ROTARY_AMOUNT=1" if self.rotary_amount else "") + (" NONSQUARE=1" if self.nonsquare else "")
            c.sendall(f"CAPS SUBSCRIPTIONS=1 BITMAP_FORMATS={fm}{extra}\n".encode())
        buf = b""
        while True:
            try:
                d = c.recv(65536)
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.decode()
                self.lines.append(line)
                try:
                    self._reply(c, line)
                except OSError:
                    return

    def _reply(self, c, line):
        if line.startswith("ADD-DEVICE"):
            dev = line.split("DEVICEID=")[1].split()[0]
            self.by_dev[dev] = c
            c.sendall(f"ADD-DEVICE OK DEVICEID={dev}\n".encode())
            self.added.set()
        elif line.startswith("PING"):
            c.sendall(("PONG" + line[4:] + "\n").encode())
        elif line.startswith(("KEY-PRESS", "KEY-ROTATE", "SET-VARIABLE-VALUE")):
            self.pressed.append(line)

    def key_rgb(self, dev, key, side, rgb=(255, 0, 0), text="GO"):
        raw = bytes(rgb) * (side * side)
        self.send(f"KEY-STATE DEVICEID={dev} KEY={key} TYPE=BUTTON BITMAP={base64.b64encode(raw).decode()} "
                  f"COLOR=#ff0000 TEXT={base64.b64encode(text.encode()).decode()} PRESSED=0", dev)

    def drop(self):
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)   # a real hang-up, as when Companion quits
            except OSError:
                pass
            c.close()
        self.conns, self.by_dev = [], {}

    def close(self):
        try:
            self.srv.shutdown(socket.SHUT_RDWR)        # stop listening for real
        except OSError:
            pass
        self.srv.close()
        self.drop()

    def variable(self, dev, name, value):
        b64 = base64.b64encode(str(value).encode()).decode()
        self.send(f'VARIABLE-VALUE DEVICEID={dev} VARIABLE="{name}" VALUE="{b64}"', dev)

    def key_ctl(self, dev, ctl, w, h, location=None, rgb=(0, 0, 255)):
        """KEY-STATE in the free layout (CONTROLID), raw RGB w x h."""
        raw = bytes(rgb) * (w * h)
        loc = f" LOCATION={location}" if location else ""
        self.send(f'KEY-STATE DEVICEID={dev} CONTROLID="{ctl}" TYPE=BUTTON BITMAP={base64.b64encode(raw).decode()}'
                  f" COLOR=#0000ff{loc} PRESSED=0", dev)
