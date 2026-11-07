"""All the decks on this install: start, stop, add, resize, remove, and the
app settings they share. The web GUI, pop-out and tray all go through here."""
import logging
import threading

from . import settings
from .satellite import Deck

log = logging.getLogger("rebeldeck")

# settings the web GUI may change, with their checks
APP_KEYS = {
    "companion_host": lambda v: str(v).strip() or "127.0.0.1",
    "companion_port": lambda v: settings.int_setting(v, 1, 65535, 16622),
    "button_px": lambda v: settings.int_setting(v, 36, 400, 144),
    "web_port": lambda v: settings.int_setting(v, 1024, 65535, 8091),
    "web_network": bool,
    "start_at_login": bool,
    "open_windows_at_start": bool,
    "key_px": lambda v: settings.int_setting(v, 32, 400, 96),
    "popup_size": lambda v: settings.int_setting(v, 50, 300, 100),
    "sync_decks_with_companion_sync": bool,
}
RESTART_DECKS = ("companion_host", "companion_port", "button_px")
RESTART_APP = ("web_port", "web_network")


class Manager:
    def __init__(self):
        self.cfg = settings.load()
        self.decks = {}                 # id -> Deck
        self.listeners = []             # fn(kind, deck_id, value)
        self.lock = threading.RLock()
        self.on_windows = None          # set by the app: fn(deck_id, show: bool)
        self.restart_needed = False

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
        deck = Deck(self.cfg, d)
        deck.listeners.append(lambda kind, value, i=d["id"]: self._emit(kind, i, value))
        self.decks[d["id"]] = deck
        deck.start()

    def _emit(self, kind, deck_id, value):
        for fn in list(self.listeners):
            try:
                fn(kind, deck_id, value)
            except Exception:
                log.exception("listener failed")

    # ---- reading -----------------------------------------------------------
    def deck(self, deck_id):
        return self.decks.get(deck_id)

    def deck_list(self):
        out = []
        for d in self.cfg["decks"]:
            live = self.decks.get(d["id"])
            out.append({**d, "status": live.status if live else "stopped"})
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
        return {"status": overall, "companion": companion,
                "companion_at": f"{self.cfg['companion_host']}:{self.cfg['companion_port']}",
                "decks": decks}

    def app_settings(self):
        out = {k: self.cfg[k] for k in APP_KEYS}
        out["has_password"] = bool(self.cfg.get("web_password"))
        return out

    # ---- changing ------------------------------------------------------------
    def _save_decks(self):
        settings.save({"decks": self.cfg["decks"]})

    def add_deck(self, name=None, columns=None, rows=None):
        with self.lock:
            n = len(self.cfg["decks"]) + 1
            d = settings.new_deck(name=name or (f"RebelDeck {n}" if n > 1 else "RebelDeck"),
                                  columns=columns, rows=rows)
            self.cfg["decks"].append(d)
            self._save_decks()
            self._start_deck(d)
        self._emit("decks", d["id"], None)
        if d["showing"] and self.on_windows:
            self.on_windows(d["id"], True)
        return d

    def update_deck(self, deck_id, changes):
        with self.lock:
            i = next((i for i, d in enumerate(self.cfg["decks"]) if d["id"] == deck_id), None)
            if i is None:
                raise KeyError(deck_id)
            old = self.cfg["decks"][i]
            new = settings.clean_deck({**old, **{k: v for k, v in changes.items()
                                                 if k in ("name", "columns", "rows", "showing")}})
            self.cfg["decks"][i] = new
            self._save_decks()
            # a new name or size means Companion needs the deck registered again
            if any(new[k] != old[k] for k in ("name", "columns", "rows")):
                live = self.decks.pop(deck_id, None)
                if live:
                    live.stop()
                self._start_deck(new)
        self._emit("decks", deck_id, None)
        if new["showing"] != old["showing"] and self.on_windows:
            self.on_windows(deck_id, new["showing"])
        elif new["showing"] and (new["columns"], new["rows"]) != (old["columns"], old["rows"]) and self.on_windows:
            self.on_windows(deck_id, True)      # reopen at the new size
        return new

    def remove_deck(self, deck_id):
        with self.lock:
            before = len(self.cfg["decks"])
            self.cfg["decks"] = [d for d in self.cfg["decks"] if d["id"] != deck_id]
            if len(self.cfg["decks"]) == before:
                raise KeyError(deck_id)
            self._save_decks()
            live = self.decks.pop(deck_id, None)
            if live:
                live.stop()
        if self.on_windows:
            self.on_windows(deck_id, False)
        self._emit("decks", deck_id, None)

    def set_showing(self, deck_id, showing):
        return self.update_deck(deck_id, {"showing": showing})

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
                    live = self.decks.pop(d["id"], None)
                    if live:
                        live.stop()
                    self._start_deck(d)
            if any(clean.get(k) != before.get(k) for k in RESTART_APP if k in clean):
                self.restart_needed = True
        self._emit("settings", None, None)
        return self.app_settings()

    def apply_synced_decks(self, decks):
        """Replace the deck list with one that came from another rig (Companion Sync)."""
        with self.lock:
            showing = {d["id"]: d["showing"] for d in self.cfg["decks"]}
            fresh = []
            for d in decks:
                d = settings.clean_deck(d) if d.get("id") and d.get("device_id") else settings.new_deck(**d)
                d["showing"] = showing.get(d["id"], d["showing"])      # windows stay this rig's choice
                fresh.append(d)
            self.stop()
            self.cfg["decks"] = fresh
            self._save_decks()
            self.start()
        self._emit("decks", None, None)
