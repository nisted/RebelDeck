"""All the decks on this install: start, stop, add, change, remove; the
Companion servers they use; saved profiles; and the app settings they share.
The web GUI, pop-out and tray all go through here."""
import copy
import logging
import sys
import threading

from . import settings
from .satellite import Deck

log = logging.getLogger("rebeldeck")
WIN = sys.platform == "win32"

# settings the web GUI may change, with their checks
APP_KEYS = {
    "button_px": lambda v: settings.int_setting(v, 0, 400, 0),
    "web_port": lambda v: settings.int_setting(v, 1024, 65535, 8091),
    "web_network": bool,
    "start_at_login": bool,
    "open_windows_at_start": bool,
    "popup_size": lambda v: settings.int_setting(v, 50, 300, 100),
    "sync_decks_with_companion_sync": bool,
    "find_other_rigs": bool,
    "discovery_port": lambda v: settings.int_setting(v, 1024, 65535, 8092),
    "link_address": lambda v: v if v in settings.LINK_ADDRESSES else "local",
}
RESTART_DECKS = ("button_px",)
RESTART_APP = ("web_port", "web_network", "discovery_port")

# deck settings anyone with the GUI may change, and which of them need the deck
# registering with Companion again
DECK_KEYS = ("name", "columns", "rows", "showing", "own_server", "server", "click_mode", "blocked",
             "key_types", "rotary", "hotkeys", "strip_ratio", "breakout", "breakout_windows", "float", "movable", "transparency", "colour", "window")
REREGISTER = ("name", "columns", "rows", "key_types", "strip_ratio", "picture_px")

PICTURE_STEPS = (72, 96, 120, 144, 196, 240, 288)


class NameInUse(ValueError):
    pass


