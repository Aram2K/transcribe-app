"""Regression test for the "stuck on Finalising" GPU hang.

On a machine that has an NVIDIA GPU but is missing the CUDA runtime libraries
(cuBLAS/cuDNN, e.g. cublas64_12.dll), a faster-whisper CUDA model *constructs*
fine and only fails when it actually computes. load_model() must detect that via
its warm-up and fall back to CPU. The bug: after the first CUDA attempt failed
its warm-up, a second CUDA attempt (offline cache) skipped the warm-up and
handed back the broken GPU model, which then hung on real dictation.
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

# Reuse the heavy-import stubs (+ the main import) from the core test module so
# this runs without GPU/audio hardware or a display.
try:
    from tests import test_core  # noqa: F401  (import installs stubs)
except ImportError:  # when run as a top-level module
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import faster_whisper


class _FakeModel:
    """Constructs on any device; computing on cuda raises - exactly how
    ctranslate2 behaves when the CUDA math libs cannot be loaded."""

    def __init__(self, device):
        self.device = device

    def transcribe(self, *args, **kwargs):
        if self.device == "cuda":
            raise RuntimeError(
                "Library cublas64_12.dll is not found or cannot be loaded")
        return iter([]), types.SimpleNamespace(language="en")


class TestGpuFallback(unittest.TestCase):
    def test_missing_cublas_falls_back_to_cpu(self):
        constructed = []

        def fake_whispermodel(name, device="cpu", **kwargs):
            constructed.append(device)
            return _FakeModel(device)

        rec = main.AudioRecorder()
        # Force the "this host has a CUDA GPU" path regardless of the test machine.
        with patch.object(faster_whisper, "WhisperModel",
                          side_effect=fake_whispermodel), \
             patch.object(main.AudioRecorder, "_whisper_device",
                          staticmethod(lambda: ("cuda", "int8_float16"))):
            rec.load_model("base")

        # GPU proven unusable, the stored model ended up on CPU, and we did NOT
        # build a second warm-up-skipping CUDA model.
        self.assertIs(rec._cuda_usable, False)
        self.assertEqual(getattr(rec._model, "device", None), "cpu")
        self.assertEqual(
            constructed.count("cuda"), 1,
            f"CUDA should be attempted exactly once, got {constructed}")


if __name__ == "__main__":
    unittest.main()
