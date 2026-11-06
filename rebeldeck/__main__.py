"""RebelDeck v0.1: one deck, as a window here and a page for phones and tablets.

  python -m rebeldeck              run, and open the deck window
  python -m rebeldeck --no-window  run without opening a window
"""
import logging
import signal
import socket
import sys
import threading

from . import __version__, settings
from .satellite import Deck
from .web import make_server
from .window import open_deck


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("rebeldeck")
    cfg = settings.load()
    deck = Deck(cfg)
    try:
        server = make_server(cfg, deck)
    except OSError as e:
        log.error("can't use port %s (%s). Is RebelDeck already running?", cfg["web_port"], e)
        return 1
    deck.start()
    threading.Thread(target=server.serve_forever, name="web", daemon=True).start()

    port = cfg["web_port"]
    log.info("RebelDeck %s: %s×%s deck \"%s\", Companion at %s:%s",
             __version__, cfg["columns"], cfg["rows"], cfg["deck_name"],
             cfg["companion_host"], cfg["companion_port"])
    log.info("deck on this computer: http://127.0.0.1:%s/", port)
    if cfg.get("web_network", True):
        log.info("deck for phones and tablets: http://%s:%s/", lan_address(), port)
    if cfg.get("open_window", True) and "--no-window" not in argv:
        open_deck(f"http://127.0.0.1:{port}/")

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:
            pass
    try:
        while not stop.wait(0.5):
            pass
    finally:
        deck.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
