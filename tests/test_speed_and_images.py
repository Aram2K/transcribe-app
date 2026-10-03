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
            main.AudioRecorder._build_model_locked(rec, "base")
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
            main.AudioRecorder._build_model_locked(rec, "base")
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

    def _shots(self, n=1, w=400, h=300, dpr=1.0):
        """Fake monitors: each grabWindow returns a w x h device-pixel shot."""
        from PySide6.QtGui import QColor, QPixmap
        screens = []
        for i in range(n):
            pm = QPixmap(w, h)
            pm.fill(QColor("#1e293b" if i == 0 else "#b91c1c"))
            pm.setDevicePixelRatio(dpr)
            screens.append(SimpleNamespace(grabWindow=MagicMock(return_value=pm)))
        return screens

    def _drag(self, view, a, b):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        def ev(kind, x, y):
            return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.LeftButton,
                               Qt.LeftButton, Qt.NoModifier)
        view.mousePressEvent(ev(QEvent.MouseButtonPress, *a))
        view.mouseMoveEvent(ev(QEvent.MouseMove, *b))
        view.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, *b))

    def _open_crop(self, n=1, under=0, card=0, **shot):
        screens = self._shots(n, **shot)
        with mock.patch.object(self.la.QApplication, "screens", return_value=screens), \
                mock.patch.object(self.la, "screen_to_capture", return_value=screens[under]), \
                mock.patch.object(self.o, "screen", return_value=screens[card]):
            self.o._start_snip()
        return screens

    def test_the_snip_crops_inside_the_card_not_on_the_screen(self):
        from PySide6.QtWidgets import QApplication
        before = set(QApplication.topLevelWidgets())
        self._open_crop()
        self.assertEqual(set(QApplication.topLevelWidgets()), before)   # no new window
        self.assertFalse(self.o.crop_panel.isHidden())
        self.assertTrue(self.o.body.isHidden())
        self.assertEqual(self.o.btn_crop_attach.text(), "Attach whole screen")
        self.assertTrue(self.o.btn_crop_screen.isHidden())               # one monitor

    def test_the_whole_screen_is_attached_without_a_drag(self):
        self._open_crop()
        self.o._crop_attach()
        self.assertTrue(self.o._image_b64)
        self.assertEqual((self.o._image.width(), self.o._image.height()), (400, 300))
        self.assertTrue(self.o.crop_panel.isHidden())
        self.assertFalse(self.o.body.isHidden())

    def test_a_drag_crops_in_screenshot_pixels(self):
        self._open_crop()
        view = self.o.crop_view
        view.resize(201, 151)                  # drawn at half size, 1px frame
        t = view._target()
        self._drag(view, (t.x() + 10, t.y() + 20), (t.x() + 110, t.y() + 70))
        self.assertTrue(view.has_selection())
        self.assertEqual(self.o.btn_crop_attach.text(), "Attach selection")
        r = view.crop_rect()
        self.assertAlmostEqual(r.width(), 200, delta=2)
        self.assertAlmostEqual(r.height(), 100, delta=2)
        self.o._crop_attach()
        self.assertAlmostEqual(self.o._image.width(), 200, delta=2)

    def test_a_click_without_a_drag_means_the_whole_screen(self):
        self._open_crop()
        view = self.o.crop_view
        view.resize(201, 151)
        t = view._target()
        self._drag(view, (t.x() + 10, t.y() + 10), (t.x() + 60, t.y() + 60))
        self._drag(view, (t.x() + 10, t.y() + 10), (t.x() + 11, t.y() + 11))
        self.assertFalse(view.has_selection())
        self.assertEqual((view.crop_rect().width(), view.crop_rect().height()), (400, 300))

    def test_a_double_click_attaches_the_selection_it_confirms(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        self._open_crop()
        view = self.o.crop_view
        view.resize(201, 151)
        t = view._target()
        self._drag(view, (t.x() + 10, t.y() + 20), (t.x() + 110, t.y() + 70))
        # What Qt delivers for a double-click: press, release, double-click, release.
        pt = QPointF(t.x() + 50, t.y() + 40)

        def ev(kind):
            return QMouseEvent(kind, pt, pt, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        view.mousePressEvent(ev(QEvent.MouseButtonPress))
        view.mouseReleaseEvent(ev(QEvent.MouseButtonRelease))
        view.mouseDoubleClickEvent(ev(QEvent.MouseButtonDblClick))
        view.mouseReleaseEvent(ev(QEvent.MouseButtonRelease))
        self.assertAlmostEqual(self.o._image.width(), 200, delta=2)   # not the whole 400
        self.assertTrue(self.o.crop_panel.isHidden())

    def test_a_click_then_a_quick_drag_is_a_drag_not_a_double_click(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        self._open_crop()
        view = self.o.crop_view
        view.resize(201, 151)
        t = view._target()
        accepted = []
        view.accepted.connect(lambda: accepted.append(1))

        def ev(kind, x, y):
            p = QPointF(t.x() + x, t.y() + y)
            return QMouseEvent(kind, p, p, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        view.mousePressEvent(ev(QEvent.MouseButtonPress, 10, 10))
        view.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, 10, 10))
        view.mouseDoubleClickEvent(ev(QEvent.MouseButtonDblClick, 11, 11))   # 80 ms later
        view.mouseMoveEvent(ev(QEvent.MouseMove, 110, 80))
        view.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, 110, 80))
        self.assertEqual(accepted, [])
        self.assertTrue(view.has_selection())
        self.assertFalse(self.o.crop_panel.isHidden())

    def test_a_thin_drag_over_one_line_is_a_crop(self):
        self._open_crop(w=3840, h=2160)
        view = self.o.crop_view
        view.resize(452, 256)
        t = view._target()
        self._drag(view, (t.x() + 20, t.y() + 100), (t.x() + 270, t.y() + 103))   # 250x3
        self.assertTrue(view.has_selection())
        self.assertGreater(view.crop_rect().width(), 2000)

    def test_cancel_puts_the_keys_back_in_the_ask_box(self):
        from PySide6.QtWidgets import QApplication
        self.o.show()
        self._open_crop()
        self.o.crop_view.setFocus()
        QApplication.processEvents()
        self.o.crop_view.cancelled.emit()                     # Esc
        QApplication.processEvents()
        self.assertTrue(self.o.input_ask.hasFocus())          # not Stop/Start, where Space acts
        self.assertFalse(self.o.btn_stop.hasFocus() or self.o.btn_start.hasFocus())

    def test_closing_the_card_any_way_ends_the_crop(self):
        self.o.show()
        self._open_crop()
        self.o.close()                                        # e.g. Alt+F4
        self.assertEqual(self.o._crop_shots, [])
        self.assertTrue(self.o.crop_view._shot.isNull())
        self.o.show()
        self.assertTrue(self.o.crop_panel.isHidden())
        self.assertFalse(self.o.body.isHidden())

    def test_while_visible_the_crop_starts_on_the_cards_monitor_not_the_cursors(self):
        self.o._exclusion_ok = False
        self._open_crop(2, under=1, card=0)
        self.assertEqual(self.o._crop_idx, 0)
        self.o._crop_attach()
        self.assertEqual(self.o._image.pixelColor(5, 5).name(), "#1e293b")

    def test_while_hidden_the_crop_starts_on_the_monitor_you_look_at(self):
        self.o._exclusion_ok = True
        self._open_crop(2, under=1, card=0)
        self.assertEqual(self.o._crop_idx, 1)

    def test_turning_private_off_leaves_the_other_monitor_before_unhiding(self):
        self.o._exclusion_ok = True
        self._open_crop(2, under=1, card=0)
        self.assertEqual(self.o._crop_idx, 1)
        seen = []

        def exclude(widget, enabled=True):
            seen.append((enabled, self.o._crop_idx))
            return enabled
        with mock.patch.object(self.la.glass, "exclude_from_capture", side_effect=exclude), \
                mock.patch.object(self.la.glass, "capture_exclusion_supported", return_value=True), \
                mock.patch.object(self.la.glass, "is_remote_session", return_value=False):
            self.o._private = False
            self.o._apply_private()
        self.assertEqual(seen, [(False, 0)])                  # already on the card's monitor
        self.assertEqual(self.o._crop_idx, 0)

    def test_the_card_screen_failing_to_capture_while_visible_never_shows_another(self):
        self.o._exclusion_ok = False
        screens = self._shots(2)
        screens[0].grabWindow.return_value = self.la.QPixmap()     # the card's monitor fails
        with mock.patch.object(self.la.QApplication, "screens", return_value=screens), \
                mock.patch.object(self.la, "screen_to_capture", return_value=screens[1]), \
                mock.patch.object(self.o, "screen", return_value=screens[0]):
            self.o._start_snip()
        self.assertTrue(self.o.crop_panel.isHidden())
        self.assertIn("Couldn't capture", self.o.lbl_status.text())

    def test_cancel_and_collapse_close_the_crop(self):
        self._open_crop()
        self.o.crop_view.cancelled.emit()                     # Esc
        self.assertTrue(self.o.crop_panel.isHidden())
        self.assertEqual(self.o._image_b64, "")
        self.assertTrue(self.o.crop_view._shot.isNull())      # screen not kept in memory
        self._open_crop()
        self.o.set_expanded(False)
        self.assertTrue(self.o.crop_panel.isHidden())
        self.o.set_expanded(True)
        self.assertFalse(self.o.body.isHidden())
        self._open_crop()                                      # and it opens again

    def test_two_monitors_can_be_switched(self):
        self.o._exclusion_ok = True                           # hidden from capture
        self._open_crop(2)
        self.assertFalse(self.o.btn_crop_screen.isHidden())
        self.assertTrue(self.o.btn_crop_screen.isEnabled())
        self.assertEqual(self.o.btn_crop_screen.text(), "Screen 1 of 2")
        self.o._crop_next_screen()
        self.assertEqual(self.o.btn_crop_screen.text(), "Screen 2 of 2")
        self.o._crop_attach()
        self.assertEqual(self.o._image.pixelColor(5, 5).name(), "#b91c1c")

    def test_another_monitor_never_shows_while_the_card_is_visible(self):
        self.o._exclusion_ok = False                          # e.g. Private off
        self._open_crop(2)
        self.assertFalse(self.o.btn_crop_screen.isEnabled())
        self.assertIn("isn't hidden from screen sharing", self.o.lbl_crop_hint.text())
        # Switched while hidden, then the card became visible: back to its own monitor.
        self.o._exclusion_ok = True
        self.o._crop_next_screen()
        self.assertEqual(self.o._crop_idx, 1)
        self.o._exclusion_ok = False
        self.o._refresh_crop_controls()
        self.assertEqual(self.o._crop_idx, 0)                 # the card's own monitor
        self.assertEqual(self.o.btn_crop_screen.text(), "Screen 1 of 2")
        self.o._crop_attach()
        self.assertEqual(self.o._image.pixelColor(5, 5).name(), "#1e293b")

    def test_a_handed_back_image_does_not_steal_the_crop_views_keys(self):
        from PySide6.QtWidgets import QApplication
        self.o.show()
        self._open_crop()
        self.o.crop_view.setFocus()
        QApplication.processEvents()
        self.o._attach_image(self._img())                     # a failed answer's image
        QApplication.processEvents()
        self.assertFalse(self.o.input_ask.hasFocus())
        self.assertFalse(self.o.crop_panel.isHidden())

    def test_a_wobbly_click_on_a_4k_screen_is_still_a_click(self):
        self._open_crop(w=3840, h=2160)
        view = self.o.crop_view
        view.resize(452, 256)
        t = view._target()
        self._drag(view, (t.x() + 50, t.y() + 50), (t.x() + 52, t.y() + 52))   # 2px on screen
        self.assertFalse(view.has_selection())                # ~17 screenshot px, still a click
        self._drag(view, (t.x() + 50, t.y() + 50), (t.x() + 60, t.y() + 60))
        self.assertTrue(view.has_selection())

    def test_hidpi_screenshots_crop_in_device_pixels(self):
        self._open_crop(w=800, h=600, dpr=2.0)               # a 400x300 logical screen
        view = self.o.crop_view
        view.resize(402, 302)
        t = view._target()
        self._drag(view, (t.x() + t.width() * 0.25, t.y() + t.height() * 0.25),
                   (t.x() + t.width() * 0.75, t.y() + t.height() * 0.75))
        r = view.crop_rect()
        self.assertAlmostEqual(r.width(), 400, delta=3)
        self.assertAlmostEqual(r.height(), 300, delta=3)
        img = view.crop_image()
        self.assertAlmostEqual(img.width(), 400, delta=3)

    def test_macos_chip_says_may_be_hidden_when_the_flag_is_set(self):
        for macos26, caveat in ((False, "doesn't guarantee"), (True, "Not yet verified on macOS 26")):
            with mock.patch.object(self.la.sys, "platform", "darwin"), \
                    mock.patch.object(self.la.glass, "macos_26_or_later", return_value=macos26):
                self.o._refresh_private_chip(remote=False, supported=False, excluded=True)
            tip = self.o.btn_private.toolTip()
            self.assertEqual(self.o.btn_private.text(), "May be hidden")
            for must in ("QuickTime", "single window", "second device", caveat):
                self.assertIn(must, tip)
            for never in ("undetect", "invisible", "always hidden"):
                self.assertNotIn(never, tip.lower())

    def _hover_tooltip(self, widget):
        """What Qt does on hover: a ToolTip event to the widget, which shows it."""
        from PySide6.QtCore import QEvent, QPoint
        from PySide6.QtGui import QHelpEvent
        from PySide6.QtWidgets import QApplication
        QApplication.sendEvent(widget, QHelpEvent(QEvent.ToolTip, QPoint(5, 5),
                                                  widget.mapToGlobal(QPoint(5, 5))))
        QApplication.processEvents()

    def test_tooltips_and_menus_from_the_card_are_hidden_like_it(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication, QMenu, QPushButton, QToolTip, QWidget
        self.o.show()
        self.o._private, self.o._exclusion_ok = True, True
        calls = []
        other = QWidget()                                     # e.g. the Settings window
        other_btn = QPushButton("x", other)
        other_btn.setToolTip("A Settings tooltip")
        other.show()
        with mock.patch.object(self.la.glass, "exclude_from_capture",
                               side_effect=lambda w, on=True: calls.append((w, on)) or True):
            menu = QMenu(self.o.input_ask)                    # a right-click menu
            menu.addAction("Copy")
            menu.popup(QPoint(10, 10))
            QApplication.processEvents()
            self._hover_tooltip(self.o.btn_theme)
            tips = [w for w, on in calls if on and w.windowType() == self.la.Qt.ToolTip]
            self.assertTrue(tips)                             # the card's tooltip: hidden
            QToolTip.hideText()
            QApplication.processEvents()
            calls.clear()
            self._hover_tooltip(other_btn)
            QApplication.processEvents()
            foreign = QMenu(other)
            foreign.popup(QPoint(30, 30))
            QApplication.processEvents()
        menu.close(); foreign.close(); QToolTip.hideText()
        # Another window's tooltip and menus are never hidden from shares.
        self.assertEqual([w for w, on in calls if on], [])
        self.assertNotIn(foreign, [w for w, _ in calls])
        other.deleteLater()

    def test_a_card_menu_is_hidden_as_it_shows(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication, QMenu
        self.o.show()
        self.o._private, self.o._exclusion_ok = True, True
        with mock.patch.object(self.la.glass, "exclude_from_capture", return_value=True) as ex:
            menu = QMenu(self.o.input_ask)
            menu.addAction("Copy")
            menu.popup(QPoint(10, 10))
            QApplication.processEvents()
        menu.close()
        self.assertIn(mock.call(menu, True), ex.call_args_list)

    def test_popups_are_left_alone_while_the_card_is_visible_in_shares(self):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication, QMenu
        self.o.show()
        self.o._private, self.o._exclusion_ok, self.o._mac_partial = False, False, False
        with mock.patch.object(self.la.glass, "exclude_from_capture") as exclude:
            menu = QMenu(self.o.input_ask)
            menu.addAction("Copy")
            menu.popup(QPoint(10, 10))
            QApplication.processEvents()
        menu.close()
        # (The card's own watchdog may call it for the card itself.)
        self.assertNotIn(menu, [c.args[0] for c in exclude.call_args_list])

    def _mac15(self, sticks=True):
        return [mock.patch.object(self.la.sys, "platform", "darwin"),
                mock.patch.object(self.la.glass, "IS_MAC", True),
                mock.patch.object(self.la.glass, "capture_exclusion_supported", return_value=False),
                mock.patch.object(self.la.glass, "is_remote_session", return_value=False),
                mock.patch.object(self.la.glass, "exclude_from_capture", return_value=sticks),
                mock.patch.object(self.la.glass, "is_excluded_from_capture", return_value=sticks)]

    def test_macos_15_apply_private_shows_mostly_hidden_only_when_the_flag_sticks(self):
        from contextlib import ExitStack
        for sticks, text in ((True, "May be hidden"), (False, "Not hidden")):
            with ExitStack() as stack:
                for p in self._mac15(sticks):
                    stack.enter_context(p)
                self.o._private = True
                self.o._apply_private()
                self.assertEqual(self.o.btn_private.text(), text)
                self.assertFalse(self.o._exclusion_ok)        # never a promise on 15+

    def test_macos_15_watchdog_reapplies_a_lost_flag(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._mac15(sticks=False):
                stack.enter_context(p)
            self.o._private = True
            stack.enter_context(mock.patch.object(self.o, "isVisible", return_value=True))
            glass_again = stack.enter_context(mock.patch.object(self.o, "_apply_glass"))
            self.o._on_tick()
        glass_again.assert_called()

    def test_macos_15_chip_says_not_hidden_and_how_to_share(self):
        with mock.patch.object(self.la.sys, "platform", "darwin"):
            self.o._refresh_private_chip(remote=False, supported=False, excluded=False)
        self.assertEqual(self.o.btn_private.text(), "Not hidden")
        self.assertIn("Share a single window", self.o.btn_private.toolTip())


if __name__ == "__main__":
    unittest.main()
