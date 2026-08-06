"""Stop the mouse wheel from silently changing dropdown / spin-box values.

Qt delivers a wheel event to whatever widget sits under the cursor, and
QComboBox, QAbstractSpinBox and QSlider all treat a wheel tick as "change my
value". Inside a scrollable settings page that means simply scrolling past a
dropdown quietly rewrites a setting - which is how the Whisper model, spoken
language or meeting audio device could change without the user ever clicking
anything.

This installs a single application-wide event filter that swallows those wheel
events and hands the scroll to the nearest scroll area instead, so the page
scrolls the way the user expects. The values are still fully changeable: click
the widget and pick an entry, or use the keyboard (arrow keys / Page Up-Down)
once it has focus. Scrolling *inside* an open dropdown list is unaffected -
that list is its own view widget, so those events never reach here.
"""
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QComboBox, QSlider,
)

# Widgets whose value must never move because the pointer happened to be over
# them while the user scrolled the page.
GUARDED_TYPES = (QComboBox, QAbstractSpinBox, QSlider)


def _enclosing_scroll_area(widget):
    """The nearest QAbstractScrollArea ancestor, or None."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


class WheelGuard(QObject):
    def eventFilter(self, obj, event):
        try:
            if event.type() != QEvent.Type.Wheel or not isinstance(obj, GUARDED_TYPES):
                return False
            # Redirect the scroll to the page so the gesture isn't just dropped.
            # The viewport isn't a guarded type, so this can't recurse.
            scroll_area = _enclosing_scroll_area(obj)
            if scroll_area is not None:
                QApplication.sendEvent(scroll_area.viewport(), event)
            return True  # never let the widget read it as a value change
        except Exception:
            # An event filter must never raise into the Qt event loop.
            return False


_guard = None


def install(app):
    """Install the wheel guard on ``app``. Idempotent; returns the guard."""
    global _guard
    if _guard is None:
        _guard = WheelGuard(app)
        app.installEventFilter(_guard)
    return _guard
