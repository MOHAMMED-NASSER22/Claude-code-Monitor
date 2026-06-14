# OAuth-token bridge — working notes

How the device gets the **real** Claude plan 5h/7d limit %s without a browser.
Replaces the browser-scrape approach (see `CLOUDFLARE_NOTES.md`, now historical).

_Last updated: 2026-06-14._

## Architecture

```
mint_token.sh ─▶ Docker (node:slim + Claude Code) ─login─▶ ~/.claude/.credentials.json
                                                                   │ docker cp
                                          ~/.claude_usage_bridge/credentials.json
                                                                   │
                                  token_bridge.py (Mac) ──refresh──▶ platform.claude.com
                                          │  /token (LAN)
                                          ▼
                                      D1 Mini ──HTTPS──▶ api.anthropic.com/api/oauth/usage
```

- **mint_token.sh** runs `claude auth login --claudeai` in a throwaway Linux
  container so you log in once; copies the container's `~/.claude/.credentials.json`
  to the host. On Linux (no Keychain / no libsecret in the slim image) that file
  is plain JSON — the whole reason for the container.
  - **Login flow is paste-the-code, not localhost-callback** (verified): the CLI
    prints `https://claude.com/cai/oauth/authorize?...&redirect_uri=https://platform.claude.com/oauth/code/callback&code=true`,
    you approve, claude.com shows a code, you paste it into the terminal. So no
    `-p`/port mapping is needed — important, since Docker Desktop on Mac can't
    route a localhost callback into the container anyway. PKCE (S256) is used.
  - Scopes granted: `user:inference user:profile user:sessions:claude_code
    user:mcp_servers user:file_upload org:create_api_key`.
- **token_bridge.py** (pure stdlib) refreshes the access token(s) before expiry,
  persists each rotated bundle, and serves them to the device on `/token`. Also
  serves `/usage` (proxy) so the old firmware path still works.
- **Device** (`USE_DIRECT_API 1`) fetches the bearer(s) from `/token`, then calls
  `api.anthropic.com/api/oauth/usage` itself over HTTPS.

### Multiple accounts

- `./mint_token.sh <name>` writes `credentials-<name>.json`; the bridge picks up
  every `credentials*.json` (bare `credentials.json` sorted first). The device's
  existing dwell/rotate logic cycles through `accounts[]`.
