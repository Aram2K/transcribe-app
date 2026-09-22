"""Windows window effects for the Live Assist overlay: capture exclusion,
backdrop blur ("liquid glass"), rounded corners, click-through.

Every function is a safe no-op off Windows or on failure, and reports what it
did so the caller can fall back to painted effects. Verified on this project's
Windows 11 build (26200) with a frameless WA_TranslucentBackground PySide6
window: SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) removed the window
from a screen BitBlt while it stayed visible to the user, and both the DWM
system backdrop and the SetWindowCompositionAttribute acrylic path returned
success - including with exclusion active at the same time.

Positioning note for anyone touching this: exclusion is a PRIVACY feature -
the user's private notes never appear on a screen they share or record. It
does not hide anything from the people in the room, and the app must never
market it otherwise.
"""
import ctypes
import logging
import sys

logger = logging.getLogger("transcribe")

IS_WINDOWS = sys.platform == "win32"

# SetWindowDisplayAffinity
WDA_NONE = 0x00
WDA_EXCLUDEFROMCAPTURE = 0x11          # Windows 10 2004+ (build 19041)

# DwmSetWindowAttribute
DWMWA_WINDOW_CORNER_PREFERENCE = 33    # Windows 11
DWMWA_SYSTEMBACKDROP_TYPE = 38         # Windows 11 22H2+ (build 22621)
DWMWCP_ROUND = 2
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2                  # Mica
DWMSBT_TRANSIENTWINDOW = 3             # Acrylic
DWMSBT_TABBEDWINDOW = 4                # Mica Alt

# SetWindowCompositionAttribute (undocumented but stable since Win10 1803)
_WCA_ACCENT_POLICY = 19
_ACCENT_DISABLED = 0
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000


_bound = False


def _bind():
    """Declare ctypes signatures once. Without argtypes, ctypes passes a Python
    int HWND as a 32-bit C int - fine in practice (handles stay 32-bit safe)
    but wrong in principle - and reads HRESULTs as unsigned. Also picks the
    *LongPtr* ex-style APIs on 64-bit."""
    global _bound
    if _bound or not IS_WINDOWS:
        return
    u, d = ctypes.windll.user32, ctypes.windll.dwmapi
    u.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.SetWindowDisplayAffinity.restype = ctypes.c_int
    u.GetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    u.GetWindowDisplayAffinity.restype = ctypes.c_int
    u.GetSystemMetrics.argtypes = [ctypes.c_int]
    u.GetSystemMetrics.restype = ctypes.c_int
    d.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_void_p, ctypes.c_uint]
    d.DwmSetWindowAttribute.restype = ctypes.c_long
    for name in ("GetWindowLongPtrW", "SetWindowLongPtrW"):
        if not hasattr(u, name):
            continue
    if hasattr(u, "GetWindowLongPtrW"):
        u.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _bound = True


def _get_exstyle(hwnd):
    u = ctypes.windll.user32
    fn = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
    return int(fn(hwnd, GWL_EXSTYLE))


def _set_exstyle(hwnd, value):
    u = ctypes.windll.user32
    fn = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
    return fn(hwnd, GWL_EXSTYLE, value)


def is_remote_session():
    """True inside Remote Desktop / VDI: there the remote stream IS a capture
    path, so an excluded window would vanish for the user themselves."""
    if not IS_WINDOWS:
        return False
    try:
        _bind()
        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))   # SM_REMOTESESSION
    except Exception:
        return False


def _hwnd(widget):
    try:
        _bind()
        return int(widget.winId())
    except Exception:
        return 0


def windows_build():
    if not IS_WINDOWS:
        return 0
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def exclude_from_capture(widget, enabled=True):
    """Hide ``widget`` from screen capture (shared screens, recordings) while
    keeping it visible on the monitor. Returns True when the OS confirmed the
    new affinity via GetWindowDisplayAffinity."""
    if not IS_WINDOWS:
        return False
    # Never request exclusion below Win10 2004: there the same flag degrades to
    # WDA_MONITOR - a BLACK RECTANGLE on the shared screen, worse than visible.
    if enabled and not capture_exclusion_supported():
        return False
    hwnd = _hwnd(widget)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        want = WDA_EXCLUDEFROMCAPTURE if enabled else WDA_NONE
        if not user32.SetWindowDisplayAffinity(hwnd, want):
            logger.warning("SetWindowDisplayAffinity failed (err %s)",
                           ctypes.get_last_error())
            return False
        got = ctypes.c_uint(0)
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
        return got.value == want
    except Exception as e:
        logger.warning("exclude_from_capture: %s", e)
        return False


