"""meeting_store + audio_export: listing, parts, WAV round-trip, MP3, concat;
and the meeting window's post-recording pipeline and setup state."""
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

import audio_export as ax
import meeting_store as ms


def _real_numpy():
    # tests/test_core.py stubs numpy for the whole discover run; the WAV/MP3
    # tests need the real thing and are skipped under the stub.
    try:
        return np.__name__ == "numpy" and tuple(np.zeros(2).shape) == (2,)
    except Exception:
        return False


@unittest.skipUnless(_real_numpy(), "real numpy not importable (stubbed)")
class TestAudioExport(unittest.TestCase):
    def _tone(self, seconds=0.5, freq=440.0):
        t = np.arange(int(ax.RATE * seconds)) / ax.RATE
        return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    def test_wav_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.wav"
            ax.write_wav(p, self._tone())
            data, rate = ax.read_wav_int16(p)
            self.assertEqual(rate, ax.RATE)
            self.assertEqual(len(data), int(ax.RATE * 0.5))
            self.assertGreater(int(np.abs(data).max()), 10000)

    def test_concat_and_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = Path(tmp) / "1.wav", Path(tmp) / "2.wav"
            ax.write_wav(p1, self._tone(0.5))
            ax.write_wav(p2, self._tone(0.25))
            data, _ = ax.concat_wavs([p1, p2])
            self.assertEqual(len(data), int(ax.RATE * 0.75))
            self.assertAlmostEqual(ax.duration_seconds([p1, p2]), 0.75, places=2)

    def test_export_wav_and_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "1.wav"
            ax.write_wav(p1, self._tone(1.0))
            out = ax.export_recording([p1], Path(tmp) / "rec.wav")
            self.assertTrue(out.is_file() and out.stat().st_size > 20000)
            mp3 = ax.export_recording([p1], Path(tmp) / "rec.mp3")
            # PyAV ships libmp3lame in this venv; if a build lacked it the
            # export must still produce a playable WAV instead of failing.
            self.assertTrue(mp3.is_file())
            self.assertIn(mp3.suffix, (".mp3", ".wav"))
            if mp3.suffix == ".mp3":
                self.assertGreater(mp3.stat().st_size, 2000)

    def test_export_nothing(self):
        self.assertIsNone(ax.export_recording([], "x.wav"))

    def test_clipping_is_safe(self):
        loud = np.array([2.0, -2.0, 0.0], dtype=np.float32)
        ints = ax.float_to_int16(loud)
        self.assertEqual(list(ints), [32767, -32767, 0])


