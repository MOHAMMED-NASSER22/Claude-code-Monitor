# Token Maxxing — Windows Desktop

Self-contained Windows desktop app: a floating always-on-top widget showing Claude **SESSION (5h)** and **WEEKLY (7d)** usage plus Cursor **Auto** and **API** pool usage.

Everything needed to build and run the `.exe` lives in this folder. The app does not import or depend on `token_bridge.py`, `claude_monitor/`, or any other repo path at runtime or build time.

| File | Role |
|------|------|
| `claude_monitor_overlay.py` | Main app (PyQt6) |
| `cursor_usage.py` | Optional Cursor Auto + API usage source |
| `make_icon.py` | Generates `spark.ico` at build time |
| `pyi_rth_appusermodelid.py` | PyInstaller runtime hook (taskbar icon) |
| `build.bat` | One-file `.exe` build |
| `requirements.txt` | Python dependencies |
| `version_info.txt` | Windows PE version resource |
| `token_bridge.py` | Optional LAN bridge (same credentials; overlay-aligned rate limits). Not required for the `.exe`. |
| `reference/` | Firmware patch notes for ESP8266 parity — not bundled in the `.exe` |

This folder is **isolated from the firmware** (`claude_monitor/`, `token_bridge.py`, etc.). Nothing here is required for the ESP8266 build, and changes here should not touch those paths.

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

Output: `windows\dist\TokenMaxxing.exe`

## First-time sign-in

No Docker or `mint_token.sh` required. On first launch:

1. Click **Open Browser** and sign in to Claude
2. Paste the authorization code → **Submit**

Credentials are saved to `%USERPROFILE%\.claude_usage_bridge\credentials.json`.

You can also mint credentials with the repo’s `mint_token.sh` at the project root — the overlay reads the same files.

## Controls

| Action | Effect |
|--------|--------|
| Drag | Move widget (full overlay only) |
| Left-click | Cycle accounts (multi-account) |
| Scroll wheel | Adjust transparency |
| Ctrl+scroll | Resize the full overlay (100–300%, remembered) |
| Double-click | Full: reset transparency. Mini: expand to the full overlay |
| Right-click → **Minimize to clock** | Collapse to a one-line strip **on** the taskbar, left of the clock (`S 38%  W 62%`, or `A` / `P` on a Cursor card) |
| Right-click → **Expand** | Restore the full overlay (also from mini) |
| Right-click | Menu: refresh, settings, tutorial, re-auth, size, opacity, exit |
| Right-click → **Show tutorial** | Replay the first-run tour (scroll to fade, Ctrl+scroll to resize, then the rest) |
| Update bar (v1.5+) | Appears when a newer GitHub Release exists. Click to open it; ✕ dismisses that version |

Mini mode is remembered in `overlay_config.json` (`compact_mode`) and restored on the next launch.

## Settings (right-click → ⚙ Settings)

**Per Claude account** (shown only on a Claude card), stored in that account's credentials JSON:

| Field | Key | Default |
|-------|-----|---------|
| Display name | `name` | from filename |
| Auto-start 5h session when idle | `autoStartSession` | on |

**Auto-start** matches the desk-gadget behavior conceptually: when SESSION is idle, sends one minimal Haiku message (~22 tokens) to anchor a new 5h block.

**Sources** (global), stored in `%USERPROFILE%\.claude_usage_bridge\overlay_config.json`:

| Field | Key | Default |
|-------|-----|---------|
| Show Claude | `show_claude` | on |
| Show Cursor (Auto + API) | `show_cursor` | on |
| Cursor display name | `cursor_name` | account email |
| Mini mode beside the clock | `compact_mode` | off |
| Overlay size | `overlay_scale` | 2.0 (200%) |
| Dismissed update | `dismissed_update` | (empty) |
| First-run tutorial completed | `tutorial_done` | off |

Toggle either source off to hide it (or to show just one). Saving re-fetches so the cards update immediately.

## Cursor usage (Auto + API pools)

If [Cursor](https://cursor.com) is installed and signed in on the same machine, the
overlay adds a **Cursor** card to the cycle showing your two monthly pools:

| Card | Source |
|------|--------|
| **AUTO** | Auto + Composer pool (`autoPercentUsed`) |
| **API**  | API pool — Claude/GPT/Gemini/Grok (`apiPercentUsed`) |

The token is read locally from Cursor's own store
(`%APPDATA%\Cursor\User\globalStorage\state.vscdb`, key `cursorAuth/accessToken`)
read-only, and only ever sent to Cursor's own usage endpoint
(`cursor.com/api/dashboard/get-current-period-usage`). Nothing to configure — if
Cursor isn't installed, no extra card appears. The pool resets badge is `mo`
(monthly billing cycle).

## Polling & rate limits

- Auto-refresh every **2 minutes**. Opening the overlay does **not** fetch again
  if a snapshot from the last 2 minutes is already saved
  (`overlay_usage_cache.json` next to the config).
- Header ring shows time until next refresh
- Manual **Refresh now**: max **2 per minute**. The Claude usage API is rate-limited
  (~6 req/5 min); on HTTP 429 the app honors the server's `Retry-After`, saves
  that cooldown, and will not retry until it expires — even if you quit and
  reopen. Cached numbers stay on screen.

## Design

UI layout mirrors `../simulator.html` and `../design-package/` (160×128 TFT design at 2× scale).

## Optional: LAN bridge on Windows

If you also run an ESP8266 desk gadget from this PC, use the copy in this folder
(not the repo-root `token_bridge.py`):

```powershell
cd windows
python token_bridge.py
```

## Distribute

`dist/` and `build/` are gitignored. Attach `TokenMaxxing.exe` to a [GitHub Release](https://github.com/MOHAMMED-NASSER22/Claude-code-Monitor/releases). From v1.5 the overlay checks that page for newer versions.

## Files

See the table at the top of this README. Optional: `TokenMaxxing.spec` (PyInstaller spec; `build.bat` uses CLI flags directly).
