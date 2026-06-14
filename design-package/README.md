# Claude Usage Monitor — screen design package

A self-contained kit for redesigning the two screens of a tiny hardware gadget:
an **ESP8266 + ST7735 1.8" TFT (160×128)** that shows Claude session/weekly usage.

Hand this whole folder to Claude (or any designer) to get better screen layouts
that still fit the hardware and translate straight back into the firmware.

## What's inside

| File | Purpose |
|------|---------|
| [BRIEF.md](BRIEF.md) | The design goals — what "better" means, the deliverable. |
| [CONSTRAINTS.md](CONSTRAINTS.md) | Hard limits: 160×128, RGB565, pixel font, draw primitives, perf, RAM. Read this first. |
| [palette.json](palette.json) | The exact colors (hex + RGB565), machine-readable. |
| [data-contract.json](data-contract.json) | The data available to draw, with example payloads for every screen state. |
| [simulator.html](simulator.html) | **Pixel-accurate sandbox.** Open in a browser. Its draw API mirrors `Adafruit_GFX` 1:1. Edit the two draw functions to redesign. |

## How to use it with Claude

1. Open `simulator.html` in a browser to see the current screens and states.
2. Attach this folder to a Claude conversation and ask, e.g.:

   > Redesign the dashboard and boot screens in `simulator.html`. Stay within
   > `CONSTRAINTS.md` and only use the data in `data-contract.json`. Show the
   > result in the canvas, and give me the updated `drawDashboard` / `drawBoot`
   > functions plus the `Adafruit_GFX` mapping so I can paste it into the firmware.

3. Every JS draw call in the simulator has a 1:1 `tft.*` equivalent (see the
   mapping table in [CONSTRAINTS.md](CONSTRAINTS.md#firmware-mapping)), so a
   winning design ports to `claude_monitor.ino` mechanically.

## The hardware, in one line

160×128 landscape TFT, 16-bit color, no anti-aliasing, a 6×8 pixel font, ~30 fps,
a few KB of RAM for off-screen buffers. Design for *glanceability from a desk*.
