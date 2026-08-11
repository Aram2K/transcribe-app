# Transcribe App - PySide6 Entry Point and Logic Controller

import os
import sys
import threading
import time
import json
import math
import wave
import io
import struct
import webbrowser
import socket
import queue
import logging
import hashlib
import datetime
import shutil
import base64
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pyperclip
import ctypes
import psutil
from PIL import Image, ImageDraw

from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtGui import QIcon, QPixmap, QImage, QAction
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QMessageBox

import storage
import history as hist
import actions
import action_api
import local_llm
import telemetry
import auth
import entitlements
import text_cleanup
import vocabulary

# ── Version ───────────────────────────────────────────────────────────────────
APP_VERSION = "1.7.0"

# ── Managed cloud transcription (Pro moat) ────────────────────────────────────
MANAGED_PROXY_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/transcribe-proxy"
FEEDBACK_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/submit-feedback"
DELETE_ACCOUNT_URL = "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/delete-account"

# ── Monetization links (Stripe) ───────────────────────────────────────────────
PRO_MONTHLY_URL = "https://buy.stripe.com/3cI5kC30N1oeari7rh0Ba00"
PRO_ANNUAL_URL  = "https://buy.stripe.com/fZuaEW0SF4Aq1UMh1R0Ba01"
# Set this to your Stripe Customer Portal link (Billing → Customer portal) so Pro
# users can self-manage their subscription. Left blank until configured.
STRIPE_PORTAL_URL = ""
PROJECT_GITHUB_URL = "https://github.com/Aram2K/transcribe-app"
RELEASES_URL = "https://github.com/Aram2K/transcribe-app/releases/latest"
RELEASES_API = "https://api.github.com/repos/Aram2K/transcribe-app/releases/latest"
RELEASES_MANIFEST_URL = "https://github.com/Aram2K/transcribe-app/releases/latest/download/update-manifest.json"
AIBUBEN_URL = "https://aibuben.xyz"

SINGLE_INSTANCE_PORT = 47823   # localhost-only IPC for "open on second launch"

# ── Config ────────────────────────────────────────────────────────────────────
LOG_PATH = str(storage.path_for("transcribe.log"))
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("transcribe")

CONFIG_PATH = str(storage.path_for("config.json"))
LEGACY_CONFIG_PATH = "config.json"
ANALYTICS_CONSENT_PATH = storage.path_for("analytics_consent.accepted")
ANALYTICS_DECLINED_PATH = storage.path_for("analytics_consent.declined")

DEFAULT = {
    "hotkey":        "alt+r",
    "whisper_model": "base",
    "language":      "auto",
    "sample_rate":   16000,
    "chunk_size":    1024,
    "accent_color":  "#3b82f6",
    "backend":       "local",
    "google_api_key": "",
    "mistral_api_key": "",
    "initial_prompt": "",
    "output_action": "transcribe_only",
    "action_model": "rule_based",
    "action_api_provider": "openai_compatible",
    "action_api_key": "",
    "action_api_base_url": "",
    "action_api_model": "",
    "translate_target": "en",
    "input_device_index": None,
    "meeting_audio_mode": "smart_meeting",
    "silence_trigger_sec": 0.8,
    "min_speech_sec": 1.5,
    "max_speech_sec": 25.0,
    "privacy_mode": False,
    "save_history": True,
    "analytics_enabled": True,
    "analytics_consent_applied": False,
    "analytics_endpoint": "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/transcribe-analytics",
    "onboarding_done": False,
    "account_gate_seen": False,
    "admin_tier_override": "auto",  # super-admin only: auto|guest|free|pro
    "dismissed_update_version": "",
    "pending_update_version": "",
    "pending_update_body": "",
    "previous_version": "",
    "tray_hint_shown": False,
    "meeting_consent_ack": False,  # one-time recording-consent notice
    "macos_perms_guide_shown": False,  # one-time macOS permission walkthrough
    # Output-quality pass (text_cleanup.py). Runs on every dictation - the only
    # safety net between a Whisper hallucination and the user's cursor.
    "cleanup_enabled": True,
    "cleanup_strip_hallucinations": True,
    "cleanup_strip_artifacts": True,
    "cleanup_remove_fillers": False,   # OFF: it changes the user's words
    "cleanup_replacements": [],        # [{"from": "pyside 6", "to": "PySide6"}]
    "cleanup_custom_hallucinations": [],
    # Custom vocabulary (vocabulary.py). "initial_prompt" above is kept as a
    # derived mirror of this list so the local decode path needs no changes.
    "vocabulary": [],
    "vocabulary_share_with_cloud": True,
    # Put the user's clipboard back after we paste.
    "restore_clipboard": True,
    "clipboard_restore_delay_ms": 400,
    # Bumped when the config shape changes in a way that needs migration.
    "config_schema_version": 1,
}

storage.migrate_legacy_file(LEGACY_CONFIG_PATH, CONFIG_PATH)

# Mistral's /audio/transcriptions endpoint only accepts the Voxtral Mini
# Transcribe model. "voxtral-small/large-latest" are NOT valid there (Small is a
# chat model; there is no Large), which is why selecting them returned
# "Invalid model". We normalize any stale/invalid choice to the working alias.
# Defined above load_config() because load_config() calls it at import time.
MISTRAL_STT_DEFAULT = "voxtral-mini-latest"
MISTRAL_STT_MODELS = {"voxtral-mini-latest", "voxtral-mini-2602", "voxtral-mini-2507"}


def normalize_mistral_model(model):
    return model if model in MISTRAL_STT_MODELS else MISTRAL_STT_DEFAULT


# BYO key fields that must never sit in plaintext config.json (they live in the
# OS keyring; config keeps "" once migrated).
_SECRET_CFG_KEYS = ("google_api_key", "action_api_key", "mistral_api_key")


def _strip_user_secret_keys(store):
    """Split cfg['user_secrets'] into (sanitized_store_for_disk, keys_blob).
    The blob ({owner: {key_field: value}}) goes to the OS keyring; the on-disk
    store keeps everything else (engine choices) with the key fields blanked."""
    if not isinstance(store, dict):
        return store, {}
    sanitized, blob = {}, {}
    for owner, snap in store.items():
        if not isinstance(snap, dict):
            sanitized[owner] = snap
            continue
        clean = dict(snap)
        keys = {}
        for k in _SECRET_CFG_KEYS:
            v = clean.get(k)
            if isinstance(v, str) and v.strip():
                keys[k] = v
            clean[k] = ""
        sanitized[owner] = clean
        if keys:
            blob[owner] = keys
    return sanitized, blob


def load_config():
    data = storage.read_json(CONFIG_PATH, DEFAULT)
    if not isinstance(data, dict):
        data = {}
    loaded = {**DEFAULT, **data}
    # Keep initial_prompt in step with the structured vocabulary list, so a
    # hand-edited or older config self-heals on load.
    vocabulary.sync_config(loaded)

    key = (loaded.get("google_api_key") or "").strip()
    if key:
        if storage.write_secret(storage.GOOGLE_API_KEY_SECRET, key):
            loaded["google_api_key"] = key
            disk = {**loaded, "google_api_key": ""}
            try:
                storage.atomic_write_json(CONFIG_PATH, disk)
            except OSError as e:
                logger.warning("Could not sanitize API key in config: %s", e)
    else:
        loaded["google_api_key"] = storage.read_secret(storage.GOOGLE_API_KEY_SECRET)

    if loaded.get("privacy_mode"):
        loaded["backend"] = "local"
        # Privacy Mode only blocks the cloud; local history stays on the device
        # under the user's own "Save local transcription history" setting.
        if actions.ACTION_MODELS.get(actions.normalize_action_model(loaded.get("action_model")), {}).get("kind") == "cloud":
            loaded["action_model"] = actions.RULE_BASED_ID
    loaded["action_model"] = actions.normalize_action_model(loaded.get("action_model"))
    # Heal any stale/invalid Mistral STT choice (e.g. the removed voxtral-small).
    if loaded.get("mistral_stt_model"):
        loaded["mistral_stt_model"] = normalize_mistral_model(loaded["mistral_stt_model"])

    # Output mode stays on "transcribe_only" by default. Smart Actions are a
    # Pro feature and must be turned on explicitly - we never auto-enable them.

    action_key = (loaded.get("action_api_key") or "").strip()
    if action_key:
        if storage.write_secret(storage.ACTION_API_KEY_SECRET, action_key):
            loaded["action_api_key"] = action_key
            disk = {**loaded, "google_api_key": "", "action_api_key": ""}
            try:
                storage.atomic_write_json(CONFIG_PATH, disk)
            except OSError as e:
                logger.warning("Could not sanitize action API key in config: %s", e)
    else:
        loaded["action_api_key"] = storage.read_secret(storage.ACTION_API_KEY_SECRET)

    mistral_key = (loaded.get("mistral_api_key") or "").strip()
    if mistral_key:
        # Migrate a plaintext Mistral key from config.json into the OS keyring.
        if storage.write_secret(storage.MISTRAL_API_KEY_SECRET, mistral_key):
            loaded["mistral_api_key"] = mistral_key
            disk = {**loaded, "google_api_key": "", "action_api_key": "", "mistral_api_key": ""}
            try:
                storage.atomic_write_json(CONFIG_PATH, disk)
            except OSError as e:
                logger.warning("Could not sanitize Mistral API key in config: %s", e)
    else:
        loaded["mistral_api_key"] = storage.read_secret(storage.MISTRAL_API_KEY_SECRET)

    # Per-user stashed keys (account switching) live in the keyring as one JSON
    # blob; merge them back over the sanitized on-disk snapshots.
    try:
        blob_raw = storage.read_secret(storage.USER_SECRETS_SECRET)
        if blob_raw:
            blob = json.loads(blob_raw)
            store = loaded.get("user_secrets")
            if isinstance(blob, dict) and isinstance(store, dict):
                for owner, keys in blob.items():
                    snap = store.get(owner)
                    if isinstance(snap, dict) and isinstance(keys, dict):
                        for k, v in keys.items():
                            if k in _SECRET_CFG_KEYS and v and not snap.get(k):
                                snap[k] = v
    except Exception:
        logger.debug("Could not merge user secrets from keyring", exc_info=True)

    if ANALYTICS_CONSENT_PATH.exists() and not loaded.get("analytics_consent_applied"):
        if ANALYTICS_DECLINED_PATH.exists():
            loaded["analytics_enabled"] = False
        loaded["analytics_consent_applied"] = True
        disk = {**loaded, "google_api_key": "", "action_api_key": ""}
        try:
            storage.atomic_write_json(CONFIG_PATH, disk)
        except OSError as e:
            logger.warning("Could not record installer analytics consent: %s", e)

    return loaded

def save_config(c):
    disk = {**c}
    if disk.get("privacy_mode"):
        disk["backend"] = "local"
        # Local history is independent of Privacy Mode (it never leaves the device).
        if actions.ACTION_MODELS.get(actions.normalize_action_model(disk.get("action_model")), {}).get("kind") == "cloud":
            disk["action_model"] = actions.RULE_BASED_ID
    key = (disk.get("google_api_key") or "").strip()
    if storage.write_secret(storage.GOOGLE_API_KEY_SECRET, key):
        disk["google_api_key"] = ""
    action_key = (disk.get("action_api_key") or "").strip()
    if storage.write_secret(storage.ACTION_API_KEY_SECRET, action_key):
        disk["action_api_key"] = ""
    mistral_key = (disk.get("mistral_api_key") or "").strip()
    if storage.write_secret(storage.MISTRAL_API_KEY_SECRET, mistral_key):
        disk["mistral_api_key"] = ""
    # Per-user stashed keys (account switching) go to the keyring too - the
    # on-disk store keeps only the non-secret engine config per user.
    sanitized_store, keys_blob = _strip_user_secret_keys(disk.get("user_secrets"))
    try:
        if storage.write_secret(storage.USER_SECRETS_SECRET, json.dumps(keys_blob)):
            disk["user_secrets"] = sanitized_store
    except Exception:
        logger.debug("Could not stash user secrets in keyring", exc_info=True)
    storage.atomic_write_json(CONFIG_PATH, disk)

cfg = load_config()

def resource_path(*parts):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = base.joinpath(*parts)
    if candidate.exists():
        return str(candidate)
    return str(Path(__file__).resolve().parent.joinpath(*parts))

# ── Single-instance IPC ──────────────────────────────────────────────────────
def acquire_single_instance_lock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        sock.listen(5)
        return sock
    except OSError:
        sock.close()
        return None

def signal_running_instance(action="show_settings"):
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=1) as s:
            s.settimeout(1)
            s.sendall(action.encode("utf-8"))
            return s.recv(32) == b"transcribe-ok"
    except OSError:
        return False

def start_ipc_server(server_sock, on_action):
    def _accept():
        while True:
            try:
                conn, _ = server_sock.accept()
                data = conn.recv(64).decode("utf-8", errors="ignore").strip()
                if data in ("show_settings", "show_onboarding"):
                    conn.sendall(b"transcribe-ok")
                conn.close()
                if data:
                    on_action(data)
            except Exception:
                break
    threading.Thread(target=_accept, daemon=True).start()

# ── System Info ───────────────────────────────────────────────────────────────
RAM_GB = psutil.virtual_memory().total / (1024 ** 3)

# speed_rank is a relative compute cost (1 = fastest). The UI turns it into a
# plain-language estimate adjusted for the user's hardware (GPU vs CPU), instead
# of a fixed "~Ns" that is meaningless across machines. min_ram is a realistic,
# monotonic minimum-recommended system RAM for the int8 runtime.
MODELS = {
    "tiny":           {"min_ram": 2,  "speed_rank": 1, "quality": "Good",        "size": "75 MB",   "armenian": None},
    "base":           {"min_ram": 2,  "speed_rank": 2, "quality": "Better",      "size": "140 MB",  "armenian": None},
    "small":          {"min_ram": 4,  "speed_rank": 3, "quality": "Great",       "size": "460 MB",  "armenian": None},
    "medium":         {"min_ram": 6,  "speed_rank": 5, "quality": "Excellent",   "size": "1.4 GB",  "armenian": None},
    "large-v3-turbo": {"min_ram": 6,  "speed_rank": 4, "quality": "Best (fast)", "size": "1.6 GB",  "armenian": "Recommended for Armenian"},
    "large-v3":       {"min_ram": 8,  "speed_rank": 6, "quality": "Best",        "size": "3 GB",    "armenian": None},
}

LANG_NAMES = {
    "auto":  "Auto-detect",
    "multi": "Multilingual",
    "hy":    "Armenian",
    "en":    "English",
    "ru":    "Russian",
    "fr":    "French",
    "de":    "German",
    "es":    "Spanish",
    "ar":    "Arabic",
}

# Languages Mistral Voxtral transcription accepts (ISO-639-1). Anything else -
# including our "auto"/"multi" pseudo-codes - is omitted so we never send an
# unsupported code, which Voxtral rejects with a 400 (this was the cause of cloud
# transcription failing even when the API key tested fine).
MISTRAL_LANGS = {"en", "es", "fr", "de", "it", "nl", "pt", "hi", "ar", "ru",
                 "uk", "pl", "tr", "ja", "ko", "zh", "ro", "cs", "el"}


