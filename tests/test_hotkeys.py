"""hotkeys.py: capture (incl. the macOS Command/Control swap), display, the
pynput syntax, the bare-key guard, and the macOS listener-thread layout
patch. Qt-dependent parts are guarded like test_wheelguard."""
import contextlib
import sys
import threading
import types
import unittest
from unittest import mock

import hotkeys


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


class TestCombine(unittest.TestCase):
    def test_mac_undoes_qts_command_control_swap(self):
        # Qt on macOS: ControlModifier is the Command key, MetaModifier Control.
        self.assertEqual(hotkeys.combine("e", ctrl=True, mac=True), ("cmd+e", None))
        self.assertEqual(hotkeys.combine("e", meta=True, mac=True), ("ctrl+e", None))
        self.assertEqual(hotkeys.combine("r", alt=True, mac=True), ("alt+r", None))
        self.assertEqual(hotkeys.combine("space", ctrl=True, shift=True, mac=True),
                         ("shift+cmd+space", None))

    def test_windows_mapping_is_unchanged(self):
        self.assertEqual(hotkeys.combine("e", ctrl=True, mac=False), ("ctrl+e", None))
        self.assertEqual(hotkeys.combine("e", meta=True, mac=False), ("win+e", None))
        self.assertEqual(hotkeys.combine("r", alt=True, shift=True, mac=False),
                         ("alt+shift+r", None))

    def test_a_bare_typing_key_needs_a_modifier(self):
        for mac in (True, False):
            self.assertEqual(hotkeys.combine("space", mac=mac), (None, "needs_modifier"))
            self.assertEqual(hotkeys.combine("r", mac=mac), (None, "needs_modifier"))
            self.assertEqual(hotkeys.combine("f9", mac=mac), ("f9", None))


class TestDisplay(unittest.TestCase):
    def test_mac_uses_symbols_in_apples_order(self):
        self.assertEqual(hotkeys.display("alt+r", mac=True), "⌥ R")
        self.assertEqual(hotkeys.display("cmd+shift+e", mac=True), "⇧⌘ E")
        self.assertEqual(hotkeys.display("shift+cmd+ctrl+alt+k", mac=True), "⌃⌥⇧⌘ K")
        self.assertEqual(hotkeys.display("ctrl+space", mac=True), "⌃ Space")
        self.assertEqual(hotkeys.display("cmd+enter", mac=True), "⌘ Return")
        self.assertEqual(hotkeys.display("f5", mac=True), "F5")
        self.assertEqual(hotkeys.display("mouse:x1", mac=True), "Mouse Back")

    def test_windows_reads_as_before(self):
        self.assertEqual(hotkeys.display("alt+r", mac=False), "Alt + R")
        self.assertEqual(hotkeys.display("ctrl+shift+t", mac=False), "Ctrl + Shift + T")
        self.assertEqual(hotkeys.display("win+space", mac=False), "Win + Space")
        self.assertEqual(hotkeys.display("mouse:middle", mac=False), "Mouse Middle")


class TestPynputSyntax(unittest.TestCase):
    def test_named_keys_get_angle_brackets(self):
        self.assertEqual(hotkeys.to_pynput("alt+r"), "<alt>+r")
        self.assertEqual(hotkeys.to_pynput("cmd+shift+space"), "<cmd>+<shift>+<space>")
        self.assertEqual(hotkeys.to_pynput("ctrl+page up"), "<ctrl>+<page_up>")
        self.assertEqual(hotkeys.to_pynput("f9"), "<f9>")
        self.assertEqual(hotkeys.to_pynput("win+e"), "<cmd>+e")

    def test_pynput_accepts_everything_we_can_capture(self):
        # "space" without brackets made pynput raise, so the hotkey silently
        # never registered - check every capturable name round-trips.
        from pynput.keyboard import HotKey
        names = ["space", "tab", "enter", "esc", "backspace", "delete", "home", "end",
                 "page up", "page down", "up", "down", "left", "right", "a", "7"] + \
                [f"f{i}" for i in range(1, 13)]
        for name in names:
            for combo in (f"alt+{name}", f"cmd+shift+{name}", f"ctrl+{name}"):
                HotKey.parse(hotkeys.to_pynput(combo))       # raises on bad syntax


