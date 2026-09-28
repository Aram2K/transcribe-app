"""Windows window effects for the Live Assistance overlay: capture exclusion,
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
import os
import sys
import threading

logger = logging.getLogger("transcribe")

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

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
# ctypes.windll.user32 is shared and drops GetLastError, so every failure used
# to log "err 0". This handle keeps the real code for ctypes.get_last_error().
_user32_le = None


def _bind():
    """Declare ctypes signatures once. Without argtypes, ctypes passes a Python
    int HWND as a 32-bit C int - fine in practice (handles stay 32-bit safe)
    but wrong in principle - and reads HRESULTs as unsigned. Also picks the
    *LongPtr* ex-style APIs on 64-bit."""
    global _bound, _user32_le
    if _bound or not IS_WINDOWS:
        return
    _user32_le = ctypes.WinDLL("user32", use_last_error=True)
    _user32_le.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32_le.SetWindowDisplayAffinity.restype = ctypes.c_int
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


# ── macOS: NSWindow.sharingType ──
# NSWindowSharingNone keeps a window's pixels out of other processes: screen
# sharing and recording apps that use the system capture APIs leave it out.
# It is the same switch Electron's setContentProtection uses. Talked to via
# the Objective-C runtime with exact prototypes (arm64 must never call
# objc_msgSend through a variadic signature). GUI thread only (AppKit).
_NS_SHARING_NONE, _NS_SHARING_READ_ONLY = 0, 1
_objc = None


def _objc_runtime():
    global _objc
    if _objc is None:
        lib = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        lib.sel_registerName.restype = ctypes.c_void_p
        lib.sel_registerName.argtypes = [ctypes.c_char_p]
        send = lambda restype, *args: ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                                                       *args)(("objc_msgSend", lib))
        _objc = {
            "sel": lib.sel_registerName,
            "id": send(ctypes.c_void_p),                    # -(id)x
            "get_uint": send(ctypes.c_ulong),               # -(NSUInteger)x
            "set_uint": send(None, ctypes.c_ulong),         # -(void)x:(NSUInteger)v
        }
    return _objc


def _ns_window(widget):
    """The NSWindow behind a Qt top level (winId() is its NSView on macOS)."""
    view = int(widget.winId())
    if not view:
        return None
    rt = _objc_runtime()
    return rt["id"](view, rt["sel"](b"window")) or None


def _mac_sharing_type(widget):
    win = _ns_window(widget)
    if not win:
        return None
    rt = _objc_runtime()
    return int(rt["get_uint"](win, rt["sel"](b"sharingType")))


def _mac_set_excluded(widget, enabled):
    win = _ns_window(widget)
    if not win:
        return False
    rt = _objc_runtime()
    want = _NS_SHARING_NONE if enabled else _NS_SHARING_READ_ONLY
    rt["set_uint"](win, rt["sel"](b"setSharingType:"), want)
    return _mac_sharing_type(widget) == want


def exclude_from_capture(widget, enabled=True):
    """Hide ``widget`` from screen capture (shared screens, recordings) while
    keeping it visible on the monitor. Returns True when the OS confirmed the
    new state: GetWindowDisplayAffinity on Windows, NSWindow.sharingType read
    back on macOS."""
    if IS_MAC:
        try:
            return _mac_set_excluded(widget, enabled)
        except Exception as e:
            logger.warning("exclude_from_capture (macOS): %s", e)
            return False
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
        if not _user32_le.SetWindowDisplayAffinity(hwnd, want):
            err = ctypes.get_last_error()   # read before anything else can reset it
            logger.warning("SetWindowDisplayAffinity failed (err %s, hwnd=%#x)", err, hwnd)
            return False
        got = ctypes.c_uint(0)
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
        return got.value == want
    except Exception as e:
        logger.warning("exclude_from_capture: %s", e)
        return False


def is_excluded_from_capture(widget):
    if IS_MAC:
        try:
            return _mac_sharing_type(widget) == _NS_SHARING_NONE
        except Exception:
            return False
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


def mac_capture_protection_reliable():
    """True through macOS 14, where NSWindow.sharingType keeps a window out of
    every capture. From macOS 15 (Darwin 24) ScreenCaptureKit - what Zoom,
    Teams, browsers and QuickTime use - ignores it, and Apple offers no other
    public way: the window can't be promised hidden there."""
    if not IS_MAC:
        return False
    try:
        return int(os.uname().release.split(".")[0]) < 24
    except Exception:
        return False


