"""RebelDeck: the app.

Three layers, like Rebel Sync:
  tray icon   the basics: status, pop-out, web GUI, decks, update, quit
  pop-out     Companion's address and showing/hiding the deck windows
  web GUI     everything else (http://127.0.0.1:8091/gui)

  RebelDeck                 tray icon, deck windows
  RebelDeck --headless      no tray or windows (the Pi service, servers)
  RebelDeck --popup         the pop-out on its own (started by the tray)
  RebelDeck --hotkey <deck> <key>   press a key (the Pi desktop's keybinds run this)
"""
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler

from . import __version__, settings
from .manager import Manager
from .web import make_server

APP = "RebelDeck"
REPO = "nisted/RebelDeck"
RELEASES = f"https://github.com/{REPO}/releases/latest"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
WIN, MAC = sys.platform == "win32", sys.platform == "darwin"
LINUX = not (WIN or MAC)
FROZEN = getattr(sys, "frozen", False)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # holds run.py when running from source
UPDATE_ASSET = "RebelDeck-Setup.exe" if WIN else ("RebelDeck-Mac.zip" if MAC else "RebelDeck-Pi.tar.gz")

try:
    from ._build import BUILD          # written by the GitHub build
except ImportError:
    BUILD = "dev"
LABEL = f"v{__version__} (build {BUILD})" if BUILD != "dev" else f"v{__version__} (dev)"

LOG_FILE = os.path.join(settings.DIR, "rebeldeck.log")
log = logging.getLogger("rebeldeck")


def setup_logging():
    os.makedirs(settings.DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr and not FROZEN:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def fetch(url, dest=None):
    """Download `url`: the bytes, or saved to `dest`. The Mac uses curl, which
    trusts the system's certificates."""
    if MAC:
        cmd = ["/usr/bin/curl", "-fsSL", "--max-time", "300", "-H", "User-Agent: rebeldeck", url]
        r = subprocess.run(cmd + (["-o", dest] if dest else []), capture_output=True)
        if r.returncode:
            raise OSError(r.stderr.decode(errors="replace").strip() or f"curl failed ({r.returncode})")
        return r.stdout
    req = urllib.request.Request(url, headers={"User-Agent": "rebeldeck"})
    with urllib.request.urlopen(req, timeout=300) as r:
        if not dest:
            return r.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)


def child_env():
    """Environment for programs started from the built app, without
    PyInstaller's own settings."""
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
    for k in [k for k in env if k.startswith(("_PYI", "_MEIPASS"))]:
        env.pop(k)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    return env


def self_command(*args):
    """How to start this app again (frozen or from source)."""
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, os.path.join(ROOT, "run.py"), *args]


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


# ---- start at login --------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PLIST = os.path.expanduser("~/Library/LaunchAgents/uk.co.nigelisted.rebeldeck.plist")
AUTOSTART = os.path.expanduser("~/.config/autostart/rebeldeck.desktop")


def mac_app_path():
    p = os.path.abspath(sys.executable)
    while p != "/" and not p.endswith(".app"):
        p = os.path.dirname(p)
    return p


def set_login(on):
    try:
        if WIN:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                if on:
                    winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, subprocess.list2cmdline(self_command("--login")))
                else:
                    try:
                        winreg.DeleteValue(k, APP)
                    except FileNotFoundError:
                        pass
        elif MAC:
            if on:
                os.makedirs(os.path.dirname(PLIST), exist_ok=True)
                args = ["/usr/bin/open", "-a", mac_app_path(), "--args", "--login"] if FROZEN else self_command("--login")
                items = "".join(f"<string>{a}</string>" for a in args)
                with open(PLIST, "w") as f:
                    f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>'
                            f'<key>Label</key><string>uk.co.nigelisted.rebeldeck</string><key>ProgramArguments</key>'
                            f'<array>{items}</array><key>RunAtLoad</key><true/></dict></plist>\n')
            elif os.path.exists(PLIST):
                os.remove(PLIST)
        else:
            if on:
                os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
                with open(AUTOSTART, "w") as f:
                    f.write("[Desktop Entry]\nType=Application\nName=RebelDeck\n"
                            f"Exec={subprocess.list2cmdline(self_command('--login'))}\nX-GNOME-Autostart-enabled=true\n")
            elif os.path.exists(AUTOSTART):
                os.remove(AUTOSTART)
    except OSError as e:
        log.warning("start at login: %s", e)


