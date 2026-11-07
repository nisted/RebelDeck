"""RebelDeck tests against a stand-in Companion.  python3 tests/test_rebeldeck.py"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
from fake_companion import FakeCompanion  # noqa: E402
from rebeldeck import settings  # noqa: E402


def fresh_settings(extra=None):
    d = tempfile.mkdtemp()
    settings.DIR = d
    settings.FILE = os.path.join(d, "settings.json")
    if extra is not None:
        with open(settings.FILE, "w") as f:
            json.dump(extra, f)


from rebeldeck.manager import Manager  # noqa: E402
from rebeldeck.satellite import parse  # noqa: E402
from rebeldeck.web import make_server  # noqa: E402


def wait(cond, t=6):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def call(base, method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def adds(fc):
    return [l for l in fc.lines if l.startswith("ADD-DEVICE")]


def run(png, caps):
    fc = FakeCompanion(png=png, caps=caps)
    fresh_settings({"companion_port": fc.port, "web_port": 0, "button_px": 4})
    m = Manager()
    srv = make_server(m)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    m.start()
    try:
        # one deck by default, 8x4
        assert fc.added.wait(5), "deck never registered"
        first = m.cfg["decks"][0]
        add = adds(fc)[0]
        assert "KEYS_TOTAL=32" in add and "KEYS_PER_ROW=8" in add, add
        assert f"DEVICEID={first['device_id']}" in add and "SERIAL=rebeldeck:" in add
        assert ("BITMAP_FORMAT=png" in add) == (png and caps), add
        assert wait(lambda: m.status()["status"] == "connected")

        # a button picture arrives and is served as PNG
        fc.key_rgb(first["device_id"], 3, 4)
        deck = m.deck(first["id"])
        assert wait(lambda: deck.keys[3].image is not None) and deck.keys[3].image.startswith("data:image/png;base64,")
        assert deck.keys[3].text == "GO"

        # pages
        assert "REBELDECK" in urllib.request.urlopen(base + "/gui").read().decode()
        assert "Pick a deck" in urllib.request.urlopen(base + "/").read().decode()
        page = urllib.request.urlopen(base + f"/deck/{first['id']}").read().decode()
        assert first["id"] in page and "{{" not in page
        try:
            urllib.request.urlopen(base + "/deck/nope")
            raise AssertionError("missing deck page served")
        except urllib.error.HTTPError as e:
            assert e.code == 404

        # API
        st = call(base, "GET", "/api/status")
        assert st["status"] == "connected" and len(st["decks"]) == 1
        assert any(p["columns"] == 5 and p["rows"] == 3 for p in call(base, "GET", "/api/presets"))
        state = call(base, "GET", f"/api/decks/{first['id']}/state")
        assert state["cols"] == 8 and len(state["keys"]) == 32

        # live events
        ev = urllib.request.urlopen(base + f"/api/decks/{first['id']}/events", timeout=5)
        assert ev.readline().startswith(b"event: state")
        fc.key_rgb(first["device_id"], 5, 4, rgb=(0, 255, 0), text="B")
        got = b""
        end = time.time() + 5
        while b"event: key" not in got and time.time() < end:
            got += ev.readline()
        assert b"event: key" in got

        # taps
        call(base, "POST", f"/api/decks/{first['id']}/press", {"key": 5, "pressed": True})
        assert wait(lambda: any(f"DEVICEID={first['device_id']} KEY=5 PRESSED=true" in l for l in fc.pressed))
        try:
            call(base, "POST", f"/api/decks/{first['id']}/press", {})
            raise AssertionError("bad press accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # a second deck: its own surface
        n = len(adds(fc))
        second = call(base, "POST", "/api/decks", {"name": "FOH Deck", "columns": 5, "rows": 3})
        assert wait(lambda: len(adds(fc)) == n + 1)
        assert "KEYS_TOTAL=15" in adds(fc)[-1] and 'PRODUCT_NAME="FOH Deck"' in adds(fc)[-1]
        assert wait(lambda: m.status()["status"] == "connected" and len(m.status()["decks"]) == 2)

        # resizing re-registers that deck only, and open pages are told
        ev.close()
        ev2 = urllib.request.urlopen(base + f"/api/decks/{second['id']}/events", timeout=5)
        assert ev2.readline().startswith(b"event: state")
        ev2.readline(); ev2.readline()
        n = len(adds(fc))
        call(base, "PUT", f"/api/decks/{second['id']}", {"columns": 4, "rows": 2})
        assert wait(lambda: len(adds(fc)) == n + 1) and "KEYS_TOTAL=8" in adds(fc)[-1]
        got = b""
        end = time.time() + 5
        while b'"cols": 4' not in got and time.time() < end:
            got += ev2.readline()
        assert b'"cols": 4' in got, got[-300:]
        ev2.close()
        saved = json.load(open(settings.FILE))
        assert [(d["columns"], d["rows"]) for d in saved["decks"]] == [(8, 4), (4, 2)]

        # silly sizes are kept in range
        call(base, "PUT", f"/api/decks/{second['id']}", {"columns": 999, "rows": 0})
        d2 = next(d for d in m.cfg["decks"] if d["id"] == second["id"])
        assert (d2["columns"], d2["rows"]) == (32, 1)

        # settings: checked and saved
        out = call(base, "PUT", "/api/settings", {"key_px": 120, "companion_port": 99999})
        assert out["key_px"] == 120 and out["companion_port"] == 16622    # 99999 rejected: default
        call(base, "PUT", "/api/settings", {"companion_port": fc.port})   # back to the stand-in
        assert wait(lambda: m.status()["status"] == "connected", 8)

        # removing a deck
        call(base, "DELETE", f"/api/decks/{second['id']}")
        assert len(m.cfg["decks"]) == 1 and m.deck(second["id"]) is None
        assert call(base, "GET", f"/api/decks/{second['id']}/state") == {"gone": True}

        # Companion goes away and comes back
        n = len(adds(fc))
        fc.added.clear()
        fc.drop()
        assert wait(lambda: m.status()["status"] != "connected", 5)
        assert fc.added.wait(10), "didn't reconnect"
        assert len(adds(fc)) >= n + 1
        assert wait(lambda: m.status()["status"] == "connected")
    finally:
        m.stop()
        srv.shutdown()
        fc.close()


def test_parse():
    cmd, a = parse('KEY-STATE DEVICEID=x KEY=2 TEXT=SGk= PRODUCT_NAME="A b c" EMPTY=')
    assert cmd == "KEY-STATE" and a["KEY"] == "2" and a["PRODUCT_NAME"] == "A b c" and a["EMPTY"] == ""


def test_v01_settings_move_over():
    fresh_settings({"deck_name": "Old", "columns": 5, "rows": 3, "device_id": "rebeldeck-abc12345", "web_port": 9000})
    cfg = settings.load()
    assert len(cfg["decks"]) == 1
    d = cfg["decks"][0]
    assert (d["name"], d["columns"], d["rows"], d["device_id"]) == ("Old", 5, 3, "rebeldeck-abc12345")
    saved = json.load(open(settings.FILE))
    assert "columns" not in saved and saved["web_port"] == 9000 and saved["decks"][0]["device_id"] == "rebeldeck-abc12345"


def test_password():
    fresh_settings({"web_port": 0, "web_password": "sekrit", "companion_port": 1})
    m = Manager()
    srv = make_server(m)
    # pretend every request comes from another device
    srv.RequestHandlerClass.local = lambda self: False
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for path in ("/gui", "/api/settings"):
            try:
                urllib.request.urlopen(base + path)
                raise AssertionError(f"{path} open without the password")
            except urllib.error.HTTPError as e:
                assert e.code == 401
        import base64
        req = urllib.request.Request(base + "/api/settings", headers={
            "Authorization": "Basic " + base64.b64encode(b"x:sekrit").decode()})
        assert "key_px" in json.load(urllib.request.urlopen(req))
        # decks stay open to everyone
        assert urllib.request.urlopen(base + "/").status == 200
        assert urllib.request.urlopen(base + "/api/status").status == 200
    finally:
        srv.shutdown()


if __name__ == "__main__":
    test_parse()
    test_v01_settings_move_over()
    test_password()
    print("ok: parse, v0.1 settings, password")
    for png, caps in ((True, True), (False, True), (True, False)):
        run(png, caps)
        print(f"ok: png={png} caps={caps}")
    print("all passed")
