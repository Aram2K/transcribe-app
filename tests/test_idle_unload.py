"""Idle RAM: the speech (Whisper) and local AI (llama.cpp) models are freed
after sitting unused, and never while something is using them. Fake models
only - nothing real is loaded."""
import gc
import os
import sys
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Never the real settings, keyring or app data (main loads config at import).
os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="transcribe-test-data-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")

import actions
import local_llm
import main


# ── Whisper ────────────────────────────────────────────────────────────────

class _FakeWhisper:
    def transcribe(self, *a, **k):
        return iter([]), SimpleNamespace(language="en")


def _recorder():
    rec = main.AudioRecorder.__new__(main.AudioRecorder)     # no PyAudio
    rec._model_lock = threading.Lock()
    rec._users_lock = threading.Lock()
    rec._infer_lock = threading.Lock()
    rec._model = None
    rec._model_name = None
    rec._model_users = 0
    rec._last_model_use = 0.0
    rec.recording = False
    rec._chunk_threads = []
    return rec


def _resident(rec):
    """A _load_locked stand-in: a fake model, built once."""
    return lambda name: setattr(rec, "_model", rec._model or _FakeWhisper())


class TestWhisperLease(unittest.TestCase):
    def test_each_caller_gets_exactly_the_model_it_asked_for(self):
        # A file job (large-v3) and a dictation (base) at the same time: one
        # lock hold from load to hand-out, so neither gets the other's model
        # (and never None).
        rec, builds = _recorder(), []

        def load(name):
            if rec._model is None or rec._model_name != name:
                rec._model = None
                time.sleep(0.03)                       # a build takes a while
                rec._model, rec._model_name = SimpleNamespace(name=name), name
                builds.append(name)
        rec._load_locked = load
        got, errors = [], []

        def use(name):
            try:
                with rec.use_model(name) as m:
                    got.append((name, m.name))
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=use, args=(n,)) for n in ("large-v3", "base") * 4]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(got), 8)
        self.assertTrue(all(asked == handed for asked, handed in got), got)
        self.assertEqual(rec._model_users, 0)

    def test_finishing_never_waits_behind_another_models_load(self):
        rec = _recorder()
        rec._load_locked = _resident(rec)
        with mock.patch.dict(main.cfg, {"whisper_model": "base"}):
            lease = rec.use_model()
            lease.__enter__()
            held, release = threading.Event(), threading.Event()

            def build():                                # another model's 4-9 s load
                with rec._model_lock:
                    held.set()
                    release.wait(3)
            t = threading.Thread(target=build, daemon=True)
            t.start()
            held.wait(2)
            t0 = time.monotonic()
            lease.__exit__(None, None, None)
            took = time.monotonic() - t0
            release.set()
            t.join(3)
        self.assertLess(took, 0.1)                      # the text isn't held back
        self.assertEqual(rec._model_users, 0)

    def test_the_idle_sweep_never_frees_a_model_in_use(self):
        rec = _recorder()
        rec._load_locked = _resident(rec)
        with mock.patch.dict(main.cfg, {"whisper_model": "base"}):
            with rec.use_model() as model:
                later = time.monotonic() + 3600
                self.assertFalse(rec.maybe_unload_idle(600, now=later))
                self.assertIs(rec._model, model)
        self.assertTrue(rec.maybe_unload_idle(600, now=time.monotonic() + 3600))
        self.assertIsNone(rec._model)

    def test_a_failing_transcription_still_releases_the_model(self):
        rec = _recorder()
        rec._load_locked = _resident(rec)
        with mock.patch.dict(main.cfg, {"whisper_model": "base"}):
            with self.assertRaises(ValueError):
                with rec.use_model():
                    raise ValueError("decode failed")
        self.assertEqual(rec._model_users, 0)

    def test_the_sweep_waits_for_idle_time_recordings_and_chunk_threads(self):
        rec = _recorder()
        rec._model, rec._model_name = _FakeWhisper(), "base"
        rec._last_model_use = 1000.0
        self.assertFalse(rec.maybe_unload_idle(600, now=1300.0))        # 5 min: too soon
        rec.recording = True
        self.assertFalse(rec.maybe_unload_idle(600, now=99999.0))
        rec.recording = False
        busy = threading.Event()
        t = threading.Thread(target=busy.wait, daemon=True)
        t.start()
        rec._chunk_threads = [t]
        self.assertFalse(rec.maybe_unload_idle(600, now=99999.0))
        busy.set()
        t.join()
        self.assertFalse(rec.maybe_unload_idle(0, now=99999.0))          # 0 = keep
        self.assertTrue(rec.maybe_unload_idle(600, now=99999.0))

    def test_the_sweep_never_waits_on_a_load_in_progress(self):
        rec = _recorder()
        rec._model, rec._model_name = _FakeWhisper(), "base"
        with rec._model_lock:                  # a 4-9 s load holds it
            t0 = time.monotonic()
            self.assertFalse(rec.maybe_unload_idle(1, now=99999.0))
            self.assertLess(time.monotonic() - t0, 0.05)
        self.assertIsNotNone(rec._model)

    def test_a_recording_counts_as_use(self):
        # The real start stamps the use before anything can fail...
        self.assertIn("_last_model_use", main.AudioRecorder._start_recording_impl.__code__.co_names)
        rec = _recorder()
        before = time.monotonic()

        def impl(self, *a, **k):
            self.recording = True
            self._last_model_use = time.monotonic()
        with mock.patch.object(main.AudioRecorder, "_start_recording_impl", impl):
            rec.start_recording()
        self.assertGreaterEqual(rec._last_model_use, before)
        self.assertTrue(rec.recording)

    def test_a_device_that_wont_open_is_not_a_recording(self):
        rec = _recorder()
        rec._model, rec._model_name = _FakeWhisper(), "base"

        def impl(self, *a, **k):
            self.recording = True
            raise OSError(-9996, "Invalid input device (no default output device)")
        with mock.patch.object(main.AudioRecorder, "_start_recording_impl", impl):
            with self.assertRaises(OSError):
                rec.start_recording()
        self.assertFalse(rec.recording)
        # ...so the idle sweep can still free the speech model later.
        self.assertTrue(rec.maybe_unload_idle(600, now=time.monotonic() + 3600))

    def _transcribe_during_a_build(self, backend):
        me = SimpleNamespace(_chunk_threads=[], _abort=False, _record_error="",
                             _chunk_lock=threading.Lock(), _chunk_errors=[], _chunk_frames=[],
                             _chunk_idx=0, _chunk_results={}, on_finalising=None,
                             on_lang_detected=None, partial_text="", _building_model=True)

        def build():                                    # a 0.3 s "cold load"
            time.sleep(0.3)
            me._building_model = False
        threading.Thread(target=build, daemon=True).start()
        t0 = time.monotonic()
        with mock.patch.object(main.np, "frombuffer", return_value=SimpleNamespace(copy=lambda: [])), \
                mock.patch.dict(main.cfg, {"backend": backend, "mistral_api_key": "k"}):
            main.AudioRecorder.transcribe(me)
        return time.monotonic() - t0, me

    def test_a_cold_load_does_not_eat_the_chunks_time_budget(self):
        took, me = self._transcribe_during_a_build("local")
        self.assertGreaterEqual(took, 0.25)                     # waited for the build first
        self.assertGreaterEqual(me._last_load_wait, 0.25)       # and knows it (GPU offer)

    def test_a_cloud_dictation_never_waits_for_a_local_build(self):
        took, _ = self._transcribe_during_a_build("mistral")
        self.assertLess(took, 0.2)

    def test_a_gpu_failure_retries_every_chunk_on_the_cpu(self):
        rec, builds = _recorder(), []

        def load(name):
            if rec._model is None:
                dev = ("cpu" if rec.__dict__.get("_cuda_usable") is False
                       or name in main.AudioRecorder._oom_models(rec) else "cuda")
                rec._model, rec._model_name = SimpleNamespace(device=dev), name
                builds.append(dev)
        rec._load_locked = load
        all_on_gpu = threading.Barrier(3, timeout=3)

        def run_with(self, model, audio):
            if model.device == "cuda":
                all_on_gpu.wait()                       # all three hold the broken GPU model
                raise RuntimeError("CUDA failed with error unspecified launch failure")
            return "text", "en"
        results, errors = [], []

        def chunk():
            try:
                results.append(main.AudioRecorder._run_local_once(rec, None))
            except Exception as e:
                errors.append(e)
        with mock.patch.object(main.AudioRecorder, "_run_local_with", run_with), \
                mock.patch.dict(main.cfg, {"whisper_model": "base"}):
            threads = [threading.Thread(target=chunk) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(results, [("text", "en")] * 3)
        self.assertEqual(builds, ["cuda", "cpu"])
        self.assertIs(rec._cuda_usable, False)

    def test_a_cloud_backend_running_locally_is_remembered(self):
        for backend, remembered in (("managed", True), ("local", False)):
            rec = _recorder()
            with mock.patch.object(main.AudioRecorder, "_run_local_once", lambda self, a: ("t", "en")), \
                    mock.patch.dict(main.cfg, {"backend": backend, "sample_rate": 16000}):
                main.AudioRecorder._run_local(rec, [0.0] * 16000)
            self.assertEqual(hasattr(rec, "_local_fallback_at"), remembered, backend)

    def test_a_local_fallback_that_failed_is_not_remembered(self):
        rec = _recorder()

        def broken(self, a):
            raise RuntimeError("model not cached, offline")
        with mock.patch.object(main.AudioRecorder, "_run_local_once", broken), \
                mock.patch.dict(main.cfg, {"backend": "google", "sample_rate": 16000}):
            with self.assertRaises(RuntimeError):
                main.AudioRecorder._run_local(rec, [0.0] * 16000)
        self.assertFalse(hasattr(rec, "_local_fallback_at"))      # no early loads (downloads) for it

    def test_the_gpu_retry_lets_the_broken_gpu_model_go(self):
        rec, refs, alive_during_retry = _recorder(), [], []

        class M:
            def __init__(self, device):
                self.device = device

        def load(name):
            if rec._model is None:
                dev = ("cpu" if rec.__dict__.get("_cuda_usable") is False
                       or name in main.AudioRecorder._oom_models(rec) else "cuda")
                rec._model, rec._model_name = M(dev), name
                refs.append(weakref.ref(rec._model))
        rec._load_locked = load

        def run_with(self, model, audio):
            if model.device == "cuda":
                raise RuntimeError("CUDA failed with error out of memory")
            alive_during_retry.append(refs[0]() is not None)
            return "ok", "en"
        gc.disable()
        try:
            with mock.patch.object(main.AudioRecorder, "_run_local_with", run_with), \
                    mock.patch.object(main.logger, "warning", lambda *a, **k: None), \
                    mock.patch.dict(main.cfg, {"whisper_model": "base"}):
                self.assertEqual(main.AudioRecorder._run_local_once(rec, None), ("ok", "en"))
        finally:
            gc.enable()
        self.assertEqual(alive_during_retry, [False])             # freed before the CPU model ran


class _WM:
    """faster_whisper.WhisperModel stand-in: CUDA builds but can't compute
    (cublas64_12.dll missing), like the packaged app on an NVIDIA PC."""
    alive = []

    def __init__(self, name, device="cpu", **kw):
        self.name, self.device = name, device
        self.others_alive = [r() is not None for r in _WM.alive]
        _WM.alive.append(weakref.ref(self))

    def transcribe(self, *a, **k):
        if self.device == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return iter([]), None


class TestWhisperLoadFrees(unittest.TestCase):
    def setUp(self):
        _WM.alive = []
        self.rec = _recorder()
        self.rec._add_cuda_dll_dirs = lambda: None

    def _load(self, name, device=("cpu", "int8")):
        self.rec._whisper_device = lambda: device
        # Plain functions, not MagicMocks: a mock (or pytest's log capture)
        # records the CUDA exception, whose traceback would keep the models
        # alive - in the test, not in the app.
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=_WM)}), \
                mock.patch.object(main, "_looks_like_whisper_cache_error", lambda e: False), \
                mock.patch.object(main.logger, "warning", lambda *a, **k: None):
            self.rec.load_model(name)

    def test_a_model_switch_frees_the_old_model_first(self):
        self._load("large-v3-turbo")
        gc.disable()
        try:
            self._load("small")                # e.g. the file tab's pick
        finally:
            gc.enable()
        new = self.rec._model
        self.assertEqual(new.name, "small")
        self.assertEqual(new.others_alive, [False])   # never both resident

    def test_a_failed_cuda_attempt_does_not_keep_the_models_alive(self):
        self._load("large-v3-turbo", device=("cuda", "int8_float16"))
        self.assertEqual(self.rec._model.device, "cpu")
        refs = list(_WM.alive)
        self.assertEqual(len(refs), 2)         # the broken CUDA one + the CPU one
        gc.disable()                           # freed by refcount alone - no cycle
        try:
            self.rec.unload_model()
            self.assertEqual([r() for r in refs], [None, None])
        finally:
            gc.enable()


