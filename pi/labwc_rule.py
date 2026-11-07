"""Stop labwc drawing a title bar on RebelDeck's deck windows.

RebelDeck's deck windows ask for no frame, but labwc (the Pi's Wayland desktop)
draws its own title bar with minimise/maximise/close on them anyway (BD-1,
7 Nov). A labwc window rule turns that off for windows whose title ends in
" · RebelDeck" (deck windows and broken-out keys; not the pop-out or GUI).

    python3 labwc_rule.py <user's home>

Adds the rule to the user's ~/.config/labwc/rc.xml once (marked with a
comment), keeping everything else in the file as it was. If the user has no
rc.xml of their own, it starts from the system one, because labwc reads only
one rc.xml. A copy of the old file is kept as rc.xml.rebeldeck-backup.
Prints what it did; exits 0 even when it changes nothing.
"""
import os
import shutil
import sys

MARK = "<!-- RebelDeck: no labwc title bar on deck windows -->"
RULE = f'    {MARK}\n    <windowRule title="* · RebelDeck" serverDecoration="no" />\n'
SYSTEM = "/etc/xdg/labwc/rc.xml"
EMPTY = '<?xml version="1.0"?>\n<labwc_config>\n</labwc_config>\n'


def add_rule(text):
    """The rc.xml text with the rule in it, or None if it's already there."""
    if MARK in text:
        return None
    i = text.find("<windowRules>")
    if i >= 0:
        j = text.index(">", i) + 1
        return text[:j] + "\n" + RULE + text[j:]
    for close in ("</labwc_config>", "</openbox_config>"):
        k = text.rfind(close)
        if k >= 0:
            return text[:k] + "  <windowRules>\n" + RULE + "  </windowRules>\n" + text[k:]
    raise ValueError("rc.xml has no <labwc_config> or <openbox_config> to add to")


def main(home):
    folder = os.path.join(home, ".config", "labwc")
    path = os.path.join(folder, "rc.xml")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
    elif os.path.exists(SYSTEM):
        with open(SYSTEM, encoding="utf-8") as f:
            text = f.read()
    else:
        text = EMPTY
    try:
        new = add_rule(text)
    except ValueError as e:
        print(f"labwc: left rc.xml alone ({e})")
        return 0
    if new is None:
        print("labwc: deck window rule already there")
        return 0
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".rebeldeck-backup")
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)
    os.replace(tmp, path)
    print(f"labwc: added the deck window rule to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