def capture_exclusion_supported():
    """Whether hiding a window from screen capture can be PROMISED here."""
    return mac_capture_protection_reliable() or (IS_WINDOWS and windows_build() >= 19041)


# Private user32/gdi32 handles for the helpers below, so their argtypes/restype
# never leak into other callers of the shared ctypes.windll.* - and set up
# under a lock, because the dictation HUD samples the screen from a worker.
_u32 = None
_gdi = None
_dll_lock = threading.Lock()
SPI_GETCLIENTAREAANIMATION = 0x1042
MONITOR_DEFAULTTONEAREST = 2
SM_SWAPBUTTON = 23
WS_EX_NOACTIVATE = 0x08000000


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", ctypes.c_long * 4),
                ("rcWork", ctypes.c_long * 4), ("dwFlags", ctypes.c_ulong),
                ("szDevice", ctypes.c_wchar * 32)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


def _private_user32():
    global _u32
    with _dll_lock:
        if _u32 is None:
            u = ctypes.WinDLL("user32")
            vp = ctypes.c_void_p
            u.GetForegroundWindow.restype = vp
            u.MonitorFromWindow.argtypes = [vp, ctypes.c_uint]
            u.MonitorFromWindow.restype = vp
            u.GetMonitorInfoW.argtypes = [vp, vp]
            u.GetMonitorInfoW.restype = ctypes.c_int
            u.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint, vp, ctypes.c_uint]
            u.SystemParametersInfoW.restype = ctypes.c_int
            u.GetDC.argtypes = [vp]
            u.GetDC.restype = vp
            u.ReleaseDC.argtypes = [vp, vp]
            u.ReleaseDC.restype = ctypes.c_int
            u.GetWindowRect.argtypes = [vp, ctypes.POINTER(ctypes.c_long * 4)]
            u.GetWindowRect.restype = ctypes.c_int
            u.GetAsyncKeyState.argtypes = [ctypes.c_int]
            u.GetAsyncKeyState.restype = ctypes.c_short
            u.GetSystemMetrics.argtypes = [ctypes.c_int]
            u.GetSystemMetrics.restype = ctypes.c_int
            _u32 = u
    return _u32


def _private_gdi32():
    global _gdi
    with _dll_lock:
        if _gdi is None:
            g = ctypes.WinDLL("gdi32")
            vp = ctypes.c_void_p
            g.CreateCompatibleDC.argtypes = [vp]
            g.CreateCompatibleDC.restype = vp
            g.CreateDIBSection.argtypes = [vp, vp, ctypes.c_uint, ctypes.POINTER(vp), vp,
                                           ctypes.c_uint32]
            g.CreateDIBSection.restype = vp
            g.SelectObject.argtypes = [vp, vp]
            g.SelectObject.restype = vp
            g.BitBlt.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 vp, ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
            g.BitBlt.restype = ctypes.c_int
            g.DeleteObject.argtypes = [vp]
            g.DeleteObject.restype = ctypes.c_int
            g.DeleteDC.argtypes = [vp]
            g.DeleteDC.restype = ctypes.c_int
            g.GdiFlush.restype = ctypes.c_int
            _gdi = g
    return _gdi


def window_rect(widget_or_hwnd):
    """(left, top, right, bottom) of a window in physical screen pixels, or
    None. Any thread."""
    if not IS_WINDOWS:
        return None
    hwnd = widget_or_hwnd if isinstance(widget_or_hwnd, int) else _hwnd(widget_or_hwnd)
    if not hwnd:
        return None
    try:
        r = (ctypes.c_long * 4)()
        if _private_user32().GetWindowRect(hwnd, ctypes.byref(r)):
            return tuple(r)
    except Exception:
        pass
    return None


