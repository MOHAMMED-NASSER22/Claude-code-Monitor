#!/usr/bin/env python3
"""
Cursor usage source for the desktop overlay.

Reads the Cursor IDE's locally stored auth token (the same token Cursor itself
keeps fresh) and queries the dashboard's current-period usage endpoint to get
the two monthly pools:

  - Auto + Composer pool   -> autoPercentUsed   (shown as the "AUTO" card)
  - API pool               -> apiPercentUsed    (shown as the "API"  card)

The token lives in Cursor's SQLite state store (state.vscdb), key
'cursorAuth/accessToken'. We open it read-only so a running Cursor isn't
disturbed, and we re-read every poll so we always pick up Cursor's own refreshed
token (no refresh logic of our own needed).

Recent Cursor builds no longer keep that value in the clear: it is wrapped as
'cursor-safe-storage:v1:<base64>' by Electron's safeStorage, i.e. a Chromium
OSCrypt 'v10' blob (AES-256-GCM) whose master key sits DPAPI-protected in
Cursor's 'Local State'. We unwrap it with Windows' own crypt32/bcrypt through
ctypes, so there is no new dependency, and a bare token from an older build is
still accepted.

Returns account dicts in the SAME shape the overlay's Claude accounts use, plus
kind="cursor" so the renderer can relabel the cards (AUTO / API instead of
SESSION / WEEKLY).
"""
import base64
import ctypes
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

# Marker Cursor puts in front of a safeStorage-encrypted value.
_SAFE_PREFIX = "cursor-safe-storage:"


class _CursorAuthError(Exception):
    """Cursor is installed but holds a token we could not turn into a bearer."""


# --- Electron safeStorage / Chromium OSCrypt, via Windows' own crypto DLLs ---

