"""Download and hand off an app update (the Windows installer) - with real
progress, a checksum check and a clean hand-off to a quiet install that
reopens the app by itself.

Before this, "Install update" downloaded ~90 MB with no window at all, then
the app vanished: it looked like nothing happened (or that it crashed), and
any failure - a locked leftover installer, a dropped connection - was only
written to the log. Each download now gets its own file, so a leftover
installer that's still open can't block the next one.

No Qt here (ui/update_dialog.py shows it; AppController wires them), so all
of it is testable without a display or a network.
"""
import glob
import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import uuid

logger = logging.getLogger("transcribe")

PROJECT_GITHUB_URL = "https://github.com/Aram2K/transcribe-app"
SETUP_NAME = "TranscribeApp-Windows-Setup.exe"
MAC_NAME = "TranscribeApp-Mac.dmg"
TEMP_PREFIX = "TranscribeApp-Update-"
MIN_INSTALLER_BYTES = 1024 * 1024          # anything smaller is an error page, not an installer
APP_ID = "{8E3B7C8A-9D54-4F61-9F6C-2E8C7F0A1B23}"     # installer.iss AppId
# Inno Setup: a quiet install (its own small progress window, no wizard
# pages, no Cancel - cancelling would leave the app closed). Never the
# fresh-install "start at login" task: an update keeps the user's setting.
INSTALL_ARGS = ["/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NOCANCEL", "/NORESTART",
                '/MERGETASKS="!startup"']
# How long the relauncher waits for the installer before reopening the app
# anyway - it never stays gone.
RELAUNCH_TIMEOUT_MIN = 20


class UpdateError(RuntimeError):
    """A failed update step, worded for the user."""


class UpdateCancelled(UpdateError):
    pass


def setup_url(tag):
    return f"{PROJECT_GITHUB_URL}/releases/download/{tag}/{SETUP_NAME}"


def mac_url(tag):
    return f"{PROJECT_GITHUB_URL}/releases/download/{tag}/{MAC_NAME}"


def releases_page():
    return f"{PROJECT_GITHUB_URL}/releases/latest"


def clean_old(directory=None):
    """Earlier downloads and install logs (and the old fixed name every
    version used to share). One still open - an installer that's running - is
    simply left alone."""
    directory = directory or tempfile.gettempdir()
    for path in (glob.glob(os.path.join(directory, TEMP_PREFIX + "*.exe"))
                 + glob.glob(os.path.join(directory, TEMP_PREFIX + "*.log"))
                 + [os.path.join(directory, SETUP_NAME)]):
        try:
            os.remove(path)
        except OSError:
            pass


def can_self_install(exe=None):
    """Only an installed copy (its uninstaller next to it) updates itself in
    place. The portable zip would get a separate, freshly installed copy
    instead - it's pointed to the download page."""
    if not getattr(sys, "frozen", False) and exe is None:
        return False
    folder = os.path.dirname(exe or sys.executable)
    try:
        return any(n.lower().startswith("unins") and n.lower().endswith(".exe")
                   for n in os.listdir(folder))
    except OSError:
        return False


def is_all_users_install(exe=None, registry=None):
    """Is THIS copy installed for all users (Program Files, or the all-users
    uninstall entry points at its folder)? Then the installer needs
    administrator rights - asked for up front, so a "No" leaves the app
    running instead of closed. Another account's all-users copy on the same PC
    doesn't count: updating that one would leave this one old forever.

    ``registry()`` -> {"HKCU": [install folders], "HKLM": [...]} (tests)."""
    exe = os.path.normcase(os.path.abspath(exe or sys.executable))
    folder = os.path.dirname(exe)
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(var)
        if root and exe.startswith(os.path.normcase(os.path.abspath(root)) + os.sep):
            return True
    try:
        found = (registry or _registry_install_folders)()
    except Exception:
        return False

    def here(paths):
        return any(os.path.normcase(os.path.abspath(p.rstrip("\\/"))) == folder
                   for p in paths if p)
    if here(found.get("HKCU", ())):
        return False
    return here(found.get("HKLM", ()))


