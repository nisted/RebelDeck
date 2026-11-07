"""The pop-out: Companion's address and showing/hiding the deck windows.

A small window of its own (tkinter), talking to RebelDeck through its local
API, so it can't hold the app up. Closing it only hides it.
"""
import json
import sys
import urllib.request
import webbrowser

from . import settings

BG, PANEL, EDGE, PURPLE, PDIM, LAV, DIM = "#06070b", "#0e0b18", "#2a1a4a", "#b973ff", "#5500aa", "#e8d6ff", "#9aa0b4"
LIGHTS = {"connected": "#50f078", "partly connected": "#ffb84d", "connecting": "#ffb84d", "no decks": DIM}


def main():
    try:
        import tkinter as tk
    except ImportError:
        cfg = settings.load()
        webbrowser.open(f"http://127.0.0.1:{cfg['web_port']}/gui")
        return 0
    cfg = settings.load()
    base = f"http://127.0.0.1:{cfg['web_port']}"
    k = max(0.5, min(3.0, int(cfg.get("popup_size", 100)) / 100)) * (1.25 if sys.platform == "darwin" else 1)

    def api(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.load(r)

    root = tk.Tk()
    root.title("RebelDeck")
    root.configure(bg=BG, highlightthickness=1, highlightbackground=PURPLE)
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    f = lambda size, bold=False: ("Helvetica", int(size * k), "bold" if bold else "normal")
    pad = int(12 * k)

    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=pad, pady=(pad, 4))
    tk.Label(head, text="REBELDECK", bg=BG, fg=PURPLE, font=f(16, True)).pack(side="left")
    light = tk.Canvas(head, width=int(14 * k), height=int(14 * k), bg=BG, highlightthickness=0)
    light.pack(side="right")
    dot = light.create_oval(2, 2, int(12 * k), int(12 * k), fill=DIM, outline="")
    status = tk.Label(root, text="Looking for RebelDeck…", bg=BG, fg=DIM, font=f(10), anchor="w", justify="left")
    status.pack(fill="x", padx=pad)

    # Companion's address
    box = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=EDGE)
    box.pack(fill="x", padx=pad, pady=pad)
    tk.Label(box, text="Companion", bg=PANEL, fg=LAV, font=f(11, True)).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 2))
    host = tk.Entry(box, width=18, bg=BG, fg=LAV, insertbackground=LAV, relief="flat", font=f(11))
    port = tk.Entry(box, width=6, bg=BG, fg=LAV, insertbackground=LAV, relief="flat", font=f(11))
    host.grid(row=1, column=0, padx=(8, 4), pady=(0, 8), sticky="we")
    port.grid(row=1, column=1, padx=4, pady=(0, 8))
    host.insert(0, cfg["companion_host"])
    port.insert(0, str(cfg["companion_port"]))

    def save_conn():
        try:
            api("PUT", "/api/settings", {"companion_host": host.get().strip(), "companion_port": int(port.get())})
            status.config(text="Saved. Reconnecting…")
        except Exception as e:
            status.config(text=f"Didn't save: {e}")
    button(tk, box, "Save", save_conn, f, k).grid(row=1, column=2, padx=(4, 8), pady=(0, 8))

    # decks
    tk.Label(root, text="Decks", bg=BG, fg=LAV, font=f(11, True), anchor="w").pack(fill="x", padx=pad)
    decks = tk.Frame(root, bg=BG)
    decks.pack(fill="x", padx=pad, pady=(2, pad))
    vars_ = {}

    def toggle(deck_id):
        try:
            api("PUT", f"/api/decks/{deck_id}", {"showing": bool(vars_[deck_id].get())})
        except Exception as e:
            status.config(text=f"Couldn't: {e}")

    def draw_decks(items):
        for w in decks.winfo_children():
            w.destroy()
        vars_.clear()
        if not items:
            tk.Label(decks, text="No decks yet: make one in the web GUI.", bg=BG, fg=DIM, font=f(10)).pack(anchor="w")
        for d in items:
            v = tk.IntVar(value=1 if d["showing"] else 0)
            vars_[d["id"]] = v
            row = tk.Frame(decks, bg=BG)
            row.pack(fill="x", pady=1)
            tk.Checkbutton(row, text=f"{d['name']}  ({d['columns']}×{d['rows']})", variable=v, command=lambda i=d["id"]: toggle(i),
                           bg=BG, fg=LAV, selectcolor=PDIM, activebackground=BG, activeforeground=LAV, font=f(11),
                           anchor="w").pack(side="left")
            c = tk.Canvas(row, width=int(10 * k), height=int(10 * k), bg=BG, highlightthickness=0)
            c.pack(side="right")
            c.create_oval(1, 1, int(9 * k), int(9 * k), fill=LIGHTS.get(d["status"], "#ff4d6d"), outline="")

    bottom = tk.Frame(root, bg=BG)
    bottom.pack(fill="x", padx=pad, pady=(0, pad))
    button(tk, bottom, "Open web GUI", lambda: webbrowser.open(base + "/gui"), f, k, primary=True).pack(side="left")
    button(tk, bottom, "Hide", root.destroy, f, k).pack(side="right")
    root.bind("<Escape>", lambda *_: root.destroy())

    last = {"sig": None}

    def poll():
        try:
            st = api("GET", "/api/status")
            light.itemconfig(dot, fill=LIGHTS.get(st["status"], "#ff4d6d"))
            txt = (f"Companion {st.get('companion') or ''} at {st['companion_at']}" if st["status"] == "connected"
                   else f"{st['status'].capitalize()} · Companion at {st['companion_at']}")
            if st.get("update", {}).get("newer"):
                txt += f"\nUpdate available: {st['update']['latest']} (tray menu)"
            if st.get("lan"):
                txt += f"\nPhones: {st['lan']}"
            status.config(text=txt)
            sig = json.dumps([(d["id"], d["name"], d["columns"], d["rows"], d["showing"], d["status"]) for d in st["decks"]])
            if sig != last["sig"] and not root.focus_get() in vars_.values():
                last["sig"] = sig
                draw_decks(st["decks"])
        except Exception:
            status.config(text="Lost contact with RebelDeck. Is it running?")
            light.itemconfig(dot, fill="#ff4d6d")
        root.after(2000, poll)

    poll()
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    root.mainloop()
    return 0


def button(tk, parent, text, cmd, f, k, primary=False):
    return tk.Button(parent, text=text.upper(), command=cmd, bg=PDIM if primary else "#17102b", fg=LAV,
                     activebackground=PURPLE, activeforeground="#000", relief="flat", font=f(10, True),
                     padx=int(10 * k), pady=int(4 * k), highlightthickness=1, highlightbackground=PURPLE, bd=0)
