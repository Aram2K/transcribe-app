"""File-transcription tab: tier gating, auto-model choice, formatting helpers.

The Qt widget itself is exercised by offscreen smokes; these cover the pure
decision logic. Guarded like test_wheelguard: under the suite-wide PySide6
stubs the module import is skipped rather than faked.
"""
import unittest


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestTierGate(unittest.TestCase):
    def setUp(self):
        import ui.file_transcribe as ft
        self.ft = ft

    def test_five_hour_files_for_everyone_without_plan_talk(self):
        # Transcription is local: no plan decides the length, only memory.
        self.assertEqual(self.ft.duration_error(61 * 60), "")
        self.assertEqual(self.ft.duration_error(5 * 3600), "")
        msg = self.ft.duration_error(6 * 3600)
        self.assertIn("up to 5 hours", msg)
        self.assertNotIn("Pro", msg)
        self.assertNotIn("upgrade", msg.lower())

    def test_duration_formatting(self):
        self.assertEqual(self.ft._fmt_dur(59), "59 s")
        self.assertEqual(self.ft._fmt_dur(3725), "1 h 02 min")

    def test_auto_model_is_a_real_catalog_model(self):
        from main import MODELS
        self.assertIn(self.ft.pick_auto_model(), MODELS)

    def test_progress_stage_bounds_are_ordered(self):
        f = self.ft
        self.assertLess(f._P_READ_END, f._P_DOWNLOAD_END)
        self.assertLess(f._P_DOWNLOAD_END, f._P_TRANSCRIBE_END_WITH_SPK)
        self.assertLess(f._P_TRANSCRIBE_END_WITH_SPK, f._P_SPEAKERS_END)
        self.assertLessEqual(f._P_TRANSCRIBE_END_NO_SPK, 96)


if __name__ == "__main__":
    unittest.main()
