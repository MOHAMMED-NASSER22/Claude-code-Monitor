#!/usr/bin/env python3
"""
Claude usage bridge (OAuth-token edition, multi-account).

Replaces the old browser-scrape bridge (bridge.py). Instead of driving real
Chrome sessions through Cloudflare to read claude.ai's same-origin API, this:

  1. Reads the OAuth credential bundle(s) minted by mint_token.sh -- one per
     Claude account, as ~/.claude_usage_bridge/credentials*.json. Each holds an
     accessToken, a refreshToken, and an expiresAt.
  2. Keeps each accessToken fresh by refreshing it against the OAuth token
     endpoint before it expires (the one thing an ESP8266 can't do itself --
     refresh tokens are single-use and must be persisted as they rotate).
  3. Reads the real plan 5h/7d utilization from Claude Code's own usage endpoint,
     api.anthropic.com/api/oauth/usage -- no browser, no Cloudflare.

It then serves, over the LAN:
  GET /token   -> { beta, usage_url, accounts:[{label, token, expires_at}, ...] }
                  so the DEVICE can hit api.anthropic.com/api/oauth/usage DIRECTLY
                  for EACH account (the chosen design).
  GET /usage   -> the compact multi-account device JSON the old firmware expects
                  (accounts[] with session/weekly), built from the API. Proxy
                  fallback + powers the debug page.
  GET /usage_raw -> the raw API response per account (for tuning / curiosity).
  GET /         -> human-readable debug page.
  GET /refresh -> force a token refresh for every account now.

Add accounts with:  ./mint_token.sh work   (writes credentials-work.json)
All constants below (endpoints, client id, beta header, credential-file shape)
were taken from the shipping Claude Code binary, not guessed.

No third-party deps -- pure stdlib. Just:  python3 token_bridge.py
"""

import glob
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Config (override via environment variables)
# ---------------------------------------------------------------------------
PORT         = int(os.environ.get("PORT", "8088"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "120"))        # how often to re-read usage
                                                                 # (2 min; ~6 req/300s budget,
                                                                 #  60s sat on the 429 edge)
CRED_DIR     = os.path.expanduser(os.environ.get("CRED_DIR", "~/.claude_usage_bridge"))
# Force an exact set of credential files (os.pathsep-separated). If unset, every
# CRED_DIR/credentials*.json is used -- so ./mint_token.sh <name> just works.
CRED_FILES_ENV = os.environ.get("CREDENTIALS_FILE", "").strip()
# Per-account display labels, in sorted-filename order, e.g. "Personal,Work".
# If unset, each account's real email (from the API) is used.
ACCOUNT_LABELS = [x.strip() for x in os.environ.get("ACCOUNT_LABELS", "").split(",") if x.strip()]
# Refresh this many seconds BEFORE the token's stated expiry (clock-skew margin).
REFRESH_SKEW_SEC = int(os.environ.get("REFRESH_SKEW_SEC", "300"))
# Kept only for the device payload's (cosmetic) budget fields; the % bars now
# come straight from the API's utilization, not from a cost budget.
SESSION_COST_BUDGET = float(os.environ.get("SESSION_COST_BUDGET", "40"))
WEEKLY_COST_BUDGET  = float(os.environ.get("WEEKLY_COST_BUDGET", "300"))
# PROVISION_ONLY (DEFAULT ON): don't run the background refresh poll. This matches
# the firmware default (DEVICE_MANAGES_TOKENS=1) where the DEVICE owns refresh --
# the bridge only hands out the bundle on /provision and never rotates the
# (single-use) refresh token itself, so it can't fight the device over it.
# Set PROVISION_ONLY=0 to run the bridge as the refresher (DEVICE_MANAGES_TOKENS=0).
PROVISION_ONLY = os.environ.get("PROVISION_ONLY", "1").strip().lower() not in ("0", "false", "no")

# --- constants extracted from the Claude Code binary (do not guess these) ----
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"          # Claude Code public OAuth client
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"    # refresh endpoint
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"     # plan 5h/7d utilization
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"  # best-effort account email
BETA_HEADER = "oauth-2025-04-20"                            # required anthropic-beta value
USER_AGENT = "claude-usage-bridge/1.0"