class TestBareKeyGuard(unittest.TestCase):
    def test_bare_keys(self):
        for hk in ("space", "r", "enter", "SPACE"):
            self.assertTrue(hotkeys.is_bare_typing_key(hk), hk)
        for hk in ("alt+r", "cmd+space", "f9", "mouse:middle", "", None):
            self.assertFalse(hotkeys.is_bare_typing_key(hk), hk)


class TestMacListenerLayoutPatch(unittest.TestCase):
    """pynput's listener thread must not call the macOS keyboard-layout
    (TIS) API: the layout is read on the main thread and handed over."""

    def _fake_pynput(self):
        calls = []

        @contextlib.contextmanager
        def keycode_context():
            calls.append(threading.current_thread() is threading.main_thread())
            yield ("layout", len(calls))

        util = types.ModuleType("pynput._util.darwin")
        util.keycode_context = keycode_context
        kb = types.ModuleType("pynput.keyboard._darwin")
        kb.keycode_context = keycode_context
        return util, kb, calls

    def test_listener_threads_get_the_main_threads_copy(self):
        util, kb, calls = self._fake_pynput()
        # A whole fake pynput package: the real macOS modules can't import
        # here, and test_core may have stubbed pynput already.
        util_pkg = types.ModuleType("pynput._util")
        util_pkg.darwin = util
        kb_pkg = types.ModuleType("pynput.keyboard")
        kb_pkg._darwin = kb
        root = types.ModuleType("pynput")
        root._util, root.keyboard = util_pkg, kb_pkg
        fakes = {"pynput": root, "pynput._util": util_pkg, "pynput._util.darwin": util,
                 "pynput.keyboard": kb_pkg, "pynput.keyboard._darwin": kb}
        with mock.patch.object(hotkeys, "IS_MAC", True), \
             mock.patch.object(hotkeys, "_layout", None), \
             mock.patch.dict(sys.modules, fakes):
            self.assertTrue(hotkeys.prepare_pynput_listeners())
            self.assertEqual(calls, [True])                   # read on the main thread
            got = []

            def listener_thread():
                with kb.keycode_context() as ctx:
                    got.append(ctx)

            t = threading.Thread(target=listener_thread)
            t.start()
            t.join()
            self.assertEqual(got, [("layout", 1)])            # the copy...
            self.assertEqual(calls, [True])                   # ...no TIS call off-main
            self.assertTrue(hotkeys.prepare_pynput_listeners())
            self.assertEqual(calls, [True, True])             # refreshed on main

    def test_no_op_off_macos(self):
        with mock.patch.object(hotkeys, "IS_MAC", False):
            self.assertFalse(hotkeys.prepare_pynput_listeners())


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestFromKeyEvent(unittest.TestCase):
    def _ev(self, key, mods):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        return QKeyEvent(QEvent.KeyPress, key, mods)

    def test_mac_and_windows_modifiers(self):
        from PySide6.QtCore import Qt
        ev = self._ev(Qt.Key_E, Qt.ControlModifier)
        self.assertEqual(hotkeys.from_key_event(ev, mac=True), ("cmd+e", None))
        self.assertEqual(hotkeys.from_key_event(ev, mac=False), ("ctrl+e", None))
        ev = self._ev(Qt.Key_E, Qt.MetaModifier)
        self.assertEqual(hotkeys.from_key_event(ev, mac=True), ("ctrl+e", None))
        ev = self._ev(Qt.Key_Space, Qt.ShiftModifier | Qt.AltModifier)
        self.assertEqual(hotkeys.from_key_event(ev, mac=True), ("alt+shift+space", None))

    def test_modifier_alone_and_bare_keys(self):
        from PySide6.QtCore import Qt
        self.assertEqual(hotkeys.from_key_event(self._ev(Qt.Key_Control, Qt.ControlModifier)),
                         (None, "modifier_only"))
        self.assertEqual(hotkeys.from_key_event(self._ev(Qt.Key_Space, Qt.NoModifier)),
                         (None, "needs_modifier"))
        self.assertEqual(hotkeys.from_key_event(self._ev(Qt.Key_F7, Qt.NoModifier)),
                         ("f7", None))


if __name__ == "__main__":
    unittest.main()
