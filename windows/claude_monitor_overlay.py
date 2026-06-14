#!/usr/bin/env python3
"""
Token Maxxing — Desktop Overlay
Floating glassy widget showing Claude session (5h) / weekly (7d) usage and
Cursor Auto + API pool usage.

Reads credentials from ~/.claude_usage_bridge/credentials*.json.
No external tools needed — includes a built-in OAuth login page so you can
sign in (or re-auth) directly from the overlay.

Requirements:  pip install PyQt6
Build to .exe: cd windows && build.bat
               (or: pyinstaller --onefile --windowed --name TokenMaxxing claude_monitor_overlay.py)

Usage:
  - Drag anywhere on the widget to move it
  - Left-click (no drag) to cycle through accounts
  - Scroll wheel to change transparency
  - Double-click to reset transparency
  - Right-click for context menu (refresh, settings, re-auth, opacity, exit)
"""

import base64, glob, hashlib, json, os, re, secrets, ssl, sys, threading, time, webbrowser
import urllib.error, urllib.parse, urllib.request
from datetime import datetime

import cursor_usage   # optional extra source: Cursor Auto + API pools

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QFrame, QMenu, QLineEdit, QPushButton, QStackedWidget, QCheckBox,
)
import math

from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QPoint, QPointF, QRectF,
)
from PyQt6.QtGui import (
    QPainter, QColor, QBrush, QPen, QLinearGradient,
    QPainterPath, QAction, QFont, QFontMetrics,
)

# ── OAuth / API constants (extracted from the Claude Code binary) ────────────
# All values below were extracted from the shipping Claude Code binary's OAuth
# config object (the `--claudeai` subscription flow), not guessed.
CLIENT_ID    = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
TOKEN_URL    = "https://platform.claude.com/v1/oauth/token"
AUTH_URL     = "https://claude.com/cai/oauth/authorize"        # CLAUDE_AI_AUTHORIZE_URL
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"  # MANUAL_REDIRECT_URL
# Exact scope set a real `claude auth login --claudeai` grants (ground truth from
# a minted credentials.json). org:create_api_key is omitted — it's for console/org
# accounts and breaks the authorize request on a personal Pro account.
OAUTH_SCOPE  = ("user:inference user:profile user:sessions:claude_code "
                "user:mcp_servers user:file_upload")
USAGE_URL    = "https://api.anthropic.com/api/oauth/usage"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
CHEAP_MODEL  = "claude-haiku-4-5"
CLAUDE_CODE_SYSTEM = (
    "You are Claude Code, Anthropic's official CLI for Claude.")
BETA_HEADER  = "oauth-2025-04-20"
CRED_DIR     = os.path.expanduser(os.environ.get("CRED_DIR", "~/.claude_usage_bridge"))
POLL_MS      = 120 * 1000       # 2 min. Budget is ~6 req/300s window (server enforces a
                                # 300s cooldown via Retry-After on 429); 60s sat on the
                                # edge and tripped 429 once manual refreshes piled on.
MANUAL_REFRESH_MAX    = 2       # max manual refreshes per rolling window
MANUAL_REFRESH_WINDOW = 60.0    # seconds

# Set from a 429's Retry-After header; usage polls/refreshes pause until then.
_rate_limit_until = 0.0         # time.monotonic() value


def _rate_limited_remaining() -> float:
    return max(0.0, _rate_limit_until - time.monotonic())

# ── Palette — mirrors simulator.html PAL / palette.json ─────────────────────
C_PANEL   = QColor(  0,   0,   0)        # BG   (#000000) dashboard fill
C_BG      = QColor(  6,   7,  10, 225)   # glassy outer panel (desktop chrome)
C_NOTCH   = QColor(  0,   0,   0)        # meter notches (PAL.BG)
C_BORDER  = QColor(255, 255, 255,  20)   # hairline window border
C_TEXT    = QColor(255, 255, 255)        # TEXT
C_DIM     = QColor(128, 128, 128)        # DIM
C_TRACK   = QColor( 32,  32,  32)        # TRACK  (#202020)
C_ACCENT  = QColor(  0, 168, 248)        # ACCENT (#00A8F8)
C_CLAUDE  = QColor(216, 116,  80)        # CLAUDE (#D87450)
C_CURSOR  = QColor(230, 230, 236)        # CURSOR brand accent (mono / near-white)
C_GREEN   = QColor( 40, 188,  80)        # GREEN  (#28BC50)
C_YELLOW  = QColor(248, 204,   0)        # YELLOW (#F8CC00)
C_RED     = QColor(248,  52,  48)        # RED    (#F83430)

SCALE     = 2                              # 160×128 TFT coords → desktop pixels
PANEL_W   = 160 * SCALE
PANEL_H   = 128 * SCALE
PANEL_PAD = 8                              # glassy margin around the dashboard
                                           # (small → content fills to the edges)


def _pct_color(pct: int) -> QColor:
    if pct >= 85: return C_RED
    if pct >= 60: return C_YELLOW
    return C_GREEN


# ── Windows Acrylic blur ─────────────────────────────────────────────────────

def _enable_acrylic(hwnd: int, tint_abgr: int = 0xAA0A0A10) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import c_int, Structure, POINTER, pointer, sizeof

        class ACCENT_POLICY(Structure):
            _fields_ = [("AccentState", c_int), ("AccentFlags", c_int),
                        ("GradientColor", c_int), ("AnimationId", c_int)]

        class WCA_DATA(Structure):
            _fields_ = [("Attribute", c_int), ("Data", POINTER(ACCENT_POLICY)),
                        ("SizeOfData", c_int)]

        accent = ACCENT_POLICY()
        accent.AccentState   = 4
        accent.GradientColor = tint_abgr
        data = WCA_DATA()
        data.Attribute  = 19
        data.SizeOfData = sizeof(accent)
        data.Data       = pointer(accent)
        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except Exception:
        pass


# ── HTTP / OAuth helpers (stdlib-only) ───────────────────────────────────────

_SSL  = ssl.create_default_context()
_lock = threading.Lock()


def _b64url(raw: bytes) -> str:
    """Node's base64url: URL-safe alphabet, no padding (matches the CC binary)."""
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _pkce_pair() -> tuple[str, str]:
    # Matches the binary: randomBytes(32).toString("base64url") for the verifier,
    # then sha256(verifier-string) -> base64url for the challenge.
    verifier  = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _new_state() -> str:
    # Binary uses randomBytes(32).toString("base64url"); short states are rejected
    # by the consent endpoint as "Invalid request format".
    return _b64url(secrets.token_bytes(32))


def _cred_files() -> list[str]:
    files = glob.glob(os.path.join(CRED_DIR, "credentials*.json"))
    return sorted(files, key=lambda p: (os.path.basename(p) != "credentials.json", p))


def _load_doc(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _save_doc(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_oauth(path: str) -> dict:
    doc = _load_doc(path)
    return doc.get("claudeAiOauth", doc) if isinstance(doc, dict) else doc


def _update_cred_doc(path: str, edit) -> None:
    try:
        doc = _load_doc(path)
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}
    edit(doc)
    _save_doc(path, doc)


def _account_settings(path: str) -> dict:
    try:
        doc = _load_doc(path)
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}
    return {
        "name":             (doc.get("name") or "").strip(),
        "auto_start":       doc.get("autoStartSession", True),
        "session_started":  bool(doc.get("sessionStarted", False)),
    }