def grab_screen(x, y, w, h):
    """Top-down BGRX bytes of a physical-pixel screen rect (GDI BitBlt), or
    None. Safe on a worker thread - a grab waits for the next DWM frame, so it
    must not run on the GUI thread in a loop. Like every capture API it leaves
    out windows excluded with WDA_EXCLUDEFROMCAPTURE."""
    if not IS_WINDOWS or w <= 0 or h <= 0:
        return None
    try:
        u, g = _private_user32(), _private_gdi32()
    except Exception:
        return None
    sdc = u.GetDC(None)
    if not sdc:
        return None
    mdc = bmp = old = None
    try:
        mdc = g.CreateCompatibleDC(sdc)
        info = _BITMAPINFO()
        info.bmiHeader = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32,
                                           0, 0, 0, 0, 0, 0)       # BI_RGB, top-down
        bits = ctypes.c_void_p()
        bmp = g.CreateDIBSection(sdc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not mdc or not bmp or not bits.value:
            return None
        old = g.SelectObject(mdc, bmp)
        if not g.BitBlt(mdc, 0, 0, w, h, sdc, x, y, 0x00CC0020):   # SRCCOPY
            return None
        g.GdiFlush()
        return ctypes.string_at(bits.value, w * h * 4)
    except Exception as e:
        logger.debug("grab_screen: %s", e)
        return None
    finally:
        if old:
            g.SelectObject(mdc, old)
        if bmp:
            g.DeleteObject(bmp)
        if mdc:
            g.DeleteDC(mdc)
        u.ReleaseDC(None, sdc)


def primary_button_down():
    """Whether the primary mouse button is held right now - the physical
    state, so it also catches a release that happened outside our window.
    Honours swapped buttons. False off Windows."""
    if not IS_WINDOWS:
        return False
    try:
        u = _private_user32()
        vk = 0x02 if u.GetSystemMetrics(SM_SWAPBUTTON) else 0x01    # VK_RBUTTON / VK_LBUTTON
        return bool(u.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def set_no_activate(widget, enabled=True):
    """WS_EX_NOACTIVATE: clicking or dragging the window never activates it,
    so the app the user is typing in keeps focus. Returns True on success."""
    if not IS_WINDOWS:
        return False
    hwnd = _hwnd(widget)
    if not hwnd:
        return False
    try:
        ex = _get_exstyle(hwnd)
        ex = (ex | WS_EX_NOACTIVATE) if enabled else (ex & ~WS_EX_NOACTIVATE)
        _set_exstyle(hwnd, ex)
        return bool(_get_exstyle(hwnd) & WS_EX_NOACTIVATE) == enabled
    except Exception as e:
        logger.warning("set_no_activate: %s", e)
        return False


def animations_enabled():
    """False when the user switched off Windows "Animation effects"
    (Settings > Accessibility > Visual effects): decorative motion should stop.
    True off Windows or when the setting can't be read."""
    if not IS_WINDOWS:
        return True
    try:
        val = ctypes.c_int(1)
        if _private_user32().SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0,
                                                   ctypes.byref(val), 0):
            return bool(val.value)
    except Exception:
        pass
    return True


def transparency_enabled():
    """False when the user switched off Windows "Transparency effects"
    (Settings > Personalization > Colors): see-through surfaces should turn
    solid. True off Windows or when the setting can't be read."""
    if not IS_WINDOWS:
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "EnableTransparency")[0])
    except Exception:
        return True


def foreground_monitor_name():
    """Device name (e.g. \\\\.\\DISPLAY2) of the monitor showing the foreground
    window - where the user is typing - or "" when unknown. Equals
    QScreen.name() for that monitor on Windows."""
    if not IS_WINDOWS:
        return ""
    try:
        u = _private_user32()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return ""
        mon = u.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if mon and u.GetMonitorInfoW(mon, ctypes.byref(info)):
            return info.szDevice
    except Exception:
        pass
    return ""
