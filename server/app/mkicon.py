"""Generate the app icon: a broadcast/position motif in the dashboard's palette.
Pure stdlib — no PIL on Apple's python. Supersampled 3x for clean edges."""
import math, struct, zlib

N, SS = 1024, 3
W = N * SS

BG   = (0x18, 0x1b, 0x20)
GO   = (0x3d, 0xdc, 0x84)
DIM  = (0x2a, 0x2f, 0x37)

def rounded(px, py, w, r):
    """signed distance to a rounded square centred in the canvas"""
    cx = cy = w / 2
    dx, dy = abs(px - cx) - (w / 2 - r), abs(py - cy) - (w / 2 - r)
    dx, dy = max(dx, 0), max(dy, 0)
    inner = min(max(abs(px - cx) - (w / 2 - r), abs(py - cy) - (w / 2 - r)), 0)
    return math.hypot(dx, dy) + inner - r

def blend(dst, src, a):
    return tuple(int(round(d + (s - d) * a)) for d, s in zip(dst, src))

rows = []
cx = W / 2
cy = W / 2 + W * 0.17
dot_r   = W * 0.085
arcs    = [(W * 0.19, W * 0.030), (W * 0.30, W * 0.030), (W * 0.41, W * 0.030)]
corner  = W * 0.22

for y in range(W):
    row = bytearray()
    for x in range(W):
        # outside the rounded square is transparent
        if rounded(x, y, W, corner) > 0:
            row += bytes((0, 0, 0, 0)); continue
        px = BG
        d = math.hypot(x - cx, y - cy)
        if d <= dot_r:
            px = GO
        else:
            # upward-opening broadcast arcs: keep the wedge above the dot
            ang = math.atan2(cy - y, x - cx)
            for i, (r, t) in enumerate(arcs):
                if abs(d - r) <= t / 2 and 0.62 < ang < math.pi - 0.62:
                    px = GO if i == 0 else blend(BG, GO, 0.82 - i * 0.22)
                    break
        row += bytes(px + (255,))
    rows.append(bytes(row))

# box-downsample SS x SS
out = bytearray()
for y in range(N):
    out.append(0)  # filter: none
    for x in range(N):
        r = g = b = a = 0
        for j in range(SS):
            src = rows[y * SS + j]
            for i in range(SS):
                o = (x * SS + i) * 4
                r += src[o]; g += src[o+1]; b += src[o+2]; a += src[o+3]
        n = SS * SS
        out += bytes((r // n, g // n, b // n, a // n))

def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(bytes(out), 9))
       + chunk(b"IEND", b""))
open("icon.png", "wb").write(png)
print(f"icon.png {len(png)} bytes, {N}x{N}")
