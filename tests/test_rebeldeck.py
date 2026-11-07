"""RebelDeck tests against a stand-in Companion.  python3 tests/test_rebeldeck.py"""
import base64
import json
import os
import socket
import struct
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
from rebeldeck import qr, rigs, settings  # noqa: E402


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


def status_of(base, method, path, body=None):
    try:
        call(base, method, path, body)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


def adds(fc):
    return [l for l in fc.lines if l.startswith("ADD-DEVICE")]


def read_events(ev, want, t=5):
    got = b""
    end = time.time() + t
    while want not in got and time.time() < end:
        got += ev.readline()
    return got


def run(png, caps, rotary_amount=False):
    fc = FakeCompanion(png=png, caps=caps, rotary_amount=rotary_amount)
    fresh_settings({"companion_port": fc.port, "web_port": 0, "button_px": 72})   # an older file: one Companion
    m = Manager()
    rg = rigs.Rigs(0, enabled=False)
    srv = make_server(m, rigs=rg)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    m.start()
    try:
        # the old Companion address became the first server, and the default
        assert [(s["host"], s["port"]) for s in m.cfg["servers"]] == [("127.0.0.1", fc.port)]
        assert "companion_port" not in json.load(open(settings.FILE))
        # one deck by default, 8x4
        assert fc.added.wait(5), "deck never registered"
        first = m.cfg["decks"][0]
        add = adds(fc)[0]
        assert "KEYS_TOTAL=32" in add and "KEYS_PER_ROW=8" in add and "BITMAPS=72" in add, add
        assert f"DEVICEID={first['device_id']}" in add and "SERIAL=rebeldeck:" in add
        assert ("BITMAP_FORMAT=png" in add) == (png and caps), add
        assert wait(lambda: m.status()["status"] == "connected")

        # a button picture arrives and is served as PNG
        fc.key_rgb(first["device_id"], 3, 4)
        deck = m.deck(first["id"])
        assert wait(lambda: deck.keys[3].image is not None) and deck.keys[3].image.startswith("data:image/png;base64,")
        assert deck.keys[3].text == "GO"

        # pages: by address name and by id
        assert "REBELDECK" in urllib.request.urlopen(base + "/gui").read().decode()
        assert "Pick a deck" in urllib.request.urlopen(base + "/").read().decode()
        for ref in (first["slug"], first["id"]):
            page = urllib.request.urlopen(base + f"/deck/{ref}").read().decode()
            assert first["id"] in page and "{{" not in page
        assert first["slug"] == "rebeldeck"
        try:
            urllib.request.urlopen(base + "/deck/nope")
            raise AssertionError("missing deck page served")
        except urllib.error.HTTPError as e:
            assert e.code == 404

        # API
        st = call(base, "GET", "/api/status")
        assert st["status"] == "connected" and len(st["decks"]) == 1 and st["servers"][0]["default"]
        assert any(p["columns"] == 5 and p["rows"] == 3 for p in call(base, "GET", "/api/presets"))
        state = call(base, "GET", f"/api/decks/{first['slug']}/state")
        assert state["cols"] == 8 and len(state["keys"]) == 32 and state["style"]["click_mode"] == "clickable"

        # live events
        ev = urllib.request.urlopen(base + f"/api/decks/{first['id']}/events", timeout=5)
        assert ev.readline().startswith(b"event: state")
        fc.key_rgb(first["device_id"], 5, 4, rgb=(0, 255, 0), text="B")
        assert b"event: key" in read_events(ev, b"event: key")

        # taps
        call(base, "POST", f"/api/decks/{first['slug']}/press", {"key": 5, "pressed": True})
        assert wait(lambda: any(f"DEVICEID={first['device_id']} KEY=5 PRESSED=true" in l for l in fc.pressed))
        assert status_of(base, "POST", f"/api/decks/{first['id']}/press", {}) == 400

        # identify flashes open pages
        call(base, "POST", f"/api/decks/{first['slug']}/identify")
        assert b"event: identify" in read_events(ev, b"event: identify")

        # click modes: unclickable ignores everything; hybrid blocks the chosen keys
        call(base, "PUT", f"/api/decks/{first['id']}", {"click_mode": "unclickable"})
        n = len(fc.pressed)
        assert call(base, "POST", f"/api/decks/{first['id']}/press", {"key": 1, "pressed": True})["ok"] is False
        call(base, "PUT", f"/api/decks/{first['id']}", {"click_mode": "hybrid", "blocked": [1, 2, 999]})
        assert m.deck_cfg(first["id"])["blocked"] == [1, 2]
        assert call(base, "POST", f"/api/decks/{first['id']}/press", {"key": 2, "pressed": True})["ok"] is False
        assert call(base, "POST", f"/api/decks/{first['id']}/press", {"key": 3, "pressed": True})["ok"] is True
        assert wait(lambda: len(fc.pressed) == n + 1) and "KEY=3 PRESSED=true" in fc.pressed[-1]
        call(base, "PUT", f"/api/decks/{first['id']}", {"click_mode": "clickable"})

        # rotaries and faders: the deck re-registers with fader variables
        n = len(adds(fc))
        call(base, "PUT", f"/api/decks/{first['id']}", {"key_types": {"0": "rotary", "1": "fader", "99": "fader", "2": "toaster"}})
        assert m.deck_cfg(first["id"])["key_types"] == {"0": "rotary", "1": "fader"}
        assert wait(lambda: len(adds(fc)) == n + 1)
        assert wait(lambda: m.status()["status"] == "connected")
        reg = adds(fc)[-1]
        assert "VARIABLES=" in reg, reg
        vs = json.loads(base64.b64decode(parse(reg)[1]["VARIABLES"]))
        assert {v["id"]: v["type"] for v in vs} == {"fader_1": "input", "fader_1_set": "output"}
        n = len(fc.pressed)
        call(base, "POST", f"/api/decks/{first['id']}/rotate", {"key": 0, "steps": -3})
        want = 1 if rotary_amount else 3
        assert wait(lambda: len(fc.pressed) == n + want), fc.pressed[n:]
        assert ("DIRECTION=-3" if rotary_amount else "DIRECTION=-1") in fc.pressed[-1]
        n = len(fc.pressed)
        call(base, "POST", f"/api/decks/{first['id']}/fader", {"key": 1, "value": 5})
        assert wait(lambda: any("SET-VARIABLE-VALUE" in l for l in fc.pressed[n:]))
        sv = next(l for l in fc.pressed[n:] if "SET-VARIABLE-VALUE" in l)
        assert "VARIABLE=fader_1" in sv and base64.b64decode(parse(sv)[1]["VALUE"]) == b"5", sv
        assert sum("KEY-ROTATE" in l and "KEY=1 " in l for l in fc.pressed[n:]) == (1 if rotary_amount else 5)
        ev2 = urllib.request.urlopen(base + f"/api/decks/{first['id']}/events", timeout=5)
        assert ev2.readline().startswith(b"event: state")
        fc.variable(first["device_id"], "fader_1_set", 64)
        got = read_events(ev2, b'"value": 64')
        assert b"event: fader" in got and m.deck(first["id"]).faders[1] == 64, got[-200:]
        ev2.close()

        # a second deck: its own surface, and an address from its name
        ev.close()
        n = len(adds(fc))
        second = call(base, "POST", "/api/decks", {"name": "FOH Deck", "columns": 5, "rows": 3})
        assert second["slug"] == "foh-deck"
        assert wait(lambda: len(adds(fc)) == n + 1)
        assert "KEYS_TOTAL=15" in adds(fc)[-1] and 'PRODUCT_NAME="FOH Deck"' in adds(fc)[-1]
        assert urllib.request.urlopen(base + "/deck/foh-deck").status == 200
        assert wait(lambda: m.status()["status"] == "connected" and len(m.status()["decks"]) == 2)

        # names must differ (they're addresses)
        assert status_of(base, "PUT", f"/api/decks/{second['id']}", {"name": "rebeldeck"}) == 409
        assert status_of(base, "POST", "/api/decks", {"name": "FOH  deck!"}) == 400

        # resizing re-registers that deck only, and its open pages are told
        ev3 = urllib.request.urlopen(base + f"/api/decks/{second['id']}/events", timeout=5)
        assert ev3.readline().startswith(b"event: state")
        n = len(adds(fc))
        call(base, "PUT", f"/api/decks/foh-deck", {"columns": 4, "rows": 2})
        assert wait(lambda: len(adds(fc)) == n + 1) and "KEYS_TOTAL=8" in adds(fc)[-1]
        assert b'"cols": 4' in read_events(ev3, b'"cols": 4')
        ev3.close()

        # window settings save without bothering Companion
        n = len(adds(fc))
        call(base, "PUT", f"/api/decks/{second['id']}", {"float": False, "movable": False, "transparency": 40,
                                                         "colour": "#112233", "window": {"x": 10, "y": 20, "w": 400, "h": 300}})
        d2 = m.deck_cfg(second["id"])
        assert (d2["float"], d2["movable"], d2["transparency"], d2["colour"]) == (False, False, 40, "#112233")
        time.sleep(0.4)
        assert len(adds(fc)) == n, "window settings re-registered the deck"
        call(base, "PUT", f"/api/decks/{second['id']}", {"transparency": 500, "colour": "red"})
        d2 = m.deck_cfg(second["id"])
        assert (d2["transparency"], d2["colour"]) == (0, "#000000")

        # silly sizes are kept in range
        call(base, "PUT", f"/api/decks/{second['id']}", {"columns": 999, "rows": 0})
        d2 = m.deck_cfg(second["id"])
        assert (d2["columns"], d2["rows"]) == (32, 1)

        # QR codes say the deck's address
        import cv2
        import numpy as np
        raw = urllib.request.urlopen(base + f"/api/decks/{second['id']}/qr.png").read()
        im = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        text = cv2.QRCodeDetector().detectAndDecode(im)[0] or cv2.QRCodeDetectorAruco().detectAndDecode(im)[0]
        assert text.endswith("/deck/foh-deck"), text

        # several Companions: a second server, used by one deck through its switch
        fc2 = FakeCompanion(png=png, caps=caps)
        try:
            s2 = call(base, "POST", "/api/servers", {"name": "Backup", "host": "127.0.0.1", "port": fc2.port})
            assert status_of(base, "POST", "/api/servers", {"name": "No address"}) == 400
            call(base, "PUT", f"/api/decks/{second['id']}", {"own_server": True, "server": s2["id"]})
            assert fc2.added.wait(5), "deck didn't move to the second Companion"
            assert second["device_id"] in adds(fc2)[-1]
            assert status_of(base, "PUT", f"/api/decks/{second['id']}", {"server": "nope"}) == 400
            assert {s["name"]: s["decks"] for s in call(base, "GET", "/api/servers")} == {"Companion": 1, "Backup": 1}
            # make it the default: the first deck moves too
            fc2.added.clear()
            call(base, "PUT", f"/api/servers/{s2['id']}", {"default": True})
            assert wait(lambda: sum(first["device_id"] in l for l in adds(fc2)) >= 1)
            call(base, "PUT", f"/api/servers/{m.cfg['servers'][0]['id']}", {"default": True})
            # removing it sends its decks back to the default
            call(base, "DELETE", f"/api/servers/{s2['id']}")
            assert not m.deck_cfg(second["id"])["own_server"]
            assert status_of(base, "DELETE", f"/api/servers/{m.cfg['servers'][0]['id']}") == 400   # the last one stays
        finally:
            fc2.close()
        assert wait(lambda: m.status()["status"] == "connected", 10)

        # settings: checked and saved
        out = call(base, "PUT", "/api/settings", {"popup_size": 120, "web_port": 99999})
        assert out["popup_size"] == 120 and out["web_port"] == 8091       # 99999 rejected: default

        # profiles: save, change, load back (same ids, so Companion keeps their setup)
        call(base, "POST", "/api/profiles", {"name": "Show A"})
        assert [p["name"] for p in call(base, "GET", "/api/profiles")] == ["Show A"]
        call(base, "DELETE", f"/api/decks/{second['id']}")
        assert len(m.cfg["decks"]) == 1 and m.deck(second["id"]) is None
        assert call(base, "GET", f"/api/decks/{second['id']}/state") == {"gone": True}
        call(base, "POST", "/api/profiles/Show%20A/load")
        assert [d["id"] for d in m.cfg["decks"]] == [first["id"], second["id"]]
        call(base, "DELETE", "/api/profiles/Show%20A")
        assert call(base, "GET", "/api/profiles") == []

        # short addresses: /1, /2 in the Decks page's order
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(NoRedirect)
        try:
            op.open(base + "/2")
            raise AssertionError("no redirect")
        except urllib.error.HTTPError as e:
            assert e.code == 302 and e.headers["Location"] == "/deck/foh-deck", (e.code, e.headers["Location"])
        assert status_of(base, "GET", "/9") == 404
        assert [d["n"] for d in call(base, "GET", "/api/decks")] == [1, 2]

        # strip rows and gaps: Companion's free layout, wide pictures where Companion allows
        mine = lambda: [l for l in adds(fc) if second["device_id"] in l]
        n = len(mine())
        call(base, "PUT", f"/api/decks/{second['id']}", {"columns": 4, "rows": 3, "strip_ratio": 2,
                                                         "key_types": {"4": "strip", "6": "strip", "7": "strip",
                                                                       "5": "gap", "8": "rotary", "1": "strip"}})
        assert wait(lambda: len(mine()) == n + 1)
        reg = mine()[-1]
        assert "LAYOUT_MANIFEST=" in reg and "KEYS_TOTAL" not in reg, reg
        man = json.loads(base64.b64decode(parse(reg)[1]["LAYOUT_MANIFEST"]))
        assert "1/1" not in man["controls"] and len(man["controls"]) == 11
        assert man["controls"]["1/0"]["stylePreset"] == "strip" and "stylePreset" not in man["controls"]["0/0"]
        assert man["controls"]["0/1"]["stylePreset"] == "strip"            # one strip button in a normal row
        px = man["stylePresets"]["default"]["bitmap"]["w"]
        sh = px // 2 if caps else px        # wide pictures only where Companion says it can (NONSQUARE)
        assert man["stylePresets"]["strip"]["bitmap"] == {"w": px, "h": sh}
        assert wait(lambda: m.deck(second["id"]).status == "connected")
        n = len(fc.pressed)
        call(base, "POST", f"/api/decks/{second['id']}/press", {"key": 6, "pressed": True})
        call(base, "POST", f"/api/decks/{second['id']}/press", {"key": 5, "pressed": True})       # a gap: nothing
        call(base, "POST", f"/api/decks/{second['id']}/rotate", {"key": 8, "steps": 1})
        assert wait(lambda: len(fc.pressed) == n + 2), fc.pressed[n:]
        assert 'CONTROLID="1/2" PRESSED=true' in fc.pressed[n] and 'CONTROLID="2/0" DIRECTION=1' in fc.pressed[n + 1], fc.pressed[n:]
        fc.key_ctl(second["device_id"], "1/2", px, sh, location="60/7/3")
        dk = m.deck(second["id"])
        assert wait(lambda: dk.keys[6].image is not None) and dk.keys[6].location == "60/7/3"
        assert call(base, "GET", "/api/decks")[1]["locations"][6] == "60/7/3"
        presets = {p["name"]: p for p in call(base, "GET", "/api/presets")}
        xl = presets["+ XL"]
        assert (xl["columns"], xl["rows"]) == (9, 6)
        assert sorted(int(k) for k, v in xl["key_types"].items() if v == "strip") == [36, 38, 39, 41, 42, 44]
        assert sorted(int(k) for k, v in xl["key_types"].items() if v == "gap") == [37, 40, 43, 46, 49, 52]
        assert sum(v == "rotary" for v in xl["key_types"].values()) == 6
        plus = presets["+ (Plus)"]
        assert (plus["columns"], plus["rows"]) == (4, 4)
        assert sorted(int(k) for k, v in plus["key_types"].items() if v == "strip") == [8, 9, 10, 11]
        call(base, "PUT", f"/api/decks/{second['id']}", {"columns": 5, "rows": 3, "key_types": {}})
        assert wait(lambda: "KEYS_TOTAL=15" in mine()[-1]) and "LAYOUT_MANIFEST" not in mine()[-1]

        # finding Companions: this one answers with its version
        from rebeldeck.rigs import probe_companion
        assert probe_companion("127.0.0.1", fc.port) == "5.1.2"
        assert probe_companion("127.0.0.1", 1) is None

        # this rig on the pick-a-deck list
        rl = call(base, "GET", "/api/rigs")
        assert rl[0]["here"] and {d["slug"] for d in rl[0]["decks"]} == {"rebeldeck", "foh-deck"}

        # Companion goes away and comes back
        assert wait(lambda: m.status()["status"] == "connected", 10)
        fc.added.clear()
        fc.drop()
        assert wait(lambda: m.status()["status"] != "connected", 5)
        assert fc.added.wait(10), "didn't reconnect"
        assert wait(lambda: m.status()["status"] == "connected", 10)
    finally:
        m.stop()
        srv.shutdown()
        fc.close()


