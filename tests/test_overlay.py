"""Dictation HUD (ui/overlay.py): sizes, error copy, theme choice, level
gating, the never-steal-focus window contract, and an offscreen paint smoke
for every state. Qt-dependent parts are guarded like test_wheelguard."""
import os
import time
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


class _FixedMetrics:
    """7 px per character: the offscreen platform has no real fonts, so copy
    decisions are tested against deterministic widths."""
    def __init__(self, font):
        pass

    def horizontalAdvance(self, text):
        return 7.0 * len(text)

    def elidedText(self, text, mode, width):
        return text


def _ensure_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestErrorHint(unittest.TestCase):
    def test_contextual_hints(self):
        from ui.overlay import _error_hint
        self.assertIn("API key", _error_hint("401 Unauthorized"))
        self.assertIn("microphone", _error_hint("Microphone error: device busy"))
        self.assertIn("shorter clip", _error_hint("Request timed out"))
        self.assertEqual(_error_hint("something odd"), "Try again or restart Transcribe")


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestOverlay(unittest.TestCase):
    def setUp(self):
        _ensure_app()
        import ui.overlay as ov
        self.ov = ov
        self.cfg = {"hotkey": "alt+r", "accent_color": "#3b82f6", "backend": "local"}
        self.app = SimpleNamespace(cfg=self.cfg, save_config=MagicMock())
        self.hud = ov.Overlay(main_app=self.app)

    def tearDown(self):
        self.hud.timer.stop()
        self.hud.hide()
        self.hud.deleteLater()

    # ── window contract ──
    def test_never_takes_focus_but_takes_the_mouse(self):
        # The text is pasted into whatever has focus: the HUD must never take
        # it - but it must get mouse input, so it can be dragged. (Outside
        # the capsule the window is made click-through at runtime.)
        from PySide6.QtCore import Qt
        flags = self.hud.windowFlags()
        self.assertTrue(flags & Qt.WindowDoesNotAcceptFocus)
        self.assertFalse(flags & Qt.WindowTransparentForInput)
        self.assertTrue(flags & Qt.WindowStaysOnTopHint)
        self.assertTrue(self.hud.testAttribute(Qt.WA_ShowWithoutActivating))
        # Offscreen test platform: no fake window id may reach a Win32 call.
        self.assertEqual(self.hud._hwnd, 0)

    # ── moving it ──
    def _mouse(self, kind, local, button=None):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        types = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove,
                 "release": QEvent.MouseButtonRelease, "double": QEvent.MouseButtonDblClick}
        local = QPointF(local)
        glob = QPointF(self.hud.mapToGlobal(local.toPoint()))
        btn = Qt.LeftButton if kind != "move" else Qt.NoButton
        held = Qt.LeftButton if kind in ("press", "move", "double") else Qt.NoButton
        ev = QMouseEvent(types[kind], local, glob, btn, held, Qt.NoModifier)
        getattr(self.hud, {"press": "mousePressEvent", "move": "mouseMoveEvent",
                           "release": "mouseReleaseEvent",
                           "double": "mouseDoubleClickEvent"}[kind])(ev)
        return ev

    def _capsule_centre(self):
        return self.hud._capsule_now().center()

    def test_dragging_moves_it_and_remembers_the_spot(self):
        from PySide6.QtCore import QPointF
        self.hud.show_overlay(self.ov.RECORDING)
        start = self.hud.pos()
        c = self._capsule_centre()
        self._mouse("press", c)
        self._mouse("move", c + QPointF(-60, -40))
        self._mouse("release", c + QPointF(-60, -40))
        moved = self.hud.pos()
        self.assertNotEqual(moved, start)
        self.assertEqual(self.cfg["overlay_pos"], [moved.x(), moved.y()])
        self.app.save_config.assert_called()
        self.assertIsNone(self.hud._press)

    def test_a_click_is_not_a_drag(self):
        from PySide6.QtCore import QPointF
        self.hud.show_overlay(self.ov.RECORDING)
        start = self.hud.pos()
        c = self._capsule_centre()
        self._mouse("press", c)
        self._mouse("move", c + QPointF(1, 1))           # under the drag slop
        self._mouse("release", c + QPointF(1, 1))
        self.assertEqual(self.hud.pos(), start)
        self.assertNotIn("overlay_pos", self.cfg)
        self.app.save_config.assert_not_called()

    def test_presses_outside_the_capsule_are_ignored(self):
        from PySide6.QtCore import QPointF
        self.hud.show_overlay(self.ov.RECORDING)
        ev = self._mouse("press", QPointF(3, 3))           # transparent corner
        self.assertFalse(ev.isAccepted())
        self.assertIsNone(self.hud._press)

    def test_double_click_sends_it_home(self):
        self.cfg["overlay_pos"] = [40, 40]
        self.hud.show_overlay(self.ov.RECORDING)
        self._mouse("double", self._capsule_centre())
        self.assertIsNone(self.cfg["overlay_pos"])
        self.assertIsNotNone(self.hud._glide)
        for _ in range(200):                               # let the glide finish
            self.hud._glide_step(0.016)
            if self.hud._glide is None:
                break
        self.assertIsNone(self.hud._glide)
        self.assertEqual(self.hud.pos(), self.hud._home(self.hud._screen))

    def test_saved_position_is_used_only_while_on_a_screen(self):
        from PySide6.QtWidgets import QApplication
        a = QApplication.primaryScreen().availableGeometry()
        self.cfg["overlay_pos"] = [a.x() + 10, a.y() + 10]
        self.hud.reposition()
        self.assertEqual((self.hud.x(), self.hud.y()), (a.x() + 10, a.y() + 10))
        self.cfg["overlay_pos"] = [a.x() + 100000, a.y() + 100000]   # monitor gone
        self.hud.reposition()
        self.assertEqual(self.hud.pos(), self.hud._home(self.hud._screen))
        self.cfg["overlay_pos"] = "garbage"
        self.hud.reposition()
        self.assertEqual(self.hud.pos(), self.hud._home(self.hud._screen))

    # ── live glass samples ──
    def _sample(self, luma, gen=None):
        from PySide6.QtGui import QImage
        img = QImage(8, 8, QImage.Format_RGB32)
        img.fill(0)
        return (self.hud._glass_gen if gen is None else gen, img, (0, 0), 1.0, luma)

    def test_first_sample_picks_the_theme_and_starts_the_fade_in(self):
        self.hud.show_overlay(self.ov.RECORDING)
        self.hud._live, self.hud._waiting = True, True
        self.hud._on_backdrop(self._sample(0.95))
        self.assertFalse(self.hud._waiting)
        self.assertEqual(self.hud._theme, "light")           # auto, bright backdrop
        self.assertIsNotNone(self.hud._bd)
        seq = self.hud._bd_seq
        self.hud._on_backdrop(self._sample(0.1))              # a later live sample
        self.assertEqual(self.hud._bd_seq, seq + 1)
        self.assertEqual(self.hud._theme, "light")            # theme holds per appearance

    def test_samples_from_an_earlier_appearance_are_dropped(self):
        self.hud.show_overlay(self.ov.RECORDING)
        self.hud._live, self.hud._waiting = True, True
        self.hud._on_backdrop(self._sample(0.95, gen=self.hud._glass_gen - 1))
        self.assertTrue(self.hud._waiting)
        self.assertIsNone(self.hud._bd)

    def test_without_live_glass_one_sample_only_sets_the_theme(self):
        self.hud.show_overlay(self.ov.RECORDING)
        self.hud._live, self.hud._waiting = False, True
        self.hud._sampler = MagicMock()
        self.hud._on_backdrop(self._sample(0.1))
        self.assertEqual(self.hud._theme, "dark")
        self.assertIsNone(self.hud._bd)                       # plate, not a stale picture
        self.assertFalse(self.hud._sampler.active)

    def test_the_fade_in_never_waits_forever_for_a_sample(self):
        self.hud.show_overlay(self.ov.RECORDING)
        self.hud._waiting = True
        self.hud._shown_at = time.monotonic() - self.ov.FIRST_SAMPLE_S - 0.01
        self.hud._loop()
        self.assertFalse(self.hud._waiting)
        self.assertGreater(self.hud._show, 0.0)

    def test_done_waits_while_the_cursor_is_on_it(self):
        self.hud.show_done(True)
        self.hud._hover = True
        self.hud._update_hover = lambda: None              # keep the hover
        self.hud._hide_at = time.monotonic() - 1           # already due
        self.hud._loop()
        self.assertTrue(self.hud._visible)
        self.assertGreater(self.hud._hide_at, time.monotonic())

    def test_frame_clock_runs_only_while_shown(self):
        self.assertFalse(self.hud.timer.isActive())
        self.hud.show_overlay(self.ov.RECORDING)
        self.assertTrue(self.hud.timer.isActive())
        self.hud.hide_overlay()
        self.hud._show = 0.0001
        self.hud._loop()
        self.assertFalse(self.hud.timer.isActive())
        self.assertFalse(self.hud.isVisible())

    # ── sizes ──
    def test_recording_capsule_settles_after_the_hint(self):
        ov = self.ov
        self.hud.overlay_state = ov.RECORDING
        self.hud._rec_started = time.monotonic()
        self.assertEqual(self.hud._target_size(), (float(ov.REC_W_HINT), float(ov.PILL_H)))
        self.hud._rec_started = time.monotonic() - ov.HINT_S - 0.1
        self.assertEqual(self.hud._target_size(), (float(ov.REC_W), float(ov.PILL_H)))
        self.assertLess(ov.REC_W, ov.REC_W_HINT)

    def test_busy_capsule_grows_with_live_words_up_to_a_cap(self):
        ov = self.ov
        self.hud.overlay_state = ov.TRANSCRIBING
        short_w, _ = self.hud._target_size()
        self.hud.set_partial("so the launch moves to the second week of October")
        mid_w, h = self.hud._target_size()
        self.hud.set_partial("word " * 400)
        long_w, _ = self.hud._target_size()
        self.assertGreater(mid_w, short_w)
        self.assertEqual(long_w, float(ov.PILL_MAX_W))
        self.assertEqual(h, float(ov.PILL_H))
        # Every state fits in the fixed window with room for the shadow.
        self.assertLessEqual(ov.PILL_MAX_W, ov.WIN_W - 2 * 16)

    def test_error_is_taller_and_fits(self):
        ov = self.ov
        self.hud.show_error("x" * 500)
        w, h = self.hud._target_size()
        self.assertEqual(h, float(ov.PILL_H_ERROR))
        self.assertLessEqual(w, float(ov.PILL_MAX_W))

    # ── error copy ──
    def _lines(self, msg):
        self.hud.show_error(msg)
        self.hud._err_split = None          # drop the split memoised with real metrics
        with mock.patch.object(self.ov, "QFontMetricsF", _FixedMetrics):
            return self.hud._error_lines()

    def test_short_error_gets_a_contextual_hint(self):
        title, detail = self._lines("Only background noise was detected.")
        self.assertEqual(title, "Only background noise was detected.")
        self.assertEqual(detail, "Try again or restart Transcribe")

    def test_long_error_splits_at_its_own_dash_instead_of_repeating_a_hint(self):
        title, detail = self._lines("Transcription timed out after 120s - try a shorter clip "
                                    "or a smaller model.")
        self.assertEqual(title, "Transcription timed out after 120s")
        self.assertEqual(detail, "Try a shorter clip or a smaller model.")

    def test_long_error_without_separator_wraps(self):
        msg = ("The selected input device stopped delivering audio frames while the "
               "recording was running so nothing could be transcribed this time")
        title, detail = self._lines(msg)
        self.assertTrue(title and detail)
        self.assertEqual((title + " " + detail).split(), msg.split())

    # ── theme ──
    def test_theme_is_auto_by_default(self):
        # No key: light glass over bright content, dark over dark.
        self.hud._bd_luma = 0.95
        self.assertEqual(self.hud._pick_theme(), "light")
        self.hud._bd_luma = 0.1
        self.assertEqual(self.hud._pick_theme(), "dark")
        # No snapshot (macOS, transparency effects off): the system theme.
        self.hud._bd_luma = None
        with mock.patch.object(self.ov, "_system_prefers_light", return_value=True):
            self.assertEqual(self.hud._pick_theme(), "light")
        with mock.patch.object(self.ov, "_system_prefers_light", return_value=False):
            self.assertEqual(self.hud._pick_theme(), "dark")

    def test_pinned_theme_wins_over_the_backdrop(self):
        self.cfg["overlay_theme"] = "dark"
        self.hud._bd_luma = 0.95
        self.assertEqual(self.hud._pick_theme(), "dark")
        self.cfg["overlay_theme"] = "light"
        self.hud._bd_luma = 0.1
        self.assertEqual(self.hud._pick_theme(), "light")
        self.cfg["overlay_theme"] = "neon"                         # unknown -> auto
        self.assertEqual(self.hud._pick_theme(), "dark")

    # ── voice levels ──
    def test_levels_gate_the_noise_floor(self):
        self.hud.update_levels([0.02] * 20)
        self.assertLess(self.hud._level_target, 0.02)
        self.hud.update_levels([0.9] * 20)
        self.assertGreater(self.hud._level_target, 0.8)
        before = self.hud._level_target
        self.hud.update_levels([])                                 # ignored
        self.assertEqual(self.hud._level_target, before)

    # ── paint smoke ──
    def _render(self):
        from PySide6.QtGui import QImage, QColor
        from PySide6.QtCore import Qt
        hud = self.hud
        hud._show = 1.0
        hud._xfade = 1.0
        hud._from_vis = None
        hud._cur_vis = hud._visual()
        hud._pw, hud._ph = hud._target_size()
        img = QImage(hud.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        hud.render(img)
        ov = self.ov
        centre = img.pixelColor(ov.WIN_W // 2, int(ov.WIN_H - ov.BOTTOM_PAD - hud._ph / 2))
        corner = img.pixelColor(2, 2)
        return centre, corner

    def test_every_state_paints_a_capsule_on_a_clear_window(self):
        ov = self.ov
        states = [
            lambda: self.hud.show_overlay(ov.RECORDING),
            lambda: self.hud.set_state(ov.TRANSCRIBING),
            lambda: self.hud.set_partial("hello world, this is a test"),
            lambda: self.hud.set_state(ov.PROCESSING),
            lambda: self.hud.show_done(True),
            lambda: self.hud.show_done(False),
            lambda: self.hud.show_error("Microphone error: device unavailable"),
        ]
        for theme in ("dark", "light"):
            self.hud._theme = theme
            for step in states:
                step()
                centre, corner = self._render()
                self.assertGreater(centre.alpha(), 200, (theme, self.hud._visual()))
                self.assertEqual(corner.alpha(), 0, (theme, self.hud._visual()))


class TestGlassHelpers(unittest.TestCase):
    def test_helpers_are_safe_everywhere(self):
        from ui import glass
        self.assertIsInstance(glass.animations_enabled(), bool)
        self.assertIsInstance(glass.foreground_monitor_name(), str)


if __name__ == "__main__":
    unittest.main()