# ---- deck windows ------------------------------------------------------------
class Windows:
    """Deck windows: RebelDeck's own (pywebview), one small process each, so
    the app can open and close them for certain. Broken-out keys get windows
    of their own too. Closing a deck window by hand marks the deck hidden;
    closing a broken-out key's window puts the key back in the deck.
    Without pywebview (e.g. running from source), decks open in the browser."""

    def __init__(self, app):
        self.app = app
        self.procs = {}         # "id" or "id:key" -> Popen
        self.lock = threading.Lock()
        from .window import available
        self.own = available() or FROZEN
        if not self.own:
            log.info("pywebview isn't installed: deck windows open in the browser instead")
        # v0.2 opened decks in Edge/Chrome with a profile each: tidy those away
        shutil.rmtree(os.path.join(settings.DIR, "windows"), ignore_errors=True)

    def showing(self):
        with self.lock:
            return {i: p.poll() is None for i, p in self.procs.items() if ":" not in i}

    def _open(self, name, args, d):
        with self.lock:
            p = self.procs.get(name)
            if p and p.poll() is None:
                return
        try:
            p = subprocess.Popen(self_command(*args), env=child_env(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.warning("couldn't open %s: %s", d["name"], e)
            return
        log.info("opening the %s window%s", d["name"], f" (key {name.partition(':')[2]})" if ":" in name else "")
        with self.lock:
            self.procs[name] = p
        threading.Thread(target=self._watch, args=(name, p), daemon=True).start()

    def _close(self, name):
        with self.lock:
            p = self.procs.pop(name, None)
        if not p or p.poll() is not None:
            return
        if WIN:
            # the packaged .exe runs as a launcher plus the real window process:
            # close the whole tree, not just the launcher (v0.3.0 left windows open)
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                           creationflags=0x08000000)
        else:
            p.terminate()
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()

    def _watch(self, name, p):
        p.wait()
        with self.lock:
            if self.procs.get(name) is not p:
                return              # closed by us
            self.procs.pop(name, None)
        log.info("window %s ended by itself (exit %s)", name, p.returncode)
        if self.app.quitting:
            return
        deck_id, _, key = name.partition(":")
        d = self.app._deck(deck_id)
        if not d:
            return
        try:
            if key:                 # a broken-out key closed by hand: back into the deck
                self.app.m.update_deck(deck_id, {"breakout": [k for k in d["breakout"] if str(k) != key]})
            elif p.returncode == 0:  # the deck window closed by hand: remember it's hidden
                self.app.m.update_deck(deck_id, {"showing": False})
            else:
                log.warning("the %s window stopped (exit %s)", d["name"], p.returncode)
        except KeyError:
            pass

    def set(self, deck_id, show):
        """Make the windows match the deck: its own, and its broken-out keys'.
        show "reopen": close and open again (a change the open window can't make)."""
        if show == "reopen":
            self.set(deck_id, False)
            show = True
        d = self.app._deck(deck_id)
        if not show or not d:
            for name in [n for n in list(self.procs) if n == deck_id or n.startswith(deck_id + ":")]:
                self._close(name)
            return
        if not self.own:
            webbrowser.open(f"http://127.0.0.1:{self.app.m.cfg['web_port']}/deck/{d['slug']}")
            return
        self._open(deck_id, ("--deck-window", deck_id), d)
        want = {f"{deck_id}:{k}" for k in d["breakout"]}
        for name in [n for n in list(self.procs) if n.startswith(deck_id + ":") and n not in want]:
            self._close(name)
        for k in d["breakout"]:
            self._open(f"{deck_id}:{k}", ("--deck-window", deck_id, "--key", str(k)), d)

    def show(self, deck_id):
        self.set(deck_id, True)

    def close_all(self):
        for name in list(self.procs):
            self._close(name)


# ---- the app --------------------------------------------------------------------
class App:
    def __init__(self, headless=False):
        self.headless = headless
        self.quitting = False
        self.restart = False
        self.latest = None
        self.m = Manager()
        self.windows = None if headless else Windows(self)
        self.icon = None
        self.popup_proc = None
        self.stop = threading.Event()

    # hooks for the web server
    def label(self):
        return LABEL

    def lan_url(self):
        ip = lan_address()
        if not self.m.cfg.get("web_network", True) or not ip or ip.startswith("127."):
            return ""
        return f"http://{ip}:{self.m.cfg['web_port']}/"

    def identify(self, deck_id):
        """From the tray or GUI: make sure the deck's window is open (it flashes its name)."""
        d = self._deck(deck_id)
        if d and self.windows and not d["showing"]:
            self.m.set_showing(deck_id, True)

    def popup_show(self):
        if not (self.popup_proc and self.popup_proc.poll() is None):
            self.toggle_popup()

    def windows_showing(self):
        return self.windows.showing() if self.windows else {}

    def log_tail(self, n):
        try:
            with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()[-n:]
        except OSError:
            return []

    # ---- updates --------------------------------------------------------------
    def check_update(self, force=False):
        if force or not self.latest or time.time() - self.latest[0] > 600:
            try:
                rel = json.loads(fetch(LATEST_API))
                name = (rel.get("name") or "").removeprefix(APP).strip()
                self.latest = (time.time(), rel.get("tag_name", "").removeprefix("build-"), name)
            except Exception as e:
                log.info("update check: %s", e)
                self.latest = (time.time() - 540, None, None)      # try again in a minute
        return self.update_cached()

    def update_cached(self):
        new = self.latest[1] if self.latest else None
        newer = bool(new and new.isdigit() and (BUILD == "dev" or int(new) > int(BUILD)))
        label = self.latest[2] if self.latest and self.latest[2] else (f"build {new}" if new else None)
        can = FROZEN or LINUX
        return {"current": LABEL, "latest": label, "latest_build": new, "newer": newer, "can_update": can}

    def update_now(self):
        try:
            self._update()
        except Exception as e:
            log.error("update failed: %s", e)
            self.notify(f"The update didn't work: {e}")

    def _update(self):
        if not FROZEN and not LINUX:
            raise RuntimeError("running from source: update it with git")
        rel = json.loads(fetch(LATEST_API))
        new = rel.get("tag_name", "").removeprefix("build-")
        if BUILD != "dev" and new.isdigit() and int(new) <= int(BUILD):
            self.notify(f"You're on the latest, {LABEL}.")
            return
        asset = next((a for a in rel.get("assets", []) if a.get("name") == UPDATE_ASSET), None)
        if not asset:
            webbrowser.open(RELEASES)
            raise RuntimeError(f"build {new} has no {UPDATE_ASSET}")
        tmp = tempfile.mkdtemp(prefix="rebeldeck-update-")
        dest = os.path.join(tmp, UPDATE_ASSET)
        self.notify(f"Downloading build {new}…")
        fetch(asset["browser_download_url"], dest)
        log.info("update: build %s -> %s", BUILD, new)
        if WIN:
            self.quitting = True
            subprocess.Popen([dest, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], env=child_env(), close_fds=True,
                             creationflags=0x00000008 | 0x00000200)
            self.quit()
        elif MAC:
            app = mac_app_path()
            if not app.endswith(".app") or not os.access(os.path.dirname(app), os.W_OK):
                raise RuntimeError(f"can't replace {app}: move {APP} into Applications first")
            unpacked = os.path.join(tmp, "new")
            r = subprocess.run(["ditto", "-x", "-k", dest, unpacked], capture_output=True, text=True)
            new_app = os.path.join(unpacked, f"{APP}.app")
            if r.returncode or not os.path.isdir(new_app):
                raise RuntimeError("the download couldn't be unpacked")
            script = os.path.join(tmp, "swap.sh")
            with open(script, "w") as f:
                f.write('while kill -0 "$1" 2>/dev/null; do sleep 0.5; done\nrm -rf "$2" && mv "$3" "$2"\n'
                        'xattr -dr com.apple.quarantine "$2" 2>/dev/null\nopen "$2"\n')
            subprocess.Popen(["/bin/sh", script, str(os.getpid()), app, new_app], env=child_env(),
                             start_new_session=True, close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.quit()
        else:
            with tarfile.open(dest) as t:
                t.extractall(tmp, filter="data")
            installer = os.path.join(tmp, "rebeldeck", "pi", "install.sh")
            if not os.path.exists(installer):
                raise RuntimeError("the download doesn't contain the installer")
            cmd = ["/bin/bash", installer, "--from-update"]
            r = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True)
            if r.returncode and "password" in (r.stderr or "").lower() and not self.headless:
                r = subprocess.run(["pkexec", *cmd], capture_output=True, text=True)   # asks for the password
            log.info("install.sh:\n%s%s", r.stdout, r.stderr)
            if r.returncode:
                last = ((r.stdout + r.stderr).strip().splitlines() or ["no output"])[-1]
                raise RuntimeError(f"install.sh failed: {last}")
            # headless: the installer restarts the service. With a desktop: start
            # the new copy once this one has let go of the port.
            if not self.headless:
                subprocess.Popen(["/bin/sh", "-c", 'sleep 3; exec "$@"', "sh", *self_command()], env=child_env(),
                                 start_new_session=True, close_fds=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.quit()

    # ---- tray and pop-out ---------------------------------------------------------
    def notify(self, text):
        log.info(text)
        if self.icon is not None:
            try:
                self.icon.notify(text, APP)
            except Exception:
                pass

    def open_gui(self, page=""):
        # the pop-out steps aside for the web GUI
        if self.popup_proc and self.popup_proc.poll() is None:
            self.popup_proc.terminate()
            self.popup_proc = None
        webbrowser.open(f"http://127.0.0.1:{self.m.cfg['web_port']}/gui{('#' + page) if page else ''}")

    def toggle_popup(self):
        if self.popup_proc and self.popup_proc.poll() is None:
            self.popup_proc.terminate()
            self.popup_proc = None
            return
        try:
            self.popup_proc = subprocess.Popen(self_command("--popup"), env=child_env())
        except OSError as e:
            log.warning("pop-out: %s", e)
            self.open_gui()

    def tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            log.info("no tray icon (pystray or Pillow missing): running without one")
            return False
        self.icon_images = {s: self._icon(Image, ImageDraw, c) for s, c in
                            (("connected", "#50f078"), ("partly", "#ffb84d"), ("off", "#ff4d6d"), ("none", "#9aa0b4"))}

        def status_text(_=None):
            st = self.m.status()["status"]
            n = len(self.m.cfg["decks"])
            return {"connected": f"Companion: connected ({n} deck{'s' if n != 1 else ''})",
                    "partly connected": "Companion: partly connected", "no decks": "No decks yet",
                    "refused": "Companion refused a deck"}.get(st, "Companion: not connected")

        def deck_items():
            items = []
            for d in self.m.cfg["decks"]:
                items.append(pystray.MenuItem(
                    d["name"], (lambda i: lambda *_: self.m.set_showing(i, not self._deck(i)["showing"]))(d["id"]),
                    checked=(lambda i: lambda _: bool(self._deck(i) and self._deck(i)["showing"]))(d["id"])))
            return items or [pystray.MenuItem("No decks yet", None, enabled=False)]

        def identify_items():
            items = [pystray.MenuItem(d["name"], (lambda i: lambda *_: (self.identify(i), self.m.identify(i)))(d["id"]))
                     for d in self.m.cfg["decks"]]
            return items or [pystray.MenuItem("No decks yet", None, enabled=False)]

        def update_text(_=None):
            u = self.update_cached()
            return f"Update to {u['latest']}" if u["newer"] else "Check for updates"

        def do_update(*_):
            if self.update_cached()["newer"]:
                threading.Thread(target=self.update_now, daemon=True).start()
            else:
                self.open_gui("update")

        menu = pystray.Menu(
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.MenuItem("Show/hide pop-out", lambda *_: self.toggle_popup(), default=True),
            pystray.MenuItem("Visible decks", pystray.Menu(lambda: deck_items())),
            pystray.MenuItem("Identify a deck", pystray.Menu(lambda: identify_items())),
            pystray.MenuItem(update_text, do_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Quit {APP}", lambda *_: self.quit()),
            pystray.MenuItem("Open web GUI", lambda *_: self.open_gui()),     # the bottom item (Nige, 8 Nov)
        )
        if LINUX:
            # the Pi's taskbar can't draw pystray's temp-file icons: give it named PNGs
            from . import trayfix
            # where the desktop's own icon lookup looks too: the Pi taskbar checks the
            # app's folder but then loads by name from the system's icon folders only
            folder = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "icons")
            trayfix.tidy(os.path.join(settings.DIR, "tray-icons"), APP, [])     # v0.4.3's folder
            if trayfix.install(pystray, APP, folder):
                trayfix.tidy(folder, APP, self.icon_images.values())
        self.icon = pystray.Icon(APP, self.icon_images["none"], f"{APP} {LABEL}", menu)

        def refresh():
            while not self.stop.wait(2):
                st = self.m.status()["status"]
                key = {"connected": "connected", "partly connected": "partly", "no decks": "none"}.get(st, "off")
                try:
                    self.icon.icon = self.icon_images[key]
                    self.icon.title = f"{APP}: {status_text()}"
                    self.icon.update_menu()
                except Exception:
                    pass
        threading.Thread(target=refresh, daemon=True).start()
        return True

    def _deck(self, deck_id):
        return next((d for d in self.m.cfg["decks"] if d["id"] == deck_id), None)

    @staticmethod
    def _icon(Image, ImageDraw, light):
        """A purple cut-corner deck with a status light."""
        s = 64
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.polygon([(4, 4), (50, 4), (60, 14), (60, 60), (14, 60), (4, 50)], fill="#5500aa", outline="#b973ff")
        for r in range(3):
            for c in range(3):
                x, y = 13 + c * 14, 13 + r * 14
                d.rectangle((x, y, x + 9, y + 9), fill="#e8d6ff" if (r, c) != (0, 2) else light)
        return im

    # ---- run -----------------------------------------------------------------------
    def quit(self):
        if self.stop.is_set():
            return
        self.quitting = True
        self.stop.set()
        if self.popup_proc and self.popup_proc.poll() is None:
            self.popup_proc.terminate()
        if self.windows:
            self.windows.close_all()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass

    def run(self):
        log.info("%s %s starting%s", APP, LABEL, " (headless)" if self.headless else "")
        if WIN:
            # the installer waits for this to go before replacing the app
            import ctypes
            self._mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "RebelDeckRunning")
        from .rigs import LocalName, Rigs
        self.rigs = Rigs(int(self.m.cfg["web_port"]), int(self.m.cfg.get("discovery_port", 8092)),
                         enabled=bool(self.m.cfg.get("web_network", True)))
        try:
            server = make_server(self.m, self, self.rigs)
        except OSError as e:
            if already_running(self.m.cfg["web_port"]):
                log.info("already running: opening the web GUI")
                self.open_gui()
                return 0
            log.error("can't use port %s: %s", self.m.cfg["web_port"], e)
            return 1
        threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
        self.rigs.start()
        if self.m.cfg.get("web_network", True):
            LocalName().start()
        self.m.start()
        from .hotkeys import Hotkeys
        self.hotkeys = Hotkeys(self.m, lambda deck_id, key: self_command("--hotkey", deck_id, str(key)),
                               headless=self.headless)
        self.hotkeys.start()
        log.info("hotkeys: %s", self.hotkeys.backend.name)
        log.info("decks for phones and tablets: %s", self.lan_url() or "(off)")
        if self.windows:
            self.m.on_windows = self.windows.set
            if self.m.cfg.get("open_windows_at_start", True):
                log.info("reopening deck windows: %s", ", ".join(d["name"] for d in self.m.cfg["decks"] if d["showing"]) or "none")
                for d in self.m.cfg["decks"]:
                    if d["showing"]:
                        self.windows.show(d["id"])
        if not self.headless:
            set_login(bool(self.m.cfg.get("start_at_login", True)))
            self.m.listeners.append(lambda kind, *_: kind == "settings" and
                                    set_login(bool(self.m.cfg.get("start_at_login", True))))
        threading.Thread(target=self._update_watch, daemon=True).start()

        if not self.headless and self.tray():
            self.icon.run()                     # until Quit
        else:
            import signal
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    signal.signal(sig, lambda *_: self.quit())
                except ValueError:
                    pass
            while not self.stop.wait(0.5):
                pass
        self.m.stop()
        server.shutdown()
        log.info("%s stopped", APP)
        return 0

    def _update_watch(self):
        self.check_update()
        while not self.stop.wait(600):
            self.check_update()


def already_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
            return "decks" in json.load(r)
    except Exception:
        return False


def webkit_safe_mode():
    """Linux: draw deck windows without WebKitGTK's GPU paths. On a Pi the GPU
    (DMA-BUF) renderer turns the window into horizontal streaks (BD-1, 7 Nov).
    setdefault, so anyone can still turn either back on (set it to 0)."""
    if LINUX:
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
        os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")


def hotkey_press(deck, key):
    """--hotkey: ask the running RebelDeck to press a key, then go."""
    try:
        port = settings.int_setting(settings._read().get("web_port"), 1024, 65535, 8091)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/decks/{urllib.request.quote(deck)}/tap",
                                     data=json.dumps({"key": int(key)}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=3).read()
        return 0
    except Exception as e:
        print(f"RebelDeck --hotkey: {e}", file=sys.stderr)
        return 1


def main(argv):
    if argv[:1] == ["--hotkey"] and len(argv) >= 3:
        return hotkey_press(argv[1], argv[2])
    webkit_safe_mode()                  # before any window; deck windows inherit it
    if "--deck-window" in argv:
        # its own quiet log: several processes sharing one rotating log file trip over each other on Windows
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s",
                            filename=os.path.join(settings.DIR, "windows.log"))
        from .window import main as window_main
        return window_main(argv)
    if "--popup" in argv:
        from .popup import main as popup_main
        return popup_main()
    setup_logging()
    headless = "--headless" in argv or (LINUX and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))
    return App(headless=headless).run()
