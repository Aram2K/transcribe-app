"""meeting_store + audio_export: listing, parts, WAV round-trip, MP3, concat."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