def cloud_error_message(provider, status, body_text):
    """Turn a cloud provider's HTTP error into one clear, user-facing sentence."""
    msg = ""
    try:
        j = json.loads(body_text)
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                msg = err.get("message", "") or ""
            elif isinstance(err, str):
                msg = err
            if not msg:
                msg = j.get("message") or j.get("detail") or ""
            if isinstance(msg, (dict, list)):
                msg = str(msg)
    except Exception:
        msg = ""
    msg = (msg or (body_text or "")).strip()
    low = msg.lower()
    if (
        status in (401, 403)
        or "unauthorized" in low
        or "invalid api key" in low
        or "api key not valid" in low
        or ("api key" in low and "not valid" in low)
        or ("api key" in low and "invalid" in low)
    ):
        return f"{provider} API key is invalid or unauthorized. Check it in Settings."
    if status == 402 or "insufficient" in low or "out of credit" in low or "no credit" in low or "billing" in low:
        return f"Your {provider} account is out of credits. Add billing/credits to keep using it."
    if status == 429 or "rate limit" in low or "quota" in low or "resource_exhausted" in low or "exhausted" in low:
        return f"{provider} rate limit or quota reached. Wait a moment and try again."
    if status == 400 and "language" in low:
        return f"{provider} does not support the selected language. Pick another or use Auto-detect."
    if status == 400:
        return f"{provider} rejected the request: {msg[:90]}" if msg else f"{provider}: bad request."
    return (f"{provider} error (HTTP {status}). {msg[:90]}").strip()


def model_ok(name):
    return RAM_GB >= MODELS[name]["min_ram"]

def _looks_like_whisper_cache_error(err):
    low = str(err).lower()
    return (
        "model.bin" in low
        or ("unable to open file" in low and "model" in low)
        or ("no such file" in low and "model" in low)
    )

def _friendly_transcription_error(err):
    if _looks_like_whisper_cache_error(err):
        return (
            "Local Whisper model files are incomplete. Open Settings > Models "
            "and redownload the selected Whisper model."
        )
    return str(err)

def _materialize_hf_snapshot_links(model_dir):
    """Replace HF snapshot symlinks with real files when possible.

    Some Windows installs leave a snapshot that resolves through symlinks which
    CTranslate2 then fails to open as model.bin. Copying the blob into the
    snapshot makes the local model usable without deleting the whole cache.
    """
    try:
        model_dir = Path(model_dir)
        if not model_dir.exists():
            return False
        changed = False
        for item in model_dir.iterdir():
            if not item.is_symlink():
                continue
            try:
                raw_target = os.readlink(item)
                target = Path(raw_target)
                if not target.is_absolute():
                    target = (item.parent / target).resolve()
                if not target.exists() or not target.is_file():
                    continue
                tmp = item.with_name(f".{item.name}.materializing")
                shutil.copy2(target, tmp)
                item.unlink()
                os.replace(tmp, item)
                changed = True
            except Exception as e:
                logger.debug("Could not materialize HF cache link %s: %s", item, e)
        return changed
    except Exception as e:
        logger.debug("HF cache materialization failed for %s: %s", model_dir, e)
        return False

def _repair_whisper_model_cache(name):
    try:
        from faster_whisper.utils import download_model
        path = Path(download_model(name, local_files_only=False))
        _materialize_hf_snapshot_links(path)
        model_bin = path / "model.bin"
        return model_bin.exists() and model_bin.stat().st_size > 1024
    except Exception as e:
        logger.warning("Could not repair Whisper model cache for %s: %s", name, e)
        return False

def model_downloaded(name):
    try:
        from faster_whisper.utils import download_model
        path = Path(download_model(name, local_files_only=True))
        model_bin = path / "model.bin"
        return model_bin.exists() and model_bin.stat().st_size > 1024
    except Exception:
        return False

def download_whisper_model(name, on_progress=None):
    if on_progress is None:
        from faster_whisper.utils import download_model
        return download_model(name)

    import huggingface_hub
    from tqdm.auto import tqdm
    import faster_whisper.utils as fw_utils

    model_map = getattr(fw_utils, "_MODELS", {})
    repo_id = model_map.get(name, name)
    if "/" not in repo_id and name not in model_map:
        from faster_whisper.utils import download_model
        return download_model(name)

    allow_patterns = [
        "config.json",
        "preprocessor_config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.*",
    ]

    progress_lock = threading.Lock()
    bars = {}
    totals = {"done": 0, "expected": 0, "last_emit": 0.0}

    def _emit(force=False):
        now = time.time()
        if not force and now - totals["last_emit"] < 0.2:
            return
        totals["last_emit"] = now
        with progress_lock:
            active_done = sum(max(0, v.get("n") or 0) for v in bars.values())
            active_expected = sum(max(0, v.get("total") or 0) for v in bars.values())
            done = totals["done"] + active_done
            expected = totals["expected"] + active_expected
        percent = None
        if expected > 0:
            percent = max(0, min(99, int(done * 100 / expected)))
        try:
            on_progress(percent, done, expected)
        except Exception:
            pass

    class ProgressTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["file"] = io.StringIO()
            kwargs["disable"] = False
            super().__init__(*args, **kwargs)
            with progress_lock:
                bars[id(self)] = {"n": self.n or 0, "total": self.total or 0}
            _emit(force=True)

        def update(self, n=1):
            result = super().update(n)
            with progress_lock:
                bars[id(self)] = {"n": self.n or 0, "total": self.total or 0}
            _emit()
            return result

        def close(self):
            with progress_lock:
                item = bars.pop(id(self), None)
                if item:
                    done = max(item.get("n") or 0, item.get("total") or 0)
                    totals["done"] += done
                    totals["expected"] += max(item.get("total") or done, done)
            try:
                super().close()
            finally:
                _emit(force=True)

    on_progress(0, 0, 0)
    kwargs = {
        "local_files_only": False,
        "allow_patterns": allow_patterns,
        "tqdm_class": ProgressTqdm,
        "max_workers": 8,
    }
    try:
        path = huggingface_hub.snapshot_download(repo_id, **kwargs)
    except TypeError as e:
        if "max_workers" not in str(e):
            raise
        kwargs.pop("max_workers", None)
        path = huggingface_hub.snapshot_download(repo_id, **kwargs)
    on_progress(100, totals["expected"], totals["expected"])
    return path

def whisper_model_cache_dir(name):
    try:
        from pathlib import Path
        from faster_whisper.utils import download_model
        path = Path(download_model(name, local_files_only=True)).resolve()
        for candidate in (path, *path.parents):
            if candidate.name.startswith("models--"):
                return candidate
        return path
    except Exception:
        return None

