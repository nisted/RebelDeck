#!/bin/bash
# RebelDeck on the Pi.
#
#   sudo ./pi/install.sh              install or update from this unpacked folder
#   sudo ./pi/install.sh --headless   no tray icon or deck windows: a service, decks for phones and tablets
#   sudo ./pi/install.sh --desktop    tray icon and deck windows (e.g. to undo --headless)
#
# With a desktop it starts at login with a tray icon, like on a Mac or PC.
# Without one it runs as a service: open http://<this pi>:8091/ on a phone or tablet.
# An update keeps the choice as it was. "Update RebelDeck" in the tray menu or
# the web GUI runs this from a downloaded release (with --from-update).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/rebeldeck
say() { echo "[rebeldeck] $*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo: sudo $0"; exit 1; }

ASKED=""
for a in "$@"; do
    case "$a" in
        --headless) ASKED=headless ;;
        --desktop) ASKED=desktop ;;
        --from-update) ;;
        *) echo "Unknown option: $a"; exit 1 ;;
    esac
done

USER_NAME="${REBELDECK_USER:-${SUDO_USER:-${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}}}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then
    [ -f "$DEST/USER" ] && USER_NAME="$(cat "$DEST/USER")"
fi
[ -n "$USER_NAME" ] && [ "$USER_NAME" != root ] || { echo "Can't tell which user to install for. Run it with sudo from your own account."; exit 1; }
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
say "Installing for $USER_NAME"

AUTOSTART="$HOME_DIR/.config/autostart/rebeldeck.desktop"
if [ -n "$ASKED" ]; then
    MODE="$ASKED"
elif [ -f "$DEST/HEADLESS" ]; then
    MODE=headless
elif [ -f "$AUTOSTART" ] || [ -x "$DEST/venv/bin/python" ]; then
    MODE=desktop
elif [ "$(systemctl get-default 2>/dev/null)" = graphical.target ] \
     || ls /usr/share/xsessions/*.desktop /usr/share/wayland-sessions/*.desktop >/dev/null 2>&1; then
    MODE=desktop
else
    MODE=headless
fi
say "Mode: $MODE"

# Packages
need=()
pkgs=(python3-pil rsync)
if [ "$MODE" = desktop ]; then
    # tray icon, pop-out, and RebelDeck's own deck windows (pywebview on WebKitGTK)
    pkgs+=(python3-venv python3-gi gir1.2-ayatanaappindicator3-0.1 python3-tk python3-pil.imagetk)
    if apt-cache show gir1.2-webkit2-4.1 >/dev/null 2>&1; then pkgs+=(gir1.2-webkit2-4.1); else pkgs+=(gir1.2-webkit2-4.0); fi
fi
for p in "${pkgs[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
done
if [ ${#need[@]} -gt 0 ]; then
    say "Installing ${need[*]}"
    apt-get install -y "${need[@]}"
fi

# Files
mkdir -p "$DEST"
rsync -a --delete --exclude USER --exclude HEADLESS --exclude venv --exclude icon.png --exclude __pycache__ "$SRC/" "$DEST/"
echo "$USER_NAME" > "$DEST/USER"
chmod 755 "$DEST/run.py" "$DEST/pi/install.sh"

cat > /usr/local/bin/rebeldeck <<'CMD'
#!/bin/sh
PY=/usr/bin/python3
[ -x /opt/rebeldeck/venv/bin/python ] && [ -z "${REBELDECK_HEADLESS:-}" ] && PY=/opt/rebeldeck/venv/bin/python
exec "$PY" /opt/rebeldeck/run.py "$@"
CMD
chmod 755 /usr/local/bin/rebeldeck

if [ "$MODE" = desktop ]; then
    rm -f "$DEST/HEADLESS"
    if systemctl list-unit-files rebeldeck.service >/dev/null 2>&1; then
        systemctl disable --now rebeldeck.service 2>/dev/null || true
        rm -f /etc/systemd/system/rebeldeck.service
        systemctl daemon-reload
    fi
    if [ ! -x "$DEST/venv/bin/python" ]; then
        say "Setting up the tray app's Python"
        python3 -m venv --system-site-packages "$DEST/venv"
    fi
    "$DEST/venv/bin/pip" install --quiet --upgrade pystray pywebview
    (cd "$DEST" && "$DEST/venv/bin/python" -c "from icon import draw; draw(256).save('icon.png')")
    entry() {
        printf '[Desktop Entry]\nType=Application\nName=RebelDeck\nComment=Companion button decks on any screen\nExec=%s %s %s\nIcon=%s\nTerminal=false\nCategories=AudioVideo;Utility;\n' \
            "$DEST/venv/bin/python" "$DEST/run.py" "$1" "$DEST/icon.png"
    }
    install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.config/autostart"
    entry --login > "$AUTOSTART"
    chown "$USER_NAME:$USER_NAME" "$AUTOSTART"
    entry "" > /usr/share/applications/rebeldeck.desktop
    if [ -n "${WAYLAND_DISPLAY:-}" ] || loginctl show-session "$(loginctl | awk -v u="$USER_NAME" '$3==u{print $1; exit}')" -p Type 2>/dev/null | grep -q wayland; then
        say "This Pi's desktop uses Wayland: deck windows may not float on top or remember where they were."
        say "To get both, switch to X11: sudo raspi-config -> Advanced Options -> Wayland -> X11."
    fi
    say "Done: build $(cat "$DEST/BUILD" 2>/dev/null || echo dev). It starts at the next login (or open RebelDeck from the menu)."
else
    touch "$DEST/HEADLESS"
    rm -f "$AUTOSTART" /usr/share/applications/rebeldeck.desktop
    sed "s/@USER@/$USER_NAME/g" "$DEST/pi/rebeldeck.service" > /etc/systemd/system/rebeldeck.service
    systemctl daemon-reload
    systemctl enable rebeldeck.service
    systemctl restart rebeldeck.service
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    say "Done: build $(cat "$DEST/BUILD" 2>/dev/null || echo dev). Decks: http://${IP:-this-pi}:8091/  Settings: http://${IP:-this-pi}:8091/gui"
fi
