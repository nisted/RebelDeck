"""RebelDeck: the app.

Three layers, like Companion Sync:
  tray icon   the basics: status, pop-out, web GUI, decks, update, quit
  pop-out     Companion's address and showing/hiding the deck windows
  web GUI     everything else (http://127.0.0.1:8091/gui)

  RebelDeck                 tray icon, deck windows
  RebelDeck --headless      no tray or windows (the Pi service, servers)
  RebelDeck --popup         the pop-out on its own (started by the tray)
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
def browser_candidates():
    if WIN:
        for base in (os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")):
            if base:
                yield os.path.join(base, r"Google\Chrome\Application\chrome.exe")
        # Edge last: a new profile signs itself in to the Windows account and
        # shows a sync screen once per deck window ("Got it" ends it)
        for base in (os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")):
            if base:
                yield os.path.join(base, r"Microsoft\Edge\Application\msedge.exe")
    elif MAC:
        yield "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        yield "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        yield "/Applications/Chromium.app/Contents/MacOS/Chromium"
    else:
        for name in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge"):
            p = shutil.which(name)
            if p:
                yield p


class Windows:
    """One app-mode browser window per deck, each with its own profile so it
    can be closed on its own. Closing a window by hand marks the deck hidden."""

    def __init__(self, app):
        self.app = app
        self.procs = {}         # deck id -> Popen
        self.lock = threading.Lock()
        self.browser = next((b for b in browser_candidates() if os.path.exists(b)), None)

    def showing(self):
        with self.lock:
            return {i: p.poll() is None for i, p in self.procs.items()}

    def show(self, deck_id):
        d = next((x for x in self.app.m.cfg["decks"] if x["id"] == deck_id), None)
        if not d:
            return
        with self.lock:
            p = self.procs.get(deck_id)
            if p and p.poll() is None:
                return
        url = f"http://127.0.0.1:{self.app.m.cfg['web_port']}/deck/{deck_id}?window=1"
        if not self.browser:
            webbrowser.open(url)
            return
        k = int(self.app.m.cfg["key_px"])
        w = d["columns"] * k + (d["columns"] - 1) * 8 + 28
        h = d["rows"] * k + (d["rows"] - 1) * 8 + 28
        profile = os.path.join(settings.DIR, "windows", deck_id)
        try:
            p = subprocess.Popen([self.browser, f"--app={url}", f"--window-size={w},{h}", f"--user-data-dir={profile}",
                                  "--no-first-run", "--no-default-browser-check", "--disable-sync",
                                  "--disable-features=Translate,msImplicitSignin,msEdgeSyncConsent"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env())
        except OSError as e:
            log.warning("couldn't open the %s window: %s", d["name"], e)
            webbrowser.open(url)
            return
        with self.lock:
            self.procs[deck_id] = p
        threading.Thread(target=self._watch, args=(deck_id, p), daemon=True).start()

    def _watch(self, deck_id, p):
        p.wait()
        with self.lock:
            if self.procs.get(deck_id) is not p:
                return              # replaced or closed by us
            self.procs.pop(deck_id, None)
        if not self.app.quitting:
            # closed by hand: remember it's hidden
            try:
                self.app.m.update_deck(deck_id, {"showing": False})
            except KeyError:
                pass

    def hide(self, deck_id):
        with self.lock:
            p = self.procs.pop(deck_id, None)
        if p and p.poll() is None:
            p.terminate()

    def set(self, deck_id, show):
        if show:
            # reopening after a resize: close the old window first
            self.hide(deck_id)
            self.show(deck_id)
        else:
            self.hide(deck_id)

    def close_all(self):
        for i in list(self.procs):
            self.hide(i)


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
        if not self.m.cfg.get("web_network", True):
            return ""
        return f"http://{lan_address()}:{self.m.cfg['web_port']}/"

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
            pystray.MenuItem("Open web GUI", lambda *_: self.open_gui()),
            pystray.MenuItem("Decks", pystray.Menu(lambda: deck_items())),
            pystray.MenuItem(update_text, do_update),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Quit {APP}", lambda *_: self.quit()),
        )
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
        try:
            server = make_server(self.m, self)
        except OSError as e:
            if already_running(self.m.cfg["web_port"]):
                log.info("already running: opening the web GUI")
                self.open_gui()
                return 0
            log.error("can't use port %s: %s", self.m.cfg["web_port"], e)
            return 1
        threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
        self.m.start()
        log.info("decks for phones and tablets: %s", self.lan_url() or "(off)")
        if self.windows:
            self.m.on_windows = self.windows.set
            if self.m.cfg.get("open_windows_at_start", True):
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


def main(argv):
    if "--popup" in argv:
        from .popup import main as popup_main
        return popup_main()
    setup_logging()
    headless = "--headless" in argv or (LINUX and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))
    return App(headless=headless).run()
