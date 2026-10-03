"""Optional NVIDIA GPU acceleration for local Whisper (Windows).

CTranslate2 - the engine under faster-whisper - can already SEE an NVIDIA GPU
in the packaged app, but running on it needs NVIDIA's cuBLAS libraries
(cublas64_12.dll + cublasLt64_12.dll, 736 MB), which the installer leaves out
so the download stays small for everyone else. Measured: 10 s of audio in
0.35 s on a GPU vs 6.8 s on a 20-core CPU.

This module finds whether they're needed, available, or already on the PC (a
CUDA Toolkit, a developer's pip packages, a folder on PATH, or our own earlier
download), and downloads them on request from NVIDIA's official package on
PyPI, pinned to a version verified with this app's CTranslate2 and checked
against its SHA-256. It also holds the policy for WHEN to suggest it: once up
front, then again only while the user is actually waiting on a model the GPU
would speed up - rarely, and never after "don't suggest this again".

No Qt here, so all of it is testable without a display.
"""
import errno
import hashlib
import logging
import os
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

import storage

logger = logging.getLogger("transcribe")

IS_WINDOWS = sys.platform == "win32"

# NVIDIA's official cuBLAS package (nvidia-cublas-cu12), the Windows wheel.
# Pinned: verified with ctranslate2 4.7 (CUDA 12) - requirements.txt keeps
# ctranslate2 below 5 so the CUDA major version can't drift away from it.
CUBLAS_VERSION = "12.9.2.10"
WHEEL_URL = ("https://files.pythonhosted.org/packages/20/e2/"
             "fc9a0e985249d873150276d5afb02e39a66817fedbf1a385724393e505ed/"
             "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl")
WHEEL_SHA256 = "623f43027d40d44ceadf0043f002bd25cf353e8f13ce90b9a87057019f560661"
WHEEL_SIZE = 553_162_896
# All Whisper loads on a GPU (measured): nvrtc and cuDNN are not needed.
DLLS = ("cublas64_12.dll", "cublasLt64_12.dll")
UNPACKED_SIZE = 771_191_808                      # the two DLLs on disk
DOWNLOAD_MB = round(WHEEL_SIZE / 2**20)          # what the UI quotes (~528 MB)

DIR_NAME = "gpu"
REMOVE_MARKER = "remove.pending"

# Models that are slow enough on a CPU for the GPU to matter. tiny/base are
# near-instant on any CPU - suggesting a 528 MB download for them is noise.
BENEFITS = {"small", "medium", "large-v3-turbo", "large-v3", "large-v2", "large",
            "distil-large-v3"}
# The contextual suggestion: only after waiting at least this long on the CPU,
# at most this often, and at most this many times in total.
SLOW_SECONDS = 2.0
OFFER_EVERY_S = 3 * 24 * 3600
MAX_OFFERS = 5

# cfg keys
CFG_DECLINED = "gpu_offer_never"       # "don't suggest this again"
CFG_LAST = "gpu_offer_last"            # epoch seconds of the last suggestion
CFG_COUNT = "gpu_offer_count"
CFG_INTRO = "gpu_offer_intro_done"     # the one up-front recommendation was made
CFG_FAILED = "gpu_failed_reason"       # the GPU was tried and couldn't run
CFG_KEYS = (CFG_DECLINED, CFG_LAST, CFG_COUNT, CFG_INTRO, CFG_FAILED)

_lock = threading.Lock()
_device_count = None                   # cached ctranslate2.get_cuda_device_count()
_dll_handles = []                      # os.add_dll_directory handles, kept alive


class GpuAccelError(RuntimeError):
    pass


def _base_dir():
    """Machine-specific and re-downloadable: Local AppData on Windows, never
    the roaming profile (736 MB would sync between PCs on a domain). The
    app-data override (TRANSCRIBE_APP_DATA_DIR - tests, portable setups) wins."""
    if IS_WINDOWS and not os.environ.get("TRANSCRIBE_APP_DATA_DIR"):
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "Transcribe" / DIR_NAME
    return storage.path_for(DIR_NAME)


