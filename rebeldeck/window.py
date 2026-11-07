"""A deck in RebelDeck's own window (pywebview): frameless, optionally
floating on top, drag-to-resize locked to the deck's shape (the page works
out the size), and it remembers where it was.

Each window is its own small process, started by the app:
    rebeldeck --deck-window <deck id> [--key <n>]
so the app can open and close it for certain, and one stuck window can't
take the others with it. It talks to the app over the local API.

pywebview draws with the system's own web engine: WebView2 on Windows,
WebKit on the Mac, WebKitGTK on the Pi. No browser profile, no sign-in.
"""
import json
import logging
import sys
import threading
import time
import urllib.request

from . import settings

log = logging.getLogger("rebeldeck.window")
WIN = sys.platform == "win32"


def available():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def _call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.load(r)


def default_size(cols, rows, key_px=96, gap=6, pad=8, head=26):
    return (cols * key_px + gap * (cols - 1) + pad * 2,
            head + rows * key_px + gap * (rows - 1) + pad * 2)


def main(argv):
    import webview

    cfg = settings.load()
    base = f"http://127.0.0.1:{cfg['web_port']}"
    deck_id = argv[argv.index("--deck-window") + 1]
    key = int(argv[argv.index("--key") + 1]) if "--key" in argv else None
    d = next((x for x in _call(base, "GET", "/api/decks") if x["id"] == deck_id), None)
    if not d:
        return 1
    if key is None:
        geo = d.get("window") or {}
        w, h = default_size(d["columns"], d["rows"])
        url = f"{base}/deck/{d['slug']}?window=1"
        title = f"{d['name']} · RebelDeck"
    else:
        geo = d.get("breakout_windows", {}).get(str(key)) or {}
        w, h = 110, 122
        url = f"{base}/deck/{d['slug']}?window=1&key={key}"
        title = f"{d['name']} · key {key} · RebelDeck"
    w, h = geo.get("w", w), geo.get("h", h)
    see_through = d.get("transparency", 0) > 0

    state = {"x": geo.get("x"), "y": geo.get("y"), "w": w, "h": h}
    save_timer = [None]

    def save_geometry():
        box = {k: int(state[k]) for k in ("x", "y", "w", "h") if state.get(k) is not None}
        if len(box) < 4:
            return
        try:
            if key is None:
                _call(base, "PUT", f"/api/decks/{deck_id}", {"window": box})
            else:
                fresh = next((x for x in _call(base, "GET", "/api/decks") if x["id"] == deck_id), {})
                bw = dict(fresh.get("breakout_windows", {}))
                bw[str(key)] = box
                _call(base, "PUT", f"/api/decks/{deck_id}", {"breakout_windows": bw})
        except Exception as e:
            log.info("couldn't save the window's place: %s", e)

    def save_soon():
        if save_timer[0]:
            save_timer[0].cancel()
        save_timer[0] = threading.Timer(0.8, save_geometry)
        save_timer[0].start()

    # Requests from the page arrive while the window may be busy handling them;
    # pywebview's window changes wait for the window to be free. Making a change
    # and waiting for it from the same request can hang the window (it did with
    # Float on Windows), so each change is handed to a thread of its own and the
    # page gets its answer straight away. Resizes are squashed to the latest.
    pending = {"size": None}
    size_lock = threading.Lock()

    def later(fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    # On Windows, float and size go straight to Windows (SetWindowPos with
    # SWP_ASYNCWINDOWPOS: Windows queues the change and never waits for the
    # window), not through pywebview: Float through pywebview froze windows.
    hwnd = [None]

    def find_hwnd():
        if hwnd[0] is None:
            import ctypes
            hwnd[0] = ctypes.windll.user32.FindWindowW(None, title) or None
            log.info("%s: window handle %s", title, hwnd[0])
        return hwnd[0]

    def win_set_pos(after, w=0, h=0, flags=0):
        import ctypes
        from ctypes import wintypes
        h_ = find_hwnd()
        if not h_:
            return False
        u = ctypes.windll.user32
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        ok = u.SetWindowPos(h_, after, 0, 0, w, h, flags | 0x0002 | 0x0010 | 0x4000)   # NOMOVE, NOACTIVATE, ASYNC
        return bool(ok)

    def dpi_scale():
        try:
            import ctypes
            return ctypes.windll.user32.GetDpiForWindow(find_hwnd()) / 96 or 1
        except Exception:
            return 1

    def apply_size():
        while True:
            with size_lock:
                size, pending["size"] = pending["size"], None
                if size is None:
                    pending["busy"] = False
                    return
            try:
                if WIN:
                    k = dpi_scale()
                    if win_set_pos(None, round(size[0] * k), round(size[1] * k), 0x0004 | 0x0200):   # NOZORDER, NOOWNERZORDER
                        continue
                window.resize(*size)
            except Exception as e:
                log.info("resize: %s", e)

    def set_on_top(on):
        log.info("%s: float %s", title, "on" if on else "off")
        try:
            if WIN:
                import ctypes
                from ctypes import wintypes
                after = wintypes.HWND(-1 if on else -2)                 # HWND_TOPMOST / HWND_NOTOPMOST
                if win_set_pos(after, flags=0x0001):                     # NOSIZE
                    return
                log.info("%s: no window handle; float via pywebview", title)
            window.on_top = bool(on)
        except Exception as e:
            log.info("float: %s", e)

    def set_opacity(a):
        if WIN:
            try:
                window.native.Invoke(__import__("System").Action(lambda: setattr(window.native, "Opacity", a)))
            except Exception:
                try:
                    window.native.Opacity = a
                except Exception as e:
                    log.info("transparency: %s", e)

    class Api:
        def resize(self, width, height):
            state["w"], state["h"] = int(width), int(height)
            with size_lock:
                pending["size"] = (int(width), int(height))
                if pending.get("busy"):
                    return
                pending["busy"] = True
            later(apply_size)

        def set_float(self, on):
            later(set_on_top, on)

        def set_see_through(self, pct):
            later(set_opacity, max(0.1, 1 - int(pct) / 100))

        def save_geometry(self):
            save_soon()

        def front(self):
            def go():
                try:
                    log.info("%s: identify", title)
                    window.show()
                    window.restore()
                    set_on_top(True)
                    time.sleep(3)
                    set_on_top(bool(_call(base, "GET", f"/api/decks/{deck_id}/state")["style"]["float"]))
                except Exception as e:
                    log.info("front: %s", e)
            later(go)

        def close(self):
            log.info("%s: closing (asked by the page)", title)
            later(window.destroy)

    window = webview.create_window(
        title, url, width=int(w), height=int(h), x=state["x"], y=state["y"],
        frameless=True, easy_drag=False, resizable=True, on_top=bool(d.get("float", True)),
        transparent=see_through and not WIN, background_color=d.get("colour", "#000000"),
        min_size=(40, 40), js_api=Api())

    def moved(*args):
        if len(args) >= 2:
            state["x"], state["y"] = int(args[0]), int(args[1])
            save_soon()

    def resized(*args):
        if len(args) >= 2:
            state["w"], state["h"] = int(args[0]), int(args[1])

    def started():
        # Windows draws see-through windows differently: fade the whole window instead
        if WIN and see_through:
            try:
                window.native.Opacity = max(0.1, 1 - d.get("transparency", 0) / 100)
            except Exception as e:
                log.info("transparency: %s", e)

    window.events.moved += moved
    window.events.resized += resized
    window.events.shown += started
    log.info("%s: opening", title)
    webview.start(private_mode=True)
    log.info("%s: closed", title)
    if save_timer[0]:
        save_timer[0].cancel()
    save_geometry()
    return 0
