"""Settings: ~/.config/rebeldeck/settings.json.

App settings sit at the top level; each deck is an entry in "decks".
Only what differs from the defaults needs to be in the file.
"""
import json
import os
import threading
import uuid

DIR = os.path.expanduser("~/.config/rebeldeck")
FILE = os.path.join(DIR, "settings.json")

DEFAULTS = {
    "companion_host": "127.0.0.1",   # the machine running Companion
    "companion_port": 16622,         # Companion's Satellite port (TCP)
    "button_px": 144,                # button picture size asked of Companion
    "web_port": 8091,                # deck pages, web GUI and API
    "web_network": True,             # phones and tablets on the network can open decks
    "web_password": "",              # optional: other devices need it for the web GUI
    "start_at_login": True,
    "open_windows_at_start": True,   # reopen the deck windows that were showing
    "key_px": 96,                    # deck window: button size on screen
    "popup_size": 100,               # pop-out size, %
    "sync_decks_with_companion_sync": False,
}

DECK_DEFAULTS = {"name": "RebelDeck", "columns": 8, "rows": 4, "showing": True}

# Stream Deck sizes, plus a few handy ones (columns, rows)
PRESETS = [
    ("Mini", 3, 2), ("MK.2 / Original", 5, 3), ("XL", 8, 4), ("Plus", 4, 2), ("Neo", 4, 2),
    ("Pedal-ish strip", 3, 1), ("Big pad", 10, 6),
]

MAX_COLS, MAX_ROWS = 32, 32

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


def new_deck(**kw):
    d = dict(DECK_DEFAULTS)
    d.update({k: v for k, v in kw.items() if v is not None})
    d["id"] = d.get("id") or uuid.uuid4().hex[:8]
    d["device_id"] = d.get("device_id") or "rebeldeck-" + d["id"]
    return clean_deck(d)


def clean_deck(d):
    """Keep a deck's values sensible, whatever was typed into the file."""
    out = dict(DECK_DEFAULTS)
    out.update(d)
    out["name"] = (str(out.get("name") or "RebelDeck").strip() or "RebelDeck")[:40]
    for k, hi in (("columns", MAX_COLS), ("rows", MAX_ROWS)):
        try:
            out[k] = max(1, min(hi, int(out[k])))
        except (TypeError, ValueError):
            out[k] = DECK_DEFAULTS[k]
    out["showing"] = bool(out.get("showing", True))
    return out


def load():
    """App settings and decks. Moves a v0.1 single-deck file to the deck list."""
    with _lock:
        mine = _read()
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in mine.items() if k != "decks"})
        decks = mine.get("decks")
        changed = False
        if not isinstance(decks, list) or not decks:
            # v0.1 kept one deck's size and id at the top level
            decks = [new_deck(name=mine.get("deck_name"), columns=mine.get("columns"), rows=mine.get("rows"),
                              device_id=mine.get("device_id"))]
            for k in ("deck_name", "columns", "rows", "device_id", "open_window"):
                mine.pop(k, None)
                cfg.pop(k, None)
            changed = True
        cfg["decks"] = [clean_deck(d) if d.get("id") and d.get("device_id") else new_deck(**d) for d in decks]
        if changed or any("id" not in d or "device_id" not in d for d in decks):
            mine["decks"] = cfg["decks"]
            _write(mine)
        return cfg


def save(changes):
    """Merge changes (app keys and/or a full "decks" list) into the file."""
    with _lock:
        mine = _read()
        mine.update(changes)
        _write(mine)


def int_setting(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default
