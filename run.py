"""Start RebelDeck: python3 run.py  (also the entry point for the packaged app)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rebeldeck.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