_state_lock  = threading.Lock()
_state       = {}                        # path -> {usage, email, ts, err}
_locks       = {}                        # path -> threading.Lock (serialize refresh/writes)
_locks_guard = threading.Lock()
_shutdown    = threading.Event()

# ---------------------------------------------------------------------------
# Which credential files / accounts exist
# ---------------------------------------------------------------------------
def credential_files():
    if CRED_FILES_ENV:
        return [p for p in CRED_FILES_ENV.split(os.pathsep) if p]
    # bare credentials.json (the default/first account) sorts first, then named
    # ones alphabetically -- otherwise ASCII puts "credentials-work.json" before
    # "credentials.json" and the named account would wrongly become primary.
    files = glob.glob(os.path.join(CRED_DIR, "credentials*.json"))
    return sorted(files, key=lambda p: (os.path.basename(p) != "credentials.json",
                                        os.path.basename(p)))

def _label_no_email(path, idx):
    """The label derivable WITHOUT a network call, or None if only email is left.
    Precedence: ACCOUNT_LABELS env -> minted "name" -> filename slug (title-cased)."""
    if idx < len(ACCOUNT_LABELS) and ACCOUNT_LABELS[idx]:
        return ACCOUNT_LABELS[idx]
    name = account_name(path)                    # top-level "name" stored at mint time
    if name:
        return name
    m = re.match(r"credentials-(.+)\.json$", os.path.basename(path))
    if m:                                        # "credentials-work.json" -> "Work"
        return m.group(1).replace("-", " ").replace("_", " ").title()
    return None

def label_for(path, idx, email):
    # The account NAME (env / minted / slug) wins over the email.
    return _label_no_email(path, idx) or email or "Claude"

def _lock_for(path):
    with _locks_guard:
        if path not in _locks:
            _locks[path] = threading.Lock()
        return _locks[path]

# ---------------------------------------------------------------------------
# Minimal HTTP/JSON helper (stdlib only)
# ---------------------------------------------------------------------------
_SSL_CTX = ssl.create_default_context()

# Set from a 429's Retry-After header; the poller backs off until then.
_rate_limit_until = 0.0          # time.monotonic() value

def _rate_limited_remaining():
    return max(0.0, _rate_limit_until - time.monotonic())