def _registry_install_folders():
    """Where the per-user (HKCU) and all-users (HKLM) installs of this app
    are, from Inno's uninstall entries."""
    if sys.platform != "win32":
        return {}
    import winreg
    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"
    found = {"HKCU": [], "HKLM": []}
    for name, hive, views in (("HKCU", winreg.HKEY_CURRENT_USER, (0,)),
                              ("HKLM", winreg.HKEY_LOCAL_MACHINE,
                               (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY))):
        for view in views:
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                    for value in ("Inno Setup: App Path", "InstallLocation"):
                        try:
                            found[name].append(str(winreg.QueryValueEx(k, value)[0]))
                        except OSError:
                            pass
            except OSError:
                continue
    return found


def expected_sha256(tag, get):
    """The release's published checksum (<installer>.sha256), or "" when it
    can't be had - then the size and the installer's own checks still apply."""
    try:
        with get(setup_url(tag) + ".sha256", timeout=15,
                 headers={"User-Agent": "Transcribe-Updater"}) as resp:
            if resp.status_code != 200:
                return ""
            text = resp.text if isinstance(getattr(resp, "text", None), str) else ""
    except Exception:
        return ""
    for token in text.replace("\r", " ").replace("\n", " ").split():
        if re.fullmatch(r"[0-9a-fA-F]{64}", token.strip()):
            return token.strip().lower()
    return ""


def download(tag, on_progress=None, should_cancel=None, get=None, dest_dir=None):
    """Download ``tag``'s installer and verify it. Returns its path. Raises
    UpdateCancelled / UpdateError (worded for the user).

    ``on_progress(done_bytes, total_bytes)`` - total is 0 when unknown."""
    if get is None:
        import requests
        get = requests.get
    cancelled = should_cancel or (lambda: False)
    dest_dir = dest_dir or tempfile.gettempdir()
    clean_old(dest_dir)
    sha = expected_sha256(tag, get)
    safe_tag = re.sub(r"[^0-9A-Za-z._-]", "", str(tag)) or "latest"
    dest = os.path.join(dest_dir, f"{TEMP_PREFIX}{safe_tag}-{uuid.uuid4().hex[:8]}.exe")
    digest = hashlib.sha256()
    done = total = 0
    try:
        with get(setup_url(tag), stream=True, timeout=30,
                 headers={"User-Agent": "Transcribe-Updater"}) as resp:
            if resp.status_code == 404:
                raise UpdateError("This version's installer isn't on GitHub yet - "
                                  "try again in a few minutes.")
            resp.raise_for_status()
            total = int((resp.headers or {}).get("Content-Length") or 0)
            with open(dest, "wb") as out:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if cancelled():
                        raise UpdateCancelled("Update cancelled.")
                    if not chunk:
                        continue
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
    except UpdateError:
        _remove(dest)
        raise
    except OSError as e:
        _remove(dest)
        if cancelled():                       # the stall that made them cancel
            raise UpdateCancelled("Update cancelled.") from e
        if getattr(e, "errno", None) == 28 or getattr(e, "winerror", None) in (39, 112):
            raise UpdateError("There isn't enough free disk space for the update.") from e
        raise UpdateError(_network_message()) from e
    except Exception as e:
        _remove(dest)
        if cancelled():
            raise UpdateCancelled("Update cancelled.") from e
        raise UpdateError(_network_message()) from e
    if cancelled():
        _remove(dest)
        raise UpdateCancelled("Update cancelled.")
    if (total and done != total) or done < MIN_INSTALLER_BYTES:
        _remove(dest)
        raise UpdateError(_network_message())
    if sha and digest.hexdigest() != sha:
        _remove(dest)
        raise UpdateError("The download was damaged, so it wasn't installed. Please try again.")
    logger.info("Update %s downloaded (%d bytes, checksum %s)", tag, done,
                "verified" if sha else "not published")
    return dest


def _network_message():
    return "The download didn't finish - check your internet connection and try again."


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def new_log_path(directory=None):
    """A fresh install log for each attempt: the relauncher waits for THIS
    one to close, never an earlier run's."""
    return os.path.join(directory or tempfile.gettempdir(),
                        f"{TEMP_PREFIX}{uuid.uuid4().hex[:8]}.log")


