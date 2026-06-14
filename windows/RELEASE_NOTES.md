## Claude Monitor — Windows desktop overlay v1.0

Floating always-on-top widget for Claude **SESSION (5h)** and **WEEKLY (7d)** usage — same numbers as Claude Code `/usage` and the ESP8266 desk gadget.

### Requirements

- Windows 10/11 (64-bit)
- No Python install needed (standalone `.exe`)

### First run

1. Launch `ClaudeMonitor.exe`
2. If Windows SmartScreen warns, choose **More info → Run anyway** (app is not code-signed)
3. Click **Open Browser**, sign in to Claude, paste the authorization code → **Submit**

### Controls

- **Drag** to move
- **Left-click** to cycle accounts
- **Scroll wheel** → opacity
- **Double-click** → reset opacity
- **Right-click** → refresh, settings, re-auth, exit

### Notes

- Auto-refresh every 1 minute; manual refresh capped at 2/min (API rate limits)
- Settings: display name + auto-start 5h session when idle (desk-gadget parity)
- Source: `windows/` in the repo
