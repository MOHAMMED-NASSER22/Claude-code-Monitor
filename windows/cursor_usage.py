#!/usr/bin/env python3
"""
Cursor usage source for the desktop overlay.

Reads the Cursor IDE's locally stored auth token (the same token Cursor itself
keeps fresh) and queries the dashboard's current-period usage endpoint to get
the two monthly pools:

  - Auto + Composer pool   -> autoPercentUsed   (shown as the "AUTO" card)
  - API pool               -> apiPercentUsed    (shown as the "API"  card)

The token lives in Cursor's SQLite state store (state.vscdb), key
'cursorAuth/accessToken'. We open it read-only/immutable so a running Cursor
isn't disturbed, and we re-read every poll so we always pick up Cursor's own
refreshed token (no refresh logic of our own needed).

Returns account dicts in the SAME shape the overlay's Claude accounts use, plus
kind="cursor" so the renderer can relabel the cards (AUTO / API instead of
SESSION / WEEKLY).
"""
import base64
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USAGE_URL = "https://cursor.com/api/dashboard/get-current-period-usage"
AUTH_ME_URL = "https://cursor.com/api/auth/me"
_SSL = ssl.create_default_context()


def _state_db() -> str | None:
    """Platform-specific path to Cursor's globalStorage/state.vscdb."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    if not base:
        return None
    path = os.path.join(base, "Cursor", "User", "globalStorage", "state.vscdb")
    return path if os.path.exists(path) else None


def _read_token() -> tuple[str, str] | None:
    """Return (access_token, user_id) from the local Cursor store, or None."""
    db = _state_db()
    if not db:
        return None
    try:
        # immutable=1 avoids touching/locking a DB a running Cursor holds open.
        con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'"
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    tok = row[0].decode() if isinstance(row[0], bytes) else str(row[0])
    tok = tok.strip().strip('"')
    if tok.count(".") != 2:
        return None
    user_id = _jwt_user_id(tok)
    return (tok, user_id) if user_id else None


def _jwt_user_id(tok: str) -> str:
    """Extract the WorkOS user id from the JWT 'sub' claim (no signature use)."""
    try:
        seg = tok.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        sub = json.loads(base64.urlsafe_b64decode(seg)).get("sub", "")
    except Exception:
        return ""
    return sub.split("|")[-1] if "|" in sub else sub


def _post(url: str, cookie: str, body: dict, timeout: int = 20):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={
            "Cookie": cookie,
            "Content-Type": "application/json",
            "Accept": "application/json",
            # cursor.com rejects state-changing requests without a matching origin.
            "Origin": "https://cursor.com",
            "Referer": "https://cursor.com/dashboard?tab=usage",
            "User-Agent": "claude-overlay-cursor/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def _minutes_until(ms) -> int | None:
    try:
        secs = int(ms) / 1000.0 - time.time()
        return max(0, int(secs // 60))
    except (TypeError, ValueError):
        return None


def fetch_cursor_accounts() -> list[dict]:
    """One overlay account dict for Cursor, or [] if Cursor isn't present/usable.

    Never raises — the overlay treats this as an optional extra source.
    """
    creds = _read_token()
    if not creds:
        return []  # Cursor not installed / not signed in -> show nothing extra.
    token, user_id = creds
    cookie = (f"WorkosCursorSessionToken="
              f"{urllib.parse.quote(user_id)}%3A%3A{urllib.parse.quote(token)}")

    # Best-effort label from the account email; falls back to "Cursor".
    label = "Cursor"
    status, me = _get(AUTH_ME_URL, cookie)
    if status == 200 and isinstance(me, dict) and me.get("email"):
        label = me["email"]

    status, resp = _post(USAGE_URL, cookie, {})
    if status in (401, 403):
        return [_err(label, "Cursor token stale — reopen Cursor")]
    if status != 200 or not isinstance(resp, dict):
        return [_err(label, f"Cursor usage HTTP {status}")]

    plan = resp.get("planUsage") or {}
    auto = plan.get("autoPercentUsed")
    api = plan.get("apiPercentUsed")
    reset_min = _minutes_until(resp.get("billingCycleEnd"))

    def pct(v):
        try:
            return max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0

    return [{
        "label":       label,
        "kind":        "cursor",
        "path":        None,            # no local-credential settings for Cursor
        "session_pct": pct(auto),       # -> AUTO card
        "session_min": reset_min,
        "weekly_pct":  pct(api),        # -> API card
        "weekly_min":  reset_min,
        "active":      True,
        "ok":          True,
        "error":       "",
    }]


def _err(label: str, msg: str) -> dict:
    return {
        "label": label, "kind": "cursor", "path": None,
        "session_pct": 0, "session_min": None,
        "weekly_pct": 0, "weekly_min": None,
        "active": False, "ok": False, "error": msg,
    }


def _get(url: str, cookie: str, timeout: int = 15):
    req = urllib.request.Request(
        url, method="GET",
        headers={"Cookie": cookie, "Accept": "application/json",
                 "User-Agent": "claude-overlay-cursor/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


if __name__ == "__main__":
    import pprint
    pprint.pprint(fetch_cursor_accounts())
