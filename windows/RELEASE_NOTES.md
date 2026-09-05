## Token Maxxing — Windows desktop overlay v1.6

### What's new in v1.6

- **Release 1.6** — same overlay as v1.5 (mini mode beside the clock, resize,
  tutorial, in-app update check). Publishing this tag makes running **v1.5**
  builds show the update bar: **Update 1.6 · click to download**.

### What's new in v1.5

- **Minimize to clock** — right-click the overlay and choose **Minimize to clock**
  to collapse it into a one-line strip on the taskbar, just left of the system
  clock (TrafficMonitor / NetSpeed style). Shows `S 38%  W 62%` for Claude or
  `A` / `P` for Cursor Auto / API, plus the spark or cube. Double-click or
  **Expand** restores the full widget. The mode is remembered across launches.
- **Resize the overlay** — right-click → **Size** (100–300%), or **Ctrl+scroll**
  on the full widget. Size is remembered. Mini mode stays taskbar-height.
- **Update check** — from v1.5 the overlay checks GitHub every 6 hours (and shortly
  after launch). If a newer release is published, a small bar appears:
  **Update 1.x · click to download**. ✕ dismisses that version. Mini mode shows
  a blue tick on the left; right-click → **Get v1.x** opens the release.
- **First-run tutorial** — after sign-in (and for existing installs until skipped),
  a short in-widget tour. Scroll to fade and Ctrl+scroll to resize come first
  (try them on those screens). Right-click → **Show tutorial** to replay.

### What's new in v1.4

- **Auto-start once per idle period** — the overlay sets the `sessionStarted` latch
  only after a successful anchor POST when the usage API still reports idle, instead
  of retrying every poll or getting stuck forever.

### What's new in v1.3

- **Fixed auto-start stuck on IDLE** — if auto-start sent the anchor message but the
  usage API still reported no active 5h block, a stale `sessionStarted` flag blocked
  all retries. The overlay now retries until the SESSION clock appears.

### What's new in v1.2

- **Fixed multi-monitor scaling** — dragging the widget onto a screen with a
  different DPI / aspect ratio no longer makes it expand, lose its glassy
  background, or crash. The overlay now stays a fixed size across all displays.
- **Fixed false "Rate limited" after sleep** — the rate-limit cooldown is now
  tracked on the wall clock, so it expires while the PC sleeps. Waking the
  machine no longer leaves the app stuck showing "Rate limited" until relaunch.

Floating always-on-top widget that tracks your AI usage across **Claude** and **Cursor** in one cycling card:

- **Claude** — SESSION (5h) and WEEKLY (7d), same numbers as Claude Code `/usage` and the ESP8266 desk gadget
- **Claude session reset** — optional **Auto-start 5h session when idle**: when your SESSION window has expired, the app sends one minimal Haiku message (~22 tokens) to anchor a fresh 5h block automatically (same behavior as the desk gadget)
- **Cursor** — Auto + Composer pool and API pool (read from your local Cursor sign-in; no setup). Cursor cards are marked with the spinning Cursor cube.

### Requirements

- Windows 10/11 (64-bit)
- No Python install needed (standalone `.exe`)

### First run

1. Download and run **`TokenMaxxing.exe`**
2. If Windows SmartScreen warns, choose **More info → Run anyway** (app is not code-signed)
3. Click **Open Browser**, sign in to Claude, paste the authorization code → **Submit**

### Controls

- **Drag** to move (full overlay)
- **Left-click** to cycle accounts
- **Scroll wheel** → opacity
- **Ctrl+scroll** → resize full overlay (100–300%)
- **Double-click** → reset opacity (full) or expand (mini)
- **Right-click** → minimize to clock / expand, size, tutorial, refresh, settings, re-auth, exit

### Settings (right-click → ⚙)

- **Sources**: toggle Claude / Cursor on or off (show one or both), set a Cursor display name
- Per Claude account: display name + **Auto-start 5h session when idle** (on by default) — restarts the SESSION clock when idle so you begin a new 5h window without opening Claude Code
- Changing settings never calls the rate-limited Claude API; re-enabling Cursor repopulates instantly

### Notes

- Auto-refresh every 2 minutes; manual refresh capped at 2/min. The Claude usage API
  is rate-limited (~6 req/5 min); on a 429 the app honors the server's `Retry-After`
  and pauses polling until the cooldown clears
- Cursor token is read locally (`%APPDATA%\Cursor\…\state.vscdb`) and only sent to
  Cursor's own usage endpoint; if Cursor isn't installed, no Cursor card appears
- Source: `windows/` in the repo (self-contained; root `token_bridge.py` / firmware unchanged)
- Optional `windows/token_bridge.py` includes the same rate-limit and poll fixes for LAN bridge users