def remove_whisper_model(name):
    import os, stat, shutil
    cache_dir = whisper_model_cache_dir(name)
    if not cache_dir or not cache_dir.exists():
        return False

    def _onerror(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            raise

    shutil.rmtree(cache_dir, onerror=_onerror)
    return True

# Detect CUDA GPU via ctranslate2
HAS_GPU = False
try:
    import ctranslate2 as _ct2
    HAS_GPU = _ct2.get_cuda_device_count() > 0
except Exception:
    pass

# ── Audio Recorder ────────────────────────────────────────────────────────────
SILENCE_TRIGGER_SEC = 0.8
MIN_SPEECH_SEC      = 1.5
MAX_SPEECH_SEC      = 25

# Dynamically import PyAudio or PyAudioWPatch
try:
    import pyaudiowpatch as pyaudio
    HAS_LOOPBACK = True
except ImportError:
    import pyaudio
    HAS_LOOPBACK = False

def cfg_float(name, default, minimum=None, maximum=None):
    try:
        value = float(cfg.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value

class AudioRecorder:
    def __init__(self):
        self.recording        = False
        self.stop_requested   = False
        self._record_thread   = None
        self.audio            = pyaudio.PyAudio()
        self._model           = None
        self._model_name      = None
        self._model_lock      = threading.Lock()
        # Serializes actual transcribe() inference. A single WhisperModel is NOT
        # safe for concurrent calls; the chunked pipeline runs several chunk
        # threads at once, which hangs/crashes on CUDA (CPU only tolerated it by
        # luck). One global inference lock fixes the GPU hang.
        self._infer_lock      = threading.Lock()
        
        self.on_levels        = lambda lvls: None
        self.on_finalising    = lambda done, total: None
        self.on_chunk_complete = lambda idx, text, lang: None
        self.on_lang_detected = lambda code, name: None
        self.on_partial       = lambda text: None
        # Returns the signed-in user's Supabase access token (set by AppController);
        # used by the managed cloud backend to authenticate to the proxy.
        self.get_auth_token   = None
        
        self._chunk_frames    = []
        self._chunk_results   = {}
        self._chunk_idx       = 0
        self._chunk_lock      = threading.Lock()
        self._samples_in_chunk= 0
        self._session_lang    = None
        self._chunk_threads   = []
        self._chunk_errors    = []
        self._chunk_silence_before = {}
        self._record_error    = ""
        self._abort           = False   # cooperative cancel: transcribe() bails fast when set (Esc)
        self._cloud_capped    = False   # set when managed cloud hit its cap and we fell back to local
        # Rolling peak amplitude for adaptive bar normalization. Decays slowly
        # so bars stay calibrated to recent mic activity (handles mics with
        # very different gain levels - laptop mic vs. headset vs. far-field).
        self._level_peak      = 0.05
        # Per-session language override (the meeting window's language
        # selector). None = use the global cfg["language"] default.
        self.language_override = None
        self._capture_mode = None     # smart_meeting/system_only/default_mic; None = dictation
        self._loopback_peak = 0.0     # loudest loopback sample this session (0 = no system audio)

    @staticmethod
    def _whisper_device():
        """Prefer a CUDA GPU when one is usable (much faster); else CPU int8.
        int8_float16 keeps GPU VRAM low so it fits on small cards too."""
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return ("cuda", "int8_float16")
        except Exception:
            pass
        return ("cpu", "int8")

    @staticmethod
    def _add_cuda_dll_dirs():
        """Make CUDA libs (cuBLAS/cuDNN) from the `nvidia-*-cu12` pip packages
        discoverable on Windows, so GPU works after a simple
        `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` - no system CUDA needed.
        No-op if the packages aren't installed."""
        if sys.platform != "win32":
            return
        try:
            import importlib.util
            for pkg in ("nvidia.cublas", "nvidia.cudnn"):
                spec = importlib.util.find_spec(pkg)
                locs = getattr(spec, "submodule_search_locations", None) if spec else None
                if not locs:
                    continue
                binp = os.path.join(list(locs)[0], "bin")
                if not os.path.isdir(binp):
                    continue
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(binp)
                    except Exception:
                        pass
                # ctranslate2 loads cuBLAS/cuDNN lazily via the standard search
                # order, so the bin dir must also be on PATH (add_dll_directory
                # alone isn't honored for its delayed loads).
                if binp not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = binp + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            pass

    def load_model(self, name=None):
        name = name or cfg["whisper_model"]
        with self._model_lock:
            if self._model is None or self._model_name != name:
                from faster_whisper import WhisperModel
                dev, ct = self._whisper_device()
                # Try GPU first (if available), then CPU; and online first, then
                # offline cache (HF revalidation can fail on flaky networks even
                # when the model is fully cached). A GPU load can fail when the
                # CUDA libraries are missing or VRAM is short - we fall back to CPU
                # rather than erroring, so dictation always works.
                # Use all physical CPU cores (ctranslate2 otherwise caps at a low
                # default), so transcription runs as fast as the machine allows.
                try:
                    import psutil
                    cpu_threads = psutil.cpu_count(logical=False) or 0
                except Exception:
                    cpu_threads = os.cpu_count() or 0
                # Once we learn the CUDA libs are missing, don't keep retrying GPU
                # on every model switch - go straight to CPU.
                if getattr(self, "_cuda_usable", None) is False:
                    dev = "cpu"
                if dev == "cuda":
                    self._add_cuda_dll_dirs()
                attempts = []
                if dev == "cuda":
                    attempts += [("cuda", ct, False), ("cuda", ct, True)]
                attempts += [("cpu", "int8", False), ("cpu", "int8", True)]
                last_err = None
                repaired_cache = False
                for d, c, local_only in attempts:
                    # GPU already proven unusable earlier in THIS load (its warm-up
                    # raised) - skip the remaining CUDA attempt. Building a second
                    # CUDA model would skip the warm-up below and hand back a broken
                    # GPU model that hangs on real inference - the "stuck on
                    # Finalising" bug on machines where cuBLAS/cuDNN are missing.
                    if d == "cuda" and getattr(self, "_cuda_usable", None) is False:
                        continue
                    try:
                        m = WhisperModel(
                            name, device=d, compute_type=c, local_files_only=local_only,
                            cpu_threads=cpu_threads, num_workers=1)
                        if d == "cuda":
                            # A CUDA model constructs even when the CUDA runtime libs
                            # (cuBLAS/cuDNN, e.g. cublas64_12.dll) are missing - it
                            # only fails when it actually computes. Force a tiny
                            # warm-up (unless the GPU is already proven good) so we
                            # catch that here and fall back to CPU instead of handing
                            # back a model that hangs on a real dictation.
                            if getattr(self, "_cuda_usable", None) is not True:
                                seg, _ = m.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)
                                list(seg)
                            self._cuda_usable = True
                        self._model = m
                        self._model_name = name
                        logger.info("Whisper on %s (%s, cpu_threads=%s)", d, c, cpu_threads)
                        return
                    except Exception as e:
                        last_err = e
                        if d == "cuda":
                            self._cuda_usable = False  # remember: skip GPU next time
                        logger.warning("Whisper load failed on %s/%s (offline=%s): %s",
                                       d, c, local_only, e)
                        if not repaired_cache and _looks_like_whisper_cache_error(e):
                            if _repair_whisper_model_cache(name):
                                repaired_cache = True
                                logger.info("Repaired Whisper cache for %s; retrying load", name)
                                continue
                if last_err:
                    raise last_err

    def unload_model(self, name=None):
        with self._model_lock:
            if name is None or self._model_name == name:
                self._model = None
                self._model_name = None

    def _find_default_loopback_device(self):
        """
        Dynamically scans host APIs and devices to find the WASAPI loopback device
        associated with the active default system playback/output device.
        If no exact match is found, falls back to the first WASAPI loopback or any loopback device.
        """
        # PyAudioWPatch ships a native resolver that pairs the default output
        # device with its loopback twin - always prefer it. The name matching
        # below is only a fallback, and names can collide: a virtual device
        # like 'Speakers (Steam Streaming Microphone)' also starts with
        # 'Speakers', so a loose match can capture a silent virtual endpoint
        # instead of the real speakers.
        try:
            info = self.audio.get_default_wasapi_loopback()
            if info is not None and info.get("index") is not None:
                logger.info("Native default WASAPI loopback: '%s' (index %s)",
                            info.get("name", "?"), info.get("index"))
                return int(info["index"])
        except Exception as e:
            logger.debug("get_default_wasapi_loopback unavailable: %s", e)

        out_name = ""
        try:
            default_out = self.audio.get_default_output_device_info()
            out_name = default_out.get("name", "")
            if isinstance(out_name, bytes):
                out_name = out_name.decode("utf-8", errors="ignore")
            logger.info("Active system default output device: %s", out_name)
        except Exception as e:
            logger.warning("Could not get default output device info: %s", e)

        # Extract core name to match loopbacks, e.g. "Speakers" or "Headphones"
        core_name = ""
        if out_name:
            core_name = out_name.split("(")[0].strip() if "(" in out_name else out_name.strip()
            if len(core_name) <= 3 and "(" in out_name:
                parts = out_name.split("(")
                if len(parts) > 1:
                    core_name = parts[1].split(")")[0].strip()

        num_devices = self.audio.get_device_count()
        wasapi_loopbacks = []
        any_loopbacks = []
        
        for i in range(num_devices):
            try:
                dev = self.audio.get_device_info_by_index(i)
                if dev.get('maxInputChannels', 0) <= 0:
                    continue
                name = dev.get('name', '')
                if isinstance(name, bytes):
                    name = name.decode('utf-8', errors='ignore')
                
                host_idx = dev.get('hostApi', 0)
                host_api_name = self.audio.get_host_api_info_by_index(host_idx).get('name', '')
                
                is_wasapi = "wasapi" in host_api_name.lower()
                is_loop = "loopback" in name.lower() or dev.get('isLoopback', False) or dev.get('is_loopback', False)
                is_stereo_mix = "stereo mix" in name.lower() or "wave out" in name.lower() or "monitor" in name.lower()
                
                if is_wasapi and (is_loop or is_stereo_mix):
                    wasapi_loopbacks.append((i, name))
                elif is_loop or is_stereo_mix:
                    any_loopbacks.append((i, name))
            except Exception as e:
                logger.debug("Error scanning audio device %d: %s", i, e)
                
        if wasapi_loopbacks:
            # The loopback twin carries the FULL output device name
            # ("Speakers (Realtek(R) Audio) [Loopback]") - match on that first.
            # The loose core-name match alone picked the wrong device when
            # several share a prefix (e.g. Steam's virtual speakers).
            if out_name:
                for idx, name in wasapi_loopbacks:
                    if out_name.lower() in name.lower():
                        logger.info("Matched WASAPI loopback device: '%s' (index %d) for output '%s'", name, idx, out_name)
                        return idx
            if core_name:
                for idx, name in wasapi_loopbacks:
                    if core_name.lower() in name.lower():
                        logger.info("Loosely matched WASAPI loopback device: '%s' (index %d) for output '%s'", name, idx, out_name)
                        return idx
            logger.info("Using first available WASAPI loopback device: '%s' (index %d)", wasapi_loopbacks[0][1], wasapi_loopbacks[0][0])
            return wasapi_loopbacks[0][0]

        if any_loopbacks:
            if out_name:
                for idx, name in any_loopbacks:
                    if out_name.lower() in name.lower():
                        logger.info("Matched fallback loopback device: '%s' (index %d)", name, idx)
                        return idx
            if core_name:
                for idx, name in any_loopbacks:
                    if core_name.lower() in name.lower():
                        logger.info("Loosely matched fallback loopback device: '%s' (index %d)", name, idx)
                        return idx
            logger.info("Using first available loopback device: '%s' (index %d)", any_loopbacks[0][1], any_loopbacks[0][0])
            return any_loopbacks[0][0]
            
        logger.warning("No loopback audio devices found on the system.")
        return None

    def _loopback_to_mono16k(self, raw):
        """Convert raw float32 loopback bytes (device rate/channels) to the
        16 kHz mono float32 stream the rest of the pipeline expects."""
        arr = np.frombuffer(raw, dtype=np.float32)
        # Stereo to mono. Truncate to a whole number of frames first so a
        # short/misaligned read can't crash reshape.
        if self._loopback_channels > 1 and len(arr) > 0:
            usable = len(arr) - (len(arr) % self._loopback_channels)
            arr = (arr[:usable].reshape(-1, self._loopback_channels).mean(axis=1)
                   if usable > 0 else arr[:0])
        if self._loopback_rate != cfg["sample_rate"] and len(arr) > 0:
            duration = len(arr) / self._loopback_rate
            target = int(duration * cfg["sample_rate"])
            if target > 0:
                indices = np.linspace(0, len(arr) - 1, target)
                arr = np.interp(indices, np.arange(len(arr)), arr).astype(np.float32)
            else:
                arr = arr[:0]
        return arr.astype(np.float32, copy=False)

    def _read_available_loopback(self, stream):
        """Non-blockingly read whatever the loopback stream has buffered, as
        16 kHz mono. Returns an empty array when nothing is playing - WASAPI
        loopback devices deliver NO frames while the computer is silent, so a
        blocking read could stall forever (and closing the stream under that
        blocked read crashed the whole app natively)."""
        try:
            avail = stream.get_read_available()
            if avail <= 0:
                return np.zeros(0, dtype=np.float32)
            raw = stream.read(avail, exception_on_overflow=False)
            arr = self._loopback_to_mono16k(raw)
            if len(arr) > 0:
                # Remember the loudest system-audio sample so the meeting UI
                # can tell "nothing was playing on the captured device" apart
                # from a real transcription failure.
                self._loopback_peak = max(self._loopback_peak, float(np.abs(arr).max()))
            return arr
        except Exception as e:
            logger.debug("loopback read failed: %s", e)
            return np.zeros(0, dtype=np.float32)

    @staticmethod
    def _read_input_draining(stream, sr):
        """Read at least one chunk from an input stream, draining any backlog
        in the same read. While Whisper saturates the CPU cores transcribing a
        chunk, this loop stalls for hundreds of ms; with fixed-size reads the
        backlog overflowed PortAudio's buffer and the overflowed audio was
        silently dropped (words went missing mid-dictation/meeting). Reading
        the whole backlog at once keeps up instead. Single reads are capped at
        ~2s; anything beyond that drains on the next tick."""
        avail = 0
        try:
            avail = int(stream.get_read_available())
        except Exception:
            pass
        read_n = max(cfg["chunk_size"], min(avail, sr * 2))
        return stream.read(read_n, exception_on_overflow=False)

    def start_recording(self, capture_mode=None, language=None):
        self.recording = True
        # Per-session language: the meeting window passes its selector value;
        # plain dictation passes nothing, which resets to the global default.
        self.language_override = language or None
        self._capture_mode = capture_mode
        self._loopback_peak = 0.0
        with self._chunk_lock:
            self._chunk_frames     = []
            self._chunk_results    = {}
            self._chunk_idx        = 0
            self._samples_in_chunk = 0
            self._chunk_errors     = []
            self._chunk_silence_before = {}
        self._session_lang  = None
        self._chunk_threads = []
        self._record_error  = ""
        self._abort         = False
        self._cloud_capped  = False
        self._level_peak    = 0.05
        # Full wall-clock recording retained for post-meeting speaker
        # diarization + a clean speaker-attributed re-transcription. Capped so a
        # marathon meeting can't grow memory without bound (~0.23 GB/hour).
        self._full_audio_frames = []
        self._full_audio_len = 0
        self._full_audio_truncated = False
        self._full_audio_max = cfg["sample_rate"] * 60 * 120  # ~2 hours

        open_args = {
            "format": pyaudio.paFloat32,
            "channels": 1,
            "rate": cfg["sample_rate"],
            "input": True,
            "frames_per_buffer": cfg["chunk_size"],
        }
        # Resolve what to capture. `capture_mode` is passed explicitly by the
        # caller: the meetings window passes "smart_meeting" (system + mic),
        # "system_only" (system sound, no mic) or "default_mic"; plain hotkey
        # dictation passes nothing and just uses the configured mic. This keeps
        # loopback/system-audio capture scoped to meetings - it can never leak
        # into ordinary dictation via a shared config key.
        self.mic_stream = None
        is_loopback = False
        self._loopback_rate = cfg["sample_rate"]
        self._loopback_channels = 1

        actual_device_index = None

        if capture_mode in ("smart_meeting", "system_only"):
            discovered = self._find_default_loopback_device()
            if discovered is not None:
                actual_device_index = discovered
                is_loopback = True
                try:
                    dev_info = self.audio.get_device_info_by_index(actual_device_index)
                    self._loopback_rate = int(dev_info.get("defaultSampleRate", 48000))
                    self._loopback_channels = int(dev_info.get("maxInputChannels", 2))
                except Exception as e:
                    logger.warning("Could not query loopback device rate/channels: %s", e)
                    self._loopback_rate = 48000
                    self._loopback_channels = 2
            else:
                logger.warning("System-audio capture selected but no loopback device discovered. Recording default microphone only.")
        elif capture_mode in ("default_mic", "default"):
            actual_device_index = None
        else:
            # Dictation / legacy: input_device_index may name a specific input
            # device. Meeting-mode sentinels left over in this key are treated as
            # the default microphone so a stale config never makes plain
            # dictation start capturing system audio.
            device_index = cfg.get("input_device_index")
            if device_index not in (None, "", "default", "default_mic", "smart_meeting", "system_only"):
                try:
                    actual_device_index = int(device_index)
                except (TypeError, ValueError):
                    actual_device_index = None

        # Open primary stream
        if actual_device_index is not None:
            open_args["input_device_index"] = actual_device_index
            
        if is_loopback:
            open_args["rate"] = self._loopback_rate
            open_args["channels"] = self._loopback_channels
            open_args["frames_per_buffer"] = int(cfg["chunk_size"] * (self._loopback_rate / cfg["sample_rate"]))
            
        self.stream = self.audio.open(**open_args)

        # If primary stream is a loopback, open a secondary stream for the default
        # microphone - but only in smart_meeting mode. "system_only" deliberately
        # records the computer's sound with NO mic.
        if is_loopback and capture_mode == "smart_meeting":
            try:
                mic_args = {
                    "format": pyaudio.paFloat32,
                    "channels": 1,
                    "rate": cfg["sample_rate"],
                    "input": True,
                    "frames_per_buffer": cfg["chunk_size"],
                }
                self.mic_stream = self.audio.open(**mic_args)
                logger.info("Successfully opened secondary default mic stream to mix with loopback!")
            except Exception as me:
                logger.warning("Could not open secondary mic stream: %s", me)
                
        self.stop_requested = False
        # The recording thread OWNS the streams: it is the only reader and the
        # only closer. Stream handles are passed as locals so a later
        # start_recording can never make an old thread close the new streams.
        self._record_thread = threading.Thread(
            target=self._record, args=(is_loopback, self.stream, self.mic_stream),
            daemon=True)
        self._record_thread.start()

    def _record(self, is_loopback, stream, mic_stream):
        try:
            self._record_loop(is_loopback, stream, mic_stream)
        finally:
            self._drain_and_close_streams(is_loopback, stream, mic_stream)

    def _drain_and_close_streams(self, is_loopback, stream, mic_stream):
        """Pull any tail audio still in PortAudio's buffers, then close the
        streams. Runs on the recording thread, which owns the streams -
        closing them from another thread while a read is in flight is a
        native crash (access violation inside PortAudio), which is exactly
        what happened when a meeting was stopped while the loopback read sat
        blocked on a silent device."""
        try:
            avail = stream.get_read_available()
            if avail > 0:
                extra = stream.read(avail, exception_on_overflow=False)
                if extra:
                    if is_loopback:
                        extra = self._loopback_to_mono16k(extra).tobytes()
                    if extra:
                        with self._chunk_lock:
                            self._chunk_frames.append(extra)
        except Exception as e:
            logger.debug("Failed to drain primary stream: %s", e)
        if mic_stream is not None:
            try:
                avail = mic_stream.get_read_available()
                if avail > 0:
                    extra_mic = mic_stream.read(avail, exception_on_overflow=False)
                    if extra_mic:
                        with self._chunk_lock:
                            self._chunk_frames.append(extra_mic)
            except Exception as e:
                logger.debug("Failed to drain secondary mic stream: %s", e)
        for s in (stream, mic_stream):
            if s is None:
                continue
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass

    def _record_loop(self, is_loopback, stream, mic_stream):
        sr          = cfg["sample_rate"]
        frame_dur   = cfg["chunk_size"] / sr
        silence_trigger_sec = cfg_float("silence_trigger_sec", SILENCE_TRIGGER_SEC, 0.2, 5.0)
        min_speech_sec      = cfg_float("min_speech_sec", MIN_SPEECH_SEC, 0.2, 10.0)
        max_speech_sec      = cfg_float("max_speech_sec", MAX_SPEECH_SEC, 2.0, 120.0)

        vad_buf      = []
        speech_sec   = 0.0
        silence_sec  = 0.0
        noise_hist   = []
        read_errors   = 0

        post_roll_chunks = 0
        # ~512ms post-roll after stop to catch final spoken syllables in flight.
        # 256ms still clipped word endings when the hotkey was pressed while the
        # last word was being finished; the extra quarter second costs little.
        max_post_roll = 8

        # Pending loopback samples (16 kHz mono) waiting to be mixed with mic
        # frames, and how long the loopback has delivered nothing (silence).
        loop_fifo = np.zeros(0, dtype=np.float32)
        loop_idle_sec = 0.0

        # Meetings: continuous program audio (videos, music beds, long
        # talkers) may never show a VAD pause, so the silence-gap chunker
        # would sit on the whole recording until Stop and the live transcript
        # stayed empty. On a meeting capture, force a chunk on a fixed
        # cadence - gated on energy so quiet stretches don't send
        # hallucination-prone silence to Whisper.
        meeting_cadence = self._capture_mode is not None
        MEETING_CHUNK_SEC = 10.0
        buffered_sec = 0.0
        chunk_peak = 0.0

        while self.recording or post_roll_chunks > 0:
            if self.stop_requested and post_roll_chunks == 0:
                post_roll_chunks = max_post_roll

            if post_roll_chunks > 0:
                post_roll_chunks -= 1
                if post_roll_chunks == 0:
                    self.recording = False

            try:
                if is_loopback and mic_stream is not None:
                    # smart_meeting: the MIC is the loop's clock (it always
                    # delivers frames); the loopback is drained opportunistically
                    # and mixed in. The old code blocked on the loopback read
                    # first - WASAPI loopbacks deliver nothing while the
                    # computer is silent, so the whole loop stalled (not even
                    # the mic was transcribed) and stopping then crashed the
                    # app by closing the stream under the blocked read.
                    mic_data = self._read_input_draining(mic_stream, sr)
                    arr_mic = np.frombuffer(mic_data, dtype=np.float32)
                    loop_chunk = self._read_available_loopback(stream)
                    if len(loop_chunk) > 0:
                        loop_fifo = np.concatenate([loop_fifo, loop_chunk])
                        # Clock-drift safety valve only - generous, because
                        # dropping from this queue loses system audio.
                        if len(loop_fifo) > sr * 10:
                            loop_fifo = loop_fifo[-sr * 10:]
                    n = min(len(arr_mic), len(loop_fifo))
                    if n > 0:
                        mixed = arr_mic.copy()
                        mixed[:n] = np.clip((arr_mic[:n] + loop_fifo[:n]) * 0.75, -1.0, 1.0)
                        loop_fifo = loop_fifo[n:]
                        data = mixed.tobytes()
                    else:
                        data = mic_data
                elif is_loopback:
                    # system_only: poll instead of blocking - a read blocked on
                    # a silent loopback device cannot be interrupted or closed
                    # safely. While silent, synthesize the occasional silence
                    # frame so the VAD still flushes the last spoken chunk to
                    # the live transcript.
                    loop_chunk = self._read_available_loopback(stream)
                    if len(loop_chunk) == 0:
                        time.sleep(0.05)
                        loop_idle_sec += 0.05
                        if loop_idle_sec >= silence_trigger_sec and speech_sec >= min_speech_sec:
                            loop_idle_sec = 0.0
                            data = np.zeros(cfg["chunk_size"], dtype=np.float32).tobytes()
                        else:
                            continue
                    else:
                        loop_idle_sec = 0.0
                        data = loop_chunk.tobytes()
                else:
                    data = self._read_input_draining(stream, sr)

                read_errors = 0

                arr = np.frombuffer(data, dtype=np.float32)
                this_dur = (len(arr) / sr) if len(arr) > 0 else frame_dur
                buffered_sec += this_dur
                if len(arr) > 0:
                    chunk_peak = max(chunk_peak, float(np.abs(arr).max()))
                rms = float(np.sqrt(np.mean(arr ** 2))) if len(arr) > 0 else 0.0

                noise_hist.append(rms)
                if len(noise_hist) > 200:
                    noise_hist.pop(0)
                n10        = sorted(noise_hist)[max(0, len(noise_hist) // 10)]
                threshold  = max(n10 * 4.0, 0.006)
                is_speech  = rms > threshold

                if self.on_levels:
                    # Auto-normalize: track rolling peak amplitude so bars
                    # adapt to whatever mic gain the user has. Loud speech
                    # fills the bars, quiet speech still produces visible
                    # motion. Peak decays ~halves every 5s of silence so
                    # bars recalibrate if mic gain changes.
                    peak_now = float(np.abs(arr).max()) if len(arr) > 0 else 0.0
                    if peak_now > self._level_peak:
                        self._level_peak = peak_now
                    else:
                        self._level_peak = max(0.01, self._level_peak * 0.995)
                    # Use 0.7 * peak as the "full bar" reference - leaves
                    # headroom so transient loud sounds visibly spike past
                    # normal speech levels.
                    scale = max(0.01, self._level_peak * 0.7)
                    n, sz  = 20, max(len(arr) // 20, 1)
                    levels = [min(float(np.abs(arr[i*sz:(i+1)*sz]).mean()) / scale, 1.0)
                              for i in range(n)]
                    self.on_levels(levels)

                vad_buf.append(data)
                with self._chunk_lock:
                    self._chunk_frames.append(data)
                    # Retain the full wall-clock track (post-meeting diarization)
                    # until the memory cap, then stop growing it.
                    if not self._full_audio_truncated:
                        self._full_audio_frames.append(data)
                        self._full_audio_len += len(arr)
                        if self._full_audio_len > self._full_audio_max:
                            self._full_audio_truncated = True

                if is_speech:
                    speech_sec  += this_dur
                    silence_sec  = 0.0
                elif speech_sec > 0:
                    silence_sec += this_dur

                total_sec    = speech_sec + silence_sec
                force_chunk = (meeting_cadence
                               and buffered_sec >= MEETING_CHUNK_SEC
                               and chunk_peak >= 0.01)
                should_chunk = (
                    (silence_sec >= silence_trigger_sec and speech_sec >= min_speech_sec)
                    or total_sec >= max_speech_sec
                    or force_chunk
                )

                # During background post-roll, accumulate everything into remaining frames
                # rather than starting new parallel chunk transcription threads.
                if post_roll_chunks > 0:
                    should_chunk = False

                if should_chunk and (speech_sec >= min_speech_sec or force_chunk):
                    with self._chunk_lock:
                        chunk_audio = np.frombuffer(
                            b"".join(vad_buf), dtype=np.float32).copy()
                        idx = self._chunk_idx
                        self._chunk_idx   += 1
                        self._chunk_frames = []
                        self._chunk_silence_before[idx] = float(silence_sec)
                    vad_buf      = []
                    speech_sec   = 0.0
                    silence_sec  = 0.0
                    buffered_sec = 0.0
                    chunk_peak   = 0.0
                    t = threading.Thread(target=self._transcribe_chunk,
                                         args=(chunk_audio, idx), daemon=True)
                    self._chunk_threads.append(t)
                    t.start()

            except Exception as e:
                read_errors += 1
                if read_errors >= 5:
                    self._record_error = f"!audio:{e}"
                    self.recording = False
                    break
                time.sleep(0.05)

    def _transcribe_chunk(self, audio, idx):
        try:
            if cfg["backend"] == "managed":
                text, lang = self._run_managed(audio)
            elif cfg["backend"] == "google" and cfg["google_api_key"]:
                text, lang = self._run_google(audio)
            elif cfg["backend"] == "mistral" and cfg.get("mistral_api_key"):
                text, lang = self._run_mistral(audio)
            else:
                text, lang = self._run_local(audio)

            if lang and lang.startswith("!"):
                with self._chunk_lock:
                    self._chunk_errors.append(lang)
                return

            with self._chunk_lock:
                self._chunk_results[idx] = text
                partial = " ".join(
                    self._chunk_results[i]
                    for i in range(self._chunk_idx)
                    if i in self._chunk_results and self._chunk_results[i]
                )
            if self.on_partial and partial:
                self.on_partial(partial)
            if lang and not lang.startswith("!") and self.on_lang_detected:
                self.on_lang_detected(lang, LANG_NAMES.get(lang, lang.upper()))
            try:
                if text and self.on_chunk_complete:
                    self.on_chunk_complete(idx, text, lang or "")
            except Exception as e:
                logger.warning("on_chunk_complete failed: %s", e)
        except Exception as e:
            with self._chunk_lock:
                self._chunk_errors.append(f"!transcribe:{_friendly_transcription_error(e)}")

    def request_abort(self):
        """Cooperative cancel for an in-flight transcribe(): stop waiting on
        chunk threads and return immediately, so Esc frees the app fast instead
        of blocking on a slow/stuck local transcription."""
        self._abort = True
        self.stop_requested = True

    def stop_recording(self):
        self.stop_requested = True

        # The recording thread finishes its post-roll reads, drains the device
        # tail, and closes the streams itself (it owns them). NEVER close the
        # streams from this thread: a cross-thread close while a read is in
        # flight is a native PortAudio crash - the meeting-stop crash.
        t = getattr(self, "_record_thread", None)
        if t is not None:
            t.join(timeout=5.0)
            if t.is_alive():
                logger.warning("Recording thread still busy; it will close its streams itself.")

        self.recording = False
        self.mic_stream = None

    def transcribe(self):
        pending = list(self._chunk_threads)
        budget = max(30, min(180, 5 * len(pending)))
        deadline = time.time() + budget
        for t in pending:
            # Poll in short slices so Esc (request_abort) is honored within
            # ~0.15s instead of blocking up to the full budget on a slow chunk.
            while t.is_alive():
                if self._abort:
                    return "", "!aborted:Cancelled."
                if time.time() > deadline:
                    break
                t.join(timeout=0.15)
            try:
                with self._chunk_lock:
                    done = sum(1 for i in range(self._chunk_idx) if i in self._chunk_results)
                if self.on_finalising:
                    self.on_finalising(done, self._chunk_idx)
            except Exception:
                pass

        if self._abort:
            return "", "!aborted:Cancelled."
        if self._record_error:
            return "", self._record_error
        with self._chunk_lock:
            if self._chunk_errors:
                return "", self._chunk_errors[0]

        remaining = np.frombuffer(b"".join(self._chunk_frames), dtype=np.float32).copy()
        last_text, detected = "", ""

        is_cloud = (cfg["backend"] in ("managed", "google", "mistral"))
        min_samples = cfg["sample_rate"] // 4 if is_cloud else cfg["sample_rate"] // 2

        # Smart Audio Padding: Pad short final audio snippets with silence (zeros)
        # to satisfy the backend's minimum length requirement rather than discarding them.
        if 0 < len(remaining) < min_samples:
            padding_len = min_samples - len(remaining)
            remaining = np.concatenate([remaining, np.zeros(padding_len, dtype=np.float32)])

        # A tail at the noise floor would only make Whisper hallucinate (it
        # tends to echo the custom-vocabulary prompt on near-silence), so the
        # final blob must carry actual signal to be worth transcribing.
        if len(remaining) >= min_samples and float(np.abs(remaining).max()) >= 0.004:
            if cfg["backend"] == "managed":
                last_text, detected = self._run_managed(remaining)
            elif cfg["backend"] == "google" and cfg["google_api_key"]:
                last_text, detected = self._run_google(remaining)
            elif cfg["backend"] == "mistral" and cfg.get("mistral_api_key"):
                last_text, detected = self._run_mistral(remaining)
            else:
                last_text, detected = self._run_local(remaining)

        if detected and (detected.startswith("!google:") or detected.startswith("!mistral:") or detected.startswith("!managed:")):
            return "", detected

        with self._chunk_lock:
            if last_text:
                self._chunk_results[self._chunk_idx] = last_text
            parts = [
                self._chunk_results[i]
                for i in range(self._chunk_idx + 1)
                if i in self._chunk_results and self._chunk_results[i]
            ]
            full_text = " ".join(parts)

        if detected and self.on_lang_detected:
            self.on_lang_detected(detected, LANG_NAMES.get(detected, detected.upper()))
        return full_text.strip(), detected or "en"

    def get_full_audio(self):
        """The full retained wall-clock recording as 16 kHz mono float32 (empty
        array if nothing was retained). Used post-meeting for diarization + a
        clean speaker-attributed re-transcription."""
        with self._chunk_lock:
            frames = b"".join(self._full_audio_frames)
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(frames, dtype=np.float32).copy()

    def transcribe_segments(self, audio, language=None):
        """Transcribe a full audio array into timestamped segments using the
        LOCAL Whisper model: a list of {"start", "end", "text"} in seconds, for
        the post-meeting speaker-attributed transcript. Returns [] on failure or
        very short audio. Holds the shared inference lock (safe after the live
        chunk threads have drained)."""
        try:
            audio = np.asarray(audio, dtype=np.float32)
        except Exception:
            return []
        sr = cfg["sample_rate"]
        if len(audio) < sr // 2:
            return []
        try:
            self.load_model()
        except Exception as e:
            logger.warning("transcribe_segments: model load failed: %s", e)
            return []
        with self._model_lock:
            model = self._model
        if model is None:
            return []
        lang_setting = language or self._lang_setting()
        lang_arg = None if lang_setting in ("auto", "multi", "", None) else lang_setting
        prompt = cfg.get("initial_prompt", "").strip() or None
        try:
            with self._infer_lock:
                segs, _info = model.transcribe(
                    audio, language=lang_arg, initial_prompt=prompt,
                    beam_size=4,
                    # Anti-hallucination (meeting re-transcription): VAD + restored
                    # confidence/no-speech thresholds + no carry-over context, so
                    # silent/music gaps aren't captioned with invented filler.
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                    condition_on_previous_text=False,
                    compression_ratio_threshold=2.4,
                    log_prob_threshold=-1.0,
                    no_speech_threshold=0.6,
                    repetition_penalty=1.3, no_repeat_ngram_size=5,
                    temperature=[0.0, 0.2, 0.4, 0.6, 0.8])
                return [{"start": float(s.start), "end": float(s.end),
                         "text": s.text.strip()} for s in segs if s.text.strip()]
        except Exception as e:
            logger.warning("transcribe_segments failed: %s", e)
            return []

    def _lang_setting(self):
        """The language for THIS recording session: an explicit per-session
        override (the meeting window's selector) wins over the global default."""
        return self.language_override or cfg.get("language", "auto")

    def _run_local(self, audio):
        sr = cfg["sample_rate"]
        if len(audio) < sr // 2:
            return "", "en"
        try:
            return self._run_local_once(audio)
        except Exception as e:
            # If we were running on the GPU and it failed mid-dictation (OOM,
            # driver hiccup, a CUDA lib problem), drop to CPU and retry once so the
            # transcription still succeeds instead of erroring.
            if getattr(self, "_cuda_usable", None):
                logger.warning("GPU transcription failed (%s); retrying on CPU.", e)
                self._cuda_usable = False
                self.unload_model()
                return self._run_local_once(audio)
            raise

    def _fallback_to_local_or_error(self, audio, backend, reason):
        try:
            return self._run_local(audio)
        except Exception as e:
            friendly = _friendly_transcription_error(e)
            logger.warning(
                "%s cloud failed (%s), and local fallback also failed: %s",
                backend, reason, friendly,
            )
            return "", f"!{backend}:{reason} Local fallback is unavailable: {friendly}"

    def _run_local_once(self, audio):
        self.load_model()
        sr = cfg["sample_rate"]
        with self._model_lock:
            model = self._model

        lang_setting = self._lang_setting()
        prompt = cfg.get("initial_prompt", "").strip() or None

        # Serialize ALL inference on this model: faster-whisper/ctranslate2 is
        # not safe for concurrent transcribe() calls, and the chunked pipeline
        # fires several chunk threads at once. Without this, GPU/CUDA hangs (the
        # "works on the no-GPU laptop, stuck on the GPU PC" bug). The lock must
        # also span the generator consumption below - transcribe() is lazy and
        # does the real work while the segments are iterated.
        with self._infer_lock:
            if lang_setting not in ("auto", "multi"):
                lang_arg = lang_setting
            elif lang_setting == "auto" and self._session_lang is not None:
                lang_arg = self._session_lang
            else:
                sample = audio[:sr * 8]
                segs_detect, detect_info = model.transcribe(
                    sample, language=None, beam_size=1,
                    vad_filter=False, without_timestamps=True
                )
                list(segs_detect)
                lang_arg = detect_info.language
                if lang_setting == "auto":
                    self._session_lang = lang_arg

            # Anti-hallucination. Whisper invents filler ("Thank you.", "I'll
            # see you next time.") and loops it on silence/non-speech; the old
            # config disabled its safeguards. condition_on_previous_text=False
            # breaks the repetition loop, and the restored confidence + no-speech
            # thresholds make it drop silence instead of captioning it. VAD is
            # enabled for meetings (long silent stretches) but left OFF for
            # push-to-talk dictation, where the user is deliberately speaking and
            # VAD could clip a short utterance.
            is_meeting = getattr(self, "_capture_mode", None) is not None
            segs, info = model.transcribe(
                audio,
                language=lang_arg,
                initial_prompt=prompt,
                beam_size=4,
                vad_filter=is_meeting,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                no_speech_threshold=0.6,
                repetition_penalty=1.3,
                no_repeat_ngram_size=5,
                temperature=[0.0, 0.2, 0.4, 0.6, 0.8],
            )
            text = " ".join(s.text for s in segs)
        return text, lang_arg

    def _run_google(self, audio):
        # Uses the Google AI Studio (Gemini) API, which accepts a plain API key.
        # (Google Cloud Speech-to-Text rejects API keys and needs OAuth/service
        # accounts, so the key people copy from AI Studio could never work there.)
        try:
            import requests
            key = (cfg.get("google_api_key") or "").strip()
            if not key:
                return "", "!google:Add your Google AI Studio (Gemini) API key in Settings."

            wav_bytes = self._float_to_wav(audio)  # full WAV; Gemini reads the header
            b64_data = base64.b64encode(wav_bytes).decode("utf-8")

            lang_setting = self._lang_setting()
            lang_names = {"hy": "Armenian", "ru": "Russian", "en": "English",
                          "fr": "French", "de": "German", "es": "Spanish", "ar": "Arabic"}
            lang_hint = ""
            if lang_setting in lang_names:
                nm = lang_names[lang_setting]
                # A strong, explicit instruction makes Gemini stay in the target
                # language and native script (e.g. Armenian) instead of drifting
                # to English or transliteration.
                lang_hint = (f" The speaker is speaking {nm}. Transcribe in {nm} using its"
                             f" native script and return only {nm} text.")

            # The user's custom vocabulary used to be dropped on every cloud
            # backend - configure it, upgrade to cloud, and it silently stopped
            # working. Gemini is prompt-driven, so the terms go straight in.
            vocab_hint = vocabulary.cloud_transcription_hint(cfg)

            model_name = cfg.get("google_stt_model", "gemini-2.5-flash")
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model_name}:generateContent?key={key}")
            payload = {
                "contents": [{
                    "parts": [
                        {"text": ("Transcribe this audio verbatim. Output only the exact "
                                  "spoken words with correct punctuation and capitalization, "
                                  "and nothing else." + lang_hint + vocab_hint)},
                        {"inline_data": {"mime_type": "audio/wav", "data": b64_data}},
                    ]
                }],
                "generationConfig": {"temperature": 0},
            }
            resp = requests.post(url, json=payload, timeout=30)
            if resp.status_code != 200:
                reason = cloud_error_message("Google", resp.status_code, resp.text)
                return self._fallback_to_local_or_error(audio, "google", reason)

            data = resp.json()
            cands = data.get("candidates", [])
            if not cands:
                return "", "en"
            parts = cands[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts).strip()
            return text, (lang_setting if lang_setting != "auto" else "en")
        except Exception as e:
            return self._fallback_to_local_or_error(
                audio, "google", f"Google request failed: {e}")

    def _run_managed(self, audio):
        """Pro managed cloud transcription. Sends audio + the user's access token
        to our server proxy, which holds the real STT key. The key never touches
        the client, so this can't be used without a valid Pro/trial account."""
        try:
            import requests
            token = None
            try:
                if callable(self.get_auth_token):
                    token = self.get_auth_token()
            except Exception:
                token = None
            if not token:
                return "", "!managed:Sign in to use managed cloud transcription."

            wav_bytes = self._float_to_wav(audio)  # full WAV; the proxy sends it to Gemini
            b64_data = base64.b64encode(wav_bytes).decode("utf-8")

            # Pro users pick which managed provider to use (server keys): Gemini
            # (default) or Mistral. No BYO key is involved here.
            provider = cfg.get("managed_provider", "gemini")
            # Custom vocabulary for the proxy to fold into its prompt. Older
            # proxy deployments ignore unknown fields, so this is a no-op until
            # the server side ships - never an error.
            body = {
                "audio": b64_data,
                "sample_rate": cfg["sample_rate"],
                "language": self._lang_setting(),
                "provider": provider,
            }
            vocab_terms = vocabulary.cloud_terms(cfg)
            if vocab_terms:
                body["vocabulary"] = vocab_terms
            resp = requests.post(
                MANAGED_PROXY_URL,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code == 403:
                return "", "!managed:Pro required for managed cloud transcription."
            if resp.status_code == 429:
                # Monthly cloud allowance used up - transparently fall back to the
                # local model so the user keeps working. Flag it so the UI can
                # notify once.
                self._cloud_capped = True
                return self._fallback_to_local_or_error(
                    audio, "managed", "Cloud limit reached.")
            if resp.status_code == 403:
                # Not entitled to managed cloud - fall back to local.
                return self._fallback_to_local_or_error(
                    audio, "managed", "Managed cloud access was denied.")
            if resp.status_code == 401:
                return "", "!managed:Session expired - sign in again."
            if resp.status_code != 200:
                # Any cloud error: don't fail the dictation, use local instead.
                reason = cloud_error_message("Managed cloud", resp.status_code, resp.text)
                return self._fallback_to_local_or_error(audio, "managed", reason)
            data = resp.json()
            return data.get("text", ""), data.get("lang", "en")
        except Exception as e:
            # Network/other failure - fall back to local rather than erroring.
            return self._fallback_to_local_or_error(
                audio, "managed", f"Managed cloud request failed: {e}")

    def _run_mistral(self, audio):
        try:
            import requests
            import io
            
            api_key = cfg.get("mistral_api_key", "")
            if not api_key:
                return "", "!mistral:Add your Mistral API key in Settings."

            model = normalize_mistral_model(cfg.get("mistral_stt_model", MISTRAL_STT_DEFAULT))
            lang_setting = self._lang_setting()

            wav_bytes = self._float_to_wav(audio)
            wav_io = io.BytesIO(wav_bytes)

            url = "https://api.mistral.ai/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {api_key}"}
            files = {"file": ("audio.wav", wav_io, "audio/wav")}
            data = {"model": model}
            # Only send a language Voxtral actually supports; "auto"/"multi" and
            # unsupported codes (e.g. Armenian) are omitted so it auto-detects
            # instead of 400-ing.
            if lang_setting in MISTRAL_LANGS:
                data["language"] = lang_setting

            resp = requests.post(url, headers=headers, files=files, data=data, timeout=20)
            if resp.status_code != 200:
                reason = cloud_error_message("Mistral", resp.status_code, resp.text)
                return self._fallback_to_local_or_error(audio, "mistral", reason)

            resp_data = resp.json()
            text = resp_data.get("text", "").strip()
            detected_lang = resp_data.get("language", lang_setting if lang_setting in MISTRAL_LANGS else "en")

            return text, detected_lang
        except Exception as e:
            return self._fallback_to_local_or_error(
                audio, "mistral", f"Mistral request failed: {e}")

    def _float_to_wav(self, audio_float):
        int_data = (audio_float * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(cfg["sample_rate"])
            wf.writeframes(int_data.tobytes())
        return buf.getvalue()

    def shutdown(self):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        if hasattr(self, "mic_stream") and self.mic_stream is not None:
            try:
                self.mic_stream.stop_stream()
                self.mic_stream.close()
            except Exception:
                pass
        self.audio.terminate()

# ── Dynamic Pillow Tray Icon Generator ───────────────────────────────────────
def make_icon(color="#3b82f6"):
    # Create the transparent base image
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    
    # Create a temporary image for the Armenian flag background
    bg = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    
    # Armenian flag colors
    red = (217, 30, 24, 255)     # #d91e18
    blue = (0, 51, 160, 255)     # #0033a0
    orange = (242, 168, 21, 255) # #f2a815
    
    # Draw three horizontal stripes
    bg_draw.rectangle([0, 0, 64, 22], fill=red)
    bg_draw.rectangle([0, 22, 64, 42], fill=blue)
    bg_draw.rectangle([0, 42, 64, 64], fill=orange)
    
    # Create a circular mask
    mask = Image.new("L", (64, 64), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([2, 2, 62, 62], fill=255)
    
    # Composite the flag background onto the main image using the circular mask
    img.paste(bg, (0, 0), mask)
    
    # Draw the white microphone outline/elements on top
    d = ImageDraw.Draw(img)
    d.rectangle([24, 12, 40, 40], fill="white")
    d.ellipse([18, 32, 46, 52],   fill="white")
    d.rectangle([30, 50, 34, 60], fill="white")
    d.rectangle([22, 58, 42, 62], fill="white")
    return img

def make_qicon(color="#3b82f6"):
    img = make_icon(color)
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.size[0], img.size[1], QImage.Format_RGBA8888)
    return QIcon(QPixmap.fromImage(qimg))

# ── Main PySide6 Application Controller ───────────────────────────────────────
class AppController(QObject):
    # Cross-thread Signals - emitted from background hotkey-listener threads
    # (keyboard lib / pynput) and delivered on the Qt main thread via
    # QueuedConnection. Avoids QTimer.singleShot from non-Qt threads, which
    # silently no-ops because those threads have no Qt event loop.
    sig_hotkey = Signal()
    sig_enter = Signal()
    sig_escape = Signal()
    sig_update_available = Signal(str)  # tag of newer version
    sig_auth_changed = Signal()         # auth/entitlement state changed (from worker threads)

    def __init__(self, qapp):
        super().__init__()
        self.qapp = qapp
        self.cfg = cfg

        # Smart Actions is opt-in PER SESSION: every fresh app start begins in
        # plain transcribe-only mode, so dictation always does the predictable
        # thing after a restart; the user re-enables Smart mode when they want
        # it for the session.
        try:
            if actions.normalize_action_mode(self.cfg.get("output_action")) != actions.ACTION_TRANSCRIBE_ONLY:
                self.cfg["output_action"] = actions.ACTION_TRANSCRIBE_ONLY
                save_config(self.cfg)  # module-level: UI/tray don't exist yet
        except Exception:
            logger.debug("could not reset output_action", exc_info=True)

        # Load stylesheet. Resolve the checkbox tick asset to an absolute path
        # so it renders regardless of the process working directory (packaged
        # builds and autostart can launch from an arbitrary CWD).
        from ui.styles import STYLESHEET
        _check_svg = resource_path("assets", "check.svg").replace("\\", "/")
        _chevron_svg = resource_path("assets", "chevron.svg").replace("\\", "/")
        self.style_content = (
            STYLESHEET
            .replace('url("assets/check.svg")', f'url("{_check_svg}")')
            .replace('url("assets/chevron.svg")', f'url("{_chevron_svg}")')
        )

        # Setup pure business Audio Recorder
        self.recorder = AudioRecorder()
        self.recorder.on_levels = self._on_levels
        self.recorder.on_lang_detected = self._on_lang
        self.recorder.on_partial = self._on_partial
        from pynput.keyboard import Controller
        self.kbd = Controller()
        self.is_rec = False
        self._rec_started_at = None  # wall-clock start of the current dictation
        self._mouse_listener = None
        self._kbd_listener = None
        self._registered_kbd_hotkey = None
        self._transient_kbd_handles = []

        # Auth + Pro entitlement (Supabase). All network calls run on worker
        # threads; state changes are marshaled to the GUI thread via the
        # sig_auth_changed signal so tray/UI updates stay thread-safe.
        self.auth = auth.AuthManager(on_state_changed=self.sig_auth_changed.emit)
        # Let the recorder fetch a fresh access token for managed cloud transcription.
        self.recorder.get_auth_token = lambda: self.auth.get_access_token()

        # Initialize UI Dialog Windows (modularly split!)
        from ui.overlay import Overlay
        from ui.settings import Settings
        from ui.history import HistoryWindow
        from ui.meetings import MeetingsWindow
        from ui.onboarding import Onboarding
        
        self.overlay = Overlay(main_app=self)
        self.settings_win = Settings(main_app=self)
        self.history_win = HistoryWindow(main_app=self)
        self.meetings_win = MeetingsWindow(main_app=self)
        self.onboarding_win = Onboarding(main_app=self)
        # When onboarding completes (guest or signed in), land the user in the
        # app panel instead of silently minimizing to the tray - first-run
        # users otherwise think the app closed.
        self.onboarding_win.accepted.connect(self.show_settings)

        # Wire Signals to main-thread handlers. QueuedConnection guarantees the
        # slot runs on this QObject's owning thread (the Qt main thread),
        # regardless of which thread emits the signal.
        self.sig_hotkey.connect(self._on_hotkey, Qt.QueuedConnection)
        self.sig_enter.connect(self._on_enter, Qt.QueuedConnection)
        self.sig_escape.connect(self._on_escape, Qt.QueuedConnection)
        self.sig_update_available.connect(self._prompt_update, Qt.QueuedConnection)
        self.sig_auth_changed.connect(self._on_auth_changed, Qt.QueuedConnection)
        self._update_prompt_open = False

        # Register the configured hotkey (keyboard combo or mouse button).
        self._setup_hotkey(self.cfg["hotkey"])

        # Setup System Tray
        self._setup_tray()

        # Restore any saved login + Pro entitlement in the background.
        threading.Thread(target=self.auth.load_session, daemon=True).start()

        # Check updates in background
        threading.Thread(target=self._background_check_updates, daemon=True).start()
        
        telemetry.track(
            "app_started",
            {
                "privacy_mode": self.cfg.get("privacy_mode", False),
                "backend": self.cfg.get("backend", "local"),
                "output_action": self.cfg.get("output_action", "transcribe_only"),
            },
            self.cfg,
            APP_VERSION,
        )

        # Onboarding wizard trigger on first launch. Existing users (already
        # onboarded before accounts existed) get a one-time account gate instead,
        # so they aren't silently dropped to the 10-minute guest cap.
        if not self.cfg.get("onboarding_done", False):
            QTimer.singleShot(500, self.show_onboarding)
        elif not self.cfg.get("account_gate_seen", False):
            QTimer.singleShot(900, self.show_account_gate)

        # macOS needs explicit permission grants (mic / Accessibility / Input
        # Monitoring) or dictation looks silently broken. One-time guided setup.
        if sys.platform == "darwin" and not self.cfg.get("macos_perms_guide_shown", False):
            QTimer.singleShot(1200, self.show_macos_permissions_guide)

        # If a previous session already detected an update, prompt at startup
        # immediately - don't wait for the network check to confirm. (The
        # background check still runs and will clear the flag if no update.)
        cached_pending = self.cfg.get("pending_update_version", "")
        if cached_pending and self.cfg.get("onboarding_done", False):
            QTimer.singleShot(1500, lambda: self.sig_update_available.emit(cached_pending))

    def _setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.update_tray_icon()
        self._build_tray_menu()
        self.tray_icon.show()

        # Single click tray icon wakes settings window
        self.tray_icon.activated.connect(self._on_tray_activated)

    def _build_tray_menu(self):
        """(Re)build the tray context menu. Called again on auth changes so the
        Pro badge + account section stay current."""
        is_pro = self.is_pro()
        menu = QMenu()

        title_text = f"Transcribe  v{APP_VERSION}" + ("   ·  PRO" if is_pro else "")
        action_title = QAction(title_text, self)
        action_title.setEnabled(False)
        menu.addAction(action_title)

        # The #1 thing people open the menu for - first, bold, one click.
        action_open = QAction("Open Settings", self)
        _f = action_open.font()
        _f.setBold(True)
        action_open.setFont(_f)
        action_open.setIcon(self._dot_icon(self._tier_color()))
        action_open.triggered.connect(self.show_settings)
        menu.addAction(action_open)
        menu.setDefaultAction(action_open)

        menu.addSeparator()

        # ── Account section ──
        if self.auth.is_authenticated:
            who = self.auth.user_email or "Account"
            acct = QAction(f"{who}  ·  {'Pro' if is_pro else 'Free'}", self)
            acct.setEnabled(False)
            menu.addAction(acct)
            if is_pro:
                manage = QAction("Manage subscription…", self)
                manage.triggered.connect(lambda: self.open_billing())
                menu.addAction(manage)
            else:
                upgrade = QAction("Upgrade to Pro…", self)
                upgrade.triggered.connect(lambda: self._pro_upsell())
                menu.addAction(upgrade)
            signout = QAction("Sign out", self)
            signout.triggered.connect(lambda: self.sign_out())
            menu.addAction(signout)
        else:
            mins = entitlements.guest_minutes_remaining()
            guest_lbl = QAction(f"Guest · ~{mins} min free recording left", self)
            guest_lbl.setEnabled(False)
            menu.addAction(guest_lbl)
            signin = QAction("Sign in / Sign up (free)…", self)
            signin.triggered.connect(lambda: self.show_auth_gate())
            menu.addAction(signin)

        menu.addSeparator()

        action_rec_meet = QAction("Record Meeting..." + ("" if is_pro else "   (Pro)"), self)
        action_rec_meet.triggered.connect(self.show_meeting)
        menu.addAction(action_rec_meet)

        action_history = QAction("History Log", self)
        action_history.triggered.connect(self.show_history)
        menu.addAction(action_history)

        # Privacy Mode toggle - accessible from anywhere, not just Settings.
        action_privacy = QAction("Privacy Mode (on-device only)", self)
        action_privacy.setCheckable(True)
        action_privacy.setChecked(bool(self.cfg.get("privacy_mode", False)))
        action_privacy.triggered.connect(lambda: self.toggle_privacy_mode())
        menu.addAction(action_privacy)

        menu.addSeparator()

        action_quit = QAction("Quit", self)
        action_quit.triggered.connect(self.quit_app)
        menu.addAction(action_quit)

        self._tray_menu = menu  # keep a reference so Qt doesn't GC it
        self.tray_icon.setContextMenu(menu)

        tip = "Transcribe - Pro" if is_pro else "Transcribe"
        if self.auth.is_authenticated and self.auth.user_email:
            tip += f"\n{self.auth.user_email}"
        self.tray_icon.setToolTip(tip)

    # ── Auth / Pro helpers ────────────────────────────────────────────────────
    def is_pro(self):
        # has_pro_access: a real Pro/trial entitlement is never downgraded by the
        # admin force-tier preview, so a genuine Pro user is never blocked/upsold.
        try:
            return entitlements.has_pro_access(self.auth, self.cfg)
        except Exception:
            return bool(getattr(self, "auth", None) and self.auth.is_pro)

    def current_tier(self):
        try:
            return entitlements.tier(self.auth, self.cfg)
        except Exception:
            return entitlements.TIER_GUEST

    def _user_secret_id(self):
        return entitlements.user_secret_id(getattr(self, "auth", None))

    def _reconcile_user_secrets(self):
        """Keep BYO API keys + cloud-engine config scoped per user. Returns True if
        the active keys were swapped for a different user (so callers refresh UI)."""
        current = self._user_secret_id()
        if self.cfg.get("secrets_owner") == current:
            return False  # same user (or same guest session) - nothing to do
        switched = entitlements.reconcile_user_secrets(self.cfg, current)
        self.save_config()
        return switched

    def _tier_color(self):
        """Green = Free, Purple = Pro, Gray = Guest."""
        return {
            entitlements.TIER_PRO:   "#a855f7",
            entitlements.TIER_FREE:  "#22c55e",
            entitlements.TIER_GUEST: "#94a3b8",
        }.get(self.current_tier(), "#94a3b8")

    def _dot_icon(self, color_hex):
        """A small filled circle QIcon used as the tier indicator."""
        from PySide6.QtGui import QPixmap, QPainter, QColor, QBrush
        pm = QPixmap(14, 14)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor(color_hex)))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 10, 10)
        p.end()
        return QIcon(pm)

    def set_admin_tier_override(self, value):
        """Super-admin only: force a tier locally (auto|guest|free|pro)."""
        self.cfg["admin_tier_override"] = value
        self.save_config()
        self.sig_auth_changed.emit()

    def _on_auth_changed(self):
        # Runs on the GUI thread (QueuedConnection). Refresh tray + any open UI.
        # First: swap in/out the per-user API keys if the signed-in user changed,
        # so we never expose the previous account's keys on a shared computer.
        switched = False
        try:
            switched = self._reconcile_user_secrets()
        except Exception:
            logger.debug("user secret reconcile failed", exc_info=True)
        # Track the moment a user becomes Pro (trial or paid) for the funnel.
        # Use the REAL server entitlement here (not is_pro(), which honors the
        # admin force-tier preview) so toggling the preview never fires the
        # activation funnel or wipes the managed-engine choice during a preview.
        now_pro = bool(getattr(self.auth, "is_pro", False))
        if now_pro and not getattr(self, "_was_pro", False):
            try:
                telemetry.track("pro_activated", {"plan": getattr(self.auth, "plan", "") or ""}, self.cfg, APP_VERSION)
            except Exception:
                pass
        self._was_pro = now_pro
        # If Pro lapsed while a keyless managed engine/backend was selected, fall
        # back to local so the UI and behavior stay consistent (managed = Pro).
        if not now_pro:
            _changed = False
            if self.cfg.get("backend") == "managed":
                self.cfg["backend"] = "local"
                _changed = True
            if self.cfg.get("action_model") == actions.API_MANAGED_ID:
                self.cfg["action_model"] = actions.RULE_BASED_ID
                _changed = True
            if _changed:
                self.save_config()
        # Remember the signed-in email so the sign-in form can prefill it next time
        # (convenience only - the real session is restored from the encrypted
        # refresh token in the OS keyring, never from this).
        try:
            em = (getattr(self.auth, "user_email", None) or "").strip()
            if em and self.cfg.get("last_signin_email") != em:
                self.cfg["last_signin_email"] = em
                known = list(self.cfg.get("known_emails") or [])
                if em not in known:
                    known.insert(0, em)
                self.cfg["known_emails"] = known[:5]
                self.save_config()
        except Exception:
            logger.debug("could not remember email", exc_info=True)
        try:
            self._build_tray_menu()
        except Exception:
            logger.debug("tray rebuild failed", exc_info=True)
        sw = getattr(self, "settings_win", None)
        if sw is not None and hasattr(sw, "refresh_pro_state"):
            try:
                if switched and hasattr(sw, "reload_secret_fields"):
                    sw.reload_secret_fields()  # pull the swapped-in user's keys
                sw.refresh_pro_state()
            except Exception:
                pass

    def start_google_login(self):
        def _run():
            try:
                self.auth.sign_in_with_google()
            except Exception:
                logger.debug("google login failed", exc_info=True)
        threading.Thread(target=_run, daemon=True).start()
        self.show_tray_hint("Sign in", "Opening your browser to sign in with Google…")

    def sign_out(self):
        threading.Thread(target=self.auth.sign_out, daemon=True).start()

    def open_billing(self):
        if STRIPE_PORTAL_URL:
            webbrowser.open(STRIPE_PORTAL_URL)
        else:
            self.show_tray_hint(
                "Manage subscription",
                "Use the 'Manage subscription' link in your Stripe receipt email "
                "(Customer Portal not configured yet)."
            )

    def set_privacy_mode(self, on, notify=True):
        """Apply Privacy Mode globally and immediately. Privacy forces everything
        local: no cloud/managed backend, no history. Called from the tray and from
        the in-app footer checkbox so the two stay in lockstep."""
        on = bool(on)
        if on == bool(self.cfg.get("privacy_mode", False)):
            return  # no change
        self.cfg["privacy_mode"] = on
        if on:
            self.cfg["backend"] = "local"   # privacy = on-device only
        self.save_config()
        if notify:
            self.show_tray_hint(
                "Privacy Mode ON" if on else "Privacy Mode OFF",
                "Everything stays on your device - cloud features are disabled."
                if on else "Cloud features are available again.",
            )
        # Refresh any open Settings window + rebuild the tray checkmark.
        self.sig_auth_changed.emit()
        try:
            self._build_tray_menu()
        except Exception:
            pass

    def toggle_privacy_mode(self):
        """Flip Privacy Mode from the tray."""
        self.set_privacy_mode(not self.cfg.get("privacy_mode", False))

    def _pro_upsell(self, feature=None):
        try:
            telemetry.track("paywall_viewed", {"feature": feature or ""}, self.cfg, APP_VERSION)
        except Exception:
            pass
        try:
            from ui.pro_dialog import ProDialog
            dlg = ProDialog(main_app=self, feature=feature)
            dlg.exec()
            return
        except Exception as e:
            logger.warning("Pro dialog failed, using fallback: %s", e)

        # Fallback: simple prompt if the rich dialog can't be shown.
        box = QMessageBox()
        box.setWindowTitle("Transcribe Pro")
        feat = f"{feature} is a Pro feature.\n\n" if feature else ""
        box.setText(feat + "Upgrade to unlock Meetings, Smart Actions, and fast cloud transcription.")
        if not self.auth.is_authenticated:
            signin_btn = box.addButton("Create account", QMessageBox.AcceptRole)
        else:
            signin_btn = None
        monthly_btn = box.addButton("€7.99/mo", QMessageBox.ActionRole)
        annual_btn = box.addButton("€59/yr", QMessageBox.ActionRole)
        box.addButton("Maybe later", QMessageBox.RejectRole)
        if hasattr(self, "style_content"):
            box.setStyleSheet(self.style_content)
        box.exec()
        clicked = box.clickedButton()
        if signin_btn is not None and clicked == signin_btn:
            self.show_auth_gate()
        elif clicked == monthly_btn:
            webbrowser.open(self._checkout_url(PRO_MONTHLY_URL))
        elif clicked == annual_btn:
            webbrowser.open(self._checkout_url(PRO_ANNUAL_URL))

    def _checkout_url(self, base):
        """Append the signed-in user's id (+ email) to a Stripe payment link so
        the webhook can link the resulting subscription to the right account."""
        try:
            uid = getattr(self.auth, "user_id", None)
            if not uid:
                return base
            from urllib.parse import urlencode
            params = {"client_reference_id": uid}
            email = getattr(self.auth, "user_email", None)
            if email:
                params["prefilled_email"] = email
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}{urlencode(params)}"
        except Exception:
            return base

    def _account_recording_time(self):
        """Add the just-finished recording's duration to the guest meter. Only
        guests are metered; free/pro have unlimited local dictation."""
        started = self._rec_started_at
        self._rec_started_at = None
        if started is None:
            return
        elapsed = max(0.0, time.time() - started)
        if entitlements.tier(self.auth, self.cfg) == entitlements.TIER_GUEST:
            entitlements.add_guest_seconds(elapsed)
            self.sig_auth_changed.emit()  # refresh tray remaining-time line

    def _guest_limit_reached(self):
        try:
            telemetry.track("guest_trial_exhausted", {}, self.cfg, APP_VERSION)
        except Exception:
            pass
        # Pressing the hotkey when out of free minutes opens the sign in / sign up
        # screen directly, so the user knows the next step is to create an account.
        self.show_tray_hint(
            "Free minutes used up",
            "Sign in (free) to keep dictating - unlimited local transcription, no charge.",
        )
        self.show_auth_gate()

    def _on_tray_activated(self, reason):
        # Any direct click on the tray icon opens the panel - single, double,
        # or middle click. (Right-click shows the menu, whose first item is
        # also "Open Settings".)
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick,
                      QSystemTrayIcon.MiddleClick):
            self.show_settings()

    def update_tray_icon(self):
        color = self.cfg.get("accent_color", "#3b82f6")
        self.tray_icon.setIcon(make_qicon(color))

    def show_tray_hint(self, title, message):
        self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information, 4000)

    def _background_check_updates(self):
        import requests
        try:
            resp = requests.get(RELEASES_API, timeout=8, proxies={"http": None, "https": None})
            if resp.status_code == 200:
                data = resp.json()
                tag = data.get("tag_name", "")

                from main import _parse_version
                if tag and _parse_version(tag) > _parse_version(APP_VERSION):
                    self.cfg["pending_update_version"] = tag
                    self.save_config()
                    # Emit signal - handled on main thread via QueuedConnection.
                    self.sig_update_available.emit(tag)
                else:
                    if self.cfg.get("pending_update_version"):
                        self.cfg["pending_update_version"] = ""
                        self.save_config()
        except Exception as e:
            logger.debug("Background update check failed: %s", e)

    def _prompt_update(self, tag):
        # Runs on Qt main thread. Pops up a modal asking the user to install
        # the new version. Reentrancy guard so a second emit (e.g. another
        # transcription finishes while the dialog is open) doesn't stack popups.
        if self._update_prompt_open:
            return
        if not tag:
            return
        # Skip if user is mid-recording - don't interrupt them.
        if self.is_rec:
            return
        self._update_prompt_open = True
        try:
            reply = QMessageBox.question(
                None, "Transcribe - Update Available",
                f"A new version ({tag}) is available.\n"
                f"You are running v{APP_VERSION}.\n\n"
                "Install update now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
                threading.Thread(
                    target=self._download_and_install_update,
                    args=(tag,), daemon=True,
                ).start()
        finally:
            self._update_prompt_open = False

    def _download_and_install_update(self, tag):
        # Background-thread installer fetch. Identical to the Settings → About
        # button flow, lifted here so the popup works without the user
        # opening Settings first.
        import urllib.request, tempfile, shutil, os
        try:
            setup_url = (
                f"{PROJECT_GITHUB_URL}/releases/download/{tag}/"
                "TranscribeApp-Windows-Setup.exe"
            )
            dest_path = os.path.join(tempfile.gettempdir(), "TranscribeApp-Windows-Setup.exe")
            req = urllib.request.Request(setup_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as response:
                with open(dest_path, "wb") as out_file:
                    shutil.copyfileobj(response, out_file)
            os.startfile(dest_path)
            self.cfg["pending_update_version"] = ""
            self.save_config()
            # Give the installer a moment to launch before we exit.
            time.sleep(1.5)
            QTimer.singleShot(0, self.qapp.quit)
        except Exception as e:
            logger.warning("Update download failed: %s", e)

    # ── Modular Window Surface Triggers (Main thread-safe wrappers) ──
    @staticmethod
    def _bring_to_front(w):
        """Reliably surface a window. A minimized window ignores plain show() on
        Windows - which made tray clicks look like they did nothing - and a
        window behind others needs the raise+activate pair."""
        if w.isMinimized():
            w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()

    def show_settings(self):
        self._bring_to_front(self.settings_win)

    def show_history(self):
        self.history_win.refresh_list()
        self._bring_to_front(self.history_win)

    def show_meeting(self):
        # Meeting recording + AI notes are Pro-only.
        if not self.is_pro():
            self._pro_upsell("Meeting recording")
            return
        self._bring_to_front(self.meetings_win)

    def show_onboarding(self):
        self.onboarding_win.show()
        self.onboarding_win.raise_()
        self.onboarding_win.activateWindow()

    def show_macos_permissions_guide(self):
        """macOS only: a one-time walkthrough of the three permissions the app
        needs (Microphone for dictation, Accessibility for auto-paste, Input
        Monitoring for the global hotkey), with buttons that deep-link straight
        into the right System Settings pane. Without these grants the app looks
        silently broken on a Mac."""
        if sys.platform != "darwin":
            return
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                       QLabel, QPushButton)
        import subprocess

        dlg = QDialog()
        dlg.setWindowTitle("Set up macOS permissions")
        if hasattr(self, "style_content"):
            dlg.setStyleSheet(self.style_content)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)
        title = QLabel("Three quick permissions", dlg)
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #0f172a;")
        lay.addWidget(title)
        sub = QLabel(
            "macOS requires your explicit approval for each of these. "
            "Grant them once and dictation just works.", dlg)
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #475569;")
        lay.addWidget(sub)

        perms = (
            ("Microphone", "Hear your voice for dictation.",
             "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"),
            ("Accessibility", "Paste the finished text into the app you're using.",
             "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"),
            ("Input Monitoring", "Detect the global dictation hotkey.",
             "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"),
        )
        for name, why, url in perms:
            row = QHBoxLayout()
            row.setSpacing(10)
            txt = QLabel(f"<b>{name}</b><br><span style='color:#64748b'>{why}</span>", dlg)
            txt.setWordWrap(True)
            row.addWidget(txt, 1)
            btn = QPushButton("Open Settings", dlg)
            btn.setObjectName("secondaryButton")
            btn.clicked.connect(lambda _=False, u=url: subprocess.Popen(["open", u]))
            row.addWidget(btn, 0)
            lay.addLayout(row)

        done = QPushButton("Done - I've granted them", dlg)
        done.setObjectName("primaryButton")
        done.setMinimumHeight(38)
        done.clicked.connect(dlg.accept)
        lay.addWidget(done)

        dlg.exec()
        self.cfg["macos_perms_guide_shown"] = True
        self.save_config()

    def show_auth_gate(self):
        """Open the email-first sign in / sign up dialog. Used by the Account tab,
        tray, and upgrade prompts when the user wants to create or access an
        account."""
        if self.auth.is_authenticated:
            self.show_settings()
            return
        # Reuse a single dialog instance so repeated clicks just bring it forward
        # instead of stacking new windows.
        gate = getattr(self, "_auth_gate", None)
        if gate is None:
            from ui.onboarding import Onboarding
            gate = Onboarding(main_app=self, account_only=True)
            self._auth_gate = gate
        gate.show()
        gate.raise_()
        gate.activateWindow()

    def show_account_gate(self):
        """One-time sign-in / guest gate for users who onboarded before accounts
        existed, so the accounts update doesn't silently cap them."""
        if self.cfg.get("account_gate_seen", False):
            return
        if self.auth.is_authenticated:
            self.cfg["account_gate_seen"] = True
            self.save_config()
            return
        from ui.onboarding import Onboarding
        self._account_gate = Onboarding(main_app=self, account_only=True)
        # Same as first-run onboarding: finishing the gate (guest or sign-in)
        # opens the app panel instead of dropping the user at the tray.
        self._account_gate.accepted.connect(self.show_settings)
        self._account_gate.show()
        self._account_gate.raise_()
        self._account_gate.activateWindow()

    def save_config(self):
        save_config(self.cfg)
        self.update_tray_icon()
        
        # Propagate color settings to window panels
        if hasattr(self.settings_win, "refresh_list"):
            self.settings_win.setStyleSheet(self.style_content)
        if hasattr(self.history_win, "refresh_list"):
            self.history_win.setStyleSheet(self.style_content)
        if hasattr(self.meetings_win, "txt_summary"):
            self.meetings_win.setStyleSheet(self.style_content)

    # ── Hotkeys ──────────────────────────────────────────────────────────────
    # The dictation hotkey can be either a keyboard combo ("alt+r") or a mouse
    # button ("mouse:middle"). We use the `keyboard` library on Windows for
    # keyboard combos because it uses a proven low-level hook (the same one
    # that worked in earlier Tkinter releases). On macOS/Linux we fall back to
    # pynput's GlobalHotKeys. Mouse always goes through pynput.
    #
    # All listeners emit `sig_hotkey` rather than calling handlers directly,
    # so the work is marshalled onto the Qt main thread safely.
    def _setup_hotkey(self, hotkey, remove_old=None):
        # Tear down any previous registration before installing the new one.
        self._unregister_kbd_hotkey()
        self._unregister_mouse_listener()

        try:
            if hotkey.startswith("mouse:"):
                from pynput import mouse as pynput_mouse
                btn_map = {
                    "middle": pynput_mouse.Button.middle,
                    "left":   pynput_mouse.Button.left,
                    "right":  pynput_mouse.Button.right,
                    "x1":     pynput_mouse.Button.x1,
                    "x2":     pynput_mouse.Button.x2,
                }
                target = btn_map.get(hotkey.split(":")[1], pynput_mouse.Button.middle)

                def _on_click(x, y, btn, pressed):
                    if pressed and btn == target:
                        self.sig_hotkey.emit()

                listener = pynput_mouse.Listener(on_click=_on_click)
                listener.daemon = True
                listener.start()
                self._mouse_listener = listener
                logger.info("Registered mouse hotkey: %s", hotkey)
            elif sys.platform == "win32":
                import keyboard as kbd_lib
                # `keyboard` accepts the same "alt+r" / "ctrl+shift+space"
                # syntax we already produce in settings.py.
                kbd_lib.add_hotkey(
                    hotkey,
                    lambda: self.sig_hotkey.emit(),
                    suppress=False,
                    trigger_on_release=False,
                )
                self._registered_kbd_hotkey = hotkey
                logger.info("Registered keyboard hotkey: %s", hotkey)
            else:
                # macOS / Linux: pynput GlobalHotKeys (handles modifier tracking
                # internally - more robust than a custom Listener).
                from pynput import keyboard as pynput_keyboard
                listener = pynput_keyboard.GlobalHotKeys({
                    self._to_pynput_hotkey(hotkey): lambda: self.sig_hotkey.emit(),
                })
                listener.daemon = True
                listener.start()
                self._kbd_listener = listener
                logger.info("Registered keyboard hotkey (pynput): %s", hotkey)
        except Exception as e:
            logger.warning("Could not register hotkey %s: %s", hotkey, e)
            return False
        return True

    def _unregister_kbd_hotkey(self):
        if self._registered_kbd_hotkey is not None:
            try:
                import keyboard as kbd_lib
                kbd_lib.remove_hotkey(self._registered_kbd_hotkey)
            except Exception:
                pass
            self._registered_kbd_hotkey = None
        if self._kbd_listener is not None:
            try:
                self._kbd_listener.stop()
            except Exception:
                pass
            self._kbd_listener = None

    def _unregister_mouse_listener(self):
        if self._mouse_listener is not None:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None

    @staticmethod
    def _to_pynput_hotkey(hotkey):
        # "alt+shift+r" -> "<alt>+<shift>+r" for pynput.GlobalHotKeys
        parts = []
        for p in hotkey.lower().split("+"):
            p = p.strip()
            if p in ("ctrl", "control"):
                parts.append("<ctrl>")
            elif p in ("alt", "option"):
                parts.append("<alt>")
            elif p == "shift":
                parts.append("<shift>")
            elif p in ("win", "super", "cmd", "command"):
                parts.append("<cmd>")
            else:
                parts.append(p)
        return "+".join(parts)

    def apply_tray_bindings(self):
        self._setup_hotkey(self.cfg["hotkey"])

    # ── Event Callbacks ──
    def _on_levels(self, levels):
        self.overlay.call_soon(self.overlay.update_levels, levels)

    def _on_lang(self, code, name):
        self.overlay.call_soon(self.overlay.set_lang, name)

    def _on_partial(self, text):
        self.overlay.call_soon(self.overlay.set_partial, text)

    def _on_hotkey(self):
        logger.info("[main] _on_hotkey fired (is_rec=%s)", self.is_rec)
        # A previous dictation is still transcribing/processing - ignore the
        # hotkey so we never re-enter the shared recorder mid-flight (crash).
        if getattr(self, "_busy", False):
            # If Esc already cancelled the transcribe/process phase, the worker
            # may still be unwinding. Stay quiet so the user doesn't see a stale
            # "previous dictation" warning for something they just cancelled.
            if getattr(self, "_cancel_processing", False):
                return
            if not self.is_rec:
                self.show_tray_hint(
                    "One moment",
                    "Finishing your previous dictation - try again in a second.",
                )
            return
        if not self.is_rec:
            # Dictation and the meeting recorder share one AudioRecorder + audio
            # device. Don't let the hotkey start a dictation on top of an active
            # meeting - it would clobber the meeting's audio stream. Tell the
            # user instead.
            if self._is_meeting_busy():
                self.show_tray_hint(
                    "Meeting Recording Active",
                    "Alt-R dictation is paused while a meeting is recording so "
                    "the two don't mix. Stop the meeting to dictate again.",
                )
                return
            self._start()
        else:
            threading.Thread(target=self._stop, daemon=True).start()

    def _is_meeting_busy(self):
        """True while the meeting window is actively recording or processing -
        i.e. while it owns the shared AudioRecorder."""
        win = getattr(self, "meetings_win", None)
        if win is None:
            return False
        return getattr(win, "state", None) in (win.STATE_RECORDING, win.STATE_PROCESSING)

    def force_stop_dictation(self):
        """Cancel an in-progress Alt-R dictation (discarding it). Called when a
        meeting starts so the two don't share/clobber the audio device. Returns
        True if a dictation was actually running."""
        if not self.is_rec:
            return False
        try:
            self.recorder.stop_recording()
        except Exception as e:
            logger.warning("force_stop_dictation: stop_recording failed: %s", e)
        self.is_rec = False
        self._unregister_transient_keys()
        try:
            self.overlay.call_soon(self.overlay.hide_overlay)
        except Exception:
            pass
        return True

    def _on_enter(self):
        logger.info("[main] _on_enter fired (is_rec=%s)", self.is_rec)
        if self.is_rec and not getattr(self, "_busy", False):
            threading.Thread(target=self._stop, daemon=True).start()

    def _on_escape(self):
        logger.info("[main] _on_escape fired (is_rec=%s, busy=%s)", self.is_rec, getattr(self, "_busy", False))
        # One Esc handler for both phases: stop everything and free the app for
        # the next recording immediately - whether we're still recording or
        # already transcribing/processing.
        threading.Thread(target=self._abort_everything, daemon=True).start()

    def _abort_everything(self):
        """Esc = full stop. Invalidate any in-flight transcription job (so its
        result is discarded and can't paste), tell the recorder to bail, stop
        recording, and clear the busy/recording state right away so the very
        next hotkey starts a fresh recording with no 'still finishing' wait."""
        # Bumping the job sequence orphans whatever _stop_impl is doing: its
        # result is dropped and it won't re-clear _busy from under a new job.
        self._job_seq = getattr(self, "_job_seq", 0) + 1
        self._cancel_processing = True
        self.is_rec = False
        try:
            self.recorder.request_abort()   # make transcribe() return fast
        except Exception:
            pass
        try:
            self.recorder.stop_recording()  # safe/idempotent if already stopped
        except Exception:
            pass
        self._busy = False                  # immediately ready for the next take
        self._account_recording_time()
        self._unregister_transient_keys()
        self.overlay.call_soon(self.overlay.hide_overlay)

    def _cloud_preflight_warn(self):
        """Immediate, friendly warning if a cloud backend is selected but clearly
        misconfigured (no key). Recording still proceeds and transcription falls
        back to the local model, so a dictation is never lost. Returns (title,
        body) or None."""
        b = self.cfg.get("backend", "local")
        if b == "mistral" and not (self.cfg.get("mistral_api_key") or "").strip():
            return ("Mistral key missing", "Add it in Settings - recording with the local model for now.")
        if b == "google" and not (self.cfg.get("google_api_key") or "").strip():
            return ("Google key missing", "Add your AI Studio key in Settings - recording with the local model for now.")
        return None

    def _start(self):
        if self.is_rec:
            return
        # Guests get a 10-minute free recording trial; block once it's spent.
        if not entitlements.can_record(self.auth, self.cfg):
            self._guest_limit_reached()
            return
        warn = self._cloud_preflight_warn()
        if warn:
            try:
                self.show_tray_hint(*warn)
            except Exception:
                pass
        try:
            self.is_rec = True
            self._rec_started_at = time.time()
            self.overlay.set_partial("")
            from ui.overlay import RECORDING
            self.overlay.show_overlay(RECORDING)
            self.recorder.start_recording()
            self._register_transient_keys()

            if self.cfg["backend"] == "local":
                threading.Thread(
                    target=self.recorder.load_model,
                    daemon=True,
                ).start()

            # If Smart mode + local LLM, pre-warm the action model into RAM
            # while the user is dictating, so post-Enter latency is just
            # inference time (no cold-load wait).
            output_mode = actions.normalize_action_mode(self.cfg.get("output_action"))
            if output_mode == actions.ACTION_SMART_AUTO:
                action_model = actions.normalize_action_model(self.cfg.get("action_model"))
                if action_model in local_llm.MODEL_CATALOG and local_llm.model_downloaded(action_model):
                    def _prewarm():
                        try:
                            local_llm._load_model(action_model)
                        except Exception as exc:
                            logger.debug("Action LLM pre-warm failed: %s", exc)
                    threading.Thread(target=_prewarm, daemon=True).start()
        except Exception as e:
            self.is_rec = False
            self._unregister_transient_keys()
            self.overlay.show_error(f"Microphone error: {e}")

    def _cancel(self):
        self.recorder.stop_recording()
        self.is_rec = False
        self._account_recording_time()
        self._unregister_transient_keys()
        self.overlay.call_soon(self.overlay.hide_overlay)

    def _register_transient_keys(self):
        # Enter (stop) and Esc (cancel) are only meaningful while recording.
        # Use a pynput Listener - we confirmed in the logs that pynput reliably
        # receives plain Enter/Esc events on Windows, while the `keyboard`
        # library's on_press_key callback wasn't firing for those keys when
        # another hotkey was already registered with add_hotkey.
        self._unregister_transient_keys()
        try:
            from pynput import keyboard as pynput_keyboard

            def _on_press(key):
                try:
                    if key == pynput_keyboard.Key.enter:
                        logger.info("[hook] Enter pressed, is_rec=%s", self.is_rec)
                        if self.is_rec:
                            self.sig_enter.emit()
                    elif key == pynput_keyboard.Key.esc:
                        logger.info("[hook] Esc pressed, is_rec=%s", self.is_rec)
                        # Esc cancels while recording AND while transcribing/
                        # processing, so a long transcription can be aborted.
                        if self.is_rec or getattr(self, "_busy", False):
                            self.sig_escape.emit()
                except Exception:
                    pass

            listener = pynput_keyboard.Listener(on_press=_on_press)
            listener.daemon = True
            listener.start()
            self._transient_kbd_handles.append(listener)
            logger.info("Registered transient Enter/Esc listener")
        except Exception as e:
            logger.warning("Could not register enter/esc listener: %s", e)

    def _unregister_transient_keys(self):
        for h in self._transient_kbd_handles:
            try:
                h.stop()
            except Exception:
                pass
        self._transient_kbd_handles = []

    def _effective_cfg(self):
        """The config governing THIS dictation.

        Today simply the global config. It exists as a single interception point
        so a future per-app profile system can override settings in one place
        rather than having to find every self.cfg read in the dictation path.
        """
        return self.cfg

    def _postprocess_transcript(self, text):
        """Output-quality pass (see text_cleanup). Extracted so it can be tested
        without standing up the recorder, overlay and telemetry."""
        if not text:
            return ""
        cfg = self._effective_cfg()
        try:
            opts = text_cleanup.options_from_config(cfg)
            if not cfg.get("cleanup_enabled", True):
                # Master switch off: nothing is second-guessed about what the
                # recognizer heard. The user's own replacement dictionary still
                # applies - the UI calls it "always replace these words", and a
                # deliberate correction isn't a cleanup heuristic.
                opts = text_cleanup.CleanupOptions(
                    strip_hallucinations=False, strip_artifacts=False,
                    collapse_repeats=False, remove_fillers=False,
                    replacements=opts.replacements)
            cleaned, report = text_cleanup.clean_with_report(text, options=opts)
            if any(report.values()):
                # Counts only - never transcript text.
                logger.debug("text cleanup: %s", report)
            return cleaned
        except Exception:
            # A regex bug must never cost the user a dictation.
            logger.debug("text cleanup failed; using raw transcript", exc_info=True)
            return text

    @staticmethod
    def should_restore_clipboard(pasted, restore_enabled, prev_was_text, still_ours):
        """Whether to put the user's previous clipboard back after a paste.

        Pure so the rules are testable:
        - only when the feature is on,
        - only if we actually pasted (otherwise the text is intentionally left
          on the clipboard for the user to paste by hand),
        - only if the previous content was text (never clear an image/file
          clipboard to an empty string - worse than leaving our text),
        - only if nothing else has changed the clipboard since.
        """
        return bool(restore_enabled and pasted and prev_was_text and still_ours)

    def _paste_via_clipboard(self, text, restore=True):
        """Copy `text`, send the paste chord, then restore the prior clipboard.

        Returns True if the paste chord was sent. The restore is deliberately
        delayed: the target app reads the clipboard asynchronously after Ctrl+V,
        so restoring immediately would race and paste the OLD content.
        """
        cfg = self._effective_cfg()
        prev, prev_was_text = None, False
        if restore and cfg.get("restore_clipboard", True):
            try:
                prev = pyperclip.paste()
                prev_was_text = isinstance(prev, str) and prev != ""
            except Exception:
                prev, prev_was_text = None, False

        pyperclip.copy(text)

        pasted = False
        try:
            from pynput.keyboard import Key
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            self.kbd.press(mod); self.kbd.press("v")
            self.kbd.release("v"); self.kbd.release(mod)
            pasted = True
        except Exception as e:
            logger.warning("[Paste] %s", e)

        if self.should_restore_clipboard(
                pasted, cfg.get("restore_clipboard", True), prev_was_text, True):
            def _restore():
                try:
                    delay = int(cfg.get("clipboard_restore_delay_ms", 400))
                except (TypeError, ValueError):
                    delay = 400
                time.sleep(max(0, min(delay, 5000)) / 1000.0)
                try:
                    # Someone else may have taken the clipboard meanwhile.
                    if pyperclip.paste() == text:
                        pyperclip.copy(prev)
                except Exception:
                    pass
            threading.Thread(target=_restore, daemon=True).start()
        return pasted

    def _resolve_action_engine(self, action_model=None):
        """Resolve (model, config) for a Smart Action / meeting-notes run,
        applying Pro routing: a signed-in Pro user runs through the managed cloud
        (our key, no BYO key needed) unless they deliberately picked a local LLM
        or a cloud engine with their OWN key. Shared by dictation and meetings so
        both honor a Pro subscription identically (meetings used to skip this,
        which is why Pro users were wrongly asked for an action API key)."""
        action_model = actions.normalize_action_model(
            action_model if action_model is not None
            else self.cfg.get("action_model", actions.RULE_BASED_ID))
        action_config = self.cfg
        if self.is_pro():
            token = None
            try:
                token = self.auth.get_access_token()
            except Exception:
                token = None
            if token:
                action_config = {**self.cfg, "_managed_token": token}
                m_kind = actions.ACTION_MODELS.get(action_model, {}).get("kind")
                has_own_cloud_key = bool(
                    ((self.cfg.get("action_api_key") or "") or (self.cfg.get("google_api_key") or "")).strip())
                if m_kind in ("rules", "managed") or (m_kind == "cloud" and not has_own_cloud_key):
                    action_model = actions.API_MANAGED_ID
        return action_model, action_config

    def _stop(self):
        # _busy spans the whole transcribe→action→paste pipeline so a second
        # hotkey press can't re-enter and clobber the shared recorder mid-flight
        # (a real crash source). Each run gets a job id; Esc bumps it to orphan
        # this run, and only the still-current job is allowed to clear _busy.
        self._job_seq = getattr(self, "_job_seq", 0) + 1
        job = self._job_seq
        self._busy = True
        self._cancel_processing = False
        try:
            self._stop_impl(job)
        finally:
            if job == self._job_seq:  # not superseded by an Esc/new recording
                self._busy = False
                self._unregister_transient_keys()

    def _stop_impl(self, job):
        self.recorder.stop_recording()
        # Recording is finished now - flip is_rec immediately so Esc during the
        # transcription below is treated as "cancel processing", and a stale
        # is_rec can't make the next hotkey think we're still recording.
        self.is_rec = False
        self._account_recording_time()
        # NOTE: the Esc/Enter listener is intentionally left running here so Esc
        # can abort the transcription below; it's torn down in _stop's finally.
        from ui.overlay import TRANSCRIBING
        self.overlay.call_soon(self.overlay.set_state, TRANSCRIBING)

        TRANSCRIBE_TIMEOUT_SEC = 120
        _result = {}
        def _do_transcribe():
            try:
                _result["text"], _result["lang"] = self.recorder.transcribe()
            except Exception as e:
                _result["exc"] = e

        t = threading.Thread(target=_do_transcribe, daemon=True)
        t.start()
        # Poll instead of a blocking join so Esc can cancel mid-transcription.
        waited = 0.0
        while t.is_alive() and waited < TRANSCRIBE_TIMEOUT_SEC:
            if job != self._job_seq:
                # Esc/new recording superseded this run - drop it silently
                # (_abort_everything already reset state + hid the overlay).
                logger.info("[main] transcription job %s cancelled", job)
                return
            t.join(timeout=0.1)
            waited += 0.1

        # If managed cloud hit its monthly cap, we transparently used the local
        # model - tell the user once so the switch isn't a mystery.
        if getattr(self.recorder, "_cloud_capped", False):
            self.recorder._cloud_capped = False
            self.overlay.call_soon(
                self.show_tray_hint,
                "Cloud limit reached",
                "You've used this month's fast-cloud minutes - switched to the local model (still unlimited).",
            )

        if t.is_alive():
            telemetry.track(
                "action_failed",
                {"action": "transcribe", "reason": f"timeout_{TRANSCRIBE_TIMEOUT_SEC}s"},
                self.cfg, APP_VERSION,
            )
            self.overlay.call_soon(
                self.overlay.show_error,
                f"Transcription timed out after {TRANSCRIBE_TIMEOUT_SEC}s - try a shorter clip or a smaller model.",
            )
            return
            
        if "exc" in _result:
            self.overlay.call_soon(
                self.overlay.show_error,
                _friendly_transcription_error(_result["exc"])[:120],
            )
            return

        # Esc pressed while the transcription was finishing - discard it.
        if job != self._job_seq:
            return

        raw_text = _result.get("text", "")
        lang = _result.get("lang", "")

        # Output-quality pass. Deliberately here and nowhere else: this is the
        # dictation-only path (meetings call the recorder directly), so cleanup
        # can never perturb the meeting/diarization pipeline.
        text = self._postprocess_transcript(raw_text)

        if raw_text and not text:
            # Cleanup consumed the whole transcript, i.e. it was pure
            # hallucination/noise. Say so rather than vanishing silently.
            self.overlay.call_soon(
                self.overlay.show_error, "Only background noise was detected.")
            return

        if not text:
            if lang and lang.startswith("!"):
                err_msg = lang[1:]
                if ":" in err_msg:
                    err_msg = err_msg.split(":", 1)[1]
                self.overlay.call_soon(self.overlay.show_error, err_msg)
            else:
                self.overlay.call_soon(self.overlay.hide_overlay)
            return

        action_mode = actions.normalize_action_mode(self.cfg.get("output_action"))
        output_text = text

        # Smart Actions: Pro = unlimited; everyone else gets 5 free tries, then it
        # falls back to pasting raw text (and we nudge them to upgrade).
        if action_mode != actions.ACTION_TRANSCRIBE_ONLY and not self.is_pro():
            if entitlements.can_use_smart_action(self.auth, self.cfg):
                entitlements.add_smart_action_use(self.auth)
                rem = entitlements.smart_actions_remaining(self.auth, self.cfg)
                self.overlay.call_soon(
                    self.show_tray_hint,
                    "Smart Action (free trial)",
                    f"{rem} free Smart Action(s) left. Upgrade to Pro for unlimited.",
                )
                # Reflect the new count in any open Settings window.
                self.sig_auth_changed.emit()
            else:
                action_mode = actions.ACTION_TRANSCRIBE_ONLY
                if entitlements.tier(self.auth, self.cfg) == entitlements.TIER_GUEST:
                    self.overlay.call_soon(
                        self.show_tray_hint,
                        "Smart Actions (Pro)",
                        "Pasted your raw text. Sign up free to get 5 Smart Action trials.",
                    )
                else:
                    self.overlay.call_soon(
                        self.show_tray_hint,
                        "Free Smart Actions used up",
                        "Pasted your raw text. Upgrade to Transcribe Pro for unlimited Smart Actions.",
                    )

        if action_mode != actions.ACTION_TRANSCRIBE_ONLY:
            # Switch the overlay to "Thinking…" while the smart action runs.
            from ui.overlay import PROCESSING
            self.overlay.call_soon(self.overlay.set_state, PROCESSING)
            # Pick the engine + inject the auth token for Pro managed actions.
            action_model, action_config = self._resolve_action_engine()
            try:
                output_text = actions.process(
                    text,
                    action_mode,
                    source_lang=lang,
                    target_lang=self.cfg.get("translate_target", "en"),
                    model=action_model,
                    config=action_config,
                )
                telemetry.track(
                    "action_completed",
                    {
                        "action": action_mode,
                        "model": action_model,
                        "language": lang,
                        "output_length_bucket": _bucket_count(len(output_text)),
                    },
                    self.cfg,
                    APP_VERSION,
                )
            except actions.ActionError as e:
                telemetry.track(
                    "action_failed",
                    {"action": action_mode, "reason": str(e)[:60]},
                    self.cfg,
                    APP_VERSION,
                )
                self.overlay.call_soon(self.overlay.show_error, str(e))
                return

        # Esc during the Smart Action / "Thinking…" phase: don't paste or save.
        if job != self._job_seq:
            return

        if self.cfg.get("save_history", True) and not self.cfg.get("privacy_mode", False):
            hist.save_entry(output_text, lang, self.cfg["backend"])

        telemetry.track(
            "transcription_completed",
            {
                "backend": self.cfg.get("backend", "local"),
                "language": lang,
                "action": action_mode,
                "input_length_bucket": _bucket_count(len(text)),
                "output_length_bucket": _bucket_count(len(output_text)),
            },
            self.cfg,
            APP_VERSION,
        )

        time.sleep(0.35)
        pasted = self._paste_via_clipboard(output_text)

        self.overlay.call_soon(self.overlay.show_done, pasted)

        # After a finished transcription, nudge the user about a pending update.
        # Wait until the "done" overlay has had time to show before popping up.
        pending = self.cfg.get("pending_update_version", "")
        if pending:
            time.sleep(2.5)
            self.sig_update_available.emit(pending)

    def paste_text(self, text):
        # Triggers cursor paste for clicked history logs
        self._paste_via_clipboard(text)

    def quit_app(self):
        # Graceful shutdown of system hooks
        self._unregister_transient_keys()
        self._unregister_kbd_hotkey()
        self._unregister_mouse_listener()
        self.recorder.shutdown()
        self.qapp.quit()

