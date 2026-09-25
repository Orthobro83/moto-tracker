#!/usr/bin/env python3
"""
The launcher icon: a rider's dot with a motion trail sweeping in behind it, on the
app's own dark ground. Generated rather than drawn so it can be regenerated at any
size without a design tool.

    /usr/bin/python3 tools/make_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

BG = (15, 17, 21)          # the app's background
GO = (61, 220, 132)        # the riding green
DIM = (36, 110, 74)        # the tail of the trail

HERE = Path(__file__).resolve().parent.parent
RES = HERE / "app/src/main/res"


def png(path: Path, size: int, glyph: float = 0.62, rounded: bool = True,
        clear_bg: bool = False) -> None:
    cx = cy = (size - 1) / 2
    r_out = size * 0.46
    corner = size * 0.22
    dot_r = size * glyph * 0.17
    trail_r = size * glyph * 0.36
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = x - cx, y - cy
            inside = True
            if rounded:
                inside = abs(dx) <= r_out and abs(dy) <= r_out
                ox, oy = abs(dx) - (r_out - corner), abs(dy) - (r_out - corner)
                if inside and ox > 0 and oy > 0 and (ox * ox + oy * oy) > corner * corner:
                    inside = False
            d = math.hypot(dx, dy)
            ddx, ddy = dx - trail_r * 0.62, dy + trail_r * 0.62
            on_dot = math.hypot(ddx, ddy) <= dot_r
            angle = math.atan2(-dy, dx)
            on_trail = abs(d - trail_r) <= size * 0.045 and 0.15 < angle < 2.5
            if not inside:
                row += bytes((0, 0, 0, 0))
            elif on_dot:
                row += bytes(GO + (255,))
            elif on_trail:
                fade = min(1.0, max(0.25, (angle - 0.1) / 1.6))
                row += bytes(tuple(int(DIM[i] + (GO[i] - DIM[i]) * (1 - fade))
                                   for i in range(3)) + (255,))
            elif clear_bg:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes(BG + (255,))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
                     + chunk(b"IEND", b""))


def main() -> None:
    for folder, size in (("mipmap-mdpi", 48), ("mipmap-hdpi", 72), ("mipmap-xhdpi", 96),
                         ("mipmap-xxhdpi", 144), ("mipmap-xxxhdpi", 192)):
        png(RES / folder / "ic_launcher.png", size)
        png(RES / folder / "ic_launcher_round.png", size)
        # Adaptive foreground: the glyph alone, smaller, on transparency, in the
        # 108dp frame Android expects with the middle 72dp kept safe.
        png(RES / folder / "ic_launcher_fg.png", round(size * 108 / 48),
            glyph=0.42, rounded=False, clear_bg=True)

    (RES / "mipmap-anydpi-v26").mkdir(parents=True, exist_ok=True)
    for name in ("ic_launcher", "ic_launcher_round"):
        (RES / "mipmap-anydpi-v26" / f"{name}.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
            '    <background android:drawable="@color/icon_background" />\n'
            '    <foreground android:drawable="@mipmap/ic_launcher_fg" />\n'
            '</adaptive-icon>\n')

    colors = RES / "values/colors.xml"
    colors.parent.mkdir(parents=True, exist_ok=True)
    text = colors.read_text() if colors.exists() else (
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n</resources>\n')
    if "icon_background" not in text:
        text = text.replace("</resources>",
                            '    <color name="icon_background">#0F1115</color>\n</resources>')
    colors.write_text(text)
    print("icons written to", RES)


if __name__ == "__main__":
    main()
