"""Faster dictation (no post-roll or tail decode when nothing is in flight, the
cached model first, a background preload, paste once the keys are up), Live
Assistance hearing both sides, the honest Mac privacy chip, simpler answers,
and images attached to a question (a snip or a paste)."""
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

import numpy as np

import action_api
import live_context


def _skip_without_real_numpy(case):
    # Other test files stub numpy out before main is imported.
    import types
    import main
    if type(main.np) is not types.ModuleType:        # the stub answers any attribute
        case.skipTest("numpy stubbed in main by another test module")


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


FRAME = 1024                                   # cfg chunk_size at 16 kHz: 64 ms


def _frame(level):
    t = np.arange(FRAME, dtype=np.float32) / 16000.0
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32).tobytes()


class TestRecordLoopStop(unittest.TestCase):
    """The real _record_loop on scripted audio: 10 quiet frames, 20 of speech,
    then quiet - and a stop at a chosen frame."""

    def setUp(self):
        _skip_without_real_numpy(self)

    def _run(self, stop_at, abort=False, capture_mode=None, chunks=0):
        import main
        reads = []

        def read(stream, sr):
            n = len(reads)
            reads.append(n)
            if n + 1 == stop_at:
                rec.stop_requested = True
                if abort:
                    rec._abort = True
            return _frame(0.3 if 10 <= n < 30 else 0.0005)

        rec = SimpleNamespace(
            recording=True, stop_requested=False, _abort=False, _capture_mode=capture_mode,
            fast_live_chunks=False, on_levels=None, _chunk_lock=threading.Lock(),
            _chunk_frames=[], _full_audio_truncated=False, _full_audio_frames=[],
            _full_audio_len=0, _full_audio_max=10 ** 9, _chunk_idx=chunks,
            _chunk_silence_before={}, _chunk_threads=[], _level_peak=0.05,
            _record_error="", _read_input_draining=read,
            _transcribe_chunk=lambda audio, idx: None)
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": FRAME}):
            main.AudioRecorder._record_loop(rec, False, object(), None)
        return len(reads), rec

    def test_already_quiet_stops_at_once(self):
        n, rec = self._run(stop_at=36)               # 6 quiet frames (~0.38 s) after speech
        self.assertEqual(n, 36)                      # no post-roll
        self.assertGreater(rec._tail_speech_sec, 0)  # the speech is still in the tail

    def test_mid_word_keeps_the_post_roll(self):
        n, _ = self._run(stop_at=20)                 # still speaking
        self.assertEqual(n, 20 + 8)                  # ~0.5 s to catch the last syllable

    def test_esc_never_waits(self):
        n, _ = self._run(stop_at=20, abort=True)
        self.assertEqual(n, 20)

    def test_before_any_speech_was_heard_the_post_roll_stays(self):
        n, _ = self._run(stop_at=5)                  # quiet so far, no chunk yet
        self.assertEqual(n, 5 + 8)

    def test_quiet_since_the_last_chunk(self):
        n, _ = self._run(stop_at=5, chunks=1)        # dictation: the chunk had the speech
        self.assertEqual(n, 5)
        # A meeting cuts chunks on a timer - they prove nothing about the
        # detector hearing a quiet speaker, so the post-roll stays.
        n, _ = self._run(stop_at=5, chunks=1, capture_mode="smart_meeting")
        self.assertEqual(n, 5 + 8)


