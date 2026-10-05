"""In-app updates (updater.py, ui/update_dialog.py, AppController.start_update)
and the model-download fixes. Nothing real is downloaded or launched."""
import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# Never the real settings, keyring or app data (main loads config at import).
os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="transcribe-test-data-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")

import local_llm
import main
import updater

INSTALLER = b"MZ" + b"\x00" * (2 * 1024 * 1024)        # a 2 MB "installer"
SHA = hashlib.sha256(INSTALLER).hexdigest()


class _Resp:
    def __init__(self, status=200, body=b"", text="", headers=None):
        self.status_code, self.body, self.text = status, body, text
        self.headers = dict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), 256 * 1024):
            yield self.body[i:i + 256 * 1024]


def _server(body=INSTALLER, sha=SHA, status=200, length=None, fail=None):
    calls = []

    def get(url, **kw):
        calls.append(url)
        if fail is not None:
            raise fail
        if url.endswith(".sha256"):
            return _Resp(200 if sha else 404, text=f"{sha}  {updater.SETUP_NAME}\n" if sha else "")
        return _Resp(status, body, headers={"Content-Length": str(len(body) if length is None else length)})
    return get, calls


class TestDownload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_downloads_with_progress_and_a_verified_checksum(self):
        get, calls = _server()
        progress = []
        path = updater.download("v1.9.5", on_progress=lambda d, t: progress.append((d, t)),
                                get=get, dest_dir=self.dir)
        self.assertEqual(Path(path).read_bytes(), INSTALLER)
        self.assertEqual(progress[-1], (len(INSTALLER), len(INSTALLER)))
        self.assertTrue(all(t == len(INSTALLER) for _, t in progress))
        self.assertTrue(calls[0].endswith(".sha256"))
        self.assertIn("/releases/download/v1.9.5/", calls[1])

    def test_every_download_gets_its_own_file_and_old_ones_go(self):
        old = Path(self.dir, updater.SETUP_NAME)                   # the old fixed name
        old.write_bytes(b"x")
        older = Path(self.dir, updater.TEMP_PREFIX + "v1.9.4-abcd1234.exe")
        older.write_bytes(b"x")
        get, _ = _server()
        a = updater.download("v1.9.5", get=get, dest_dir=self.dir)
        b = updater.download("v1.9.5", get=get, dest_dir=self.dir)
        self.assertNotEqual(a, b)
        self.assertFalse(old.exists())
        self.assertFalse(older.exists())

    def test_clean_old_removes_leftover_installers_only(self):
        keep = Path(self.dir, "notes.exe")
        keep.write_bytes(b"x")
        Path(self.dir, updater.TEMP_PREFIX + "v1.9.5-0123abcd.exe").write_bytes(b"x")
        updater.clean_old(self.dir)
        self.assertEqual(os.listdir(self.dir), ["notes.exe"])

    def test_a_leftover_installer_still_running_never_blocks_the_update(self):
        Path(self.dir, updater.SETUP_NAME).write_bytes(b"x")
        get, _ = _server()
        real_remove = os.remove

        def locked(path):
            if path.endswith(updater.SETUP_NAME):
                raise PermissionError(32, "being used by another process")
            real_remove(path)
        with mock.patch.object(updater.os, "remove", locked):
            path = updater.download("v1.9.5", get=get, dest_dir=self.dir)
        self.assertTrue(os.path.exists(path))

    def test_a_damaged_download_is_refused_and_removed(self):
        get, _ = _server(sha="0" * 64)
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download("v1.9.5", get=get, dest_dir=self.dir)
        self.assertIn("damaged", str(cm.exception))
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".exe")], [])

    def test_a_release_without_a_checksum_still_installs(self):
        get, _ = _server(sha="")
        self.assertTrue(os.path.exists(updater.download("v1.9.5", get=get, dest_dir=self.dir)))

    def test_a_missing_installer_says_so(self):
        get, _ = _server(status=404)
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download("v1.9.5", get=get, dest_dir=self.dir)
        self.assertIn("isn't on GitHub yet", str(cm.exception))

    def test_a_cut_off_download_or_an_error_page_is_never_installed(self):
        for body, length in ((INSTALLER[:500_000], len(INSTALLER)), (b"<html>error</html>", None)):
            get, _ = _server(body=body, length=length, sha="")
            with self.assertRaises(updater.UpdateError):
                updater.download("v1.9.5", get=get, dest_dir=self.dir)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".exe")], [])

    def test_cancel_stops_and_cleans_up(self):
        get, _ = _server()
        with self.assertRaises(updater.UpdateCancelled):
            updater.download("v1.9.5", get=get, dest_dir=self.dir, should_cancel=lambda: True)
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".exe")], [])

    def test_a_connection_error_after_cancel_is_a_cancel_not_an_error(self):
        # They pressed Cancel because it stalled; the stall then times out.
        flag = []
        get, _ = _server(fail=ConnectionError("timed out"))

        def cancelled():
            return bool(flag)
        real = get

        def get2(url, **kw):
            if not url.endswith(".sha256"):
                flag.append(1)
            return real(url, **kw)
        with self.assertRaises(updater.UpdateCancelled):
            updater.download("v1.9.5", get=get2, dest_dir=self.dir, should_cancel=cancelled)

    def test_a_dropped_connection_is_worded_for_people(self):
        get, _ = _server(fail=ConnectionError("reset by peer"))
        with self.assertRaises(updater.UpdateError) as cm:
            updater.download("v1.9.5", get=get, dest_dir=self.dir)
        self.assertIn("internet connection", str(cm.exception))

    def test_a_full_disk_says_so(self):
        get, _ = _server()
        with mock.patch.object(updater, "open", create=True,
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(updater.UpdateError) as cm:
                updater.download("v1.9.5", get=get, dest_dir=self.dir)
        self.assertIn("disk space", str(cm.exception))


class TestInstallKind(unittest.TestCase):
    def test_only_an_installed_copy_updates_itself(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "TranscribeApp.exe")
            self.assertFalse(updater.can_self_install(exe))           # the portable zip
            Path(d, "unins000.exe").write_bytes(b"x")
            self.assertTrue(updater.can_self_install(exe))

    def test_a_source_checkout_never_updates_itself(self):
        with mock.patch.object(updater.sys, "frozen", False, create=True):
            self.assertFalse(updater.can_self_install())

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_all_users_install_detection(self):
        local = r"C:\Users\a\AppData\Local\Programs\Transcribe\TranscribeApp.exe"
        pf = r"C:\Program Files\Transcribe"
        with mock.patch.dict(os.environ, {"ProgramFiles": r"C:\Program Files",
                                          "ProgramFiles(x86)": r"C:\Program Files (x86)",
                                          "ProgramW6432": r"C:\Program Files"}):
            self.assertTrue(updater.is_all_users_install(pf + r"\TranscribeApp.exe",
                                                         registry=lambda: {}))
            self.assertFalse(updater.is_all_users_install(local, registry=lambda: {}))
            # Another account's all-users copy elsewhere: not THIS copy.
            self.assertFalse(updater.is_all_users_install(local, registry=lambda: {"HKLM": [pf]}))
            # Installed for all users outside Program Files: the entry points here.
            self.assertTrue(updater.is_all_users_install(
                r"D:\Apps\Transcribe\TranscribeApp.exe",
                registry=lambda: {"HKLM": ["d:\\apps\\transcribe\\"]}))
            # Both entries point here (odd, but): the per-user one wins.
            self.assertFalse(updater.is_all_users_install(
                r"D:\Apps\Transcribe\TranscribeApp.exe",
                registry=lambda: {"HKCU": [r"D:\Apps\Transcribe"], "HKLM": [r"D:\Apps\Transcribe"]}))

            def broken():
                raise OSError("registry unavailable")
            self.assertFalse(updater.is_all_users_install(r"D:\Apps\T\TranscribeApp.exe",
                                                          registry=broken))

    @unittest.skipUnless(os.name == "nt", "the Windows registry")
    def test_the_registry_lookup_runs(self):
        found = updater._registry_install_folders()
        self.assertEqual(set(found), {"HKCU", "HKLM"})


class TestLaunch(unittest.TestCase):
    def test_a_quiet_install_for_this_user_with_its_own_log(self):
        started = []
        updater.launch_installer("C:/t/setup.exe", log_path="C:/t/x.log",
                                 start=lambda *a: started.append(a), environ={})
        path, verb, args = started[0]
        self.assertEqual((path, verb), ("C:/t/setup.exe", "open"))
        for flag in ("/SILENT", "/NOCANCEL", "/relaunch=1", '/LOG="C:/t/x.log"',
                     '/MERGETASKS="!startup"', "/CURRENTUSER"):
            self.assertIn(flag, args)
        self.assertNotIn("/ALLUSERS", args)

    def test_the_installer_gets_a_fresh_environment_too(self):
        env = {"_PYI_PARENT_PROCESS_LEVEL": "1", "PATH": "x"}
        updater.launch_installer("C:/t/setup.exe", start=lambda *a: None, environ=env)
        self.assertEqual(env, {"PATH": "x", "PYINSTALLER_RESET_ENVIRONMENT": "1"})

    def test_no_installer_relaunch_when_the_app_reopens_itself(self):
        started = []
        updater.launch_installer("C:/t/setup.exe", relaunch=False,
                                 start=lambda *a: started.append(a), environ={})
        self.assertNotIn("/relaunch", started[0][2])
        self.assertIn("/LOG=", started[0][2])            # a fresh log even unasked

    def test_an_all_users_install_asks_for_administrator_rights_up_front(self):
        started = []
        updater.launch_installer("C:/t/setup.exe", all_users=True,
                                 start=lambda *a: started.append(a), environ={})
        _, verb, args = started[0]
        self.assertEqual(verb, "runas")
        self.assertIn("/ALLUSERS", args)
        self.assertNotIn("/CURRENTUSER", args)

    def test_saying_no_to_the_administrator_prompt_is_worded_for_people(self):
        def refuse(*a):
            err = OSError(22, "The operation was canceled by the user")
            err.winerror = 1223
            raise err
        with self.assertRaises(updater.UpdateError) as cm:
            updater.launch_installer("C:/t/setup.exe", all_users=True, start=refuse, environ={})
        self.assertIn("administrator", str(cm.exception).lower())

    def test_windows_refusing_to_start_it_is_an_update_error(self):
        def refuse(*a):
            raise OSError(2, "not found")
        with self.assertRaises(updater.UpdateError) as cm:
            updater.launch_installer("C:/t/setup.exe", start=refuse, environ={})
        self.assertIn("wouldn't start", str(cm.exception))

    def test_every_attempt_gets_its_own_log_and_old_ones_are_cleaned(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = updater.new_log_path(d), updater.new_log_path(d)
            self.assertNotEqual(a, b)
            Path(a).write_text("x")
            updater.clean_old(d)
            self.assertFalse(os.path.exists(a))

    def test_the_installer_reopens_the_app_only_for_an_in_app_update(self):
        iss = Path(main.__file__).with_name("installer").joinpath("installer.iss").read_text(encoding="utf-8")
        self.assertIn("Check: RelaunchAfterUpdate", iss)
        self.assertIn("{param:relaunch|0}", iss)
        self.assertIn("runasoriginaluser", iss)


class TestRelauncher(unittest.TestCase):
    def test_it_waits_for_the_installer_log_then_reopens_the_app(self):
        calls = []
        exe = "C:\\Users\\D\u2019Souza\\Programs\\Transcribe\\TranscribeApp.exe"
        log = r"C:\Users\O'Neil\Temp\TranscribeApp-Update-ab.log"
        with mock.patch.dict(os.environ, {"_PYI_APPLICATION_HOME_DIR": "x", "_MEIPASS2": "y"}):
            proc = updater.spawn_relauncher(exe, log, popen=lambda cmd, **kw: calls.append((cmd, kw)) or "proc")
        self.assertEqual(proc, "proc")
        cmd, kw = calls[0]
        self.assertTrue(cmd[0].lower().endswith("powershell.exe"))
        self.assertNotIn("-ExecutionPolicy", cmd)                    # no "hidden bypass" look
        script = cmd[-1]
        self.assertEqual(script, updater.RELAUNCH_SCRIPT)
        self.assertIn("'Log closed.'", script)
        self.assertIn("UtcNow", script)                              # a clock change can't cut it short
        self.assertIn("'--show-settings'", script)
        # No path inside the script text - no quote can break it.
        self.assertNotIn("Souza", script)
        self.assertNotIn("O'Neil", script)
        env = kw["env"]
        self.assertEqual(env["TRANSCRIBE_RELAUNCH_EXE"], exe)
        self.assertEqual(env["TRANSCRIBE_RELAUNCH_LOG"], log)
        self.assertEqual(env["TRANSCRIBE_RELAUNCH_MINUTES"], str(updater.RELAUNCH_TIMEOUT_MIN))
        # A NEW Transcribe, not a child of this bundle.
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)
        self.assertNotIn("_MEIPASS2", env)
        self.assertTrue(kw["creationflags"] & 0x08000000)            # no window

    @unittest.skipUnless(os.name == "nt", "Windows")
    def test_powershell_by_full_path(self):
        self.assertTrue(os.path.isabs(updater._powershell()))

    def test_without_job_breakaway_it_still_starts(self):
        flags = []

        def popen(cmd, creationflags=0, **kw):
            flags.append(creationflags)
            if creationflags & 0x01000000:
                raise PermissionError(5, "Access is denied")
            return "proc"
        self.assertEqual(updater.spawn_relauncher("C:/a.exe", "C:/l.log", popen=popen), "proc")
        self.assertEqual(len(flags), 2)

    def test_none_when_it_cant_start(self):
        def popen(*a, **kw):
            raise FileNotFoundError(2, "powershell.exe")
        self.assertIsNone(updater.spawn_relauncher("C:/a.exe", "C:/l.log", popen=popen))


class _Inline:
    def __init__(self, target=None, daemon=None, **kw):
        self.target = target

    def start(self):
        self.target()

    def is_alive(self):
        return False


class _Deferred(_Inline):
    """A worker that runs only when the test says so."""
    pending = []

    def start(self):
        _Deferred.pending.append(self.target)


class _Dialog:
    def __init__(self, tag, style=None):
        self.tag, self.calls, self.stage, self.visible = tag, [], "downloading", False
        sig = lambda: SimpleNamespace(connect=lambda fn: None)
        self.cancel_requested, self.retry_requested, self.website_requested = sig(), sig(), sig()

    def isVisible(self):
        return self.visible

    def __getattr__(self, name):
        if name.startswith("show_") or name == "hide":
            def call(*a, **kw):
                self.calls.append((name,) + a + tuple(kw.values()))
                self.visible = name != "hide"
                if name.startswith("show_") and name != "show_progress":
                    self.stage = name[5:]
            return call
        raise AttributeError(name)


def _front(w):
    """AppController._bring_to_front: shows the window, on top."""
    if isinstance(w, _Dialog):
        w.calls.append(("front",))
        w.visible = True


def _app(**over):
    app = SimpleNamespace(
        cfg={"pending_update_version": "v1.9.5"}, is_rec=False, _busy=False,
        _file_job_running=False, _update_thread=None, settings_win=None,
        _update_dialog=None, _update_cancel=False, _update_manual=False, _update_path=None,
        _update_prompt_open=False,
        save_config=mock.MagicMock(), track=mock.MagicMock(), show_tray_hint=mock.MagicMock(),
        quit_app=mock.MagicMock(), qapp=mock.MagicMock(), recorder=mock.MagicMock(),
        _unregister_transient_keys=mock.MagicMock(), _unregister_kbd_hotkey=mock.MagicMock(),
        _unregister_mouse_listener=mock.MagicMock(),
        _bring_to_front=_front,
        _is_meeting_busy=lambda: False, _is_newer=main.AppController._is_newer)
    for name in ("start_update", "_cancel_update", "_on_update_progress", "_on_update_stage",
                 "_launch_update", "_launch_update_when_free", "_update_blocked",
                 "_quit_for_update", "_update_in_progress", "_update_stale", "_update_dialog_for",
                 "_settings_saved_for_update", "_show_update_failed_after_restart",
                 "_after_restart_checks", "_prompt_update", "_start", "_ask_about_settings"):
        setattr(app, name, getattr(main.AppController, name).__get__(app))
    stage = lambda s, i: app._on_update_stage(s, i)
    app.sig_update_stage = SimpleNamespace(emit=stage)
    app.sig_update_progress = SimpleNamespace(emit=lambda d, t: app._on_update_progress(d, t))
    for k, v in over.items():
        setattr(app, k, v)
    return app


class _Settings:
    """The Settings window as the update sees it."""

    def __init__(self, unsaved=(), answer=True):
        self.unsaved, self.answer, self.asked = list(unsaved), answer, 0

    def isVisible(self):
        return True

    def _unsaved_keys(self):
        return self.unsaved

    def _confirm_discard_or_save(self):
        if not self.unsaved:
            return True                     # like the real one: no box at all
        self.asked += 1
        if self.answer:
            self.unsaved = []
        return self.answer


class TestAppFlow(unittest.TestCase):
    def _patches(self, download, launch=None, all_users=False, self_install=True, timer=None,
                 watcher="default", thread=_Inline):
        launch = launch or mock.MagicMock()
        self.watcher = mock.MagicMock() if watcher == "default" else watcher
        self.spawn = mock.MagicMock(return_value=self.watcher)
        ps = [mock.patch.object(main.sys, "platform", "win32"),
              mock.patch.object(main.sys, "executable", r"C:\T\TranscribeApp.exe"),
              mock.patch.object(main.threading, "Thread", thread),
              mock.patch.object(main.updater, "download", download),
              mock.patch.object(main.updater, "launch_installer", launch),
              mock.patch.object(main.updater, "can_self_install", lambda: self_install),
              mock.patch.object(main.updater, "is_all_users_install", lambda: all_users),
              mock.patch.object(main.updater, "new_log_path", lambda: "C:/t/u.log"),
              mock.patch.object(main.updater, "spawn_relauncher", self.spawn),
              mock.patch.object(main.QTimer, "singleShot", timer or (lambda ms, fn: fn())),
              mock.patch("ui.update_dialog.UpdateDialog", _Dialog)]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)
        return launch

    def _ready(self, tag, on_progress=None, should_cancel=None):
        if on_progress:
            on_progress(50.0, 100.0)
        return "C:/t/TranscribeApp-Update-v1.9.5.exe"

    def _stages(self, app):
        return [c[0] for c in app._update_dialog.calls if c[0] != "front"]

    def test_progress_then_installing_then_the_app_closes_for_the_installer(self):
        app = _app()
        timers = []
        launch = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app.start_update("v1.9.5", manual=True)
        self.assertEqual(self._stages(app), ["show_downloading", "show_progress", "show_installing"])
        timers.pop(0)()
        # A per-user Setup reopens the app when it succeeds; the watcher, as
        # this user, covers everything else (a failed install too).
        launch.assert_called_once_with("C:/t/TranscribeApp-Update-v1.9.5.exe", all_users=False,
                                       log_path="C:/t/u.log", relaunch=True)
        self.spawn.assert_called_once_with(r"C:\T\TranscribeApp.exe", "C:/t/u.log")
        # No new dictation from here on; what's being installed is remembered.
        app._unregister_kbd_hotkey.assert_called_once()
        self.assertEqual(app.cfg["update_attempt"], {"to": "v1.9.5", "log": "C:/t/u.log"})
        app.qapp.exit.assert_not_called()                   # only after the 1.5 s timer
        timers.pop(0)()
        # Closed for good - never through a quit a window could veto.
        app.qapp.exit.assert_called_once_with(0)
        app.recorder.shutdown.assert_called_once()
        app.quit_app.assert_not_called()
        self.assertEqual(app.cfg["pending_update_version"], "")
        app.track.assert_any_call("update_install_started", {"manual": True, "to": "v1.9.5"})
        app.track.assert_any_call("update_install_result", {"manual": True, "ok": True})

    def test_an_elevated_installer_never_reopens_the_app_itself(self):
        app = _app()
        timers = []
        launch = self._patches(self._ready, all_users=True, timer=lambda ms, fn: timers.append((ms, fn)))
        app.start_update("v1.9.5")
        timers.pop(0)[1]()
        self.assertEqual(launch.call_args.kwargs["all_users"], True)
        self.assertFalse(launch.call_args.kwargs["relaunch"])
        self.spawn.assert_called_once()                     # the watcher does, as this user
        self.assertEqual(timers[0][0], 1500)

    def test_when_nothing_can_reopen_it_the_window_says_so(self):
        # All-users install and PowerShell blocked: be honest, and give it
        # time to be read.
        app = _app()
        timers = []
        self._patches(self._ready, all_users=True, watcher=None,
                      timer=lambda ms, fn: timers.append((ms, fn)))
        app.start_update("v1.9.5")
        timers.pop(0)[1]()
        self.assertIn(("show_installing", False), app._update_dialog.calls)
        self.assertEqual(timers[0][0], 5000)

    def test_never_a_second_installer(self):
        app = _app()
        launch = self._patches(self._ready)
        app.start_update("v1.9.5")
        app._launch_update("C:/t/again.exe")
        launch.assert_called_once()

    def test_saying_no_to_administrator_rights_keeps_the_app_open(self):
        app = _app()
        launch = mock.MagicMock(side_effect=updater.UpdateError(
            "Administrator permission is needed for it, and it wasn't given."))
        self._patches(self._ready, launch=launch, all_users=True)
        app.start_update("v1.9.5")
        self.assertEqual(app._update_dialog.calls[-1][0], "front")
        self.assertEqual(self._stages(app)[-1], "show_failed")
        self.spawn.assert_not_called()                      # nothing to reopen later
        app.qapp.exit.assert_not_called()
        app._unregister_kbd_hotkey.assert_not_called()      # still fully working
        self.assertNotIn("update_attempt", app.cfg)
        app.track.assert_any_call("update_install_result", {"manual": False, "ok": False})

    def test_a_failure_is_shown_with_a_way_forward_and_the_app_stays(self):
        app = _app()

        def download(tag, **kw):
            raise updater.UpdateError("The download didn't finish.")
        launch = self._patches(download)
        app.start_update("v1.9.5", manual=True)
        self.assertIn(("show_failed", "The download didn't finish."), app._update_dialog.calls)
        launch.assert_not_called()
        self.spawn.assert_not_called()
        app.qapp.exit.assert_not_called()

    def test_an_unexpected_error_is_a_failure_too(self):
        app = _app()

        def download(tag, **kw):
            raise ValueError("bug")
        self._patches(download)
        app.start_update("v1.9.5")
        self.assertEqual(self._stages(app)[-1], "show_failed")

    def test_never_in_the_middle_of_a_recording(self):
        for busy in ({"is_rec": True}, {"_busy": True}, {"_file_job_running": True},
                     {"_is_meeting_busy": lambda: True}):
            app = _app(**busy)
            download = mock.MagicMock()
            self._patches(download)
            app.start_update("v1.9.5")
            download.assert_not_called()
            self.assertIn("recording", app.show_tray_hint.call_args[0][0].lower())
            # Not counted as an install that started.
            self.assertNotIn("update_install_started",
                             [c[0][0] for c in app.track.call_args_list])

    def test_a_recording_started_during_the_download_is_waited_for(self):
        app = _app()
        timers = []
        launch = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))

        app.start_update("v1.9.5")
        self.assertEqual(self._stages(app)[-1], "show_installing")
        app.is_rec = True                                   # Alt+R during the download
        timers.pop(0)()
        self.assertEqual(self._stages(app)[-1], "show_waiting")
        launch.assert_not_called()
        timers.pop(0)()                                     # still recording
        launch.assert_not_called()
        app.is_rec = False
        timers.pop(0)()
        self.assertEqual(self._stages(app)[-1], "show_installing")
        launch.assert_called_once()
        app.qapp.exit.assert_not_called()                   # only after the 1.5 s timer
        timers.pop(0)()
        app.qapp.exit.assert_called_once_with(0)

    def test_settings_edits_made_during_the_download_are_asked_about(self):
        win = _Settings()
        app = _app(settings_win=win)
        timers = []
        launch = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app.start_update("v1.9.5")                          # nothing unsaved yet
        win.unsaved, win.answer = ["hotkey"], False         # edited meanwhile; then "Cancel"
        timers.pop(0)()
        self.assertEqual(win.asked, 1)
        launch.assert_not_called()
        self.assertIn("Settings", app._update_dialog.calls[-1][1])   # show_waiting(text)
        timers.pop(0)()                                     # asked once, not every 2 s
        self.assertEqual(win.asked, 1)
        launch.assert_not_called()
        win.unsaved = []                                    # they saved or discarded
        timers.pop(0)()
        launch.assert_called_once()

    def test_settings_saved_at_the_prompt_go_straight_on(self):
        win = _Settings()
        app = _app(settings_win=win)
        launch = self._patches(self._ready)
        win.unsaved = []
        app.start_update("v1.9.5")
        launch.assert_called_once()
        # With edits: Save at the prompt, then it installs.
        win2 = _Settings(answer=True)
        app2 = _app(settings_win=win2)
        timers = []
        launch2 = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app2.start_update("v1.9.5")
        win2.unsaved = ["backend"]
        timers.pop(0)()
        self.assertEqual(win2.asked, 1)
        launch2.assert_called_once()

    def test_cancelling_the_update_while_the_settings_box_is_open(self):
        # The save prompt runs its own event loop: the update window's
        # Cancel can be pressed meanwhile - then nothing installs.
        app = _app()
        win = _Settings(answer=True)

        def confirm_and_cancel():
            app._cancel_update()
            win.unsaved = []
            return True
        app.settings_win = win
        timers = []
        launch = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app.start_update("v1.9.5")
        win.unsaved = ["hotkey"]
        win._confirm_discard_or_save = confirm_and_cancel
        timers.pop(0)()
        launch.assert_not_called()
        self.assertEqual(timers, [])
        self.assertFalse(app._update_dialog.isVisible())    # no buttonless window left up
        self.assertFalse(app._update_in_progress())

    def test_a_dictation_started_while_the_settings_box_is_open_is_waited_for(self):
        app = _app()
        win = _Settings()

        def confirm_while_dictating():
            app.is_rec = True                               # Alt+R with the box open
            win.unsaved = []
            return True
        app.settings_win = win
        timers = []
        launch = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app.start_update("v1.9.5")
        win.unsaved = ["hotkey"]
        win._confirm_discard_or_save = confirm_while_dictating
        timers.pop(0)()
        launch.assert_not_called()
        app._unregister_kbd_hotkey.assert_not_called()      # the dictation can still stop
        self.assertEqual(self._stages(app)[-1], "show_waiting")
        app.is_rec = False
        timers.pop(0)()
        launch.assert_called_once()

    def test_the_settings_box_is_never_hidden_under_the_update_window(self):
        app = _app()
        seen = []
        win = _Settings()

        def confirm():
            seen.append(app._update_dialog.isVisible())
            win.unsaved = []
            return True
        app.settings_win = win
        timers = []
        self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app.start_update("v1.9.5")
        win.unsaved = ["hotkey"]
        win._confirm_discard_or_save = confirm
        timers.pop(0)()
        self.assertEqual(seen, [False])                     # stepped aside while asking
        self.assertTrue(app._update_dialog.isVisible())     # and back after

    def test_no_second_update_while_the_settings_box_is_open(self):
        app = _app()
        win = _Settings(unsaved=["hotkey"])
        downloads = []

        def confirm():
            # The popup's Yes (or another click) arrives inside the box's loop.
            app.start_update("v1.9.5")
            app._prompt_update("v99.0.0")
            win.unsaved = []
            return True
        win._confirm_discard_or_save = confirm
        app.settings_win = win
        self._patches(lambda tag, **kw: downloads.append(tag) or "C:/t/s.exe")
        with mock.patch.object(main.QMessageBox, "question") as ask:
            app.start_update("v1.9.5", manual=True)
        ask.assert_not_called()
        self.assertEqual(downloads, ["v1.9.5"])

    def test_a_dictation_started_during_the_first_settings_box_blocks_the_start(self):
        app = _app()
        win = _Settings(unsaved=["hotkey"])

        def confirm():
            app.is_rec = True
            win.unsaved = []
            return True
        win._confirm_discard_or_save = confirm
        app.settings_win = win
        download = mock.MagicMock()
        self._patches(download)
        app.start_update("v1.9.5", manual=True)
        download.assert_not_called()
        self.assertIn("recording", app.show_tray_hint.call_args[0][0].lower())

    def test_install_again_while_waiting_to_install_shows_that_window(self):
        # Downloaded, waiting for Settings edits: no second 90 MB download.
        app = _app()
        win = _Settings()
        app.settings_win = win
        timers = []
        launch = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app.start_update("v1.9.5")
        win.unsaved, win.answer = ["hotkey"], False
        timers.pop(0)()                                     # asked; "Cancel"
        self.assertEqual(self._stages(app)[-1], "show_waiting")
        win.answer = True                                   # would say Save if asked again
        with mock.patch.object(main.updater, "download") as download:
            app.start_update("v1.9.5", manual=True)
        download.assert_not_called()
        self.assertEqual(win.asked, 1)                      # not asked again from here
        self.assertEqual(app._update_dialog.calls[-1], ("front",))
        launch.assert_not_called()

    def test_nothing_starts_once_the_installer_runs(self):
        app = _app(_update_launched=True)
        download = mock.MagicMock()
        self._patches(download)
        app.start_update("v1.9.5")
        download.assert_not_called()

    def test_a_queued_retry_that_cant_start_leaves_no_stuck_window(self):
        app = _app()
        _Deferred.pending = []
        self._patches(self._ready, thread=_Deferred)
        app.start_update("v1.9.5")
        app._cancel_update()
        app.start_update("v1.9.5")                          # queued
        app.is_rec = True                                   # a dictation meanwhile
        _Deferred.pending.pop(0)()                          # the old one gives up
        self.assertEqual(_Deferred.pending, [])
        self.assertFalse(app._update_dialog.isVisible())
        self.assertFalse(app._update_in_progress())

    def test_cancelling_while_waiting_never_installs(self):
        app = _app()
        timers = []
        launch = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app.start_update("v1.9.5")
        app.is_rec = True
        timers.pop(0)()
        app._cancel_update()
        self.assertEqual(app._update_dialog.calls[-1][0], "hide")   # Cancel closes it
        timers.pop(0)()
        self.assertEqual(timers, [])
        launch.assert_not_called()

    def test_cancel_closes_the_window_at_once_even_mid_download(self):
        app = _app()
        _Deferred.pending = []
        self._patches(self._ready, thread=_Deferred)
        app.start_update("v1.9.5")
        app._cancel_update()
        self.assertEqual(app._update_dialog.calls[-1][0], "hide")

    def test_install_again_while_a_cancelled_download_winds_down(self):
        # A stalled connection: Cancel, then Install again before the worker
        # has noticed. The new request waits for it, then starts - never
        # dropped, never two downloads at once.
        app = _app()
        _Deferred.pending = []
        launch = self._patches(self._ready, thread=_Deferred)
        app.start_update("v1.9.5", manual=True)
        app._cancel_update()
        app.start_update("v1.9.5", manual=True)
        self.assertEqual(len(_Deferred.pending), 1)         # no second download yet
        self.assertEqual(self._stages(app)[-1], "show_downloading")
        _Deferred.pending.pop(0)()                          # the stalled one gives up
        self.assertEqual(len(_Deferred.pending), 1)         # the new one has started
        launch.assert_not_called()
        _Deferred.pending.pop(0)()
        launch.assert_called_once()

    def test_cancel_also_drops_a_queued_retry(self):
        app = _app()
        _Deferred.pending = []
        self._patches(self._ready, thread=_Deferred)
        app.start_update("v1.9.5")
        app._cancel_update()
        app.start_update("v1.9.5")                          # queued
        app._cancel_update()                                # ...and cancelled again
        _Deferred.pending.pop(0)()
        self.assertEqual(_Deferred.pending, [])
        self.assertEqual(app._update_dialog.calls[-1][0], "hide")

    def test_a_cancel_that_lands_as_the_download_finishes_wins(self):
        for outcome in ("ready", "failed"):
            app = _app()

            def download(tag, on_progress=None, should_cancel=None, outcome=outcome):
                app._cancel_update()                         # Cancel pressed just now
                if outcome == "failed":
                    raise updater.UpdateError("timed out")
                return "C:/t/s.exe"
            launch = self._patches(download)
            app.start_update("v1.9.5")
            self.assertEqual(app._update_dialog.calls[-1][0], "hide")
            launch.assert_not_called()
            self.assertEqual(app.cfg["pending_update_version"], "v1.9.5")

    def test_a_timer_from_a_cancelled_attempt_never_acts_on_the_next(self):
        app = _app()
        timers = []
        launch = self._patches(self._ready, timer=lambda ms, fn: timers.append(fn))
        app.start_update("v1.9.5")
        app.is_rec = True
        timers.pop(0)()                                     # waiting
        stale = timers.pop(0)
        app._cancel_update()
        app.is_rec = False

        def slow(tag, **kw):                                # the new attempt still downloading
            raise updater.UpdateCancelled("x")
        with mock.patch.object(main.updater, "download", slow):
            app.start_update("v1.9.5")
        stale()
        launch.assert_not_called()

    def test_unsaved_settings_are_asked_about_first(self):
        win = SimpleNamespace(isVisible=lambda: True, _confirm_discard_or_save=lambda: False)
        app = _app(settings_win=win)
        download = mock.MagicMock()
        self._patches(download)
        app.start_update("v1.9.5", manual=True)
        download.assert_not_called()                        # they chose Cancel
        win._confirm_discard_or_save = lambda: True
        app.start_update("v1.9.5", manual=True)
        download.assert_called_once()

    def test_a_second_click_brings_the_running_update_forward(self):
        app = _app(_update_running=True, _update_dialog=_Dialog("v1.9.5"))
        download = mock.MagicMock()
        self._patches(download)
        app.start_update("v1.9.5")
        download.assert_not_called()
        self.assertEqual(app._update_dialog.calls, [("front",)])

    def test_the_update_popup_never_asks_again_during_an_update(self):
        app = _app(_update_running=True)
        with mock.patch.object(main.QMessageBox, "question") as ask:
            app._prompt_update("v99.0.0")
        ask.assert_not_called()
        app = _app(_update_dialog=_Dialog("v99.0.0"))
        app._update_dialog.show_installing()                # downloaded, installing
        with mock.patch.object(main.QMessageBox, "question") as ask:
            app._prompt_update("v99.0.0")
        ask.assert_not_called()

    def test_the_popup_never_hides_under_a_failure_window(self):
        app = _app(_update_dialog=_Dialog("v99.0.0"))
        app._update_dialog.show_failed("x")
        with mock.patch.object(main.QMessageBox, "question") as ask:
            app._prompt_update("v99.0.0")
        ask.assert_not_called()
        self.assertEqual(app._update_dialog.calls[-1], ("front",))  # Try again is there
        app = _app(_update_failed_notice=True)
        with mock.patch.object(main.QMessageBox, "question") as ask:
            app._prompt_update("v99.0.0")
        ask.assert_not_called()

    def test_the_popup_never_pulls_the_update_window_forward_mid_dictation(self):
        app = _app(is_rec=True, _update_dialog=_Dialog("v99.0.0"))
        app._update_dialog.show_failed("x")
        app._prompt_update("v99.0.0")
        self.assertNotIn(("front",), app._update_dialog.calls)

    def test_no_dictation_once_the_installer_runs(self):
        app = _app(_update_launched=True, overlay=mock.MagicMock())
        app._start()
        self.assertFalse(app.is_rec)
        app.overlay.show_overlay.assert_not_called()
        self.assertIn("updating", app.show_tray_hint.call_args[0][0].lower())

    def test_a_mac_gets_its_disk_image_in_the_browser(self):
        app = _app()
        with mock.patch.object(main.sys, "platform", "darwin"), \
                mock.patch.object(main.webbrowser, "open") as browse, \
                mock.patch.object(main.updater, "download") as download:
            app.start_update("v1.9.5")
        browse.assert_called_once_with(updater.mac_url("v1.9.5"))
        download.assert_not_called()
        app.track.assert_called_once_with("update_install_started",
                                          {"manual": False, "to": "v1.9.5", "handoff": "browser"})

    def test_a_portable_copy_gets_the_download_page(self):
        app = _app()
        download = mock.MagicMock()
        self._patches(download, self_install=False)
        with mock.patch.object(main.webbrowser, "open") as browse:
            app.start_update("v1.9.5")
        browse.assert_called_once_with(updater.releases_page())
        download.assert_not_called()

    def test_the_popup_and_settings_both_use_the_update_window(self):
        src = Path(main.__file__).read_text(encoding="utf-8")
        prompt = src[src.index("    def _prompt_update"):src.index("    def _is_newer")]
        self.assertIn("self.start_update(tag, manual=False)", prompt)
        settings = Path(main.__file__).with_name("ui").joinpath("settings.py").read_text(encoding="utf-8")
        self.assertIn("self.app.start_update(tag, manual=True)", settings)
        self.assertNotIn("TranscribeApp-Windows-Setup.exe\"", settings)
        self.assertNotIn("update_install_finished", settings)


