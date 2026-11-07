"""Settings: ~/.config/rebeldeck/settings.json.

App settings sit at the top level; each deck is an entry in "decks", each
Companion server an entry in "servers", saved sets of decks in "profiles".
Only what differs from the defaults needs to be in the file.
"""
import copy
import json
import os
import re
import threading
import uuid

DIR = os.path.expanduser("~/.config/rebeldeck")
FILE = os.path.join(DIR, "settings.json")

DEFAULTS = {
    "button_px": 0,                  # button picture size asked of Companion; 0 = automatic
    "web_port": 8091,                # deck pages, web GUI and API
    "web_network": True,             # phones and tablets on the network can open decks
    "web_password": "",              # optional: other devices need it for the web GUI
    "start_at_login": True,
    "open_windows_at_start": True,   # reopen the deck windows that were showing
    "popup_size": 100,               # pop-out size, %
    "sync_decks_with_companion_sync": False,
    "find_other_rigs": True,         # list decks from other RebelDecks on the network
    "discovery_port": 8092,          # UDP port RebelDecks say hello on
    "default_server": "main",
}

SERVER_DEFAULTS = {"id": "main", "name": "Companion", "host": "127.0.0.1", "port": 16622}

DECK_DEFAULTS = {
    "name": "RebelDeck", "columns": 8, "rows": 4, "showing": True,
    "own_server": False, "server": "",          # own_server on: this deck uses "server", not the default
    "click_mode": "clickable",                  # clickable | unclickable | hybrid
    "blocked": [],                              # hybrid: keys that ignore presses
    "key_types": {},                            # "n": "rotary" | "fader" (others are buttons)
    "breakout": [],                             # keys in their own little windows
    "breakout_windows": {},                     # "n": {"x", "y", "w", "h"}
    "float": True,                              # deck window stays on top
    "movable": True,                            # deck window can be moved and resized
    "transparency": 0,                          # % see-through background (0-90)
    "colour": "#000000",                        # window background
    "window": {},                               # {"x", "y", "w", "h"}: where it was last
}

KEY_TYPES = ("button", "rotary", "fader")
CLICK_MODES = ("clickable", "unclickable", "hybrid")

# Stream Deck sizes, plus a few handy ones (columns, rows)
PRESETS = [
    ("Mini", 3, 2), ("MK.2 / Original", 5, 3), ("XL", 8, 4), ("Plus", 4, 2), ("Neo", 4, 2),
    ("Pedal-ish strip", 3, 1), ("Big pad", 10, 6),
]

MAX_COLS, MAX_ROWS = 32, 32
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

_lock = threading.RLock()


def _read():
    try:
        with open(FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d):
    os.makedirs(DIR, exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, FILE)


def slug(name):
    """A deck's name as it appears in its address: 'FOH Mini' -> 'foh-mini'."""
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s or "deck"