def reset_child_env(environ):
    """Whatever this app starts that later starts a NEW Transcribe (the
    installer, the relauncher) must not hand it this bundle's private
    PyInstaller variables - PyInstaller's documented way to launch an
    independent instance."""
    for key in [k for k in environ if k.startswith("_PYI_") or k == "_MEIPASS2"]:
        environ.pop(key, None)
    environ["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environ


def launch_installer(path, all_users=False, log_path=None, relaunch=True, start=None,
                     environ=None):
    """Start the quiet install through the shell (os.startfile). For an
    all-users install, as administrator ("runas") so the Windows prompt comes
    now: a "No" raises here and the app stays open. Otherwise explicitly for
    this user only (/CURRENTUSER), so Inno never picks another account's
    all-users install instead. ``relaunch``: the installer reopens the app
    itself when it's done (installer.iss: RelaunchAfterUpdate) - for per-user
    installs only: an elevated Setup can't start it as the user. Raises
    UpdateError if Windows won't start it."""
    if sys.platform != "win32" and start is None:
        raise UpdateError("Updates install automatically on Windows only.")
    args = INSTALL_ARGS + [f'/LOG="{log_path or new_log_path()}"',
                           "/ALLUSERS" if all_users else "/CURRENTUSER"]
    if relaunch:
        args.append("/relaunch=1")
    # The shell hands this process's environment on to Setup and its relaunch.
    reset_child_env(os.environ if environ is None else environ)
    try:
        (start or os.startfile)(path, "runas" if all_users else "open", " ".join(args))
    except OSError as e:
        if getattr(e, "winerror", None) == 1223:
            raise UpdateError("Administrator permission is needed for it, and it wasn't "
                              "given.") from e
        raise UpdateError(f"Windows wouldn't start the installer ({e.strerror or e}).") from e


def _powershell():
    """By full path: CreateProcess doesn't search PowerShell's folder, so a
    trimmed PATH mustn't decide whether the app comes back."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.exists(path) else "powershell.exe"


# The watcher, in PowerShell. The paths come in through the environment, never
# inside the script text: no quoting can break it (PowerShell also ends a
# '...' string at typographic quotes - a folder like D’Souza). The deadline is
# in UTC, so a clock change never cuts it short.
RELAUNCH_SCRIPT = (
    "$log = $env:TRANSCRIBE_RELAUNCH_LOG; "
    "$end = [DateTime]::UtcNow.AddMinutes([int]$env:TRANSCRIBE_RELAUNCH_MINUTES); "
    "do { Start-Sleep -Seconds 2; "
    "$done = (Test-Path -LiteralPath $log) -and "
    "[bool](Select-String -LiteralPath $log -SimpleMatch 'Log closed.' -Quiet "
    "-ErrorAction SilentlyContinue) "
    "} until ($done -or ([DateTime]::UtcNow -gt $end)); "
    "Start-Process -FilePath $env:TRANSCRIBE_RELAUNCH_EXE -ArgumentList '--show-settings'"
)


def spawn_relauncher(exe, log_path, popen=None):
    """Start a tiny hidden watcher, as the user running the app, that reopens
    Transcribe once the installer has finished (its log ends with "Log
    closed.") - also after a FAILED install, so the app never just vanishes;
    and never elevated or as the administrator who approved the install,
    which Inno's own relaunch can't promise. Start it once the installer is
    running. Returns the process, or None if it couldn't be started (e.g.
    PowerShell blocked by policy)."""
    if sys.platform != "win32" and popen is None:
        return None
    env = reset_child_env(dict(os.environ))
    env.update(TRANSCRIBE_RELAUNCH_EXE=str(exe), TRANSCRIBE_RELAUNCH_LOG=str(log_path),
               TRANSCRIBE_RELAUNCH_MINUTES=str(RELAUNCH_TIMEOUT_MIN))
    cmd = [_powershell(), "-NoProfile", "-NonInteractive", "-Command", RELAUNCH_SCRIPT]
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    breakaway = 0x01000000                     # CREATE_BREAKAWAY_FROM_JOB: outlive the app
    popen = popen or subprocess.Popen
    for flags in (no_window | breakaway, no_window):
        try:
            return popen(cmd, creationflags=flags, close_fds=True, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        except OSError as e:
            logger.debug("Relauncher not started (flags %#x): %s", flags, e)
    logger.warning("Couldn't start the update relauncher")
    return None
