# RebelDeck

Virtual control surfaces for Bitfocus Companion, on Raspberry Pi screens, Windows, Mac and in the browser.

Based on [ScreenDeck](https://github.com/josephdadams/screendeck) by Joseph Adams (MIT).

## v0.1 (early test version)

One deck, 8×4 by default, as a window on this computer and as a page any phone or tablet on the network can open.
It connects to Companion's Satellite API (Companion 3.5 or later), and the deck appears in Companion's
**Surfaces** list as "RebelDeck".

Run it (Python 3.9 or later):

```
python3 -m rebeldeck              # opens the deck window
python3 -m rebeldeck --no-window  # phones and tablets only
```

It prints the address for phones and tablets, e.g. `http://192.168.1.20:8091/`. On an iPhone or iPad,
**Share → Add to Home Screen** makes it open full-screen like an app.

Settings go in `~/.config/rebeldeck/settings.json` (only the ones you change):

| Setting | Default | |
| --- | --- | --- |
| `companion_host` | `127.0.0.1` | the computer running Companion |
| `companion_port` | `16622` | Companion's Satellite port |
| `deck_name` | `RebelDeck` | the name in Companion's Surfaces list |
| `columns`, `rows` | `8`, `4` | deck size |
| `button_px` | `144` | button picture size asked of Companion |
| `web_port` | `8091` | the deck page's port |
| `web_network` | `true` | let phones and tablets on the network open the deck |
| `open_window` | `true` | open the deck window at start |

Pillow is only needed with Companions that can't send PNG button pictures (older than API 1.12).

Tests: `python3 tests/test_rebeldeck.py` (runs against a stand-in Companion).
