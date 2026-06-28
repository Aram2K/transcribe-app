"""Tests for diarization speaker/segment alignment + a guarded model smoke test.

The alignment tests are pure logic (no numpy/models) so they always run. The
smoke test runs the real sherpa-onnx pipeline only when the models are present
AND numpy is the real one (it's skipped under the suite-wide stubs in
test_core, but runs when this file is executed on its own).
"""
import unittest

import diarization


def _real_numpy():
    try:
        import numpy as np
        return float(np.asarray([1.0, 2.0, 3.0]).mean()) == 2.0
    except Exception:
        return False


class TestAttributeSegments(unittest.TestCase):
    def test_max_overlap_assignment(self):
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        segs = [{"start": 0.5, "end": 4.0, "text": "a"},
                {"start": 6.0, "end": 9.0, "text": "b"}]
        out = diarization.attribute_segments(segs, turns)
        self.assertEqual(out[0]["speaker"], 0)
        self.assertEqual(out[1]["speaker"], 1)

    def test_straddle_picks_dominant_speaker(self):
        turns = [(0.0, 5.0, 0), (5.0, 10.0, 1)]
        # 4.0-10.0 overlaps spk0 by 1s and spk1 by 5s -> spk1 wins.
        out = diarization.attribute_segments(
            [{"start": 4.0, "end": 10.0, "text": "x"}], turns)
        self.assertEqual(out[0]["speaker"], 1)

    def test_no_overlap_snaps_to_nearest(self):
        # 5-6 doesn't overlap the 0-2 turn, but snaps to its speaker (nearest).
        out = diarization.attribute_segments(
            [{"start": 5.0, "end": 6.0, "text": "x"}], [(0.0, 2.0, 7)])
        self.assertEqual(out[0]["speaker"], 7)

    def test_picks_nearest_of_several(self):
        turns = [(0.0, 2.0, 0), (100.0, 102.0, 1)]
        out = diarization.attribute_segments(
            [{"start": 3.0, "end": 4.0, "text": "x"}], turns)  # closer to turn 0
        self.assertEqual(out[0]["speaker"], 0)

    def test_empty_turns_is_none(self):
        out = diarization.attribute_segments(
            [{"start": 5.0, "end": 6.0, "text": "x"}], [])
        self.assertIsNone(out[0]["speaker"])

    def test_offset_shifts_matching(self):
        # 1-3 shifted by +10 -> 11-13 overlaps the 10-15 turn.
        out = diarization.attribute_segments(
            [{"start": 1.0, "end": 3.0, "text": "x"}], [(10.0, 15.0, 3)], offset=10.0)
        self.assertEqual(out[0]["speaker"], 3)

    def test_preserves_original_keys(self):
        out = diarization.attribute_segments(
            [{"start": 0.0, "end": 1.0, "text": "hello", "language": "en"}], [])
        self.assertIsNone(out[0]["speaker"])
        self.assertEqual(out[0]["text"], "hello")
        self.assertEqual(out[0]["language"], "en")


class TestBuildSpeakerTranscript(unittest.TestCase):
    def test_merges_consecutive_same_speaker(self):
        segs = [
            {"text": "hello there", "speaker": 0},
            {"text": "how are you", "speaker": 0},
            {"text": "i am good", "speaker": 1},
        ]
        out = diarization.build_speaker_transcript(segs)
        self.assertEqual(
            out, "Speaker 1: hello there how are you\nSpeaker 2: i am good")

    def test_labels_are_first_appearance_order(self):
        # raw speaker ids 7 then 3 -> renumbered 1 then 2
        segs = [{"text": "a", "speaker": 7}, {"text": "b", "speaker": 3},
                {"text": "c", "speaker": 7}]
        out = diarization.build_speaker_transcript(segs)
        self.assertEqual(out, "Speaker 1: a\nSpeaker 2: b\nSpeaker 1: c")

    def test_names_override(self):
        segs = [{"text": "hi", "speaker": 0}, {"text": "yo", "speaker": 1}]
        out = diarization.build_speaker_transcript(segs, names={0: "You"})
        self.assertEqual(out, "You: hi\nSpeaker 1: yo")

    def test_none_speaker_and_blank_segments(self):
        segs = [{"text": "  ", "speaker": 0}, {"text": "lonely", "speaker": None}]
        out = diarization.build_speaker_transcript(segs)
        self.assertEqual(out, "Speaker ?: lonely")

    def test_empty(self):
        self.assertEqual(diarization.build_speaker_transcript([]), "")


@unittest.skipUnless(_real_numpy() and diarization.is_available(),
                     "diarization models / real numpy not available")
class TestDiarizeSmoke(unittest.TestCase):
    def test_diarize_sample_returns_turns(self):
        import os
        import wave
        import numpy as np
        wav = os.path.join(str(diarization._models_dir()), "sample.wav")
        if not os.path.exists(wav):
            self.skipTest("sample.wav not present")
        with wave.open(wav) as w:
            ch, n = w.getnchannels(), w.getnframes()
            raw = w.readframes(n)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            audio = audio.reshape(-1, ch).mean(axis=1)
        turns = diarization.diarize(audio)
        self.assertTrue(turns, "expected non-empty speaker turns")
        for (s, e, spk) in turns:
            self.assertLessEqual(s, e)
            self.assertIsInstance(spk, int)


if __name__ == "__main__":
    unittest.main()
