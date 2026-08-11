"""AppController's dictation-path seams: cleanup and clipboard restore.

Built via AppController.__new__ with a hand-set .cfg, so no Qt/recorder/tray is
constructed. Reuses the heavy-import stubs installed by test_core.
"""
import os
import sys
import unittest

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main


def controller(**cfg):
    c = main.AppController.__new__(main.AppController)
    c.cfg = {**main.DEFAULT, **cfg}
    return c


class TestPostprocessTranscript(unittest.TestCase):
    def test_collapses_hallucination_loop(self):
        c = controller()
        self.assertEqual(
            c._postprocess_transcript("Thank you. Thank you. Thank you. Thank you."),
            "Thank you.")

    def test_leaves_normal_dictation_alone(self):
        c = controller()
        text = "Let's ship the release on Friday."
        self.assertEqual(c._postprocess_transcript(text), text)

    def test_applies_user_replacements(self):
        c = controller(cleanup_replacements=[{"from": "pyside 6", "to": "PySide6"}])
        self.assertEqual(c._postprocess_transcript("i use pyside 6"), "i use PySide6")

    def test_master_switch_off_stops_second_guessing_the_recognizer(self):
        c = controller(cleanup_enabled=False)
        text = "Thank you. Thank you. Thank you."
        self.assertEqual(c._postprocess_transcript(text), text)

    def test_master_switch_off_still_applies_replacements(self):
        # The UI labels these "always replace these words" - a deliberate
        # correction is not a cleanup heuristic, so it must survive the switch.
        c = controller(cleanup_enabled=False,
                       cleanup_replacements=[{"from": "gorge", "to": "George"}])
        self.assertEqual(c._postprocess_transcript("hi gorge"), "hi George")

    def test_master_switch_off_leaves_artifacts_alone(self):
        c = controller(cleanup_enabled=False)
        self.assertEqual(c._postprocess_transcript("[Applause] hello"),
                         "[Applause] hello")

    def test_empty_input(self):
        self.assertEqual(controller()._postprocess_transcript(""), "")
        self.assertEqual(controller()._postprocess_transcript(None), "")

    def test_never_raises_on_a_broken_config(self):
        # A malformed config must degrade to the raw transcript, not lose it.
        c = controller(cleanup_replacements="not-a-list")
        self.assertEqual(c._postprocess_transcript("hello"), "hello")

    def test_effective_cfg_is_the_interception_point(self):
        c = controller()
        self.assertIs(c._effective_cfg(), c.cfg)


class TestShouldRestoreClipboard(unittest.TestCase):
    def _call(self, **kw):
        args = {"pasted": True, "restore_enabled": True,
                "prev_was_text": True, "still_ours": True}
        args.update(kw)
        return main.AppController.should_restore_clipboard(**args)

    def test_restores_in_the_normal_case(self):
        self.assertTrue(self._call())

    def test_not_when_disabled(self):
        self.assertFalse(self._call(restore_enabled=False))

    def test_not_when_paste_failed(self):
        # The overlay says "Copied to clipboard" - the text must stay there.
        self.assertFalse(self._call(pasted=False))

    def test_not_when_previous_clipboard_was_not_text(self):
        # Never clear an image/file clipboard to an empty string.
        self.assertFalse(self._call(prev_was_text=False))

    def test_not_when_someone_else_changed_the_clipboard(self):
        self.assertFalse(self._call(still_ours=False))


class TestNewConfigDefaults(unittest.TestCase):
    def test_cleanup_defaults(self):
        self.assertTrue(main.DEFAULT["cleanup_enabled"])
        self.assertTrue(main.DEFAULT["cleanup_strip_hallucinations"])
        self.assertTrue(main.DEFAULT["cleanup_strip_artifacts"])
        # Filler removal changes the user's words, so it must be opt-in.
        self.assertFalse(main.DEFAULT["cleanup_remove_fillers"])
        self.assertEqual(main.DEFAULT["cleanup_replacements"], [])

    def test_vocabulary_defaults(self):
        self.assertEqual(main.DEFAULT["vocabulary"], [])
        self.assertTrue(main.DEFAULT["vocabulary_share_with_cloud"])

    def test_clipboard_defaults(self):
        self.assertTrue(main.DEFAULT["restore_clipboard"])
        self.assertEqual(main.DEFAULT["clipboard_restore_delay_ms"], 400)

    def test_schema_version_present(self):
        self.assertEqual(main.DEFAULT["config_schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
