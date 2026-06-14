# Hard constraints

Everything a design must obey to be implementable on this device.

## Display

- **Resolution:** 160 × 128 px, **landscape** (ST7735, rotation 3). Origin top-left.
- **Color:** 16-bit RGB565. **No alpha, no anti-aliasing, no gradients.** Solid
  fills and 1px lines. (You *can* fake a gradient with dithering / stepped bands.)
- **Backlight:** always on. Background is black (`#000000`) today.
- **No sub-pixel:** every coordinate is an integer pixel.

## Color palette

Use these named colors (full list + RGB565 in `palette.json`). Adding new colors
is fine — just give the hex; RGB565 conversion is mechanical.

| Name | Hex | Role |
|------|-----|------|
| BG | `#000000` | background |
| TEXT | `#FFFFFF` | primary text / numerals |
| DIM | `#808080` | secondary text, labels, hairlines |
| TRACK | `#202020` | empty portion of a bar/gauge |
| ACCENT | `#00A8F8` | header bar / structural accent (cyan-blue) |
| CLAUDE | `#D87450` | brand spark + section labels (warm orange) |
| GREEN | `#28BC50` | usage < 60% |
| YELLOW | `#F8CC00` | usage 60–85% |
| RED | `#F83430` | usage ≥ 85% / stale / error |

Usage color thresholds: `pct < 60 → GREEN`, `60–84 → YELLOW`, `≥ 85 → RED`.

## Type

- Built-in font cell is **6 × 8 px** (5×7 glyph + 1px gap). **Monospace.**
- Sizes are integer multiples: size 1 = 6×8, size 2 = 12×16, size 3 = 18×24.
- A char advances `6 × size` px. A string of N chars is `6 × size × N` px wide.
  → At size 1, **26 chars** is the max that fits 160px. The account email is
  truncated to 22 chars to clear the header spark.
- A custom proportional `GFXfont` (e.g. a cleaner numeric face) *can* be embedded
  if a design needs nicer big numbers — note it if you rely on it.

## Drawing primitives (Adafruit_GFX)

Only these are available. All take a color argument.

- `drawPixel(x, y, c)`
- `drawLine(x0, y0, x1, y1, c)` · `drawFastHLine(x, y, w, c)` · `drawFastVLine(x, y, h, c)`
- `drawRect / fillRect (x, y, w, h, c)`
- `drawRoundRect / fillRoundRect (x, y, w, h, r, c)`
- `drawCircle / fillCircle (cx, cy, r, c)`
- `drawTriangle / fillTriangle (x0,y0, x1,y1, x2,y2, c)`
- `drawChar` / `setCursor`+`setTextColor`+`setTextSize`+`print(...)`
- `drawBitmap` (1-bit) · `drawRGBBitmap` (16-bit, for sprites)
- No arcs natively — approximate a ring/arc with many short lines or a polygon fan.

## Performance & memory

- Data refreshes every **10s**; the spark animates continuously at ~**30 fps**.
- The MCU is an ESP8266 @ 80 MHz, SPI to the panel at 40 MHz.
- **Don't redraw the whole screen every frame.** Animated regions are
  double-buffered into a `GFXcanvas16` RAM sprite and pushed with one
  `drawRGBBitmap` blit (no flicker). Budget a few KB per sprite (e.g. the current
  spark sprite is 52×52 = ~5.4 KB). Total free RAM with WiFi up is ~40 KB.
- Static parts (labels, dividers) are drawn once; only changed values repaint.

## Firmware mapping

The simulator's `g.*` API is the same shape as the firmware's `tft.*` API, so a
design ports almost verbatim:

| Simulator call | Firmware call |
|----------------|---------------|
| `g.fillScreen(c)` | `tft.fillScreen(c)` |
| `g.fillRect(x,y,w,h,c)` | `tft.fillRect(x,y,w,h,c)` |
| `g.drawRect / drawRoundRect(...)` | `tft.drawRect / drawRoundRect(...)` |
| `g.drawFastHLine(x,y,w,c)` | `tft.drawFastHLine(x,y,w,c)` |
| `g.fillCircle(cx,cy,r,c)` | `tft.fillCircle(cx,cy,r,c)` |
| `g.fillTriangle(...)` | `tft.fillTriangle(...)` |
| `g.text(str,x,y,c,size)` | `tft.setTextColor(c); tft.setTextSize(size); tft.setCursor(x,y); tft.print(str);` |
| colors `'#RRGGBB'` | `COL_*` macros / `tft.color565(r,g,b)` |

In the firmware these live in `claude_monitor.ino`: `drawStatic()` (static layout),
`render(doc)` (dynamic values), `splashBegin()`/`splashFrame()` (boot), and
`headerSpark()` / `drawSpark()` (the spark). Match those names when handing back.