# ── Helpers ───────────────────────────────────────────────────────────────────
from ui.meetings import MeetingsWindow
from ui.settings import Settings


def _parse_version(v):
    try:
        parts = [int(x) for x in v.lstrip("v").split(".")[:3]]
        return tuple((parts + [0, 0, 0])[:3])
    except Exception:
        return (0, 0, 0)

def _format_bytes(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024

def _trusted_update_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc.lower() == "github.com"
    except Exception:
        return False

def _sha256_from_checksum_text(text):
    for token in (text or "").replace("\r", " ").replace("\n", " ").split():
        token = token.strip()
        if len(token) == 64 and all(c in "0123456789abcdefABCDEF" for c in token):
            return token.lower()
    return ""

def _asset_url(data, wanted_name):
    wanted = wanted_name.lower()
    for asset in data.get("assets", []):
        if asset.get("name", "").lower() == wanted:
            return asset.get("browser_download_url")
    return None

def _update_info_from_manifest(manifest, ignore_dismissed=False):
    tag = manifest.get("tag") or f"v{manifest.get('version', '')}"
    version = manifest.get("version") or tag
    if not tag or _parse_version(version) <= _parse_version(APP_VERSION):
        return None
    if not ignore_dismissed and cfg.get("dismissed_update_version") == tag:
        return None

    windows = manifest.get("windows", {})
    installer = windows.get("installer", {})
    return {
        "tag": tag,
        "installer_url": installer.get("url"),
        "checksum_url": installer.get("sha256_url"),
        "checksum": installer.get("sha256", ""),
        "body": manifest.get("notes", "") or manifest.get("changelog", ""),
        "notes_url": manifest.get("notes_url", RELEASES_URL),
        "source": "manifest",
    }

def _update_info_from_release(data, ignore_dismissed=False):
    latest = data.get("tag_name", "")
    if _parse_version(latest) <= _parse_version(APP_VERSION):
        return None
    if not ignore_dismissed and cfg.get("dismissed_update_version") == latest:
        return None

    installer_url = None
    installer_name = ""
    preferred = _asset_url(data, "TranscribeApp-Windows-Setup.exe")
    if preferred:
        installer_url = preferred
        installer_name = "transcribeapp-windows-setup.exe"
    else:
        for asset in data.get("assets", []):
            name = asset.get("name", "").lower()
            if "setup" in name and name.endswith(".exe"):
                installer_url = asset.get("browser_download_url")
                installer_name = name
                break

    checksum_url = None
    if installer_name:
        checksum_url = (
            _asset_url(data, f"{installer_name}.sha256")
            or _asset_url(data, f"{installer_name}.sha256sum")
        )
    return {
        "tag": latest,
        "installer_url": installer_url,
        "checksum_url": checksum_url,
        "checksum": "",
        "body": data.get("body", "") or f"Release notes: {data.get('html_url', RELEASES_URL)}",
        "notes_url": data.get("html_url", RELEASES_URL),
        "source": "release",
    }

def _bucket_count(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "0"
    for limit in (25, 100, 250, 500, 1000, 2500):
        if value <= limit:
            return f"<= {limit}"
    return "> 2500"


# ── Main Entrypoint ───────────────────────────────────────────────────────────
def main():
    # 1. IPC Single-Instance check
    lock_sock = acquire_single_instance_lock()
    if not lock_sock:
        # Another instance is running, wake it and exit
        signal_running_instance("show_settings")
        sys.exit(0)

    # 2. Standard Qt Setup
    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False) # Tray-resident background app constraint!

    # Scrolling the page must never silently rewrite a dropdown/spin-box the
    # cursor happens to pass over (Qt's default wheel behaviour).
    try:
        from ui.wheelguard import install as install_wheel_guard
        install_wheel_guard(qapp)
    except Exception as e:
        logging.warning("Could not install wheel guard: %s", e)

    # Set premium global application window icon (circular Armenian Flag + Microphone)
    try:
        qapp.setWindowIcon(make_qicon())
    except Exception as e:
        logging.error("Failed to set global window icon: %s", e)

    # Create app logic controller
    controller = AppController(qapp)

    # Attach lock socket IPC listener
    start_ipc_server(lock_sock, lambda action: QTimer.singleShot(0, lambda: controller.show_settings()))

    # The Settings panel IS the app - show it on every launch. The only quiet
    # launch is the Windows-startup shortcut (--background), and the first run
    # ever belongs to the onboarding wizard (which lands in the panel itself).
    if "--background" not in sys.argv and controller.cfg.get("onboarding_done", False):
        QTimer.singleShot(200, controller.show_settings)

    sys.exit(qapp.exec())

if __name__ == "__main__":
    main()
