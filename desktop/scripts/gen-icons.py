"""Generate crisp multi-resolution desktop & tray icons matching assets/logo.svg.

Draws at 1024x1024 (4x supersampled) with Pillow and downsamples via LANCZOS so
there are zero native Cairo/librsvg dependencies on Windows.
"""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent.parent / "assets"
SIZE = 1024  # 8x assets/logo.svg's 128x128 viewBox
S = SIZE / 128.0


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _make_vertical_grad(w: int, h: int, top: tuple[int, int, int], bot: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (w, h))
    px = img.load()
    for y in range(h):
        c = _lerp(top, bot, y / max(1, h - 1))
        for x in range(w):
            px[x, y] = (*c, 255)
    return img


def _make_diag_grad(w: int, h: int) -> Image.Image:
    # Matches #38bdf8 -> #10b981 -> #22c55e
    c0 = (0x38, 0xBD, 0xF8)
    c1 = (0x10, 0xB9, 0x81)
    c2 = (0x22, 0xC5, 0x5E)
    img = Image.new("RGBA", (w, h))
    px = img.load()
    denom = max(1, (w - 1) + (h - 1))
    for y in range(h):
        for x in range(w):
            t = (x + y) / denom
            c = _lerp(c0, c1, t * 2.0) if t <= 0.5 else _lerp(c1, c2, (t - 0.5) * 2.0)
            px[x, y] = (*c, 255)
    return img


def _draw_thick_line(draw: ImageDraw.ImageDraw, p0: tuple[float, float], p1: tuple[float, float], width: float, fill) -> None:
    draw.line([p0, p1], fill=fill, width=int(round(width)))
    r = width / 2.0
    for px, py in (p0, p1):
        draw.ellipse([px - r, py - r, px + r, py + r], fill=fill)


def render_master() -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 1. Rounded background card: rect x=12 y=12 w=104 h=104 rx=26
    rect_box = [int(12 * S), int(12 * S), int(116 * S), int(116 * S)]
    radius = int(26 * S)
    bg_mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(bg_mask).rounded_rectangle(rect_box, radius=radius, fill=255)
    bg_grad = _make_vertical_grad(SIZE, SIZE, (0x1E, 0x29, 0x3B), (0x0F, 0x17, 0x2A))
    canvas.paste(bg_grad, (0, 0), bg_mask)

    # Border stroke (#334155, width 3)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(rect_box, radius=radius, outline=(0x33, 0x41, 0x55, 255), width=int(round(3 * S)))

    # 2. Gradient diamond polygon: (64,26) (98,64) (64,102) (30,64), stroke-width 6.5
    diamond_pts = [(64 * S, 26 * S), (98 * S, 64 * S), (64 * S, 102 * S), (30 * S, 64 * S)]
    diamond_mask = Image.new("L", (SIZE, SIZE), 0)
    dm = ImageDraw.Draw(diamond_mask)
    sw = 6.5 * S
    for i in range(4):
        _draw_thick_line(dm, diamond_pts[i], diamond_pts[(i + 1) % 4], sw, 255)
    diag_grad = _make_diag_grad(SIZE, SIZE)
    canvas.paste(diag_grad, (0, 0), diamond_mask)

    # 3. Prompt chevron ">" in #f8fafc: (49,56) -> (59,64) -> (49,72), stroke 5.5
    cw = 5.5 * S
    white = (0xF8, 0xFA, 0xFC, 255)
    _draw_thick_line(draw, (49 * S, 56 * S), (59 * S, 64 * S), cw, white)
    _draw_thick_line(draw, (59 * S, 64 * S), (49 * S, 72 * S), cw, white)

    # 4. Prompt underscore "_" in #38bdf8: (65,72) -> (79,72), stroke 5.5
    cyan = (0x38, 0xBD, 0xF8, 255)
    _draw_thick_line(draw, (65 * S, 72 * S), (79 * S, 72 * S), cw, cyan)

    return canvas


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    master = render_master()

    icon_png = master.resize((256, 256), Image.Resampling.LANCZOS)
    icon_png.save(OUT_DIR / "icon.png", format="PNG")

    tray_png = master.resize((32, 32), Image.Resampling.LANCZOS)
    tray_png.save(OUT_DIR / "tray.png", format="PNG")

    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save(OUT_DIR / "icon.ico", format="ICO", sizes=ico_sizes)
    print(f"Generated icons in {OUT_DIR}")


if __name__ == "__main__":
    main()
