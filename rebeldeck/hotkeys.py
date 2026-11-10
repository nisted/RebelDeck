"""Hotkeys: a keyboard shortcut per key that presses it, from anywhere, even
when no deck window is in front.

Hotkeys are kept as text, "Ctrl+Alt+Shift+Super+K" (modifiers in that order;
Super is the Windows key on a PC and Cmd on a Mac). Each needs Ctrl, Alt or
Super, unless it's an F key, so ordinary typing is never swallowed.

How each computer does it:
  Windows   RegisterHotKey, on a thread of its own with a message loop
  Mac       Carbon's RegisterEventHotKey (no Accessibility permission needed),
            on the main thread, where the tray's run loop delivers them
  Pi        labwc (the Pi's Wayland desktop) won't let apps grab keys, so
            RebelDeck writes keybinds into ~/.config/labwc/rc.xml that run
            "RebelDeck --hotkey <deck> <key>", and asks labwc to reread it
  other Linux desktops: not yet (listed as a problem on each hotkey)
"""
import logging
import os
import re
import shutil
import subprocess
import sys
import threading

log = logging.getLogger("rebeldeck.hotkeys")
WIN, MAC = sys.platform == "win32", sys.platform == "darwin"

MODS = ("Ctrl", "Alt", "Shift", "Super")
_MOD_ALIASES = {"ctrl": "Ctrl", "control": "Ctrl", "alt": "Alt", "option": "Alt", "opt": "Alt",
                "shift": "Shift", "super": "Super", "win": "Super", "windows": "Super", "cmd": "Super",
                "command": "Super", "meta": "Super"}
NAV = ("Up", "Down", "Left", "Right", "Space", "Home", "End", "PageUp", "PageDown", "Insert", "Delete")
KEYS = ([chr(c) for c in range(ord("A"), ord("Z") + 1)] + [str(n) for n in range(10)] +
        [f"F{n}" for n in range(1, 25)] + [f"Num{n}" for n in range(10)] + list(NAV))
_KEY_LOOKUP = {k.lower(): k for k in KEYS}
_KEY_LOOKUP.update({"pgup": "PageUp", "pgdn": "PageDown", "del": "Delete", "ins": "Insert", "spacebar": "Space"})


def split(text):
    """'ctrl + alt + k' -> (('Ctrl', 'Alt'), 'K'), or None if it isn't a usable hotkey."""
    if not isinstance(text, str) or not text.strip():
        return None
    parts = [p.strip() for p in re.split(r"\s*\+\s*", text.strip()) if p.strip()]
    if not parts:
        return None
    mods, key = set(), None
    for p in parts:
        m = _MOD_ALIASES.get(p.lower())
        if m:
            mods.add(m)
        elif key is None and p.lower() in _KEY_LOOKUP:
            key = _KEY_LOOKUP[p.lower()]
        else:
            return None
    if key is None:
        return None
    if not (mods & {"Ctrl", "Alt", "Super"}) and not re.fullmatch(r"F\d+", key):
        return None                         # a bare letter, or Shift+letter, is just typing
    return tuple(m for m in MODS if m in mods), key


def normalise(text):
    s = split(text)
    return "+".join((*s[0], s[1])) if s else None


# ---- Windows ----------------------------------------------------------------------
_VK = {**{chr(c): c for c in range(ord("A"), ord("Z") + 1)}, **{str(n): 0x30 + n for n in range(10)},
       **{f"F{n}": 0x6F + n for n in range(1, 25)}, **{f"Num{n}": 0x60 + n for n in range(10)},
       "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27, "Space": 0x20, "Home": 0x24, "End": 0x23,
       "PageUp": 0x21, "PageDown": 0x22, "Insert": 0x2D, "Delete": 0x2E}
_WIN_MOD = {"Alt": 0x1, "Ctrl": 0x2, "Shift": 0x4, "Super": 0x8}