class TestOpenBlas(unittest.TestCase):
    def test_openblas_threads_are_capped_before_numpy_is_imported(self):
        src = Path(main.__file__).read_text(encoding="utf-8")
        self.assertLess(src.index('os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")'),
                        src.index("import numpy"))


# ── local AI model (llama.cpp) ─────────────────────────────────────────────

class FakeLlama:
    made = []
    closes = 0
    gate = None

    def __init__(self, model_path=None, **kw):
        self.path = model_path
        FakeLlama.made.append(weakref.ref(self))

    def close(self):                           # must never be called by an unload
        FakeLlama.closes += 1

    def tokenize(self, b, add_bos=False, special=False):
        return list(range(len(b) // 4 + 1))

    def create_chat_completion(self, messages=None, stream=False, **kw):
        if FakeLlama.gate is not None:
            FakeLlama.gate.wait(5)
        if stream:
            return iter([{"choices": [{"delta": {"content": "hi"}}]}])
        return {"choices": [{"message": {"content": "ok"}}]}


class TestLocalModelLeases(unittest.TestCase):
    def setUp(self):
        FakeLlama.made, FakeLlama.closes, FakeLlama.gate = [], 0, None
        self._clear()
        # A throwaway data dir: nothing here can touch the real model files.
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.patches = [
            mock.patch.object(local_llm.storage, "path_for", lambda name: data / name),
            mock.patch.dict(sys.modules, {"llama_cpp": SimpleNamespace(Llama=FakeLlama)}),
            mock.patch.object(local_llm, "model_downloaded", return_value=True),
            mock.patch.object(local_llm, "model_path", lambda m="x": data / f"{m}.gguf"),
            mock.patch.object(local_llm, "_has_cuda", return_value=False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        FakeLlama.gate = None
        for p in self.patches:
            p.stop()
        self._clear()
        self._tmp.cleanup()

    @staticmethod
    def _clear():
        local_llm._llms.clear()
        local_llm._in_use.clear()
        local_llm._last_used.clear()
        local_llm._removing.clear()
        local_llm._load_failures.clear()

    def test_a_running_generation_is_never_freed_and_is_freed_after(self):
        FakeLlama.gate = threading.Event()
        out = []
        t = threading.Thread(target=lambda: out.append(local_llm.run_action(
            "please summarise this", "summarize", model_id="qwen_3b")), daemon=True)
        t.start()
        deadline = time.monotonic() + 3
        while not local_llm._in_use.get("qwen_3b") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(local_llm._in_use.get("qwen_3b"), 1)
        self.assertEqual(local_llm.unload_idle(0), [])          # mid-generation: kept
        FakeLlama.gate.set()
        t.join(5)
        self.assertEqual(out, ["ok"])
        ref = FakeLlama.made[-1]
        self.assertEqual(local_llm.unload_idle(0), ["qwen_3b"])
        self.assertIsNone(ref())                                # memory really freed
        self.assertEqual(FakeLlama.closes, 0)                   # never close() under a run

    def test_a_failing_stream_callback_still_ends_the_lease(self):
        def boom(_delta):
            raise RuntimeError("UI gone")
        with self.assertRaises(RuntimeError):
            local_llm.run_action_stream("hello", "live_assist", boom, model_id="qwen_3b")
        self.assertEqual(local_llm._in_use.get("qwen_3b"), 0)

    def test_idle_time_and_keep_are_respected(self):
        with local_llm._lease("qwen_3b"):
            pass
        last = local_llm._last_used["qwen_3b"]
        self.assertEqual(local_llm.unload_idle(300, now=last + 60), [])     # 1 min: kept
        self.assertEqual(local_llm.unload_idle(0, keep="qwen_3b"), [])      # dictation needs it
        self.assertEqual(local_llm.unload_idle(300, now=last + 301), ["qwen_3b"])

    def test_the_sweep_never_waits_on_a_load_in_progress(self):
        with local_llm._lease("qwen_3b"):
            pass
        with local_llm._llm_lock:
            t0 = time.monotonic()
            self.assertEqual(local_llm.unload_idle(0), [])
            self.assertLess(time.monotonic() - t0, 0.05)

    def test_loading_another_model_evicts_an_idle_one_but_not_a_busy_one(self):
        with local_llm._lease("qwen_3b"):
            pass
        old = FakeLlama.made[-1]
        with local_llm._lease("gemma_2b"):
            pass
        self.assertNotIn("qwen_3b", local_llm._llms)
        self.assertIsNone(old())
        with local_llm._lease("gemma_2b"):
            local_llm._load_model("qwen_3b")                    # gemma is in use
            self.assertIn("gemma_2b", local_llm._llms)

    def test_removing_a_model_in_use_is_refused(self):
        with mock.patch.object(local_llm.shutil, "rmtree") as rmtree:
            with local_llm._lease("qwen_3b"):
                with self.assertRaises(local_llm.LocalLLMError):
                    local_llm.remove_model("qwen_3b")
                self.assertIn("qwen_3b", local_llm._llms)
        rmtree.assert_not_called()

    def test_no_new_use_can_start_while_a_model_is_being_removed(self):
        seen = []

        def rmtree(path, *a, **k):
            # Mid-removal, a meeting summary tries to start on it.
            try:
                local_llm._load_model("qwen_3b", lease=True)
                seen.append("loaded")
            except local_llm.LocalLLMError:
                seen.append("refused")
        with mock.patch.object(local_llm, "model_dir",
                               lambda m: SimpleNamespace(exists=lambda: True)), \
                mock.patch.object(local_llm, "partial_path",
                                  lambda m: SimpleNamespace(exists=lambda: False)), \
                mock.patch.object(local_llm.shutil, "rmtree", rmtree):
            local_llm.remove_model("qwen_3b")
        self.assertEqual(seen, ["refused"])
        self.assertNotIn("qwen_3b", local_llm._removing)

    def test_a_model_that_wont_load_is_reported_as_a_local_model_error(self):
        def broken(*a, **k):
            raise ValueError("Failed to create llama_context")
        with mock.patch.dict(sys.modules, {"llama_cpp": SimpleNamespace(Llama=broken)}):
            with self.assertRaises(local_llm.LocalLLMError) as cm:
                local_llm._load_model("qwen_3b")
        self.assertIn("llama_context", str(cm.exception))

    def test_a_local_live_session_loads_its_model_at_start_even_in_privacy_mode(self):
        with mock.patch.object(actions, "normalize_action_model", lambda m: m), \
                mock.patch.dict(actions.ACTION_MODELS, {"qwen_3b": {"kind": "local_llm"}}):
            actions.warm_up("qwen_3b", {"privacy_mode": True})
        self.assertIn("qwen_3b", local_llm._llms)
        self.assertEqual(local_llm._in_use.get("qwen_3b", 0), 0)     # loaded, not leased

    def test_meeting_notes_say_when_the_model_wont_load(self):
        def broken(*a, **k):
            raise ValueError("Failed to create llama_context")
        with mock.patch.dict(sys.modules, {"llama_cpp": SimpleNamespace(Llama=broken)}), \
                mock.patch.object(actions, "normalize_action_model", lambda m: m), \
                mock.patch.dict(actions.ACTION_MODELS, {"qwen_3b": {"kind": "local_llm"}}):
            with self.assertRaises(actions.ActionError) as cm:
                actions.process("We agreed to ship on Friday.", actions.ACTION_MEETING_NOTES,
                                model="qwen_3b")
        self.assertIn("llama_context", str(cm.exception))         # not quietly extractive


# ── the app's sweep ────────────────────────────────────────────────────────

class _Inline:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self.target = target

    def start(self):
        self.target()


class TestAppSweep(unittest.TestCase):
    def _app(self, state="idle", **kw):
        mw = SimpleNamespace(state=state, STATE_RECORDING="recording",
                             STATE_PROCESSING="processing")
        return SimpleNamespace(meetings_win=mw, is_rec=False, _busy=False,
                               _file_job_running=False, recorder=mock.MagicMock(),
                               cfg=main.cfg, **kw)

    def _sweep(self, app, whisper_min=10, llm_min=5):
        with mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch.object(main.local_llm, "unload_idle") as llm, \
                mock.patch.dict(main.cfg, {"whisper_idle_unload_min": whisper_min,
                                           "llm_idle_unload_min": llm_min}):
            main.AppController._idle_sweep(app)
        return llm

    def test_idle_models_are_swept_with_the_configured_minutes(self):
        app = self._app()
        llm = self._sweep(app)
        llm.assert_called_once_with(300.0)
        app.recorder.maybe_unload_idle.assert_called_once_with(600.0)

    def test_nothing_is_swept_during_a_session(self):
        for state, flags in (("recording", {}), ("processing", {}), ("idle", {"is_rec": True}),
                             ("idle", {"_busy": True}), ("idle", {"_file_job_running": True})):
            app = self._app(state)
            for k, v in flags.items():
                setattr(app, k, v)
            llm = self._sweep(app)
            llm.assert_not_called()
            app.recorder.maybe_unload_idle.assert_not_called()

    def test_zero_minutes_keeps_a_model_loaded(self):
        app = self._app()
        llm = self._sweep(app, whisper_min=0, llm_min=0)
        llm.assert_not_called()
        app.recorder.maybe_unload_idle.assert_not_called()

    def test_after_a_session_the_ai_model_goes_unless_dictation_uses_it(self):
        for mode, keep in (("transcribe_only", None), ("smart_auto", "qwen_3b"),
                           ("write_email", "qwen_3b")):
            app = SimpleNamespace(cfg={"output_action": mode, "action_model": "qwen_3b"})
            with mock.patch.object(main.threading, "Thread", _Inline), \
                    mock.patch.object(main.local_llm, "unload_idle") as llm, \
                    mock.patch.dict(main.cfg, {"llm_idle_unload_min": 5}):
                main.AppController.release_models_after_session(app)
            llm.assert_called_once_with(0, keep=keep)

    def test_meetings_prewarm_the_speech_model_and_free_the_ai_model(self):
        src = Path(main.__file__).with_name("ui").joinpath("meetings.py").read_text(encoding="utf-8")
        start = src.index("self.app.recorder.start_recording(capture_mode=meeting_mode")
        self.assertIn("prewarm_speech_model", src[start:start + 600])
        done = src.index("def _on_processing_finished")
        self.assertIn("release_models_after_session", src[done:done + 400])


class TestGpuFailureKinds(unittest.TestCase):
    """Only the GPU stack's own errors switch the GPU off, and out of memory
    only for the model that didn't fit."""

    def _rec(self, fail_with):
        rec, builds = _recorder(), []

        def load(name):
            if rec._model is None or rec._model_name != name:
                dev = ("cpu" if rec.__dict__.get("_cuda_usable") is False
                       or name in main.AudioRecorder._oom_models(rec) else "cuda")
                rec._model, rec._model_name = SimpleNamespace(device=dev, name=name), name
                builds.append((name, dev))
        rec._load_locked = load
        reported = []
        rec.on_cuda_failed = reported.append

        def run_with(self, model, audio):
            if model.device == "cuda" and model.name == "large-v3":
                raise RuntimeError(fail_with)
            return model.device, "en"
        return rec, builds, reported, run_with

    def _dictate(self, rec, run_with, name):
        with mock.patch.object(main.AudioRecorder, "_run_local_with", run_with), \
                mock.patch.object(main.logger, "warning", lambda *a, **k: None), \
                mock.patch.dict(main.cfg, {"whisper_model": name}):
            return main.AudioRecorder._run_local_once(rec, None)

    def test_out_of_memory_moves_only_that_model_to_the_cpu(self):
        rec, builds, reported, run_with = self._rec("CUDA failed with error out of memory")
        self.assertEqual(self._dictate(rec, run_with, "large-v3"), ("cpu", "en"))
        self.assertIsNot(rec.__dict__.get("_cuda_usable"), False)     # the GPU is still on
        self.assertEqual(reported, [])                                 # not a GPU failure
        self.assertEqual(self._dictate(rec, run_with, "small"), ("cuda", "en"))

    def test_a_driver_error_switches_the_gpu_off_and_is_reported(self):
        rec, builds, reported, run_with = self._rec("CUDA driver version is insufficient")
        self.assertEqual(self._dictate(rec, run_with, "large-v3"), ("cpu", "en"))
        self.assertIs(rec._cuda_usable, False)
        self.assertEqual(len(reported), 1)

    def test_a_bug_on_the_gpu_is_not_blamed_on_the_gpu(self):
        rec, builds, reported, run_with = self._rec("KeyError: 'segments'")
        with self.assertRaises(RuntimeError):
            self._dictate(rec, run_with, "large-v3")
        self.assertIsNot(rec.__dict__.get("_cuda_usable"), False)
        self.assertEqual(reported, [])

    def test_a_file_job_retries_on_the_cpu_instead_of_no_speech(self):
        rec, builds, reported, _ = self._rec("CUDA failed with error unspecified launch failure")

        def segments(self, model, audio, sr, language, on_progress, should_cancel, gpu_errors=False):
            if model.device == "cuda":
                try:
                    raise RuntimeError("CUDA failed with error unspecified launch failure")
                except RuntimeError as e:
                    if gpu_errors:
                        raise main._GpuDecodeError(str(e))
                    return []
            return [{"start": 0.0, "end": 1.0, "text": "hello"}]
        # (other test modules may stub numpy inside main)
        np_stub = SimpleNamespace(asarray=lambda a, dtype=None: a, float32=None)
        with mock.patch.object(main.AudioRecorder, "_segments_with", segments), \
                mock.patch.object(main, "np", np_stub), \
                mock.patch.object(main.logger, "warning", lambda *a, **k: None), \
                mock.patch.dict(main.cfg, {"whisper_model": "large-v3", "sample_rate": 16000}):
            out = main.AudioRecorder.transcribe_segments(rec, [0.0] * 16000, model_name="large-v3")
        self.assertEqual(out, [{"start": 0.0, "end": 1.0, "text": "hello"}])
        self.assertEqual(builds, [("large-v3", "cuda"), ("large-v3", "cpu")])
        self.assertEqual(len(reported), 1)

    def test_a_damaged_cache_during_the_gpu_attempt_never_switches_the_gpu_off(self):
        class WM:
            def __init__(self, name, device="cpu", local_files_only=True, **kw):
                if device == "cuda":
                    raise RuntimeError("Unable to open file 'model.bin' in model '...'")
                self.device = device
        rec = _recorder()
        rec._add_cuda_dll_dirs = lambda: None
        rec._whisper_device = lambda: ("cuda", "int8_float16")
        reported = []
        rec.on_cuda_failed = reported.append
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=WM)}), \
                mock.patch.object(main, "_looks_like_whisper_cache_error", lambda e: False), \
                mock.patch.object(main.logger, "warning", lambda *a, **k: None):
            rec.load_model("small")
        self.assertEqual(rec._model.device, "cpu")
        self.assertIsNot(rec.__dict__.get("_cuda_usable"), False)
        self.assertEqual(reported, [])

    def _real_load(self, rec, wm, name):
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=wm)}), \
                mock.patch.object(main, "_looks_like_whisper_cache_error", lambda e: False), \
                mock.patch.object(main.logger, "warning", lambda *a, **k: None):
            rec.load_model(name)

    def test_out_of_memory_while_building_keeps_the_gpu_for_smaller_models(self):
        class WM:                                   # the real load path, warm-up included
            def __init__(self, name, device="cpu", **kw):
                self.name, self.device = name, device

            def transcribe(self, *a, **k):
                if self.device == "cuda" and self.name == "large-v3":
                    raise RuntimeError("CUDA failed with error out of memory")
                return iter([]), None
        rec = _recorder()
        rec._add_cuda_dll_dirs = lambda: None
        rec._whisper_device = lambda: ("cuda", "int8_float16")
        reported = []
        rec.on_cuda_failed = reported.append
        self._real_load(rec, WM, "large-v3")
        self.assertEqual(rec._model.device, "cpu")              # too big for the card
        self.assertIsNot(rec.__dict__.get("_cuda_usable"), False)
        self.assertEqual(reported, [])
        self._real_load(rec, WM, "small")
        self.assertEqual(rec._model.device, "cuda")             # fits: still on the GPU
        self._real_load(rec, WM, "large-v3")
        self.assertEqual(rec._model.device, "cpu")              # no second doomed attempt

    def test_a_file_job_gpu_failure_goes_through_the_real_decode_path(self):
        class Seg:
            start, end, text = 0.0, 1.0, "hello"

        class Model:
            def __init__(self, device):
                self.device = device

            def transcribe(self, *a, **k):
                if self.device == "cuda":
                    raise RuntimeError("CUDA failed with error unspecified launch failure")
                return iter([Seg()]), None
        rec, builds, reported = _recorder(), [], []
        rec.on_cuda_failed = reported.append
        rec._lang_setting = lambda: "en"

        def load(name):
            if rec._model is None or rec._model_name != name:
                dev = "cpu" if rec.__dict__.get("_cuda_usable") is False else "cuda"
                rec._model, rec._model_name = Model(dev), name
                builds.append(dev)
        rec._load_locked = load
        np_stub = SimpleNamespace(asarray=lambda a, dtype=None: a, float32=None)
        with mock.patch.object(main, "np", np_stub), \
                mock.patch.object(main.logger, "warning", lambda *a, **k: None), \
                mock.patch.object(main.vocabulary, "load_terms", lambda c: []), \
                mock.patch.object(main.vocabulary, "correct_spellings", lambda t, terms: t), \
                mock.patch.dict(main.cfg, {"whisper_model": "large-v3", "sample_rate": 16000}):
            out = main.AudioRecorder.transcribe_segments(rec, [0.0] * 16000, model_name="large-v3")
        self.assertEqual(out, [{"start": 0.0, "end": 1.0, "text": "hello"}])
        self.assertEqual(builds, ["cuda", "cpu"])
        self.assertEqual(len(reported), 1)

    def test_a_cloud_backend_that_fell_back_once_does_not_wait_for_local(self):
        rec = _recorder()
        rec._local_fallback_at = time.monotonic()
        with mock.patch.dict(main.cfg, {"backend": "managed"}):
            self.assertFalse(main.AudioRecorder._runs_locally(rec))
        with mock.patch.dict(main.cfg, {"backend": "google", "google_api_key": ""}):
            self.assertTrue(main.AudioRecorder._runs_locally(rec))     # no key = always local