def int_setting(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


def _key_list(v, total):
    out = set()
    for x in v if isinstance(v, (list, tuple)) else []:
        try:
            x = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= x < total:
            out.add(x)
    return sorted(out)


def _box(v):
    if not isinstance(v, dict):
        return {}
    out = {}
    for k in ("x", "y", "w", "h"):
        try:
            out[k] = int(v[k])
        except (KeyError, TypeError, ValueError):
            return {}
    return out if out["w"] > 20 and out["h"] > 20 else {}


def new_deck(**kw):
    d = copy.deepcopy(DECK_DEFAULTS)
    d.update({k: v for k, v in kw.items() if v is not None})
    d["id"] = d.get("id") or uuid.uuid4().hex[:8]
    d["device_id"] = d.get("device_id") or "rebeldeck-" + d["id"]
    return clean_deck(d)


def clean_deck(d):
    """Keep a deck's values sensible, whatever was typed into the file."""
    out = copy.deepcopy(DECK_DEFAULTS)
    out.update(d)
    out["name"] = (str(out.get("name") or "RebelDeck").strip() or "RebelDeck")[:40]
    out["slug"] = slug(out["name"])
    for k, hi in (("columns", MAX_COLS), ("rows", MAX_ROWS)):
        try:
            out[k] = max(1, min(hi, int(out[k])))
        except (TypeError, ValueError):
            out[k] = DECK_DEFAULTS[k]
    total = out["columns"] * out["rows"]
    for k in ("showing", "own_server", "float", "movable"):
        out[k] = bool(out.get(k, DECK_DEFAULTS[k]))
    out["server"] = str(out.get("server") or "")
    if out["click_mode"] not in CLICK_MODES:
        out["click_mode"] = "clickable"
    out["blocked"] = _key_list(out.get("blocked"), total)
    out["breakout"] = _key_list(out.get("breakout"), total)
    kt = out.get("key_types") if isinstance(out.get("key_types"), dict) else {}
    out["key_types"] = {str(k): v for k, v in kt.items()
                        if v in ("rotary", "fader") and str(k).isdigit() and int(k) < total}
    bw = out.get("breakout_windows") if isinstance(out.get("breakout_windows"), dict) else {}
    out["breakout_windows"] = {str(k): b for k, b in ((k, _box(v)) for k, v in bw.items())
                               if b and str(k).isdigit() and int(k) < total}
    out["transparency"] = int_setting(out.get("transparency"), 0, 90, 0)
    out["colour"] = out["colour"] if HEX.match(str(out.get("colour"))) else "#000000"
    out["window"] = _box(out.get("window"))
    return out


def clean_server(s):
    out = dict(SERVER_DEFAULTS)
    out.update(s if isinstance(s, dict) else {})
    out["id"] = str(out.get("id") or uuid.uuid4().hex[:6])
    out["name"] = (str(out.get("name") or "Companion").strip() or "Companion")[:40]
    out["host"] = str(out.get("host") or "127.0.0.1").strip()[:200] or "127.0.0.1"
    out["port"] = int_setting(out.get("port"), 1, 65535, 16622)
    return out


def unique_names(decks):
    """Deck names must differ (their addresses come from them): add 2, 3… to repeats."""
    seen = set()
    for d in decks:
        base, n = d["name"], 2
        while d["slug"] in seen:
            d["name"] = f"{base[:36]} {n}"
            d["slug"] = slug(d["name"])
            n += 1
        seen.add(d["slug"])
    return decks


def load():
    """App settings, servers and decks. Moves older files to the current layout."""
    with _lock:
        mine = _read()
        cfg = copy.deepcopy(DEFAULTS)
        cfg.update({k: v for k, v in mine.items() if k not in ("decks", "servers", "profiles")})
        changed = False

        # servers: v0.1/v0.2 had one Companion address at the top level
        servers = mine.get("servers")
        if not isinstance(servers, list) or not servers:
            servers = [clean_server({"id": "main", "name": "Companion",
                                     "host": mine.get("companion_host", "127.0.0.1"),
                                     "port": mine.get("companion_port", 16622)})]
            changed = True
        servers = [clean_server(s) for s in servers]
        ids = set()
        for s in servers:
            while s["id"] in ids:
                s["id"] = uuid.uuid4().hex[:6]
            ids.add(s["id"])
        cfg["servers"] = servers
        if cfg.get("default_server") not in ids:
            cfg["default_server"] = servers[0]["id"]
        for k in ("companion_host", "companion_port", "key_px"):
            if k in mine:
                mine.pop(k)
                changed = True
            cfg.pop(k, None)

        # decks: v0.1 kept one deck's size and id at the top level
        decks = mine.get("decks")
        if not isinstance(decks, list) or not decks:
            decks = [new_deck(name=mine.get("deck_name"), columns=mine.get("columns"), rows=mine.get("rows"),
                              device_id=mine.get("device_id"))]
            for k in ("deck_name", "columns", "rows", "device_id", "open_window"):
                mine.pop(k, None)
                cfg.pop(k, None)
            changed = True
        cfg["decks"] = unique_names([clean_deck(d) if isinstance(d, dict) and d.get("id") and d.get("device_id")
                                     else new_deck(**(d if isinstance(d, dict) else {})) for d in decks])
        profiles = mine.get("profiles")
        cfg["profiles"] = profiles if isinstance(profiles, dict) else {}
        if changed or cfg["decks"] != decks:
            mine["decks"] = cfg["decks"]
            mine["servers"] = cfg["servers"]
            mine["default_server"] = cfg["default_server"]
            _write(mine)
        return cfg


def save(changes):
    """Merge changes (app keys, "decks", "servers", "profiles") into the file."""
    with _lock:
        mine = _read()
        mine.update(changes)
        _write(mine)