class WindowsKeys:
    name = "windows"
    WM_HOTKEY, WM_APP = 0x0312, 0x8001

    def __init__(self, fire):
        self.fire = fire
        self.want, self.ids, self.problems = {}, {}, {}
        self.ready, self.done = threading.Event(), threading.Event()
        self.tid = None
        threading.Thread(target=self._run, name="hotkeys", daemon=True).start()
        self.ready.wait(3)

    def _run(self):
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)      # gives this thread a message queue
        self.tid = kernel32.GetCurrentThreadId()
        self.ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                hk = self.ids.get(int(msg.wParam))
                if hk:
                    threading.Thread(target=self.fire, args=(hk,), daemon=True).start()
            elif msg.message == self.WM_APP:
                self._register(user32, ctypes)
                self.done.set()

    def _register(self, user32, ctypes):
        for i in list(self.ids):
            user32.UnregisterHotKey(None, i)
        self.ids, self.problems = {}, {}
        for n, hk in enumerate(sorted(self.want), 1):
            mods, key = split(hk)
            flags = sum(_WIN_MOD[m] for m in mods) | 0x4000           # MOD_NOREPEAT: holding it down presses once
            if user32.RegisterHotKey(None, n, flags, _VK[key]):
                self.ids[n] = hk
            else:
                self.problems[hk] = "another program already uses this shortcut"

    def apply(self, want):
        if not self.tid:
            return {hk: "hotkeys didn't start on this computer" for hk in want}
        import ctypes
        self.want = dict(want)
        self.done.clear()
        ctypes.windll.user32.PostThreadMessageW(self.tid, self.WM_APP, 0, 0)
        if not self.done.wait(3):
            return {hk: "hotkeys didn't answer in time" for hk in want}
        return dict(self.problems)


# ---- Mac ----------------------------------------------------------------------------
_MAC_KEY = {"A": 0x00, "S": 0x01, "D": 0x02, "F": 0x03, "H": 0x04, "G": 0x05, "Z": 0x06, "X": 0x07, "C": 0x08,
            "V": 0x09, "B": 0x0B, "Q": 0x0C, "W": 0x0D, "E": 0x0E, "R": 0x0F, "Y": 0x10, "T": 0x11, "1": 0x12,
            "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19, "7": 0x1A, "8": 0x1C, "0": 0x1D,
            "O": 0x1F, "U": 0x20, "I": 0x22, "P": 0x23, "L": 0x25, "J": 0x26, "K": 0x28, "N": 0x2D, "M": 0x2E,
            "Space": 0x31, "Delete": 0x75, "Home": 0x73, "End": 0x77, "PageUp": 0x74, "PageDown": 0x79,
            "Left": 0x7B, "Right": 0x7C, "Down": 0x7D, "Up": 0x7E, "Insert": 0x72,
            "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61, "F7": 0x62, "F8": 0x64,
            "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F, "F13": 0x69, "F14": 0x6B, "F15": 0x71,
            "F16": 0x6A, "F17": 0x40, "F18": 0x4F, "F19": 0x50, "F20": 0x5A,
            "Num0": 0x52, "Num1": 0x53, "Num2": 0x54, "Num3": 0x55, "Num4": 0x56, "Num5": 0x57, "Num6": 0x58,
            "Num7": 0x59, "Num8": 0x5B, "Num9": 0x5C}
_MAC_MOD = {"Super": 0x100, "Shift": 0x200, "Alt": 0x800, "Ctrl": 0x1000}


def _fourcc(s):
    return int.from_bytes(s.encode(), "big")


class MacKeys:
    """Untested on a real Mac so far (no Mac here): built from Apple's Carbon docs."""
    name = "mac"

    def __init__(self, fire):
        import ctypes
        self.ctypes, self.fire = ctypes, fire
        self.c = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
        self.c.GetApplicationEventTarget.restype = ctypes.c_void_p
        self.c.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                             ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]

        class HotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        class TypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

        self.HotKeyID = HotKeyID
        self.c.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, HotKeyID, ctypes.c_void_p,
                                               ctypes.c_uint32, ctypes.c_void_p]
        self.c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        self.refs, self.ids = {}, {}
        self.sig = _fourcc("RDhk")
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        self._handler = handler_type(self._on_event)                     # kept, or ctypes frees it
        self._spec = TypeSpec(_fourcc("keyb"), 5)                        # kEventClassKeyboard, kEventHotKeyPressed
        self._installed = False
        try:
            from PyObjCTools import AppHelper
            self.on_main = AppHelper.callAfter
        except ImportError:
            self.on_main = lambda fn, *a: fn(*a)

    def _install(self):
        if not self._installed:
            ref = self.ctypes.c_void_p()
            self.c.InstallEventHandler(self.ctypes.c_void_p(self.c.GetApplicationEventTarget()), self._handler, 1,
                                       self.ctypes.byref(self._spec), None, self.ctypes.byref(ref))
            self._installed = True

    def _on_event(self, _call, event, _data):
        hk = self.HotKeyID()
        err = self.c.GetEventParameter(event, _fourcc("----"), _fourcc("hkid"), None,
                                       self.ctypes.sizeof(hk), None, self.ctypes.byref(hk))
        name = self.ids.get(hk.id) if not err else None
        if name:
            threading.Thread(target=self.fire, args=(name,), daemon=True).start()
        return 0

    def _register(self, want, out, done):
        try:
            self._install()
            for ref in self.refs.values():
                self.c.UnregisterEventHotKey(ref)
            self.refs, self.ids = {}, {}
            target = self.ctypes.c_void_p(self.c.GetApplicationEventTarget())
            for n, hk in enumerate(sorted(want), 1):
                mods, key = split(hk)
                if key not in _MAC_KEY:
                    out[hk] = f"Macs have no {key} key"
                    continue
                ref = self.ctypes.c_void_p()
                err = self.c.RegisterEventHotKey(_MAC_KEY[key], sum(_MAC_MOD[m] for m in mods),
                                                 self.HotKeyID(self.sig, n), target, 0, self.ctypes.byref(ref))
                if err:
                    out[hk] = "another program already uses this shortcut"
                else:
                    self.refs[n], self.ids[n] = ref, hk
        except Exception as e:
            log.exception("hotkeys")
            for hk in want:
                out.setdefault(hk, f"couldn't set hotkeys: {e}")
        done.set()

    def apply(self, want):
        out, done = {}, threading.Event()
        self.on_main(self._register, dict(want), out, done)
        # the tray's run loop does the work; it may not be running yet at startup
        return dict(out) if done.wait(3) else {}


