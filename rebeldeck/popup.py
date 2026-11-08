"""The pop-out: the quick stuff, built like Companion Sync's pop-up.

A small window of its own (tkinter) with no title bar: drag it from anywhere
that isn't a control; Hide or Esc closes it. It talks to RebelDeck through
its local API, so it can't hold the app up, and quits if RebelDeck goes.

  - Companion status (every deck's connection) and update notice
  - a switch per deck to open or close its window, a QR code per deck
  - the address and QR code for phones, tablets and other browsers
  - Open web GUI (the pop-out steps aside for it)
  - Start at login switch
"""
import base64
import json
import os
import sys
import urllib.request
import webbrowser

from . import settings

BG, TOP, TEXT, DIM, LINE = "#17141f", "#221a33", "#e9e2ff", "#9d8ccc", "#2c2440"
PURPLE, PURPLE2, BTN = "#5500AA", "#7b3ae0", "#2a1c48"
STATE = {   # border, fill, sub-text
    "connected": ("#2ea043", "#1f3a26", "#a9d9b4"),
    "partly connected": ("#e6961a", "#3d2d14", "#f0cf99"),
    "connecting": ("#e6961a", "#3d2d14", "#f0cf99"),
    "no decks": ("#8c8c8c", "#26242b", "#c4c4c4"),
    "problem": ("#d23232", "#3d1a1a", "#f0a6a6"),
}
FONT = "Segoe UI" if sys.platform == "win32" else ("Helvetica Neue" if sys.platform == "darwin" else "DejaVu Sans")
POS_FILE = os.path.join(settings.DIR, "popup-position.json")


def _scale(cfg):
    """Same sizing as Companion Sync's: a bit bigger than the design's pixels
    (Tk's fonts come out smallest on the Mac), times popup_size %."""
    base = 1.45 if sys.platform == "darwin" else 1.2
    try:
        pct = float(cfg.get("popup_size", 100))
    except (TypeError, ValueError):
        pct = 100
    return base * min(max(pct, 50), 300) / 100


