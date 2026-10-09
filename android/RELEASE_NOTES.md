## Token Maxxing for Android v1.1

### What's new in v1.1

- **Cursor on your phone** — Add account → **Cursor** → Open cursor.com →
  confirm the login. The app picks it up by itself, no code to paste. Shows the
  Auto + Composer pool (with pace against your billing month) and the API pool.
  It's a separate login, so Cursor on your PC stays signed in.
- **Widget fixed for Samsung** — layouts no longer depend on the size the
  launcher reports, which Samsung's taller grid cells got wrong. The widget now
  picks the richest layout that fits:
  - 1×1 one number, 2×1 one account, 2×2 one account in detail,
  - 4×1 strip with every account (5h / 7d, auto / api),
  - 4×2 and up: every account with both limits and reset countdowns.
- Sharper bars and pace lines in the widget.
- Fixed the all-accounts widget failing to draw with three or more accounts.

## Token Maxxing for Android v1.0

Your Claude usage on your phone, with home-screen widgets. Same numbers and
pace rules as the Windows overlay.

### Install (Samsung Galaxy)

1. On the phone, open this release page and tap **TokenMaxxing.apk**.
2. Samsung blocks apps from outside the store while **Auto Blocker** is on:
   Settings → Security and privacy → Auto Blocker → off. (Turn it back on
   after installing; updates need it off again.)
3. Open the downloaded file and allow your browser to install apps when asked.
   If Play Protect warns, tap **More details → Install anyway**.
4. Open Token Maxxing → **Claude** → **Open claude.ai** → Authorize → copy the
   code → paste it in the app → **Add account**. For Cursor pick **Cursor** →
   **Open cursor.com** and confirm.
5. Add the widget: long-press the home screen → Widgets → Token Maxxing.

### What's in it

- **All your Claude accounts** — session, weekly and per-model (FABLE) limits,
  plan badge, reset countdowns.
- **Pace** — the faded line marks an even pace; `+13` means burning faster,
  `-38` means room to spare. Account detail says it in words ("at this rate it
  runs out about 2 days early").
- **One resizable widget** — 1×1 (one number), 2×1, 2×2 (one account), 4×1
  strip like the taskbar, 4×2 and up for all accounts. Pick the account for
  the small sizes in its detail screen ("Show on small widgets").
- **Notify at 85%** — optional, per account.
- Refreshes every 2 min while open and about every 15 min in the background.
- The phone signs in separately, so it never logs out the PC overlay.
- Tells you when a newer Android version is on GitHub.