def _save_account_settings(path: str, *, name: str | None = None,
                           auto_start: bool | None = None,
                           session_started: bool | None = None) -> None:
    def edit(doc: dict) -> None:
        if name is not None:
            n = name.strip()[:40]
            if n:
                doc["name"] = n
            else:
                doc.pop("name", None)
        if auto_start is not None:
            doc["autoStartSession"] = bool(auto_start)
        if session_started is not None:
            doc["sessionStarted"] = bool(session_started)
    _update_cred_doc(path, edit)


# ── App-level config (source toggles + Cursor name) ─────────────────────────
# Stored separately from credential files since Cursor has no file we own.
APP_CONFIG_PATH = os.path.join(CRED_DIR, "overlay_config.json")
_APP_CONFIG_DEFAULTS = {"show_claude": True, "show_cursor": True, "cursor_name": ""}


def load_app_config() -> dict:
    cfg = dict(_APP_CONFIG_DEFAULTS)
    try:
        with open(APP_CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in _APP_CONFIG_DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    cfg["show_claude"] = bool(cfg["show_claude"])
    cfg["show_cursor"] = bool(cfg["show_cursor"])
    cfg["cursor_name"] = (str(cfg.get("cursor_name") or "")).strip()[:40]
    return cfg


def save_app_config(*, show_claude: bool | None = None,
                    show_cursor: bool | None = None,
                    cursor_name: str | None = None) -> None:
    cfg = load_app_config()
    if show_claude is not None:
        cfg["show_claude"] = bool(show_claude)
    if show_cursor is not None:
        cfg["show_cursor"] = bool(show_cursor)
    if cursor_name is not None:
        cfg["cursor_name"] = cursor_name.strip()[:40]
    try:
        os.makedirs(CRED_DIR, exist_ok=True)
        tmp = APP_CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, APP_CONFIG_PATH)
    except Exception:
        pass


def _account_label(path: str, idx: int) -> str:
    settings = _account_settings(path)
    if settings["name"]:
        return settings["name"]
    m = re.match(r"credentials-(.+)\.json$", os.path.basename(path))
    if m:
        return m.group(1).replace("-", " ").replace("_", " ").title()
    return f"Account {idx + 1}"


def _start_session(token: str) -> bool:
    """Anchor an idle 5h block — one minimal Haiku message (~22 tokens)."""
    headers = {
        "Authorization":    f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta":   BETA_HEADER,
    }
    body = {
        "model":      CHEAP_MODEL,
        "max_tokens": 1,
        "system":     CLAUDE_CODE_SYSTEM,
        "messages":   [{"role": "user", "content": "hi"}],
    }
    status, _resp = _http_json("POST", MESSAGES_URL, headers=headers, body=body)
    return status == 200


def _http_json(method: str, url: str, headers: dict | None = None,
               body: dict | None = None, form: dict | None = None,
               timeout: int = 20) -> tuple[int, dict | str]:
    hdrs = {"User-Agent": "claude-overlay/1.0", "Accept": "application/json"}
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
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
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
    except Exception as e:
        return 0, str(e)


def _do_refresh(path: str, oauth: dict) -> dict:
    payload = {
        "grant_type":    "refresh_token",
        "refresh_token": oauth.get("refreshToken", ""),
        "client_id":     CLIENT_ID,
    }
    status, resp = _http_json("POST", TOKEN_URL, body=payload)
    if status in (400, 415, 422):
        status, resp = _http_json("POST", TOKEN_URL, form=payload)
    if status != 200 or not isinstance(resp, dict) or "access_token" not in resp:
        raise RuntimeError(f"token refresh failed (HTTP {status}): {resp}")
    oauth["accessToken"] = resp["access_token"]
    if resp.get("refresh_token"):
        oauth["refreshToken"] = resp["refresh_token"]
    if resp.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(resp["expires_in"]) * 1000
    try:
        doc = _load_doc(path)
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}
    doc["claudeAiOauth"] = oauth
    _save_doc(path, doc)
    return oauth


def _exchange_code(code: str, verifier: str) -> dict:
    # The paste flow returns the code as "<code>#<state>"; the token endpoint
    # wants only the code part (the state is for CSRF verification, not exchange).
    code  = code.strip()
    state = None
    if "#" in code:
        code, state = code.split("#", 1)
    payload = {
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": verifier,
    }
    if state:
        payload["state"] = state
    status, resp = _http_json("POST", TOKEN_URL, body=payload)
    if status in (400, 415, 422):
        status, resp = _http_json("POST", TOKEN_URL, form=payload)
    if status != 200 or not isinstance(resp, dict) or "access_token" not in resp:
        raise RuntimeError(f"code exchange failed (HTTP {status}): {resp}")
    return resp


def _save_new_credential(resp: dict, path: str) -> None:
    oauth: dict = {
        "accessToken":  resp["access_token"],
        "refreshToken": resp.get("refresh_token", ""),
        "expiresAt":    int(time.time() * 1000) + int(resp.get("expires_in", 3600)) * 1000,
    }
    if resp.get("scope"):
        oauth["scopes"] = resp["scope"].split()
    try:
        doc = _load_doc(path)
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}
    doc["claudeAiOauth"] = oauth
    _save_doc(path, doc)


def _get_token(path: str, force: bool = False) -> str:
    with _lock:
        oauth = _load_oauth(path)
        near_expiry = (oauth.get("expiresAt", 0) - time.time() * 1000) < 300_000
        if force or near_expiry:
            oauth = _do_refresh(path, oauth)
        return oauth["accessToken"]


def _to_minutes(resets_at) -> int | None:
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


def fetch_all_accounts() -> list[dict]:
    cfg = load_app_config()
    results = _fetch_claude_accounts() if cfg["show_claude"] else []
    if cfg["show_cursor"]:
        try:
            cur = cursor_usage.fetch_cursor_accounts()   # optional, never raises
            if cfg["cursor_name"]:
                for c in cur:
                    c["label"] = cfg["cursor_name"]
            results += cur
        except Exception:
            pass
    if not results:
        return [{"label": "No credentials", "ok": False,
                 "error": "Sign in via the login page",
                 "session_pct": 0, "session_min": None,
                 "weekly_pct":  0, "weekly_min":  None, "active": False}]
    return results


