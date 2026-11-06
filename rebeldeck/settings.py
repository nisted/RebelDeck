"""Settings: ~/.config/rebeldeck/settings.json, only the keys you change."""
import json
import os
import uuid

FILE = os.path.expanduser("~/.config/rebeldeck/settings.json")

DEFAULTS = {
    "companion_host": "127.0.0.1",   # the machine running Companion
    "companion_port": 16622,         # Satellite API (TCP)
    "deck_name": "RebelDeck",        # shown in Companion's Surfaces list
    "columns": 8,
    "rows": 4,
    "button_px": 144,                # picture size asked of Companion
    "web_port": 8091,                # the deck page and API
    "web_network": True,             # True: phones/tablets on the network can open the deck
    "open_window": True,             # open the deck window on this computer at start
}


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not cfg.get("device_id"):
        # a stable id, so Companion keeps the deck's page and settings between runs
        cfg["device_id"] = "rebeldeck-" + uuid.uuid4().hex[:8]
        save({"device_id": cfg["device_id"]})
    return cfg


def save(changes):
    try:
        with open(FILE) as f:
            mine = json.load(f)
    except (OSError, ValueError):
        mine = {}
    mine.update(changes)
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    with open(FILE, "w") as f:
        json.dump(mine, f, indent=2)
