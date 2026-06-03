import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


APP_DIR_NAME = "Transcribe"
SECRET_SERVICE = "TranscribeApp"
GOOGLE_API_KEY_SECRET = "google_api_key"
ACTION_API_KEY_SECRET = "action_api_key"
MEETINGS_DIR_NAME = "meetings"


def app_data_dir():
    override = os.environ.get("TRANSCRIBE_APP_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        root = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        base = Path(root) / APP_DIR_NAME if root else Path.home() / "AppData" / "Roaming" / APP_DIR_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        root = os.environ.get("XDG_CONFIG_HOME")
        base = Path(root) / APP_DIR_NAME if root else Path.home() / ".config" / APP_DIR_NAME

    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        return Path.cwd()


def path_for(filename):
    return app_data_dir() / filename


def meetings_dir():
    """Root directory for per-meeting subfolders (one per recording)."""
    d = app_data_dir() / MEETINGS_DIR_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def new_meeting_dir(timestamp_iso):
    """Return a fresh per-meeting directory keyed by an ISO-8601 UTC stamp.

    The colons that ISO timestamps contain aren't legal in Windows folder
    names, so we replace them with dashes."""
    safe_stamp = timestamp_iso.replace(":", "-")
    d = meetings_dir() / safe_stamp
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def append_jsonl(path, record):
    """Atomically append a JSON record as one line to `path`. Used by the
    meeting recorder to durably persist each chunk as it completes, so a
    crash never loses already-transcribed segments."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except OSError:
        return False


def read_jsonl(path):
    """Read a JSONL file produced by `append_jsonl` into a list. Skips
    malformed lines rather than failing - chunk recovery should be robust
    even if the last write was partial."""
    out = []
    p = Path(path)
    if not p.exists():
        return out
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return out


def migrate_legacy_file(legacy_path, target_path):
    if os.environ.get("TRANSCRIBE_SKIP_MIGRATION"):
        return False

    src = Path(legacy_path)
    dst = Path(target_path)
    try:
        if not src.exists() or dst.exists():
            return False
        if src.resolve() == dst.resolve():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def read_json(path, default):
    path = Path(path)
    for candidate in (path, Path(f"{path}.bak")):
        try:
            with candidate.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data
        except (FileNotFoundError, json.JSONDecodeError, PermissionError, OSError):
            continue
    return copy.deepcopy(default)


def atomic_write_json(path, data, ensure_ascii=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = Path(f"{path}.bak")

    if path.exists():
        try:
            shutil.copy2(path, backup)
        except OSError:
            pass

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_secret(name):
    if os.environ.get("TRANSCRIBE_DISABLE_KEYRING"):
        return ""
    if sys.platform == "win32":
        return _windows_read_secret(name)
    if sys.platform == "darwin":
        return _mac_read_secret(name)
    return ""


def write_secret(name, value):
    if os.environ.get("TRANSCRIBE_DISABLE_KEYRING"):
        return False
    if sys.platform == "win32":
        return _windows_write_secret(name, value)
    if sys.platform == "darwin":
        return _mac_write_secret(name, value)
    return False


def _secret_target(name):
    return f"{SECRET_SERVICE}.{name}"


def _windows_read_secret(name):
    try:
        import ctypes
        import ctypes.wintypes as wt

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wt.DWORD),
                ("Type", wt.DWORD),
                ("TargetName", wt.LPWSTR),
                ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD),
                ("Attributes", wt.LPVOID),
                ("TargetAlias", wt.LPWSTR),
                ("UserName", wt.LPWSTR),
            ]

        cred_ptr = ctypes.POINTER(CREDENTIAL)()
        ok = ctypes.windll.advapi32.CredReadW(
            _secret_target(name),
            1,  # CRED_TYPE_GENERIC
            0,
            ctypes.byref(cred_ptr),
        )
        if not ok:
            return ""
        try:
            cred = cred_ptr.contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return blob.decode("utf-16le")
        finally:
            ctypes.windll.advapi32.CredFree(cred_ptr)
    except Exception:
        return ""


def _windows_write_secret(name, value):
    try:
        import ctypes
        import ctypes.wintypes as wt

        target = _secret_target(name)
        if not value:
            ctypes.windll.advapi32.CredDeleteW(target, 1, 0)
            return True

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wt.DWORD),
                ("Type", wt.DWORD),
                ("TargetName", wt.LPWSTR),
                ("Comment", wt.LPWSTR),
                ("LastWritten", wt.FILETIME),
                ("CredentialBlobSize", wt.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wt.DWORD),
                ("AttributeCount", wt.DWORD),
                ("Attributes", wt.LPVOID),
                ("TargetAlias", wt.LPWSTR),
                ("UserName", wt.LPWSTR),
            ]

        blob = value.encode("utf-16le")
        blob_buf = ctypes.create_string_buffer(blob)
        cred = CREDENTIAL()
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = SECRET_SERVICE
        return bool(ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0))
    except Exception:
        return False


def _mac_read_secret(name):
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", SECRET_SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _mac_write_secret(name, value):
    try:
        if not value:
            subprocess.run(
                ["security", "delete-generic-password", "-s", SECRET_SERVICE, "-a", name],
                capture_output=True,
                timeout=5,
            )
            return True
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", SECRET_SERVICE, "-a", name, "-w", value],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False
