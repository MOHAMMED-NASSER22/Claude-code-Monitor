# Hardware sync reference (not used by TokenMaxxing.exe)

These files preserve logic that was developed alongside the Windows overlay but
belongs on the ESP8266 firmware path, not in the desktop app.

| File | Purpose |
|------|---------|
| `esp8266_auto_start_latch.patch` | Patch for `claude_monitor/claude_monitor.ino` — auto-start sets `sessionStarted` only after a successful anchor POST when usage still reports idle (matches overlay v1.4). Apply manually on the hardware branch when you want parity. |

The overlay implements the same latch in `claude_monitor_overlay.py` (`_fetch_claude_accounts`).