# ---- Pi (labwc) --------------------------------------------------------------------------
_LABWC_KEY = {**{chr(c): chr(c).lower() for c in range(ord("A"), ord("Z") + 1)}, **{str(n): str(n) for n in range(10)},
              **{f"F{n}": f"F{n}" for n in range(1, 25)}, **{f"Num{n}": f"KP_{n}" for n in range(10)},
              "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right", "Space": "space", "Home": "Home",
              "End": "End", "PageUp": "Prior", "PageDown": "Next", "Insert": "Insert", "Delete": "Delete"}
_LABWC_MOD = {"Ctrl": "C", "Alt": "A", "Shift": "S", "Super": "W"}
BEGIN, END = "<!-- RebelDeck hotkeys: written by RebelDeck, changes here are replaced -->", "<!-- /RebelDeck hotkeys -->"


def labwc_name(hk):
    mods, key = split(hk)
    return "-".join([_LABWC_MOD[m] for m in mods] + [_LABWC_KEY[key]])


def _xml(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def labwc_block(binds):
    """binds: {hotkey: [command, args…]} -> the lines RebelDeck keeps in rc.xml."""
    if not binds:
        return ""
    lines = [f"    {BEGIN}"]
    if binds.get("__default__"):
        lines.append("    <default />")
    for hk in sorted(k for k in binds if k != "__default__"):
        cmd = " ".join(_shell_quote(a) for a in binds[hk])          # labwc splits it as a shell would
        lines.append(f'    <keybind key="{labwc_name(hk)}"><action name="Execute" command="{_xml(cmd)}" /></keybind>')
    lines.append(f"    {END}")
    return "\n".join(lines) + "\n"


def _shell_quote(a):
    return a if re.fullmatch(r"[\w@%+=:,./-]+", a) else "'" + a.replace("'", "'\"'\"'") + "'"


def labwc_text(text, binds):
    """rc.xml with RebelDeck's keybinds replaced by `binds` (none: taken out)."""
    i, j = text.find(BEGIN), text.find(END)
    if i >= 0 and j > i:
        start = text.rfind("\n", 0, i) + 1
        stop = text.find("\n", j)
        text = text[:start] + text[(stop + 1 if stop >= 0 else len(text)):]
    if not binds:
        return text
    binds = dict(binds)
    # labwc only loads its own keybinds (Alt+Tab and the rest) when rc.xml has none:
    # if RebelDeck's are the first, ask for the defaults too
    binds["__default__"] = "<keybind" not in text and "<default" not in text
    block = labwc_block(binds)
    k = text.find("<keyboard>")
    if k < 0 and re.search(r"<keyboard\s*/>", text):
        text = re.sub(r"<keyboard\s*/>", "<keyboard>\n  </keyboard>", text, count=1)
        k = text.find("<keyboard>")
    if k >= 0:
        k = text.index(">", k) + 1
        return text[:k] + "\n" + block + text[k:].lstrip("\n")
    for close in ("</labwc_config>", "</openbox_config>"):
        c = text.rfind(close)
        if c >= 0:
            return text[:c] + "  <keyboard>\n" + block + "  </keyboard>\n" + text[c:]
    raise ValueError("rc.xml has no <labwc_config> to add keybinds to")


class LabwcKeys:
    name = "labwc"
    SYSTEM = "/etc/xdg/labwc/rc.xml"
    EMPTY = '<?xml version="1.0"?>\n<labwc_config>\n</labwc_config>\n'

    def __init__(self, command, home=None):
        self.command = command                  # fn(deck_id, key) -> [program, args…]
        self.path = os.path.join(home or os.path.expanduser("~"), ".config", "labwc", "rc.xml")

    @staticmethod
    def here():
        return not (WIN or MAC) and (bool(shutil.which("labwc")) or os.path.isdir(os.path.expanduser("~/.config/labwc")))

    def apply(self, want):
        binds = {hk: self.command(*where) for hk, where in want.items()}
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    text = f.read()
            elif not binds:
                return {}
            elif os.path.exists(self.SYSTEM):
                with open(self.SYSTEM, encoding="utf-8") as f:
                    text = f.read()
            else:
                text = self.EMPTY
            new = labwc_text(text, binds)
            if new == text:
                return {}
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new)
            os.replace(tmp, self.path)
            log.info("hotkeys: %d written to %s", len(binds), self.path)
        except (OSError, ValueError) as e:
            log.warning("hotkeys: %s", e)
            return {hk: f"couldn't write labwc's settings: {e}" for hk in want}
        self.reconfigure()
        return {}

    @staticmethod
    def reconfigure():
        """Ask labwc to reread rc.xml."""
        try:
            if os.environ.get("LABWC_PID") and shutil.which("labwc"):
                subprocess.run(["labwc", "--reconfigure"], capture_output=True, timeout=5)
            else:
                subprocess.run(["pkill", "-HUP", "-u", str(os.getuid()), "-x", "labwc"], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            log.info("hotkeys: couldn't ask labwc to reread its settings: %s", e)


class NoKeys:
    name = "none"

    def apply(self, want):
        return {hk: "hotkeys need Windows, a Mac, or the Pi's desktop (labwc)" for hk in want}


NOTES = {"windows": "Hotkeys work from anywhere on this computer.",
         "mac": "Hotkeys work from anywhere on this Mac (not yet tried on a real Mac).",
         "labwc": "Hotkeys are keybinds in the Pi desktop's settings (labwc's rc.xml); RebelDeck keeps them up to date.",
         "none": "This computer's desktop can't do hotkeys yet: they need Windows, a Mac, or the Pi's desktop (labwc)."}


class Hotkeys:
    """Keeps the computer's hotkeys matching the decks' settings."""

    def __init__(self, manager, command, headless=False):
        self.m = manager
        self.backend = NoKeys()
        try:
            if WIN:
                self.backend = WindowsKeys(self.fire)
            elif MAC and not headless:
                self.backend = MacKeys(self.fire)
            elif LabwcKeys.here():
                self.backend = LabwcKeys(command)
        except Exception as e:
            log.warning("hotkeys: %s", e)
        self.want, self.applied = {}, None
        self.wake = threading.Event()
        manager.hotkeys_info = {"backend": self.backend.name, "note": NOTES[self.backend.name],
                                "platform": "windows" if WIN else "mac" if MAC else "linux"}
        manager.listeners.append(lambda kind, *_: kind == "decks" and self.wake.set())

    def start(self):
        threading.Thread(target=self._loop, name="hotkeys-sync", daemon=True).start()
        self.wake.set()

    def wanted(self):
        out = {}
        for d in self.m.cfg["decks"]:
            for k, hk in d.get("hotkeys", {}).items():
                if d["key_types"].get(k) not in ("gap", "fader"):
                    out.setdefault(hk, (d["id"], int(k)))
        return out

    def _loop(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            want = self.wanted()
            if want == self.applied:
                continue
            problems = self.backend.apply(want)
            self.want, self.applied = want, want
            by_deck = {}
            for hk, msg in problems.items():
                if hk in want:
                    d, k = want[hk]
                    by_deck.setdefault(d, {})[str(k)] = msg
            if by_deck != self.m.hotkey_problems:
                self.m.hotkey_problems = by_deck
                for hk, msg in problems.items():
                    log.warning("hotkey %s: %s", hk, msg)
                self.m._emit("decks", None, None)          # pages redraw with the news

    def fire(self, hk):
        where = self.want.get(hk)
        if where:
            try:
                self.m.tap(*where)
            except KeyError:
                pass
