# RebelDeck

Virtual Companion button decks on Raspberry Pi screens, Windows, Mac, and any phone or tablet's browser.
A rebuild of [ScreenDeck](https://github.com/josephdadams/screendeck) by Joseph Adams (MIT), aimed squarely at being
easier to use.

Each deck turns up in Companion's **Surfaces** list as its own surface (Satellite API, Companion 3.5 or later).

## Download

From the [latest release](https://github.com/nisted/RebelDeck/releases/latest):

| | |
| --- | --- |
| Windows | `RebelDeck-Setup.exe` (no admin needed) |
| Mac (Apple silicon) | `RebelDeck-Mac.zip`: unzip, drag into Applications. First time: right-click → Open |
| Raspberry Pi | `RebelDeck-Pi.tar.gz`: `tar xzf RebelDeck-Pi.tar.gz && sudo rebeldeck/pi/install.sh` |

On a Pi with a desktop you get the tray icon and deck windows. Without one (or with `--headless`) it runs as a
service for phones and tablets.

## Three places, three jobs

- **Tray menu**: status, the pop-out, the web GUI, show/hide each deck, identify a deck, update, quit.
- **Pop-out**: start at login, a switch per deck window, QR codes for phones, Companion status.
- **Web GUI** (`http://127.0.0.1:8091/gui`): everything else.

## What it does

- **Decks** of any size (presets or columns × rows), each its own Companion surface. Renaming or resizing keeps its
  Companion setup.
- **Its own deck windows**: frameless, float on top (switch on the window's header), drag to move, corner grip to
  resize (locked to the deck's shape), remembers where they were, movable lock, colour and see-through background.
- **Button editor**: Clickable / Unclickable / Hybrid (pick which buttons work); paint keys as **buttons**,
  **rotaries** (drag or scroll to turn, tap to push) or **faders** (Intech Grid style: turn steps plus a 0-127 surface
  variable `fader_N`; Companion moves them with `fader_N_set`); **break out** single keys into their own little
  floating windows.
- **Strip rows** (wide, short buttons like a Stream Deck + touch strip) and **gaps**, using Companion's free layout
  (Companion 4.2+; wide pictures on Companion 5+). Presets for the **+** and **+ XL** match how Companion lays them out.
- **Several Companions**: a list of servers with a default; any deck can use its own. **Find Companions** scans this
  computer and the local network for Companion's Satellite port.
- **Short addresses**: `/1`, `/2`… open decks by their number on the Decks page.
- **Profiles**: save and switch whole sets of decks.
- **Browsers**: every deck at `http://<computer>:8091/deck/<deck-name>`, a pick-a-deck page that also lists decks on
  other RebelDecks on the network, QR codes, and a `<computer>.local` name. Set a web password and other devices need
  it for the web GUI (pressing buttons stays open).
- A deck's QR code can sit on a Companion button: Generic HTTP's **Image from URL** feedback with
  `http://<computer>:8091/api/decks/<deck-name>/qr.png`.

Known limits: a press always ends in a release in Companion (the Satellite API has no "cancel"); on Wayland Pi
desktops, float and remembered positions may not work.

## Updating

**Update RebelDeck** in the tray menu or the web GUI's Update page downloads and installs the newest build.

## With Companion Sync

If [Companion Sync](https://github.com/nisted/companion-sync) is on the same computer, its tray menu gets a
RebelDeck item (decks, web GUI, updates). Turn on *Let my decks travel between rigs with Companion Sync* in Settings
and deck layouts go with the Companion config on Save and come back on Take. Which windows are open stays each
rig's own choice.

## Running from source

```
python3 run.py              # tray icon and deck windows
python3 run.py --headless   # no tray or windows: phones and tablets only
```

Optional: `pywebview` (deck windows; without it decks open in the browser), `pystray` and `Pillow` (tray icon;
Pillow also for QR codes and for Companions older than API 1.12), `tkinter` (pop-out).

Settings live in `~/.config/rebeldeck/settings.json`; the web GUI edits them.

Tests: `python3 tests/test_rebeldeck.py` (against a stand-in Companion; needs `opencv-python-headless` to read the QR codes).

## Licence

MIT. Copyright Joseph Adams (ScreenDeck) and Nige Isted.
