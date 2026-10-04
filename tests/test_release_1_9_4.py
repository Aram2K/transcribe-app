"""1.9.4 review fixes: the fast on-disk model check stays in sync with
faster-whisper, Settings learns the GPU state without importing ctranslate2
on the GUI thread, and a crop the card shows to a share ends when the card
moves to another monitor."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


def _real_module(name):
    try:
        mod = __import__(name)
    except Exception:
        return False
    import types
    return type(mod) is types.ModuleType and bool(getattr(mod, "__file__", None))


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


class TestWhisperRepos(unittest.TestCase):
    def test_the_fast_check_knows_the_same_repos_as_faster_whisper(self):
        import main
        if not _real_module("faster_whisper"):
            self.skipTest("faster_whisper stubbed in this run")
        from faster_whisper.utils import _MODELS
        for name in main.MODELS:
            self.assertEqual(main._WHISPER_REPOS.get(name), _MODELS.get(name), name)

    def test_a_missing_cache_folder_reads_as_not_downloaded(self):
        import main
        with mock.patch("os.listdir", side_effect=FileNotFoundError):
            self.assertFalse(main._cached_model_bin("Systran/faster-whisper-base"))


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestSpeedPhrase(unittest.TestCase):
    def _me(self, state):
        rec = SimpleNamespace(_cuda_usable=None)
        return SimpleNamespace(_cuda=None, _gpu_state=state,
                               app=SimpleNamespace(recorder=rec, cfg={}))

    def test_cpu_wording_until_the_worker_reports(self):
        from ui import settings
        me = self._me(None)
        with mock.patch.object(settings.sys, "platform", "win32"), \
             mock.patch.object(settings.gpu_accel, "status",
                               side_effect=AssertionError("no GPU probe on the GUI thread")):
            self.assertTrue(settings.Settings._speed_phrase(me, 3).endswith("on your CPU"))
        self.assertIsNone(me._cuda)                      # not remembered: asked again later
        me._gpu_state = "ready"
        with mock.patch.object(settings.sys, "platform", "win32"):
            self.assertTrue(settings.Settings._speed_phrase(me, 3).endswith("on your GPU"))

    def test_a_mac_never_imports_ctranslate2_for_it(self):
        from ui import settings
        me = self._me(None)
        with mock.patch.object(settings.sys, "platform", "darwin"), \
             mock.patch.dict(sys.modules, {"ctranslate2": None}):
            self.assertTrue(settings.Settings._speed_phrase(me, 3).endswith("on your CPU"))


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestCropFollowsTheCard(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from ui import live_assist
        self.la = live_assist
        self.o = live_assist.LiveAssistOverlay(main_app=SimpleNamespace(
            cfg={}, save_config=mock.MagicMock(), track=mock.MagicMock()))

    def tearDown(self):
        self.o.deleteLater()

    def _open_crop(self, screen):
        from PySide6.QtGui import QPixmap
        self.o._crop_shots = [(screen, QPixmap(20, 20))]
        self.o._crop_safe = 0
        self.o._crop_idx = 0
        self.o.crop_panel.show()

    def test_moving_a_visible_card_to_another_monitor_ends_the_crop(self):
        here, elsewhere = object(), object()
        self._open_crop(here)
        self.o._exclusion_ok = False
        self.o.screen = lambda: here
        self.o._guard_crop_screen()
        self.assertTrue(self.o._crop_shots)              # same monitor: stays open
        self.o.screen = lambda: elsewhere
        self.o._guard_crop_screen()
        self.assertEqual(self.o._crop_shots, [])
        self.assertIn("moved to another screen", self.o.lbl_status.text())

    def test_a_hidden_card_keeps_its_crop(self):
        self._open_crop(object())
        self.o._exclusion_ok = True
        self.o.screen = lambda: object()
        self.o._guard_crop_screen()
        self.assertTrue(self.o._crop_shots)


if __name__ == "__main__":
    unittest.main()
