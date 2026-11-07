"""RebelDeck's icon: a purple cut-corner deck."""
from PIL import Image, ImageDraw


def draw(size=256):
    s = 64
    im = Image.new("RGBA", (s * 4, s * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    k = 4
    d.polygon([(4 * k, 4 * k), (50 * k, 4 * k), (60 * k, 14 * k), (60 * k, 60 * k), (14 * k, 60 * k), (4 * k, 50 * k)],
              fill="#5500aa", outline="#b973ff", width=2 * k)
    for r in range(3):
        for c in range(3):
            x, y = (13 + c * 14) * k, (13 + r * 14) * k
            d.rounded_rectangle((x, y, x + 9 * k, y + 9 * k), radius=k, fill="#b973ff" if (r, c) == (0, 2) else "#e8d6ff")
    return im.resize((size, size), Image.LANCZOS)