def _http_json(method, url, headers=None, body=None, form=None, timeout=25):
    """Return (status_code, parsed_json_or_text). Never raises on HTTP errors.
    body -> JSON-encoded; form -> application/x-www-form-urlencoded."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    else:
        data = None
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        if e.code == 429:
            global _rate_limit_until
            try:
                ra = float(e.headers.get("Retry-After", "") or 0)
            except (TypeError, ValueError):
                ra = 0.0
            # Server sends Retry-After: ~299s. Fall back to 300s if absent.
            _rate_limit_until = time.monotonic() + (ra if ra > 0 else 300.0)
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return 0, str(e)

# ---------------------------------------------------------------------------
# Credential files (Claude Code's ~/.claude/.credentials.json shape)
#   { "claudeAiOauth": { accessToken, refreshToken, expiresAt(ms), scopes, ... } }
# We tolerate a bare {...} too (already unwrapped).
# ---------------------------------------------------------------------------
def load_doc(path):
    with open(path) as f:
        return json.load(f)

def save_doc(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)                      # atomic; never leave a half-written file
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def load_oauth(path):
    doc = load_doc(path)
    return doc.get("claudeAiOauth", doc) if isinstance(doc, dict) else doc

def account_name(path):
    """The display name stored at mint time (top-level "name"), or None."""
    try:
        doc = load_doc(path)
        return doc.get("name") if isinstance(doc, dict) else None
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Token refresh + access-token accessor (per account file)
# ---------------------------------------------------------------------------
def _refresh(path, oauth):
    """POST the refresh token, persist the rotated bundle, return updated oauth."""
    rt = oauth.get("refreshToken")
    if not rt:
        raise RuntimeError("no refreshToken -- re-run ./mint_token.sh for this account")
    payload = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": CLIENT_ID}
    status, resp = _http_json("POST", TOKEN_URL, body=payload)
    if status in (400, 415, 422):              # some OAuth servers want form-encoded
        status, resp = _http_json("POST", TOKEN_URL, form=payload)
    if status != 200 or not isinstance(resp, dict) or "access_token" not in resp:
        raise RuntimeError(f"token refresh failed (HTTP {status}): {resp}")
    oauth["accessToken"] = resp["access_token"]
    if resp.get("refresh_token"):              # refresh tokens rotate -- keep the new one
        oauth["refreshToken"] = resp["refresh_token"]
    if resp.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(resp["expires_in"]) * 1000
    if resp.get("scope"):
        oauth["scopes"] = resp["scope"].split()
    # persist the rotated bundle, PRESERVING top-level fields like "name"
    try:
        doc = load_doc(path)
        if not isinstance(doc, dict) or "claudeAiOauth" not in doc:
            doc = {}
    except Exception:
        doc = {}
    doc["claudeAiOauth"] = oauth
    save_doc(path, doc)
    return oauth

def get_access_token(path, force=False):
    """Current access token for an account, refreshing if expired/near expiry."""
    with _lock_for(path):
        oauth = load_oauth(path)
        expires_at = oauth.get("expiresAt", 0)              # epoch ms
        near_expiry = (expires_at - time.time() * 1000) < REFRESH_SKEW_SEC * 1000
        if force or near_expiry:
            oauth = _refresh(path, oauth)
        return oauth["accessToken"], oauth.get("expiresAt", 0)

# ---------------------------------------------------------------------------
# Usage API
# ---------------------------------------------------------------------------
def _auth_headers(token):
    return {"Authorization": f"Bearer {token}", "anthropic-beta": BETA_HEADER}

def fetch_usage(path):
    """GET /api/oauth/usage; retry once with a forced refresh if the token is rejected."""
    token, _ = get_access_token(path)
    status, resp = _http_json("GET", USAGE_URL, headers=_auth_headers(token))
    if status in (401, 403):                                # token stale/revoked -> refresh & retry
        token, _ = get_access_token(path, force=True)
        status, resp = _http_json("GET", USAGE_URL, headers=_auth_headers(token))
    if status != 200 or not isinstance(resp, dict):
        raise RuntimeError(f"usage request failed (HTTP {status}): {resp}")
    return resp

def fetch_email(path):
    """Best-effort account email for the device header label. Never raises."""
    try:
        token, _ = get_access_token(path)
        status, resp = _http_json("GET", PROFILE_URL, headers=_auth_headers(token))
        if status == 200 and isinstance(resp, dict):
            acct = resp.get("account") or {}
            return (resp.get("email") or resp.get("email_address")
                    or acct.get("email_address") or acct.get("email"))
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Map the API response into the device payload (same contract as the old bridge)
# ---------------------------------------------------------------------------
def _to_minutes(resets_at):
    """resets_at as epoch (s/ms) or ISO string -> minutes from now (or None)."""
    if resets_at is None:
        return None
    try:
        if isinstance(resets_at, (int, float)):
            ts = resets_at / 1000.0 if resets_at > 1e11 else float(resets_at)
        else:
            ts = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00")).timestamp()
        return max(0, int((ts - time.time()) // 60))
    except Exception:
        return None

def _pct(v):
    return 0 if v is None else max(0, min(100, int(round(v))))

def _window(block):
    block = block or {}
    return _pct(block.get("utilization")), _to_minutes(block.get("resets_at"))

def _account_entry(path, idx):
    with _state_lock:
        st = dict(_state.get(path, {}))
    usage, email, ts, err = st.get("usage"), st.get("email"), st.get("ts", 0), st.get("err")
    label = label_for(path, idx, email)
    if usage:
        s_pct, s_reset = _window(usage.get("five_hour"))
        w_pct, w_reset = _window(usage.get("seven_day"))
        session = {"cost": 0, "tokens": 0, "budget": SESSION_COST_BUDGET, "pct": s_pct,
                   "resets_in_min": s_reset or 0, "active": usage.get("five_hour") is not None}
        weekly  = {"cost": 0, "tokens": 0, "budget": WEEKLY_COST_BUDGET, "pct": w_pct,
                   "resets_in_min": w_reset or 0, "resets_at": "", "days": 7}
    else:
        session = {"cost": 0, "tokens": 0, "budget": SESSION_COST_BUDGET, "pct": 0,
                   "resets_in_min": 0, "active": False}
        weekly  = {"cost": 0, "tokens": 0, "budget": WEEKLY_COST_BUDGET, "pct": 0,
                   "resets_in_min": 0, "resets_at": "", "days": 7}
    return {"account": label, "idx": idx + 1, "active": idx == 0,
            "logged_in": bool(usage), "note": err or "",
            "stale_sec": int(time.time() - ts) if ts else None,
            "session": session, "weekly": weekly}

def build_payload():
    accounts = [_account_entry(p, i) for i, p in enumerate(credential_files())]
    primary = accounts[0] if accounts else None
    ok = any(a["logged_in"] for a in accounts)
    if primary is None:
        empty_s = {"cost": 0, "tokens": 0, "budget": SESSION_COST_BUDGET, "pct": 0,
                   "resets_in_min": 0, "active": False}
        empty_w = {"cost": 0, "tokens": 0, "budget": WEEKLY_COST_BUDGET, "pct": 0,
                   "resets_in_min": 0, "resets_at": "", "days": 7}
        return {"ok": False, "ts": 0, "account": "Claude", "note": "no credentials",
                "session": empty_s, "weekly": empty_w, "accounts": []}
    return {"ok": ok, "ts": int(time.time()), "account": primary["account"],
            "session": primary["session"], "weekly": primary["weekly"], "accounts": accounts}

# ---------------------------------------------------------------------------
# Background poller: keep _state fresh so the HTTP handlers are instant
# ---------------------------------------------------------------------------
def poll_loop():
    first = True
    while not _shutdown.is_set():
        files = credential_files()
        summary = []
        for idx, path in enumerate(files):
            try:
                usage = fetch_usage(path)
                with _state_lock:
                    email = (_state.get(path) or {}).get("email")
                # only spend a profile API call if the email would actually be the
                # label (i.e. no env label, no minted name, no filename slug)
                if not email and _label_no_email(path, idx) is None:
                    email = fetch_email(path)
                with _state_lock:
                    _state[path] = {"usage": usage, "email": email,
                                    "ts": time.time(), "err": None}
                fh = (usage.get("five_hour") or {}).get("utilization")
                sd = (usage.get("seven_day") or {}).get("utilization")
                summary.append(f"{label_for(path, idx, email)}: 5h={fh}% 7d={sd}%")
            except Exception as e:
                with _state_lock:
                    prev = _state.get(path) or {}
                    prev["err"] = str(e)
                    _state[path] = prev
                summary.append(f"{os.path.basename(path)}: ERROR {e}")
                if first:
                    print("[bridge] (a 4xx / 'no refreshToken' usually means re-run "
                          "./mint_token.sh for that account.)", file=sys.stderr)
        if files:
            print("[bridge] " + " | ".join(summary), flush=True)
        else:
            print("[bridge] no credentials yet -- run ./mint_token.sh", file=sys.stderr)
        first = False
        # If a 429 set a cooldown, wait it out instead of the normal interval.
        cooldown = _rate_limited_remaining()
        if cooldown > 0:
            print(f"[bridge] rate limited -- backing off {int(cooldown)}s", flush=True)
        _shutdown.wait(max(POLL_SECONDS, cooldown))

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/usage_raw"):
            with _state_lock:
                raw = {label_for(p, i, (_state.get(p) or {}).get("email")):
                       (_state.get(p) or {}).get("usage")
                       for i, p in enumerate(credential_files())}
            self._send(200, json.dumps(raw, indent=2))
        elif self.path.startswith("/usage"):
            self._send(200, json.dumps(build_payload()))
        elif self.path.startswith("/token"):
            # one fresh bearer per account, for the device's direct API calls
            accounts = []
            for idx, path in enumerate(credential_files()):
                try:
                    token, expires_at = get_access_token(path)
                    with _state_lock:
                        email = (_state.get(path) or {}).get("email")
                    accounts.append({"label": label_for(path, idx, email),
                                     "token": token, "expires_at": int(expires_at)})
                except Exception as e:
                    print(f"[bridge] /token: {os.path.basename(path)}: {e}", file=sys.stderr)
            code = 200 if accounts else 503
            self._send(code, json.dumps({"beta": BETA_HEADER, "usage_url": USAGE_URL,
                                         "accounts": accounts}))
        elif self.path.startswith("/provision"):
            # Hand the device the FULL bundle (incl. refresh tokens) so it can
            # manage tokens itself. Read the files as-is -- do NOT refresh here,
            # or we'd rotate the very refresh token we're handing over.
            accounts = []
            for idx, path in enumerate(credential_files()):
                try:
                    oauth = load_oauth(path)
                    with _state_lock:
                        email = (_state.get(path) or {}).get("email")
                    accounts.append({"label": label_for(path, idx, email),
                                     "access_token": oauth.get("accessToken"),
                                     "refresh_token": oauth.get("refreshToken"),
                                     "expires_at": int(oauth.get("expiresAt", 0))})
                except Exception as e:
                    print(f"[bridge] /provision: {os.path.basename(path)}: {e}", file=sys.stderr)
            code = 200 if accounts else 503
            self._send(code, json.dumps({"client_id": CLIENT_ID, "token_url": TOKEN_URL,
                                         "beta": BETA_HEADER, "usage_url": USAGE_URL,
                                         "accounts": accounts}))
        elif self.path.startswith("/refresh"):
            out = {}
            for path in credential_files():
                try:
                    _, exp = get_access_token(path, force=True)
                    out[os.path.basename(path)] = {"ok": True, "expires_at": int(exp)}
                except Exception as e:
                    out[os.path.basename(path)] = {"ok": False, "error": str(e)}
            self._send(200, json.dumps(out))
        elif self.path == "/" or self.path.startswith("/?"):
            self._send(200, self._debug_html(), "text/html; charset=utf-8")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _debug_html(self):
        d = build_payload()
        rows = []
        for a in d["accounts"]:
            s, w = a["session"], a["weekly"]
            star = " &#9733;" if a.get("active") else ""
            state = "OK" if a.get("logged_in") else f"NO DATA{(' -- ' + a['note']) if a.get('note') else ''}"
            rows.append(
                f"<tr><td>{a['account']}{star}<br><small>{state}</small></td>"
                f"<td>{s['pct']}%<br><small>resets {s['resets_in_min']}m</small></td>"
                f"<td>{w['pct']}%<br><small>resets {w['resets_in_min']}m</small></td></tr>")
        if not rows:
            rows.append("<tr><td colspan=3><i>No credentials yet -- run ./mint_token.sh</i></td></tr>")
        return f"""<!doctype html><meta charset=utf-8>