def _fetch_claude_accounts() -> list[dict]:
    files = _cred_files()
    results = []
    for idx, path in enumerate(files):
        label = _account_label(path, idx)
        try:
            token = _get_token(path)
            headers = {"Authorization": f"Bearer {token}", "anthropic-beta": BETA_HEADER}
            status, resp = _http_json("GET", USAGE_URL, headers=headers)
            if status in (401, 403):
                token = _get_token(path, force=True)
                headers["Authorization"] = f"Bearer {token}"
                status, resp = _http_json("GET", USAGE_URL, headers=headers)
            if status == 429:
                wait = int(round(_rate_limited_remaining())) or 300
                raise RuntimeError(f"Rate limited — retry in {wait}s")
            if status != 200 or not isinstance(resp, dict):
                raise RuntimeError(f"Usage API returned HTTP {status}")
            fh = resp.get("five_hour") or {}
            sd = resp.get("seven_day")  or {}
            active = fh.get("resets_at") is not None
            cfg = _account_settings(path)
            if active:
                if cfg["session_started"]:
                    _save_account_settings(path, session_started=False)
            elif cfg["auto_start"] and not cfg["session_started"]:
                if _start_session(token):
                    _save_account_settings(path, session_started=True)
                    status, resp = _http_json("GET", USAGE_URL, headers=headers)
                    if status == 200 and isinstance(resp, dict):
                        fh = resp.get("five_hour") or {}
                        sd = resp.get("seven_day") or {}
                        active = fh.get("resets_at") is not None
            s_pct = max(0, min(100, int(round(fh.get("utilization", 0) or 0))))
            w_pct = max(0, min(100, int(round(sd.get("utilization", 0) or 0))))
            results.append({
                "label":       label,
                "path":        path,
                "session_pct": s_pct,
                "session_min": _to_minutes(fh.get("resets_at")),
                "weekly_pct":  w_pct,
                "weekly_min":  _to_minutes(sd.get("resets_at")),
                "active":      active,
                "ok":          True,
                "error":       "",
            })
        except Exception as exc:
            results.append({
                "label": label, "path": path, "session_pct": 0, "session_min": None,
                "weekly_pct": 0, "weekly_min": None,
                "active": False, "ok": False, "error": str(exc),
            })
    return results


# ── Simulator draw API (mirrors simulator.html g.* 1:1) ─────────────────────

class Gfx:
    """Minimal Adafruit_GFX work-alike in 160×128 logical pixels (simulator.html)."""

    def __init__(self, p: QPainter):
        self.p = p

    def fill_screen(self, col: QColor) -> None:
        self.fill_rect(0, 0, 160, 128, col)

    def fill_rect(self, x, y, w, h, col: QColor) -> None:
        self.p.fillRect(QRectF(x, y, w, h), col)

    def draw_rect(self, x, y, w, h, col: QColor) -> None:
        self.fill_rect(x, y, w, 1, col)
        self.fill_rect(x, y + h - 1, w, 1, col)
        self.fill_rect(x, y, 1, h, col)
        self.fill_rect(x + w - 1, y, 1, h, col)

    def draw_round_rect(self, x, y, w, h, r, col: QColor) -> None:
        self.fill_rect(x + r, y, w - 2 * r, 1, col)
        self.fill_rect(x + r, y + h - 1, w - 2 * r, 1, col)
        self.fill_rect(x, y + r, 1, h - 2 * r, col)
        self.fill_rect(x + w - 1, y + r, 1, h - 2 * r, col)

    def draw_fast_hline(self, x, y, w, col: QColor) -> None:
        self.fill_rect(x, y, w, 1, col)

    def draw_fast_vline(self, x, y, h, col: QColor) -> None:
        self.fill_rect(x, y, 1, h, col)

    def draw_line(self, x0, y0, x1, y1, col: QColor) -> None:
        pen = QPen(col, 1)
        self.p.setPen(pen)
        self.p.drawLine(QPointF(x0 + 0.5, y0 + 0.5), QPointF(x1 + 0.5, y1 + 0.5))

    def fill_circle(self, cx, cy, r, col: QColor) -> None:
        self.p.setPen(Qt.PenStyle.NoPen)
        self.p.setBrush(col)
        self.p.drawEllipse(QPointF(cx, cy), r, r)

    def draw_circle(self, cx, cy, r, col: QColor) -> None:
        pen = QPen(col, 1)
        self.p.setPen(pen)
        self.p.setBrush(Qt.BrushStyle.NoBrush)
        self.p.drawEllipse(QPointF(cx, cy), r, r)

    def fill_triangle(self, x0, y0, x1, y1, x2, y2, col: QColor) -> None:
        path = QPainterPath()
        path.moveTo(x0, y0)
        path.lineTo(x1, y1)
        path.lineTo(x2, y2)
        path.closeSubpath()
        self.p.setPen(Qt.PenStyle.NoPen)
        self.p.setBrush(col)
        self.p.drawPath(path)

    def draw_poll_ring(self, cx, cy, r, frac: float, fetching: bool, t: float) -> None:
        """Ring at (cx,cy): fills clockwise as the next auto-refresh approaches."""
        self.p.setBrush(Qt.BrushStyle.NoBrush)
        self.p.setPen(QPen(C_TRACK, 1))
        self.p.drawEllipse(QPointF(cx, cy), r, r)
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        if fetching:
            self.p.setPen(QPen(C_ACCENT, 1.5))
            offset = (t * 0.8) % 1.0
            start = int((90 + offset * 360) * 16)
            self.p.drawArc(rect, start, -int(0.22 * 360 * 16))
        elif frac > 0.002:
            self.p.setPen(QPen(C_ACCENT, 1.5))
            self.p.drawArc(rect, 90 * 16, -int(min(1.0, frac) * 360 * 16))

    def text(self, s: str, x, y, col: QColor, size: int = 1) -> None:
        # Simulator: 6×8 px cells, top-left origin (textBaseline='top').
        f = QFont("Courier New")
        f.setPixelSize(8 * size)
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setFixedPitch(True)
        self.p.setFont(f)
        self.p.setPen(col)
        cell_w, cell_h = 6 * size, 8 * size
        cx = x
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        for ch in str(s):
            self.p.drawText(QRectF(cx, y, cell_w, cell_h), align, ch)
            cx += cell_w


def _text_w(s: str, size: int = 1) -> int:
    return len(str(s)) * 6 * size


def _fmt_dur(minutes: int) -> str:
    if minutes <= 0:
        return "--"
    d = minutes // 1440
    if d >= 1:
        h = (minutes % 1440) // 60
        return f"{d}d {h}h"
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m" if h > 0 else f"{m}m"


def _draw_badge(g: Gfx, x, y, txt: str, col: QColor) -> int:
    w = _text_w(txt, 1) + 5
    g.draw_round_rect(x, y, w, 10, 2, col)
    g.text(txt, x + 3, y + 2, col, 1)
    return w


def _draw_clock(g: Gfx, cx, cy, col: QColor) -> None:
    g.draw_circle(cx, cy, 3, col)
    g.draw_line(cx, cy, cx, cy - 2, col)
    g.draw_line(cx, cy, cx + 2, cy, col)


def _draw_meter(g: Gfx, x, y, w, h, pct: int, col: QColor | None) -> None:
    pct = max(0, min(100, pct))
    g.fill_rect(x, y, w, h, C_TRACK)
    if col is not None:
        fw = round(w * pct / 100)
        if fw > 0:
            g.fill_rect(x, y, fw, h, col)
    for i in range(1, 10):
        g.draw_fast_vline(x + round(w * i / 10), y, h, C_NOTCH)
    border = C_RED if col == C_RED else C_DIM
    g.draw_rect(x, y, w, h, border)


