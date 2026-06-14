# Claude Usage Monitor — Windows Desktop

Self-contained Windows desktop app: a floating always-on-top widget showing Claude **SESSION (5h)** and **WEEKLY (7d)** usage — the same numbers as Claude Code `/usage` and the ESP8266 desk gadget.

This folder is **isolated from the firmware** (`claude_monitor/`, `token_bridge.py`, etc.). Nothing here is required for the ESP8266 build.

## Requirements

- **Windows 10/11** (64-bit)
- To build from source: Python 3.10+ and pip

## Run from source

```powershell
cd windows
pip install -r requirements.txt
python claude_monitor_overlay.py
```

## Build `.exe`

```powershell
cd windows
.\build.bat
```

Output: `windows\dist\ClaudeMonitor.exe`

## First-time sign-in

No Docker or `mint_token.sh` required. On first launch:

1. Click **Open Browser** and sign in to Claude
2. Paste the authorization code → **Submit**

Credentials are saved to `%USERPROFILE%\.claude_usage_bridge\credentials.json`.

You can also mint credentials with the repo’s `mint_token.sh` at the project root — the overlay reads the same files.

## Controls

| Action | Effect |
|--------|--------|
| Drag | Move widget |
| Left-click | Cycle accounts (multi-account) |
| Scroll wheel | Adjust transparency |
| Double-click | Reset transparency |
| Right-click | Menu: refresh, settings, re-auth, opacity, exit |

## Settings (right-click → ⚙ Settings)

Per account, stored in the credentials JSON:

| Field | Key | Default |
|-------|-----|---------|
| Display name | `name` | from filename |
| Auto-start 5h session when idle | `autoStartSession` | on |

**Auto-start** matches the ESP8266 `AUTO_START_SESSION` behavior: when SESSION is idle, sends one minimal Haiku message (~22 tokens) to anchor a new 5h block.

## Polling & rate limits

- Auto-refresh every **1 minute**
- Header ring shows time until next refresh
- Manual **Refresh now**: max **2 per minute** (API returns HTTP 429 if polled too aggressively)

## Design

UI layout mirrors `../simulator.html` and `../design-package/` (160×128 TFT design at 2× scale).

## Distribute

`dist/` and `build/` are gitignored. Attach `ClaudeMonitor.exe` to a [GitHub Release](https://github.com/aabdlwahab/Claude-code-Monitor/releases) — see issue #1.

## Files

| File | Purpose |
|------|---------|
| `claude_monitor_overlay.py` | Main app (PyQt6) |
| `build.bat` | PyInstaller one-file build |
| `requirements.txt` | Python dependencies |
| `ClaudeMonitor.spec` | PyInstaller spec (optional; `build.bat` uses CLI flags) |