<title>Claude usage bridge (OAuth token)</title>
<body style="font-family:system-ui;max-width:680px;margin:40px auto">
<h2>Claude usage bridge <small style="font-weight:normal">(OAuth token, {len(d['accounts'])} account(s))</small></h2>
<p><a href="/refresh">force token refresh</a> &middot; <a href="/usage_raw">raw API</a>
   &middot; <a href="/token">/token</a> (device bearers)</p>
<table border=1 cellpadding=8 cellspacing=0 style="border-collapse:collapse;width:100%">
<tr><th>Account</th><th>Session (5h)</th><th>Weekly (7d)</th></tr>
{''.join(rows)}
</table>
<pre style="margin-top:24px">{json.dumps(d, indent=2)}</pre>
</body>"""

    def log_message(self, *a):
        pass

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def main():
    files = credential_files()
    if not files:
        print(f"ERROR: no credentials found in {CRED_DIR} (credentials*.json)\n"
              f"       Run ./mint_token.sh first to mint your first account.",
              file=sys.stderr)
        sys.exit(1)

    ip = lan_ip()
    print("\nClaude usage bridge (OAuth token) starting.")
    print(f"  Accounts    : {len(files)} ({', '.join(os.path.basename(f) for f in files)})")
    print(f"  Token (dev) : http://{ip}:{PORT}/token   <- device fetches bearers here")
    print(f"  Device JSON : http://{ip}:{PORT}/usage   <- proxy fallback (old firmware)")
    print(f"  Debug page  : http://{ip}:{PORT}/")
    if PROVISION_ONLY:
        print(f"  Provision   : http://{ip}:{PORT}/provision   (device manages its own tokens)")
        print(f"  Mode        : PROVISION_ONLY -- no background refresh (won't rotate tokens)")
    else:
        print(f"  Provision   : http://{ip}:{PORT}/provision   (one-time device hand-off)")
        print(f"  Poll        : every {POLL_SECONDS}s   (refresh {REFRESH_SKEW_SEC}s before expiry)")

    if not PROVISION_ONLY:
        threading.Thread(target=poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown.set()
        srv.shutdown()
        print("\nbye")

if __name__ == "__main__":
    main()
