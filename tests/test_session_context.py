"""Live Assistance session context: what the user writes about the call
before (or during) it is locked in and shapes every answer of the session.
The prompt side is pure logic; the overlay parts need a real PySide6 and are
guarded like test_live_assist."""
import os
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import action_api
import local_llm
from live_context import CONTEXT_CHARS, clip_context, rolling_context

BRIEF = ("Call with Acme's data team about moving their reports to our platform. "
         "I'm the solutions engineer; they care about cost and security.")


class TestPrompt(unittest.TestCase):
    def test_the_context_comes_first_and_is_the_same_on_every_call(self):
        a = rolling_context("Hello there.", question="What now?", title="Sync",
                            session_context=BRIEF)
        b = rolling_context("Hello there. Something else was said.", session_context=BRIEF)
        head = "About this session (written by the user; it holds for every answer):\n" + BRIEF
        self.assertTrue(a.startswith(head))
        self.assertTrue(b.startswith(head))            # a stable, cacheable prefix
        self.assertLess(a.index(BRIEF), a.index("Meeting: Sync"))
        self.assertTrue(a.endswith("User's question: What now?"))

    def test_no_context_no_section(self):
        self.assertNotIn("About this session", rolling_context("Hi.", session_context="  \n "))
        self.assertNotIn("About this session", rolling_context("Hi."))

    def test_a_long_context_is_clipped(self):
        self.assertEqual(clip_context("  brief  "), "brief")
        long = "word " * 3000
        clipped = clip_context(long)
        self.assertLessEqual(len(clipped), CONTEXT_CHARS + 1)
        self.assertTrue(clipped.endswith("…"))
        self.assertIn(clipped, rolling_context("Hi.", session_context=long))

    def test_the_briefs_explain_it(self):
        cloud = action_api.build_messages("x", "live_assist")[0]["content"]
        self.assertIn('"About this session"', cloud)
        self.assertIn("holds for the whole session", cloud)
        self.assertIn("beyond what the session context says", cloud)   # its facts may be used
        self.assertNotIn("interview", cloud)
        local = local_llm._messages_for("live_assist", "hi?", "auto", "en")[-1]["content"]
        self.assertIn("About this session", local)
        self.assertLess(len(local), len(cloud))


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestOverlayContext(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from ui import live_assist
        self.la = live_assist
        self.app = SimpleNamespace(cfg={}, save_config=MagicMock(), track=MagicMock())
        self.o = live_assist.LiveAssistOverlay(main_app=self.app)

    def tearDown(self):
        self.o.hide()
        self.o.deleteLater()

    def _write(self, text):
        self.o._open_context_editor()
        self.o.ctx_edit.setPlainText(text)
        self.o._finish_context_edit()

    def test_empty_invites_you_to_add_one(self):
        chip = self.o.ctx_chip
        self.assertEqual(chip.text.text(), self.la.CONTEXT_EMPTY)
        self.assertTrue(chip.tag.isHidden())
        self.assertTrue(self.o.ctx_panel.isHidden())

    def test_the_editor_takes_the_transcript_slot_and_saves(self):
        self.o._open_context_editor()
        self.assertFalse(self.o.ctx_panel.isHidden())
        self.assertTrue(self.o.txt_summary.isHidden())
        self.assertTrue(self.o.ctx_chip.isHidden())
        self.o.ctx_edit.setPlainText(BRIEF + "\n")
        self.o._finish_context_edit()
        self.assertTrue(self.o.ctx_panel.isHidden())
        self.assertFalse(self.o.txt_summary.isHidden())
        self.assertEqual(self.app.cfg["live_assist_context"], BRIEF)
        self.app.save_config.assert_called()
        self.assertEqual(self.o.ctx_chip.tag.text(), "CONTEXT")
        self.assertTrue(self.o.ctx_chip.text.text().startswith("Call with Acme"))
        self.assertIn("Context saved", self.o.lbl_status.text())

    def test_locked_in_for_the_session_and_sent_with_every_answer(self):
        self._write(BRIEF)
        self.o.set_meeting_active(True, "Live session 10:30", "")
        self.assertEqual(self.o.ctx_chip.tag.text(), "LOCKED IN")
        self.assertFalse(self.o.ctx_chip.lock.isHidden())
        self.assertEqual(self.o.lbl_status.text(), "Context locked in for this session.")
        self.o.btn_screen.setChecked(False)
        sent = []

        class _Thread:
            def __init__(self, target=None, args=(), daemon=None):
                sent.append(args[0])

            def start(self):
                pass

        with mock.patch.object(self.la.threading, "Thread", _Thread):
            self.o.feed_transcript("How much would the migration cost?", answer=False)
            self.o.suggest("What should I say?")
        self.assertTrue(sent[0].startswith("About this session"))
        self.assertIn(BRIEF, sent[0])
        self.o.set_meeting_active(False)
        self.assertEqual(self.o.ctx_chip.tag.text(), "CONTEXT")   # unlocked, still kept

    def test_a_pasted_document_is_cut_to_what_the_model_gets(self):
        self.o._open_context_editor()
        self.o.ctx_edit.setPlainText("x" * (CONTEXT_CHARS + 900))
        self.assertEqual(len(self.o.ctx_edit.toPlainText()), CONTEXT_CHARS)
        self.assertIn(f"{CONTEXT_CHARS:,} / {CONTEXT_CHARS:,}", self.o.lbl_ctx_count.text())

    def test_keys_ctrl_enter_finishes_enter_is_a_new_line(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        self.o._open_context_editor()
        self.o.ctx_edit.setPlainText("line one")
        self.o.ctx_edit.moveCursor(self.o.ctx_edit.textCursor().MoveOperation.End)
        self.o.ctx_edit.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, "\r"))
        self.assertFalse(self.o.ctx_panel.isHidden())
        self.assertIn("\n", self.o.ctx_edit.toPlainText())
        self.o.ctx_edit.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ControlModifier))
        self.assertTrue(self.o.ctx_panel.isHidden())
        self.assertEqual(self.app.cfg["live_assist_context"], "line one")

    def test_start_locks_in_what_is_typed(self):
        self.o._open_context_editor()
        self.o.ctx_edit.setPlainText(BRIEF)
        self.o._start_listening()                    # no recorder here: it stops after
        self.assertTrue(self.o.ctx_panel.isHidden())
        self.assertEqual(self.app.cfg["live_assist_context"], BRIEF)

    def test_a_meeting_started_elsewhere_locks_in_what_is_typed(self):
        self.o._open_context_editor()
        self.o.ctx_edit.setPlainText(BRIEF)
        self.o.set_meeting_active(True, "Weekly sync", "")     # from Record Meeting
        self.assertTrue(self.o.ctx_panel.isHidden())
        self.assertEqual(self.o._context, BRIEF)
        self.assertEqual(self.o.ctx_chip.tag.text(), "LOCKED IN")
        self.assertEqual(self.o.lbl_status.text(), "Context locked in for this session.")

    def test_clear_removes_it(self):
        self._write(BRIEF)
        self.o._open_context_editor()
        self.o._clear_context_text()
        self.o._finish_context_edit()
        self.assertEqual(self.app.cfg["live_assist_context"], "")
        self.assertEqual(self.o.ctx_chip.text.text(), self.la.CONTEXT_EMPTY)
        self.assertEqual(self.o.lbl_status.text(), "Context cleared.")

    def test_the_saved_context_is_back_next_time(self):
        app = SimpleNamespace(cfg={"live_assist_context": "Board prep"},
                              save_config=MagicMock(), track=MagicMock())
        o = self.la.LiveAssistOverlay(main_app=app)
        try:
            self.assertEqual(o._context, "Board prep")
            self.assertEqual(o.ctx_chip.text.text(), "Board prep")
            o._open_context_editor()
            self.assertEqual(o.ctx_edit.toPlainText(), "Board prep")
        finally:
            o.deleteLater()


if __name__ == "__main__":
    unittest.main()
