"""The mouse wheel must never silently change a dropdown/spin-box value.

Scrolling a settings page with the cursor over a QComboBox used to rewrite the
setting under it (Qt's default wheel behaviour) - e.g. quietly switching the
Whisper model or spoken language. Two layers are covered here: the pure
ancestor-walk helper (always runs), and the real end-to-end filter behaviour
(runs when real Qt is importable; skipped under the suite-wide PySide6 stubs
installed by test_core, and runnable on its own).
"""
import unittest


def _real_qt():
    """True only with genuine PySide6 - the suite's stubs hand back a dummy
    class whose __module__ is the stubbing test module, not PySide6."""
    try:
        from PySide6.QtWidgets import QComboBox
        return (isinstance(QComboBox, type)
                and QComboBox.__module__.startswith("PySide6"))
    except Exception:
        return False


class TestEnclosingScrollArea(unittest.TestCase):
    """Pure ancestor walk - duck-typed, no Qt needed."""

    def test_walks_up_to_the_scroll_area(self):
        if not _real_qt():
            self.skipTest("needs real Qt for the isinstance check")
        from PySide6.QtWidgets import QScrollArea, QWidget, QComboBox
        from ui.wheelguard import _enclosing_scroll_area
        _ensure_app()
        area = QScrollArea()
        inner = QWidget(area)
        combo = QComboBox(inner)
        self.assertIs(_enclosing_scroll_area(combo), area)

    def test_returns_none_without_a_scroll_area(self):
        if not _real_qt():
            self.skipTest("needs real Qt for the isinstance check")
        from PySide6.QtWidgets import QWidget, QComboBox
        from ui.wheelguard import _enclosing_scroll_area
        _ensure_app()
        parent = QWidget()
        combo = QComboBox(parent)
        self.assertIsNone(_enclosing_scroll_area(combo))


def _ensure_app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestWheelGuardBehaviour(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QScrollArea, QWidget, QVBoxLayout, QComboBox
        import ui.wheelguard as wg

        self.app = _ensure_app()
        # Fresh guard per test so install()'s idempotence can't leak state.
        wg._guard = None
        self.guard = wg.install(self.app)

        self.area = QScrollArea()
        inner = QWidget()
        lay = QVBoxLayout(inner)
        self.combo = QComboBox(inner)
        self.combo.addItems([f"item {i}" for i in range(10)])
        self.combo.setCurrentIndex(3)
        lay.addWidget(self.combo)
        inner.setFixedHeight(3000)          # taller than the viewport -> scrollable
        self.area.setWidget(inner)
        self.area.resize(300, 200)
        self.area.show()

    def tearDown(self):
        import ui.wheelguard as wg
        self.app.removeEventFilter(self.guard)
        wg._guard = None
        self.area.close()

    def _wheel(self, widget, dy=-120):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        pos = QPointF(widget.rect().center())
        return QWheelEvent(pos, widget.mapToGlobal(pos.toPoint()).toPointF(),
                           QPoint(0, dy), QPoint(0, dy), Qt.NoButton,
                           Qt.NoModifier, Qt.NoScrollPhase, False)

    def test_wheel_does_not_change_combo_value(self):
        from PySide6.QtWidgets import QApplication
        before = self.combo.currentIndex()
        for _ in range(5):
            QApplication.sendEvent(self.combo, self._wheel(self.combo))
        self.assertEqual(self.combo.currentIndex(), before,
                         "scrolling over the dropdown changed its value")

    def test_wheel_scrolls_the_page_instead(self):
        from PySide6.QtWidgets import QApplication
        bar = self.area.verticalScrollBar()
        bar.setValue(0)
        QApplication.sendEvent(self.combo, self._wheel(self.combo))
        self.assertGreater(bar.value(), 0,
                           "the scroll gesture was swallowed instead of scrolling the page")

    def test_spinbox_value_is_guarded_too(self):
        from PySide6.QtWidgets import QApplication, QSpinBox
        spin = QSpinBox(self.area.widget())
        spin.setRange(0, 100)
        spin.setValue(10)
        QApplication.sendEvent(spin, self._wheel(spin))
        self.assertEqual(spin.value(), 10)

    def test_normal_widgets_are_untouched(self):
        # A plain scrollable widget must still receive its wheel events.
        from PySide6.QtWidgets import QApplication, QTextEdit
        edit = QTextEdit(self.area.widget())
        ev = self._wheel(edit)
        QApplication.sendEvent(edit, ev)
        self.assertFalse(self.guard.eventFilter(edit, ev),
                         "guard must not filter non-value widgets")

    def test_keyboard_still_changes_the_value(self):
        # Blocking the wheel must not block deliberate keyboard changes.
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent as _QEvent
        from PySide6.QtWidgets import QApplication
        self.combo.setFocus()
        before = self.combo.currentIndex()
        QApplication.sendEvent(self.combo, QKeyEvent(
            _QEvent.Type.KeyPress, Qt.Key_Down, Qt.NoModifier))
        self.assertEqual(self.combo.currentIndex(), before + 1)


if __name__ == "__main__":
    unittest.main()
