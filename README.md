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

- **Tray menu**: status, the pop-out, the web GUI, show/hide each deck, update, quit.
- **Pop-out**: Companion's address, and which decks have their windows open.
- **Web GUI** (`http://127.0.0.1:8091/gui`): make, name and size decks (presets or any columns × rows),
  and every setting.

Phones and tablets open `http://<this computer>:8091/` and pick a deck. On an iPhone or iPad,
**Share → Add to Home Screen** makes it open full-screen. Set a web password in Settings and other devices need it
for the web GUI (pressing buttons stays open).

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

Optional: `pystray` and `Pillow` (tray icon; Pillow also for Companions older than API 1.12), `tkinter` (pop-out).
Deck windows use Edge, Chrome or Chromium in app mode, falling back to the normal browser.

Settings live in `~/.config/rebeldeck/settings.json`; the web GUI edits them.

Tests: `python3 tests/test_rebeldeck.py` (against a stand-in Companion).

## Licence

MIT. Copyright Joseph Adams (ScreenDeck) and Nige Isted.