def _draw_spark(g: Gfx, cx, cy, t: float, base_outer, inner_r, rays, color, center_col) -> None:
    rot = t * 0.5
    breathe = 0.72 + 0.28 * math.sin(t * 1.6)
    outer = max(base_outer * breathe, inner_r + 1)
    da = 0.45
    for i in range(rays):
        a = rot + i * (math.pi * 2 / rays)
        tx = cx + math.cos(a) * outer
        ty = cy + math.sin(a) * outer
        b1x = cx + math.cos(a + da) * inner_r
        b1y = cy + math.sin(a + da) * inner_r
        b2x = cx + math.cos(a - da) * inner_r
        b2y = cy + math.sin(a - da) * inner_r
        g.fill_triangle(tx, ty, b1x, b1y, b2x, b2y, color)
    g.fill_circle(cx, cy, inner_r, color)
    g.fill_circle(cx, cy, max(1, inner_r - 2), center_col)


# Cube faces as (corner quad, base shade). Corners indexed (ix, iy, iz) in {0,1}.
_CUBE_FACES = [
    ([(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)], 1.00),  # top    (y+)
    ([(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)], 0.28),  # bottom (y-)
    ([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], 0.72),  # front  (z+)
    ([(0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)], 0.46),  # back   (z-)
    ([(0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)], 0.54),  # left   (x-)
    ([(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)], 0.62),  # right  (x+)
]


def _cube_shade(shade: float, ok: bool) -> QColor:
    r, gc, b = (236, 236, 242) if ok else (250, 92, 88)
    return QColor(int(r * shade), int(gc * shade), int(b * shade))


def _draw_cursor_mark(g: Gfx, cx, cy, ok: bool, t: float) -> None:
    """Cursor's cube logo, spinning about its vertical axis (Claude's spark analog)."""
    breathe = 0.92 + 0.08 * math.sin(t * 1.6)
    s = 3.9 * breathe
    a = t * 0.9                       # spin
    tilt = 0.60                       # fixed pitch so the lit top face shows
    ca, sa = math.cos(a), math.sin(a)
    ct, st = math.cos(tilt), math.sin(tilt)

    def project(X, Y, Z):
        x1 = X * ca + Z * sa          # yaw about vertical axis
        z1 = -X * sa + Z * ca
        y2 = Y * ct - z1 * st         # pitch to reveal the top
        z2 = Y * st + z1 * ct
        return (cx + x1, cy - y2, z2)  # screen y is downward → negate

    P = {(ix, iy, iz): project((ix * 2 - 1) * s, (iy * 2 - 1) * s, (iz * 2 - 1) * s)
         for ix in (0, 1) for iy in (0, 1) for iz in (0, 1)}

    blip = (int(time.time() * 1000) % 5000) < 450
    faces = []
    for quad, shade in _CUBE_FACES:
        pts = [P[v] for v in quad]
        # Back-face cull via screen-space signed area (screen y is downward).
        area = ((pts[1][0] - pts[0][0]) * (pts[2][1] - pts[0][1])
                - (pts[1][1] - pts[0][1]) * (pts[2][0] - pts[0][0]))
        if area <= 0:
            continue
        depth = sum(p[2] for p in pts) / 4
        faces.append((depth, pts, shade))

    faces.sort(key=lambda f: f[0])    # painter's algorithm: far first
    for _depth, pts, shade in faces:
        col = C_TEXT if blip else _cube_shade(shade, ok)
        g.fill_triangle(pts[0][0], pts[0][1], pts[1][0], pts[1][1],
                        pts[2][0], pts[2][1], col)
        g.fill_triangle(pts[0][0], pts[0][1], pts[2][0], pts[2][1],
                        pts[3][0], pts[3][1], col)


def _draw_metric_card(g: Gfx, y0, label, badge, pct, reset_min, ok: bool, t: float,
                      brand: QColor = C_CLAUDE) -> None:
    idle = pct < 0
    col = C_DIM if idle else _pct_color(pct)
    red = (not idle and ok and pct >= 85)
    num_col = col if ok else C_DIM
    now_ms = int(time.time() * 1000)

    g.text(label, 6, y0, brand if ok else C_DIM, 1)
    lw = _text_w(label, 1)
    _draw_badge(g, 6 + lw + 5, y0 - 1, badge, C_DIM)

    if red and (now_ms % 900) < 450:
        wx = 6 + lw + 5 + _text_w(badge, 1) + 5 + 10
        g.fill_triangle(wx - 5, y0 + 8, wx + 5, y0 + 8, wx, y0 - 1, C_RED)
        g.fill_rect(wx, y0 + 2, 1, 3, C_PANEL)
        g.fill_rect(wx, y0 + 6, 1, 1, C_PANEL)

    if idle:
        g.text("IDLE", 6, y0 + 15, C_DIM, 2)
    else:
        ps = str(pct)
        g.text(ps, 6, y0 + 11, num_col, 3)
        g.text("%", 6 + _text_w(ps, 3) + 2, y0 + 19, num_col, 2)

    rs = "idle" if idle else _fmt_dur(reset_min)
    g.text("RESETS", 160 - 6 - _text_w("RESETS", 1), y0 + 11, C_DIM, 1)
    rw = _text_w(rs, 2)
    _draw_clock(g, 160 - 6 - rw - 8, y0 + 27, C_DIM if idle else brand)
    g.text(rs, 160 - 6 - rw, y0 + 22, C_DIM if idle else C_TEXT, 2)

    meter_col = None if idle else (C_RED if red else col)
    _draw_meter(g, 6, y0 + 40, 148, 7, 0 if idle else pct, meter_col)


def _draw_dashboard(g: Gfx, d: dict, t: float,
                    poll_frac: float = 0.0, fetching: bool = False) -> None:
    s, w = d["session"], d["weekly"]
    acct = d.get("account") or "CLAUDE USAGE"
    if len(acct) > 23:
        acct = acct[:23]
    ok = d.get("ok", True)

    cursor = d.get("kind") == "cursor"
    brand = C_CURSOR if cursor else C_CLAUDE

    g.text(acct, 5, 2, C_DIM if ok else C_RED, 1)
    g.draw_poll_ring(138, 6, 5, poll_frac, fetching, t)
    if cursor:
        _draw_cursor_mark(g, 152, 6, ok, t)
    else:
        blip = ok and (int(time.time() * 1000) % 5000) < 450
        spark_col = C_CLAUDE if ok else C_RED
        _draw_spark(g, 152, 6, t, 7 if blip else 6, 2, 6, spark_col,
                    C_TEXT if blip else spark_col)
    g.draw_fast_hline(0, 12, 160, C_ACCENT if ok else C_RED)

    sess_pct = s["pct"] if s.get("active", True) else -1
    _draw_metric_card(g, 16, s.get("label", "SESSION"), s.get("badge", "5h"), sess_pct,
                      s.get("resets_in_min", 0) if sess_pct >= 0 else 0, ok, t, brand)

    g.draw_fast_hline(6, 71, 148, C_TRACK)

    _draw_metric_card(g, 75, w.get("label", "WEEKLY"), w.get("badge", "7d"), w["pct"],
                      w.get("resets_in_min", 0), ok, t, brand)