- `/token` returns `{ beta, usage_url, accounts:[{label, token, expires_at}, ...] }`.
  In direct mode the firmware loops it, making **one HTTPS usage call per account,
  sequentially** — so peak TLS RAM stays at a single connection regardless of
  account count (it's N× the time per poll, not N× the memory).
- Labels (what the device shows), highest precedence first:
  `ACCOUNT_LABELS="Personal,Work"` env (filename order) → the **minted name**
  (top-level `"name"` in the credential file, set by `mint_token.sh`) → the
  filename slug → the account email from `/api/oauth/profile`. The name is
  preserved across token refreshes (refresh rewrites the file but keeps top-level
  fields), and the email is only fetched when it would actually be the label.

## Constants — extracted from the shipping Claude Code binary (NOT guessed)

Pulled via `strings` from
`~/Library/Application Support/Claude/claude-code/<ver>/claude.app/Contents/MacOS/claude`:

| Thing | Value |
|---|---|
| Usage endpoint | `GET https://api.anthropic.com/api/oauth/usage` |
| Required header | `anthropic-beta: oauth-2025-04-20` + `Authorization: Bearer <accessToken>` |
| Token refresh | `POST https://platform.claude.com/v1/oauth/token` |
| OAuth client_id | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| Credential file (Linux) | `~/.claude/.credentials.json` |
| Credential shape | `{ "claudeAiOauth": { accessToken, refreshToken, expiresAt(ms), scopes, subscriptionType } }` |
| Other oauth paths seen | `/api/oauth/{account,profile,organizations,validate}` |

**Refresh request body** (JSON):
`{ "grant_type": "refresh_token", "refresh_token": <rt>, "client_id": "<id>" }`
→ returns `{ access_token, refresh_token, expires_in, scope? }`. The refresh token
**rotates — CONFIRMED live**: one refresh changed both the access token AND the
refresh token (each refresh invalidates the previous refresh token). Consequences:
whoever refreshes MUST persist the new refresh token, and **only one party may
refresh a given account** (bridge OR device, never both — see standalone mode).

**Usage response shape** (the fields we use):
```json
{ "five_hour":  { "utilization": <0-100>, "resets_at": <iso-or-epoch> },
  "seven_day":  { "utilization": <0-100>, "resets_at": <iso-or-epoch> },
  "seven_day_opus": { ... }, "seven_day_sonnet": { ... } }
```
We map `five_hour`→SESSION card, `seven_day`→WEEKLY card. The opus/sonnet
sub-windows are available in `/usage_raw` if you ever want a third card.

## Gotchas confirmed on real hardware (2026-06-14)

- **`/api/oauth/usage` is RATE-LIMITED (HTTP 429).** Confirmed live: aggressive
  polling (10s, ×2 accounts, plus a polling bridge and ad-hoc test calls) drove
  the endpoint to `429` for ~all requests until it cooled down. Keep total load
  low: device polls every **5 min** (`POLL_MS`), the bridge stays PROVISION_ONLY
  (no background poll), and don't run extra pollers/test loops against it. If you
  see `429`, stop everything and wait ~15-30 min for it to reset.
- **The API response is `Transfer-Encoding: chunked`.** The firmware MUST parse it
  via `HTTPClient::getString()` (which de-chunks), NOT `getStream()` — feeding the
  raw chunk framing to ArduinoJson yields `InvalidInput`. (Same for the refresh
  response from `platform.claude.com`.)
- **Heap is fine** (~32-34 KB free during a fetch) — the earlier OOM worry was
  unfounded; TX buffer is shrunk to 512 and MFLN (if supported) shrinks RX too.

## Auto-start idle session (AUTO_START_SESSION) — verified live

When an account's `five_hour.resets_at` is `null` (no active 5h block), the firmware
sends ONE minimal inference request to anchor a new block. **Verified live**: a
single call flipped `resets_at` from `null` → a timestamp ~5h out, at ~0% usage.

- **Endpoint/shape that works** (the firmware floor — anything less 403s):
  `POST https://api.anthropic.com/v1/messages`, headers `Authorization: Bearer <oat>`,
  `anthropic-version: 2023-06-01`, `anthropic-beta: oauth-2025-04-20`,
  `Content-Type: application/json`. Body:
  `{"model":"claude-haiku-4-5","max_tokens":1,"system":"You are Claude Code, Anthropic's official CLI for Claude.","messages":[{"role":"user","content":"hi"}]}`.
- The **system prompt string is mandatory** — the OAuth subscription token is gated
  to Claude Code; without that exact first system block the request is rejected.
- Cost: ~22 input + 1 output Haiku tokens per start. Cheapest model.
- Firmware fires once per idle period (a per-account `sessionStarted` latch that
  rearms when a block reappears) and only after NTP has synced.
- **Intended-use note:** this drives the *subscription* token from an automated
  device presenting as Claude Code — your own account, benign, but outside the
  token's interactive intended use; Anthropic could change/curb it.

## Caveats / things to verify on real hardware

- **`resets_at` format is unconfirmed.** Both `token_bridge.py` (`_to_minutes`)
  and the firmware (`resetsEpoch`) accept ISO-8601, epoch-seconds, AND epoch-ms,
  so either way works. If the countdown is wrong, dump `/usage_raw` and check.
- **Device TLS RAM.** `api.anthropic.com` may not support MFLN, in which case
  BearSSL needs the full ~16 KB RX buffer. The firmware probes MFLN and only
  shrinks the buffer if the server agrees; otherwise it uses defaults. If you see
  heap-exhaustion resets, set `USE_DIRECT_API 0` (proxy mode — no device TLS).
- **`setInsecure()`** — the device skips cert validation (personal-gadget
  trade-off). To pin instead, set a trust anchor for the ISRG/Amazon root.
- **NTP required in direct mode** — the API returns absolute reset times; the
  device needs a clock to make a countdown. `configTime(0,0,...)` (UTC) handles
  it. The first poll or two may show `0m` until NTP syncs.
## Standalone mode (DEVICE_MANAGES_TOKENS) — no Mac after setup

The default firmware (`DEVICE_MANAGES_TOKENS 1`) makes the device fully
independent of the Mac once provisioned:

1. **One-time hand-off.** The device GETs the bridge's `/provision`, which returns
   the full bundle per account `{label, access_token, refresh_token, expires_at}`
   (+ `client_id`, `token_url`), and stores it in **LittleFS** (`/creds.json`).
2. **Device owns refresh.** On a 401 it POSTs the refresh itself to
   `platform.claude.com/v1/oauth/token`, then **immediately** rewrites
   `/creds.json` (temp file + atomic rename) with the rotated token.
3. **Bridge must NOT also refresh** (rotation would invalidate the device's
   token) — it now defaults to provision-only, so plain `python3 token_bridge.py`
   serves `/provision` but never runs the background refresh. After the device
   has provisioned, you can stop the bridge entirely. (Set `PROVISION_ONLY=0`
   only for `DEVICE_MANAGES_TOKENS 0`, where the bridge is the refresher.)

**Exclusive-ownership rule:** once the device provisions, the bridge's stored
refresh token goes stale (the device rotated it). So re-provisioning only helps
*after a re-mint*. The firmware re-tries `/provision` automatically when an
account is locked out — so recovery = re-mint + start a PROVISION_ONLY bridge.

**Residual risk:** a power loss in the ~millisecond window between the refresh
response and the LittleFS rename can strand the account (old refresh token already
spent server-side). Recovery is the same: `./mint_token.sh <name>` → re-provision.

To keep refresh on the Mac instead (more robust, Mac always-on), set
`DEVICE_MANAGES_TOKENS 0` (device fetches a bearer from `/token` each poll).

## When the token stops working

1. **Standalone device** stuck on "Waiting for API...": an account's refresh token
   was spent/revoked. Re-mint it (`./mint_token.sh <name>`), start the bridge
   (`python3 token_bridge.py`, provision-only by default), and the device
   re-provisions itself on the next poll.
2. **Bridge mode**: check `http://<mac>:8088/` — if it errors, hit `/refresh`.
   A persistent 4xx means the refresh token was revoked → re-run `./mint_token.sh`.
3. Confirm Docker Desktop is running before `mint_token.sh`.
