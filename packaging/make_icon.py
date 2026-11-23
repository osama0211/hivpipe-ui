#!/usr/bin/env python3
"""Draw the application icon and write every format the installers need.

    python3 packaging/make_icon.py

Writes packaging/icon.ico (Windows, multi-resolution), icon.png (Linux and
the installer banner) and icon.icns (macOS) beside this script. Run it only
when the mark changes; the generated files are committed so a build needs
no image tooling.

The mark: five strands entering from the left and converging on one node,
with a single thicker strand leaving to the right — five pipelines, one
runner. It is drawn at 8x and downsampled, which is cheaper than writing an
antialiasing routine and gives cleaner curves at 16 px than most.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent

S = 1024                      # working canvas, downsampled at the end
BG_TOP = (31, 86, 190)        # deep blue
BG_BOTTOM = (23, 62, 140)
INK = (255, 255, 255)
DIM = (168, 202, 255)         # the strands before they merge

# Where the strands meet, and where the output leaves.
NODE = (0.595, 0.5)
OUT_END = (0.845, 0.5)
IN_X = 0.165
STRAND_Y = (0.235, 0.3675, 0.5, 0.6325, 0.765)


def px(p):
    return (p[0] * S, p[1] * S)


def rounded_background(img):
    """A rounded square with a soft vertical gradient."""
    grad = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        grad.putpixel((0, y), tuple(
            round(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    grad = grad.resize((S, S))

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255)
    img.paste(grad, (0, 0), mask)
    return mask


def strand(draw, y0, width, colour):
    """One input strand: straight in, then a bezier-ish bend to the node.

    Drawn as a polyline of many short segments rather than a curve object,
    because PIL has no curve primitive and the 8x canvas hides the joins.
    """
    x0, (nx, ny) = IN_X, NODE
    bend_start = x0 + (nx - x0) * 0.42

    pts = [(x0, y0), (bend_start, y0)]
    steps = 48
    for i in range(1, steps + 1):
        t = i / steps
        # Quadratic bezier from (bend_start, y0) to the node, with the
        # control point level with the strand so it leaves horizontally.
        cx, cy = bend_start + (nx - bend_start) * 0.55, y0
        x = (1 - t) ** 2 * bend_start + 2 * (1 - t) * t * cx + t ** 2 * nx
        y = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t ** 2 * ny
        pts.append((x, y))

    draw.line([px(p) for p in pts], fill=colour, width=width,
              joint="curve")
    # Rounded cap at the open end.
    r = width / 2
    cx, cy = px((x0, y0))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)


def draw_mark(draw):
    thin = int(S * 0.052)
    thick = int(S * 0.105)

    for y in STRAND_Y:
        strand(draw, y, thin, DIM)

    # The merged output, drawn over the node so the joins disappear.
    draw.line([px(NODE), px(OUT_END)], fill=INK, width=thick)
    r = thick / 2
    for p in (NODE, OUT_END):
        cx, cy = px(p)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=INK)

    # The node itself: a solid disc, so at 16 px the icon still reads as
    # "something converges here" even when the strands have blurred away.
    nr = S * 0.098
    cx, cy = px(NODE)
    draw.ellipse([cx - nr, cy - nr, cx + nr, cy + nr], fill=INK)


def build():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    base = Image.new("RGB", (S, S), BG_BOTTOM)
    mask = rounded_background(base)
    img.paste(base, (0, 0), mask)

    overlay = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw_mark(ImageDraw.Draw(overlay))
    # Keep the mark inside the rounded corners.
    overlay.putalpha(Image.composite(
        overlay.getchannel("A"), Image.new("L", (S, S), 0), mask))
    img = Image.alpha_composite(img, overlay)
    return img


def main():
    master = build()

    png = HERE / "icon.png"
    master.resize((512, 512), Image.LANCZOS).save(png)

    sizes = [(n, n) for n in (16, 24, 32, 48, 64, 128, 256)]
    master.save(HERE / "icon.ico", format="ICO", sizes=sizes)

    try:
        icns = master.resize((1024, 1024), Image.LANCZOS)
        icns.save(HERE / "icon.icns", format="ICNS")
        made_icns = True
    except (OSError, ValueError):
        made_icns = False

    print(f"wrote {png.name}, icon.ico ({len(sizes)} sizes)"
          + (", icon.icns" if made_icns else ", icon.icns skipped"))

    # A quick legibility check: the 16 px version should still have strong
    # contrast between the mark and the background.
    small = master.resize((16, 16), Image.LANCZOS).convert("L")
    lo, hi = small.getextrema()
    print(f"16 px contrast: {lo}-{hi}" + ("" if hi - lo > 110 else "  (LOW)"))


if __name__ == "__main__":
    main()
