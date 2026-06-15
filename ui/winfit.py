"""Keep top-level windows sized and positioned inside the visible desktop.

Every dialog in the app ships with a designed size, but real desktops vary
wildly (1366x768 laptops, 125-150% DPI scaling, taskbar placement). Without
this, windows could open with the title bar above the screen edge, overshoot
the bottom, or refuse to shrink because of hard minimums. One helper, applied
in every window's showEvent, guarantees: never larger than the work area,
fully visible, centered on first open.
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

_MARGIN = 24


def _frame_overhead(win):
    """Extra width/height the OS window frame adds around the client area.

    Qt's resize()/width()/height() exclude the frame, but move()/x()/y()
    position the frame's top-left corner - so all fitting math must add the
    frame back, or a window capped to the work area still hangs its title
    bar's worth of pixels off the bottom edge of the screen."""
    try:
        fg = win.frameGeometry()
        return (max(0, fg.width() - win.width()),
                max(0, fg.height() - win.height()))
    except Exception:
        return 0, 0


def size_to_screen(win, frac_w, frac_h, min_w, min_h, max_w, max_h):
    """Give `win` a default size proportional to the work area, clamped to a
    sane [min, max] design range - so dialogs feel the same on a 13" laptop at
    150% scaling and a 32" 4K monitor, instead of one fixed pixel size that
    towers over small screens and looks lost (or oversized) on big ones."""
    scr = win.screen() or QApplication.primaryScreen()
    if scr is None:
        return
    a = scr.availableGeometry()
    fw, fh = _frame_overhead(win)
    w = int(min(max(a.width() * frac_w, min_w), max_w))
    h = int(min(max(a.height() * frac_h, min_h), max_h))
    # Never propose more than the work area itself (fit_on_screen still runs
    # after this as the final guarantee).
    w = min(w, a.width() - _MARGIN - fw)
    h = min(h, a.height() - _MARGIN - fh)
    win.resize(w, h)


def fit_on_screen(win, recenter=False):
    """Cap `win` to the available desktop, then position it fully on screen.

    - Shrinks hard minimum sizes when the display is smaller than the design
      size (also unlocks setFixedSize windows, where min == max).
    - Centers on the first show (or every show with recenter=True).
    - On later shows, only nudges the window back into view - it never fights
      a position the user chose.
    """
    scr = win.screen() or QApplication.primaryScreen()
    if scr is None:
        return
    avail = scr.availableGeometry()
    fw, fh = _frame_overhead(win)
    max_w = max(320, avail.width() - _MARGIN - fw)
    max_h = max(240, avail.height() - _MARGIN - fh)

    # Hard minimums that don't fit this display must come down first,
    # otherwise resize() silently refuses to shrink the window.
    if win.minimumWidth() > max_w or win.minimumHeight() > max_h:
        win.setMinimumSize(min(win.minimumWidth(), max_w),
                           min(win.minimumHeight(), max_h))

    w = min(win.width(), max_w)
    h = min(win.height(), max_h)
    if (w, h) != (win.width(), win.height()):
        win.resize(w, h)

    first = not getattr(win, "_fit_positioned", False)
    win._fit_positioned = True
    if first or recenter:
        win.move(avail.center().x() - (w + fw) // 2,
                 avail.center().y() - (h + fh) // 2)
    # Always clamp fully into view afterwards. On first show the frame size
    # estimate can be wrong (the OS hasn't decorated the window yet), so a
    # centered window could still hang its title bar above the top edge -
    # clamp so the top is never off-screen. On later shows this only nudges a
    # window that drifted out of view; a position the user chose is untouched.
    x = max(avail.left(), min(win.x(), avail.right() - (w + fw) + 1))
    y = max(avail.top(), min(win.y(), avail.bottom() - (h + fh) + 1))
    if (x, y) != (win.x(), win.y()):
        win.move(x, y)


def settle_on_screen(win, recenter=False):
    """Fit now, then again on the next event-loop tick.

    A window's true frame geometry (OS title bar) and final laid-out size
    aren't known until after showEvent returns and the window is mapped. The
    immediate fit positions it from estimates; the deferred fit re-clamps once
    those are real - which is what fixes a first-open window whose top rendered
    above the screen until the user closed and reopened it."""
    fit_on_screen(win, recenter=recenter)

    def _again():
        try:
            if win.isVisible():
                fit_on_screen(win)
        except RuntimeError:
            pass  # window destroyed before the tick
    QTimer.singleShot(0, _again)
