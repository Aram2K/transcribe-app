"""Keep top-level windows sized and positioned inside the visible desktop.

Every dialog in the app ships with a designed size, but real desktops vary
wildly (1366x768 laptops, 125-150% DPI scaling, taskbar placement). Without
this, windows could open with the title bar above the screen edge, overshoot
the bottom, or refuse to shrink because of hard minimums. One helper, applied
in every window's showEvent, guarantees: never larger than the work area,
fully visible, centered on first open.
"""

from PySide6.QtWidgets import QApplication

_MARGIN = 24


def size_to_screen(win, frac_w, frac_h, min_w, min_h, max_w, max_h):
    """Give `win` a default size proportional to the work area, clamped to a
    sane [min, max] design range - so dialogs feel the same on a 13" laptop at
    150% scaling and a 32" 4K monitor, instead of one fixed pixel size that
    towers over small screens and looks lost (or oversized) on big ones."""
    scr = win.screen() or QApplication.primaryScreen()
    if scr is None:
        return
    a = scr.availableGeometry()
    w = int(min(max(a.width() * frac_w, min_w), max_w))
    h = int(min(max(a.height() * frac_h, min_h), max_h))
    # Never propose more than the work area itself (fit_on_screen still runs
    # after this as the final guarantee).
    w = min(w, a.width() - _MARGIN)
    h = min(h, a.height() - _MARGIN)
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
    max_w = max(320, avail.width() - _MARGIN)
    max_h = max(240, avail.height() - _MARGIN)

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
        win.move(avail.center().x() - w // 2, avail.center().y() - h // 2)
        return
    x = max(avail.left(), min(win.x(), avail.right() - w + 1))
    y = max(avail.top(), min(win.y(), avail.bottom() - h + 1))
    if (x, y) != (win.x(), win.y()):
        win.move(x, y)