class TestAfterRestart(unittest.TestCase):
    def _run(self, cfg):
        app = _app(cfg=cfg)
        timers = []
        with mock.patch.object(main.QTimer, "singleShot", lambda ms, fn: timers.append((ms, fn))), \
                mock.patch.object(main, "save_config") as save, \
                mock.patch("ui.update_dialog.UpdateDialog", _Dialog):
            app._after_restart_checks()
            for _, fn in list(timers):
                fn()
        return app, timers, save

    def test_just_updated_says_so_and_tidies_up(self):
        with mock.patch.object(main.updater, "clean_old") as clean:
            app, timers, save = self._run({"last_run_version": "0.0.1",
                                           "update_attempt": {"to": main.APP_VERSION}})
        self.assertIn("Updated to version", app.show_tray_hint.call_args[0][1])
        clean.assert_called_once()
        self.assertIn(60_000, [ms for ms, _ in timers])     # once the installer has exited
        self.assertEqual(app.cfg["update_attempt"], {})
        self.assertEqual(app.cfg["last_run_version"], main.APP_VERSION)
        save.assert_called_once()

    def test_an_install_that_stopped_is_never_silent(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "TranscribeApp-Update-x.log")
            Path(log).write_text("...")
            app, _, save = self._run({"last_run_version": main.APP_VERSION,
                                      "update_attempt": {"to": "v99.0.0", "log": log}})
        failed = [c for c in app._update_dialog.calls if c[0] == "show_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn(f"still have version {main.APP_VERSION}", failed[0][1])
        self.assertIn(log, failed[0][1])
        self.assertEqual(app._update_dialog.tag, "v99.0.0")  # Try again = that version
        self.assertEqual(app.cfg["update_attempt"], {})       # said once
        save.assert_called_once()

    def test_the_failure_notice_holds_the_popup_back_until_shown(self):
        app = _app(cfg={"last_run_version": main.APP_VERSION,
                        "update_attempt": {"to": "v99.0.0", "log": ""}})
        timers = []
        with mock.patch.object(main.QTimer, "singleShot", lambda ms, fn: timers.append(fn)), \
                mock.patch.object(main, "save_config"), \
                mock.patch("ui.update_dialog.UpdateDialog", _Dialog):
            app._after_restart_checks()
            self.assertTrue(app._update_failed_notice)
            timers.pop(0)()
        self.assertFalse(app._update_failed_notice)

    def test_the_failure_notice_never_covers_an_update_started_since(self):
        app = _app(_update_gen=1, _update_dialog=_Dialog("v99.0.0"))
        app._update_dialog.show_downloading()
        with mock.patch("ui.update_dialog.UpdateDialog", _Dialog):
            app._show_update_failed_after_restart("v99.0.0", None)
        self.assertNotIn("show_failed", [c[0] for c in app._update_dialog.calls])

    def test_an_ordinary_start_does_nothing(self):
        app, timers, save = self._run({"last_run_version": main.APP_VERSION, "update_attempt": {}})
        self.assertEqual(timers, [])
        save.assert_not_called()
        app.show_tray_hint.assert_not_called()


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


class TestUpdateDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _real_qt():
            raise unittest.SkipTest("real PySide6 not importable (stubbed)")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.qapp = QApplication.instance() or QApplication([])

    def _dialog(self):
        from ui.update_dialog import UpdateDialog
        d = UpdateDialog("v1.9.5")
        hits = []
        d.cancel_requested.connect(lambda: hits.append(1))
        d.show()
        self.addCleanup(d.deleteLater)
        return d, hits

    def test_stages(self):
        d, _ = self._dialog()
        d.show_progress(50 * 2**20, 100 * 2**20)
        self.assertEqual(d.bar.value(), 50)
        self.assertIn("50 of 100 MB", d.lbl_detail.text())
        self.assertFalse(d.btn_cancel.isHidden())
        d.show_installing()
        self.assertIn("reopen by itself", d.lbl_status.text())
        self.assertTrue(d.btn_cancel.isHidden())
        d.show_waiting()
        self.assertIn("as soon as your recording", d.lbl_status.text())
        self.assertFalse(d.btn_cancel.isHidden())
        d.show_failed("The download didn't finish.")
        self.assertIn("didn't finish", d.lbl_status.text())
        for b in (d.btn_retry, d.btn_website, d.btn_close):
            self.assertFalse(b.isHidden())
        d.close()

    def test_waiting_text_can_change_and_repeats_dont_flicker(self):
        d, _ = self._dialog()
        d.show_waiting()
        first = d.lbl_status.text()
        d.show_waiting()
        self.assertEqual(d.lbl_status.text(), first)
        d.show_waiting("Save or discard your changes in Settings first.")
        self.assertIn("Settings", d.lbl_status.text())
        self.assertEqual(d.stage, "waiting")

    def test_never_covers_other_windows_question_boxes(self):
        from PySide6.QtCore import Qt
        d, _ = self._dialog()
        self.assertFalse(d.windowFlags() & Qt.WindowStaysOnTopHint)

    def test_installing_without_a_reopen_says_how_to_get_back(self):
        d, _ = self._dialog()
        d.show_installing(reopens=False)
        self.assertIn("Start menu", d.lbl_status.text())
        self.assertNotIn("reopen by itself", d.lbl_status.text())

    def test_closing_mid_download_cancels(self):
        d, hits = self._dialog()
        d.close()
        self.assertEqual(hits, [1])

    def test_escape_mid_download_cancels_too(self):
        d, hits = self._dialog()
        d.reject()                                          # what Esc does
        self.assertEqual(hits, [1])
        self.assertFalse(d.isVisible())

    def test_escape_while_waiting_cancels(self):
        d, hits = self._dialog()
        d.show_waiting()
        d.reject()
        self.assertEqual(hits, [1])

    def test_escape_while_installing_does_nothing(self):
        d, hits = self._dialog()
        d.show_installing()
        d.reject()
        self.assertEqual(hits, [])
        self.assertTrue(d.isVisible())

    def test_escape_after_a_failure_just_closes(self):
        d, hits = self._dialog()
        d.show_failed("x")
        d.reject()
        self.assertEqual(hits, [])
        self.assertFalse(d.isVisible())


# ── the model downloads ────────────────────────────────────────────────────

class _ModelResp:
    """A fake Hugging Face reply; records the request headers."""

    def __init__(self, status, body, headers):
        self.status_code, self.body, self.headers = status, body, headers

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = RuntimeError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), 300):
            yield self.body[i:i + 300]