class _Blob(ctypes.Structure):
    """DATA_BLOB."""
    _fields_ = [("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


class _GcmInfo(ctypes.Structure):
    """BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO."""
    _fields_ = [
        ("cbSize", ctypes.c_ulong), ("dwInfoVersion", ctypes.c_ulong),
        ("pbNonce", ctypes.POINTER(ctypes.c_char)), ("cbNonce", ctypes.c_ulong),
        ("pbAuthData", ctypes.POINTER(ctypes.c_char)), ("cbAuthData", ctypes.c_ulong),
        ("pbTag", ctypes.POINTER(ctypes.c_char)), ("cbTag", ctypes.c_ulong),
        ("pbMacContext", ctypes.POINTER(ctypes.c_char)),
        ("cbMacContext", ctypes.c_ulong),
        ("cbAAD", ctypes.c_ulong), ("cbData", ctypes.c_ulonglong),
        ("dwFlags", ctypes.c_ulong),
    ]


def _dpapi_unprotect(data: bytes) -> bytes:
    """CryptUnprotectData for the current user (no entropy, as Chromium uses it)."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    src = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(src, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None,
                                      None, 0, ctypes.byref(blob_out)):
        raise _CursorAuthError(
            f"DPAPI unwrap failed (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ct: bytes, tag: bytes) -> bytes:
    """AES-256-GCM open via bcrypt.dll; the tag is checked, so a wrong key fails."""
    bcrypt = ctypes.WinDLL("bcrypt")
    h_alg = ctypes.c_void_p()
    if bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(h_alg), "AES", None, 0):
        raise _CursorAuthError("BCryptOpenAlgorithmProvider(AES) failed")
    try:
        mode = "ChainingModeGCM".encode("utf-16-le") + b"\x00\x00"
        if bcrypt.BCryptSetProperty(h_alg, "ChainingMode", mode, len(mode), 0):
            raise _CursorAuthError("BCryptSetProperty(GCM) failed")
        h_key = ctypes.c_void_p()
        key_buf = ctypes.create_string_buffer(key, len(key))
        if bcrypt.BCryptGenerateSymmetricKey(h_alg, ctypes.byref(h_key), None, 0,
                                             key_buf, len(key), 0):
            raise _CursorAuthError("BCryptGenerateSymmetricKey failed")
        try:
            nonce_buf = ctypes.create_string_buffer(nonce, len(nonce))
            tag_buf = ctypes.create_string_buffer(tag, len(tag))
            info = _GcmInfo()
            info.cbSize = ctypes.sizeof(_GcmInfo)
            info.dwInfoVersion = 1          # BCRYPT_INIT_AUTH_MODE_INFO_VERSION
            info.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_char))
            info.cbNonce = len(nonce)
            info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_char))
            info.cbTag = len(tag)
            ct_buf = ctypes.create_string_buffer(ct, len(ct))
            out = ctypes.create_string_buffer(len(ct) or 1)
            written = ctypes.c_ulong(0)
            status = bcrypt.BCryptDecrypt(h_key, ct_buf, len(ct), ctypes.byref(info),
                                          None, 0, out, len(ct),
                                          ctypes.byref(written), 0)
            if status:
                raise _CursorAuthError(
                    f"AES-GCM decrypt failed (0x{status & 0xffffffff:08x})")
            return out.raw[:written.value]
        finally:
            bcrypt.BCryptDestroyKey(h_key)
    finally:
        bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


def _oscrypt_key() -> bytes:
    """Cursor's OSCrypt master key, out of 'Local State' and through DPAPI."""
    base = os.environ.get("APPDATA", "")
    path = os.path.join(base, "Cursor", "Local State") if base else ""
    if not path or not os.path.exists(path):
        raise _CursorAuthError("Cursor 'Local State' not found")
    try:
        with open(path, encoding="utf-8") as f:
            encoded = (json.load(f).get("os_crypt") or {}).get("encrypted_key")
    except (OSError, ValueError) as exc:
        raise _CursorAuthError(f"Cursor 'Local State' unreadable: {exc}") from exc
    if not encoded:
        raise _CursorAuthError("Cursor 'Local State' has no os_crypt key")
    try:
        raw = base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise _CursorAuthError("Cursor os_crypt key is not base64") from exc
    if not raw.startswith(b"DPAPI"):
        raise _CursorAuthError("Cursor os_crypt key is not DPAPI-wrapped")
    key = _dpapi_unprotect(raw[5:])
    if len(key) != 32:
        raise _CursorAuthError(f"Cursor os_crypt key is {len(key)} bytes, want 32")
    return key


def _unwrap_safe_storage(value: str) -> str:
    """'cursor-safe-storage:v1:<base64 v10 blob>' -> the plaintext token."""
    if sys.platform != "win32":
        # macOS keeps the key in the login Keychain, Linux in kwallet/libsecret;
        # neither is wired up here yet.
        raise _CursorAuthError("Cursor token is encrypted — unsupported on this OS")
    parts = value.split(":", 2)
    if len(parts) != 3:
        raise _CursorAuthError("Cursor token has an unknown wrapper")
    version, payload = parts[1], parts[2]
    if version != "v1":
        raise _CursorAuthError(f"Cursor safeStorage {version} not supported")
    try:
        blob = base64.b64decode(payload + "=" * (-len(payload) % 4))
    except (ValueError, TypeError) as exc:
        raise _CursorAuthError("Cursor token payload is not base64") from exc
    if not blob.startswith(b"v10") or len(blob) < 3 + 12 + 16:
        raise _CursorAuthError("Cursor token is not an OSCrypt v10 blob")
    plain = _aes_gcm_decrypt(_oscrypt_key(), blob[3:15], blob[15:-16], blob[-16:])
    try:
        return plain.decode()
    except UnicodeDecodeError as exc:
        raise _CursorAuthError("Decrypted Cursor token is not text") from exc


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


_AUTH_KEYS = (
    "cursorAuth/accessToken",
    "cursorAuth/cachedScopedProfile",
    "cursorAuth/stripeMembershipAuthId",
)


def _read_state(db: str) -> dict[str, str]:
    """Read the auth rows out of state.vscdb without disturbing a running Cursor.

    Plain read-only first so a write-ahead log is honoured (a token Cursor has
    just rotated lives there); immutable=1 as the fallback for when the
    -wal/-shm pair can't be opened read-only.
    """
    out: dict[str, str] = {}
    for uri in (f"file:{db}?mode=ro", f"file:{db}?mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                marks = ",".join("?" * len(_AUTH_KEYS))
                rows = con.execute(
                    f"SELECT key, value FROM ItemTable WHERE key IN ({marks})",
                    _AUTH_KEYS,
                ).fetchall()
            finally:
                con.close()
        except Exception:
            continue
        for key, value in rows:
            if value is None:
                continue
            text = value.decode() if isinstance(value, bytes) else str(value)
            out[key] = text.strip().strip('"')
        if out.get("cursorAuth/accessToken"):
            return out
    return out


def _profile_user_id(state: dict[str, str]) -> str:
    """Account id from the cached profile rows, for when the token has no 'sub'."""
    raw = state.get("cursorAuth/cachedScopedProfile") or ""
    auth_id = ""
    if raw:
        try:
            auth_id = (json.loads(raw).get("authId") or "").strip()
        except ValueError:
            auth_id = ""
    auth_id = auth_id or (state.get("cursorAuth/stripeMembershipAuthId") or "").strip()
    return auth_id.split("|")[-1] if "|" in auth_id else auth_id


def _read_token() -> tuple[str, str] | None:
    """Return (access_token, user_id) from the local Cursor store, or None.

    None means "no Cursor here / not signed in", and the overlay then shows
    nothing extra. Raises _CursorAuthError when a token *is* present but can't
    be used, so the next storage change surfaces on the card instead of making
    it quietly disappear.
    """
    db = _state_db()
    if not db:
        return None
    state = _read_state(db)
    tok = state.get("cursorAuth/accessToken") or ""
    if not tok:
        return None
    if tok.startswith(_SAFE_PREFIX):
        tok = _unwrap_safe_storage(tok).strip().strip('"')
    if tok.count(".") != 2:
        raise _CursorAuthError("Cursor token is not in a usable format")
    user_id = _jwt_user_id(tok) or _profile_user_id(state)
    if not user_id:
        raise _CursorAuthError("Cursor account id not found")
    return tok, user_id


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
    try:
        creds = _read_token()
    except _CursorAuthError as exc:
        # A token is there but unusable: say so rather than dropping the card,
        # which is how the last storage change went unnoticed for so long.
        return [_err("Cursor", str(exc))]
    except Exception as exc:                       # never break the overlay
        return [_err("Cursor", f"Cursor auth unreadable: {exc}")]
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
    try:   # cycle length for the pace indicator; overlay falls back to 30 days
        cycle_min = (int(resp["billingCycleEnd"]) - int(resp["billingCycleStart"])) // 60000
    except (KeyError, TypeError, ValueError):
        cycle_min = None

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
        "cycle_min":   cycle_min if cycle_min and cycle_min > 0 else None,
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
