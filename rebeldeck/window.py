"""Open the deck in a window of its own on this computer.

Uses Edge or Chrome in "app" mode (no address bar or tabs) where one is
installed, otherwise the default browser.
"""
import os
import shutil
import subprocess
import sys
import webbrowser


def _candidates():
    if sys.platform == "win32":
        pf = [os.environ.get(k, "") for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for base in pf:
            if base:
                yield os.path.join(base, r"Microsoft\Edge\Application\msedge.exe")
                yield os.path.join(base, r"Google\Chrome\Application\chrome.exe")
    elif sys.platform == "darwin":
        yield "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        yield "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        yield "/Applications/Chromium.app/Contents/MacOS/Chromium"
    else:
        for name in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge"):
            p = shutil.which(name)
            if p:
                yield p


def open_deck(url, width=1000, height=540):
    profile = os.path.expanduser("~/.config/rebeldeck/window-profile")
    for exe in _candidates():
        if os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", f"--window-size={width},{height}",
                                  f"--user-data-dir={profile}", "--no-first-run"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    webbrowser.open(url)
    return False
