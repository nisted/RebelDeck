"""QR codes for deck addresses (byte mode, error correction M, versions 1-10,
up to 213 characters: plenty for a web address). No extra libraries.

    png(text, scale=8) -> PNG bytes
    matrix(text)       -> list of rows of booleans (True = dark)
"""
import io

# version: (error-correction codewords per block, [(blocks, data codewords per block), ...]) at level M
_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
          8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_EC_M = 0          # format bits for level M

# GF(256) arithmetic for Reed-Solomon
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _mul(coef, factor)
    return result


def _codewords(data, version):
    ecc_len, groups = _M[version]
    capacity = sum(n * size for n, size in groups)
    bits = []

    def put(value, length):
        bits.extend((value >> i) & 1 for i in range(length - 1, -1, -1))

    put(0b0100, 4)                                   # byte mode
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, capacity * 8 - len(bits)))         # terminator
    put(0, (-len(bits)) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    # split into blocks, add error correction, interleave
    divisor = _rs_divisor(ecc_len)
    blocks, pos = [], 0
    for n, size in groups:
        for _ in range(n):
            d = words[pos:pos + size]
            pos += size
            blocks.append((d, _rs_remainder(d, divisor)))
    out = []
    for i in range(max(len(d) for d, _ in blocks)):
        for d, _ in blocks:
            if i < len(d):
                out.append(d[i])
    for i in range(ecc_len):
        for _, e in blocks:
            out.append(e[i])
    return out


def _version_for(n):
    for v in range(1, 11):
        ecc_len, groups = _M[v]
        capacity = sum(k * size for k, size in groups)
        if 4 + (8 if v < 10 else 16) + 8 * n <= capacity * 8:
            return v
    raise ValueError("too long for a QR code here (213 characters at most)")


_MASKS = [
    lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


class _Grid:
    def __init__(self, version):
        self.v = version
        self.size = n = version * 4 + 17
        self.dark = [[False] * n for _ in range(n)]
        self.fixed = [[False] * n for _ in range(n)]

    def set(self, x, y, on):
        self.dark[y][x] = on
        self.fixed[y][x] = True

    def patterns(self):
        n = self.size
        for i in range(n):                                          # timing
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):             # finders, with separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.v]
        last = len(pos) - 1
        for i, ay in enumerate(pos):                                # alignment
            for j, ax in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)                                         # reserve the space
        if self.v >= 7:                                             # version information
            rem = self.v
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.v << 12 | rem
            for i in range(18):
                on = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.set(a, b, on)
                self.set(b, a, on)

    def format_bits(self, mask):
        n = self.size
        data = _EC_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, n - 15 + i, bit(i))
        self.set(8, n - 8, True)                                    # the dark module

    def place(self, words):
        n = self.size
        i, total = 0, len(words) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    up = ((right + 1) & 2) == 0
                    y = n - 1 - vert if up else vert
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def masked(self, mask):
        f = _MASKS[mask]
        out = [row[:] for row in self.dark]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and f(x, y):
                    out[y][x] = not out[y][x]
        return out


def _penalty(m):
    n = len(m)
    score = 0
    for lines in (m, [list(c) for c in zip(*m)]):
        for row in lines:                                           # runs of 5 or more
            run, last = 0, None
            for v in row + [None]:
                if v == last:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, last = 1, v
    for y in range(n - 1):                                          # 2x2 blocks
        for x in range(n - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, m))                                         # balance
    score += abs(dark * 20 - n * n * 10) // (n * n) * 10
    return score


def matrix(text):
    data = text.encode("utf-8")
    v = _version_for(len(data))
    g = _Grid(v)
    g.patterns()
    g.place(_codewords(data, v))
    best = None
    for mask in range(8):
        g.format_bits(mask)
        m = g.masked(mask)
        p = _penalty(m)
        if best is None or p < best[0]:
            best = (p, mask)
    g.format_bits(best[1])
    return g.masked(best[1])


def png(text, scale=8, border=4, dark=(0, 0, 0), light=(255, 255, 255)):
    from PIL import Image
    m = matrix(text)
    n = len(m)
    side = (n + border * 2) * scale
    im = Image.new("RGB", (side, side), light)
    px = im.load()
    for y in range(n):
        for x in range(n):
            if m[y][x]:
                for dy in range(scale):
                    for dx in range(scale):
                        px[(x + border) * scale + dx, (y + border) * scale + dy] = dark
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()
