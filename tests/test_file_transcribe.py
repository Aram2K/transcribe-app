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

    def test_free_covers_a_full_hour(self):
        # The product promise: free users can do 1-hour files.
        self.assertEqual(self.ft.duration_error(60 * 60, False), "")

    def test_free_blocks_over_an_hour_and_mentions_pro(self):
        msg = self.ft.duration_error(61 * 60, False)
        self.assertTrue(msg)
        self.assertIn("Pro", msg)

    def test_pro_covers_five_hours(self):
        self.assertEqual(self.ft.duration_error(5 * 3600, True), "")
        msg = self.ft.duration_error(6 * 3600, True)
        self.assertTrue(msg)
        self.assertNotIn("upgrade", msg.lower())   # don't upsell Pro to Pro

    def test_max_seconds(self):
        self.assertEqual(self.ft.max_seconds(False), 3600)
        self.assertEqual(self.ft.max_seconds(True), 5 * 3600)

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
