"""Build the Cryptonite logo kit masters: symbol, small-size cut, wordmark, lockups, colour variants.

Every shape is a polygon (no live text). The wordmark is custom: round letters are cut with the same
45-degree facets as the crystal, so the type and the symbol share one geometry.
"""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "svg")
os.makedirs(OUT, exist_ok=True)

# ---- palette
LIGHT = "#7CDB4C"    # Kryptonite (lit facet)
SHADE = "#419547"    # Facet (shadow facet; the app's existing primary)
ORE = "#0F2B1C"      # Ore (wicks, wordmark on light)
OBSIDIAN = "#0A120E" # dark background
QUARTZ = "#EAF7E3"   # wicks/wordmark on dark

R2 = math.sqrt(2)


def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def poly(pts):
    return "M" + " L".join(f"{f(x)} {f(y)}" for x, y in pts) + " Z"


def tf(pts, ox=0.0, oy=0.0, s=1.0):
    return [(ox + x * s, oy + y * s) for x, y in pts]


def svg(parts, w, h, title="Cryptonite logo", vb=None):
    """parts: list of (d, fill, evenodd)."""
    body = "\n".join(
        f'  <path d="{d}" fill="{c}"' + (' fill-rule="evenodd"' if eo else "") + "/>" for d, c, eo in parts
    )
    vb = vb or f"0 0 {f(w)} {f(h)}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{f(w)}" height="{f(h)}" '
            f'role="img" aria-labelledby="t">\n  <title id="t">{title}</title>\n{body}\n</svg>\n')


def write(name, text):
    with open(os.path.join(OUT, name), "w") as fh:
        fh.write(text)


# ---------------------------------------------------------------- symbol
def crystal(small=False, reversed_=False):
    """Returns dict of polygons: wick_top, left, right, wick_bottom on a 256 canvas (centre x 128).

    Main: body 116 wide, 45-degree tips, wick 12 = facet gap.
    Small cut (<= 32 px): wider gap/wick, broader body, shorter wicks so the idea survives at 16 px.
    Reversed: facet gap opened by 1 unit (light shapes on dark spread optically).
    """
    cx = 128
    if small:
        W, half, top, tip, sh1, sh2, bot, base = 22, 64, 20, 56, 120, 136, 200, 236
    else:
        W, half, top, tip, sh1, sh2, bot, base = 12, 52, 16, 60, 112, 144, 196, 240
    g = W + (1 if reversed_ else 0)
    L, R = cx - g / 2, cx + g / 2
    wl, wr = cx - W / 2, cx + W / 2
    return {
        "wick_top": [(wl, top), (wr, top), (wr, tip), (wl, tip)],
        "left": [(L, tip), (L - half, sh1), (L - half, sh2), (L, bot)],
        "right": [(R, tip), (R + half, sh1), (R + half, sh2), (R, bot)],
        "wick_bottom": [(wl, bot), (wr, bot), (wr, base), (wl, base)],
    }


def symbol_parts(scheme, small=False, ox=0, oy=0, s=1.0):
    rev = scheme in ("dark", "white")
    c = crystal(small, rev)
    colours = {
        "colour": (ORE, LIGHT, SHADE),
        "dark": (QUARTZ, LIGHT, SHADE),
        "black": ("#000",) * 3,
        "white": ("#fff",) * 3,
        "mono": (SHADE,) * 3,
        "ore": (ORE,) * 3,
    }[scheme]
    wick, lit, shade = colours
    return [
        (poly(tf(c["wick_top"], ox, oy, s)), wick, False),
        (poly(tf(c["left"], ox, oy, s)), lit, False),
        (poly(tf(c["right"], ox, oy, s)), shade, False),
        (poly(tf(c["wick_bottom"], ox, oy, s)), wick, False),
    ]


# ---------------------------------------------------------------- wordmark
H = 100.0  # cap height (units)


