"""RebelDeck v0.1 tests against a stand-in Companion.  python3 tests/test_rebeldeck.py"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_companion import FakeCompanion  # noqa: E402
from rebeldeck import settings  # noqa: E402

settings.FILE = os.path.join(tempfile.mkdtemp(), "settings.json")
from rebeldeck.satellite import Deck, parse  # noqa: E402
from rebeldeck.web import make_server  # noqa: E402


def wait(cond, t=5):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def run(png, caps):
    fc = FakeCompanion(png=png, caps=caps)
    cfg = settings.load()
    cfg.update(companion_port=fc.port, web_port=0, columns=8, rows=4, button_px=4)
    deck = Deck(cfg)
    srv = make_server(cfg, deck)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deck.start()
    try:
        assert fc.added.wait(5), "deck never registered"
        add = [l for l in fc.lines if l.startswith("ADD-DEVICE")][0]
        assert "KEYS_TOTAL=32" in add and "KEYS_PER_ROW=8" in add, add
        assert ("BITMAP_FORMAT=png" in add) == (png and caps), add
        assert wait(lambda: deck.status == "connected")

        # a button arrives as raw RGB and is shown as a PNG
        fc.key_rgb(cfg["device_id"], 3, 4)
        assert wait(lambda: deck.keys[3].image is not None)
        assert deck.keys[3].image.startswith("data:image/png;base64,")
        assert deck.keys[3].text == "GO" and deck.keys[3].color == "#ff0000"

        # the page and the API
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "RebelDeck" in html
        st = json.load(urllib.request.urlopen(base + "/api/state"))
        assert st["cols"] == 8 and st["rows"] == 4 and len(st["keys"]) == 32 and st["status"] == "connected"

        # live events: the first is the full state, then key changes
        ev = urllib.request.urlopen(base + "/api/events", timeout=5)
        first = [ev.readline() for _ in range(3)]
        assert first[0].startswith(b"event: state"), first
        fc.key_rgb(cfg["device_id"], 5, 4, rgb=(0, 255, 0), text="B")
        got = b""
        end = time.time() + 5
        while b"event: key" not in got and time.time() < end:
            got += ev.readline()
        assert b"event: key" in got
        ev.close()

        # a tap goes back to Companion
        req = urllib.request.Request(base + "/api/press", data=json.dumps({"key": 5, "pressed": True}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req).read()
        assert wait(lambda: any("KEY=5 PRESSED=true" in l for l in fc.pressed)), fc.pressed
        bad = urllib.request.Request(base + "/api/press", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(bad)
            raise AssertionError("bad press accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # Companion goes away and comes back: reconnects and registers again
        n = sum(1 for l in fc.lines if l.startswith("ADD-DEVICE"))
        fc.added.clear()
        fc.drop()
        assert wait(lambda: deck.status in ("disconnected", "connecting"), 5)
        assert fc.added.wait(8), "didn't reconnect"
        assert sum(1 for l in fc.lines if l.startswith("ADD-DEVICE")) == n + 1
        assert wait(lambda: deck.status == "connected")
    finally:
        deck.stop()
        srv.shutdown()
        fc.close()


def test_parse():
    cmd, a = parse('KEY-STATE DEVICEID=x KEY=2 TEXT=SGk= PRODUCT_NAME="A b c" EMPTY=')
    assert cmd == "KEY-STATE" and a["KEY"] == "2" and a["PRODUCT_NAME"] == "A b c" and a["EMPTY"] == ""


if __name__ == "__main__":
    test_parse()
    for png, caps in ((True, True), (False, True), (True, False)):
        run(png, caps)
        print(f"ok: png={png} caps={caps}")
    print("all passed")