class TestSilentTail(unittest.TestCase):
    def setUp(self):
        _skip_without_real_numpy(self)

    def _rec(self, tail_speech, chunks=1):
        import main
        rec = SimpleNamespace(
            _chunk_threads=[], _abort=False, _chunk_lock=threading.Lock(), _chunk_idx=chunks,
            _chunk_results={0: "Send me the report."} if chunks else {},
            on_finalising=None, _record_error="", _chunk_errors=[],
            _chunk_frames=[(np.ones(8000, dtype=np.float32) * 0.01).tobytes()],
            _tail_speech_sec=tail_speech, on_lang_detected=None, partial_text="",
            _capture_mode=None,
            _run_local=MagicMock(return_value=("Thank you.", "en")))
        return rec, main

    def test_a_tail_without_speech_is_not_decoded(self):
        rec, main = self._rec(0.0)
        with mock.patch.dict(main.cfg, {"backend": "local", "sample_rate": 16000}):
            text, _ = main.AudioRecorder.transcribe(rec)
        rec._run_local.assert_not_called()
        self.assertEqual(text, "Send me the report.")    # no made-up "Thank you."

    def test_speech_in_the_tail_or_no_chunk_yet_is_decoded(self):
        for tail, chunks in ((0.4, 1), (0.0, 0), (None, 1)):
            rec, main = self._rec(tail, chunks)
            with mock.patch.dict(main.cfg, {"backend": "local", "sample_rate": 16000}):
                main.AudioRecorder.transcribe(rec)
            rec._run_local.assert_called_once()

    def test_a_meetings_last_seconds_are_always_decoded(self):
        rec, main = self._rec(0.0)
        rec._capture_mode = "smart_meeting"          # quiet speaker, timer-cut chunks
        with mock.patch.dict(main.cfg, {"backend": "local", "sample_rate": 16000}):
            main.AudioRecorder.transcribe(rec)
        rec._run_local.assert_called_once()


