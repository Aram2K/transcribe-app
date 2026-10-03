"""Optional NVIDIA GPU acceleration (gpu_accel.py + its app/Settings wiring).
Nothing real is downloaded: a fake HTTP response serves a small fake wheel,
and every file goes to a throwaway folder (never the real app data)."""
import errno
import hashlib
import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Never the real settings, keyring or app data (main loads config at import).
os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="transcribe-test-data-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")

import gpu_accel
import main


def _fake_wheel(include=gpu_accel.DLLS):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for dll in include:
            z.writestr(f"nvidia/cublas/bin/{dll}", b"MZ" + dll.encode() * 1000)
        z.writestr("nvidia/cublas/bin/nvblas64_12.dll", b"MZnot needed")
    return buf.getvalue()


class _Resp:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, dict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.data), 4096):
            yield self.data[i:i + 4096]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.data = data
        self.wheel = _fake_wheel()
        self.patches = [
            mock.patch.object(gpu_accel, "_base_dir", lambda: data / "gpu"),
            mock.patch.object(gpu_accel, "WHEEL_SHA256", hashlib.sha256(self.wheel).hexdigest()),
            mock.patch.object(gpu_accel, "WHEEL_SIZE", len(self.wheel)),
            mock.patch.object(gpu_accel, "_pip_dirs", lambda: []),
            mock.patch.object(gpu_accel, "_path_dirs", lambda: []),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self.patches:
            p.start()
        for k in [k for k in os.environ if k.upper().startswith("CUDA_PATH")]:
            os.environ.pop(k)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def get_serving(self, data=None, honour_range=True, fail_at=None, html=False):
        payload = self.wheel if data is None else data
        calls = []

        def get(url, headers=None, **kw):
            calls.append(dict(headers or {}))
            if html:                                     # a Wi-Fi sign-in page
                return _Resp(b"<html>sign in</html>", 200, {"Content-Type": "text/html"})
            rng = (headers or {}).get("Range")
            if rng and honour_range:
                start = int(rng.split("=")[1].rstrip("-"))
                if start >= len(payload):
                    return _Resp(b"", 416)
                body = payload[start:fail_at] if fail_at else payload[start:]
                return _Resp(body, 206, {"Content-Range":
                                         f"bytes {start}-{len(payload) - 1}/{len(payload)}"})
            body = payload[:fail_at] if fail_at else payload
            return _Resp(body, 200, {"Content-Length": str(len(payload))})
        return get, calls

    def _install(self):
        d = gpu_accel.install_dir()
        d.mkdir(parents=True, exist_ok=True)
        for dll in gpu_accel.DLLS:
            (d / dll).write_bytes(b"MZ")
        return d


# ── download ───────────────────────────────────────────────────────────────

class TestDownload(_Base):
    def test_downloads_verifies_and_unpacks_only_the_two_dlls(self):
        get, calls = self.get_serving()
        progress = []
        dest = gpu_accel.download(on_progress=lambda *a: progress.append(a), _get=get)
        self.assertEqual(sorted(os.listdir(dest)), sorted(gpu_accel.DLLS))
        self.assertFalse(gpu_accel._part_path().exists())          # the wheel is gone
        self.assertEqual(progress[-1][0], 100)
        self.assertTrue(all(0 <= p[0] <= 100 for p in progress))
        self.assertEqual(gpu_accel.libs_dir(), str(dest))
        self.assertEqual(calls[0].get("Accept-Encoding"), "identity")   # byte offsets stay true

    def test_resumes_a_partial_download(self):
        part = gpu_accel._part_path()
        part.parent.mkdir(parents=True)
        part.write_bytes(self.wheel[:5000])
        get, calls = self.get_serving()
        gpu_accel.download(_get=get)
        self.assertEqual(calls[0].get("Range"), "bytes=5000-")
        self.assertTrue(gpu_accel._has_dlls(str(gpu_accel.install_dir())))

    def test_a_server_ignoring_resume_starts_over_cleanly(self):
        part = gpu_accel._part_path()
        part.parent.mkdir(parents=True)
        part.write_bytes(b"x" * 5000)                               # junk from before
        get, _ = self.get_serving(honour_range=False)
        gpu_accel.download(_get=get)
        self.assertTrue(gpu_accel._has_dlls(str(gpu_accel.install_dir())))

    def test_a_wifi_sign_in_page_never_overwrites_the_partial(self):
        part = gpu_accel._part_path()
        part.parent.mkdir(parents=True)
        part.write_bytes(self.wheel[:5000])
        get, _ = self.get_serving(html=True)
        with self.assertRaises(gpu_accel.GpuAccelError) as cm:
            gpu_accel.download(_get=get)
        self.assertIn("Wi-Fi", str(cm.exception))
        self.assertEqual(part.read_bytes(), self.wheel[:5000])        # kept for the resume

    def test_a_range_answer_at_the_wrong_offset_never_appends(self):
        part = gpu_accel._part_path()
        part.parent.mkdir(parents=True)
        part.write_bytes(self.wheel[:5000])

        def get(url, headers=None, **kw):
            return _Resp(self.wheel[100:], 206, {"Content-Range": f"bytes 100-{len(self.wheel) - 1}/1"})
        with self.assertRaises(gpu_accel.GpuAccelError):
            gpu_accel.download(_get=get)
        self.assertEqual(part.read_bytes(), self.wheel[:5000])

    def test_a_download_that_stops_early_is_kept_to_resume(self):
        get, _ = self.get_serving(fail_at=7000)
        with self.assertRaises(gpu_accel.GpuAccelError) as cm:
            gpu_accel.download(_get=get)
        self.assertIn("picks up where it stopped", str(cm.exception))
        self.assertEqual(gpu_accel._part_path().stat().st_size, 7000)

    def test_a_damaged_download_is_deleted_and_reported(self):
        bad = bytearray(self.wheel)
        bad[100] ^= 0xFF
        get, _ = self.get_serving(data=bytes(bad))
        with self.assertRaises(gpu_accel.GpuAccelError) as cm:
            gpu_accel.download(_get=get)
        self.assertIn("damaged", str(cm.exception))
        self.assertFalse(gpu_accel._part_path().exists())
        self.assertIsNone(gpu_accel.libs_dir())

    def test_a_package_without_the_dlls_installs_nothing(self):
        wheel = _fake_wheel(include=("cublas64_12.dll",))
        with mock.patch.object(gpu_accel, "WHEEL_SHA256", hashlib.sha256(wheel).hexdigest()), \
                mock.patch.object(gpu_accel, "WHEEL_SIZE", len(wheel)):
            get, _ = self.get_serving(data=wheel)
            with self.assertRaises(gpu_accel.GpuAccelError):
                gpu_accel.download(_get=get)
        self.assertFalse(gpu_accel.install_dir().exists())
        self.assertFalse(gpu_accel.install_dir().with_name("cuda12.staging").exists())

    def test_cancel_during_and_after_the_transfer(self):
        get, _ = self.get_serving()
        with self.assertRaises(gpu_accel.GpuAccelError) as cm:
            gpu_accel.download(_get=get, should_cancel=lambda: True)
        self.assertIn("cancelled", str(cm.exception))
        self.assertIsNone(gpu_accel.libs_dir())
        # Cancel clicked after the last byte arrived: still cancelled, not installed.
        polls, chunks = [], -(-len(self.wheel) // 4096)

        def late():
            polls.append(1)
            return len(polls) > chunks                 # false for every chunk, true after
        get2, _ = self.get_serving()
        with self.assertRaises(gpu_accel.GpuAccelError):
            gpu_accel.download(_get=get2, should_cancel=late)
        self.assertIsNone(gpu_accel.libs_dir())

    def test_not_enough_disk_space_is_said_plainly(self):
        get, calls = self.get_serving()
        with mock.patch.object(gpu_accel.shutil, "disk_usage",
                               return_value=SimpleNamespace(free=10 * 2**20)):
            with self.assertRaises(gpu_accel.GpuAccelError) as cm:
                gpu_accel.download(_get=get)
        self.assertIn("disk space", str(cm.exception))
        self.assertEqual(calls, [])                                 # nothing downloaded

    def test_a_disk_filling_up_mid_download_is_not_called_a_network_problem(self):
        get, _ = self.get_serving()
        real_open = Path.open

        def full(self, mode="r", *a, **k):
            f = real_open(self, mode, *a, **k)
            if "a" in mode or "w" in mode:
                def write(_b):
                    raise OSError(errno.ENOSPC, "No space left on device")
                f.write = write
            return f
        with mock.patch.object(Path, "open", full):
            with self.assertRaises(gpu_accel.GpuAccelError) as cm:
                gpu_accel.download(_get=get)
        self.assertIn("disk space", str(cm.exception))

    def test_a_dropped_connection_is_a_friendly_error(self):
        def get(*a, **k):
            raise ConnectionError("reset by peer")
        with self.assertRaises(gpu_accel.GpuAccelError) as cm:
            gpu_accel.download(_get=get)
        self.assertIn("picks up where it stopped", str(cm.exception))

    def test_files_in_use_are_reported_as_such(self):
        self._install()
        get, _ = self.get_serving()
        real_rmtree = gpu_accel.shutil.rmtree

        def locked(path, *a, **k):
            if Path(path) == gpu_accel.install_dir():
                raise PermissionError("in use")
            return real_rmtree(path, *a, **k)
        with mock.patch.object(gpu_accel.shutil, "rmtree", locked):
            with self.assertRaises(gpu_accel.GpuAccelError) as cm:
                gpu_accel.download(_get=get)
        self.assertIn("Restart Transcribe", str(cm.exception))
        self.assertTrue(gpu_accel._has_dlls(str(gpu_accel.install_dir())))   # untouched


# ── detection / status / removal ───────────────────────────────────────────

class TestStatus(_Base):
    def _gpu(self, count=1, windows=True):
        return [mock.patch.object(gpu_accel, "_cuda_device_count", lambda: count),
                mock.patch.object(gpu_accel, "IS_WINDOWS", windows)]

    def _with(self, patches, fn):
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in patches:
                p.stop()

    def test_no_nvidia_gpu_or_not_windows_is_unsupported(self):
        self.assertEqual(self._with(self._gpu(count=0), gpu_accel.status), "unsupported")
        self.assertEqual(self._with(self._gpu(windows=False), gpu_accel.status), "unsupported")

    def test_an_nvidia_gpu_without_cublas_is_available(self):
        self.assertEqual(self._with(self._gpu(), gpu_accel.status), "available")

    def test_our_download_a_cuda_toolkit_or_path_make_it_ready(self):
        d = self._install()
        self.assertEqual(self._with(self._gpu(), gpu_accel.status), "ready")
        for dll in gpu_accel.DLLS:
            (d / dll).unlink()
        tk = self.data / "toolkit" / "bin"
        tk.mkdir(parents=True)
        for dll in gpu_accel.DLLS:
            (tk / dll).write_bytes(b"MZ")
        with mock.patch.dict(os.environ, {"CUDA_PATH_V12_4": str(tk.parent)}):
            self.assertEqual(self._with(self._gpu(), gpu_accel.status), "ready")
            self.assertEqual(gpu_accel.libs_dir(), str(tk))
        with mock.patch.object(gpu_accel, "_path_dirs", lambda: [str(tk)]):   # e.g. conda
            self.assertEqual(gpu_accel.libs_dir(), str(tk))

    def test_a_cuda_13_toolkit_is_not_cublas_12(self):
        tk = self.data / "v13" / "bin"
        tk.mkdir(parents=True)
        (tk / "cublas64_13.dll").write_bytes(b"MZ")
        (tk / "cublasLt64_13.dll").write_bytes(b"MZ")
        with mock.patch.dict(os.environ, {"CUDA_PATH": str(tk.parent)}):
            self.assertIsNone(gpu_accel.libs_dir())

    def test_a_gpu_that_could_not_run_it_is_failed(self):
        self._install()
        cfg = {gpu_accel.CFG_FAILED: "driver too old"}
        self.assertEqual(self._with(self._gpu(), lambda: gpu_accel.status(cfg)), "failed")

    def test_dll_dir_goes_on_path_once(self):
        d = self._install()
        with mock.patch.object(gpu_accel, "IS_WINDOWS", True), \
                mock.patch.object(gpu_accel.os, "add_dll_directory", lambda p: object(), create=True), \
                mock.patch.dict(os.environ, {"PATH": "C:\\Windows"}):
            self.assertEqual(gpu_accel.register_dll_dirs(), str(d))
            gpu_accel.register_dll_dirs()
            self.assertEqual(os.environ["PATH"].split(os.pathsep).count(str(d)), 1)

    def test_a_pending_removal_never_counts_and_finishes_at_startup(self):
        d = self._install()
        (d.parent / gpu_accel.REMOVE_MARKER).write_text("1")
        self.assertIsNone(gpu_accel.libs_dir())                     # going away: not "ready"
        self.assertEqual(self._with(self._gpu(), gpu_accel.status), "available")
        self.assertTrue(gpu_accel.finish_pending_remove())          # main() at startup
        self.assertFalse(d.parent.exists())

    def test_a_removal_still_locked_at_startup_waits_for_the_next_one(self):
        d = self._install()
        (d.parent / gpu_accel.REMOVE_MARKER).write_text("1")
        with mock.patch.object(gpu_accel.shutil, "rmtree", side_effect=PermissionError("in use")):
            self.assertFalse(gpu_accel.finish_pending_remove())
        self.assertTrue(gpu_accel.removal_pending())                # not forgotten

    def test_remove_now_or_at_the_next_start(self):
        d = self._install()
        self.assertTrue(gpu_accel.remove())
        self.assertFalse(d.parent.exists())
        self._install()
        with mock.patch.object(gpu_accel.shutil, "rmtree", side_effect=PermissionError("in use")):
            self.assertFalse(gpu_accel.remove())                    # the DLL is loaded
        self.assertTrue(gpu_accel.removal_pending())

    def test_a_fresh_download_cancels_an_old_pending_removal(self):
        base = gpu_accel._base_dir()
        base.mkdir(parents=True)
        (base / gpu_accel.REMOVE_MARKER).write_text("1")
        get, _ = self.get_serving()
        gpu_accel.download(_get=get)
        self.assertFalse(gpu_accel.removal_pending())

    def test_files_live_in_local_not_roaming_app_data(self):
        self.patches[0].stop()                                      # the real _base_dir
        try:
            with mock.patch.object(gpu_accel, "IS_WINDOWS", True), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local"}):
                os.environ.pop("TRANSCRIBE_APP_DATA_DIR", None)
                self.assertEqual(str(gpu_accel._base_dir()),
                                 str(Path("C:\\Users\\x\\AppData\\Local") / "Transcribe" / "gpu"))
        finally:
            self.patches[0].start()


# ── when to suggest it ─────────────────────────────────────────────────────

class TestOfferPolicy(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(gpu_accel, "status", lambda cfg=None: "available")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def cfg(self, **kw):
        return {"backend": "local", **kw}

    def test_offered_after_waiting_on_a_model_the_gpu_speeds_up(self):
        self.assertTrue(gpu_accel.should_offer_now(self.cfg(), "large-v3-turbo", 4.2))

    def test_not_for_quick_models_short_waits_or_when_already_on_the_gpu(self):
        self.assertFalse(gpu_accel.should_offer_now(self.cfg(), "base", 9.0))
        self.assertFalse(gpu_accel.should_offer_now(self.cfg(), "large-v3-turbo", 1.0))
        self.assertFalse(gpu_accel.should_offer_now(self.cfg(), "large-v3-turbo", 9.0, on_gpu=True))

    def test_never_after_dont_suggest_again(self):
        self.assertFalse(gpu_accel.should_offer_now(self.cfg(**{gpu_accel.CFG_DECLINED: True}),
                                                    "large-v3", 9.0))

    def test_cloud_dictation_no_but_a_local_file_job_yes(self):
        cloud = self.cfg(backend="managed")
        self.assertFalse(gpu_accel.should_offer_now(cloud, "large-v3", 9.0))
        self.assertTrue(gpu_accel.should_offer_now(cloud, "large-v3", 9.0, local_job=True))

    def test_rarely_and_a_few_times_in_total(self):
        cfg, now = self.cfg(), 1_000_000.0
        self.assertTrue(gpu_accel.should_offer_now(cfg, "small", 3, now=now))
        gpu_accel.note_offered(cfg, now=now)
        self.assertFalse(gpu_accel.should_offer_now(cfg, "small", 3, now=now + 3600))     # same day
        self.assertTrue(gpu_accel.should_offer_now(cfg, "small", 3,
                                                   now=now + gpu_accel.OFFER_EVERY_S + 1))
        cfg[gpu_accel.CFG_COUNT] = gpu_accel.MAX_OFFERS
        self.assertFalse(gpu_accel.should_offer_now(cfg, "small", 3, now=now + 10**9))

    def test_removing_it_counts_as_not_now(self):
        cfg = self.cfg(**{gpu_accel.CFG_LAST: time.time()})          # what Remove writes
        self.assertFalse(gpu_accel.should_offer_now(cfg, "large-v3", 9.0))

    def test_not_when_the_libraries_are_already_there(self):
        with mock.patch.object(gpu_accel, "status", lambda cfg=None: "ready"):
            self.assertFalse(gpu_accel.should_offer_now(self.cfg(), "large-v3", 9.0))

    def test_out_of_memory_is_told_apart(self):
        self.assertTrue(gpu_accel.is_oom("CUDA failed with error out of memory"))
        self.assertFalse(gpu_accel.is_oom("CUDA driver version is insufficient"))


# ── the app ────────────────────────────────────────────────────────────────

class TestDeviceChoice(unittest.TestCase):
    def _device(self, count=1, libs="C:/gpu", failed=None):
        ct2 = SimpleNamespace(get_cuda_device_count=lambda: count)
        with mock.patch.dict(sys.modules, {"ctranslate2": ct2}), \
                mock.patch.object(main.sys, "platform", "win32"), \
                mock.patch.object(main.gpu_accel, "libs_dir", lambda: libs), \
                mock.patch.dict(main.cfg, {}, clear=False):
            main.cfg.pop(gpu_accel.CFG_FAILED, None)
            if failed:
                main.cfg[gpu_accel.CFG_FAILED] = failed
            return main.AudioRecorder._whisper_device()

    def test_no_doomed_gpu_attempt_without_cublas(self):
        self.assertEqual(self._device(libs=None), ("cpu", "int8"))

    def test_the_gpu_once_cublas_is_there(self):
        self.assertEqual(self._device(), ("cuda", "int8_float16"))

    def test_cpu_after_the_gpu_proved_unusable(self):
        self.assertEqual(self._device(failed="driver too old"), ("cpu", "int8"))

    def test_cpu_without_an_nvidia_gpu(self):
        self.assertEqual(self._device(count=0), ("cpu", "int8"))


class _Inline:
    def __init__(self, target=None, daemon=None, **kw):
        self.target = target

    def start(self):
        self.target()

    def is_alive(self):
        return False


def _app(**cfg):
    sent = {}
    app = SimpleNamespace(
        cfg={"backend": "local", "whisper_model": "large-v3-turbo", **cfg},
        save_config=mock.MagicMock(), track=mock.MagicMock(),
        recorder=SimpleNamespace(_cuda_usable=None, unload_model=mock.MagicMock(),
                                 load_model=mock.MagicMock()),
        _gpu_thread=None, _gpu_phase=None, _gpu_cancel=False, _tray_click=None,
        sig_gpu_progress=SimpleNamespace(emit=mock.MagicMock()),
        sig_gpu_done=SimpleNamespace(emit=lambda s, m: sent.setdefault("done", []).append((s, m))),
        show_tray_hint=mock.MagicMock())
    for name in ("gpu_download_running", "_verify_gpu", "_can_verify_gpu", "start_gpu_download",
                 "retry_gpu", "_on_gpu_wait", "_show_gpu_offer", "_on_gpu_runtime_failed",
                 "_on_gpu_done", "_on_tray_message_clicked", "show_gpu_settings"):
        setattr(app, name, getattr(main.AppController, name).__get__(app))
    app.sent = sent
    return app


class TestAppFlow(unittest.TestCase):
    def _download(self, app, model_on_disk=True):
        with mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch.object(main.gpu_accel, "download", lambda **k: Path("x")), \
                mock.patch.object(main.gpu_accel, "register_dll_dirs", lambda: None), \
                mock.patch.object(main, "model_downloaded", return_value=model_on_disk):
            return app.start_gpu_download("settings")

    def test_download_then_the_gpu_runs(self):
        app = _app()

        def load(name):
            app.recorder._cuda_usable = True
        app.recorder.load_model.side_effect = load
        self.assertTrue(self._download(app))
        self.assertEqual(app.sent["done"], [("ok", "")])
        app.recorder.unload_model.assert_called_once()             # the next use loads on the GPU
        self.assertTrue(app.cfg[gpu_accel.CFG_INTRO])

    def test_download_but_the_driver_cannot_run_it(self):
        app = _app()

        def load(name):
            app.recorder._cuda_usable = False
            app.recorder._cuda_error = "CUDA driver version is insufficient"
        app.recorder.load_model.side_effect = load
        self._download(app)
        state, msg = app.sent["done"][0]
        self.assertEqual(state, "gpu_failed")
        self.assertIn("driver", msg)

    def test_a_model_too_big_for_the_card_is_not_a_gpu_failure(self):
        app = _app()

        def load(name):
            app.recorder._cuda_usable = False
            app.recorder._cuda_error = "CUDA failed with error out of memory"
        app.recorder.load_model.side_effect = load
        self._download(app)
        self.assertEqual(app.sent["done"][0], ("gpu_oom", "large-v3-turbo"))
        with mock.patch.object(main, "QTimer"):
            app._on_gpu_done("gpu_oom", "large-v3-turbo")
        self.assertNotIn(gpu_accel.CFG_FAILED, app.cfg)            # small models still use the GPU

    def test_without_a_local_model_to_test_with_it_says_installed_not_on(self):
        app = _app()
        self._download(app, model_on_disk=False)
        self.assertEqual(app.sent["done"], [("pending", "")])
        app.recorder.load_model.assert_not_called()                 # never a model download

    def test_try_again_never_clears_a_real_failure_untested(self):
        app = _app(**{gpu_accel.CFG_FAILED: "CUDA driver version is insufficient"})
        with mock.patch.object(main, "model_downloaded", return_value=False):
            self.assertFalse(app.retry_gpu())
        self.assertEqual(app.cfg[gpu_accel.CFG_FAILED], "CUDA driver version is insufficient")

    def test_a_gpu_failure_during_real_use_is_remembered_but_not_out_of_memory(self):
        app = _app()
        with mock.patch.object(main.gpu_accel, "libs_dir", lambda: "C:/gpu"):
            app._on_gpu_runtime_failed("CUDA failed with error out of memory")
            self.assertNotIn(gpu_accel.CFG_FAILED, app.cfg)
            app._on_gpu_runtime_failed("CUDA driver version is insufficient")
        self.assertIn("driver", app.cfg[gpu_accel.CFG_FAILED])

    def test_a_failed_or_cancelled_download(self):
        for cancel, expected in ((False, "failed"), (True, "cancelled")):
            app = _app()

            def boom(**k):
                app._gpu_cancel = cancel
                raise gpu_accel.GpuAccelError("nope")
            with mock.patch.object(main.threading, "Thread", _Inline), \
                    mock.patch.object(main.gpu_accel, "download", boom):
                app.start_gpu_download("settings")
            self.assertEqual(app.sent["done"][0][0], expected)

    def test_only_one_download_at_a_time(self):
        app = _app()
        app._gpu_thread = SimpleNamespace(is_alive=lambda: True)
        self.assertFalse(app.start_gpu_download("settings"))

    def _offer(self, app, *calls, last_hint=None):
        with mock.patch.object(main.gpu_accel, "status", lambda cfg=None: "available"), \
                mock.patch.object(main.QTimer, "singleShot", lambda ms, fn: fn()):
            if last_hint is not None:
                app._last_hint_at = last_hint
            for c in calls:
                app._on_gpu_wait(*c)

    def test_a_slow_cpu_transcription_offers_the_gpu_once(self):
        app = _app()
        self._offer(app, ("dictation", "large-v3-turbo", 6.0), ("dictation", "large-v3-turbo", 6.0))
        self.assertEqual(app.show_tray_hint.call_count, 1)
        title, body = app.show_tray_hint.call_args[0][:2]
        self.assertIn("NVIDIA", title)
        self.assertIn("6 s", body)
        self.assertIs(app.show_tray_hint.call_args[1]["on_click"].__func__,
                      main.AppController.show_gpu_settings)
        self.assertEqual(app.cfg[gpu_accel.CFG_COUNT], 1)

    def test_the_offer_waits_out_another_notice_and_is_not_counted(self):
        app = _app()
        self._offer(app, ("dictation", "large-v3-turbo", 6.0), last_hint=time.monotonic())
        app.show_tray_hint.assert_not_called()
        self.assertNotIn(gpu_accel.CFG_COUNT, app.cfg)

    def test_file_jobs_offer_it_even_with_cloud_dictation(self):
        app = _app(backend="managed")
        self._offer(app, ("file", "large-v3", 40.0))
        self.assertEqual(app.show_tray_hint.call_count, 1)

    def test_a_notification_click_runs_its_action_once(self):
        app = _app()
        hit = []
        app._tray_click = lambda: hit.append(1)
        app._on_tray_message_clicked()
        app._on_tray_message_clicked()
        self.assertEqual(hit, [1])

    def test_a_plain_notification_clears_an_older_action(self):
        app = SimpleNamespace(tray_icon=mock.MagicMock(), _tray_click=lambda: None)
        main.AppController.show_tray_hint(app, "Sign in", "Opening your browser")
        self.assertIsNone(app._tray_click)


# ── the Settings card ──────────────────────────────────────────────────────

def _real_qt():
    # Other test modules swap PySide6 for stubs; only the real one counts.
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


class TestSettingsCard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Decided at run time: stubs installed by later-collected modules.
        if not _real_qt():
            raise unittest.SkipTest("real PySide6 not importable (stubbed)")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.qapp = QApplication.instance() or QApplication([])

    def _settings(self, phase=None, cfg=None):
        from ui import settings as S
        win = S.Settings.__new__(S.Settings)
        from PySide6.QtWidgets import QCheckBox, QFrame, QLabel, QProgressBar, QPushButton
        win.gpu_card, win.gpu_reco = QFrame(), QFrame()
        win.lbl_gpu_status, win.gpu_progress = QLabel(""), QProgressBar()
        win.btn_gpu, win.btn_gpu_remove = QPushButton(""), QPushButton("Remove")
        win.chk_gpu_never = QCheckBox("")
        for w in (win.gpu_card, win.gpu_reco):
            w.show()
        win._gpu_state = None
        win.app = SimpleNamespace(cfg={"backend": "local", **(cfg or {})},
                                  gpu_phase=lambda: phase,
                                  recorder=SimpleNamespace(_cuda_usable=None))
        return S, win

    def test_states(self):
        S, win = self._settings()
        with mock.patch.object(S.gpu_accel, "removal_pending", lambda: False), \
                mock.patch.object(S.gpu_accel, "libs_dir", lambda: None), \
                mock.patch.object(S.gpu_accel, "can_offer", lambda cfg: True):
            S.Settings._refresh_gpu_ui(win, "unsupported")
            self.assertTrue(win.gpu_card.isHidden())
            self.assertTrue(win.gpu_reco.isHidden())
            S.Settings._refresh_gpu_ui(win, "available")
            self.assertFalse(win.gpu_card.isHidden())
            self.assertIn("Download", win.btn_gpu.text())
            self.assertFalse(win.gpu_reco.isHidden())               # recommended up front
            win.app.cfg[gpu_accel.CFG_INTRO] = True                 # "Not now" was clicked
            S.Settings._refresh_gpu_ui(win, "available")
            self.assertTrue(win.gpu_reco.isHidden())
            win._gpu_last_error = "Not enough free disk space"      # a failed attempt stays visible
            S.Settings._refresh_gpu_ui(win, "available")
            self.assertIn("disk space", win.lbl_gpu_status.text())
            self.assertIn("Try again", win.btn_gpu.text())
        _, win = self._settings(phase="download")
        with mock.patch.object(S.gpu_accel, "removal_pending", lambda: False), \
                mock.patch.object(S.gpu_accel, "libs_dir", lambda: None):
            S.Settings._refresh_gpu_ui(win, "available")
            self.assertEqual(win.btn_gpu.text(), "Cancel")
            self.assertFalse(win.gpu_progress.isHidden())
        _, win = self._settings()
        ours = str(gpu_accel.install_dir())
        with mock.patch.object(S.gpu_accel, "removal_pending", lambda: False), \
                mock.patch.object(S.gpu_accel, "libs_dir", lambda: ours), \
                mock.patch.object(S.gpu_accel, "install_dir", lambda: Path(ours)):
            S.Settings._refresh_gpu_ui(win, "ready")
            self.assertIn("On", win.lbl_gpu_status.text())
            self.assertFalse(win.btn_gpu_remove.isHidden())
            win.app.cfg[gpu_accel.CFG_FAILED] = "CUDA driver version is insufficient"
            S.Settings._refresh_gpu_ui(win, "failed")
            self.assertEqual(win.btn_gpu.text(), "Try again")
            self.assertIn("driver", win.lbl_gpu_status.text())
        with mock.patch.object(S.gpu_accel, "removal_pending", lambda: True), \
                mock.patch.object(S.gpu_accel, "libs_dir", lambda: ours):
            S.Settings._refresh_gpu_ui(win, "ready")
            self.assertIn("restarts", win.lbl_gpu_status.text())

    def test_save_never_puts_back_stale_gpu_state(self):
        from ui import settings as S
        for key in gpu_accel.CFG_KEYS:
            self.assertIn(key, S.Settings._BACKGROUND_KEYS)


class TestRound4(_Base):
    def test_only_gpu_stack_errors_count_as_gpu_errors(self):
        for msg in ("Library cublas64_12.dll is not found", "CUDA driver version is insufficient",
                    "cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED"):
            self.assertTrue(gpu_accel.is_cuda_error(msg), msg)
        self.assertTrue(gpu_accel.is_oom("CUBLAS_STATUS_ALLOC_FAILED"))
        for msg in ("Unable to open file 'model.bin'", "Cannot find an appropriate cached snapshot",
                    "KeyError: 'segments'"):
            self.assertFalse(gpu_accel.is_cuda_error(msg), msg)

    def test_local_app_data_even_without_the_variable(self):
        self.patches[0].stop()                                      # the real _base_dir
        try:
            with mock.patch.object(gpu_accel, "IS_WINDOWS", True), \
                    mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TRANSCRIBE_APP_DATA_DIR", None)
                os.environ.pop("LOCALAPPDATA", None)
                self.assertEqual(gpu_accel._base_dir(),
                                 Path.home() / "AppData" / "Local" / "Transcribe" / "gpu")
        finally:
            self.patches[0].start()


class TestVerifyRound4(unittest.TestCase):
    def test_cloud_dictation_users_can_test_the_gpu_too(self):
        app = _app(backend="managed")

        def load(name):
            app.recorder._cuda_usable = True
        app.recorder.load_model.side_effect = load
        with mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch.object(main.gpu_accel, "download", lambda **k: Path("x")), \
                mock.patch.object(main.gpu_accel, "register_dll_dirs", lambda: None), \
                mock.patch.object(main, "model_downloaded", return_value=True):
            app.start_gpu_download("settings")
        self.assertEqual(app.sent["done"], [("ok", "")])

    def test_a_model_that_wont_load_at_all_leaves_the_gpu_untested_not_failed(self):
        app = _app()
        app.recorder.load_model.side_effect = RuntimeError("Unable to open file 'model.bin'")
        with mock.patch.object(main.threading, "Thread", _Inline), \
                mock.patch.object(main.gpu_accel, "download", lambda **k: Path("x")), \
                mock.patch.object(main.gpu_accel, "register_dll_dirs", lambda: None), \
                mock.patch.object(main.logger, "warning", lambda *a, **k: None), \
                mock.patch.object(main, "model_downloaded", return_value=True):
            app.start_gpu_download("settings")
        self.assertEqual(app.sent["done"], [("pending", "")])


if __name__ == "__main__":
    unittest.main()