def is_excluded_from_capture(widget):
    if not IS_WINDOWS:
        return False
    hwnd = _hwnd(widget)
    if not hwnd:
        return False
    try:
        got = ctypes.c_uint(0)
        ctypes.windll.user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
        return got.value == WDA_EXCLUDEFROMCAPTURE
    except Exception:
        return False


class _ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WCA_DATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                ("SizeOfData", ctypes.c_size_t)]


def _set_accent(hwnd, state, tint_abgr=0):
    accent = _ACCENT_POLICY(state, 0x20 | 0x40 | 0x80 | 0x100, tint_abgr, 0)
    data = _WCA_DATA(_WCA_ACCENT_POLICY,
                     ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),
                     ctypes.sizeof(accent))
    fn = ctypes.windll.user32.SetWindowCompositionAttribute
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WCA_DATA)]
    return bool(fn(hwnd, ctypes.byref(data)))


def apply_backdrop_blur(widget, tint_rgba=(255, 255, 255, 0x30)):
    """Real blur of whatever is behind the window. Returns the path that took:
    "acrylic" (SetWindowCompositionAttribute, renders on frameless layered
    windows on Win10 1803+/Win11), "dwm" (system backdrop, Win11 22H2+) or ""
    when neither worked - the caller then paints a translucent fallback.

    Acrylic is tried first because it demonstrably composes behind Qt's
    layered (WA_TranslucentBackground) windows; the DWM backdrop needs the
    client area cleared to transparent and is kept as the second option."""
    if not IS_WINDOWS:
        return ""
    hwnd = _hwnd(widget)
    if not hwnd:
        return ""
    r, g, b, a = tint_rgba
    abgr = (a << 24) | (b << 16) | (g << 8) | r
    try:
        if windows_build() >= 17134 and _set_accent(hwnd, _ACCENT_ENABLE_ACRYLICBLURBEHIND, abgr):
            return "acrylic"
    except Exception as e:
        logger.debug("acrylic path failed: %s", e)
    try:
        if windows_build() >= 22621:
            val = ctypes.c_int(DWMSBT_TRANSIENTWINDOW)
            hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(val), ctypes.sizeof(val))
            if hr == 0:
                return "dwm"
    except Exception as e:
        logger.debug("dwm backdrop failed: %s", e)
    return ""


def remove_backdrop_blur(widget):
    if not IS_WINDOWS:
        return
    hwnd = _hwnd(widget)
    if not hwnd:
        return
    try:
        _set_accent(hwnd, _ACCENT_DISABLED, 0)
    except Exception:
        pass
    try:
        val = ctypes.c_int(DWMSBT_NONE)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


def round_corners(widget):
    """Ask DWM for rounded corners (Windows 11). Harmless elsewhere."""
    if not IS_WINDOWS or windows_build() < 22000:
        return False
    hwnd = _hwnd(widget)
    if not hwnd:
        return False
    try:
        val = ctypes.c_int(DWMWCP_ROUND)
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(val), ctypes.sizeof(val)) == 0
    except Exception:
        return False


def set_click_through(widget, enabled=True):
    """Let mouse events pass through to whatever is underneath (for a
    'ghost' overlay that never steals a click). Returns True on success."""
    if not IS_WINDOWS:
        return False
    hwnd = _hwnd(widget)
    if not hwnd:
        return False
    try:
        ex = _get_exstyle(hwnd)
        if enabled:
            ex |= WS_EX_TRANSPARENT | WS_EX_LAYERED
        else:
            ex &= ~WS_EX_TRANSPARENT
        _set_exstyle(hwnd, ex)
        return bool(_get_exstyle(hwnd) & WS_EX_TRANSPARENT) == enabled
    except Exception as e:
        logger.warning("set_click_through: %s", e)
        return False


def capture_exclusion_supported():
    return IS_WINDOWS and windows_build() >= 19041