class TestFasterStartAndPaste(unittest.TestCase):
    def test_the_cached_model_is_tried_before_the_network(self):
        import main
        calls = []

        class _WM:
            def __init__(self, name, **kw):
                calls.append(kw["local_files_only"])

        rec = SimpleNamespace(_model_lock=threading.Lock(), _model=None, _model_name=None,
                              _whisper_device=lambda: ("cpu", "int8"),
                              _add_cuda_dll_dirs=lambda: None)
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=_WM)}):
            main.AudioRecorder.load_model(rec, "base")
        self.assertEqual(calls, [True])                  # loaded offline, no HF round trip

    def test_a_model_not_downloaded_yet_keeps_the_gpu(self):
        import main
        calls = []

        class LocalEntryNotFoundError(Exception):
            pass

        class _WM:
            def __init__(self, name, device=None, local_files_only=None, **kw):
                calls.append((device, local_files_only))
                if local_files_only:
                    raise LocalEntryNotFoundError("pass 'local_files_only=False'")

            def transcribe(self, *a, **k):
                return iter([]), None

        rec = SimpleNamespace(_model_lock=threading.Lock(), _model=None, _model_name=None,
                              _whisper_device=lambda: ("cuda", "float16"),
                              _add_cuda_dll_dirs=lambda: None)
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=_WM)}), \
             mock.patch.object(main, "_looks_like_whisper_cache_error", return_value=False):
            main.AudioRecorder.load_model(rec, "base")
        self.assertEqual(calls, [("cuda", True), ("cuda", False)])   # downloaded, on the GPU
        self.assertIs(rec._cuda_usable, True)

    def test_the_model_is_preloaded_for_local_dictation_only(self):
        import main

        class _Now:
            def __init__(self, target=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        for backend, loads in (("local", 1), ("managed", 0)):
            app = SimpleNamespace(recorder=MagicMock(),
                                  _effective_cfg=lambda b=backend: {"backend": b,
                                                                    "whisper_model": "base"})
            with mock.patch.object(main.threading, "Thread", _Now), \
                 mock.patch.object(main, "model_downloaded", return_value=True):
                main.AppController._preload_speech_model(app)
            self.assertEqual(app.recorder.load_model.call_count, loads, backend)

    @unittest.skipUnless(sys.platform == "win32", "Windows key state")
    def test_paste_waits_only_while_keys_are_held(self):
        import ctypes
        import main
        user32 = ctypes.windll.user32
        with mock.patch.object(user32, "GetAsyncKeyState", lambda k: 0):
            t = time.monotonic()
            main.AppController._wait_keys_released(0.35)
            self.assertLess(time.monotonic() - t, 0.05)
        with mock.patch.object(user32, "GetAsyncKeyState", lambda k: 0x8000 if k == 0x12 else 0):
            t = time.monotonic()
            main.AppController._wait_keys_released(0.12)     # Alt still down
            self.assertGreaterEqual(time.monotonic() - t, 0.1)


class TestSimpleAnswersAndSnipNote(unittest.TestCase):
    def test_answers_are_short_and_plain(self):
        brief = action_api.build_messages("x", "live_assist")[0]["content"]
        self.assertIn("Keep it simple", brief)
        self.assertIn("Under ~45 words", brief)
        self.assertIn("never describe the screen", brief)

    def test_a_snip_is_the_subject_and_the_note_strips_for_text_engines(self):
        ctx = live_context.rolling_context("Hi.", question="What is this?", screen="snip")
        self.assertIn("the part of the screen the user picked", ctx)
        self.assertNotIn("picked", action_api._SCREEN_NOTE.sub("", ctx))
        self.assertIn("Use it as context", live_context.rolling_context("Hi.", screen=True))


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestOverlayImagesAndMic(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from ui import live_assist
        self.la = live_assist
        self.app = SimpleNamespace(cfg={}, save_config=MagicMock(), track=MagicMock())
        self.o = live_assist.LiveAssistOverlay(main_app=self.app)
        self.o.btn_screen.setChecked(False)

    def tearDown(self):
        self.o.hide()
        self.o.deleteLater()

    def _img(self, w=120, h=80):
        from PySide6.QtGui import QColor, QImage
        img = QImage(w, h, QImage.Format_RGB32)
        img.fill(QColor("#334155"))
        return img

    def _capture_worker(self):
        sent = []

        class _Thread:
            def __init__(self, target=None, args=(), daemon=None):
                sent.append(args)

            def start(self):
                pass
        return sent, mock.patch.object(self.la.threading, "Thread", _Thread)

    def test_start_listens_to_both_sides(self):
        mw = SimpleNamespace(STATE_RECORDING="rec", STATE_PROCESSING="proc", STATE_DONE="done",
                             state="idle", input_title=MagicMock(), calls=[])
        mw.input_title.text.return_value = "x"
        mw._start_meeting = lambda **kw: mw.calls.append(kw)
        self.app.meetings_win = mw
        self.app.is_pro = lambda: True
        self.app.recorder = SimpleNamespace(fast_live_chunks=False)
        with mock.patch.object(self.la.threading, "Thread", MagicMock()):
            self.o._start_listening()
        self.assertEqual(mw.calls[0]["audio_mode"], "smart_meeting")

    def test_a_pasted_image_is_attached(self):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        def clip(has_text):
            mime = SimpleNamespace(hasUrls=lambda: False, hasImage=lambda: True,
                                   hasText=lambda: has_text)
            return mock.patch.object(self.la.QApplication, "clipboard",
                                     lambda: SimpleNamespace(mimeData=lambda: mime,
                                                             image=lambda: self._img()))
        with clip(has_text=True):                             # Excel cells: text + a picture
            self.assertIsNone(self.o.input_ask._clipboard_image())
        with clip(has_text=False):
            self.o.input_ask.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_V, Qt.ControlModifier))
        self.assertTrue(self.o._image_b64)
        self.assertFalse(self.o.img_chip.isHidden())
        self.assertEqual(self.o.input_ask.text(), "")         # nothing pasted as text
        self.o.img_chip.click()                               # remove it
        self.assertEqual(self.o._image_b64, "")
        self.assertTrue(self.o.img_chip.isHidden())

    def test_an_image_alone_is_sent_as_the_question(self):
        self.o._attach_image(self._img())
        b64 = self.o._image_b64
        shots = MagicMock(return_value="SCREEN")
        sent, patcher = self._capture_worker()
        with patcher, mock.patch.object(self.la, "capture_screen_b64", shots):
            self.o.feed_transcript("We're looking at the diagram now.", answer=False)
            self.o._ask()                                     # Enter with an empty box
        context, image = sent[0][0], sent[0][1]
        self.assertEqual(image, b64)
        shots.assert_not_called()                             # the snip replaces the screenshot
        self.assertIn("the part of the screen the user picked", context)
        self.assertTrue(context.endswith("User's question: " + self.la.SNIP_QUESTION))
        self.assertTrue(self.o.img_chip.isHidden())
        self.assertIn("✓ Sent: the image", self.o.lbl_status.text())

    def test_a_failed_answer_gives_the_image_back(self):
        self.o._attach_image(self._img())
        self.o.input_ask.setText("what is this?")
        sent, patcher = self._capture_worker()
        with patcher:
            self.o._ask()
        self.assertEqual(self.o._image_b64, "")
        self.o._on_suggestion("", "network down", self.o._gen)
        self.assertTrue(self.o._image_b64)
        self.assertEqual(self.o.input_ask.text(), "what is this?")

    def test_privacy_mode_keeps_images_on_the_computer(self):
        self.app.cfg["privacy_mode"] = True
        with mock.patch.object(self.la.QApplication, "screens", MagicMock()) as screens:
            self.o._start_snip()
        screens.assert_not_called()
        self.o._attach_image(self._img())
        self.assertEqual(self.o._image_b64, "")
        self.assertIn("Privacy Mode", self.o.lbl_status.text())

    def test_the_picker_crops_what_was_dragged(self):
        from PySide6.QtCore import QEvent, QPointF, QRect, Qt
        from PySide6.QtGui import QMouseEvent, QPixmap
        shot = QPixmap(400, 300)
        shot.setDevicePixelRatio(2.0)                          # a 200x150 logical screen
        snip = self.la._SnipOverlay(QRect(0, 0, 200, 150), shot)
        got, cancelled = [], []
        snip.picked.connect(got.append)
        snip.cancelled.connect(lambda: cancelled.append(1))

        def ev(kind, x, y, button=Qt.LeftButton):
            return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, button, Qt.NoModifier)
        snip.mousePressEvent(ev(QEvent.MouseButtonPress, 10, 20))
        snip.mouseMoveEvent(ev(QEvent.MouseMove, 60, 70))
        snip.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, 110, 70))
        self.assertEqual((got[0].width(), got[0].height()), (200, 100))   # device pixels
        self.assertEqual(cancelled, [])
        snip.deleteLater()
        again = self.la._SnipOverlay(QRect(0, 0, 200, 150), shot)
        again.cancelled.connect(lambda: cancelled.append(1))
        again.mousePressEvent(ev(QEvent.MouseButtonPress, 10, 10))
        again.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, 12, 12))   # a click, no drag
        self.assertEqual(cancelled, [1])
        again.deleteLater()

    def test_closing_a_picker_any_way_ends_the_snip(self):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QPixmap
        pickers = []
        for _ in range(2):                                    # two monitors
            o = self.la._SnipOverlay(QRect(0, 0, 100, 80), QPixmap(100, 80))
            o.picked.connect(self.o._on_snip_picked)
            o.cancelled.connect(self.o._end_snip)
            o.show()
            pickers.append(o)
        self.o._snips = list(pickers)
        pickers[0].close()                                    # e.g. Alt+F4
        self.assertEqual(self.o._snips, [])                   # the button works again
        self.assertFalse(pickers[1].isVisible())
        self.o._snips = [pickers[1]]
        self.o._end_snip()                                    # already closed: no error

    def test_macos_15_chip_says_not_hidden_and_how_to_share(self):
        with mock.patch.object(self.la.sys, "platform", "darwin"):
            self.o._refresh_private_chip(remote=False, supported=False, excluded=False)
        self.assertEqual(self.o.btn_private.text(), "Not hidden")
        self.assertIn("Share just one window", self.o.btn_private.toolTip())


if __name__ == "__main__":
    unittest.main()
