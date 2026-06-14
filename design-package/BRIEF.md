# Design brief

## The device

A desk gadget that shows, at a glance, how much of your Claude usage you've spent:
- **Session** — the current 5-hour billing block (resets every 5h).
- **Weekly** — usage since the weekly reset (resets a fixed weekday/time, e.g. Thu 08:00).

It polls a small bridge on the user's Mac every 10s and animates a "Claude spark"
(orange sunburst) continuously. Screen is a 160×128 ST7735 TFT in landscape.

## Who's looking, and how

A developer glancing over from across the desk while coding. The screen must be
**readable in under a second** without leaning in. Numbers and bars matter more
than chrome. The device is always-on, so it should look calm at idle and only
draw the eye when usage gets high.

## What to design

Two screens (plus their state variants — see `data-contract.json`):

1. **Boot / status** — shown while connecting to WiFi and fetching the first
   reading. Currently: a big breathing+rotating Claude spark, a "Claude" wordmark,
   and a status line ("Connecting WiFi…", "Fetching usage…", "Waiting for bridge…").

2. **Dashboard** — the main screen. Currently top-to-bottom:
   account email · SESSION (value + bar + reset countdown) · divider ·
   WEEKLY (value + bar + reset countdown) · animated spark in the header that
   doubles as the connection status light.

## What "better" means here

Improve any of these — you don't have to keep the current layout:

- **Hierarchy & glanceability.** Make session-vs-weekly instantly distinguishable.
  Make the most decision-relevant number (how close am I to a limit? when does it
  reset?) the most prominent thing.
- **Use the space well.** 160×128 is small; the current layout is text-heavy.
  Consider larger numerics, ring/arc gauges, battery-style meters, icons
  (clock for resets, etc.), or a sparkline — anything the primitives can draw.
- **Encode urgency with color.** Bars already go green → yellow → red
  (<60 / 60–85 / ≥85). Lean into that so a "you're nearly capped" state reads
  from across the room, while a calm state stays subdued.
- **Keep the Claude brand.** The orange spark is the identity — keep a version of
  it, but it can move, resize, or change role.
- **Respect the states.** Design how connecting / stale / idle / high-usage look,
  not just the happy path (see `data-contract.json`).

## Constraints (hard — see CONSTRAINTS.md)

- 160×128, RGB565, **no anti-aliasing or alpha** — solid fills and 1px lines only.
- Built-in font is a **6×8 px** cell, integer sizes only (1=6×8, 2=12×16, …).
  A nicer custom GFX font is possible but assume the built-in font for now.
- Only the listed `Adafruit_GFX` primitives. Animated regions should be cheap
  (double-buffered) to stay ~30 fps.
- Only the fields in `data-contract.json` exist. Don't invent data
  (no graphs of history unless you note it requires the bridge to send series).

## Deliverable

1. The redesigned `drawBoot(...)` and `drawDashboard(...)` functions in
   `simulator.html`, rendering live in the canvas across all states.
2. A short rationale (what changed and why it's more glanceable).
3. The `Adafruit_GFX` mapping (or directly edited `render()`/`drawStatic()` for
   `claude_monitor.ino`) — see the mapping table in `CONSTRAINTS.md`.