def install_dir():
    return _base_dir() / "cuda12"


def is_oom(message):
    """A CUDA out-of-memory error: this model is too big for the card - not
    a reason to give up on the GPU for smaller models. (cuBLAS reports a full
    card as ALLOC_FAILED when its workspace doesn't fit.)"""
    m = str(message or "").lower()
    return ("out of memory" in m or "cudaerrormemoryallocation" in m
            or "alloc_failed" in m)


def is_cuda_error(message):
    """The error came from the GPU stack (CUDA, cuBLAS, cuDNN, the driver) -
    not from a damaged model cache, the network or our own code, which must
    never switch the GPU off."""
    m = str(message or "").lower()
    return is_oom(m) or any(k in m for k in ("cuda", "cublas", "cudnn", "cudart", "nvrtc", "nvidia"))


def _cuda_device_count():
    """NVIDIA GPUs CTranslate2 can see (driver installed). Cached: importing
    ctranslate2 costs ~0.3 s and the answer can't change while we run."""
    global _device_count
    with _lock:
        if _device_count is None:
            try:
                import ctranslate2
                _device_count = int(ctranslate2.get_cuda_device_count())
            except Exception:
                _device_count = 0
        return _device_count


def nvidia_gpu_present():
    return IS_WINDOWS and _cuda_device_count() > 0


def _has_dlls(directory):
    return bool(directory) and all(os.path.isfile(os.path.join(directory, d)) for d in DLLS)


def _toolkit_dirs():
    """cuBLAS from an installed CUDA Toolkit (CUDA_PATH, CUDA_PATH_V12_*). The
    DLL names carry the CUDA major version, so an 11.x or 13.x toolkit never
    counts as cuBLAS 12."""
    out = []
    for key, value in os.environ.items():
        k = key.upper()
        if value and (k == "CUDA_PATH" or k.startswith("CUDA_PATH_V12")):
            out.append(os.path.join(value, "bin"))
    return out