def test_parse():
    cmd, a = parse('KEY-STATE DEVICEID=x KEY=2 TEXT=SGk= PRODUCT_NAME="A b c" EMPTY=')
    assert cmd == "KEY-STATE" and a["KEY"] == "2" and a["PRODUCT_NAME"] == "A b c" and a["EMPTY"] == ""


def test_old_settings_move_over():
    # v0.1: one deck at the top level
    fresh_settings({"deck_name": "Old", "columns": 5, "rows": 3, "device_id": "rebeldeck-abc12345", "web_port": 9000,
                    "companion_host": "10.0.0.5", "key_px": 120})
    cfg = settings.load()
    assert len(cfg["decks"]) == 1
    d = cfg["decks"][0]
    assert (d["name"], d["columns"], d["rows"], d["device_id"], d["slug"]) == ("Old", 5, 3, "rebeldeck-abc12345", "old")
    assert [(s["host"], s["port"]) for s in cfg["servers"]] == [("10.0.0.5", 16622)]
    saved = json.load(open(settings.FILE))
    assert "columns" not in saved and "companion_host" not in saved and "key_px" not in saved
    assert saved["web_port"] == 9000 and saved["decks"][0]["device_id"] == "rebeldeck-abc12345"
    # v0.4.0 strip rows become strip buttons (gaps stay gaps)
    fresh_settings({"decks": [{"id": "a", "device_id": "x-a", "name": "P", "columns": 3, "rows": 2, "strip_rows": [1],
                               "key_types": {"4": "gap"}}]})
    d = settings.load()["decks"][0]
    assert d["key_types"] == {"3": "strip", "4": "gap", "5": "strip"} and "strip_rows" not in d, d
    # repeated names get a number
    fresh_settings({"decks": [{"id": "a", "device_id": "x-a", "name": "Main"}, {"id": "b", "device_id": "x-b", "name": "main"}]})
    assert [d["name"] for d in settings.load()["decks"]] == ["Main", "main 2"]


