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
  - Ctrl+scroll to resize the full overlay (remembered)
  - Double-click to reset transparency (or expand from mini mode)
  - Right-click for context menu (minimize to clock, size, tutorial, refresh, settings, re-auth, opacity, exit)
  - First launch (or until skipped) shows a short in-widget tutorial; replay from the menu
  - Auto-checks GitHub for newer releases (from v1.5) and shows a small update bar
"""

import sys
import os

# Windows taskbar groups windows by AppUserModelID. Must be set before PyQt loads
# or Windows keeps the pythonw.exe / floppy-disk icon (including pinned shortcuts).
_APP_USER_MODEL_ID = "sherbo.TokenMaxxing"
if sys.platform == "win32":
    try:
        import ctypes as _ctypes
        _ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            _APP_USER_MODEL_ID)
    except Exception:
        pass

import base64, glob, hashlib, json, re, secrets, ssl, tempfile, threading, time, webbrowser
import urllib.error, urllib.parse, urllib.request
from datetime import datetime

import cursor_usage   # optional extra source: Cursor Auto + API pools

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
    QFrame, QMenu, QLineEdit, QPushButton, QStackedWidget, QCheckBox, QComboBox,
)
import math

from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QPoint, QPointF, QRect, QRectF, QEvent,
)
from PyQt6.QtGui import (
    QPainter, QColor, QBrush, QPen, QLinearGradient,
    QPainterPath, QAction, QFont, QFontMetrics, QIcon, QGuiApplication, QImage,
)

# Windows taskbar groups windows by AppUserModelID. Without a unique ID set
# before any UI, pinned shortcuts inherit the Python interpreter icon.
# (Also set at module import above, before PyQt; repeated here for source runs.)


def _resource_path(name: str) -> str:
    """Path to a bundled resource, working both from source and a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _icon_path() -> str:
    """Path for Win32/Qt icon APIs — prefer bundled multi-size .ico."""
    path = _resource_path("spark.ico")
    if os.path.exists(path):
        return path
    if getattr(sys, "frozen", False):
        return sys.executable
    return ""


def _app_icon() -> QIcon:
    path = _icon_path()
    if path:
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    return QIcon()


def _win_set_hwnd_icons(hwnd: int) -> None:
    """Force taskbar/title icons via Win32 — Qt alone is unreliable on frameless windows."""
    path = _icon_path()
    if not path or not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040

        LoadImageW = user32.LoadImageW
        LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_uint,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        LoadImageW.restype = wintypes.HANDLE

        flags = LR_LOADFROMFILE | LR_DEFAULTSIZE
        for idx in (ICON_SMALL, ICON_BIG):
            hicon = LoadImageW(None, path, IMAGE_ICON, 0, 0, flags)
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, idx, hicon)
    except Exception:
        pass


def _win_set_app_user_model_id() -> None:
    """Must run before QApplication — Windows reads this for taskbar grouping."""
    if sys.platform != "win32":
        return
    try:
        QGuiApplication.setDesktopFileName(_APP_USER_MODEL_ID)
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            _APP_USER_MODEL_ID)
    except Exception:
        pass


# ── Taskbar / clock docking (mini mode) ───────────────────────────────────────

def _win_tray_windows():
    """(tray_hwnd, notify_hwnd) for Shell_TrayWnd / TrayNotifyWnd, or (0, 0)."""
    if sys.platform != "win32":
        return 0, 0
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        FindWindowW = user32.FindWindowW
        FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        FindWindowW.restype = wintypes.HWND
        FindWindowExW = user32.FindWindowExW
        FindWindowExW.argtypes = [
            wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
        FindWindowExW.restype = wintypes.HWND
        tray = FindWindowW("Shell_TrayWnd", None)
        if not tray:
            return 0, 0
        notify = FindWindowExW(tray, None, "TrayNotifyWnd", None)
        return int(tray or 0), int(notify or 0)
    except Exception:
        return 0, 0


def _win_pin_overlay(hwnd: int, gadget: bool = False) -> None:
    """Keep the overlay topmost, or embed the clock strip in the taskbar."""
    if gadget:
        _win_embed_tray(hwnd, True)
        return
    if sys.platform != "win32" or not hwnd:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        HWND_TOPMOST = -1
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        SWP_FRAMECHANGED = 0x0020
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style &= ~WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED)
    except Exception:
        pass


def _win_embed_tray(hwnd: int, embed: bool, *, teardown: bool = False,
                    size: tuple[int, int] | None = None, offset: int = 0) -> None:
    """Parent the clock strip to Shell_TrayWnd so it paints on the taskbar.

    SetParent must run *before* WS_CHILD (the reverse fails with error 87 on
    Win11). Once it is a tray child, Explorer cannot cover it by raising the
    XAML taskbar. HWND_TOPMOST cannot win that fight.

    `size` is the strip's native (physical-pixel) width/height. Pass it rather
    than resizing through Qt: once the strip is a child of the taskbar, Qt
    applies the DPI scale twice and the strip shrinks (then fights the dock
    timer every 250ms, which is what made it flicker).
    """
    if sys.platform != "win32" or not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        GWL_STYLE = -16
        GWL_EXSTYLE = -20
        WS_POPUP = 0x80000000
        WS_CHILD = 0x40000000
        WS_VISIBLE = 0x10000000
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOPMOST = 0x00000008
        HWND_TOP = 0
        HWND_TOPMOST = -1
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        SWP_FRAMECHANGED = 0x0020
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002

        if embed:
            tray, notify = _win_tray_windows()
            if not tray:
                return
            nrc = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(nrc)):
                return
            # Prefer the on-bar slot left of the clock; fall back to wherever
            # Qt already placed the widget (first dock / missing notify).
            trect = _win_window_rect(tray)
            nrect = _win_window_rect(notify) if notify else None
            if trect and nrect:
                tl, tt, tr, tb = trect
                nl, nt, nr, nb = nrect
                w = size[0] if size else nrc.right - nrc.left
                h = tb - tt if (tr - tl) >= (tb - tt) else (
                    size[1] if size else nrc.bottom - nrc.top)
                # `offset` = native px the user dragged the strip away from
                # the clock (so it can sit in any free part of the taskbar).
                x = nl - w - offset
                mon = _win_monitor_rect(tray)
                if mon and (tl + tr) // 2 < (mon[0] + mon[2]) // 2:
                    x = nr + offset
                x = max(tl, min(x, tr - w))
                y = tt if (tr - tl) >= (tb - tt) else max(tt, min(nt - h, tb - h))
                nrc.left, nrc.top, nrc.right, nrc.bottom = x, y, x + w, y + h
            already = user32.GetParent(hwnd) == tray
            if already:
                cur = _win_window_rect(hwnd)
                # ±2px slack: Qt rounds native→logical at fractional DPI and
                # nudges the strip 1px back, so an exact match would re-position
                # (and flicker) on every dock tick.
                if cur and all(abs(c - n) <= 2 for c, n in zip(
                        cur, (nrc.left, nrc.top, nrc.right, nrc.bottom))):
                    return
            pt = wintypes.POINT(nrc.left, nrc.top)
            user32.ScreenToClient(tray, ctypes.byref(pt))
            if already:
                # Reposition only — SetParent / FRAMECHANGED every 250ms flickers.
                user32.SetWindowPos(
                    hwnd, HWND_TOP, pt.x, pt.y,
                    nrc.right - nrc.left, nrc.bottom - nrc.top,
                    SWP_SHOWWINDOW | SWP_NOACTIVATE)
                return
            user32.SetParent(hwnd, tray)
            style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
            style = (style | WS_CHILD | WS_VISIBLE) & ~WS_POPUP
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
            ex = (ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_TOPMOST
            user32.SetWindowLongW(hwnd, GWL_STYLE, style)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
            user32.SetWindowPos(
                hwnd, HWND_TOP, pt.x, pt.y,
                nrc.right - nrc.left, nrc.bottom - nrc.top,
                SWP_SHOWWINDOW | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            _disable_acrylic(hwnd)
            return

        parent = user32.GetParent(hwnd)
        if parent:
            user32.SetParent(hwnd, 0)
        if teardown:
            return
        style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        style = (style | WS_POPUP | WS_VISIBLE) & ~WS_CHILD
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        ex = (ex | WS_EX_TOPMOST) & ~(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        user32.SetWindowLongW(hwnd, GWL_STYLE, style)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED)
    except Exception:
        pass


def _win_grab(x: int, y: int, w: int, h: int) -> bytes | None:
    """BGRA bytes (top-down) of a native screen rect, or None."""
    try:
        import ctypes
        from ctypes import wintypes

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        for fn, res in ((user32.GetDC, ctypes.c_void_p),
                        (gdi32.CreateCompatibleDC, ctypes.c_void_p),
                        (gdi32.CreateCompatibleBitmap, ctypes.c_void_p),
                        (gdi32.SelectObject, ctypes.c_void_p)):
            fn.restype = res
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 wintypes.DWORD]
        gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                                    wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.UINT]
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        old = gdi32.SelectObject(mem, bmp)
        ok = gdi32.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020)   # SRCCOPY
        gdi32.SelectObject(mem, old)
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w,
                               biHeight=-h, biPlanes=1, biBitCount=32, biCompression=0)
        buf = (ctypes.c_ubyte * (w * h * 4))()
        rows = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0) if ok else 0
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
        return bytes(buf) if rows == h else None
    except Exception:
        return None