class Manager:
    def __init__(self):
        self.cfg = settings.load()
        self.decks = {}                 # id -> Deck
        self.listeners = []             # fn(kind, deck_id, value)
        self.lock = threading.RLock()
        self.on_windows = None          # set by the app: fn(deck_id, show: bool)
        self.restart_needed = False
        self.hotkey_problems = {}       # deck id -> {"n": why its hotkey isn't working}, from hotkeys.py
        self.hotkeys_info = {"backend": "none", "note": ""}

    # ---- lifecycle -----------------------------------------------------
    def start(self):
        for d in self.cfg["decks"]:
            self._start_deck(d)

    def stop(self):
        with self.lock:
            for deck in list(self.decks.values()):
                deck.stop()
            self.decks.clear()

    def _start_deck(self, d):
        deck = Deck(self.server_for(d), d, self.picture_px(d))
        deck.listeners.append(lambda kind, value, i=d["id"]: self._emit(kind, i, value))
        self.decks[d["id"]] = deck
        deck.start()

    def _restart_deck(self, d):
        live = self.decks.pop(d["id"], None)
        if live:
            live.stop()
        self._start_deck(d)

    def _emit(self, kind, deck_id, value):
        for fn in list(self.listeners):
            try:
                fn(kind, deck_id, value)
            except Exception:
                log.exception("listener failed")

    # ---- reading -----------------------------------------------------------
    def deck(self, ref):
        """A running deck by id or by its address name."""
        if ref in self.decks:
            return self.decks[ref]
        d = self.deck_cfg(ref)
        return self.decks.get(d["id"]) if d else None

    def deck_cfg(self, ref):
        return next((d for d in self.cfg["decks"] if ref in (d["id"], d["slug"])), None)

    def server(self, sid):
        return next((s for s in self.cfg["servers"] if s["id"] == sid), None)

    def server_for(self, d):
        """The Companion a deck uses: its own, if its switch is on, else the default."""
        s = self.server(d["server"]) if d.get("own_server") else None
        return s or self.server(self.cfg["default_server"]) or self.cfg["servers"][0]

    def picture_px(self, d):
        """Button picture size to ask Companion for: the setting, or (automatic)
        about the size each button shows at, from the deck window's last size."""
        fixed = int(self.cfg.get("button_px") or 0)
        if fixed:
            return fixed
        w = (d.get("window") or {}).get("w")
        if not w:
            return 144
        want = w / max(1, d["columns"]) * 1.5            # allow for high-resolution screens
        return min(PICTURE_STEPS, key=lambda s: abs(s - want))

    def deck_list(self):
        out = []
        for n, d in enumerate(self.cfg["decks"], 1):
            live = self.decks.get(d["id"])
            s = self.server_for(d)
            out.append({**d, "n": n, "locations": live.locations() if live else [],
                        "status": live.status if live else "stopped",
                        "companion_at": f"{s['host']}:{s['port']}", "server_name": s["name"],
                        "server_used": s["id"], "picture_px": self.picture_px(d),
                        "hotkey_problems": self.hotkey_problems.get(d["id"], {})})
        return out

    def status(self):
        decks = self.deck_list()
        sts = [d["status"] for d in decks]
        if not decks:
            overall = "no decks"
        elif all(s == "connected" for s in sts):
            overall = "connected"
        elif any(s == "refused" for s in sts):
            overall = "refused"
        elif any(s == "connected" for s in sts):
            overall = "partly connected"
        else:
            overall = "disconnected"
        companion = next((x.companion_version for x in self.decks.values() if x.companion_version), "")
        default = self.server(self.cfg["default_server"]) or self.cfg["servers"][0]
        return {"status": overall, "companion": companion,
                "companion_at": f"{default['host']}:{default['port']}",
                "decks": decks, "servers": self.server_list(), "default_server": self.cfg["default_server"],
                "hotkeys": self.hotkeys_info}

    def server_list(self):
        out = []
        for s in self.cfg["servers"]:
            using = [d for d in self.cfg["decks"] if self.server_for(d)["id"] == s["id"]]
            live = [self.decks[d["id"]] for d in using if d["id"] in self.decks]
            st = ("connected" if live and all(x.status == "connected" for x in live) else
                  "unused" if not live else live[0].status)
            ver = next((x.companion_version for x in live if x.companion_version), "")
            out.append({**s, "default": s["id"] == self.cfg["default_server"], "decks": len(using),
                        "status": st, "companion": ver})
        return out

    def app_settings(self):
        out = {k: self.cfg[k] for k in APP_KEYS}
        out["has_password"] = bool(self.cfg.get("web_password"))
        return out

    # ---- decks -----------------------------------------------------------------
    def _save_decks(self):
        settings.save({"decks": self.cfg["decks"]})

    def _check_name(self, name, deck_id=None):
        s = settings.slug(name)
        if any(d["slug"] == s and d["id"] != deck_id for d in self.cfg["decks"]):
            raise NameInUse(f'there\'s already a deck called "{name}" (or near enough)')

    def _check_hotkeys(self, hotkeys, deck_id):
        """A hotkey can press only one key, on any deck."""
        from .hotkeys import normalise
        if not isinstance(hotkeys, dict):
            raise ValueError("hotkeys should be {key number: shortcut}")
        mine = {}
        for k, v in hotkeys.items():
            n = normalise(v)
            if v and not n:
                raise ValueError(f'"{v}" can\'t be a hotkey: it needs Ctrl, Alt or Win/Cmd (or an F key) and one key')
            if n in mine.values():
                raise ValueError(f"{n} already presses another button on this deck")
            mine[k] = n
        for d in self.cfg["decks"]:
            if d["id"] == deck_id:
                continue
            for k, v in d.get("hotkeys", {}).items():
                if v in mine.values():
                    raise ValueError(f'{v} already presses a key on {d["name"]}')

    def tap(self, ref, key):
        """Press and let go (a hotkey). Sticky keys toggle."""
        deck = self.deck(ref)
        if deck is None:
            raise KeyError(ref)
        deck.press(int(key), True)
        threading.Timer(0.1, deck.press, (int(key), False)).start()

    def add_deck(self, name=None, columns=None, rows=None):
        with self.lock:
            if name:
                self._check_name(name)
            else:
                n = len(self.cfg["decks"]) + 1
                name = "RebelDeck"
                while any(d["slug"] == settings.slug(name) for d in self.cfg["decks"]):
                    name = f"RebelDeck {n}"
                    n += 1
            d = settings.new_deck(name=name, columns=columns, rows=rows)
            self.cfg["decks"].append(d)
            self._save_decks()
            self._start_deck(d)
        self._emit("decks", d["id"], None)
        if d["showing"] and self.on_windows:
            self.on_windows(d["id"], True)
        return d

    def update_deck(self, ref, changes):
        with self.lock:
            old = self.deck_cfg(ref)
            if old is None:
                raise KeyError(ref)
            i = self.cfg["decks"].index(old)
            ch = {k: v for k, v in changes.items() if k in DECK_KEYS}
            if "name" in ch:
                self._check_name(str(ch["name"]).strip() or "RebelDeck", old["id"])
            if "hotkeys" in ch:
                self._check_hotkeys(ch["hotkeys"], old["id"])
            if "server" in ch and ch["server"] and not self.server(ch["server"]):
                raise ValueError("no such Companion server")
            if ("columns" in ch or "rows" in ch) and "window" not in ch:
                ch["window"] = {}                 # new shape: the window finds a new size
            new = settings.clean_deck({**copy.deepcopy(old), **ch})
            self.cfg["decks"][i] = new
            self._save_decks()
            before = dict(old, picture_px=self.picture_px(old))
            after = dict(new, picture_px=self.picture_px(new))
            if (any(before[k] != after[k] for k in REREGISTER)
                    or self.server_for(old)["id"] != self.server_for(new)["id"]):
                self._restart_deck(new)
            else:
                live = self.decks.get(new["id"])
                if live:
                    live.deck = new
        self._emit("decks", new["id"], None)
        if new["showing"] != old["showing"] and self.on_windows:
            self.on_windows(new["id"], new["showing"])
        elif new["breakout"] != old["breakout"] and self.on_windows and new["showing"]:
            self.on_windows(new["id"], True)      # open or close the broken-out keys' windows
        elif changes.get("window") == {} and old.get("window") and self.on_windows and new["showing"]:
            self.on_windows(new["id"], "reopen")  # Reset: back to the starting size and place
        elif ((new["transparency"] > 0) != (old["transparency"] > 0) and self.on_windows and new["showing"]
              and not WIN):
            self.on_windows(new["id"], "reopen")  # Macs and Pis make a see-through window when it opens
        return new

    def remove_deck(self, ref):
        with self.lock:
            d = self.deck_cfg(ref)
            if d is None:
                raise KeyError(ref)
            self.cfg["decks"].remove(d)
            self._save_decks()
            live = self.decks.pop(d["id"], None)
            if live:
                live.stop()
        if self.on_windows:
            self.on_windows(d["id"], False)
        self._emit("decks", d["id"], None)

    def set_showing(self, ref, showing):
        return self.update_deck(ref, {"showing": showing})

    def identify(self, ref):
        """Make a deck flash its name wherever it's open, to find it on screen."""
        d = self.deck_cfg(ref)
        if d is None:
            raise KeyError(ref)
        self._emit("identify", d["id"], None)

    # ---- Companion servers ------------------------------------------------------
    def _save_servers(self):
        settings.save({"servers": self.cfg["servers"], "default_server": self.cfg["default_server"]})

    def add_server(self, name, host, port=16622, make_default=False):
        with self.lock:
            s = settings.clean_server({"id": None, "name": name, "host": host, "port": port})
            while self.server(s["id"]):
                s = settings.clean_server({**s, "id": None})
            self.cfg["servers"].append(s)
            if make_default:
                self.cfg["default_server"] = s["id"]
            self._save_servers()
        self._emit("servers", None, None)
        return s

    def update_server(self, sid, changes):
        with self.lock:
            old = self.server(sid)
            if not old:
                raise KeyError(sid)
            i = self.cfg["servers"].index(old)
            new = settings.clean_server({**old, **{k: v for k, v in changes.items() if k in ("name", "host", "port")},
                                         "id": sid})
            self.cfg["servers"][i] = new
            if changes.get("default"):
                self.cfg["default_server"] = sid
            self._save_servers()
            if (new["host"], new["port"]) != (old["host"], old["port"]) or changes.get("default"):
                for d in self.cfg["decks"]:
                    self._restart_deck(d)
        self._emit("servers", None, None)
        return new

    def remove_server(self, sid):
        with self.lock:
            s = self.server(sid)
            if not s:
                raise KeyError(sid)
            if len(self.cfg["servers"]) == 1:
                raise ValueError("RebelDeck needs at least one Companion")
            self.cfg["servers"].remove(s)
            if self.cfg["default_server"] == sid:
                self.cfg["default_server"] = self.cfg["servers"][0]["id"]
            for d in self.cfg["decks"]:
                if d["server"] == sid:
                    d["own_server"], d["server"] = False, ""
            self._save_servers()
            self._save_decks()
            for d in self.cfg["decks"]:
                self._restart_deck(d)
        self._emit("servers", None, None)
        self._emit("decks", None, None)

    # ---- app settings -----------------------------------------------------------
    def set_app_settings(self, changes):
        with self.lock:
            clean = {k: APP_KEYS[k](v) for k, v in changes.items() if k in APP_KEYS}
            if "web_password" in changes:
                clean["web_password"] = str(changes["web_password"] or "")
            before = {k: self.cfg.get(k) for k in clean}
            self.cfg.update(clean)
            settings.save(clean)
            if any(clean.get(k) != before.get(k) for k in RESTART_DECKS if k in clean):
                for d in self.cfg["decks"]:
                    self._restart_deck(d)
            if any(clean.get(k) != before.get(k) for k in RESTART_APP if k in clean):
                self.restart_needed = True
        self._emit("settings", None, None)
        return self.app_settings()

    # ---- profiles -----------------------------------------------------------------
    def profile_list(self):
        return [{"name": n, "decks": len(p.get("decks", []))} for n, p in sorted(self.cfg["profiles"].items())]

    def save_profile(self, name):
        name = str(name or "").strip()[:40]
        if not name:
            raise ValueError("a profile needs a name")
        with self.lock:
            self.cfg["profiles"][name] = {"decks": copy.deepcopy(self.cfg["decks"])}
            settings.save({"profiles": self.cfg["profiles"]})
        self._emit("profiles", None, None)
        return self.profile_list()

    def load_profile(self, name):
        p = self.cfg["profiles"].get(name)
        if not p:
            raise KeyError(name)
        self.replace_decks(p.get("decks", []), keep_showing=False)
        return self.deck_list()

    def remove_profile(self, name):
        with self.lock:
            if self.cfg["profiles"].pop(name, None) is None:
                raise KeyError(name)
            settings.save({"profiles": self.cfg["profiles"]})
        self._emit("profiles", None, None)

    def replace_decks(self, decks, keep_showing=True):
        """Swap the whole set of decks (a profile, or Rebel Sync bringing
        another rig's). Decks keep their ids, so Companion keeps their setup."""
        with self.lock:
            old_ids = {d["id"] for d in self.cfg["decks"]}
            showing = {d["id"]: d["showing"] for d in self.cfg["decks"]}
            fresh = []
            for d in decks:
                d = settings.clean_deck(d) if d.get("id") and d.get("device_id") else settings.new_deck(**d)
                if keep_showing:
                    d["showing"] = showing.get(d["id"], d["showing"])      # windows stay this rig's choice
                if d["server"] and not self.server(d["server"]):
                    d["own_server"], d["server"] = False, ""
                fresh.append(d)
            self.stop()
            self.cfg["decks"] = settings.unique_names(fresh)
            self._save_decks()
            self.start()
        if self.on_windows:
            new_ids = {d["id"] for d in self.cfg["decks"]}
            for gone in old_ids - new_ids:
                self.on_windows(gone, False)
            for d in self.cfg["decks"]:
                self.on_windows(d["id"], d["showing"])
        self._emit("decks", None, None)

    def apply_synced_decks(self, decks):
        """Deck layouts from another rig, via Rebel Sync."""
        self.replace_decks(decks, keep_showing=True)