class TestMeetingStore(unittest.TestCase):
    def _make(self, root, name, meta=None, transcript="", notes="", parts=0):
        d = Path(root) / name
        d.mkdir(parents=True)
        if meta is not None:
            (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if transcript:
            (d / "transcript.txt").write_text(transcript, encoding="utf-8")
        if notes:
            (d / "notes.md").write_text(notes, encoding="utf-8")
        for i in range(parts):
            (d / f"audio_part{i + 1}.wav").write_bytes(b"RIFF")
        return d

    def test_list_newest_first_and_skips_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._make(tmp, "20260101_090000", {"title": "Old", "duration_sec": 60,
                                                 "timestamp": "2026-01-01 09:00:00"},
                       transcript="hi", notes="## Summary\n- first point", parts=2)
            self._make(tmp, "20260301_090000", None, transcript="later one")
            (Path(tmp) / "20260201_000000").mkdir()          # aborted, empty
            with patch.object(ms.storage, "path_for", return_value=Path(tmp)):
                items = ms.list_meetings()
        self.assertEqual([i["id"] for i in items], ["20260301_090000", "20260101_090000"])
        old = items[1]
        self.assertEqual(old["title"], "Old")
        self.assertEqual(len(old["audio_parts"]), 2)
        self.assertTrue(old["has_notes"] and old["has_transcript"])
        # Folder without meta gets a timestamp derived from its name.
        self.assertEqual(items[0]["timestamp"], "2026-03-01 09:00:00")
        self.assertEqual(items[0]["title"], "Untitled Meeting")

    def test_audio_parts_order_and_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make(tmp, "m", {}, parts=0)
            for n in (1, 2, 10):        # 10 must sort after 2 (natural order)
                (d / f"audio_part{n}.wav").write_bytes(b"RIFF")
            names = [p.name for p in ms.audio_parts(d)]
            self.assertEqual(names, ["audio_part1.wav", "audio_part2.wav", "audio_part10.wav"])
            self.assertEqual(ms.next_audio_part_path(d).name, "audio_part11.wav")
            empty = self._make(tmp, "e", {})
            self.assertEqual(ms.next_audio_part_path(empty).name, "audio_part1.wav")

    def test_summary_preview_and_duration(self):
        self.assertEqual(ms.summary_preview("## Summary\n- The team agreed on Friday."),
                         "The team agreed on Friday.")
        self.assertEqual(ms.summary_preview(""), "")
        self.assertEqual(ms.format_duration(3725), "1 h 02 min")
        self.assertEqual(ms.format_duration(65), "1 min 05 s")

    def test_lists_meetings_with_only_a_recording_or_chunks(self):
        # Transcription/notes failed: the recording, and live chunks of a
        # meeting that reached processing (meta.json), must still be reachable
        # from History. Chunks alone are the meeting being recorded right now,
        # or one discarded before 1.9.1 - never listed; so is meta.json alone.
        with tempfile.TemporaryDirectory() as tmp:
            rec_only = self._make(tmp, "20260927_100000")
            _write_wav(rec_only / "audio_part1.wav", seconds=2)
            chunks_only = self._make(tmp, "20260927_110000")
            _write_chunks(chunks_only, [(1, "second."), (0, "First,")])
            discarded = self._make(tmp, "20260927_120000")
            _write_chunks(discarded, [(0, "thrown away")])
            ms.mark_discarded(discarded)
            late = self._make(tmp, "20260927_130000")        # a chunk landing after Abort
            (late / ms.DISCARDED_CHUNKS).write_text("", encoding="utf-8")
            _write_chunks(late, [(0, "late chunk")])
            bad_meta = self._make(tmp, "20260927_140000", {"duration_sec": "n/a", "title": 7})
            _write_chunks(bad_meta, [(0, "processing failed")])
            self._make(tmp, "20260927_150000")               # empty: skipped
            self._make(tmp, "20260927_160000", {"title": "Nothing captured"})   # meta only
            with patch.object(ms.storage, "path_for", return_value=Path(tmp)):
                items = {i["id"]: i for i in ms.list_meetings()}
            self.assertEqual(set(items), {"20260927_100000", "20260927_140000"})
            rec = items["20260927_100000"]
            # Every field the History card reads is present and sensible.
            self.assertEqual(rec["title"], "Untitled Meeting")
            self.assertEqual(rec["timestamp"], "2026-09-27 10:00:00")
            self.assertEqual(rec["duration_sec"], 2)           # from the WAV header
            self.assertEqual(rec["attendees"], "")
            self.assertEqual(len(rec["audio_parts"]), 1)
            self.assertFalse(rec["has_transcript"] or rec["has_notes"])
            self.assertEqual(items["20260927_140000"]["duration_sec"], 0)
            self.assertEqual(items["20260927_140000"]["title"], "7")
            # No transcript.txt: the detail view / Resume get the chunk text,
            # in spoken order.
            self.assertEqual(ms.load_transcript(chunks_only), "First, second.")
            self.assertEqual(ms.load_transcript(rec_only), "")
            self.assertTrue((discarded / ms.DISCARDED_CHUNKS).is_file())

    def test_transcript_file_wins_over_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make(tmp, "m", transcript="Final transcript.")
            _write_chunks(d, [(0, "live chunk")])
            self.assertEqual(ms.load_transcript(d), "Final transcript.")
            # A resumed folder repeats chunk indices per segment: keep file order.
            r = self._make(tmp, "r")
            _write_chunks(r, [(0, "one"), (1, "two"), (0, "three")])
            with open(r / "chunks.jsonl", "a", encoding="utf-8") as f:
                f.write('{"index": 1, "text": "cut sho')        # crash mid-write
            self.assertEqual(ms.load_transcript(r), "one two three")


def _write_wav(path, seconds=1, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(rate * seconds))


def _write_chunks(folder, rows):
    with open(Path(folder) / "chunks.jsonl", "a", encoding="utf-8") as f:
        for idx, text in rows:
            f.write(json.dumps({"index": idx, "text": text, "language": "en"}) + "\n")


class TestMeetingDeletion(unittest.TestCase):
    """Clear History deletes meetings too - it must never reach outside the
    library, and never the meeting still being recorded."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "meetings"
        self.root.mkdir()
        p = patch.object(ms.storage, "path_for", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _meeting(self, name):
        d = self.root / name
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"title": name}), encoding="utf-8")
        (d / "audio_part1.wav").write_bytes(b"RIFF")
        return d

    def test_delete_one_meeting(self):
        d = self._meeting("20260926_210641")
        self.assertTrue(ms.delete_meeting(d))
        self.assertFalse(d.exists())
        self.assertFalse(ms.delete_meeting(d))              # already gone

    def test_refuses_anything_outside_the_library(self):
        outside = Path(self._tmp.name) / "important"
        outside.mkdir()
        self.assertFalse(ms.delete_meeting(outside))
        self.assertFalse(ms.delete_meeting(self.root))      # the library itself
        self.assertFalse(ms.delete_meeting(self.root / ".." / "important"))
        self.assertTrue(outside.exists() and self.root.exists())

    def test_delete_all_keeps_the_active_meeting(self):
        self._meeting("20260101_000000")
        self._meeting("20260102_000000")
        active = self._meeting("20260103_000000")
        (self.root / "20260104_000000").mkdir()             # an aborted, empty leftover
        self.assertEqual(ms.delete_all_meetings(keep=active), 3)
        self.assertEqual([p.name for p in self.root.iterdir()], ["20260103_000000"])
        self.assertEqual(len(ms.list_meetings()), 1)


# ── Meeting window logic, without a display ──────────────────────────────────
# MeetingsWindow's methods are borrowed onto a plain class, so no QDialog is
# built (and it works the same under tests/test_core.py's suite-wide PySide6
# stubs). Widgets are tiny fakes.

def _harness():
    from ui import meetings as um
    ns = {k: v for k, v in vars(um.MeetingsWindow).items()
          if not k.startswith("__") and isinstance(v, (types.FunctionType, staticmethod, str))}
    return um, type("MeetingsHarness", (), ns)()


class _Field:
    def __init__(self, text=""):
        self._text = text

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text

    def clear(self):
        self._text = ""


class _Combo:
    def __init__(self, values, current=None):
        self.values = list(values)
        self.index = self.values.index(current) if current in self.values else 0

    def findData(self, value):
        return self.values.index(value) if value in self.values else -1

    def currentIndex(self):
        return self.index

    def setCurrentIndex(self, i):
        self.index = i

    def currentData(self):
        return self.values[self.index] if 0 <= self.index < len(self.values) else None

    def blockSignals(self, _on):
        return False


class _Emitter:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _Rec:
    """Just the AudioRecorder surface the meeting window touches."""

    def __init__(self, result=("", "en"), truncated=False):
        self.result = result
        self._full_audio_truncated = truncated
        self._full_audio_max = 16000 * 60 * 120
        self._loopback_peak = 1.0
        self.on_chunk_complete = self.on_levels = None
        self.on_partial = self.on_lang_detected = None
        self.started = None

    def transcribe(self):
        return self.result

    def start_recording(self, **kw):
        self.started = kw

    def stop_recording(self):
        pass


class _App:
    def __init__(self, recorder=None, **cfg):
        self.cfg = {"meeting_consent_ack": True, "save_history": False,
                    "meeting_audio_mode": "default_mic", "whisper_model": "base",
                    "action_model": "managed", **cfg}
        self.recorder = recorder or _Rec()
        self.live_assist = None
        self.saves = 0

    def save_config(self):
        self.saves += 1

    def track(self, *a, **k):
        pass

    def is_pro(self):
        return True


def _window(app, meeting_dir=None, chunks=(), prior=""):
    um, w = _harness()
    w.app = app
    w.state = w.STATE_PROCESSING
    w._chunks = list(chunks)
    w._chunks_lock = threading.Lock()
    w._meeting_dir = meeting_dir
    w._meeting_title, w._meeting_attendees = "Sync", ""
    w._record_started_at = w._record_stopped_at = None
    w._resume_dir, w._resume_prior, w._resume_prior_duration = None, prior, 0
    w._transcript_notices = []
    w._user_notes = ""
    w.proc_signals = types.SimpleNamespace(finished=_Emitter())
    w.statuses = []
    w._emit_status = w.statuses.append
    w._save_recording_segment = lambda: None
    w.summarized = []
    w._summarize_and_finish = lambda: w.summarized.append(w._final_transcript)
    w._build_attributed_transcript = lambda: ""
    # Setup page + other widgets.
    w.input_title, w.input_attendees = _Field(), _Field()
    w.combo_device = _Combo(["smart_meeting", "default_mic"], app.cfg.get("meeting_audio_mode"))
    w.combo_lang = _Combo(["auto", "en"], "auto")
    w.combo_whisper = _Combo(["base", "small", "large-v3"], app.cfg.get("whisper_model"))
    w.combo_action = _Combo(["managed", "api_cerebras", "qwen_3b"], app.cfg.get("action_model"))
    for name in ("container", "live_trans_log", "live_summary_log", "input_live_notes",
                 "lbl_done_title", "txt_summary", "txt_transcript"):
        setattr(w, name, MagicMock())
    return um, w


def _chunk(idx, text):
    return {"index": idx, "text": text, "language": "en"}


class TestMeetingTranscript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _run(self, result, chunks=(), prior="", truncated=False):
        um, w = _window(_App(_Rec(result, truncated)), self.dir, chunks, prior)
        w._process_meeting_notes()
        return w

    def test_one_failed_chunk_keeps_every_chunk_that_transcribed(self):
        # Chunks land in completion order; chunk 1 failed on the cloud.
        w = self._run(("", "!managed:Managed cloud error (503)."),
                      chunks=[_chunk(2, "third part."), _chunk(0, "First part,")])
        self.assertEqual(w.proc_signals.finished.calls, [])     # no error page / reset
        self.assertEqual(len(w.summarized), 1)
        self.assertTrue(w._final_transcript.startswith("First part, third part."))
        self.assertIn("— Part of the audio couldn't be transcribed —", w._final_transcript)
        self.assertEqual(w._final_lang, "en")
        # Written for the Done page, Retry and History.
        self.assertEqual((self.dir / "transcript.txt").read_text(encoding="utf-8"),
                         w._final_transcript)
        self.assertIn("transcribed: Managed cloud error (503). The", w._transcript_notices[0])

    def test_failure_with_nothing_transcribed_still_reports_the_error(self):
        w = self._run(("", "!managed:Managed cloud error (503)."))
        self.assertEqual(w.proc_signals.finished.calls, [("", "Managed cloud error (503).")])
        self.assertEqual(w.summarized, [])

    def test_failure_in_a_resumed_meeting_keeps_the_earlier_transcript(self):
        w = self._run(("", "!transcribe:Timed out."), prior="Earlier talk.")
        self.assertEqual(len(w.summarized), 1)
        self.assertTrue(w._final_transcript.startswith("Earlier talk.\n\n— Resumed "))
        self.assertIn("couldn't be transcribed", w._final_transcript)

    def test_speaker_pass_covers_failed_chunks_but_not_a_dead_device(self):
        for lang, expect_notice in (("!managed:503", False), ("!audio:device lost", True)):
            um, w = _window(_App(_Rec(("", lang))), self.dir, [_chunk(0, "hello")])
            w._build_attributed_transcript = lambda: "Speaker 1: hello there"
            w._process_meeting_notes()
            self.assertTrue(w._final_transcript.startswith("Speaker 1: hello there"))
            self.assertEqual(bool(w._transcript_notices), expect_notice, lang)
            if expect_notice:
                self.assertIn("stopped early: device lost.", w._transcript_notices[0])

    def test_transcript_is_not_doubled(self):
        # Speaker labelling unavailable: the transcript is transcribe()'s joined
        # text (ordered, with the final tail) - exactly once.
        joined = "Hello team. We ship Friday. Any questions?"
        w = self._run((joined, "en"), chunks=[_chunk(0, "Hello team."),
                                              _chunk(1, "We ship Friday.")])
        self.assertEqual(w._final_transcript, joined)
        self.assertEqual(w.summarized, [joined])

    def test_long_meeting_keeps_the_full_transcript(self):
        # Past the recorder's full-audio cap, the speaker pass would only see
        # the first 2 h and replace the complete transcript - it must not run.
        full = " ".join(f"minute {m}." for m in range(150))
        um, w = _window(_App(_Rec((full, "en"), truncated=True)), self.dir)
        w._build_attributed_transcript = MagicMock(return_value="Speaker 1: minute 0.")
        w._process_meeting_notes()
        w._build_attributed_transcript.assert_not_called()
        self.assertEqual(w._final_transcript, full)
        self.assertIn("minute 149.", (self.dir / "transcript.txt").read_text(encoding="utf-8"))
        self.assertIn("2 h 00 min", w._transcript_notices[0])
        # ...and a shorter meeting still gets its speaker labels.
        um, w = _window(_App(_Rec((full, "en"))), self.dir)
        w._build_attributed_transcript = lambda: "Speaker 1: all of it"
        w._process_meeting_notes()
        self.assertEqual(w._final_transcript, "Speaker 1: all of it")
        self.assertEqual(w._transcript_notices, [])

    def test_done_page_shows_the_notices_above_the_notes(self):
        um, w = _window(_App(), self.dir)
        w._final_transcript = "t"
        w._transcript_notices = ["The saved recording ends at about 2 h 00 min."]
        with patch.object(um.telemetry, "track"), \
                patch.dict(sys.modules, {"main": _fake_main()}):
            w._on_processing_finished("## Notes", "")
        shown = w.txt_summary.setMarkdown.call_args[0][0]
        self.assertTrue(shown.startswith("> ⚠️ The saved recording ends at about 2 h"))
        self.assertTrue(shown.endswith("## Notes"))
        self.assertEqual(w._final_notes, "## Notes")      # exports stay clean


def _fake_main():
    m = types.ModuleType("main")
    m.APP_VERSION = "0.0.0-test"
    return m


class TestMeetingStartState(unittest.TestCase):
    """Settings changes survive a Start, and a Start after a finished (or a
    failed resumed) session is a genuinely new meeting."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "meetings"
        self.root.mkdir()
        for p in (patch.object(ms.storage, "path_for", return_value=self.root),
                  patch.dict(sys.modules, {"main": _fake_main()})):
            p.start()
            self.addCleanup(p.stop)

    def _start(self, w, **kw):
        from ui import meetings as um
        with patch.object(um.telemetry, "track"):
            w._start_meeting(**kw)

    def test_start_uses_settings_changed_after_launch_without_saving(self):
        app = _App()
        um, w = _window(app)
        w.state = w.STATE_IDLE
        # Settings saved new picks after the window was built at launch.
        app.cfg.update(whisper_model="large-v3", action_model="api_cerebras",
                       meeting_audio_mode="smart_meeting")
        self._start(w, language="en")
        self.assertEqual(w.state, w.STATE_RECORDING)
        self.assertEqual((app.cfg["whisper_model"], app.cfg["action_model"],
                          app.cfg["meeting_audio_mode"]),
                         ("large-v3", "api_cerebras", "smart_meeting"))
        self.assertEqual(app.saves, 0)
        self.assertEqual(w.combo_whisper.currentData(), "large-v3")
        self.assertEqual(w.combo_action.currentData(), "api_cerebras")
        self.assertEqual(app.recorder.started["capture_mode"], "smart_meeting")

    def test_resync_is_read_only_and_a_pick_here_is_saved(self):
        app = _App(action_model="rule_based")        # not offered in the meeting picker
        um, w = _window(app)
        w._sync_setup_from_cfg()
        self.assertEqual(w.combo_action.currentData(), "managed")   # Pro default shown
        self.assertEqual((app.cfg["action_model"], app.saves), ("rule_based", 0))
        w.combo_whisper.setCurrentIndex(1)                          # user picks "small"
        w._on_whisper_picked(1)
        self.assertEqual((app.cfg["whisper_model"], app.saves), ("small", 1))
        w._sync_setup_from_cfg()
        self.assertEqual(w.combo_whisper.currentData(), "small")

    def test_live_assist_start_after_done_is_a_new_meeting(self):
        old = self.root / "20260101_090000"
        old.mkdir()
        app = _App()
        um, w = _window(app, old, prior="Old meeting transcript.")
        w._resume_dir = old                     # a resumed meeting...
        w._meeting_title, w._meeting_attendees = "Board meeting", "CEO, CFO"
        w.input_title.setText("Board meeting")
        w.input_attendees.setText("CEO, CFO")
        w._final_transcript = "Old meeting transcript."
        with patch.object(um.telemetry, "track"):
            w._on_processing_finished("", "quota exceeded")   # ...whose summary failed
        self.assertEqual(w.state, w.STATE_DONE)
        self.assertEqual(w._resume_dir, old)                  # kept for Retry
        self.assertEqual((w.input_title.text(), w.input_attendees.text()), ("", ""))
        # What the Live Assistance card does: name the session only if untitled.
        if not w.input_title.text().strip():
            w.input_title.setText("Live session 11:00")
        self._start(w, language="en")
        self.assertEqual(w.state, w.STATE_RECORDING)
        self.assertNotEqual(w._meeting_dir, old)
        self.assertEqual(w._meeting_dir.parent, self.root)
        self.assertEqual((w._resume_dir, w._resume_prior), (None, ""))
        self.assertEqual((w._meeting_title, w._meeting_attendees), ("Live session 11:00", ""))

    def test_resume_keeps_its_folder(self):
        old = self.root / "20260101_090000"
        old.mkdir()
        (old / "transcript.txt").write_text("ORIGINAL", encoding="utf-8")
        (old / "meta.json").write_text(json.dumps({"title": "Board", "duration_sec": 60}),
                                       encoding="utf-8")
        um, w = _window(_App())
        w.state = w.STATE_DONE
        w.show = w.raise_ = w.activateWindow = lambda: None
        with patch.object(um.telemetry, "track"):
            w.resume_meeting(str(old))
        self.assertEqual(w.state, w.STATE_RECORDING)
        self.assertEqual(w._meeting_dir, old)
        self.assertEqual((w._resume_prior, w._meeting_title), ("ORIGINAL", "Board"))

    def test_meetings_started_in_the_same_second_get_their_own_folders(self):
        from ui import meetings as um
        (self.root / "20260927_100000").mkdir()
        _, w = _window(_App())
        w.state = w.STATE_DONE
        with patch.object(um.time, "strftime", return_value="20260927_100000"):
            self._start(w)
        self.assertEqual(w._meeting_dir.name, "20260927_100000_2")
        self.assertEqual(ms._folder_timestamp(w._meeting_dir.name), "2026-09-27 10:00:00")

    def test_abort_keeps_a_discarded_session_out_of_history(self):
        um, w = _window(_App())
        w.state = w.STATE_IDLE
        self._start(w)
        _write_chunks(w._meeting_dir, [(0, "never mind")])
        w._abort()
        self.assertEqual(w.state, w.STATE_IDLE)
        self.assertEqual(ms.list_meetings(), [])
        # A resumed meeting's folder is left alone.
        old = self.root / "20260101_090000"
        old.mkdir()
        (old / "transcript.txt").write_text("ORIGINAL", encoding="utf-8")
        w.show = w.raise_ = w.activateWindow = lambda: None
        with patch.object(um.telemetry, "track"):
            w.resume_meeting(str(old))
        _write_chunks(old, [(0, "more")])
        w._abort()
        self.assertTrue((old / "chunks.jsonl").is_file())
        self.assertEqual([m["id"] for m in ms.list_meetings()], ["20260101_090000"])


if __name__ == "__main__":
    unittest.main()