def test_password():
    fresh_settings({"web_port": 0, "web_password": "sekrit", "companion_port": 1})
    m = Manager()
    srv = make_server(m)
    srv.RequestHandlerClass.local = lambda self: False      # pretend every request comes from another device
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for path in ("/gui", "/api/settings"):
            try:
                urllib.request.urlopen(base + path)
                raise AssertionError(f"{path} open without the password")
            except urllib.error.HTTPError as e:
                assert e.code == 401
        assert status_of(base, "POST", "/api/servers", {"host": "x"}) == 401
        req = urllib.request.Request(base + "/api/settings", headers={
            "Authorization": "Basic " + base64.b64encode(b"x:sekrit").decode()})
        assert "popup_size" in json.load(urllib.request.urlopen(req))
        # decks stay open to everyone
        assert urllib.request.urlopen(base + "/").status == 200
        assert urllib.request.urlopen(base + "/api/status").status == 200
    finally:
        srv.shutdown()


def test_qr():
    import cv2
    import numpy as np
    for t in ("http://192.168.1.20:8091/deck/foh-mini", "x" * 150):
        im = cv2.imdecode(np.frombuffer(qr.png(t, scale=6), np.uint8), cv2.IMREAD_GRAYSCALE)
        got = cv2.QRCodeDetector().detectAndDecode(im)[0] or cv2.QRCodeDetectorAruco().detectAndDecode(im)[0]
        assert got == t, (len(t), got)