def _pip_dirs():
    """A developer's `pip install nvidia-cublas-cu12` (running from source)."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("nvidia.cublas")
        locs = getattr(spec, "submodule_search_locations", None) if spec else None
        return [os.path.join(list(locs)[0], "bin")] if locs else []
    except Exception:
        return []


def _path_dirs():
    """Last resort: cuBLAS 12 already on the DLL search path (a conda env, or
    DLLs copied next to System32 - common faster-whisper advice)."""
    return [p for p in os.environ.get("PATH", "").split(os.pathsep) if p.strip()]


def libs_dir():
    """Where usable cuBLAS libraries are, or None: our download first, then a
    CUDA Toolkit, pip packages, and PATH. Our folder never counts while its
    removal is pending (it's going away)."""
    skip = os.path.normcase(str(install_dir())) if removal_pending() else None
    for d in [str(install_dir())] + _toolkit_dirs() + _pip_dirs() + _path_dirs():
        if skip and os.path.normcase(d) == skip:
            continue
        if _has_dlls(d):
            return d
    return None


def status(cfg=None):
    """unsupported  - not Windows, or no NVIDIA GPU
       ready        - an NVIDIA GPU and cuBLAS on this PC
       available    - an NVIDIA GPU, cuBLAS missing: offer the download
       failed       - cuBLAS is here but the GPU couldn't run Whisper (old
                      driver) - see CFG_FAILED"""
    if not nvidia_gpu_present():
        return "unsupported"
    if libs_dir():
        return "failed" if (cfg or {}).get(CFG_FAILED) else "ready"
    return "available"


def register_dll_dirs():
    """Make the libraries findable before CTranslate2 first loads them. It
    loads cuBLAS lazily through the standard search order, so the folder goes
    on PATH too (add_dll_directory alone isn't honoured for delayed loads)."""
    if not IS_WINDOWS:
        return None
    finish_pending_remove()
    d = libs_dir()
    if not d:
        return None
    try:
        if hasattr(os, "add_dll_directory"):
            # The directory stays added only while its handle lives.
            _dll_handles.append(os.add_dll_directory(d))
    except Exception:
        pass
    path = os.environ.get("PATH", "")
    if os.path.normcase(d) not in [os.path.normcase(p) for p in path.split(os.pathsep)]:
        os.environ["PATH"] = d + os.pathsep + path
    return d


# ── download ───────────────────────────────────────────────────────────────

def _part_path():
    # Versioned: a later pinned version never resumes onto this one's bytes.
    return _base_dir() / f"nvidia_cublas-{CUBLAS_VERSION}.whl.part"


def _disk_full(err):
    return getattr(err, "errno", None) == errno.ENOSPC or getattr(err, "winerror", None) in (39, 112)


_DISK_FULL = ("Not enough free disk space for GPU acceleration - it needs about "
              "{mb} MB. Free up some space and try again.")
_INTERRUPTED = ("The download was interrupted. Check your connection and try again - "
                "it picks up where it stopped.")


def download(on_progress=None, should_cancel=None, _get=None):
    """Download NVIDIA's cuBLAS package, verify it, and unpack the two DLLs
    into install_dir(). Resumes a partial download, and never throws one away
    unless it's proven damaged. Raises GpuAccelError (with a message for the
    user) on failure or cancel. Returns the install dir.

    ``on_progress(percent, done_bytes, total_bytes)`` - from this thread."""
    import requests
    get = _get or requests.get
    cancelled = should_cancel or (lambda: False)
    base = _base_dir()
    base.mkdir(parents=True, exist_ok=True)
    part = _part_path()
    for old in base.glob("*.part"):                  # an older pinned version
        if old != part:
            try:
                old.unlink()
            except OSError:
                pass
    got = part.stat().st_size if part.exists() else 0
    if got > WHEEL_SIZE:
        part.unlink()
        got = 0
    need = (WHEEL_SIZE - got) + UNPACKED_SIZE + 64 * 2**20
    try:
        free = shutil.disk_usage(base).free
    except OSError:
        free = None
    if free is not None and free < need:
        raise GpuAccelError(_DISK_FULL.format(mb=round(need / 2**20)))

    if got < WHEEL_SIZE:
        headers = {"Accept-Encoding": "identity"}
        if got:
            headers["Range"] = f"bytes={got}-"
        try:
            with get(WHEEL_URL, stream=True, timeout=60, headers=headers,
                     allow_redirects=True) as resp:
                if resp.status_code != 416:          # 416: already complete
                    resp.raise_for_status()
                    hdr = {k.lower(): v for k, v in (resp.headers or {}).items()}
                    # Never let an unexpected answer (a Wi-Fi sign-in page,
                    # a proxy that ignored the range) overwrite good bytes.
                    if "text/html" in hdr.get("content-type", "").lower():
                        raise GpuAccelError("The download was redirected to a web page - "
                                            "are you signed in to this Wi-Fi? Then try again.")
                    if resp.status_code == 206:
                        if not hdr.get("content-range", "").startswith(f"bytes {got}-"):
                            raise GpuAccelError(_INTERRUPTED)
                    else:
                        length = hdr.get("content-length")
                        if length is not None and int(length) != WHEEL_SIZE:
                            raise GpuAccelError(_INTERRUPTED)
                        got = 0                      # the whole file, from the start
                    try:
                        with part.open("ab" if got else "wb") as f:
                            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                                if cancelled():
                                    raise GpuAccelError("Download cancelled.")
                                if not chunk:
                                    continue
                                f.write(chunk)
                                got += len(chunk)
                                if on_progress:
                                    on_progress(min(99, int(got * 100 / WHEEL_SIZE)),
                                                got, WHEEL_SIZE)
                    except OSError as e:
                        if _disk_full(e):
                            raise GpuAccelError(_DISK_FULL.format(
                                mb=round(need / 2**20))) from e
                        raise
        except GpuAccelError:
            raise
        except Exception as e:
            raise GpuAccelError(_INTERRUPTED) from e

    if cancelled():
        raise GpuAccelError("Download cancelled.")
    size = part.stat().st_size if part.exists() else 0
    if size < WHEEL_SIZE:
        raise GpuAccelError(_INTERRUPTED)            # kept: the next try resumes
    if size > WHEEL_SIZE or _sha256(part) != WHEEL_SHA256:
        part.unlink()
        raise GpuAccelError("The download was damaged and has been removed. Please try again.")
    if cancelled():
        raise GpuAccelError("Download cancelled.")

    dest = install_dir()
    staging = dest.with_name(dest.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(part) as z:
            # Only these two names are ever written (by basename): nothing in
            # the archive can choose a path.
            names = {os.path.basename(n): n for n in z.namelist()}
            for dll in DLLS:
                if dll not in names:
                    raise GpuAccelError(f"{dll} is missing from NVIDIA's package.")
                with z.open(names[dll]) as src, open(staging / dll, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
        if cancelled():
            raise GpuAccelError("Download cancelled.")
        if dest.exists():
            try:
                shutil.rmtree(dest)
            except OSError as e:
                raise GpuAccelError("The GPU files are in use. Restart Transcribe and "
                                    "try again.") from e
        staging.replace(dest)
    except GpuAccelError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(e, OSError) and _disk_full(e):
            raise GpuAccelError(_DISK_FULL.format(mb=round(UNPACKED_SIZE / 2**20))) from e
        raise GpuAccelError(f"Couldn't unpack the GPU libraries ({type(e).__name__}).") from e
    for leftover in (part, base / REMOVE_MARKER):     # a fresh install isn't going away
        try:
            leftover.unlink()
        except OSError:
            pass
    if on_progress:
        on_progress(100, WHEEL_SIZE, WHEEL_SIZE)
    logger.info("GPU acceleration installed (cuBLAS %s) in %s", CUBLAS_VERSION, dest)
    return dest


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def remove():
    """Delete our downloaded libraries. Windows keeps a loaded DLL locked, so
    after the GPU was used this run, the folder goes at the next start instead.
    Returns True if removed now, False if it's scheduled for the next start."""
    base = _base_dir()
    try:
        if base.exists():
            shutil.rmtree(base)
        return True
    except OSError:
        try:
            base.mkdir(parents=True, exist_ok=True)
            (base / REMOVE_MARKER).write_text("1", encoding="utf-8")
        except OSError:
            pass
        return False


def finish_pending_remove():
    """A removal scheduled last run: do it now, at startup, before anything
    looks for (or loads) the libraries. If they're still locked (another copy
    running), keep the marker for next time. Returns True if removed."""
    base = _base_dir()
    if not (base / REMOVE_MARKER).exists():
        return False
    try:
        if install_dir().exists():
            shutil.rmtree(install_dir())
    except OSError:
        return False
    shutil.rmtree(base, ignore_errors=True)
    return True


def removal_pending():
    return (_base_dir() / REMOVE_MARKER).exists()


# ── when to suggest it ─────────────────────────────────────────────────────

def can_offer(cfg, local_job=False):
    """The download is worth suggesting at all: an NVIDIA GPU without cuBLAS,
    local Whisper in use (dictation on a local model, or a job that always
    runs locally - file transcription), and the user never said "don't
    suggest this again"."""
    return (not cfg.get(CFG_DECLINED)
            and (local_job or (cfg.get("backend") or "local") == "local")
            and status(cfg) == "available")


def should_offer_now(cfg, model_name, seconds_waited, on_gpu=False, now=None, local_job=False):
    """After a local transcription: suggest the GPU when the user just waited
    on the CPU for a model it would speed up - at most every few days, a few
    times in total."""
    if on_gpu or model_name not in BENEFITS or seconds_waited < SLOW_SECONDS:
        return False
    if not can_offer(cfg, local_job=local_job):
        return False
    if int(cfg.get(CFG_COUNT) or 0) >= MAX_OFFERS:
        return False
    t = time.time() if now is None else now
    return t - float(cfg.get(CFG_LAST) or 0) >= OFFER_EVERY_S


def note_offered(cfg, now=None):
    cfg[CFG_LAST] = time.time() if now is None else now
    cfg[CFG_COUNT] = int(cfg.get(CFG_COUNT) or 0) + 1


def speedup_text():
    return "about 20× faster local transcription"