def letters(s=19.0, hs=None):
    """Glyph polygons for C R Y P T O N I E. Returns {char: (width, [paths as (pts_list, evenodd)])}.

    s = vertical stem, hs = horizontal stroke (a touch thinner, optical correction).
    """
    hs = hs if hs is not None else s - 1
    c = 26.0                     # facet on C and O
    ci = c - s * (2 - R2)        # inner facet keeps the diagonal stroke = s
    yb = 62.0                    # bowl bottom (R, P)
    c2 = (H - yb) * math.tan(math.radians(30))  # bowl facet chosen so the R leg is exactly 60 degrees
    ci2 = c2 - s * (2 - R2)
    G = {}

    def octagon(x0, y0, x1, y1, ch):
        return [(x0 + ch, y0), (x1 - ch, y0), (x1, y0 + ch), (x1, y1 - ch),
                (x1 - ch, y1), (x0 + ch, y1), (x0, y1 - ch), (x0, y0 + ch)]

    # O
    w = 88.0
    G["O"] = (w, [("EO", octagon(0, 0, w, H, c), octagon(s, hs, w - s, H - hs, ci))])

    # C: octagon ring, right stroke removed; terminals cut horizontally just below the facets
    w = 84.0
    t = 4.0
    yt, ybt = c + t, H - c - t
    # inner top-right facet: (w - x) + y = c + s*sqrt2 (approx with hs on horizontals)
    xin_t = w - (s + ci + hs - yt)          # on the inner top-right facet
    xin_b = w - (s + ci + hs - (H - ybt))
    G["C"] = (w, [("P", [(w, yt), (w, c), (w - c, 0), (c, 0), (0, c), (0, H - c), (c, H), (w - c, H), (w, H - c),
                         (w, ybt), (xin_b, ybt), (w - s - ci, H - hs), (s + ci, H - hs), (s, H - hs - ci),
                         (s, hs + ci), (s + ci, hs), (w - s - ci, hs), (xin_t, yt)])])

    # P
    w = 74.0
    bowl_out = [(0, 0), (w - c2, 0), (w, c2), (w, yb - c2), (w - c2, yb)]
    def counter(w):
        return [(s, hs), (w - s - ci2, hs), (w - s, hs + ci2), (w - s, yb - hs - ci2),
                (w - s - ci2, yb - hs), (s, yb - hs)]
    G["P"] = (w, [("EO", bowl_out + [(s, yb), (s, H), (0, H)], counter(w))])

    # R: P bowl + 60-degree leg that starts where the bowl facet ends
    w = 78.0
    Lg = s / math.sin(math.radians(60))
    G["R"] = (w, [("EO", [(0, 0), (w - c2, 0), (w, c2), (w, yb - c2), (w - c2, yb), (w, H), (w - Lg, H),
                          (w - c2 - Lg, yb), (s, yb), (s, H), (0, H)], counter(w))])

    # Y: 60-degree arms meeting the stem at bowl height
    w = 84.0
    La = s / math.sin(math.radians(60))
    ym = (w / 2 - s / 2) * math.tan(math.radians(60))
    yv = (w / 2 - La) * math.tan(math.radians(60))
    G["Y"] = (w, [("P", [(0, 0), (La, 0), (w / 2, yv), (w - La, 0), (w, 0), (w / 2 + s / 2, ym),
                         (w / 2 + s / 2, H), (w / 2 - s / 2, H), (w / 2 - s / 2, ym)])])

    # T
    w = 76.0
    G["T"] = (w, [("P", [(0, 0), (w, 0), (w, hs), (w / 2 + s / 2, hs), (w / 2 + s / 2, H),
                         (w / 2 - s / 2, H), (w / 2 - s / 2, hs), (0, hs)])])

    # N: two stems + diagonal (perpendicular thickness ~ s*1.1 so the diagonal doesn't look thin)
    sd = s * 1.1
    d = sd / math.sin(math.radians(60))   # horizontal thickness of the diagonal band
    w = H / math.tan(math.radians(60)) + d  # width chosen so the diagonal is exactly 60 degrees
    G["N"] = (w, [("P", [(0, 0), (d, 0), (w - s, (w - s - d) * H / (w - d)),
                         (w - s, 0), (w, 0), (w, H), (w - d, H), (s, s * H / (w - d)), (s, H), (0, H)])])

    # I
    G["I"] = (s, [("P", [(0, 0), (s, 0), (s, H), (0, H)])])

    # E: middle bar slightly shorter and a touch above centre
    w = 66.0
    my0 = (H - hs) / 2 - 2
    G["E"] = (w, [("P", [(0, 0), (w, 0), (w, hs), (s, hs), (s, my0), (w - 6, my0), (w - 6, my0 + hs),
                         (s, my0 + hs), (s, H - hs), (w, H - hs), (w, H), (0, H)])])
    return G


# optical spacing (units at cap height 100) between consecutive letters of CRYPTONITE
WORD = "CRYPTONITE"
GAPS = [18, 12, 11, 7, 9, 18, 21, 15, 15]


def wordmark_parts(ox, oy, scale, fill, s=19.0):
    G = letters(s)
    parts, x = [], 0.0
    for i, ch in enumerate(WORD):
        w, glyph = G[ch]
        for g in glyph:
            if g[0] == "EO":
                d = poly(tf([(x + px, py) for px, py in g[1]], ox, oy, scale)) + " " + \
                    poly(tf([(x + px, py) for px, py in g[2]], ox, oy, scale))
                parts.append((d, fill, True))
            else:
                parts.append((poly(tf([(x + px, py) for px, py in g[1]], ox, oy, scale)), fill, False))
        x += w + (GAPS[i] if i < len(GAPS) else 0)
    return parts, x  # x = total advance width in units


def wordmark_width():
    return wordmark_parts(0, 0, 1, "#000")[1]