class TestLoadFailureMemory(unittest.TestCase):
    def setUp(self):
        local_llm._load_failures.clear()
        local_llm._llms.clear()
        self.built = []
        built = self.built

        def broken(*a, **k):
            built.append(1)
            raise ValueError("Failed to create llama_context")
        self.patches = [
            mock.patch.dict(sys.modules, {"llama_cpp": SimpleNamespace(Llama=broken)}),
            mock.patch.object(local_llm, "model_downloaded", return_value=True),
            mock.patch.object(local_llm, "model_path", lambda m="x": Path(f"/nowhere/{m}.gguf")),
            mock.patch.object(local_llm, "_has_cuda", return_value=False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        local_llm._load_failures.clear()

    def test_a_model_that_wont_load_is_not_rebuilt_every_recap(self):
        for _ in range(3):                                     # three live-recap ticks
            with self.assertRaises(local_llm.LocalModelLoadError):
                local_llm._load_model("qwen_3b")
        self.assertEqual(len(self.built), 1)                   # built once, then refused
        local_llm.forget_load_failures()                       # the user pressed Retry
        with self.assertRaises(local_llm.LocalModelLoadError):
            local_llm._load_model("qwen_3b")
        self.assertEqual(len(self.built), 2)


if __name__ == "__main__":
    unittest.main()