def test_mdns_answer():
    # a one-off lookup for this computer's .local name
    name = "k2so.local"
    q = struct.pack(">HHHHHH", 0x1234, 0, 1, 0, 0, 0) + b"\x04k2so\x05local\x00" + struct.pack(">HH", 1, 1)
    r = rigs.mdns_answer(q, name, "192.168.1.20", direct=True)
    assert r[:2] == b"\x12\x34" and r.endswith(socket.inet_aton("192.168.1.20"))
    assert rigs.mdns_answer(q, "other.local", "192.168.1.20") is None
    r = rigs.mdns_answer(q, name, "192.168.1.20")
    assert r[:2] == b"\x00\x00" and struct.unpack(">H", r[6:8])[0] == 1


def test_rigs_hear_each_other():
    port = 18000 + os.getpid() % 1000
    a, b = rigs.Rigs(8091, port), rigs.Rigs(8095, port)
    a.start()
    b.start()
    try:
        assert wait(lambda: any(p["port"] == 8095 for p in a.peers.values()), 8), a.peers
    finally:
        a.stop.set()
        b.stop.set()


if __name__ == "__main__":
    test_parse()
    test_old_settings_move_over()
    test_password()
    test_qr()
    test_mdns_answer()
    test_rigs_hear_each_other()
    print("ok: parse, old settings, password, QR, .local, rigs")
    for png, caps, ra in ((True, True, True), (False, True, False), (True, False, False)):
        run(png, caps, ra)
        print(f"ok: png={png} caps={caps} rotary_amount={ra}")
    print("all passed")