# ---------------------------------------------------------------- build
SCHEMES = {
    # name: (symbol scheme, wordmark fill, background for preview or None)
    "colour": ("colour", ORE),
    "reversed": ("dark", QUARTZ),
    "black": ("black", "#000"),
    "white": ("white", "#fff"),
    "mono": ("mono", SHADE),
}


def main():
    WW = wordmark_width()

    # symbol masters (256 canvas; artwork x 70..186, y 16..240)
    for key, (sch, _) in SCHEMES.items():
        write(f"cryptonite-symbol-{key}.svg", svg(symbol_parts(sch), 256, 256, "Cryptonite symbol"))
        write(f"cryptonite-symbol-small-{key}.svg", svg(symbol_parts(sch, small=True), 256, 256, "Cryptonite symbol"))

    # wordmark only (cap height 100, 12 unit margin)
    for key, (_, wf) in SCHEMES.items():
        s = 18.0 if key in ("reversed", "white") else 19.0  # reversed type thinned for irradiation
        parts, _ = wordmark_parts(12, 12, 1.0, wf, s)
        write(f"cryptonite-wordmark-{key}.svg", svg(parts, math.ceil(WW + 24), H + 24, "Cryptonite"))

    # horizontal lockup: height 256; symbol full height 224, cap height 96 centred on the symbol;
    # gap symbol -> word = 56 (= the body half-width, also the clear-space unit)
    cap = 96.0
    sc = cap / H
    sym_left = 70  # artwork's left edge in its own canvas
    pad = 16
    sym_ox = pad - sym_left
    word_x = pad + 116 + 56
    width = math.ceil(word_x + WW * sc + pad)
    for key, (sch, wf) in SCHEMES.items():
        s = 18.0 if key in ("reversed", "white") else 19.0
        parts = symbol_parts(sch, ox=sym_ox) + wordmark_parts(word_x, 128 - cap / 2, sc, wf, s)[0]
        write(f"cryptonite-horizontal-{key}.svg", svg(parts, width, 256, "Cryptonite"))

    # stacked lockup: symbol on top, word below; word width defines the canvas
    cap2 = 60.0
    sc2 = cap2 / H
    ww2 = WW * sc2
    ssym = 1.1
    sym_h = 224 * ssym
    gap = 44
    total_h = math.ceil(pad + sym_h + gap + cap2 + pad)
    total_w = math.ceil(ww2 + 2 * pad)
    sox = total_w / 2 - 128 * ssym
    soy = pad - 16 * ssym
    for key, (sch, wf) in SCHEMES.items():
        s = 18.0 if key in ("reversed", "white") else 19.0
        parts = symbol_parts(sch, ox=sox, oy=soy, s=ssym) + \
            wordmark_parts(pad, pad + sym_h + gap, sc2, wf, s)[0]
        write(f"cryptonite-stacked-{key}.svg", svg(parts, total_w, total_h, "Cryptonite"))

    # icon tiles (used for app icon / presentation): reversed symbol on Obsidian, colour symbol on Quartz
    def tile(bg, sch, small, name):
        k = 0.78
        o = 128 * (1 - k)
        parts = [("M0 0 H256 V256 H0 Z", bg, False)] + symbol_parts(sch, small, ox=o, oy=o, s=k)
        write(name, svg(parts, 256, 256, "Cryptonite"))
    tile(OBSIDIAN, "dark", True, "cryptonite-tile-dark.svg")
    tile(QUARTZ, "colour", True, "cryptonite-tile-light.svg")
    print("built", len(os.listdir(OUT)), "files; wordmark width", round(WW, 1))


if __name__ == "__main__":
    main()


def favicons():
    """favicon.svg that adapts to light/dark tabs, and a rounded Obsidian tile for the .ico/PNG fallbacks."""
    c = crystal(small=True)
    k, o = 0.92, 128 * (1 - 0.92)
    P = {n: poly(tf(c[n], o, o, k)) for n in c}
    adaptive = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256" role="img" aria-labelledby="t">
  <title id="t">Cryptonite</title>
  <style>.w{{fill:{ORE}}}@media (prefers-color-scheme: dark){{.w{{fill:{QUARTZ}}}}}</style>
  <path class="w" d="{P["wick_top"]}"/>
  <path d="{P["left"]}" fill="{LIGHT}"/>
  <path d="{P["right"]}" fill="{SHADE}"/>
  <path class="w" d="{P["wick_bottom"]}"/>
</svg>
'''
    write("cryptonite-favicon-adaptive.svg", adaptive)
    k, o = 0.8, 128 * (1 - 0.8)
    parts = [('M56 0 H200 A56 56 0 0 1 256 56 V200 A56 56 0 0 1 200 256 H56 A56 56 0 0 1 0 200 V56 A56 56 0 0 1 56 0 Z',
              OBSIDIAN, False)] + symbol_parts("dark", True, ox=o, oy=o, s=k)
    write("cryptonite-favicon-tile.svg", svg(parts, 256, 256, "Cryptonite"))


if __name__ == "__main__":
    favicons()
