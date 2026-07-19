# PyInstaller runtime hook — runs before the main script imports PyQt.
import sys

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "sherbo.TokenMaxxing")
    except Exception:
        pass