FILE = bytes(range(256)) * 4                                 # a 1024-byte "model"


class TestModelDownloads(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        p = mock.patch.object(local_llm, "model_dir", lambda m: self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.requests = []

    def _serve(self, reply):
        def get(url, headers=None, **kw):
            self.requests.append(dict(headers or {}))
            return reply(dict(headers or {}))
        p = mock.patch.object(local_llm.requests, "get", get)
        p.start()
        self.addCleanup(p.stop)

    def _partial(self, n):
        local_llm.partial_path("qwen_3b").write_bytes(FILE[:n])

    def _dest(self):
        return local_llm.model_path("qwen_3b")

    def test_qwen_7b_comes_from_a_single_file_now(self):
        url = local_llm.model_url("qwen_7b")
        self.assertIn("bartowski/Qwen2.5-7B-Instruct-GGUF", url)
        self.assertTrue(url.endswith("Qwen2.5-7B-Instruct-Q4_K_M.gguf"))
        self.assertNotIn("-of-", url)
        self.assertNotIn("legacy_filenames", local_llm.MODEL_CATALOG["qwen_7b"])

    def test_every_model_comes_from_a_pinned_upload(self):
        # Never "main": a re-upload can't splice into a resumed download.
        for mid, info in local_llm.MODEL_CATALOG.items():
            url = local_llm.model_url(mid)
            self.assertRegex(info["revision"], r"^[0-9a-f]{40}$", mid)
            self.assertIn(f"/resolve/{info['revision']}/", url)
            self.assertNotIn("/resolve/main/", url)

    def test_settings_can_ask_about_downloads_while_a_model_loads(self):
        # A model load holds _llm_lock for seconds; the GUI-thread check must
        # not wait for it.
        result = []
        with local_llm._llm_lock:
            t = threading.Thread(target=lambda: result.append(local_llm.downloading("qwen_3b")))
            t.start()
            t.join(2)
            self.assertFalse(t.is_alive(), "downloading() waited for the model lock")
        self.assertEqual(result, [False])

    def test_a_fresh_download(self):
        self._serve(lambda h: _ModelResp(200, FILE, {"Content-Length": str(len(FILE))}))
        progress = []
        local_llm.download_model("qwen_3b", on_progress=lambda *a: progress.append(a))
        self.assertEqual(self._dest().read_bytes(), FILE)
        self.assertNotIn("Range", self.requests[0])
        self.assertEqual(progress[-1], (100, len(FILE), len(FILE)))
        self.assertFalse(local_llm.partial_path("qwen_3b").exists())

    def test_a_stopped_download_resumes_where_it_stopped(self):
        self._partial(400)
        self._serve(lambda h: _ModelResp(206, FILE[400:], {
            "Content-Length": str(len(FILE) - 400),
            "Content-Range": f"bytes 400-{len(FILE) - 1}/{len(FILE)}"}))
        progress = []
        local_llm.download_model("qwen_3b", on_progress=lambda *a: progress.append(a))
        self.assertEqual(self.requests[0]["Range"], "bytes=400-")
        self.assertEqual(self._dest().read_bytes(), FILE)
        # Progress counts the whole file, not just the rest.
        self.assertEqual(progress[0][1:], (700, len(FILE)))

    def test_a_resume_cut_off_again_keeps_everything_so_far(self):
        self._partial(400)
        self._serve(lambda h: _ModelResp(206, FILE[400:700], {
            "Content-Length": str(len(FILE) - 400),
            "Content-Range": f"bytes 400-{len(FILE) - 1}/{len(FILE)}"}))
        with self.assertRaises(local_llm.LocalLLMError) as cm:
            local_llm.download_model("qwen_3b")
        self.assertIn("picks up", str(cm.exception))
        self.assertFalse(self._dest().exists())
        self.assertEqual(local_llm.partial_path("qwen_3b").read_bytes(), FILE[:700])

    def test_a_server_that_ignores_the_resume_restarts_cleanly(self):
        # It sends the whole file again (200): written from the start, not
        # appended - and not reported as a failed download.
        self._partial(400)
        self._serve(lambda h: _ModelResp(200, FILE, {"Content-Length": str(len(FILE))}))
        local_llm.download_model("qwen_3b")
        self.assertEqual(self._dest().read_bytes(), FILE)

    def test_a_reply_for_the_wrong_range_is_never_appended(self):
        # Bytes 500+ after our 400: appended, the file would be short (kept
        # to "resume") and silently scrambled.
        self._partial(400)
        self._serve(lambda h: _ModelResp(206, FILE[500:], {
            "Content-Range": f"bytes 500-{len(FILE) - 1}/{len(FILE)}"}))
        with self.assertRaises(local_llm.LocalLLMError) as cm:
            local_llm.download_model("qwen_3b")
        self.assertIn("starts over", str(cm.exception))
        self.assertFalse(local_llm.partial_path("qwen_3b").exists())
        self.assertFalse(self._dest().exists())

    def test_an_already_complete_partial_is_installed(self):
        self._partial(len(FILE))
        self._serve(lambda h: _ModelResp(416, b"", {"Content-Range": f"bytes */{len(FILE)}"}))
        local_llm.download_model("qwen_3b")
        self.assertEqual(self._dest().read_bytes(), FILE)

    def test_a_too_long_partial_starts_over(self):
        local_llm.partial_path("qwen_3b").write_bytes(FILE + b"junk")
        self._serve(lambda h: _ModelResp(416, b"", {"Content-Range": f"bytes */{len(FILE)}"}))
        with self.assertRaises(local_llm.LocalLLMError):
            local_llm.download_model("qwen_3b")
        self.assertFalse(local_llm.partial_path("qwen_3b").exists())
        self.assertFalse(self._dest().exists())

    def test_more_than_the_server_announced_is_never_installed(self):
        self._serve(lambda h: _ModelResp(200, FILE + b"extra", {"Content-Length": str(len(FILE))}))
        with self.assertRaises(local_llm.LocalLLMError) as cm:
            local_llm.download_model("qwen_3b")
        self.assertIn("starts over", str(cm.exception))
        self.assertFalse(self._dest().exists())
        self.assertFalse(local_llm.partial_path("qwen_3b").exists())

    def test_a_cut_off_model_download_is_kept_to_resume_not_installed(self):
        self._serve(lambda h: _ModelResp(200, FILE[:400], {"Content-Length": str(len(FILE))}))
        with self.assertRaises(local_llm.LocalLLMError):
            local_llm.download_model("qwen_3b")
        self.assertFalse(self._dest().exists())
        self.assertEqual(local_llm.partial_path("qwen_3b").stat().st_size, 400)

    def test_one_download_per_model_at_a_time(self):
        seen = {}

        def reply(h):
            # While the first download runs: a second one is refused, and
            # Settings can tell it's running.
            seen["running"] = local_llm.downloading("qwen_3b")
            with self.assertRaises(local_llm.LocalLLMError) as cm:
                local_llm.download_model("qwen_3b")
            seen["second"] = str(cm.exception)
            with self.assertRaises(local_llm.LocalLLMError):
                local_llm.remove_model("qwen_3b")
            return _ModelResp(200, FILE, {"Content-Length": str(len(FILE))})
        self._serve(reply)
        local_llm.download_model("qwen_3b")
        self.assertTrue(seen["running"])
        self.assertIn("already downloading", seen["second"])
        self.assertFalse(local_llm.downloading("qwen_3b"))
        self.assertEqual(self._dest().read_bytes(), FILE)

    def test_a_failed_download_frees_the_model_for_the_next_try(self):
        def reply(h):
            raise ConnectionError("reset")
        self._serve(reply)
        with self.assertRaises(ConnectionError):
            local_llm.download_model("qwen_3b")
        self.assertFalse(local_llm.downloading("qwen_3b"))

    def test_download_failures_are_worded_for_people(self):
        def http(code):
            e = Exception(str(code))
            e.response = SimpleNamespace(status_code=code)
            return e
        msg = local_llm.download_error_message
        self.assertIn("isn't at its download address", msg(http(404)))
        # Hugging Face's answer for a removed or private repo.
        self.assertIn("isn't at its download address", msg(http(401)))
        self.assertIn("busy", msg(http(503)))
        self.assertIn("busy", msg(http(429)))
        self.assertIn("refused", msg(http(403)))
        self.assertIn("resumes", msg(ConnectionError("reset")))
        self.assertIn("disk space", msg(OSError(28, "No space left")))
        self.assertIn("in use", msg(PermissionError(13, "denied")))
        self.assertIn("already downloading", msg(local_llm.LocalLLMError("this model is already downloading.")))

    @unittest.skipUnless(os.environ.get("TRANSCRIBE_NET_TESTS"), "set TRANSCRIBE_NET_TESTS=1 to check live URLs")
    def test_every_model_url_is_live(self):
        import requests
        for mid, info in local_llm.MODEL_CATALOG.items():
            r = requests.head(local_llm.model_url(mid), allow_redirects=True, timeout=30)
            self.assertEqual(r.status_code, 200, mid)
            self.assertEqual(int(r.headers.get("Content-Length", 0)), info["size"], mid)


class TestSettingsModelCards(unittest.TestCase):
    """Settings re-scans the model folders whenever it opens."""

    def test_reopening_settings_keeps_a_running_download_running(self):
        from ui import settings
        fake = SimpleNamespace(_model_states={"base": "downloading", "small": "failed"},
                               _local_llm_states={"qwen_7b": "downloading"})
        with mock.patch.object(main, "model_downloaded", lambda n: False), \
                mock.patch.object(local_llm, "downloading", lambda m: m == "qwen_3b"), \
                mock.patch.object(local_llm, "model_downloaded", lambda m: False):
            settings.Settings._scan_model_statuses(fake)
        self.assertEqual(fake._model_states["base"], "downloading")
        self.assertEqual(fake._model_states["small"], "missing")
        self.assertEqual(fake._local_llm_states["qwen_7b"], "downloading")
        self.assertEqual(fake._local_llm_states["qwen_3b"], "downloading")
        self.assertEqual(fake._local_llm_states["qwen_tiny"], "missing")

    def test_a_partly_downloaded_model_can_be_resumed_or_removed(self):
        from ui import settings
        for partial in (True, False):
            card = mock.MagicMock()
            fake = SimpleNamespace(llm_cards={"qwen_7b": card}, app=None, cfg_working={},
                                   _local_llm_states={"qwen_7b": "failed"},
                                   _local_llm_progress={})
            with tempfile.TemporaryDirectory() as d, \
                    mock.patch.object(local_llm, "model_dir", lambda m: Path(d)):
                if partial:
                    local_llm.partial_path("qwen_7b").write_bytes(b"x")
                settings.Settings._update_llm_card_ui(fake, "qwen_7b")
            card.btn_remove.setVisible.assert_called_with(partial)
            card.btn_action.setText.assert_called_with(
                "Resume Download" if partial else "Download")


if __name__ == "__main__":
    unittest.main()