def _win_taskbar_backdrop(strip: tuple | None = None):
    """(slice QImage, average QColor) of the taskbar background, or (None, None).

    The slice is a 1-px-wide, full-height column of the real taskbar. Stretched
    across the mini strip it reproduces what is behind it, including Win11's
    lighter top hairline and any vertical gradient, on any theme/accent.

    A see-through taskbar is tinted by the wallpaper, so its colour drifts
    along its length: sample right beside the strip (`strip` = its native
    rect) as well as the empty far edge, and take the per-row median of the
    three so a column that lands on an app icon is outvoted.
    """
    if sys.platform != "win32":
        return None, None
    try:
        tray, _notify = _win_tray_windows()
        r = _win_window_rect(tray)
        if not r:
            return None, None
        tl, tt, tr, tb = r
        if (tr - tl) < (tb - tt):                 # vertical taskbar: flat colour
            raw = _win_grab((tl + tr) // 2, tb - 3, 1, 1)
            if not raw:
                return None, None
            return None, QColor(raw[2], raw[1], raw[0])
        h = tb - tt
        xs = [tr - 3]
        if strip:
            xs += [strip[0] - 2, strip[2] + 1]
        cols = []
        for x in xs:
            if tl <= x < tr and not (strip and strip[0] <= x < strip[2]):
                raw = _win_grab(x, tt, 1, h)
                if raw:
                    cols.append(raw)
        if not cols:
            return None, None
        out = bytearray(h * 4)
        for yy in range(h):
            px = sorted((c[yy * 4:yy * 4 + 4] for c in cols),
                        key=lambda b: b[0] + b[1] * 2 + b[2])
            out[yy * 4:yy * 4 + 4] = px[len(px) // 2]
        img = QImage(bytes(out), 1, h, 4, QImage.Format.Format_RGB32).copy()
        body = [out[i * 4:i * 4 + 3] for i in range(h // 4, max(h // 4 + 1, 3 * h // 4))]
        avg = QColor(sum(b[2] for b in body) // len(body),
                     sum(b[1] for b in body) // len(body),
                     sum(b[0] for b in body) // len(body))
        return img, avg
    except Exception:
        return None, None


def _win_redraw(hwnd: int) -> None:
    """Force Explorer to present a tray-child frame. Qt update() alone is not
    enough once the strip is a WS_CHILD of Shell_TrayWnd — DWM keeps the
    first bitmap unless we invalidate the native hwnd."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        RDW_INVALIDATE = 0x0001
        RDW_UPDATENOW = 0x0100
        RDW_NOERASE = 0x0020
        user32.RedrawWindow(hwnd, None, None,
                            RDW_INVALIDATE | RDW_UPDATENOW | RDW_NOERASE)
    except Exception:
        pass


def _win_window_rect(hwnd: int):
    """Native virtual-desktop rect (left, top, right, bottom) or None."""
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        GetWindowRect = user32.GetWindowRect
        GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        GetWindowRect.restype = wintypes.BOOL
        rect = wintypes.RECT()
        if not GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


def _win_monitor_rect(hwnd: int):
    """Native rcMonitor for the screen that owns hwnd, or None."""
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        MONITOR_DEFAULTTONEAREST = 2
        MonitorFromWindow = user32.MonitorFromWindow
        MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        MonitorFromWindow.restype = ctypes.c_void_p
        GetMonitorInfoW = user32.GetMonitorInfoW
        GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
        GetMonitorInfoW.restype = wintypes.BOOL
        hmon = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        r = mi.rcMonitor
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def _qscreen_for_native_rect(nleft, ntop, nright, nbottom):
    """QScreen whose monitor contains this native rect's center."""
    cx = (nleft + nright) // 2
    cy = (ntop + nbottom) // 2
    best = None
    best_dpr_match = None
    nw = max(1, nright - nleft)
    nh = max(1, nbottom - ntop)
    for screen in QGuiApplication.screens():
        g = screen.geometry()
        dpr = screen.devicePixelRatio() or 1.0
        # Native extent of this screen, assuming geometry is logical DIPs.
        ng_w, ng_h = g.width() * dpr, g.height() * dpr
        ng_x, ng_y = g.x() * dpr, g.y() * dpr
        if (ng_x - 4 <= cx <= ng_x + ng_w + 4
                and ng_y - 4 <= cy <= ng_y + ng_h + 4):
            return screen
        # Direct native match (Qt 6 Windows window coords are often native).
        if (g.x() - 4 <= cx <= g.x() + g.width() + 4
                and g.y() - 4 <= cy <= g.y() + g.height() + 4):
            best = screen
        if abs(ng_w - nw) < 8 and abs(ng_h - nh) < 8:
            best_dpr_match = screen
    return (best
            or best_dpr_match
            or QGuiApplication.screenAt(QPoint(cx, cy))
            or QGuiApplication.primaryScreen())


def _map_native_to_qt(nx, ny, screen, nmon) -> tuple[int, int]:
    """Map a native virtual-desktop point into Qt widget coordinates."""
    g = screen.geometry()
    if nmon:
        nl, nt, nr, nb = nmon
        nw, nh = nr - nl, nb - nt
        if nw > 0 and nh > 0:
            qx = g.x() + (nx - nl) * g.width() / nw
            qy = g.y() + (ny - nt) * g.height() / nh
            return int(round(qx)), int(round(qy))
    dpr = screen.devicePixelRatio() or 1.0
    return int(round(nx / dpr)), int(round(ny / dpr))


def _mini_dock_rect(width: int = 0) -> QRect | None:
    """Qt-coordinate rect for the mini strip, left of the clock/tray cluster."""
    tray_hwnd, notify_hwnd = _win_tray_windows()
    tray = _win_window_rect(tray_hwnd) if tray_hwnd else None
    notify = _win_window_rect(notify_hwnd) if notify_hwnd else None
    nmon = _win_monitor_rect(tray_hwnd) if tray_hwnd else None

    if tray and notify:
        screen = _qscreen_for_native_rect(*tray)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        tl, tt, tr, tb = tray
        nl, nt, nr, nb = notify
        tray_w, tray_h = tr - tl, tb - tt
        horizontal = tray_w >= tray_h
        q_left, q_top = _map_native_to_qt(tl, tt, screen, nmon)
        q_right, q_bottom = _map_native_to_qt(tr, tb, screen, nmon)
        n_left, n_top = _map_native_to_qt(nl, nt, screen, nmon)
        n_right, n_bot = _map_native_to_qt(nr, nb, screen, nmon)
        q_tray = QRect(q_left, q_top, max(1, q_right - q_left),
                       max(1, q_bottom - q_top))
        w = width or MINI_W
        sg = screen.geometry()
        if horizontal:
            h = q_tray.height()
            # Sit on the taskbar, immediately left of the clock/tray cluster
            # (Win11 has no TrayClockWClass — TrayNotifyWnd is clock + notify).
            x = n_left - w
            if q_tray.center().x() < sg.center().x():
                x = n_right
            x = max(q_tray.x(), min(x, q_tray.x() + q_tray.width() - w))
            y = q_tray.y()
        else:
            w = q_tray.width()
            h = 28
            x = q_tray.x()
            y = n_top - h
            if y < q_tray.y():
                y = n_bot
            y = max(q_tray.y(), min(y, q_tray.y() + q_tray.height() - h))
        return QRect(x, y, max(48, w), max(22, h))

    # Fallback: excluded taskbar strip at the clock corner.
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return None
    full = screen.geometry()
    avail = screen.availableGeometry()
    left = avail.x() - full.x()
    right = (full.x() + full.width()) - (avail.x() + avail.width())
    top = avail.y() - full.y()
    bottom = (full.y() + full.height()) - (avail.y() + avail.height())
    w = width or MINI_W
    if bottom > 2:
        h = max(24, bottom)
        return QRect(full.x() + full.width() - w, avail.y() + avail.height(), w, h)
    if top > 2:
        h = max(24, top)
        return QRect(full.x() + full.width() - w, full.y(), w, h)
    if right > 2:
        h = 28
        return QRect(avail.x() + avail.width(),
                     full.y() + full.height() - h, right, h)
    if left > 2:
        h = 28
        return QRect(full.x(), full.y() + full.height() - h, left, h)
    h = 32
    return QRect(full.x() + full.width() - w, full.y() + full.height() - h, w, h)

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
PROFILE_URL  = "https://api.anthropic.com/api/oauth/profile"
PLAN_TTL_S   = 24 * 60 * 60     # plan tier rarely changes; re-check once a day
AUTH_SUB_DEFAULT = "Sign in to view your Claude usage stats"
AUTH_SUB_ADD = ("Add another account: switch claude.ai in your browser to "
                "that account first, then sign in")
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
CHEAP_MODEL  = "claude-haiku-4-5"
CLAUDE_CODE_SYSTEM = (
    "You are Claude Code, Anthropic's official CLI for Claude.")
BETA_HEADER  = "oauth-2025-04-20"
CRED_DIR     = os.path.expanduser(os.environ.get("CRED_DIR", "~/.claude_usage_bridge"))
APP_VERSION  = "1.9"            # keep in sync with windows/version_info.txt
GITHUB_REPO  = "MOHAMMED-NASSER22/Claude-code-Monitor"
UPDATE_CHECK_MS = 6 * 60 * 60 * 1000   # 6h; also runs once shortly after launch
UPDATE_BANNER_H = 26
POLL_MS      = 120 * 1000       # 2 min. Budget is ~6 req/300s window (server enforces a
                                # 300s cooldown via Retry-After on 429); 60s sat on the
                                # edge and tripped 429 once manual refreshes piled on.
MANUAL_REFRESH_MAX    = 2       # max manual refreshes per rolling window
MANUAL_REFRESH_WINDOW = 60.0    # seconds

# Set from a 429's Retry-After header; usage polls/refreshes pause until then.
# Tracked on the WALL clock (time.time()), not time.monotonic(): on Windows the
# monotonic clock freezes while the PC sleeps, so a cooldown set before sleep
# would survive a multi-hour suspend and falsely show "Rate limited" on wake.
# The wall clock advances through sleep, so the deadline expires as it should.
_rate_limit_until = 0.0         # time.time() (wall-clock) deadline


def _rate_limited_remaining() -> float:
    return max(0.0, _rate_limit_until - time.time())


USAGE_CACHE_PATH = os.path.join(CRED_DIR, "overlay_usage_cache.json")


def _load_usage_cache() -> dict:
    try:
        with open(USAGE_CACHE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            accounts = data.get("accounts")
            if not isinstance(accounts, list):
                accounts = []
            return {
                "fetched_at": float(data.get("fetched_at") or 0),
                "rate_limit_until": float(data.get("rate_limit_until") or 0),
                "accounts": accounts,
            }
    except Exception:
        pass
    return {"fetched_at": 0.0, "rate_limit_until": 0.0, "accounts": []}


def _save_usage_cache(*, accounts=None, fetched_at=None,
                      rate_limit_until=None) -> None:
    data = _load_usage_cache()
    if accounts is not None:
        data["accounts"] = accounts
    if fetched_at is not None:
        data["fetched_at"] = float(fetched_at)
    if rate_limit_until is not None:
        data["rate_limit_until"] = float(rate_limit_until)
    else:
        data["rate_limit_until"] = max(
            float(data.get("rate_limit_until") or 0), _rate_limit_until)
    try:
        os.makedirs(CRED_DIR, exist_ok=True)
        tmp = USAGE_CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, USAGE_CACHE_PATH)
    except Exception:
        pass


def _restore_rate_limit() -> None:
    global _rate_limit_until
    until = float(_load_usage_cache().get("rate_limit_until") or 0)
    if until > _rate_limit_until:
        _rate_limit_until = until


def _usage_cache_fresh() -> bool:
    cached = _load_usage_cache()
    if not cached.get("accounts"):
        return False
    age = time.time() - float(cached.get("fetched_at") or 0)
    return 0.0 <= age < (POLL_MS / 1000.0)


def _is_rate_limit_payload(accounts: list) -> bool:
    if not accounts:
        return False
    claude = [a for a in accounts if a.get("kind") != "cursor"]
    if not claude:
        return False
    return all(
        (not a.get("ok"))
        and "rate limit" in (a.get("error") or "").lower()
        for a in claude
    )

# ── Palette — mirrors simulator.html PAL / palette.json ─────────────────────
C_PANEL   = QColor(  0,   0,   0)        # BG   (#000000) dashboard fill
C_BG      = QColor(  6,   7,  10, 225)   # glassy outer panel (desktop chrome)
C_BG_MINI = QColor(  6,   7,  10)         # opaque taskbar strip (acrylic snow otherwise)
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

SCALE_DEFAULT = 2.0                        # 160×128 TFT coords → desktop pixels
SCALE_MIN     = 1.0
SCALE_MAX     = 3.0
SCALE_STEP    = 0.25
SCALE     = SCALE_DEFAULT
PANEL_W   = int(160 * SCALE)
PANEL_H   = int(128 * SCALE)
FORM_MIN_W = 270                           # Settings / sign-in page minimum
FORM_MIN_H = 360
PANEL_PAD = 8                              # glassy margin around the dashboard
                                           # (small → content fills to the edges)
MINI_W    = 160                            # logical width of the clock strip
MINI_H    = 32                             # height when parked beside the taskbar
DOCK_MS   = 250                            # re-dock while the taskbar moves / auto-hides


def _clamp_scale(value) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        scale = SCALE_DEFAULT
    steps = round(scale / SCALE_STEP)
    scale = steps * SCALE_STEP
    return max(SCALE_MIN, min(SCALE_MAX, scale))


def _pct_color(pct: int, light_bg: bool = False) -> QColor:
    if light_bg:                               # readable on a light taskbar
        if pct >= 85: return QColor(196, 32, 28)
        if pct >= 60: return QColor(168, 110, 0)
        return QColor(18, 128, 52)
    if pct >= 85: return C_RED
    if pct >= 60: return C_YELLOW
    return C_GREEN


def _is_light(col: QColor | None) -> bool:
    if col is None:
        return False
    # Relative luminance (sRGB weights); taskbar light theme is ~0.9.
    return (0.2126 * col.red() + 0.7152 * col.green() + 0.0722 * col.blue()) / 255 > 0.55


# ── Windows Acrylic blur ─────────────────────────────────────────────────────

def _enable_acrylic(hwnd: int, tint_abgr: int = 0xAA0A0A10) -> None:
    _set_accent(hwnd, enabled=True, tint_abgr=tint_abgr)


def _disable_acrylic(hwnd: int) -> None:
    """Acrylic on a WS_CHILD of Shell_TrayWnd paints DWM static / white noise."""
    _set_accent(hwnd, enabled=False)


def _set_accent(hwnd: int, *, enabled: bool,
                tint_abgr: int = 0xAA0A0A10) -> None:
    if sys.platform != "win32" or not hwnd:
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
        # 4 = ACCENT_ENABLE_ACRYLICBLURBEHIND; 0 = ACCENT_DISABLED
        accent.AccentState   = 4 if enabled else 0
        accent.GradientColor = tint_abgr if enabled else 0
        data = WCA_DATA()
        data.Attribute  = 19
        data.SizeOfData = sizeof(accent)
        data.Data       = pointer(accent)
        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except Exception:
        pass


# ── HTTP / OAuth helpers (stdlib-only) ───────────────────────────────────────

_SSL  = ssl.create_default_context()
_lock = threading.RLock()        # guards every credential read-modify-write
# Rotated OAuth bundles not yet on disk (save failed / file briefly locked).
# Each refresh invalidates the previous refresh token, so this must never be
# dropped until a write succeeds.
_pending_oauth: dict[str, dict] = {}


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
    with open(path, encoding="utf-8-sig") as f:     # tolerate a BOM
        return json.load(f)


def _save_doc(path: str, doc: dict) -> None:
    """Atomic write: unique temp + fsync + os.replace, retried because Windows
    antivirus/indexers briefly hold the target open (PermissionError)."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json",
                               dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_oauth(path: str) -> dict:
    doc = _load_doc(path)
    return doc.get("claudeAiOauth", doc) if isinstance(doc, dict) else doc


def _update_cred_doc(path: str, edit) -> bool:
    """Read-modify-write of a credential file's non-token fields.

    Never writes when the file is missing (account removed — don't resurrect
    it) or unreadable (writing {} would wipe the tokens). Returns success.
    """
    with _lock:
        try:
            doc = _load_doc(path)
        except Exception:
            return False
        if not isinstance(doc, dict):
            return False
        if path in _pending_oauth:
            doc["claudeAiOauth"] = _pending_oauth[path]
        edit(doc)
        try:
            _save_doc(path, doc)
        except Exception:
            return False
        _pending_oauth.pop(path, None)
        return True


def _persist_oauth(path: str, oauth: dict, *, create: bool = False) -> None:
    """Store an OAuth bundle, keeping the file's other fields.

    After a refresh the old refresh token is already dead, so on any failure
    the bundle stays in _pending_oauth (and _get_token keeps using + retrying
    it) instead of being lost. create=True (new sign-in) raises on failure.
    """
    with _lock:
        _pending_oauth[path] = oauth
        try:
            doc = _load_doc(path)
            if not isinstance(doc, dict):
                raise ValueError("credential file is not a JSON object")
        except FileNotFoundError:
            if not create:
                _pending_oauth.pop(path, None)       # account was removed
                return
            doc = {}
        except Exception:
            if create:
                raise
            return
        doc["claudeAiOauth"] = oauth
        try:
            _save_doc(path, doc)
        except Exception:
            if create:
                _pending_oauth.pop(path, None)
                raise
            return
        _pending_oauth.pop(path, None)


def _account_settings(path: str) -> dict:
    try:
        doc = _load_doc(path)
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}
    return {
        "name":             (doc.get("name") or "").strip(),
        # Off unless the user turned it on: it sends a real (tiny) message.
        "auto_start":       doc.get("autoStartSession", False),
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
_APP_CONFIG_DEFAULTS = {
    "show_claude": True, "show_cursor": True, "cursor_name": "",
    "compact_mode": False, "overlay_scale": SCALE_DEFAULT,
    "dismissed_update": "", "tutorial_done": False, "mini_offset": 0,
}


def load_app_config() -> dict:
    cfg = dict(_APP_CONFIG_DEFAULTS)
    try:
        with open(APP_CONFIG_PATH, encoding="utf-8-sig") as f:
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
    cfg["compact_mode"] = bool(cfg.get("compact_mode", False))
    cfg["overlay_scale"] = _clamp_scale(cfg.get("overlay_scale", SCALE_DEFAULT))
    cfg["dismissed_update"] = str(cfg.get("dismissed_update") or "").strip()
    cfg["tutorial_done"] = bool(cfg.get("tutorial_done", False))
    try:
        cfg["mini_offset"] = max(0, int(cfg.get("mini_offset") or 0))
    except (TypeError, ValueError):
        cfg["mini_offset"] = 0
    return cfg


def save_app_config(*, show_claude: bool | None = None,
                    show_cursor: bool | None = None,
                    cursor_name: str | None = None,
                    compact_mode: bool | None = None,
                    overlay_scale: float | None = None,
                    dismissed_update: str | None = None,
                    tutorial_done: bool | None = None,
                    mini_offset: int | None = None) -> None:
    cfg = load_app_config()
    try:
        with open(APP_CONFIG_PATH, encoding="utf-8-sig") as f:
            json.load(f)
    except FileNotFoundError:
        pass
    except Exception:
        # Unreadable: keep a copy instead of silently replacing the user's
        # settings with defaults.
        try:
            os.replace(APP_CONFIG_PATH, APP_CONFIG_PATH + ".bad")
        except OSError:
            pass
    if show_claude is not None:
        cfg["show_claude"] = bool(show_claude)
    if show_cursor is not None:
        cfg["show_cursor"] = bool(show_cursor)
    if cursor_name is not None:
        cfg["cursor_name"] = cursor_name.strip()[:40]
    if compact_mode is not None:
        cfg["compact_mode"] = bool(compact_mode)
    if overlay_scale is not None:
        cfg["overlay_scale"] = _clamp_scale(overlay_scale)
    if dismissed_update is not None:
        cfg["dismissed_update"] = str(dismissed_update).strip()
    if tutorial_done is not None:
        cfg["tutorial_done"] = bool(tutorial_done)
    if mini_offset is not None:
        cfg["mini_offset"] = max(0, int(mini_offset))
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


def _parse_version(text: str) -> tuple[int, int] | None:
    """Major.minor from tags like v1.5-overlay or titles like Token Maxxing v1.5."""
    m = re.search(r"v?(\d+)\.(\d+)", str(text or ""), re.I)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _version_label(ver: tuple[int, int]) -> str:
    return f"{ver[0]}.{ver[1]}"


def fetch_latest_release() -> dict | None:
    """Latest GitHub Release on the fork, or None on any failure."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    headers = {
        "User-Agent": f"TokenMaxxing/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
    }
    status, resp = _http_json("GET", url, headers=headers, timeout=12,
                              honor_retry_after=False)
    if status != 200 or not isinstance(resp, dict):
        return None
    tag = str(resp.get("tag_name") or "")
    name = str(resp.get("name") or "")
    ver = _parse_version(tag) or _parse_version(name)
    if not ver:
        return None
    html = (str(resp.get("html_url") or "").strip()
            or f"https://github.com/{GITHUB_REPO}/releases/latest")
    return {
        "tuple":   ver,
        "version": _version_label(ver),
        "url":     html,
        "tag":     tag,
    }


START_RETRY_S = 30 * 60           # after a failed auto-start, wait before retrying
_start_failed_at: dict[str, float] = {}


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
    # A 429 here (e.g. weekly quota spent) must not pause usage polling.
    status, _resp = _http_json("POST", MESSAGES_URL, headers=headers, body=body,
                               honor_retry_after=False)
    return status == 200


def _http_json(method: str, url: str, headers: dict | None = None,
               body: dict | None = None, form: dict | None = None,
               timeout: int = 20, honor_retry_after: bool = True
               ) -> tuple[int, dict | str]:
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
        if e.code == 429 and honor_retry_after:
            global _rate_limit_until
            try:
                ra = float(e.headers.get("Retry-After", "") or 0)
            except (TypeError, ValueError):
                ra = 0.0
            # Server sends Retry-After: ~299s. Fall back to 300s if absent.
            # Wall-clock deadline so it expires across a sleep/suspend (see note
            # at the _rate_limit_until definition). Persist so a relaunch
            # during cooldown does not immediately 429 again.
            _rate_limit_until = time.time() + (ra if ra > 0 else 300.0)
            _save_usage_cache(rate_limit_until=_rate_limit_until)
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
    status, resp = _http_json("POST", TOKEN_URL, body=payload,
                              honor_retry_after=False)
    if status in (400, 415, 422):
        status, resp = _http_json("POST", TOKEN_URL, form=payload,
                                  honor_retry_after=False)
    if status != 200 or not isinstance(resp, dict) or "access_token" not in resp:
        raise RuntimeError(f"token refresh failed (HTTP {status}): {resp}")
    oauth["accessToken"] = resp["access_token"]
    if resp.get("refresh_token"):
        oauth["refreshToken"] = resp["refresh_token"]
    if resp.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(resp["expires_in"]) * 1000
    _persist_oauth(path, oauth)
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
    status, resp = _http_json("POST", TOKEN_URL, body=payload,
                              honor_retry_after=False)
    if status in (400, 415, 422):
        status, resp = _http_json("POST", TOKEN_URL, form=payload,
                                  honor_retry_after=False)
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
    _persist_oauth(path, oauth, create=True)


_shutting_down = False


def _wait_for_token_writes(timeout: float = 45.0) -> None:
    """Block exit until no refresh is mid-flight (it holds _lock from the token
    request through the save, worst case two 20 s requests), and stop any new
    refresh from starting, so quitting can't strand a rotated token."""
    global _shutting_down
    _shutting_down = True
    if _lock.acquire(timeout=timeout):
        _lock.release()


def _get_token(path: str, force: bool = False) -> str:
    with _lock:
        if path in _pending_oauth:
            oauth = _pending_oauth[path]
            _persist_oauth(path, oauth)             # retry the failed save
        else:
            oauth = _load_oauth(path)
        near_expiry = (oauth.get("expiresAt", 0) - time.time() * 1000) < 300_000
        if force or near_expiry:
            if _shutting_down:
                raise RuntimeError("shutting down")
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


def _plan_from_profile(prof: dict) -> str:
    """Short plan badge ("MAX5x", "MAX20x", "PRO", "TEAM", ...) from /profile."""
    acct = prof.get("account") or {}
    org = prof.get("organization") or {}
    tier = str(org.get("rate_limit_tier") or "").lower()
    otype = str(org.get("organization_type") or "").lower()
    if "max" in otype or "max" in tier or acct.get("has_claude_max"):
        m = re.search(r"(\d+)x", tier)
        return f"MAX{m.group(1)}x" if m else "MAX"
    if "pro" in otype or acct.get("has_claude_pro"):
        return "PRO"
    if otype:
        return otype.replace("claude_", "").upper()[:6]
    return "FREE"


def _account_plan(path: str, headers: dict) -> str:
    """Plan badge for an account, cached in its credential file for PLAN_TTL_S."""
    try:
        doc = _load_doc(path)
        cached = doc.get("plan") if isinstance(doc, dict) else None
    except Exception:
        cached = None
    if (isinstance(cached, dict) and cached.get("badge")
            and time.time() - float(cached.get("checked_at") or 0) < PLAN_TTL_S):
        return cached["badge"]
    try:
        status, prof = _http_json("GET", PROFILE_URL, headers=headers,
                                  honor_retry_after=False)
        if status == 200 and isinstance(prof, dict):
            badge = _plan_from_profile(prof)
            uuid = (prof.get("account") or {}).get("uuid") or ""
            _update_cred_doc(path, lambda d: _stamp_identity(d, uuid, badge))
            return badge
    except Exception:
        pass
    return cached.get("badge", "") if isinstance(cached, dict) else ""


def _identify(token: str) -> tuple[str, str]:
    """(account uuid, plan badge) for a fresh token; blanks if /profile fails."""
    try:
        status, prof = _http_json("GET", PROFILE_URL, headers={
            "Authorization": f"Bearer {token}", "anthropic-beta": BETA_HEADER},
            honor_retry_after=False)
        if status == 200 and isinstance(prof, dict):
            return ((prof.get("account") or {}).get("uuid") or "",
                    _plan_from_profile(prof))
    except Exception:
        pass
    return "", ""


def _stamp_identity(doc: dict, uuid: str, plan: str) -> None:
    if uuid:
        doc["accountUuid"] = uuid
    if plan:
        doc["plan"] = {"badge": plan, "checked_at": time.time()}


def _credential_path_for(uuid: str, plan: str, add: bool) -> str:
    """Where a new sign-in is saved.

    Signing in to an account we already have replaces that file (so re-auth on
    the Pro account never clobbers the Max one). Otherwise "Add account" gets a
    new credentials-<plan>.json and plain re-auth keeps using credentials.json.
    """
    files = _cred_files()
    default = os.path.join(CRED_DIR, "credentials.json")

    def stamped(f: str) -> str:
        try:
            return _load_doc(f).get("accountUuid") or ""
        except Exception:
            return ""

    if uuid:
        for f in files:
            if stamped(f) == uuid:
                return f
    if not files:
        return default
    if not add:
        if not uuid and len(files) > 1:
            # Can't tell which account this is; guessing could overwrite the
            # other account's tokens.
            raise RuntimeError("couldn't identify the account (profile lookup "
                               "failed). Try again in a minute.")
        # Only reuse a file we can't prove belongs to someone else: the lone
        # file, and only if it isn't stamped with a different account.
        if len(files) == 1 and not (uuid and stamped(files[0])):
            return files[0]
        # Otherwise fall through and save it as a new account.
    slug = re.sub(r"[^a-z]", "", (plan or "account").lower()) or "account"
    path, n = os.path.join(CRED_DIR, f"credentials-{slug}.json"), 2
    while os.path.exists(path):
        path = os.path.join(CRED_DIR, f"credentials-{slug}-{n}.json")
        n += 1
    return path


def _limit_label(lim: dict) -> str:
    kind = lim.get("kind") or ""
    if kind == "session":
        return "SESSION"
    if kind == "weekly_all":
        return "WEEKLY"
    scope = lim.get("scope") or {}
    names = [((scope.get(k) or {}).get("display_name") or "").strip()
             for k in ("model", "surface")]
    name = " ".join(n for n in names if n)
    return (name or kind.replace("_", " ")).upper()


def _extra_limits(resp: dict) -> list[dict]:
    """Model/surface-scoped windows beyond session + weekly (e.g. Max's per-model
    weekly cap) — the same rows claude.ai's usage page shows.

    Prefers the server's `limits` list; falls back to the legacy
    seven_day_opus / seven_day_sonnet fields on older responses.
    """
    extras = []
    limits = resp.get("limits")
    if isinstance(limits, list):
        for lim in limits:
            if not isinstance(lim, dict) or lim.get("kind") in ("session", "weekly_all"):
                continue
            try:
                badge = {"weekly": "7d", "session": "5h"}.get(lim.get("group"), "")
                extras.append({
                    "label": _limit_label(lim)[:12],
                    "badge": badge,
                    "pct":   max(0, min(100, int(round(float(lim.get("percent") or 0))))),
                    "min":   _to_minutes(lim.get("resets_at")),
                })
            except Exception:
                continue            # skip a malformed row, keep the account
        return extras
    for key, label in (("seven_day_opus", "OPUS"), ("seven_day_sonnet", "SONNET")):
        w = resp.get(key)
        if isinstance(w, dict):
            extras.append({
                "label": label, "badge": "7d",
                "pct":   max(0, min(100, int(round(w.get("utilization") or 0)))),
                "min":   _to_minutes(w.get("resets_at")),
            })
    return extras


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
    if _rate_limited_remaining() > 0:
        cached = [a for a in (_load_usage_cache().get("accounts") or [])
                  if a.get("kind") != "cursor"]
        if cached:
            return cached
        wait = int(round(_rate_limited_remaining())) or 300
        err = f"Rate limited — retry in {wait}s"
        for idx, path in enumerate(files):
            results.append({
                "label": _account_label(path, idx), "path": path,
                "session_pct": 0, "session_min": None,
                "weekly_pct": 0, "weekly_min": None,
                "active": False, "ok": False, "error": err,
            })
        return results or [{
            "label": "Rate limited", "ok": False, "error": err,
            "session_pct": 0, "session_min": None,
            "weekly_pct": 0, "weekly_min": None, "active": False,
        }]
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
            elif (cfg["auto_start"] and not cfg["session_started"]
                  and time.time() - _start_failed_at.get(path, 0) > START_RETRY_S):
                if not _start_session(token):
                    _start_failed_at[path] = time.time()
                else:
                    status, resp = _http_json("GET", USAGE_URL, headers=headers)
                    if status == 200 and isinstance(resp, dict):
                        fh = resp.get("five_hour") or {}
                        sd = resp.get("seven_day") or {}
                        active = fh.get("resets_at") is not None
                        if not active:
                            _save_account_settings(path, session_started=True)
            s_pct = max(0, min(100, int(round(fh.get("utilization", 0) or 0))))
            w_pct = max(0, min(100, int(round(sd.get("utilization", 0) or 0))))
            results.append({
                "label":       label,
                "path":        path,
                "session_pct": s_pct,
                "session_min": _to_minutes(fh.get("resets_at")),
                "weekly_pct":  w_pct,
                "weekly_min":  _to_minutes(sd.get("resets_at")),
                "extra":       _extra_limits(resp),
                "plan":        _account_plan(path, headers),
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


WEEK_MIN       = 7 * 24 * 60
PACE_GRACE_MIN = 12 * 60                   # too early in the week to judge pace


def _weekly_pace(pct: int, reset_min) -> dict | None:
    """Usage vs an even burn of the 7d window: on day 2 an even pace is 2/7 ≈ 29%.
    None when there's nothing to judge (no reset time, first 12h, limit hit)."""
    if not reset_min or reset_min <= 0 or pct < 0 or pct >= 100:
        return None
    elapsed = WEEK_MIN - min(int(reset_min), WEEK_MIN)
    if elapsed < PACE_GRACE_MIN:
        return None
    expected = elapsed * 100 / WEEK_MIN
    ahead = pct - expected
    # Signed points vs even pace: "+12" = burning faster, "-8" = room to spare.
    d = max(-99, min(99, round(ahead)))
    delta = f"{d:+d}" if d else "0"
    if ahead <= 0:
        word, col = "UNDER", C_GREEN
    elif ahead <= 5:
        word, col = "ON PACE", C_GREEN
    elif ahead <= 15:
        word, col = "FAST", C_YELLOW
    else:
        word, col = "SLOW", C_RED
    return {"expected": expected, "ahead": ahead, "delta": delta,
            "label": f"{delta} {word}", "col": col}


def _draw_pace_tick(g: Gfx, x, y, w, h, expected: float) -> None:
    """White marker on a meter where an even weekly pace would be right now."""
    tx = x + round(w * max(0.0, min(100.0, expected)) / 100)
    g.draw_fast_vline(tx - 1, y - 2, h + 4, C_PANEL)
    g.draw_fast_vline(tx + 1, y - 2, h + 4, C_PANEL)
    g.draw_fast_vline(tx, y - 2, h + 4, C_TEXT)


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


def _cube_shade(shade: float, ok: bool, light_bg: bool = False) -> QColor:
    if light_bg and ok:
        v = int(40 + 80 * (1.0 - shade))      # lit faces darkest → still reads 3D
        return QColor(v, v, v + 6)
    r, gc, b = (236, 236, 242) if ok else (250, 92, 88)
    return QColor(int(r * shade), int(gc * shade), int(b * shade))


def _draw_cursor_mark(g: Gfx, cx, cy, ok: bool, t: float, size: float = 3.9,
                      light_bg: bool = False) -> None:
    """Cursor's cube logo, spinning about its vertical axis (Claude's spark analog).

    `size` is the cube half-extent in pixels (dashboard default 3.9). After the
    isometric tilt, the on-screen radius is about 1.75×size — pass a smaller
    size from the mini strip so the cube matches the Claude spark.
    """
    breathe = 0.92 + 0.08 * math.sin(t * 1.6)
    s = size * breathe
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
        col = ((C_TEXT if not light_bg else QColor(0, 0, 0)) if blip
               else _cube_shade(shade, ok, light_bg))
        g.fill_triangle(pts[0][0], pts[0][1], pts[1][0], pts[1][1],
                        pts[2][0], pts[2][1], col)
        g.fill_triangle(pts[0][0], pts[0][1], pts[2][0], pts[2][1],
                        pts[3][0], pts[3][1], col)


def _draw_metric_card(g: Gfx, y0, label, badge, pct, reset_min, ok: bool, t: float,
                      brand: QColor = C_CLAUDE, pace: dict | None = None) -> None:
    idle = pct < 0
    col = C_DIM if idle else _pct_color(pct)
    red = (not idle and ok and pct >= 85)
    num_col = col if ok else C_DIM
    now_ms = int(time.time() * 1000)
    pace = pace if ok and not idle else None

    g.text(label, 6, y0, brand if ok else C_DIM, 1)
    lw = _text_w(label, 1)
    hx = 6 + lw + 5 + _draw_badge(g, 6 + lw + 5, y0 - 1, badge, C_DIM)
    if pace:
        hx += 4 + _draw_badge(g, hx + 4, y0 - 1, pace["label"], pace["col"])

    if red and (now_ms % 900) < 450:
        wx = hx + 5 + 5
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
    if pace:
        _draw_pace_tick(g, 6, y0 + 40, 148, 7, pace["expected"])


EXTRA_ROW_H = 32                           # logical px per model-scoped limit row


def _plan_badge_x(plan: str) -> int:
    return 130 - (_text_w(plan, 1) + 5)    # right-aligned just left of the poll ring


def _header_chars(plan: str) -> int:
    """Account-name chars that fit in the header beside the plan badge."""
    if not plan:
        return 21
    return max(0, (_plan_badge_x(plan) - 8) // 6)


def _draw_extra_row(g: Gfx, y0, row: dict, ok: bool, brand: QColor) -> None:
    """Compact row for a scoped limit (e.g. Max's per-model weekly cap)."""
    pct = row.get("pct", 0)
    col = _pct_color(pct) if ok else C_DIM
    label = row.get("label") or "LIMIT"
    g.text(label, 6, y0, brand if ok else C_DIM, 1)
    if row.get("badge"):
        _draw_badge(g, 6 + _text_w(label, 1) + 5, y0 - 1, row["badge"], C_DIM)
    rs = _fmt_dur(row.get("min") or 0)
    rw = _text_w(rs, 1)
    g.text(rs, 154 - rw, y0, C_TEXT if ok else C_DIM, 1)
    _draw_clock(g, 154 - rw - 7, y0 + 3, brand if ok else C_DIM)
    ps = f"{pct}%"
    g.text(ps, 6, y0 + 11, col, 2)
    _draw_meter(g, 58, y0 + 15, 96, 7, pct,
                (C_RED if pct >= 85 else col) if ok else None)

def _draw_dashboard(g: Gfx, d: dict, t: float,
                    poll_frac: float = 0.0, fetching: bool = False,
                    extra_rows: int = 0) -> None:
    s, w = d["session"], d["weekly"]
    acct = d.get("account") or "CLAUDE USAGE"
    ok = d.get("ok", True)

    cursor = d.get("kind") == "cursor"
    brand = C_CURSOR if cursor else C_CLAUDE

    plan = d.get("plan") if ok else ""
    if plan:
        _draw_badge(g, _plan_badge_x(plan), 1, plan, brand)
    g.text(acct[:_header_chars(plan) if ok else 23], 5, 2, C_DIM if ok else C_RED, 1)
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
                      w.get("resets_in_min", 0), ok, t, brand,
                      None if cursor else _weekly_pace(w["pct"], w.get("resets_in_min")))

    # Model-scoped limits (e.g. Max's FABLE weekly) below the two main cards.
    extras = d.get("extra") or []
    for i in range(min(extra_rows, len(extras))):
        y0 = 128 + i * EXTRA_ROW_H + 3
        g.draw_fast_hline(6, y0 - 4, 148, C_TRACK)
        _draw_extra_row(g, y0, extras[i], ok, brand)


# ── Stacked view: every account at once (used when there are 2+ accounts) ───

STACK_TOP   = 16                           # below the title bar + accent line
STACK_HEAD  = 12                           # account name / plan badge row
STACK_ROW   = 12                           # one limit row
STACK_GAP   = 6                            # padding + divider between accounts


def _fmt_short(minutes) -> str:
    if not minutes or minutes <= 0:
        return "--"
    d, rem = divmod(int(minutes), 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _stack_section(a: dict) -> dict:
    """Account dict (from the fetchers) → header + limit rows for the stack."""
    cursor = a.get("kind") == "cursor"
    ok = a.get("ok", False)
    sec = {"name": a.get("label") or "", "plan": a.get("plan") or "",
           "cursor": cursor, "ok": ok, "error": a.get("error") or "", "rows": []}
    if not ok:
        return sec
    if cursor:
        sec["rows"] = [("AUTO", a.get("session_pct", 0), a.get("session_min"), None),
                       ("API",  a.get("weekly_pct", 0),  a.get("weekly_min"), None)]
        return sec
    active = a.get("active", False)
    w_pct, w_min = a.get("weekly_pct", 0), a.get("weekly_min")
    sec["rows"] = [("SESSION", a.get("session_pct", 0) if active else -1,
                    a.get("session_min") if active else None, None),
                   ("WEEKLY", w_pct, w_min, _weekly_pace(w_pct, w_min))]
    sec["rows"] += [(x.get("label", ""), x.get("pct", 0), x.get("min"), None)
                    for x in a.get("extra") or []]
    return sec


def _stack_height(sections: list[dict]) -> int:
    h = STACK_TOP
    for s in sections:
        h += STACK_HEAD + STACK_ROW * max(1, len(s["rows"])) + STACK_GAP
    return h - STACK_GAP + 4


def _draw_stacked(g: Gfx, d: dict, t: float,
                  poll_frac: float = 0.0, fetching: bool = False) -> None:
    sections = d["sections"]
    any_ok = any(s["ok"] for s in sections)
    title = d.get("notice") or "TOKEN MAXXING"
    g.text(title[:22], 5, 2, C_DIM, 1)
    # No corner logo here: every account row already has its own spark/cube.
    g.draw_poll_ring(150, 6, 5, poll_frac, fetching, t)
    g.draw_fast_hline(0, 12, 160, C_ACCENT if any_ok else C_RED)

    y = STACK_TOP
    for i, s in enumerate(sections):
        if i:
            g.draw_fast_hline(6, y - STACK_GAP // 2 - 1, 148, C_TRACK)
        brand = C_CURSOR if s["cursor"] else C_CLAUDE
        ok = s["ok"]
        name_chars = 24
        if s["plan"] and ok:
            bx = 154 - (_text_w(s["plan"], 1) + 5)
            _draw_badge(g, bx, y, s["plan"], C_DIM)
            name_chars = max(0, (bx - 10) // 6)
        # Same-size animated mark beside every name: Cursor cube / Claude spark.
        if s["cursor"]:
            _draw_cursor_mark(g, 9, y + 4, ok, t, size=2.4)
        else:
            blip = ok and (int(time.time() * 1000) % 5000) < 450
            col = C_CLAUDE if ok else C_RED
            _draw_spark(g, 9, y + 4, t, 5.4 if blip else 5.0, 1.6, 6, col,
                        C_TEXT if blip else col)
        g.text(s["name"][:name_chars - 2], 17, y + 1, brand if ok else C_RED, 1)
        y += STACK_HEAD

        if not ok:
            g.text((s["error"] or "error")[:24], 6, y + 1, C_DIM, 1)
            y += STACK_ROW + STACK_GAP
            continue
        for label, pct, mins, pace in s["rows"]:
            idle = pct < 0
            col = C_DIM if idle else _pct_color(pct)
            g.text(label[:7], 6, y + 1, C_DIM, 1)
            ps = "--" if idle else f"{pct}%"
            g.text(ps, 73 - _text_w(ps, 1), y + 1, col, 1)
            # Narrow bar leaves a column for the signed weekly pace ("+12").
            g.fill_rect(76, y + 2, 24, 5, C_TRACK)
            fw = 0 if idle else round(24 * max(0, min(100, pct)) / 100)
            if fw:
                g.fill_rect(76, y + 2, fw, 5, C_RED if pct >= 85 else col)
            if pace:
                _draw_pace_tick(g, 76, y + 2, 24, 5, pace["expected"])
                g.text(pace["delta"], 121 - _text_w(pace["delta"], 1), y + 1,
                       pace["col"], 1)
            rs = "idle" if idle else _fmt_short(mins)
            g.text(rs, 154 - _text_w(rs, 1), y + 1, C_DIM if idle else C_TEXT, 1)
            y += STACK_ROW
        y += STACK_GAP


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


def _mini_font(h: int) -> QFont:
    f = QFont("Segoe UI")
    f.setPixelSize(max(9, min(12, (h - 8) // 3)))
    f.setWeight(QFont.Weight.DemiBold)
    return f


def _mini_groups(accounts: list[dict]) -> list[dict]:
    """One strip group per account: logo, short tag, and two value lines."""
    groups = []
    for a in accounts:
        cursor = a.get("kind") == "cursor"
        ok = bool(a.get("ok"))
        active = a.get("active", False)
        if cursor:
            tag = "CUR"
            lines = [("A", a.get("session_pct", 0)), ("P", a.get("weekly_pct", 0))]
        else:
            tag = re.sub(r"[^A-Z]", "", (a.get("plan") or "").upper())[:3]                 or (a.get("label") or "?")[:3].upper()
            lines = [("S", a.get("session_pct", 0) if active else -1),
                     ("W", a.get("weekly_pct", 0))]
        groups.append({"cursor": cursor, "ok": ok, "tag": tag, "lines": lines,
                       "name": a.get("label") or ""})
    return groups


MINI_PAD, MINI_GAP, MINI_LOGO = 4, 7, 13


def _mini_width(groups: list[dict], h: int) -> int:
    fm = QFontMetrics(_mini_font(h))
    val_w = fm.horizontalAdvance("100")
    n = max(1, len(groups))
    return MINI_PAD * 2 + n * (MINI_LOGO + 3 + val_w) + (n - 1) * MINI_GAP


def _draw_mini(p: QPainter, groups: list[dict], t: float, w: int, h: int,
               update: bool = False, bg: QColor | None = None) -> None:
    """Taskbar strip: every account side by side. Logo + plan tag, then two
    numbers: top = session (Cursor: Auto), bottom = weekly (Cursor: API)."""
    light = _is_light(bg)
    dim = QColor(96, 96, 104) if light else C_DIM
    f = _mini_font(h)
    p.setFont(f)
    fm = QFontMetrics(f)
    val_w = fm.horizontalAdvance("100")
    line_h = fm.height()
    top = (h - 2 * line_h) / 2
    tag_f = QFont("Segoe UI")
    tag_f.setPixelSize(max(7, min(8, h // 6)))
    tag_f.setWeight(QFont.Weight.Bold)
    align_r = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    align_c = int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

    if update:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(C_ACCENT)
        p.drawRoundedRect(QRectF(2, 5, 3, max(8, h - 10)), 1.5, 1.5)

    x = MINI_PAD
    g = Gfx(p)
    for grp in groups or [{"cursor": False, "ok": True, "tag": "",
                           "lines": [("S", -1), ("W", -1)], "name": ""}]:
        ok = grp["ok"]
        # Logo in the upper part, plan/source tag underneath.
        cx, cy = x + MINI_LOGO / 2, h * 0.38
        r = MINI_LOGO * 0.46
        if grp["cursor"]:
            _draw_cursor_mark(g, cx, cy, ok, t, size=r / 1.75, light_bg=light)
        else:
            blip = ok and (int(time.time() * 1000) % 5000) < 450
            col = C_CLAUDE if ok else C_RED
            _draw_spark(g, cx, cy, t, r if blip else r * 0.92,
                        max(1.5, r * 0.32), 6, col, C_TEXT if blip else col)
        p.setFont(tag_f)
        p.setPen(dim)
        p.drawText(QRectF(x - 6, h * 0.62, MINI_LOGO + 12, h * 0.3), align_c, grp["tag"])
        p.setFont(f)

        vx = x + MINI_LOGO + 3
        for i, (_lab, pct) in enumerate(grp["lines"]):
            y = top + i * line_h
            if not ok or pct is None or pct < 0:
                txt, col = "--", dim
            else:
                pct = max(0, min(100, int(pct)))
                txt, col = str(pct), _pct_color(pct, light)
            p.setPen(col)
            p.drawText(QRectF(vx, y, val_w, line_h), align_r, txt)
        x = vx + val_w + MINI_GAP


class DashboardCanvas(QWidget):
    """Renders drawDashboard / drawBoot from simulator.html at the chosen scale."""

    def __init__(self, parent=None, scale: float = SCALE_DEFAULT):
        super().__init__(parent)
        self._mode = "boot"
        self._boot_status = "Fetching usage..."
        self._dash: dict | None = None
        self._t = 0.0
        self._poll_interval = POLL_MS / 1000.0
        self._last_fetch_at: float | None = None
        self._fetching = False
        self._scale = SCALE_DEFAULT
        self._logical_h = 128
        self.set_scale(scale)
        tm = QTimer(self)
        tm.timeout.connect(self._tick)
        tm.start(33)

    def set_scale(self, scale: float) -> None:
        self._scale = _clamp_scale(scale)
        self.setFixedSize(round(160 * self._scale),
                          round(self.logical_height() * self._scale))
        self.update()

    def logical_height(self) -> int:
        return self._logical_h

    def set_logical_height(self, h: int) -> bool:
        """Fit the content (extra limit rows / stacked accounts); True if changed."""
        if h == self._logical_h:
            return False
        self._logical_h = h
        self.set_scale(self._scale)
        return True

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

    def mark_refreshed(self, age_s: float = 0.0) -> None:
        self._last_fetch_at = time.monotonic() - max(0.0, float(age_s))
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
        p.scale(self._scale, self._scale)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)
        g = Gfx(p)
        if self._mode == "boot":
            _draw_boot(g, self._boot_status, self._t)
        elif self._dash and "sections" in self._dash:
            _draw_stacked(g, self._dash, self._t, self._poll_frac(), self._fetching)
        elif self._dash:
            _draw_dashboard(g, self._dash, self._t, self._poll_frac(), self._fetching,
                            len(self._dash.get("extra") or []))
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


class UpdateBanner(QWidget):
    """Thin bar at the bottom of the full overlay: new GitHub release available."""

    open_clicked = pyqtSignal()
    dismiss_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(UPDATE_BANNER_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 4, 0)
        lay.setSpacing(4)

        self._label = QLabel("Update available")
        self._label.setStyleSheet(
            "color: #00A8F8; font-size: 10px; font-weight: 700; "
            "background: transparent;")
        lay.addWidget(self._label, 1)

        self._x = QPushButton("✕")
        self._x.setFixedSize(22, 22)
        self._x.setCursor(Qt.CursorShape.PointingHandCursor)
        self._x.setToolTip("Dismiss this version")
        self._x.setStyleSheet("""
            QPushButton {
                background: transparent; color: #808098;
                border: none; font-size: 10px; font-weight: 700;
            }
            QPushButton:hover { color: #EBEBF0; }
        """)
        self._x.clicked.connect(self.dismiss_clicked.emit)
        lay.addWidget(self._x)

    def set_version(self, version: str) -> None:
        self._label.setText(f"Update {version}  ·  click to download")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(e.position().toPoint())
            if child is not self._x:
                self.open_clicked.emit()
                e.accept()
                return
        super().mousePressEvent(e)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setPen(QPen(C_BORDER, 1.0))
        y = 0.5
        p.drawLine(QPointF(10, y), QPointF(self.width() - 10, y))


# ── Threads ───────────────────────────────────────────────────────────────────

class FetchThread(QThread):
    done = pyqtSignal(list)

    def run(self):
        # Always emit: _fetching is only cleared by `done`, so a stray
        # exception here would otherwise stop polling for good.
        try:
            accounts = fetch_all_accounts()
        except Exception as exc:
            accounts = [{"label": "Error", "ok": False, "error": str(exc)[:60],
                         "session_pct": 0, "session_min": None,
                         "weekly_pct": 0, "weekly_min": None, "active": False}]
        self.done.emit(accounts)


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

    def __init__(self, parent, code: str, verifier: str, add: bool = False):
        super().__init__(parent)
        self._code     = code
        self._verifier = verifier
        self._add      = add

    def run(self):
        try:
            resp = _exchange_code(self._code, self._verifier)
            os.makedirs(CRED_DIR, exist_ok=True)
            uuid, plan = _identify(resp["access_token"])
            path = _credential_path_for(uuid, plan, self._add)
            _save_new_credential(resp, path)
            if uuid or plan:
                _update_cred_doc(path, lambda d: _stamp_identity(d, uuid, plan))
            self.done.emit(True, "")
        except Exception as e:
            self.done.emit(False, str(e))


class UpdateCheckThread(QThread):
    done = pyqtSignal(object)   # dict | None

    def run(self):
        try:
            self.done.emit(fetch_latest_release())
        except Exception:
            self.done.emit(None)


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
        self._compact:      bool            = False
        self._full_pos:     QPoint | None   = None
        self._scale:        float           = _clamp_scale(
            load_app_config()["overlay_scale"])
        self._update_info:  dict | None     = None
        self._update_checking: bool         = False
        self._tut_step:     int             = 0
        self._exiting:      bool            = False
        self._applying_chrome: bool         = False
        self._mini_offset:  int             = load_app_config()["mini_offset"]
        self._mini_bg:      QColor | None   = None
        self._mini_backdrop: QImage | None  = None
        self._mini_drag:    tuple | None    = None

        self._init_window()
        self._build_ui()
        self._start_update_checks()

        if _cred_files():
            self._show_main()
        else:
            self._show_auth()

    def _full_size(self) -> tuple[int, int]:
        pad = PANEL_PAD
        scale = getattr(self, "_scale", SCALE_DEFAULT)
        canvas = getattr(self, "_canvas", None)
        w = round(160 * scale) + pad * 2
        h = round((canvas.logical_height() if canvas else 128) * scale) + pad * 2
        stack = getattr(self, "_stack", None)
        if stack is not None and stack.currentIndex() in (1, 2):
            # Sign-in / Settings are Qt forms, not the scaled canvas: give
            # them room even at small sizes.
            w, h = max(w, FORM_MIN_W), max(h, FORM_MIN_H)
        if (not self._compact
                and getattr(self, "_update_banner", None) is not None
                and self._update_banner.isVisible()):
            h += UPDATE_BANNER_H
        return w, h

    def _apply_full_size(self) -> None:
        if self._compact:
            return
        fw, fh = self._full_size()
        self.setFixedSize(fw, fh)
        self._keep_on_screen()

    def _keep_on_screen(self) -> None:
        """Pull the widget back inside a visible screen (monitor unplugged,
        many accounts making it tall)."""
        if self._compact:
            return
        geo = self.frameGeometry()
        screen = (QGuiApplication.screenAt(geo.center())
                  or QGuiApplication.screenAt(geo.topLeft())
                  or QGuiApplication.primaryScreen())
        if screen is None:
            return
        avail = screen.availableGeometry()
        x = min(max(geo.x(), avail.x()), avail.x() + avail.width() - geo.width())
        y = min(max(geo.y(), avail.y()), avail.y() + avail.height() - geo.height())
        x, y = max(x, avail.x()), max(y, avail.y())
        if (x, y) != (geo.x(), geo.y()):
            self.move(x, y)

    def _set_scale(self, scale: float, persist: bool = True) -> None:
        scale = _clamp_scale(scale)
        self._scale = scale
        if persist:
            save_app_config(overlay_scale=scale)
        if hasattr(self, "_canvas"):
            self._canvas.set_scale(scale)
        self._apply_full_size()
        self._refresh_tutorial_readout()
        self.update()

    def setWindowOpacity(self, level):
        super().setWindowOpacity(level)
        self._refresh_tutorial_readout()

    def _apply_chrome(self, compact: bool) -> None:
        self._applying_chrome = True
        try:
            flags = (Qt.WindowType.FramelessWindowHint |
                     Qt.WindowType.WindowStaysOnTopHint)
            flags |= Qt.WindowType.Tool if compact else Qt.WindowType.Window
            self.setWindowFlags(flags)
            # Acrylic + translucent child of the Win11 taskbar = DWM static.
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground,
                              not compact)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, compact)
            self.setAutoFillBackground(compact)
            if compact:
                pal = self.palette()
                pal.setColor(self.backgroundRole(), C_BG_MINI)
                self.setPalette(pal)
            else:
                app = QApplication.instance()
                if app is not None:
                    self.setPalette(app.palette())
            self.show()
            if compact:
                _disable_acrylic(int(self.winId()))
            else:
                _enable_acrylic(int(self.winId()))
                _win_pin_overlay(int(self.winId()), gadget=False)
            wh = self.windowHandle()
            if wh is not None:
                try:
                    wh.screenChanged.disconnect(self._on_screen_changed)
                except TypeError:
                    pass
                wh.screenChanged.connect(self._on_screen_changed)
            QTimer.singleShot(0, self._win_apply_native_icon)
        finally:
            self._applying_chrome = False

    def _ensure_dock_timers(self) -> None:
        if not hasattr(self, "_dock_timer"):
            self._dock_timer = QTimer(self)
            self._dock_timer.timeout.connect(self._dock_mini)
            self._mini_tick = QTimer(self)
            self._mini_tick.timeout.connect(self._mini_frame)

    def _mini_frame(self) -> None:
        if not self._compact or self._exiting:
            return
        self.update()
        _win_redraw(int(self.winId()))

    def _enter_mini(self, persist: bool = True) -> None:
        if self._stack.currentIndex() != 0:
            return
        if persist:
            save_app_config(compact_mode=True)
        self._ensure_dock_timers()
        if self._compact:
            if not self._mini_tick.isActive():
                self._mini_tick.start(66)
            self._dock_mini()
            return
        self._full_pos = self.pos()
        self._compact = True
        self._update_banner.hide()
        self._stack.hide()
        # Drop the full-mode fixed size; the dock sizes the strip natively.
        self.setMinimumSize(1, 1)
        self.setMaximumSize(16777215, 16777215)
        self._apply_chrome(compact=True)
        self._dock_timer.start(DOCK_MS)
        self._mini_tick.start(66)
        self._dock_mini()
        self._mini_frame()

    def _enter_full(self, persist: bool = True) -> None:
        if persist:
            save_app_config(compact_mode=False)
        if not self._compact:
            return
        try:
            _win_embed_tray(int(self.winId()), False)
        except Exception:
            pass
        self._compact = False
        if hasattr(self, "_dock_timer"):
            self._dock_timer.stop()
            self._mini_tick.stop()
        self._stack.show()
        self._apply_chrome(compact=False)
        if self._update_info:
            self._update_banner.show()
        self._apply_full_size()
        if self._full_pos is not None:
            self.move(self._full_pos)
        self._keep_on_screen()
        self.update()

    def _dock_mini(self) -> None:
        if not self._compact or self._exiting:
            return
        if self.windowState() & Qt.WindowState.WindowMinimized:
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        if not self.isVisible():
            self.show()
        rect = _mini_dock_rect()
        h = rect.height() if rect is not None and rect.isValid() else MINI_H
        w = _mini_width(_mini_groups(self._accounts), h)
        tray_hwnd, _notify = _win_tray_windows()
        trect = _win_window_rect(tray_hwnd) if tray_hwnd else None
        if trect is None:
            # No taskbar to dock into: plain floating strip.
            if self.width() != w or self.height() != h:
                self.setFixedSize(w, h)
            return
        # Size the tray child in native pixels (see _win_embed_tray).
        dpr = (trect[3] - trect[1]) / max(1, h) if (trect[2] - trect[0]) >= (
            trect[3] - trect[1]) else (self.devicePixelRatioF() or 1.0)
        nrect = _win_window_rect(_notify) if _notify else None
        if nrect:
            # Can't be dragged past the far end of the taskbar.
            room = (nrect[0] - trect[0]) - round(w * dpr)
            self._mini_offset = max(0, min(self._mini_offset, room))
        _win_embed_tray(int(self.winId()), True,
                        size=(round(w * dpr), round(h * dpr)),
                        offset=self._mini_offset)
        self._bg_ticks = (getattr(self, "_bg_ticks", 0) + 1) % 16
        if self._bg_ticks == 1 or self._mini_bg is None:
            img, col = _win_taskbar_backdrop(_win_window_rect(int(self.winId())))
            if col is not None:
                self._mini_bg, self._mini_backdrop = col, img

    def _init_window(self):
        # FramelessWindowHint + StaysOnTop keeps the floating overlay look.
        # We intentionally do NOT use Qt.WindowType.Tool here: a Tool window is
        # hidden from the Windows taskbar/Alt-Tab. Using a normal Window gives us
        # a taskbar entry that carries the app icon. WindowTitle drives the label.
        # (Mini mode later adds Tool so the strip itself is the taskbar UI.)
        self.setWindowTitle("Token Maxxing")
        self.setWindowIcon(_app_icon())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint  |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Window
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        fw, fh = self._full_size()
        # Lock the size: a frameless/translucent window left merely resizable
        # "expands" when dragged onto a monitor with a different DPI scale —
        # Qt re-derives its physical size from the new scale factor. A fixed
        # size keeps the overlay the same logical size across all screens.
        self.setFixedSize(fw, fh)
        self.show()
        _enable_acrylic(int(self.winId()))
        _win_pin_overlay(int(self.winId()), gadget=False)
        # Re-assert size + acrylic whenever the window crosses to another
        # screen. Per-monitor DPI changes otherwise leave stale geometry and
        # drop the acrylic backdrop, which looked like the widget "breaking".
        wh = self.windowHandle()
        if wh is not None:
            wh.screenChanged.connect(self._on_screen_changed)
        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(self._on_app_state_changed)
        if sys.platform == "win32":
            QTimer.singleShot(0, self._win_apply_native_icon)

    def _on_app_state_changed(self, _state):
        # Clicking a taskbar app can deactivate us. Re-dock immediately.
        if self._compact and not self._exiting:
            QTimer.singleShot(0, self._dock_mini)

    def changeEvent(self, event):
        if (event.type() == QEvent.Type.WindowStateChange
                and self._compact
                and not self._exiting
                and self.windowState() & Qt.WindowState.WindowMinimized):
            QTimer.singleShot(0, self._dock_mini)
        super().changeEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._compact and not self._exiting and not self._applying_chrome:
            QTimer.singleShot(0, self._dock_mini)

    def closeEvent(self, event):
        self._exiting = True
        _wait_for_token_writes()
        try:
            _win_embed_tray(int(self.winId()), False, teardown=True)
        except Exception:
            pass
        event.accept()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _request_exit(self):
        self._exiting = True
        _wait_for_token_writes()
        try:
            _win_embed_tray(int(self.winId()), False, teardown=True)
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_screen_changed(self, _screen):
        if self._compact:
            self._dock_mini()
        else:
            self._apply_full_size()
            _enable_acrylic(int(self.winId()))
        _win_pin_overlay(int(self.winId()), gadget=self._compact)
        self.update()

    def _win_apply_native_icon(self):
        _win_set_hwnd_icons(int(self.winId()))
        _win_pin_overlay(int(self.winId()), gadget=self._compact)

    # ── UI layout ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget(self)
        self._stack.setStyleSheet("background: transparent;")
        root.addWidget(self._stack)
        self._stack.currentChanged.connect(lambda _i: self._apply_full_size())

        self._update_banner = UpdateBanner(self)
        self._update_banner.hide()
        self._update_banner.open_clicked.connect(self._open_update)
        self._update_banner.dismiss_clicked.connect(self._dismiss_update)
        root.addWidget(self._update_banner)

        self._build_stats_page()
        self._build_auth_page()
        self._build_settings_page()
        self._build_tutorial_page()

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

        self._canvas = DashboardCanvas(self._frame, scale=self._scale)
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

        self._lbl_auth_sub = asub = QLabel(AUTH_SUB_DEFAULT)
        asub.setStyleSheet("color: #808098; font-size: 10px; background: transparent;")
        asub.setWordWrap(True)
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

        self._btn_auth_cancel = QPushButton("Cancel")
        self._btn_auth_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_auth_cancel.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.08); color: #C8C8D8;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px; font-size: 11px; font-weight: 600; padding: 6px 12px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.14); }
        """)
        self._btn_auth_cancel.clicked.connect(self._cancel_auth)
        alay.addWidget(self._btn_auth_cancel)

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

        # ── Claude accounts: pick one to edit, add more, remove ─────────────
        acclbl = QLabel("CLAUDE ACCOUNTS")
        acclbl.setStyleSheet(sub_style + " font-weight: 700;")
        slay.addWidget(acclbl)

        self._sett_account_box = QWidget()
        self._sett_account_box.setStyleSheet("background: transparent;")
        abox = QVBoxLayout(self._sett_account_box)
        abox.setContentsMargins(0, 0, 0, 0)
        abox.setSpacing(4)

        self._sett_pick = QComboBox()
        self._sett_pick.setStyleSheet("""
            QComboBox {
                background: rgba(255,255,255,0.07);
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 6px; color: #EBEBF0;
                font-size: 11px; padding: 4px 8px;
            }
            QComboBox QAbstractItemView {
                background: #14141C; color: #EBEBF0;
                selection-background-color: rgba(255,255,255,0.12);
            }
        """)
        self._sett_pick.currentIndexChanged.connect(self._on_settings_pick)
        abox.addWidget(self._sett_pick)

        self._sett_name = QLineEdit()
        self._sett_name.setPlaceholderText("Display name (e.g. Personal, Work)")
        self._sett_name.setStyleSheet(self._FIELD_STYLE)
        abox.addWidget(self._sett_name)

        self._sett_auto = QCheckBox("Auto-start 5h session when idle")
        self._sett_auto.setStyleSheet(chk_style)
        self._sett_auto.setToolTip(
            "When SESSION is idle, send one minimal Haiku message (~22 tokens) "
            "to anchor a new 5h block — same as the desk gadget.")
        abox.addWidget(self._sett_auto)
        slay.addWidget(self._sett_account_box)

        acct_btns = QHBoxLayout()
        acct_btns.setSpacing(6)
        self._btn_add_acct = QPushButton("+  Add account")
        self._btn_add_acct.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_acct.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.08); color: #C8C8D8;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px; font-size: 10px; font-weight: 600; padding: 5px 8px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.14); }
        """)
        self._btn_add_acct.clicked.connect(lambda: self._show_auth(add=True))
        self._btn_rm_acct = QPushButton("Remove")
        self._btn_rm_acct.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_rm_acct.setStyleSheet("""
            QPushButton {
                background: rgba(255,255,255,0.08); color: #C8C8D8;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px; font-size: 10px; font-weight: 600; padding: 5px 8px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.14); }
        """)
        self._btn_rm_acct.clicked.connect(self._on_remove_account)
        acct_btns.addWidget(self._btn_add_acct)
        acct_btns.addWidget(self._btn_rm_acct)
        slay.addLayout(acct_btns)

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

        self._sett_cursor_name = QLineEdit()
        self._sett_cursor_name.setPlaceholderText("Cursor display name")
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
        self._sett_pending: dict[str, dict] = {}

    # Page 3 — first-run tutorial ─────────────────────────────────────────────

    _TUTORIAL_STEPS = (
        ("Fade",
         "Scroll the wheel on the widget to fade it. Try it now.",
         "opacity"),
        ("Size",
         "Hold Ctrl and scroll to resize. Try it now.",
         "size"),
        ("The numbers",
         "SESSION is your 5h block, WEEKLY is 7 days; Max adds a per-model "
         "row. Every account is listed together. Drag moves the widget.",
         None),
        ("Menu and clock",
         "Right-click to add accounts, refresh, or open Settings. Minimize to clock "
         "docks it in the taskbar; drag it to any free spot. Double-click to expand.",
         None),
    )

    def _build_tutorial_page(self):
        self._tut_frame = QWidget()
        self._tut_frame.setStyleSheet("background: transparent;")
        self._stack.addWidget(self._tut_frame)

        tlay = QVBoxLayout(self._tut_frame)
        tlay.setContentsMargins(14, 14, 14, 12)
        tlay.setSpacing(8)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 2)
        hdr.setSpacing(8)
        self._tut_title = QLabel("Fade")
        self._tut_title.setStyleSheet(
            "color: #EBEBF0; font-size: 12px; font-weight: 700; "
            "background: transparent;")
        hdr.addWidget(self._tut_title, 1)
        self._tut_dots = QLabel("")
        self._tut_dots.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._tut_dots.setStyleSheet(
            "color: #00A8F8; font-size: 9px; background: transparent;")
        hdr.addWidget(self._tut_dots)
        tlay.addLayout(hdr)

        tlay.addWidget(self._divider(2))

        self._tut_body = QLabel("")
        self._tut_body.setWordWrap(True)
        self._tut_body.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._tut_body.setStyleSheet(
            "color: #C8C8D8; font-size: 11px; background: transparent; "
            "padding: 4px 0 2px 0;")
        tlay.addWidget(self._tut_body)

        self._tut_hero = QLabel("")
        self._tut_hero.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tut_hero.setStyleSheet(
            "color: #00A8F8; font-size: 32px; font-weight: 800; "
            "background: transparent; padding: 8px 0;")
        tlay.addWidget(self._tut_hero, 1)

        self._tut_live = QLabel("")
        self._tut_live.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._tut_live.setStyleSheet(
            "color: #808098; font-size: 10px; font-weight: 600; "
            "background: transparent; padding: 0 0 8px 0;")
        tlay.addWidget(self._tut_live)

        nav = QHBoxLayout()
        nav.setContentsMargins(0, 4, 0, 0)
        nav.setSpacing(8)
        skip_style = """
            QPushButton {
                background: rgba(255,255,255,0.08); color: #C8C8D8;
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 7px; font-size: 11px; font-weight: 600;
                padding: 8px 10px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.14); }
            QPushButton:disabled { color: #555568; }
        """
        next_style = """
            QPushButton {
                background: rgba(40,188,80,0.85); color: #0A0A10;
                border: none; border-radius: 7px;
                font-size: 11px; font-weight: 700; padding: 8px 10px;
            }
            QPushButton:hover  { background: rgba(40,188,80,1.0); }
            QPushButton:pressed { background: rgba(30,150,60,1.0); }
        """
        self._btn_tut_skip = QPushButton("Skip")
        self._btn_tut_skip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_tut_skip.setStyleSheet(skip_style)
        self._btn_tut_skip.clicked.connect(self._finish_tutorial)
        self._btn_tut_back = QPushButton("Back")
        self._btn_tut_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_tut_back.setStyleSheet(skip_style)
        self._btn_tut_back.clicked.connect(self._tutorial_back)
        self._btn_tut_next = QPushButton("Next")
        self._btn_tut_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_tut_next.setStyleSheet(next_style)
        self._btn_tut_next.clicked.connect(self._tutorial_next)
        nav.addWidget(self._btn_tut_skip)
        nav.addWidget(self._btn_tut_back)
        nav.addWidget(self._btn_tut_next)
        tlay.addLayout(nav)

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
        _restore_rate_limit()
        cached = _load_usage_cache()
        accts = cached.get("accounts") or []
        if accts:
            self._accounts = accts
            age = max(0.0, time.time() - float(cached.get("fetched_at") or 0))
            self._canvas.mark_refreshed(age)
            self._refresh_display()
        else:
            self._canvas.set_boot("Fetching usage...")
        self._start_polling()
        if not load_app_config()["tutorial_done"]:
            QTimer.singleShot(0, self._show_tutorial)
        elif load_app_config()["compact_mode"] and _cred_files():
            QTimer.singleShot(0, lambda: self._enter_mini(persist=False))

    def _show_tutorial(self):
        if self._compact:
            self._enter_full(persist=False)
        self._tut_step = 0
        self._apply_tutorial_step()
        self._stack.setCurrentIndex(3)

    def _apply_tutorial_step(self):
        steps = self._TUTORIAL_STEPS
        n = len(steps)
        i = max(0, min(self._tut_step, n - 1))
        self._tut_step = i
        title, body, _live = steps[i]
        self._tut_title.setText(title)
        self._tut_body.setText(body)
        dots = "  ".join("●" if k == i else "○" for k in range(n))
        self._tut_dots.setText(dots)
        last = i == n - 1
        self._btn_tut_back.setEnabled(i > 0)
        self._btn_tut_next.setText("Got it" if last else "Next")
        self._refresh_tutorial_readout()

    def _refresh_tutorial_readout(self):
        if not hasattr(self, "_tut_hero"):
            return
        if self._stack.currentIndex() != 3:
            return
        live = self._TUTORIAL_STEPS[self._tut_step][2]
        if live == "opacity":
            pct = int(round(self.windowOpacity() * 100))
            self._tut_hero.setText(f"{pct}%")
            self._tut_live.setText("scroll to fade")
            self._tut_hero.show()
            self._tut_live.show()
            self._tut_body.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            lay = self._tut_frame.layout()
            lay.setStretch(2, 0)
            lay.setStretch(3, 1)
        elif live == "size":
            pct = int(round(self._scale * 100))
            self._tut_hero.setText(f"{pct}%")
            self._tut_live.setText("Ctrl + scroll to resize")
            self._tut_hero.show()
            self._tut_live.show()
            self._tut_body.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            lay = self._tut_frame.layout()
            lay.setStretch(2, 0)
            lay.setStretch(3, 1)
        else:
            self._tut_hero.hide()
            self._tut_live.hide()
            self._tut_body.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            lay = self._tut_frame.layout()
            lay.setStretch(2, 1)
            lay.setStretch(3, 0)

    def _tutorial_back(self):
        self._tut_step = max(0, self._tut_step - 1)
        self._apply_tutorial_step()

    def _tutorial_next(self):
        if self._tut_step >= len(self._TUTORIAL_STEPS) - 1:
            self._finish_tutorial()
            return
        self._tut_step += 1
        self._apply_tutorial_step()

    def _finish_tutorial(self):
        save_app_config(tutorial_done=True)
        self._stack.setCurrentIndex(0)
        self._refresh_display()
        if load_app_config()["compact_mode"]:
            self._enter_mini(persist=False)

    def _cancel_auth(self):
        self._adding_account = False
        if _cred_files() or load_app_config()["show_cursor"]:
            self._show_main()

    def _show_auth(self, add: bool = False):
        self._adding_account = add
        self._btn_auth_cancel.setVisible(
            bool(_cred_files()) or load_app_config()["show_cursor"])
        self._lbl_auth_sub.setText(AUTH_SUB_ADD if add else AUTH_SUB_DEFAULT)
        self._enter_full(persist=False)
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
        if load_app_config()["compact_mode"]:
            self._enter_mini(persist=False)

    def _show_settings(self):
        # Per-account fields apply to the Claude account picked in the combo;
        # the Sources section is global and always shown.
        self._enter_full(persist=False)
        current = None
        if self._accounts and self._acct_idx < len(self._accounts):
            current = self._accounts[self._acct_idx].get("path")
        self._sett_pending: dict[str, dict] = {}
        self._settings_path = None
        self._populate_account_pick(current)
        app = load_app_config()
        self._sett_show_claude.setChecked(app["show_claude"])
        self._sett_show_cursor.setChecked(app["show_cursor"])
        self._sett_cursor_name.setText(app["cursor_name"])
        self._stack.setCurrentIndex(2)

    def _populate_account_pick(self, select_path: str | None) -> None:
        files = _cred_files()
        self._sett_pick.blockSignals(True)
        self._sett_pick.clear()
        for idx, path in enumerate(files):
            try:
                plan = ((_load_doc(path).get("plan") or {}).get("badge") or "")
            except Exception:
                plan = ""
            label = _account_label(path, idx) + (f"  ·  {plan}" if plan else "")
            self._sett_pick.addItem(label, path)
        self._sett_pick.blockSignals(False)
        has = bool(files)
        self._sett_account_box.setVisible(has)
        self._btn_rm_acct.setVisible(has)
        self._btn_rm_acct.setText("Remove")
        if has:
            i = files.index(select_path) if select_path in files else 0
            self._sett_pick.setCurrentIndex(i)
            self._on_settings_pick(i)

    def _stash_account_fields(self) -> None:
        path = self._settings_path
        if path:
            cfg = _account_settings(path)
            name = self._sett_name.text().strip()
            auto = self._sett_auto.isChecked()
            if name == cfg["name"] and auto == cfg["auto_start"]:
                self._sett_pending.pop(path, None)      # untouched: save nothing
                return
            self._sett_pending[self._settings_path] = {
                "name": self._sett_name.text().strip(),
                "auto_start": self._sett_auto.isChecked(),
            }

    def _on_settings_pick(self, _i: int) -> None:
        self._stash_account_fields()
        path = self._sett_pick.currentData()
        self._settings_path = path
        self._btn_rm_acct.setText("Remove")
        if not path:
            return
        cfg = _account_settings(path)
        pending = self._sett_pending.get(path)
        files = _cred_files()
        self._sett_name.setPlaceholderText(
            _account_label(path, files.index(path)) if path in files
            else "Display name")
        self._sett_name.setText(pending["name"] if pending else cfg["name"])
        self._sett_auto.setChecked(pending["auto_start"] if pending
                                   else cfg["auto_start"])

    def _on_remove_account(self) -> None:
        """Two-click remove: first click arms, second deletes the credential file."""
        path = self._settings_path
        if not path:
            return
        if self._btn_rm_acct.text() != "Confirm remove":
            self._btn_rm_acct.setText("Confirm remove")
            return
        try:
            if not _lock.acquire(timeout=45):
                raise OSError("busy refreshing — try again")
            try:
                os.remove(path)
            finally:
                _lock.release()
        except OSError:
            self._btn_rm_acct.setText("Couldn't remove — retry")
            return
        _pending_oauth.pop(path, None)
        self._sett_pending.pop(path, None)
        self._settings_path = None
        self._accounts = [a for a in self._accounts if a.get("path") != path]
        self._acct_idx = 0
        _save_usage_cache(accounts=self._accounts)
        if not _cred_files():
            self._show_auth()
            return
        self._populate_account_pick(None)
        self._refresh_display()

    def _save_settings(self):
        self._stash_account_fields()
        files = _cred_files()
        edits = [(p, e) for p, e in self._sett_pending.items() if p in files]
        for path, edit in edits:
            for a in self._accounts:
                if a.get("path") == path:
                    a["label"] = edit["name"] or _account_label(path, files.index(path))
        if edits:
            # Write off the GUI thread: _update_cred_doc waits for _lock, which a
            # token refresh can hold for a while.
            def write(edits=edits):
                for path, edit in edits:
                    _save_account_settings(path, name=edit["name"],
                                           auto_start=edit["auto_start"])
            threading.Thread(target=write, name="save-settings").start()

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
        if show_cursor and not prev["show_cursor"]:
            self._fetch_cursor_only()
        if load_app_config()["compact_mode"]:
            self._enter_mini(persist=False)

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

        t = AuthThread(self, code, self._pkce_verifier,
                       add=getattr(self, "_adding_account", False))
        t.done.connect(self._on_auth_done)
        t.finished.connect(t.deleteLater)
        t.start()

    def _on_auth_done(self, ok: bool, error: str):
        self._btn_submit.setEnabled(True)
        self._btn_submit.setText("Submit")
        self._btn_browser.setEnabled(True)
        if ok:
            self._adding_account = False
            self._show_main()
            self._trigger_fetch(force=True)
        else:
            self._lbl_auth_err.setText(f"Auth failed: {error}")
            self._lbl_auth_err.show()

    # ── Polling ───────────────────────────────────────────────────────────────

    def _start_polling(self):
        _restore_rate_limit()
        if not hasattr(self, "_poll_timer"):
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._trigger_fetch)
        self._poll_timer.start(POLL_MS)
        self._trigger_fetch()

    def _start_update_checks(self) -> None:
        if not hasattr(self, "_update_timer"):
            self._update_timer = QTimer(self)
            self._update_timer.timeout.connect(self._check_for_update)
        self._update_timer.start(UPDATE_CHECK_MS)
        QTimer.singleShot(2500, self._check_for_update)

    def _check_for_update(self) -> None:
        if self._update_checking:
            return
        self._update_checking = True
        thread = UpdateCheckThread(self)
        thread.done.connect(self._on_update_check)
        thread.done.connect(lambda: setattr(self, "_update_checking", False))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_update_check(self, info) -> None:
        cur = _parse_version(APP_VERSION)
        if not info or not cur or info.get("tuple", (0, 0)) <= cur:
            return
        dismissed = load_app_config().get("dismissed_update") or ""
        if dismissed == info["version"]:
            return
        self._update_info = info
        self._update_banner.set_version(info["version"])
        if not self._compact:
            self._update_banner.show()
            self._apply_full_size()
        self.update()

    def _open_update(self) -> None:
        info = self._update_info
        if not info:
            return
        webbrowser.open(info["url"])

    def _dismiss_update(self) -> None:
        info = self._update_info
        if info:
            save_app_config(dismissed_update=info["version"])
        self._update_info = None
        self._update_banner.hide()
        self._apply_full_size()
        self.update()

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
        self._trigger_fetch(force=True)

    def _trigger_fetch(self, force: bool = False):
        if self._fetching:
            return
        _restore_rate_limit()
        remaining = _rate_limited_remaining()
        if remaining > 0:
            # Honor Retry-After. Don't flash the banner over cached numbers
            # on every launch — only when the user asked or we have nothing.
            if force or not self._accounts:
                self._refresh_notice = f"Rate limited {int(remaining)}s"
                self._refresh_notice_until = time.monotonic() + min(3.0, remaining)
                self._refresh_display()
            return
        if not force and _usage_cache_fresh():
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
        # An account removed while this fetch was in flight must not come back.
        accounts = [a for a in accounts
                    if not a.get("path") or os.path.exists(a["path"])]
        if _is_rate_limit_payload(accounts) and self._accounts:
            # Keep the last good snapshot instead of painting "Rate limited"
            # over numbers we already have.
            pass
        else:
            self._accounts = accounts
            if any(a.get("ok") for a in accounts):
                _save_usage_cache(accounts=accounts, fetched_at=time.time())
        if self._accounts:
            self._acct_idx = min(prev, len(self._accounts) - 1)
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

    def _account_to_dash(self, a: dict, idx: int, cnt: int) -> dict:
        acct = a["label"]
        plan = a.get("plan") or ""
        # Header chars left once the plan badge is drawn (see _draw_dashboard).
        budget = _header_chars(plan) if a["ok"] else 23
        if not a["ok"]:
            acct = (a.get("error") or acct)[:budget]
        elif cnt > 1:
            suffix = f" {idx + 1}/{cnt}"
            acct = (acct[: max(0, budget - len(suffix))] + suffix)[:budget]
        else:
            acct = acct[:budget]
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
            "plan":    plan,
            "extra":   a.get("extra") or [],
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
            if self._compact:
                self.update()
            return

        cnt = len(self._accounts)
        self._acct_idx = min(max(0, self._acct_idx), cnt - 1)
        a   = self._accounts[self._acct_idx]
        notice = (self._refresh_notice
                  if self._refresh_notice
                  and time.monotonic() < self._refresh_notice_until else "")
        if cnt > 1:
            sections = [_stack_section(x) for x in self._accounts]
            dash = {"sections": sections, "notice": notice}
            height = _stack_height(sections)
        else:
            dash = self._account_to_dash(a, self._acct_idx, cnt)
            if notice:
                dash["account"] = notice[:23]
                dash["ok"] = True
            height = 128 + EXTRA_ROW_H * len(dash["extra"])
        if self._canvas.set_logical_height(height):
            self._apply_full_size()
        self._canvas.set_dashboard(dash)
        if self._compact:
            self.update()

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if self._compact:
            if self._mini_backdrop is not None:
                # Stretch the taskbar slice across the strip = "see-through".
                p.drawImage(QRectF(self.rect()), self._mini_backdrop)
            else:
                p.fillRect(self.rect(), self._mini_bg or C_BG_MINI)
            _draw_mini(p, _mini_groups(self._accounts), time.time() * 4,
                       self.width(), self.height(), bool(self._update_info),
                       self._mini_bg)
            return

        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 13
        body = QPainterPath()
        body.addRoundedRect(r, radius, radius)

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
        if e.button() == Qt.MouseButton.LeftButton and not self._compact:
            gp = e.globalPosition().toPoint()
            self._drag_pos   = gp - self.frameGeometry().topLeft()
            self._drag_start = gp
        elif e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.globalPosition().toPoint()
            self._drag_pos = None
            self._mini_drag = (e.globalPosition().x(), self._mini_offset)
        e.accept()

    def mouseMoveEvent(self, e):
        if (not self._compact and self._drag_pos is not None
                and e.buttons() == Qt.MouseButton.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_pos)
        elif (self._compact and self._mini_drag is not None
                and e.buttons() == Qt.MouseButton.LeftButton):
            # Slide along the taskbar: left = further from the clock.
            x0, off0 = self._mini_drag
            dx = (e.globalPosition().x() - x0) * (self.devicePixelRatioF() or 1.0)
            if abs(dx) >= 3:
                self._mini_offset = max(0, int(round(off0 - dx)))
                self._dock_mini()
        e.accept()

    def mouseReleaseEvent(self, e):
        if self._mini_drag is not None and self._mini_offset != self._mini_drag[1]:
            save_app_config(mini_offset=self._mini_offset)
            self._bg_ticks = 0              # re-sample the backdrop at the new spot
        self._mini_drag = None
        self._drag_pos   = None
        self._drag_start = None
        e.accept()

    def wheelEvent(self, e):
        delta = e.angleDelta().y() / 120
        if (not self._compact
                and e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._set_scale(self._scale + delta * SCALE_STEP)
            e.accept()
            return
        op = max(0.15, min(1.0, self.windowOpacity() + delta * 0.05))
        self.setWindowOpacity(op)
        e.accept()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._compact:
                self._enter_full(persist=True)
            else:
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

        on_stats = self._compact or self._stack.currentIndex() == 0
        if self._update_info:
            ver = self._update_info["version"]
            a_upd = QAction(f"↓  Get v{ver}", self)
            a_upd.triggered.connect(self._open_update)
            menu.addAction(a_upd)
            menu.addSeparator()
        if on_stats:
            # Stats page actions
            if self._compact:
                a_expand = QAction("□  Expand", self)
                a_expand.triggered.connect(lambda: self._enter_full(persist=True))
                menu.addAction(a_expand)
            else:
                a_mini = QAction("–  Minimize to clock", self)
                a_mini.triggered.connect(lambda: self._enter_mini(persist=True))
                menu.addAction(a_mini)
            menu.addSeparator()

            wait = self._manual_refresh_wait()
            label = "⟳  Refresh now" if wait == 0 else f"⟳  Refresh now ({wait}s)"
            a_refresh = QAction(label, self)
            a_refresh.setEnabled(wait == 0 and not self._fetching)
            a_refresh.triggered.connect(self._manual_refresh)
            menu.addAction(a_refresh)

            a_sett = QAction("\u2699  Settings", self)
            a_sett.triggered.connect(self._show_settings)
            menu.addAction(a_sett)

            a_tut = QAction("?  Show tutorial", self)
            a_tut.triggered.connect(self._show_tutorial)
            menu.addAction(a_tut)

            menu.addSeparator()

            a_add = QAction("+  Add account", self)
            a_add.triggered.connect(lambda: self._show_auth(add=True))
            menu.addAction(a_add)

            a_reauth = QAction("↩  Re-auth (sign in again)", self)
            a_reauth.triggered.connect(lambda: self._show_auth(add=False))
            menu.addAction(a_reauth)

        else:
            # Auth / tutorial / settings — offer to go back if credentials exist
            if self._stack.currentIndex() == 3:
                a_skip = QAction("Skip tutorial", self)
                a_skip.triggered.connect(self._finish_tutorial)
                menu.addAction(a_skip)
                menu.addSeparator()
            elif _cred_files():
                a_back = QAction("← Back to stats", self)
                a_back.triggered.connect(self._show_main)
                menu.addAction(a_back)
                a_tut = QAction("?  Show tutorial", self)
                a_tut.triggered.connect(self._show_tutorial)
                menu.addAction(a_tut)
                menu.addSeparator()

        opacity_label = QAction(f"Opacity: {int(self.windowOpacity() * 100)}%", self)
        opacity_label.setEnabled(False)
        menu.addAction(opacity_label)

        for label, val in [("  100%", 1.00), ("   80%", 0.80),
                            ("   60%", 0.60), ("   40%", 0.40)]:
            a = QAction(label, self)
            a.triggered.connect(lambda _, v=val: self.setWindowOpacity(v))
            menu.addAction(a)

        if not self._compact:
            menu.addSeparator()
            size_label = QAction(f"Size: {int(round(self._scale * 100))}%", self)
            size_label.setEnabled(False)
            menu.addAction(size_label)
            for label, val in [("  100%", 1.00), ("  150%", 1.50),
                                ("  200%", 2.00), ("  250%", 2.50),
                                ("  300%", 3.00)]:
                a = QAction(label, self)
                a.setCheckable(True)
                a.setChecked(abs(self._scale - val) < 0.01)
                a.triggered.connect(lambda _, v=val: self._set_scale(v))
                menu.addAction(a)

        menu.addSeparator()

        a_quit = QAction("✕  Exit", self)
        a_quit.triggered.connect(self._request_exit)
        menu.addAction(a_quit)

        menu.exec(e.globalPos())


# ── Entry point ───────────────────────────────────────────────────────────────

_instance_mutex = None


def _single_instance() -> bool:
    """False if another Token Maxxing is already running (two copies would
    both refresh the same rotating token and log each other out)."""
    global _instance_mutex
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                          ctypes.c_wchar_p]
        _instance_mutex = kernel32.CreateMutexW(None, False,
                                                "Local\\TokenMaxxingOverlay")
        return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _install_excepthook() -> None:
    """Log uncaught errors instead of letting PyQt6 abort the whole app."""
    import traceback
    log_path = os.path.join(CRED_DIR, "overlay_errors.log")

    def hook(etype, value, tb):
        try:
            os.makedirs(CRED_DIR, exist_ok=True)
            if os.path.exists(log_path) and os.path.getsize(log_path) > 256_000:
                os.replace(log_path, log_path + ".old")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"--- {datetime.now().isoformat(timespec='seconds')} "
                        f"v{APP_VERSION}\n")
                traceback.print_exception(etype, value, tb, file=f)
        except Exception:
            pass
    sys.excepthook = hook


def main():
    if not _single_instance():
        sys.exit(0)
    _install_excepthook()
    _win_set_app_user_model_id()

    # Pass fractional DPI scale factors through unrounded. The default policy
    # rounds (e.g. 150% → snaps), which on a multi-monitor setup with mixed
    # scaling made the overlay jump size when dragged between screens.
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass

    if hasattr(Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling)

    app = QApplication(sys.argv)
    # False: if Windows hides the mini Tool strip (taskbar click / close),
    # Qt must not treat that as "last window closed" and quit the process.
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Token Maxxing")

    icon = _app_icon()
    app.setWindowIcon(icon)

    font = QFont("Segoe UI", 10) if sys.platform == "win32" else QFont("SF Pro Display", 10)
    app.setFont(font)

    w = OverlayWindow()
    w.setWindowOpacity(0.92)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
