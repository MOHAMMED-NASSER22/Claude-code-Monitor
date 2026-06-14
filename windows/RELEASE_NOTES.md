## Token Maxxing — Windows desktop overlay v1.1

Floating always-on-top widget that tracks your AI usage across **Claude** and **Cursor** in one cycling card:

- **Claude** — SESSION (5h) and WEEKLY (7d), same numbers as Claude Code `/usage` and the ESP8266 desk gadget
- **Cursor** — Auto + Composer pool and API pool (read from your local Cursor sign-in; no setup). Cursor cards are marked with the spinning Cursor cube.

### Requirements

- Windows 10/11 (64-bit)
- No Python install needed (standalone `.exe`)

### First run

1. Launch `TokenMaxxing.exe`
2. If Windows SmartScreen warns, choose **More info → Run anyway** (app is not code-signed)
3. Click **Open Browser**, sign in to Claude, paste the authorization code → **Submit**

### Controls

- **Drag** to move
- **Left-click** to cycle accounts
- **Scroll wheel** → opacity
- **Double-click** → reset opacity
- **Right-click** → refresh, settings, re-auth, exit

### Settings (right-click → ⚙)

- **Sources**: toggle Claude / Cursor on or off (show one or both), set a Cursor display name
- Per Claude account: display name + auto-start 5h session when idle (desk-gadget parity)
- Changing settings never calls the rate-limited Claude API; re-enabling Cursor repopulates instantly

### Notes

- Custom orange **spark** app icon on the `.exe`, taskbar, and window title (**Token Maxxing**)
- Auto-refresh every 2 minutes; manual refresh capped at 2/min. The Claude usage API
  is rate-limited (~6 req/5 min); on a 429 the app honors the server's `Retry-After`
  and pauses polling until the cooldown clears
- Cursor token is read locally (`%APPDATA%\Cursor\…\state.vscdb`) and only sent to
  Cursor's own usage endpoint; if Cursor isn't installed, no Cursor card appears
- Source: `windows/` in the repo