def _draw_boot(g: Gfx, status: str, t: float) -> None:
    err = bool(re.search(r"wait|unreach|error|fail", status, re.I))
    col = C_RED if err else C_CLAUDE
    _draw_spark(g, 80, 36, t, 18, 5, 8, col, C_TEXT)
    tw = _text_w("TOKEN", 2)
    g.text("TOKEN", (160 - tw) // 2, 64, C_TEXT, 2)
    tw2 = _text_w("MAXXING", 1)
    g.text("MAXXING", (160 - tw2) // 2, 84, C_DIM, 1)
    tx, tw_bar, ty = 34, 92, 104
    g.fill_rect(tx, ty, tw_bar, 3, C_TRACK)
    now_ms = int(time.time() * 1000)
    if err:
        if (now_ms % 900) < 450:
            g.fill_rect(tx, ty, tw_bar, 3, C_RED)
    else:
        seg, span = 22, tw_bar - 22
        p = math.sin(t * 1.3) * 0.5 + 0.5
        g.fill_rect(tx + round(span * p), ty, seg, 3, C_ACCENT)
    stw = _text_w(status, 1)
    g.text(status, (160 - stw) // 2, 114, C_RED if err else C_DIM, 1)


class DashboardCanvas(QWidget):
    """Renders drawDashboard / drawBoot from simulator.html at SCALE×."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mode = "boot"
        self._boot_status = "Fetching usage..."
        self._dash: dict | None = None
        self._t = 0.0
        self._poll_interval = POLL_MS / 1000.0
        self._last_fetch_at: float | None = None
        self._fetching = False
        self.setFixedSize(PANEL_W, PANEL_H)
        tm = QTimer(self)
        tm.timeout.connect(self._tick)
        tm.start(33)

    def set_boot(self, status: str) -> None:
        self._mode = "boot"
        self._boot_status = status
        self._fetching = True
        self.update()

    def set_dashboard(self, data: dict) -> None:
        self._mode = "dash"
        self._dash = data
        self.update()

    def set_fetching(self, fetching: bool) -> None:
        self._fetching = fetching
        self.update()

    def mark_refreshed(self) -> None:
        self._last_fetch_at = time.monotonic()
        self._fetching = False
        self.update()

    def _poll_frac(self) -> float:
        if self._fetching or self._last_fetch_at is None:
            return 0.0
        return min(1.0, (time.monotonic() - self._last_fetch_at) / self._poll_interval)

    def _tick(self):
        self._t = time.time() * 4
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), C_PANEL)
        p.save()
        p.scale(SCALE, SCALE)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)
        g = Gfx(p)
        if self._mode == "boot":
            _draw_boot(g, self._boot_status, self._t)
        elif self._dash:
            _draw_dashboard(g, self._dash, self._t,
                            self._poll_frac(), self._fetching)
        p.restore()


class PulseDot(QWidget):
    """Small pulsing status indicator (auth page header)."""

    def __init__(self, parent=None, size: int = 8):
        super().__init__(parent)
        self._color = C_DIM
        self._pulse = 0.0
        self.setFixedSize(size, size)
        tm = QTimer(self)
        tm.timeout.connect(self._tick)
        tm.start(50)

    def set_color(self, c: QColor) -> None:
        self._color = c
        self.update()

    def _tick(self):
        self._pulse += 0.15
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        breathe = 0.55 + 0.45 * math.sin(self._pulse)
        r = max(2.0, (self.width() / 2 - 1) * breathe)
        glow = QColor(self._color)
        glow.setAlpha(70)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(cx, cy), r + 2, r + 2)
        p.setBrush(self._color)
        p.drawEllipse(QPointF(cx, cy), r, r)


# ── Threads ───────────────────────────────────────────────────────────────────

class FetchThread(QThread):
    done = pyqtSignal(list)

    def run(self):
        self.done.emit(fetch_all_accounts())


class CursorFetchThread(QThread):
    """Fetch only the Cursor card (no Claude API call), applying the name override."""
    done = pyqtSignal(list)

    def run(self):
        try:
            accts = cursor_usage.fetch_cursor_accounts()
            name = load_app_config()["cursor_name"]
            if name:
                for c in accts:
                    c["label"] = name
            self.done.emit(accts)
        except Exception:
            self.done.emit([])


class AuthThread(QThread):
    done = pyqtSignal(bool, str)   # (success, error_message)

    def __init__(self, parent, code: str, verifier: str):
        super().__init__(parent)
        self._code     = code
        self._verifier = verifier

    def run(self):
        try:
            resp = _exchange_code(self._code, self._verifier)
            os.makedirs(CRED_DIR, exist_ok=True)
            path = os.path.join(CRED_DIR, "credentials.json")
            _save_new_credential(resp, path)
            self.done.emit(True, "")
        except Exception as e:
            self.done.emit(False, str(e))


# ── Main overlay window ───────────────────────────────────────────────────────

class OverlayWindow(QWidget):

    # ── Initialisation ────────────────────────────────────────────────────────

    def __init__(self):
        super().__init__()
        self._drag_pos:     QPoint | None = None
        self._drag_start:   QPoint | None = None
        self._accounts:     list[dict]    = []
        self._acct_idx:     int           = 0
        self._fetching:     bool          = False
        self._pkce_verifier: str          = ""
        self._manual_refresh_at: list[float] = []
        self._refresh_notice: str           = ""
        self._refresh_notice_until: float   = 0.0

        self._init_window()
        self._build_ui()

        if _cred_files():
            self._show_main()
        else:
            self._show_auth()

    def _init_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint  |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        pad = PANEL_PAD
        self.resize(PANEL_W + pad * 2, PANEL_H + pad * 2)
        self.setMinimumSize(PANEL_W + pad * 2, PANEL_H + pad * 2)
        self.show()
        _enable_acrylic(int(self.winId()))

    # ── UI layout ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget(self)
        self._stack.setStyleSheet("background: transparent;")
        root.addWidget(self._stack)

        self._build_stats_page()
        self._build_auth_page()
        self._build_settings_page()

    _FIELD_STYLE = """
        QLineEdit {
            background: rgba(255,255,255,0.07);
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 6px; color: #EBEBF0;
            font-size: 11px; padding: 6px 8px;
        }
        QLineEdit:focus { border: 1px solid rgba(216,116,80,0.6); }
    """

    # Page 0 — stats ──────────────────────────────────────────────────────────

    def _build_stats_page(self):
        self._frame = QWidget()
        self._frame.setStyleSheet("background: transparent;")
        self._stack.addWidget(self._frame)

        lay = QVBoxLayout(self._frame)
        lay.setContentsMargins(PANEL_PAD, PANEL_PAD, PANEL_PAD, PANEL_PAD)
        lay.setSpacing(0)

        self._canvas = DashboardCanvas(self._frame)
        lay.addWidget(self._canvas, 0, Qt.AlignmentFlag.AlignCenter)

    # Page 1 — login ──────────────────────────────────────────────────────────

    def _build_auth_page(self):
        self._auth_frame = QWidget()
        self._auth_frame.setStyleSheet("background: transparent;")
        self._stack.addWidget(self._auth_frame)

        alay = QVBoxLayout(self._auth_frame)
        alay.setContentsMargins(14, 11, 14, 13)
        alay.setSpacing(7)

        # Header
        ahdr = QHBoxLayout()
        ahdr.setSpacing(7)
        self._auth_dot = PulseDot(self)
        self._auth_dot.set_color(C_ACCENT)
        ahdr.addWidget(self._auth_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        atitle = QLabel("Claude Monitor")
        atitle.setStyleSheet(
            "color: #EBEBF0; font-size: 12px; font-weight: 700; background: transparent;")
        ahdr.addWidget(atitle, 1)
        alay.addLayout(ahdr)

        alay.addWidget(self._divider(4))

        asub = QLabel("Sign in to view your Claude usage stats")
        asub.setStyleSheet("color: #808098; font-size: 10px; background: transparent;")
        alay.addWidget(asub)

        alay.addSpacing(2)

        self._btn_browser = QPushButton("Open Browser  →")
        self._btn_browser.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_browser.setStyleSheet("""
            QPushButton {
                background: rgba(216,116,80,0.85); color: #EBEBF0;
                border: none; border-radius: 7px;
                font-size: 11px; font-weight: 700; padding: 8px 12px;
            }
            QPushButton:hover  { background: rgba(216,116,80,1.0); }
            QPushButton:pressed { background: rgba(180,96,60,1.0); }
            QPushButton:disabled { background: rgba(216,116,80,0.4); }
        """)
        self._btn_browser.clicked.connect(self._on_open_browser)
        alay.addWidget(self._btn_browser)

        self._lbl_code_hint = QLabel("Paste the code shown in the browser:")
        self._lbl_code_hint.setStyleSheet(
            "color: #808098; font-size: 9px; background: transparent;")
        self._lbl_code_hint.hide()
        alay.addWidget(self._lbl_code_hint)

        self._inp_code = QLineEdit()
        self._inp_code.setPlaceholderText("Paste authorization code here…")
        self._inp_code.setStyleSheet("""
            QLineEdit {
                background: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 6px; color: #EBEBF0;
                font-size: 10px; padding: 5px 8px;
            }
            QLineEdit:focus { border: 1px solid rgba(216,116,80,0.6); }
        """)
        self._inp_code.returnPressed.connect(self._on_submit_code)
        self._inp_code.hide()
        alay.addWidget(self._inp_code)

        self._btn_submit = QPushButton("Submit")
        self._btn_submit.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_submit.setStyleSheet("""
            QPushButton {
                background: rgba(40,188,80,0.85); color: #0A0A10;
                border: none; border-radius: 7px;
                font-size: 11px; font-weight: 700; padding: 8px 12px;
            }
            QPushButton:hover  { background: rgba(40,188,80,1.0); }
            QPushButton:pressed { background: rgba(30,150,60,1.0); }
            QPushButton:disabled { background: rgba(40,188,80,0.35); color: #444; }
        """)
        self._btn_submit.clicked.connect(self._on_submit_code)
        self._btn_submit.hide()
        alay.addWidget(self._btn_submit)

        self._lbl_auth_err = QLabel("")
        self._lbl_auth_err.setStyleSheet(
            "color: #F83430; font-size: 9px; background: transparent;")
        self._lbl_auth_err.setWordWrap(True)
        self._lbl_auth_err.hide()
        alay.addWidget(self._lbl_auth_err)

        alay.addStretch()

    # Page 2 — per-account settings ───────────────────────────────────────────

    def _build_settings_page(self):
        self._settings_frame = QWidget()
        self._settings_frame.setStyleSheet("background: transparent;")
        self._stack.addWidget(self._settings_frame)

        chk_style = ("color: #C8C8D8; font-size: 10px;"
                     " background: transparent; spacing: 6px;")
        sub_style = "color: #808098; font-size: 9px; background: transparent;"

        slay = QVBoxLayout(self._settings_frame)
        slay.setContentsMargins(16, 14, 16, 14)
        slay.setSpacing(5)

        stitle = QLabel("Settings")
        stitle.setStyleSheet(
            "color: #EBEBF0; font-size: 12px; font-weight: 700; background: transparent;")
        slay.addWidget(stitle)
        slay.addWidget(self._divider(1))

        # ── Per-account section (Claude only; hidden for Cursor) ────────────
        self._sett_account_box = QWidget()
        self._sett_account_box.setStyleSheet("background: transparent;")
        abox = QVBoxLayout(self._sett_account_box)
        abox.setContentsMargins(0, 0, 0, 0)
        abox.setSpacing(4)

        nlbl = QLabel("Display name")
        nlbl.setStyleSheet(sub_style)
        abox.addWidget(nlbl)

        self._sett_name = QLineEdit()
        self._sett_name.setPlaceholderText("e.g. Personal, Work…")
        self._sett_name.setStyleSheet(self._FIELD_STYLE)
        abox.addWidget(self._sett_name)

        self._sett_auto = QCheckBox("Auto-start 5h session when idle")
        self._sett_auto.setStyleSheet(chk_style)
        self._sett_auto.setToolTip(
            "When SESSION is idle, send one minimal Haiku message (~22 tokens) "
            "to anchor a new 5h block — same as the desk gadget.")
        abox.addWidget(self._sett_auto)
        slay.addWidget(self._sett_account_box)

        # ── Sources section (global) ───────────────────────────────────────
        srclbl = QLabel("SOURCES")
        srclbl.setStyleSheet(sub_style + " font-weight: 700;")
        slay.addWidget(srclbl)

        self._sett_show_claude = QCheckBox("Show Claude")
        self._sett_show_claude.setStyleSheet(chk_style)
        slay.addWidget(self._sett_show_claude)

        self._sett_show_cursor = QCheckBox("Show Cursor (Auto + API)")
        self._sett_show_cursor.setStyleSheet(chk_style)
        slay.addWidget(self._sett_show_cursor)

        cnlbl = QLabel("Cursor display name")
        cnlbl.setStyleSheet(sub_style)
        slay.addWidget(cnlbl)

        self._sett_cursor_name = QLineEdit()
        self._sett_cursor_name.setPlaceholderText("e.g. Cursor, Work Cursor…")
        self._sett_cursor_name.setStyleSheet(self._FIELD_STYLE)
        slay.addWidget(self._sett_cursor_name)

        slay.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._btn_sett_cancel = QPushButton("Cancel")
        self._btn_sett_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_sett_cancel.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.08); color: #C8C8D8;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px; font-size: 11px; font-weight: 600; padding: 8px 12px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.14); }
        """)
        self._btn_sett_cancel.clicked.connect(self._hide_settings)
        self._btn_sett_save = QPushButton("Save")
        self._btn_sett_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_sett_save.setStyleSheet("""
            QPushButton {
                background: rgba(40,188,80,0.85); color: #0A0A10;
                border: none; border-radius: 7px;
                font-size: 11px; font-weight: 700; padding: 8px 12px;
            }
            QPushButton:hover  { background: rgba(40,188,80,1.0); }
            QPushButton:pressed { background: rgba(30,150,60,1.0); }
        """)
        self._btn_sett_save.clicked.connect(self._save_settings)
        btn_row.addWidget(self._btn_sett_cancel)
        btn_row.addWidget(self._btn_sett_save)
        slay.addLayout(btn_row)

        self._settings_path: str | None = None

    # ── Widget helpers ────────────────────────────────────────────────────────

    def _divider(self, top_margin: int) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(
            f"background: rgba(255,255,255,0.07); max-height: 1px;"
            f" margin-top: {top_margin}px; margin-bottom: 0px;")
        return line

    # ── Page switching ────────────────────────────────────────────────────────

    def _show_main(self):
        self._stack.setCurrentIndex(0)
        self._canvas.set_boot("Fetching usage...")
        self._start_polling()

    def _show_auth(self):
        self._stack.setCurrentIndex(1)
        # Reset form to initial state
        self._inp_code.clear()
        self._lbl_code_hint.hide()
        self._inp_code.hide()
        self._btn_submit.hide()
        self._lbl_auth_err.hide()
        self._btn_browser.setText("Open Browser  →")
        self._btn_browser.setEnabled(True)
        self._pkce_verifier = ""

    def _hide_settings(self):
        self._stack.setCurrentIndex(0)
        self._refresh_display()

    def _show_settings(self):
        # Per-account fields only apply to Claude accounts (those with a path);
        # the Sources section is global and always shown.
        path = None
        if self._accounts:
            path = self._accounts[self._acct_idx].get("path")
        self._settings_path = path
        if path:
            cfg = _account_settings(path)
            self._sett_name.setText(
                cfg["name"] or self._accounts[self._acct_idx].get("label", ""))
            self._sett_auto.setChecked(cfg["auto_start"])
            self._sett_account_box.show()
        else:
            self._sett_account_box.hide()

        app = load_app_config()
        self._sett_show_claude.setChecked(app["show_claude"])
        self._sett_show_cursor.setChecked(app["show_cursor"])
        self._sett_cursor_name.setText(app["cursor_name"])
        self._stack.setCurrentIndex(2)

    def _save_settings(self):
        if self._settings_path:
            name = self._sett_name.text().strip()
            _save_account_settings(self._settings_path, name=name,
                                   auto_start=self._sett_auto.isChecked())
            if self._accounts and self._acct_idx < len(self._accounts):
                a = self._accounts[self._acct_idx]
                if a.get("path") == self._settings_path:
                    a["label"] = name or _account_label(
                        self._settings_path, self._acct_idx)

        prev = load_app_config()
        show_claude = self._sett_show_claude.isChecked()
        show_cursor = self._sett_show_cursor.isChecked()
        cursor_name = self._sett_cursor_name.text().strip()
        save_app_config(show_claude=show_claude, show_cursor=show_cursor,
                        cursor_name=cursor_name)

        # Apply toggles/rename from cache — NO API call. Hiding a source or
        # renaming Cursor never re-polls (the Claude usage API is rate-limited).
        kept = []
        for a in self._accounts:
            kind = a.get("kind", "claude")
            if kind == "claude" and not show_claude:
                continue
            if kind == "cursor" and not show_cursor:
                continue
            if kind == "cursor" and cursor_name:
                a["label"] = cursor_name
            kept.append(a)
        self._accounts = kept
        self._acct_idx = 0
        self._stack.setCurrentIndex(0)
        self._refresh_display()
        # Re-enabling Cursor repopulates instantly (its endpoint isn't rate-limited).
        # Claude is never fetched on save — it appears on the next 2-min poll.
        if show_cursor and not prev["show_cursor"]:
            self._fetch_cursor_only()

    # ── Auth flow ─────────────────────────────────────────────────────────────

    def _on_open_browser(self):
        verifier, challenge = _pkce_pair()
        self._pkce_verifier = verifier
        params = urllib.parse.urlencode({
            "code":                  "true",   # manual paste flow: show the code, don't redirect
            "client_id":             CLIENT_ID,
            "response_type":         "code",
            "redirect_uri":          REDIRECT_URI,
            "scope":                 OAUTH_SCOPE,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "state":                 _new_state(),
        })
        webbrowser.open(AUTH_URL + "?" + params)
        self._lbl_code_hint.show()
        self._inp_code.show()
        self._btn_submit.show()
        self._inp_code.setFocus()
        self._btn_browser.setText("Open Browser again  →")

    def _on_submit_code(self):
        code = self._inp_code.text().strip()
        if not code:
            self._lbl_auth_err.setText("Paste the authorization code first.")
            self._lbl_auth_err.show()
            return
        if not self._pkce_verifier:
            self._lbl_auth_err.setText("Click 'Open Browser' first.")
            self._lbl_auth_err.show()
            return
        self._btn_submit.setEnabled(False)
        self._btn_submit.setText("Signing in…")
        self._btn_browser.setEnabled(False)
        self._lbl_auth_err.hide()

        t = AuthThread(self, code, self._pkce_verifier)
        t.done.connect(self._on_auth_done)
        t.finished.connect(t.deleteLater)
        t.start()

    def _on_auth_done(self, ok: bool, error: str):
        self._btn_submit.setEnabled(True)
        self._btn_submit.setText("Submit")
        self._btn_browser.setEnabled(True)
        if ok:
            self._show_main()
        else:
            self._lbl_auth_err.setText(f"Auth failed: {error}")
            self._lbl_auth_err.show()

    # ── Polling ───────────────────────────────────────────────────────────────

    def _start_polling(self):
        self._trigger_fetch()
        if not hasattr(self, "_poll_timer"):
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._trigger_fetch)
            self._rotate_timer = QTimer(self)
            self._rotate_timer.timeout.connect(self._rotate_account)
        self._poll_timer.start(POLL_MS)
        self._rotate_timer.start(5000)

    def _manual_refresh_wait(self) -> int:
        """Seconds until another manual refresh is allowed (0 = ok now)."""
        now = time.monotonic()
        recent = [t for t in self._manual_refresh_at if now - t < MANUAL_REFRESH_WINDOW]
        self._manual_refresh_at = recent
        if len(recent) < MANUAL_REFRESH_MAX:
            return 0
        return max(1, int(math.ceil(MANUAL_REFRESH_WINDOW - (now - recent[0]))))

    def _manual_refresh(self):
        wait = self._manual_refresh_wait()
        if wait > 0:
            self._refresh_notice = f"Refresh in {wait}s"
            self._refresh_notice_until = time.monotonic() + min(3.0, float(wait))
            self._refresh_display()
            return
        self._manual_refresh_at.append(time.monotonic())
        self._refresh_notice = ""
        self._trigger_fetch()

    def _trigger_fetch(self):
        if self._fetching:
            return
        remaining = _rate_limited_remaining()
        if remaining > 0:
            # Honor the server's Retry-After: don't hammer during cooldown.
            self._refresh_notice = f"Rate limited {int(remaining)}s"
            self._refresh_notice_until = time.monotonic() + min(3.0, remaining)
            self._refresh_display()
            return
        self._fetching = True
        if self._stack.currentIndex() == 0:
            self._canvas.set_fetching(True)
            if not self._accounts:
                self._canvas.set_boot("Fetching usage...")
        thread = FetchThread(self)
        thread.done.connect(self._on_data)
        thread.done.connect(lambda: setattr(self, "_fetching", False))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_data(self, accounts: list[dict]):
        prev = self._acct_idx
        self._accounts = accounts
        if accounts:
            self._acct_idx = min(prev, len(accounts) - 1)
        self._canvas.mark_refreshed()
        self._refresh_display()

    def _fetch_cursor_only(self):
        """Refresh just the Cursor card (its endpoint isn't rate-limited).

        Used when Cursor is re-enabled in Settings so it appears immediately,
        without re-polling the rate-limited Claude usage API.
        """
        if getattr(self, "_cursor_fetching", False):
            return
        self._cursor_fetching = True
        thread = CursorFetchThread(self)
        thread.done.connect(self._on_cursor_data)
        thread.done.connect(lambda: setattr(self, "_cursor_fetching", False))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_cursor_data(self, cursor_accts: list[dict]):
        if not load_app_config()["show_cursor"]:
            return
        # Replace any cached Cursor entries with the fresh ones.
        self._accounts = [a for a in self._accounts if a.get("kind") != "cursor"]
        self._accounts += cursor_accts
        if self._acct_idx >= len(self._accounts):
            self._acct_idx = 0
        self._refresh_display()

    def _rotate_account(self):
        if len(self._accounts) > 1:
            self._acct_idx = (self._acct_idx + 1) % len(self._accounts)
            self._refresh_display()

    def _go_home(self):
        self._acct_idx = 0
        self._refresh_display()

    # ── Display update ────────────────────────────────────────────────────────

    def _account_to_dash(self, a: dict, idx: int, cnt: int) -> dict:
        acct = a["label"]
        if not a["ok"]:
            acct = (a.get("error") or acct)[:23]
        elif cnt > 1:
            suffix = f" {idx + 1}/{cnt}"
            acct = (acct[: max(0, 23 - len(suffix))] + suffix)[:23]
        else:
            acct = acct[:23]
        sess_min = a.get("session_min") or 0
        week_min = a.get("weekly_min") or 0
        cursor = a.get("kind") == "cursor"
        # Cursor reports two monthly pools (Auto+Composer, API); Claude reports
        # the rolling 5h session + 7d weekly windows.
        s_label, s_badge = ("AUTO", "mo") if cursor else ("SESSION", "5h")
        w_label, w_badge = ("API",  "mo") if cursor else ("WEEKLY",  "7d")
        return {
            "ok":      a["ok"],
            "account": acct,
            "kind":    a.get("kind", "claude"),
            "session": {
                "label":         s_label,
                "badge":         s_badge,
                "pct":           a["session_pct"] if a.get("active") else -1,
                "resets_in_min": sess_min,
                "active":        a.get("active", False),
            },
            "weekly": {
                "label":         w_label,
                "badge":         w_badge,
                "pct":           a["weekly_pct"],
                "resets_in_min": week_min,
            },
        }

    def _refresh_display(self):
        if not self._accounts:
            self._canvas.set_boot("Fetching usage...")
            return

        a   = self._accounts[self._acct_idx]
        cnt = len(self._accounts)
        dash = self._account_to_dash(a, self._acct_idx, cnt)
        if self._refresh_notice and time.monotonic() < self._refresh_notice_until:
            dash["account"] = self._refresh_notice[:23]
            dash["ok"] = True
        self._canvas.set_dashboard(dash)

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        body = QPainterPath()
        body.addRoundedRect(r, 13, 13)

        p.setClipPath(body)
        p.fillPath(body, QBrush(C_BG))

        shimmer = QLinearGradient(0, 0, 0, 38)
        shimmer.setColorAt(0.0, QColor(255, 255, 255, 18))
        shimmer.setColorAt(1.0, QColor(255, 255, 255,  0))
        p.fillPath(body, QBrush(shimmer))

        p.setClipping(False)
        p.setPen(QPen(C_BORDER, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(body)

    # ── Mouse events ──────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            gp = e.globalPosition().toPoint()
            self._drag_pos   = gp - self.frameGeometry().topLeft()
            self._drag_start = gp
        e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
        e.accept()

    def mouseReleaseEvent(self, e):
        if (e.button() == Qt.MouseButton.LeftButton
                and self._drag_start is not None
                and self._stack.currentIndex() == 0   # only on stats page
                and len(self._accounts) > 1):
            delta = e.globalPosition().toPoint() - self._drag_start
            if abs(delta.x()) < 5 and abs(delta.y()) < 5:
                self._acct_idx = (self._acct_idx + 1) % len(self._accounts)
                self._refresh_display()
        self._drag_pos   = None
        self._drag_start = None
        e.accept()

    def wheelEvent(self, e):
        delta = e.angleDelta().y() / 120
        op    = max(0.15, min(1.0, self.windowOpacity() + delta * 0.05))
        self.setWindowOpacity(op)
        e.accept()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setWindowOpacity(0.92)
        e.accept()

    # ── Context menu ──────────────────────────────────────────────────────────

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background: rgba(16, 16, 24, 235);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 10px; color: #DDDDE8;
                padding: 5px 4px; font-size: 12px;
            }
            QMenu::item {
                padding: 6px 18px 6px 12px;
                border-radius: 6px; margin: 1px 3px;
            }
            QMenu::item:selected { background: rgba(255, 255, 255, 0.10); }
            QMenu::item:disabled { color: #555568; }
            QMenu::separator {
                height: 1px; background: rgba(255, 255, 255, 0.07);
                margin: 3px 8px;
            }
        """)

        if self._stack.currentIndex() == 0:
            # Stats page actions
            wait = self._manual_refresh_wait()
            label = "⟳  Refresh now" if wait == 0 else f"⟳  Refresh now ({wait}s)"
            a_refresh = QAction(label, self)
            a_refresh.setEnabled(wait == 0 and not self._fetching)
            a_refresh.triggered.connect(self._manual_refresh)
            menu.addAction(a_refresh)

            if len(self._accounts) > 1:
                a_home = QAction("⌂  First account", self)
                a_home.triggered.connect(self._go_home)
                menu.addAction(a_home)

            a_sett = QAction("\u2699  Settings", self)
            a_sett.triggered.connect(self._show_settings)
            menu.addAction(a_sett)

            menu.addSeparator()

            a_reauth = QAction("↩  Re-auth (sign in again)", self)
            a_reauth.triggered.connect(self._show_auth)
            menu.addAction(a_reauth)

        else:
            # Auth page — offer to go back if credentials exist
            if _cred_files():
                a_back = QAction("← Back to stats", self)
                a_back.triggered.connect(self._show_main)
                menu.addAction(a_back)
                menu.addSeparator()

        opacity_label = QAction(f"Opacity: {int(self.windowOpacity() * 100)}%", self)
        opacity_label.setEnabled(False)
        menu.addAction(opacity_label)

        for label, val in [("  100%", 1.00), ("   80%", 0.80),
                            ("   60%", 0.60), ("   40%", 0.40)]:
            a = QAction(label, self)
            a.triggered.connect(lambda _, v=val: self.setWindowOpacity(v))
            menu.addAction(a)

        menu.addSeparator()

        a_quit = QAction("✕  Exit", self)
        a_quit.triggered.connect(QApplication.instance().quit)
        menu.addAction(a_quit)

        menu.exec(e.globalPos())


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if hasattr(Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    app.setApplicationName("Token Maxxing")

    font = QFont("Segoe UI", 10) if sys.platform == "win32" else QFont("SF Pro Display", 10)
    app.setFont(font)

    w = OverlayWindow()
    w.setWindowOpacity(0.92)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
