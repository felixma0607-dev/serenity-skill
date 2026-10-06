"""One-off script: generate PWA icons (192x192, 512x512 PNG) with zero
external dependencies (pure stdlib: zlib + struct). Run once; re-run only
if you want to change the icon design.
"""
import struct
import zlib
import os

BG = (15, 17, 21)        # --bg
BAR1 = (91, 157, 255)    # --accent (blue)
BAR2 = (62, 207, 142)    # --green
BAR3 = (242, 169, 59)    # --amber


def make_icon(size):
    px = [[BG for _ in range(size)] for _ in range(size)]

    # simple 3-bar "chart" glyph, centered, rounded corners approximated
    margin = size * 0.18
    gap = size * 0.06
    bar_w = (size - 2 * margin - 2 * gap) / 3
    base_y = size - margin
    heights = [0.30, 0.55, 0.42]  # relative bar heights
    colors = [BAR1, BAR2, BAR3]

    for i in range(3):
        x0 = margin + i * (bar_w + gap)
        x1 = x0 + bar_w
        h = heights[i] * (size - 2 * margin)
        y0 = base_y - h
        y1 = base_y
        radius = bar_w * 0.18
        for y in range(size):
            if y < y0 or y > y1:
                continue
            for x in range(size):
                if x < x0 or x > x1:
                    continue
                # rounded top corners only
                in_corner_zone = y < y0 + radius
                if in_corner_zone:
                    cx = x0 + radius if x < x0 + radius else (x1 - radius if x > x1 - radius else None)
                    if cx is not None:
                        cy = y0 + radius
                        if (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
                            continue
                px[int(y)][int(x)] = colors[i]

    return px


def write_png(path, px):
    size = len(px)
    raw = bytearray()
    for row in px:
        raw.append(0)  # filter type 0 (none)
        for (r, g, b) in row:
            raw += bytes((r, g, b))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    with open(path, "wb") as f:
        f.write(png)


if __name__ == "__main__":
    out_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for size in (192, 512):
        px = make_icon(size)
        out_path = os.path.join(out_dir, f"icon-{size}.png")
        write_png(out_path, px)
        print(f"wrote {out_path}")
