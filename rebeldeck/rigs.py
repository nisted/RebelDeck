"""Finding RebelDecks on the network.

- Every RebelDeck says hello by UDP broadcast every few seconds (port 8092 by
  default), and listens for the others, so any one of them can list the
  decks on all of them ("one page for every rig").
- On Windows, it also answers ".local" name lookups for this computer
  (mDNS), so phones can use http://<computer>.local:8091/. Macs and Pis
  already do that themselves.
"""
import json
import logging
import socket
import struct
import sys
import threading
import time
import urllib.request

log = logging.getLogger("rebeldeck.rigs")
HELLO_EVERY = 5
FORGET_AFTER = 20


def hostname():
    return socket.gethostname().split(".")[0]


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


class Rigs:
    def __init__(self, web_port, udp_port=8092, enabled=True):
        self.web_port, self.udp_port, self.enabled = web_port, udp_port, enabled
        self.me = f"{hostname()}:{web_port}"
        self.peers = {}               # "ip:port" -> {"name", "ip", "port", "seen", "decks", "fetched"}
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def start(self):
        if not self.enabled:
            return
        threading.Thread(target=self._listen, name="rigs-listen", daemon=True).start()
        threading.Thread(target=self._hello, name="rigs-hello", daemon=True).start()

    def _hello(self):
        msg = json.dumps({"rebeldeck": 1, "name": hostname(), "port": self.web_port}).encode()
        while not self.stop.is_set():
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.sendto(msg, ("255.255.255.255", self.udp_port))
            except OSError as e:
                log.debug("hello: %s", e)
            self.stop.wait(HELLO_EVERY)

    def _listen(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            s.bind(("", self.udp_port))
            s.settimeout(1)
        except OSError as e:
            log.info("not listening for other rigs: %s", e)
            return
        mine = {lan_address(), "127.0.0.1"}
        while not self.stop.is_set():
            try:
                data, (ip, _) = s.recvfrom(2048)
                m = json.loads(data)
            except socket.timeout:
                continue
            except (OSError, ValueError):
                continue
            if not isinstance(m, dict) or m.get("rebeldeck") != 1:
                continue
            port = int(m.get("port", 8091))
            if ip in mine and port == self.web_port:
                continue
            key = f"{ip}:{port}"
            with self.lock:
                p = self.peers.setdefault(key, {"ip": ip, "port": port, "decks": [], "fetched": 0})
                p["name"] = str(m.get("name", ip))[:40]
                p["seen"] = time.time()

    def others(self):
        """The other rigs heard lately, with their decks (fetched at most every 10 s)."""
        now = time.time()
        with self.lock:
            for k in [k for k, p in self.peers.items() if now - p["seen"] > FORGET_AFTER]:
                del self.peers[k]
            peers = [dict(p) for p in self.peers.values()]
        out = []
        for p in peers:
            if now - p["fetched"] > 10:
                try:
                    url = f"http://{p['ip']}:{p['port']}/api/status"
                    with urllib.request.urlopen(url, timeout=1.5) as r:
                        st = json.load(r)
                    p["decks"] = [{"name": d["name"], "slug": d.get("slug", d["id"]), "n": d.get("n"), "columns": d["columns"],
                                   "rows": d["rows"], "status": d["status"]} for d in st.get("decks", [])]
                except Exception:
                    pass
                p["fetched"] = now
                with self.lock:
                    if f"{p['ip']}:{p['port']}" in self.peers:
                        self.peers[f"{p['ip']}:{p['port']}"].update(decks=p["decks"], fetched=now)
            out.append({"name": p["name"], "url": f"http://{p['ip']}:{p['port']}", "decks": p["decks"]})
        return sorted(out, key=lambda p: p["name"].lower())


# ---- finding Companions -----------------------------------------------------------
def probe_companion(host, port=16622, timeout=0.6):
    """Companion's version if a Companion answers on host:port (its Satellite
    port sends "BEGIN CompanionVersion=..." straight away), else None."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            data = b""
            while b"\n" not in data and len(data) < 512:
                chunk = s.recv(512)
                if not chunk:
                    break
                data += chunk
            try:
                s.sendall(b"QUIT\n")
            except OSError:
                pass
    except OSError:
        return None
    line = data.split(b"\n", 1)[0].decode(errors="replace")
    if not line.startswith("BEGIN"):
        return None
    for part in line.split():
        if part.startswith("CompanionVersion="):
            return part.split("=", 1)[1]
    return "?"


def scan_companions(port=16622, extra=()):
    """Every Companion on this computer and on this computer's local network
    (its /24), with their versions. Takes a few seconds."""
    from concurrent.futures import ThreadPoolExecutor
    hosts = ["127.0.0.1"]
    ip = lan_address()
    if ip and not ip.startswith("127."):
        base = ip.rsplit(".", 1)[0]
        hosts += [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" != ip]
        hosts.append(ip)
    hosts += [h for h in extra if h not in hosts]
    with ThreadPoolExecutor(max_workers=64) as pool:
        found = list(zip(hosts, pool.map(lambda h: probe_companion(h, port), hosts)))
    out = []
    seen_here = False
    for host, ver in found:
        if ver is None:
            continue
        here = host in ("127.0.0.1", ip)
        if here and seen_here:
            continue                      # this computer, found twice
        seen_here = seen_here or here
        out.append({"host": "127.0.0.1" if here else host, "port": port, "version": ver,
                    "name": "This computer" if here else host})
    # computer names, where the network knows them (never waiting long)
    others = [f for f in out if f["host"] != "127.0.0.1"]
    if others:
        from concurrent.futures import ThreadPoolExecutor, wait
        pool = ThreadPoolExecutor(max_workers=16)
        futs = {pool.submit(socket.gethostbyaddr, f["host"]): f for f in others}
        done, _ = wait(futs, timeout=1.5)
        for fut in done:
            try:
                futs[fut]["name"] = fut.result()[0].split(".")[0]
            except OSError:
                pass
        pool.shutdown(wait=False)
    return out


# ---- .local names on Windows (mDNS answers for this computer's name only) -------
MDNS_GROUP, MDNS_PORT = "224.0.0.251", 5353


def _qname(data, i):
    labels = []
    while i < len(data):
        n = data[i]
        if n == 0:
            return ".".join(labels), i + 1
        if n & 0xC0 == 0xC0:
            name, _ = _qname(data, ((n & 0x3F) << 8) | data[i + 1])
            labels.append(name)
            return ".".join(labels), i + 2
        labels.append(data[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += 1 + n
    raise ValueError("bad name")


def mdns_answer(query, name, ip, direct=False):
    """The reply packet if query asks for name's address (A record), else None.
    direct: a one-off lookup (not from port 5353), which wants its id and question back."""
    if len(query) < 12:
        return None
    qid, flags, qd = struct.unpack(">HHH", query[:6])
    if flags & 0x8000 or not qd:
        return None
    i = 12
    for _ in range(qd):
        q, i = _qname(query, i)
        qtype, qclass = struct.unpack(">HH", query[i:i + 4])
        i += 4
        if q.lower() == name.lower() and qtype in (1, 255):
            rname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
            if direct:
                rr = rname + struct.pack(">HHIH", 1, 1, 10, 4) + socket.inet_aton(ip)
                question = rname + struct.pack(">HH", 1, 1)
                return struct.pack(">HHHHHH", qid, 0x8400, 1, 1, 0, 0) + question + rr
            rr = rname + struct.pack(">HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(ip)
            return struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + rr
    return None


class LocalName:
    """Answers "<computer>.local" lookups (Windows only: others do it themselves)."""

    def __init__(self, enabled=None):
        self.enabled = sys.platform == "win32" if enabled is None else enabled
        self.name = hostname() + ".local"
        self.stop = threading.Event()

    def start(self):
        if self.enabled:
            threading.Thread(target=self._run, name="mdns", daemon=True).start()

    def _run(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", MDNS_PORT))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton(MDNS_GROUP) + socket.inet_aton("0.0.0.0"))
            s.settimeout(1)
        except OSError as e:
            log.info(".local name not answered: %s", e)
            return
        while not self.stop.is_set():
            try:
                data, addr = s.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                continue
            ip = lan_address()
            direct = addr[1] != MDNS_PORT
            try:
                reply = mdns_answer(data, self.name, ip, direct) if ip else None
            except (ValueError, struct.error, IndexError):
                reply = None
            if reply:
                try:
                    s.sendto(reply, addr if direct else (MDNS_GROUP, MDNS_PORT))
                except OSError:
                    pass