def load_pos():
    try:
        with open(POS_FILE) as f:
            d = json.load(f)
        return int(d["x"]), int(d["y"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_pos(x, y):
    try:
        os.makedirs(os.path.dirname(POS_FILE), exist_ok=True)
        with open(POS_FILE, "w") as f:
            json.dump({"x": x, "y": y}, f)
    except OSError:
        pass


class Popup:
    def __init__(self, tk, cfg):
        self.tk = tk
        self.base = f"http://127.0.0.1:{cfg['web_port']}"
        S = _scale(cfg)
        self.z = z = lambda n: max(1, round(n * S))
        self.W = z(380)
        self.misses = 0
        self.drag = None
        self.moved = False
        self.deck_sig = None
        self.qr_for = None
        self.qr_img = None
        r = self.root = tk.Tk()
        r.title("RebelDeck")
        r.configure(bg=BG, highlightthickness=1, highlightbackground=PURPLE2, highlightcolor=PURPLE2)
        r.resizable(False, False)
        self.borderless()
        r.bind_all("<Escape>", lambda e: self.close())
        r.bind_all("<ButtonPress-1>", self.drag_start, add="+")
        r.bind_all("<B1-Motion>", self.drag_move, add="+")
        r.bind_all("<ButtonRelease-1>", self.drag_end, add="+")
        self.mac_background()

        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=z(20), pady=(z(18), z(10)))
        self.icon_img = self.make_icon(z(54))
        if self.icon_img:
            tk.Label(head, image=self.icon_img, bg=BG).pack(side="left", padx=(0, z(14)))
        t = tk.Frame(head, bg=BG)
        t.pack(side="left")
        tk.Label(t, text="RebelDeck", bg=BG, fg=TEXT, font=(FONT, z(16), "bold")).pack(anchor="w")
        self.sub = tk.Label(t, text="", bg=BG, fg=DIM, font=(FONT, z(10)))
        self.sub.pack(anchor="w")

        self.note = tk.Label(r, text="", bg=PURPLE, fg="white", font=(FONT, z(10)), cursor="hand2",
                             padx=z(10), pady=z(6), wraplength=self.W - z(70), justify="left")
        self.note_shown = False
        self.note.bind("<Button-1>", lambda e: self.open_gui("update"))

        self.box = tk.Frame(r, bg=BG, highlightthickness=1)
        self.box.pack(fill="x", padx=z(20), pady=(z(4), z(12)))
        inner = tk.Frame(self.box, bg=BG)
        inner.pack(fill="x", padx=z(12), pady=z(10))
        self.dot = tk.Canvas(inner, width=z(14), height=z(14), highlightthickness=0, bg=BG)
        self.dot.pack(side="left", padx=(0, z(10)))
        tt = tk.Frame(inner, bg=BG)
        tt.pack(side="left", fill="x")
        self.line = tk.Label(tt, text="Looking for RebelDeck…", bg=BG, fg=TEXT, font=(FONT, z(12), "bold"), anchor="w")
        self.line.pack(anchor="w")
        self.detail = tk.Label(tt, text="", bg=BG, fg=DIM, font=(FONT, z(9)), anchor="w", justify="left",
                               wraplength=self.W - z(100))
        self.detail.pack(anchor="w")
        self.box_parts = [self.box, inner, self.dot, tt, self.line, self.detail]

        # decks: a switch each (window open), and a QR
        dh = tk.Frame(r, bg=BG)
        dh.pack(fill="x", padx=z(20))
        tk.Label(dh, text="DECKS", bg=BG, fg=DIM, font=(FONT, z(9), "bold"), anchor="w").pack(side="left")
        tk.Label(dh, text="QR    VISIBLE", bg=BG, fg=DIM, font=(FONT, z(8), "bold"), anchor="e").pack(side="right")
        self.decks = tk.Frame(r, bg=BG)
        self.decks.pack(fill="x", padx=z(20), pady=(z(2), z(8)))

        # browsers
        rows = tk.Frame(r, bg=BG)
        rows.pack(fill="x", padx=z(20))
        tk.Frame(rows, bg=LINE, height=1).pack(fill="x")
        f = tk.Frame(rows, bg=BG)
        f.pack(fill="x", pady=z(6))
        tk.Label(f, text="Browsers", bg=BG, fg=DIM, font=(FONT, z(10))).pack(side="left")
        self.thumbs = {}
        qr = self.thumb(f, None)
        qr.pack(side="right", padx=(z(6), 0))
        self.lan = tk.Label(f, text="—", bg=BG, fg=TEXT, font=(FONT, z(10)))
        self.lan.pack(side="right")
        tk.Frame(rows, bg=LINE, height=1).pack(fill="x")
        self.qr_box = tk.Frame(r, bg=BG)
        self.qr_label = tk.Label(self.qr_box, bg="white")
        self.qr_label.pack(pady=(z(10), z(2)))
        self.qr_caption = tk.Label(self.qr_box, text="", bg=BG, fg=DIM, font=(FONT, z(9)))
        self.qr_caption.pack()
        self.qr_shown = False
        self.after_rows = rows

        grid = tk.Frame(r, bg=BG)
        grid.pack(fill="x", padx=z(20), pady=(z(16), z(10)))
        self.big(grid, "Open web GUI ↗", lambda: self.open_gui(), primary=True).pack(fill="x")

        foot = tk.Frame(r, bg=BG)
        foot.pack(fill="x", padx=z(20), pady=(z(4), z(16)))
        self.login = Switch(tk, foot, z, lambda on: self.put("/api/settings", {"start_at_login": on}))
        self.login.pack(side="left")
        lab = tk.Label(foot, text="Start at login", bg=BG, fg=DIM, font=(FONT, z(10)), cursor="hand2")
        lab.pack(side="left", padx=z(8))
        lab.bind("<Button-1>", lambda e: self.login.click())
        hide = self.small(foot, "Hide", self.close, pad=14)
        hide.pack(side="right")
        r.minsize(self.W, 1)

    # ---- bits -------------------------------------------------------------------
    def make_icon(self, size):
        try:
            from PIL import ImageTk
            from .art import draw
            return ImageTk.PhotoImage(draw(size))
        except Exception:
            return None

    def small(self, parent, text, cmd, pad=8):
        z = self.z
        b = self.tk.Label(parent, text=text, bg=BTN, fg=TEXT, font=(FONT, z(10)), padx=z(pad), pady=z(3),
                          highlightthickness=1, highlightbackground=PURPLE2, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=PURPLE2))
        b.bind("<Leave>", lambda e: b.configure(bg=BTN))
        return b

    def big(self, parent, text, cmd, primary=False):
        z = self.z
        bg = PURPLE if primary else BTN
        b = self.tk.Label(parent, text=text, bg=bg, fg=TEXT, font=(FONT, z(11), "bold" if primary else "normal"),
                          pady=z(9), highlightthickness=1, highlightbackground=PURPLE2, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=PURPLE2))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    def borderless(self):
        r = self.root
        try:
            if sys.platform == "darwin":
                r.tk.call("::tk::unsupported::MacWindowStyle", "style", r._w, "plain", "none")
            else:
                r.overrideredirect(True)
        except self.tk.TclError:
            pass

    def mac_background(self):
        if sys.platform != "darwin":
            return
        try:
            import ctypes

            class PSN(ctypes.Structure):
                _fields_ = [("hi", ctypes.c_uint32), ("lo", ctypes.c_uint32)]
            lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            lib.TransformProcessType(ctypes.byref(PSN(0, 2)), 4)
        except Exception:
            pass

    def place(self, first=False):
        r = self.root
        r.update_idletasks()
        w, h = max(self.W, r.winfo_reqwidth()), r.winfo_reqheight()
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
        pos = load_pos()
        if pos and -w // 2 < pos[0] < sw - 40 and 0 <= pos[1] < sh - 40:
            x, y = pos
        if first or not self.moved:
            r.geometry(f"{w}x{h}+{x}+{y}")
        else:
            r.geometry(f"{w}x{h}")

    def drag_start(self, e):
        try:
            if str(e.widget.cget("cursor")) == "hand2":
                return
        except self.tk.TclError:
            pass
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self.drag:
            self.root.geometry(f"+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}")
            self.moved = True

    def drag_end(self, e):
        if self.drag and self.moved:
            save_pos(self.root.winfo_x(), self.root.winfo_y())
        self.drag = None

    # ---- talking to RebelDeck --------------------------------------------------
    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = r.read()
        return json.loads(data) if r.headers.get("Content-Type", "").startswith("application/json") else data

    def put(self, path, body):
        try:
            self.call("PUT", path, body)
        except Exception:
            pass
        self.root.after(200, self.poll_once)

    def open_gui(self, page=""):
        webbrowser.open(self.base + "/gui" + (f"#{page}" if page else ""))
        self.close()                     # the pop-out steps aside for the web GUI

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def qr_png(self, deck):
        return self.call("GET", f"/api/decks/{deck['id']}/qr.png" if deck else "/api/qr.png")

    def thumb(self, parent, deck):
        """A small QR code: click it for a big one."""
        z = self.z
        lab = self.tk.Label(parent, bg="white", cursor="hand2", bd=0, highlightthickness=1, highlightbackground=PURPLE2)
        try:
            from PIL import Image, ImageTk
            import io
            im = Image.open(io.BytesIO(self.qr_png(deck)))
            img = ImageTk.PhotoImage(im.resize((z(26), z(26)), Image.LANCZOS))
        except Exception:
            img = None
        if img:
            self.thumbs[deck["id"] if deck else "home"] = img      # keep it alive
            lab.configure(image=img)
        else:
            lab.configure(text="QR", bg=BTN, fg=TEXT, font=(FONT, z(9)), padx=z(6))
        lab.bind("<Button-1>", lambda e: self.show_qr(deck))
        return lab

    def show_qr(self, deck):
        """QR for one deck, or (deck None) the pick-a-deck page; again to hide."""
        key = deck["id"] if deck else "home"
        if self.qr_shown and self.qr_for == key:
            self.qr_box.pack_forget()
            self.qr_shown, self.qr_for = False, None
            self.place()
            return
        try:
            png = self.qr_png(deck)
            try:
                from PIL import Image, ImageTk
                import io
                im = Image.open(io.BytesIO(png))
                side = self.z(170)
                self.qr_img = ImageTk.PhotoImage(im.resize((side, side), Image.NEAREST))
            except Exception:
                self.qr_img = self.tk.PhotoImage(data=base64.b64encode(png).decode())
        except Exception:
            return
        self.qr_label.configure(image=self.qr_img)
        self.qr_caption.configure(text=f"{deck['name']}: point a phone's camera at it" if deck
                                  else "Pick-a-deck page: point a phone's camera at it")
        if not self.qr_shown:
            self.qr_box.pack(fill="x", padx=self.z(20), after=self.after_rows)
        self.qr_shown, self.qr_for = True, key
        self.place()

    def draw_decks(self, decks, local, windows=None):
        tk, z = self.tk, self.z
        for w in self.decks.winfo_children():
            w.destroy()
        if not decks:
            tk.Label(self.decks, text="No decks yet: make one in the web GUI.", bg=BG, fg=DIM,
                     font=(FONT, z(10))).pack(anchor="w")
        for d in decks:
            row = tk.Frame(self.decks, bg=BG)
            row.pack(fill="x", pady=z(3))
            col = STATE.get(d["status"], STATE["problem"])[0]
            dot = tk.Canvas(row, width=z(10), height=z(10), highlightthickness=0, bg=BG)
            dot.create_oval(1, 1, z(10) - 1, z(10) - 1, fill=col, outline=col)
            dot.pack(side="left", padx=(0, z(8)))
            tk.Label(row, text=f"{d['name']}", bg=BG, fg=TEXT, font=(FONT, z(11))).pack(side="left")
            tk.Label(row, text=f"  {d['columns']}×{d['rows']}", bg=BG, fg=DIM, font=(FONT, z(9))).pack(side="left")
            self.thumb(row, d).pack(side="right", padx=(z(8), 0))
            if local:
                sw = Switch(tk, row, z, (lambda i: lambda on: self.put(f"/api/decks/{i}", {"showing": on}))(d["id"]))
                # the window's real state, not just the setting (it may still be opening, or failed)
                sw.set(windows.get(d["id"], False) if windows is not None else d["showing"])
                sw.pack(side="right")

    def poll_once(self):
        try:
            st = self.call("GET", "/api/status")
            self.misses = 0
        except Exception:
            self.misses += 1
            if self.misses > 2:                       # RebelDeck has gone
                self.close()
            else:
                self.line.configure(text="Lost contact with RebelDeck…")
            return
        s = st.get("status", "")
        border, fill, subcol = STATE.get(s, STATE["problem"])
        for w in self.box_parts:
            w.configure(bg=fill)
        self.box.configure(highlightbackground=border, highlightcolor=border)
        self.dot.delete("all")
        self.dot.create_oval(1, 1, self.z(14) - 1, self.z(14) - 1, fill=border, outline=border)
        n = len(st.get("decks", []))
        self.line.configure(text={"connected": f"Connected · {n} deck{'s' if n != 1 else ''}",
                                  "partly connected": "Partly connected", "no decks": "No decks yet",
                                  "refused": "Companion refused a deck"}.get(s, "Not connected"))
        servers = st.get("servers", [])
        self.detail.configure(text="  ·  ".join(f"{x['name']} {x['host']}:{x['port']}" + (f" ({x['companion']})" if x.get("companion") else "")
                                                for x in servers if x.get("decks")) or st.get("companion_at", ""), fg=subcol)
        info = st.get("info") or {}
        self.sub.configure(text=f"{info.get('computer', '')} · {st.get('label', '')}")
        self.lan.configure(text=(info.get("link") or info.get("lan") or "browsers off").replace("http://", "").rstrip("/"))
        up = st.get("update") or {}
        if up.get("newer"):
            self.note.configure(text=f"Update available: {up.get('latest')}  (click to update)")
            if not self.note_shown:
                self.note.pack(fill="x", padx=self.z(20), pady=(0, self.z(8)), before=self.box)
                self.note_shown = True
        elif self.note_shown:
            self.note.pack_forget()
            self.note_shown = False
        windows = st.get("windows")
        sig = json.dumps([[(d["id"], d["name"], d["columns"], d["rows"], d["showing"], d["status"]) for d in st.get("decks", [])],
                          windows])
        if sig != self.deck_sig:
            self.deck_sig = sig
            self.draw_decks(st.get("decks", []), st.get("local", True), windows)
            self.place()
        try:
            self.login.set(bool(self.call("GET", "/api/settings").get("start_at_login")))
        except Exception:
            pass

    def poll(self):
        self.poll_once()
        self.root.after(2000, self.poll)

    def run(self):
        self.poll_once()
        self.place(first=True)
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.root.after(2000, self.poll)
        self.root.mainloop()


