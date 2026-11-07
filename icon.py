"""RebelDeck's app icon: python3 icon.py -> icon.png and icon.ico (used by the builds)."""
from rebeldeck.art import draw

if __name__ == "__main__":
    draw(256).save("icon.png")
    draw(256).save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
