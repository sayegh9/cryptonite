# Cryptonite logo: handover notes

## What's here
- `svg/`: 29 masters. Symbol, small cut, horizontal, stacked and wordmark, each in colour, reversed, black, white and mono. Also the dark and light tiles, the adaptive favicon and the favicon tile.
- `build_kit.py`: regenerates every SVG in `svg/`.
- `GUIDELINES.md`: the one-page usage guide.
- Web icons are installed in the app: `apps/web/src/app/favicon.ico`, `icon.svg` (switches the wick colour for light and dark tabs), `apple-icon.png`, `manifest.ts`, and `apps/web/public/icon-192.png`, `icon-512.png`, `maskable-512.png`.
- PNG renders, the presentation board and mockup slides are kept outside the repo in the brand kit folder.

## Rationale
The symbol puts a Kryptonite crystal and a market candlestick into one shape. The wick is solid outside the stone and a cut inside it, and that cut is what splits the stone into the lit and shadow facets. Crypto logos are usually coins, ₿ glyphs, chains, rockets and neon gradients. This mark uses none of them, and it avoids the glowing, angled shard cluster associated with the comic-book Kryptonite. The wordmark carries the crystal's 45° facets into every round letter: C, O, P and R.

## Construction (256-unit canvas)
- **Symbol:** body 116 wide, 45° tips. The wick is 12 wide, the same as the cut. Wicks run from y 16–60 and 196–240.
- **Small cut:** 22-unit wick and cut, 150-unit body, shorter wicks.
- **Reversed:** the cut is 1 unit wider and the type strokes are 18 instead of 19, to make up for light shapes spreading on dark.
- **Wordmark:**
  - Cap height 100, vertical stems 19, horizontals 18 (a little thinner on purpose).
  - Facets are 26 on C and O. On P and R the facet is chosen so the R leg comes out at exactly 60°; the Y arms and the N diagonal are also exactly 60°.
  - Letter spacing was adjusted by eye, pair by pair.

## Tests run
- `svg_audit.py` passes 100/100 on the symbols and lockups. Everything is filled shapes: no live text, rasters or filters, and the angles are clean.
- Test sheet (kept in the brand kit folder):
  - Size ladder from 16 to 256 px, plus a pixel test at 16, 32 and 48 px.
  - One-colour, reversed, greyscale, blur ("squint"), mirrored and rotated 180°.
  - In context: browser tab, app grid, avatar and site header.
- Problems found and written into the guide:
  - The colour version on Facet green loses its shadow facet.
  - The colour version on black loses its wicks.
  - At 16 px the cut through the favicon blurs, but the stone and wick still read.

## Open items
- **Trademark:** "Kryptonite" is a DC Comics trademark, and also a bike-lock brand. Get a professional trademark search for the name and the mark before any public or commercial use. Do a reverse image search on the symbol as well.
- **Print:** the CMYK and Pantone values are approximations. Proof them on press. If a printer asks for PDF, EPS or AI masters, convert them from the SVGs in Inkscape or Illustrator.