class Switch:
    """An on/off switch (no tick boxes)."""

    def __init__(self, tk, parent, z, on_change):
        self.z, self.on_change, self.on = z, on_change, False
        self.c = tk.Canvas(parent, width=z(36), height=z(20), highlightthickness=0, bg=BG, cursor="hand2")
        self.c.bind("<Button-1>", lambda e: self.click())
        self.draw()

    def pack(self, **kw):
        self.c.pack(**kw)

    def draw(self):
        z, c = self.z, self.c
        c.delete("all")
        w, h = z(36), z(20)
        track = PURPLE if self.on else "#251a3d"
        edge = PURPLE2 if self.on else "#3a2d58"
        c.create_oval(1, 1, h - 1, h - 1, fill=track, outline=edge)
        c.create_oval(w - h + 1, 1, w - 1, h - 1, fill=track, outline=edge)
        c.create_rectangle(h // 2, 1, w - h // 2, h - 1, fill=track, outline="")
        c.create_line(h // 2, 1, w - h // 2, 1, fill=edge)
        c.create_line(h // 2, h - 1, w - h // 2, h - 1, fill=edge)
        x = w - h + z(3) if self.on else z(3)
        c.create_oval(x, z(3), x + h - z(6), h - z(3), fill=TEXT if self.on else DIM, outline="")

    def set(self, on):
        if bool(on) != self.on:
            self.on = bool(on)
            self.draw()

    def click(self):
        self.on = not self.on
        self.draw()
        self.on_change(self.on)


def main():
    cfg = settings.load()
    try:
        import tkinter as tk
    except ImportError:
        webbrowser.open(f"http://127.0.0.1:{cfg['web_port']}/gui")
        return 0
    if sys.platform == "win32":
        # same name as the app's: the installer waits until neither is open
        import ctypes
        global _mutex
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "RebelDeckRunning")
    Popup(tk, cfg).run()
    return 0
