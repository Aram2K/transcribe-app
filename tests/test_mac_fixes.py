"""Mac fixes: the bare-key hotkey guard, screen-capture privacy through
NSWindow.sharingType (against a fake Objective-C runtime), and the Live
Assistance ask box / header. Qt widget parts are guarded like test_wheelguard."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


def _ensure_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class TestBareHotkeyGuard(unittest.TestCase):
    def test_a_bare_space_hotkey_is_reset_to_the_default(self):
        # An older capture screen could save "space": registered, it would
        # start dictation on every keystroke. It goes back to the default.
        import main
        me = SimpleNamespace(cfg={"hotkey": "space"}, save_config=MagicMock(),
                             show_tray_hint=MagicMock(), sig_hotkey=MagicMock(),
                             _unregister_kbd_hotkey=MagicMock(),
                             _unregister_mouse_listener=MagicMock(),
                             _registered_kbd_hotkey=None, _kbd_listener=None)
        kbd = MagicMock()
        with mock.patch.dict(sys.modules, {"keyboard": kbd}), \
             mock.patch.object(main.sys, "platform", "win32"), \
             mock.patch.object(main.QTimer, "singleShot", side_effect=lambda ms, fn: fn()):
            main.AppController._setup_hotkey(me, "space")
        self.assertEqual(me.cfg["hotkey"], main.DEFAULT["hotkey"])
        me.save_config.assert_called_once()
        title, text = me.show_tray_hint.call_args[0]
        self.assertEqual(title, "Hotkey reset")
        self.assertIn("would trigger while you type", text)
        self.assertEqual(kbd.add_hotkey.call_args[0][0], main.DEFAULT["hotkey"])

    def test_a_proper_hotkey_is_left_alone(self):
        import main
        me = SimpleNamespace(cfg={"hotkey": "ctrl+shift+d"}, save_config=MagicMock(),
                             show_tray_hint=MagicMock(), sig_hotkey=MagicMock(),
                             _unregister_kbd_hotkey=MagicMock(),
                             _unregister_mouse_listener=MagicMock(),
                             _registered_kbd_hotkey=None, _kbd_listener=None)
        kbd = MagicMock()
        with mock.patch.dict(sys.modules, {"keyboard": kbd}), \
             mock.patch.object(main.sys, "platform", "win32"):
            main.AppController._setup_hotkey(me, "ctrl+shift+d")
        me.save_config.assert_not_called()
        self.assertEqual(kbd.add_hotkey.call_args[0][0], "ctrl+shift+d")


class TestMacCapturePrivacy(unittest.TestCase):
    """glass.exclude_from_capture on macOS sets NSWindow.sharingType and
    reports only what it can read back."""

    def _runtime(self, sticks=True, has_window=True):
        state = {"sharing": 1}                      # NSWindowSharingReadOnly

        def set_uint(obj, sel, value):
            self.assertEqual((obj, sel), (0xABC, b"setSharingType:"))
            if sticks:
                state["sharing"] = value

        rt = {
            "sel": lambda name: name,
            "id": lambda obj, sel: (0xABC if has_window and sel == b"window" else 0),
            "get_uint": lambda obj, sel: state["sharing"],
            "set_uint": set_uint,
        }
        return rt, state

    def _patched(self, rt, darwin="23.6.0"):
        # Darwin 23 = macOS 14, 24 = macOS 15, 25 = macOS 26.
        from ui import glass
        return (mock.patch.object(glass, "IS_MAC", True),
                mock.patch.object(glass, "IS_WINDOWS", False),
                mock.patch.object(glass, "_objc_runtime", lambda: rt),
                mock.patch.object(glass.os, "uname",
                                  lambda: SimpleNamespace(release=darwin), create=True))

    def test_hides_and_shows_and_reads_it_back(self):
        from ui import glass
        rt, state = self._runtime()
        widget = SimpleNamespace(winId=lambda: 0x123)
        a, b, c, d = self._patched(rt)
        with a, b, c, d:
            self.assertTrue(glass.exclude_from_capture(widget, True))
            self.assertEqual(state["sharing"], 0)    # NSWindowSharingNone
            self.assertTrue(glass.is_excluded_from_capture(widget))
            self.assertTrue(glass.exclude_from_capture(widget, False))
            self.assertEqual(state["sharing"], 1)
            self.assertFalse(glass.is_excluded_from_capture(widget))

    def test_never_claims_private_when_macos_didnt_take_it(self):
        from ui import glass
        rt, state = self._runtime(sticks=False)
        a, b, c, d = self._patched(rt)
        with a, b, c, d:
            self.assertFalse(glass.exclude_from_capture(SimpleNamespace(winId=lambda: 0x123), True))
        rt, state = self._runtime(has_window=False)
        a, b, c, d = self._patched(rt)
        with a, b, c, d:
            self.assertFalse(glass.exclude_from_capture(SimpleNamespace(winId=lambda: 0x123), True))

    def test_no_macos_version_promises_hidden(self):
        # sharingType is best effort on every macOS (QuickTime, Zoom's default
        # capture mode show it): the flag is still set, but "Private" is never
        # claimed - the badge says "May be hidden".
        from ui import glass
        for darwin in ("23.6.0", "24.6.0", "25.1.0"):
            rt, state = self._runtime()
            a, b, c, d = self._patched(rt, darwin)
            with a, b, c, d:
                self.assertFalse(glass.capture_exclusion_supported(), darwin)
                self.assertTrue(glass.exclude_from_capture(SimpleNamespace(winId=lambda: 0x123), True))
                self.assertEqual(state["sharing"], 0)
                self.assertEqual(glass.macos_26_or_later(), darwin.startswith("25"))


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestSettingsCapture(unittest.TestCase):
    """The real Settings.keyPressEvent / event, on a minimal stand-in."""

    def setUp(self):
        _ensure_app()
        from PySide6.QtWidgets import QPushButton
        from ui.settings import Settings
        self.S = Settings
        self.me = SimpleNamespace(capturing=True, _capture_target="dictation", app=object(),
                                  cfg_working={"hotkey": "alt+r"}, btn_hotkey=QPushButton(),
                                  _fmt_hotkey=Settings._fmt_hotkey,
                                  _toggle_capture=MagicMock(), _toggle_lp_capture=MagicMock(),
                                  _apply_lp_hotkey=MagicMock())

    def _press(self, key, mods):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        self.S.keyPressEvent(self.me, QKeyEvent(QEvent.KeyPress, key, mods))

    def test_records_a_combination(self):
        from PySide6.QtCore import Qt
        with mock.patch("hotkeys.IS_MAC", False):
            self._press(Qt.Key_D, Qt.ControlModifier | Qt.ShiftModifier)
        self.assertEqual(self.me.cfg_working["hotkey"], "ctrl+shift+d")
        self.assertFalse(self.me.capturing)

    def test_esc_cancels_and_a_bare_key_is_refused(self):
        from PySide6.QtCore import Qt
        self._press(Qt.Key_Escape, Qt.NoModifier)
        self.me._toggle_capture.assert_called_once()
        with mock.patch("ui.settings.QMessageBox.warning") as warn:
            self._press(Qt.Key_Space, Qt.NoModifier)
        warn.assert_called_once()
        self.assertEqual(self.me.cfg_working["hotkey"], "alt+r")    # unchanged

    def test_shortcuts_are_held_off_only_while_capturing(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        ev = QKeyEvent(QEvent.ShortcutOverride, Qt.Key_Q, Qt.ControlModifier)
        ev.ignore()
        self.assertTrue(self.S.event(self.me, ev))                 # consumed: no ⌘Q quit
        self.assertTrue(ev.isAccepted())


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestAskBox(unittest.TestCase):
    def setUp(self):
        _ensure_app()
        from ui import live_assist
        self.la = live_assist
        self.app = SimpleNamespace(cfg={}, save_config=MagicMock(), track=MagicMock())
        self.o = live_assist.LiveAssistOverlay(main_app=self.app)

        def fake_suggest(question="", auto=False, force_screen=False, image_b64=""):
            # What suggest() does when a request really goes out.
            self.o._gen += 1
            self.o._suggesting = True
            self.o._sent_question = ""
            self.o.txt_suggestion.setPlainText("Thinking…")
        self.o.suggest = fake_suggest

    def tearDown(self):
        self.o.hide()
        self.o.deleteLater()

    def test_enter_empties_the_box_and_says_it_was_sent(self):
        self.o.input_ask.setText("What did they say about the budget?")
        self.o._ask()
        self.assertEqual(self.o.input_ask.text(), "")
        self.assertEqual(self.o.input_ask.placeholderText(), self.la.ASK_SENT)
        self.assertIn("✓ Sent: What did they say about the budget?", self.o.lbl_status.text())
        self.assertIn("You asked: What did they say", self.o.txt_suggestion.toPlainText())

    def test_the_answer_restores_the_prompt_and_keeps_new_typing(self):
        self.o.input_ask.setText("first question")
        self.o._ask()
        self.o.input_ask.setText("typing the next one")      # typed while it answered
        self.o._on_suggestion("Here's the answer.", "", self.o._gen)
        self.assertEqual(self.o.input_ask.text(), "typing the next one")
        self.assertEqual(self.o.input_ask.placeholderText(), self.la.ASK_PLACEHOLDER)

    def test_a_failed_answer_puts_the_question_back(self):
        self.o.input_ask.setText("will this fail?")
        self.o._ask()
        self.o._on_suggestion("", "network down", self.o._gen)
        self.assertEqual(self.o.input_ask.text(), "will this fail?")
        self.assertIn("network down", self.o.txt_suggestion.toPlainText())

    def test_nothing_sent_leaves_the_text(self):
        self.o.suggest = lambda *a, **k: None                 # e.g. no app context
        self.o.input_ask.setText("keep me")
        self.o._ask()
        self.assertEqual(self.o.input_ask.text(), "keep me")

    def test_title_shortens_instead_of_being_cut(self):
        from PySide6.QtWidgets import QApplication, QLabel
        lbl = self.la._ElidedLabel("Live Assistance")
        lbl.show()                          # hidden widgets get no resize events
        lbl.resize(70, 20)
        QApplication.processEvents()
        self.assertTrue(QLabel.text(lbl).endswith("…"), QLabel.text(lbl))
        self.assertEqual(lbl.text(), "Live Assistance")
        lbl.resize(2000, 20)
        QApplication.processEvents()
        self.assertEqual(QLabel.text(lbl), "Live Assistance")
        lbl.hide()


if __name__ == "__main__":
    unittest.main()
