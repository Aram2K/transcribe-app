"""Start Transcribe when the user logs in ("Startup apps" in Task Manager).

Windows: a value in HKCU\\...\\CurrentVersion\\Run, the same place Task Manager
lists and toggles. macOS: a LaunchAgent plist in ~/Library/LaunchAgents. Both
launch with ``--background`` (tray only, no Settings window).

``cfg["launch_at_login"]`` says whether the app keeps a login entry, and
:func:`reconcile` applies it on every launch, so a missing or stale (moved
install) entry heals itself. Whether that entry actually runs is the OS's
call: a switch-off in Task Manager is stored under StartupApproved, and we
leave it alone - the entry stays listed there as "Disabled", so the user can
switch it back on in the same place. Only the Settings checkbox (an explicit
choice in our own UI) clears it.

Who decides the first value:
* the installer's "start automatically" box on a fresh install - it leaves
  :data:`INSTALL_DEFAULT_NAME` next to the exe ("on"/"off" + a timestamp),
  adopted once per install by each user of the machine;
* an older version's Startup-folder shortcut, migrated with its Task Manager
  state;
* else on for a first run of the portable zip or the Mac app.

Only packaged builds register, and never from a place that won't exist at the
next login (a source checkout's python.exe, a zip opened straight from the
download, the mounted .dmg, a Gatekeeper translocation copy).
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

CFG_KEY = "launch_at_login"
# The install whose default this config already adopted (its marker text).
STAMP_KEY = "launch_at_login_install"
BACKGROUND_ARG = "--background"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
APPROVED_FOLDER_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"
SHELL_FOLDERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
VALUE_NAME = "Transcribe"
# What installers before 1.9.4 put in the Startup folder ({userstartup}\Transcribe).
LEGACY_SHORTCUT = "Transcribe.lnk"
# StartupApproved data: first byte even = enabled (0x02), odd = disabled (0x03);
# the other 11 bytes are flags and a timestamp Task Manager shows.
APPROVED_ENABLED = bytes([0x02] + [0] * 11)
APPROVED_DISABLED = bytes([0x03] + [0] * 11)
# Written next to the exe by the installer on a fresh install.
INSTALL_DEFAULT_NAME = "autostart.default"

MAC_LABEL = "xyz.aibuben.transcribe"


def _temp_dir():
    import tempfile
    return tempfile.gettempdir()


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def supported():
    """Only a packaged app registers itself (see module docstring)."""
    if not getattr(sys, "frozen", False):
        return False
    exe = sys.executable
    if IS_MAC:
        # Running straight from the mounted .dmg, or from Gatekeeper's
        # randomised translocation copy: either path is gone next login.
        return not exe.startswith("/Volumes/") and "/AppTranslocation/" not in exe
    if IS_WINDOWS:
        # A zip opened straight from Explorer runs from %TEMP%\Temp1_...zip,
        # which Storage Sense / Disk Cleanup deletes.
        return not _norm(exe).startswith(_norm(_temp_dir()) + os.sep)
    return False


def command():
    """The command line the OS runs at login."""
    return f'"{sys.executable}" {BACKGROUND_ARG}'


def _exe_in(value):
    """The exe path in a Run value like ``"C:\\x\\a.exe" --background``."""
    v = (value or "").strip()
    if v.startswith('"'):
        end = v.find('"', 1)
        return v[1:end] if end > 1 else None
    return v.split(" ")[0] or None


def is_installed_build(exe=None):
    """True for an Inno Setup install (its uninstaller sits next to the exe),
    False for the portable zip."""
    exe_dir = os.path.dirname(exe or sys.executable)
    try:
        return any(n.lower().startswith("unins") and n.lower().endswith(".exe")
                   for n in os.listdir(exe_dir))
    except OSError:
        return False


def _install_default_path():
    return os.path.join(os.path.dirname(sys.executable), INSTALL_DEFAULT_NAME)


def _take_install_default(cfg):
    """The installer's box, once per install: True / False, or None when there
    is no marker or this config already adopted it (it stays on disk - every
    user of an all-users install adopts it on their own first run)."""
    try:
        with open(_install_default_path(), encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    except OSError:
        return None
    choice = {"on": True, "off": False}.get(text.split(" ", 1)[0].lower())
    if choice is None or cfg.get(STAMP_KEY) == text:
        return None
    cfg[STAMP_KEY] = text
    return choice


# ── Windows ────────────────────────────────────────────────────────────────

def _winreg():
    import winreg
    return winreg


def _read_value(key_path, name):
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _approved_off(key_path, name):
    """True when Task Manager (or Settings > Apps > Startup) switched it off."""
    data = _read_value(key_path, name)
    return isinstance(data, (bytes, bytearray)) and len(data) > 0 and data[0] % 2 == 1


def _startup_folder():
    """The user's Startup folder - it follows Start Menu folder redirection,
    like the {userstartup} old installers wrote to."""
    try:
        redirected = _read_value(SHELL_FOLDERS_KEY, "Startup")
    except Exception:
        redirected = None
    if isinstance(redirected, str) and redirected.strip():
        return os.path.expandvars(redirected)
    return os.path.join(os.environ.get("APPDATA", ""),
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def _win_registered():
    return _read_value(RUN_KEY, VALUE_NAME)


def _win_write():
    winreg = _winreg()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, command())


def _win_set_approved(data):
    winreg = _winreg()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APPROVED_RUN_KEY) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_BINARY, data)


def _win_delete(key_path, name):
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass                                   # already gone


def _win_unregister():
    _win_delete(RUN_KEY, VALUE_NAME)
    _win_delete(APPROVED_RUN_KEY, VALUE_NAME)  # no stale "Disabled" for next time


def _owned_by_other_install(value):
    """A Run value pointing at an installed copy that still exists, while this
    is a portable copy: trying the zip once must not take the entry over."""
    exe = _exe_in(value)
    return bool(exe) and _norm(exe) != _norm(sys.executable) and os.path.exists(exe) \
        and is_installed_build(exe) and not is_installed_build()


def _legacy_path():
    return os.path.join(_startup_folder(), LEGACY_SHORTCUT)


def _shortcut_targets_me(path):
    """Whether the .lnk launches this exe - only then is it ours to migrate.
    A .lnk keeps its target path as text (ANSI and/or UTF-16)."""
    try:
        with open(path, "rb") as f:
            data = f.read().lower()
    except OSError:
        return False

    def found(text):
        for enc in ("utf-16-le", "mbcs", "utf-8"):
            try:
                if text.lower().encode(enc) in data:
                    return True
            except (LookupError, UnicodeError):
                pass
        return False
    if found(sys.executable):
        return True
    # The old installer made it for {app}\TranscribeApp.exe - this very
    # install; the name alone is enough when the full path didn't survive
    # an ANSI round trip (non-ASCII user names).
    return is_installed_build() and found(os.path.basename(sys.executable))


def _legacy_shortcut():
    """The old installer's shortcut, if it's there and ours to take."""
    path = _legacy_path()
    return path if os.path.exists(path) and _shortcut_targets_me(path) else None


def _migrate_legacy_shortcut():
    """Move the old installer's Startup-folder shortcut to the Run value, so
    the app never starts (or shows up) twice. Write-first: the new entry,
    with the shortcut's Task Manager state, is in place and read back before
    the shortcut goes. Returns None when there is nothing to migrate,
    "stuck" when it couldn't be done (everything left as it was - try again
    next launch), else "on" / "off"."""
    path = _legacy_shortcut()
    if not path:
        return None
    off = _approved_off(APPROVED_FOLDER_KEY, LEGACY_SHORTCUT)
    try:
        _win_write()
        if off:
            _win_set_approved(APPROVED_DISABLED)
        elif _approved_off(APPROVED_RUN_KEY, VALUE_NAME):
            _win_set_approved(APPROVED_ENABLED)    # a stale switch-off from before
        ok = (_win_registered() == command()
              and _approved_off(APPROVED_RUN_KEY, VALUE_NAME) == off)
    except Exception as e:
        logger.warning("Could not write the new startup entry: %s", e)
        ok = False
    if ok:
        try:
            os.remove(path)
        except OSError as e:
            logger.warning("Could not remove the old startup shortcut: %s", e)
            ok = False
    if not ok:
        try:
            _win_unregister()                  # never both at once
        except Exception:
            pass
        return "stuck"
    _win_delete(APPROVED_FOLDER_KEY, LEGACY_SHORTCUT)
    return "off" if off else "on"


# ── macOS ──────────────────────────────────────────────────────────────────

def _mac_plist_path():
    return os.path.expanduser(f"~/Library/LaunchAgents/{MAC_LABEL}.plist")


def _mac_bundle_id():
    """CFBundleIdentifier of the running .app (exe is Contents/MacOS/<name>)."""
    import plistlib
    info = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Info.plist")
    try:
        with open(info, "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier") or None
    except Exception:
        return None


def _mac_plist():
    import plistlib
    agent = {
        "Label": MAC_LABEL,
        "ProgramArguments": [sys.executable, BACKGROUND_ARG],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    bundle_id = _mac_bundle_id()
    if bundle_id:
        # System Settings > Login Items then shows it as Transcribe, not as
        # an unidentified background item.
        agent["AssociatedBundleIdentifiers"] = [bundle_id]
    return plistlib.dumps(agent)


def _mac_registered():
    try:
        with open(_mac_plist_path(), "rb") as f:
            return f.read()
    except OSError:
        return None


def _mac_write():
    path = _mac_plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_mac_plist())


def _mac_delete():
    try:
        os.remove(_mac_plist_path())
    except OSError:
        pass


# ── Public API ─────────────────────────────────────────────────────────────

def is_registered():
    """Whether the app has a login entry at all (Task Manager may still have
    it switched off) - what ``cfg[CFG_KEY]`` records."""
    if not supported():
        return False
    try:
        if IS_MAC:
            return _mac_registered() is not None
        return bool(_win_registered()) or _legacy_shortcut() is not None
    except Exception as e:
        logger.warning("autostart.is_registered: %s", e)
        return False


def is_enabled():
    """Whether the app will actually start at the next login. (macOS: the
    user can still block it in System Settings > Login Items, which apps
    can't read without SMAppService.)"""
    if not supported():
        return False
    try:
        if IS_MAC:
            return _mac_registered() is not None
        if _win_registered() and not _approved_off(APPROVED_RUN_KEY, VALUE_NAME):
            return True
        # An old shortcut not migrated yet still starts the app.
        return _legacy_shortcut() is not None and \
            not _approved_off(APPROVED_FOLDER_KEY, LEGACY_SHORTCUT)
    except Exception as e:
        logger.warning("autostart.is_enabled: %s", e)
        return False


def set_enabled(on):
    """The Settings checkbox: register (clearing a Task Manager switch-off -
    the user just asked for it in our UI) or unregister. Returns True when
    the OS now matches ``on``."""
    if not supported():
        return False
    try:
        if IS_MAC:
            _mac_write() if on else _mac_delete()
        else:
            # An old shortcut first, or the app would be listed/started twice.
            if _migrate_legacy_shortcut() == "stuck":
                return False
            if on:
                _win_write()
                if _approved_off(APPROVED_RUN_KEY, VALUE_NAME):
                    _win_set_approved(APPROVED_ENABLED)
            else:
                _win_unregister()
    except Exception as e:
        logger.warning("autostart.set_enabled(%s): %s", on, e)
        return False
    return is_enabled() == bool(on)


def _initial_choice(cfg):
    """``launch_at_login`` for a config that has never had it, with no install
    default to adopt: keep what an older version set up; a brand-new portable
    or Mac install defaults to on."""
    if IS_WINDOWS:
        if _win_registered():
            return True
        if is_installed_build():
            # Installed by a version too old to leave a default, and its
            # shortcut was handled by the migration - there was no entry.
            return False
    elif IS_MAC and _mac_registered() is not None:
        return True
    # Portable zip / Mac: on for a first run, off for someone who has used the
    # app for a while without it (they never asked for it).
    return not cfg.get("onboarding_done", False)


def reconcile(cfg):
    """Apply ``cfg[CFG_KEY]`` to the OS, deciding it first if unset. Returns
    True when ``cfg`` changed and should be saved."""
    if not supported():
        return False
    changed = False
    try:
        legacy = _migrate_legacy_shortcut() if IS_WINDOWS else None
        if legacy == "stuck":
            return False                       # never register alongside it
        choice = _take_install_default(cfg)
        if choice is not None:
            cfg[CFG_KEY] = choice              # the installer's box: the latest word
            changed = True
            if choice and IS_WINDOWS and _approved_off(APPROVED_RUN_KEY, VALUE_NAME):
                _win_set_approved(APPROVED_ENABLED)    # ticked on purpose
        elif legacy is not None:
            # The old shortcut is the latest thing the user set up (already
            # moved to the Run value, switched off if it was off).
            if not cfg.get(CFG_KEY):
                cfg[CFG_KEY] = True
                changed = True
        elif CFG_KEY not in cfg:
            cfg[CFG_KEY] = bool(_initial_choice(cfg))
            changed = True
        want = bool(cfg[CFG_KEY])
        if want:
            if IS_MAC:
                if _mac_registered() != _mac_plist():
                    _mac_write()               # missing, or the app moved
            else:
                current = _win_registered()
                if current != command() and not _owned_by_other_install(current):
                    _win_write()               # missing, or the app moved
        else:
            _mac_delete() if IS_MAC else _win_unregister()
    except Exception as e:
        logger.warning("autostart.reconcile: %s", e)
    return changed
