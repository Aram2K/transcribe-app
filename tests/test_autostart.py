"""Start-at-login (autostart.py) against a fake registry / home folder, so it
runs on the Linux CI too."""
import os
import plistlib
import sys
from types import SimpleNamespace

import pytest

import autostart


class FakeWinreg:
    """The slice of winreg that autostart uses, backed by a dict."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_BINARY = 3

    def __init__(self):
        self.keys = {}                       # key path -> {name: value}
        self.fail_writes = set()             # key paths whose SetValueEx raises

    class _Key:
        def __init__(self, store, path):
            self.store = store
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        if path not in self.keys:
            raise OSError("no key")
        return self._Key(self.keys[path], path)

    def CreateKey(self, root, path):
        return self._Key(self.keys.setdefault(path, {}), path)

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise OSError("no value")
        return key.store[name], 0

    def SetValueEx(self, key, name, reserved, kind, value):
        if key.path in self.fail_writes:
            raise PermissionError("blocked by policy")
        key.store[name] = value

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise OSError("no value")
        del key.store[name]


DISABLED = bytes([0x03] + [0] * 11)


@pytest.fixture
def win(monkeypatch, tmp_path):
    reg = FakeWinreg()
    startup = tmp_path / "AppData" / "Startup"
    os.makedirs(startup)
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    exe = str(exe_dir / "TranscribeApp.exe")
    monkeypatch.setattr(autostart, "IS_WINDOWS", True)
    monkeypatch.setattr(autostart, "IS_MAC", False)
    monkeypatch.setattr(autostart, "_winreg", lambda: reg)
    monkeypatch.setattr(autostart, "_startup_folder", lambda: str(startup))
    monkeypatch.setattr(autostart, "_temp_dir", lambda: str(tmp_path / "systemp"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    reg.startup = startup
    reg.exe_dir = exe_dir
    reg.tmp = tmp_path
    return reg


def run_cmd(exe=None):
    return f'"{exe or sys.executable}" --background'


def run_value(reg):
    return reg.keys.get(autostart.RUN_KEY, {}).get(autostart.VALUE_NAME)


def approved(reg):
    return reg.keys.get(autostart.APPROVED_RUN_KEY, {}).get(autostart.VALUE_NAME)


def installed(reg):
    (reg.exe_dir / "unins000.exe").write_text("")


def install_default(reg, text):
    (reg.exe_dir / autostart.INSTALL_DEFAULT_NAME).write_text(text)


def legacy_lnk(reg, target=None):
    """A stand-in .lnk: real ones carry the target path as UTF-16 text."""
    lnk = reg.startup / autostart.LEGACY_SHORTCUT
    lnk.write_bytes(b"L\x00\x00\x00" + (target or sys.executable).encode("utf-16-le") + b"\x00\x00")
    return lnk


# ── packaging ──────────────────────────────────────────────────────────────

def test_source_checkout_never_registers(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cfg = {}
    assert autostart.reconcile(cfg) is False
    assert cfg == {}
    assert autostart.set_enabled(True) is False


def test_a_zip_opened_straight_from_the_download_never_registers(win, monkeypatch):
    monkeypatch.setattr(sys, "executable",
                        str(win.tmp / "systemp" / "Temp1_TranscribeApp-Windows.zip" / "TranscribeApp.exe"))
    cfg = {}
    assert autostart.reconcile(cfg) is False
    assert run_value(win) is None


# ── first run ──────────────────────────────────────────────────────────────

def test_portable_first_run_turns_on(win):
    cfg = {}
    assert autostart.reconcile(cfg) is True
    assert cfg["launch_at_login"] is True
    assert run_value(win) == run_cmd()
    assert autostart.is_enabled() and autostart.is_registered()


def test_portable_long_time_user_stays_off(win):
    cfg = {"onboarding_done": True}
    autostart.reconcile(cfg)
    assert cfg["launch_at_login"] is False
    assert run_value(win) is None


def test_installer_box_ticked_is_adopted_once(win):
    installed(win)
    install_default(win, "on 2026-09-30 10:00:00")
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd()}
    cfg = {}
    assert autostart.reconcile(cfg) is True
    assert cfg["launch_at_login"] is True
    assert autostart.is_enabled()
    # Adopted once: a later Settings "off" is not overridden by the same install.
    autostart.set_enabled(False)
    cfg["launch_at_login"] = False
    assert autostart.reconcile(cfg) is False
    assert run_value(win) is None


def test_installer_box_unticked_wins_over_an_old_config(win):
    # Reinstall over a config left by an earlier install that had it on.
    installed(win)
    install_default(win, "off 2026-09-30 10:00:00")
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd()}
    cfg = {"launch_at_login": True, "onboarding_done": True,
           "launch_at_login_install": "on 2026-01-01 09:00:00"}
    assert autostart.reconcile(cfg) is True
    assert cfg["launch_at_login"] is False
    assert run_value(win) is None


def test_all_users_install_registers_each_user_on_first_launch(win):
    # Admin-mode installs write no HKCU value (it could be another account's).
    installed(win)
    install_default(win, "on 2026-09-30 10:00:00")
    cfg = {}
    autostart.reconcile(cfg)
    assert run_value(win) == run_cmd()
    assert autostart.is_enabled()


def test_installer_box_ticked_clears_a_stale_task_manager_switch_off(win):
    installed(win)
    install_default(win, "on 2026-09-30 10:00:00")
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd()}
    win.keys[autostart.APPROVED_RUN_KEY] = {autostart.VALUE_NAME: DISABLED}
    autostart.reconcile({})
    assert autostart.is_enabled()


def test_installed_by_an_old_installer_without_value_stays_off(win):
    installed(win)
    cfg = {}
    autostart.reconcile(cfg)
    assert cfg["launch_at_login"] is False
    assert run_value(win) is None


def test_installer_written_value_is_kept(win):
    installed(win)
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd()}
    cfg = {}
    autostart.reconcile(cfg)
    assert cfg["launch_at_login"] is True
    assert run_value(win) == run_cmd()


# ── the old Startup-folder shortcut ────────────────────────────────────────

def test_legacy_shortcut_migrates_to_run_key(win):
    lnk = legacy_lnk(win)
    cfg = {"onboarding_done": True}
    autostart.reconcile(cfg)
    assert not lnk.exists()                  # never listed or started twice
    assert cfg["launch_at_login"] is True
    assert run_value(win) == run_cmd()
    assert autostart.is_enabled()


def test_legacy_shortcut_switched_off_moves_over_still_switched_off(win):
    lnk = legacy_lnk(win)
    win.keys[autostart.APPROVED_FOLDER_KEY] = {autostart.LEGACY_SHORTCUT: DISABLED}
    cfg = {"onboarding_done": True}
    autostart.reconcile(cfg)
    assert not lnk.exists()
    # Still listed in Task Manager (as Disabled), so it can be switched back on there.
    assert run_value(win) == run_cmd()
    assert approved(win)[0] % 2 == 1
    assert not autostart.is_enabled()
    assert autostart.is_registered()
    assert autostart.LEGACY_SHORTCUT not in win.keys[autostart.APPROVED_FOLDER_KEY]


def test_migration_keeps_the_shortcut_if_the_new_entry_cant_be_written(win):
    lnk = legacy_lnk(win)
    win.fail_writes.add(autostart.RUN_KEY)
    cfg = {}
    assert autostart.reconcile(cfg) is False
    assert lnk.exists()                      # it still starts the app, as before
    assert "launch_at_login" not in cfg
    assert autostart.is_enabled()            # via the shortcut


def test_migration_keeps_a_switched_off_shortcut_if_its_state_cant_be_carried(win):
    lnk = legacy_lnk(win)
    win.keys[autostart.APPROVED_FOLDER_KEY] = {autostart.LEGACY_SHORTCUT: DISABLED}
    win.fail_writes.add(autostart.APPROVED_RUN_KEY)
    autostart.reconcile({})
    assert lnk.exists()
    assert run_value(win) is None            # rolled back: never an enabled copy of it
    assert win.keys[autostart.APPROVED_FOLDER_KEY][autostart.LEGACY_SHORTCUT] == DISABLED


def test_legacy_shortcut_that_cannot_be_removed_changes_nothing(win, monkeypatch):
    lnk = legacy_lnk(win)
    win.keys[autostart.APPROVED_FOLDER_KEY] = {autostart.LEGACY_SHORTCUT: DISABLED}
    real_remove = os.remove

    def locked(path):
        if str(path).endswith(autostart.LEGACY_SHORTCUT):
            raise PermissionError("in use")
        real_remove(path)
    monkeypatch.setattr(autostart.os, "remove", locked)
    cfg = {}
    assert autostart.reconcile(cfg) is False
    assert lnk.exists()
    assert run_value(win) is None            # never registered alongside it
    # Its Task Manager switch-off must survive, or the shortcut would run again.
    assert win.keys[autostart.APPROVED_FOLDER_KEY][autostart.LEGACY_SHORTCUT] == DISABLED
    # And Settings can't create a second entry next to it.
    assert autostart.set_enabled(True) is False
    assert run_value(win) is None


def test_someone_elses_shortcut_is_left_alone(win):
    lnk = legacy_lnk(win, target=r"C:\Old\Transcribe\TranscribeApp.exe")   # portable run
    cfg = {}
    autostart.reconcile(cfg)
    assert lnk.exists()


# ── every launch ───────────────────────────────────────────────────────────

def test_stale_path_is_repaired(win):
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd(r"D:\old\TranscribeApp.exe")}
    cfg = {"launch_at_login": True}
    assert autostart.reconcile(cfg) is False
    assert run_value(win) == run_cmd()


def test_trying_the_portable_zip_keeps_the_installed_copys_entry(win):
    other = win.tmp / "Programs" / "Transcribe"
    other.mkdir(parents=True)
    (other / "TranscribeApp.exe").write_text("")
    (other / "unins000.exe").write_text("")
    theirs = run_cmd(str(other / "TranscribeApp.exe"))
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: theirs}
    autostart.reconcile({"launch_at_login": True})
    assert run_value(win) == theirs


def test_setting_off_removes_the_entry(win):
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd()}
    cfg = {"launch_at_login": False}
    autostart.reconcile(cfg)
    assert run_value(win) is None


def test_task_manager_switch_off_is_left_alone(win):
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd()}
    win.keys[autostart.APPROVED_RUN_KEY] = {autostart.VALUE_NAME: DISABLED}
    assert not autostart.is_enabled()
    assert autostart.is_registered()
    cfg = {"launch_at_login": True}
    assert autostart.reconcile(cfg) is False
    # Still listed (Disabled) - the user can switch it back on in Task Manager.
    assert run_value(win) == run_cmd()
    assert approved(win) == DISABLED
    assert not autostart.is_enabled()


def test_settings_tick_undoes_a_task_manager_switch_off(win):
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd()}
    win.keys[autostart.APPROVED_RUN_KEY] = {autostart.VALUE_NAME: DISABLED}
    assert autostart.set_enabled(True) is True
    assert autostart.is_enabled()


def test_a_failed_settings_tick_leaves_the_entry_registered(win):
    # What the Settings handler stores after a failure: is_registered().
    win.keys[autostart.RUN_KEY] = {autostart.VALUE_NAME: run_cmd()}
    win.keys[autostart.APPROVED_RUN_KEY] = {autostart.VALUE_NAME: DISABLED}
    win.fail_writes.add(autostart.APPROVED_RUN_KEY)
    assert autostart.set_enabled(True) is False
    assert autostart.is_registered() is True
    autostart.reconcile({"launch_at_login": autostart.is_registered()})
    assert run_value(win) == run_cmd()        # still listed in Task Manager


def test_settings_untick_unregisters_cleanly(win):
    autostart.set_enabled(True)
    win.keys[autostart.APPROVED_RUN_KEY] = {autostart.VALUE_NAME: DISABLED}
    assert autostart.set_enabled(False) is True
    assert run_value(win) is None
    assert approved(win) is None             # no stale "Disabled" for next time
    assert not autostart.is_enabled()


@pytest.mark.skipif(sys.platform != "win32", reason="%VAR% expansion is Windows-only")
def test_startup_folder_follows_redirection(monkeypatch):
    reg = FakeWinreg()
    reg.keys[autostart.SHELL_FOLDERS_KEY] = {"Startup": r"%HOMEDRIVE%\Redirected\Startup"}
    monkeypatch.setattr(autostart, "_winreg", lambda: reg)
    monkeypatch.setenv("HOMEDRIVE", "Z:")
    assert autostart._startup_folder() == r"Z:\Redirected\Startup"


def test_exe_is_read_back_from_a_run_value():
    assert autostart._exe_in(r'"C:\A B\TranscribeApp.exe" --background') == r"C:\A B\TranscribeApp.exe"
    assert autostart._exe_in(r"C:\x\a.exe --background") == r"C:\x\a.exe"
    assert autostart._exe_in("") is None


# ── macOS ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mac(monkeypatch, tmp_path):
    app = tmp_path / "Applications" / "TranscribeApp.app" / "Contents"
    (app / "MacOS").mkdir(parents=True)
    exe = str(app / "MacOS" / "TranscribeApp")
    monkeypatch.setattr(autostart, "IS_WINDOWS", False)
    monkeypatch.setattr(autostart, "IS_MAC", True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    plist = tmp_path / "LaunchAgents" / f"{autostart.MAC_LABEL}.plist"
    monkeypatch.setattr(autostart, "_mac_plist_path", lambda: str(plist))
    return SimpleNamespace(plist=plist, info=app / "Info.plist")


def test_mac_first_run_writes_launch_agent(mac):
    cfg = {}
    autostart.reconcile(cfg)
    assert cfg["launch_at_login"] is True
    data = plistlib.loads(mac.plist.read_bytes())
    assert data["ProgramArguments"] == [sys.executable, "--background"]
    assert data["RunAtLoad"] is True
    assert "AssociatedBundleIdentifiers" not in data          # no Info.plist here
    assert autostart.set_enabled(False) is True
    assert not mac.plist.exists()


def test_mac_launch_agent_names_the_app_for_login_items(mac):
    mac.info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "xyz.aibuben.TranscribeApp"}))
    autostart.reconcile({})
    data = plistlib.loads(mac.plist.read_bytes())
    assert data["AssociatedBundleIdentifiers"] == ["xyz.aibuben.TranscribeApp"]


def test_mac_running_from_dmg_does_not_register(mac, monkeypatch):
    monkeypatch.setattr(sys, "executable",
                        "/Volumes/Transcribe/TranscribeApp.app/Contents/MacOS/TranscribeApp")
    cfg = {}
    assert autostart.reconcile(cfg) is False
    assert not mac.plist.exists()


def test_mac_translocated_copy_does_not_register(mac, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/private/var/folders/x/AppTranslocation/"
                        "ABC/d/TranscribeApp.app/Contents/MacOS/TranscribeApp")
    assert autostart.supported() is False
