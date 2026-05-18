import sys, threading, time, json, math, wave, io, struct, webbrowser, socket, queue, logging, hashlib
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from urllib.parse import urlparse
import psutil, pyaudio, numpy as np, keyboard, pyperclip, pystray, requests, base64, ctypes
from PIL import Image, ImageDraw, ImageTk
from pynput.keyboard import Controller, Key
from pynput import mouse as pynput_mouse
from faster_whisper import WhisperModel
import storage
import history as hist
import actions
import local_llm
import telemetry

# ── Version ───────────────────────────────────────────────────────────────────

APP_VERSION = "1.5.26"
PROJECT_GITHUB_URL = "https://github.com/Aram2K/transcribe-app"
RELEASES_URL = "https://github.com/Aram2K/transcribe-app/releases/latest"
RELEASES_API = "https://api.github.com/repos/Aram2K/transcribe-app/releases/latest"
RELEASES_MANIFEST_URL = "https://github.com/Aram2K/transcribe-app/releases/latest/download/update-manifest.json"
AIBUBEN_URL = "https://aibuben.xyz"
GOOGLE_API_KEY_URL = "https://console.cloud.google.com/apis/credentials"
GOOGLE_SPEECH_DOCS_URL = "https://cloud.google.com/speech-to-text/docs/reference/rest/v1/RecognitionConfig"

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
DEFAULT = {
    "hotkey":        "alt+r",
    "whisper_model": "base",
    "language":      "auto",
    "sample_rate":   16000,
    "chunk_size":    1024,
    "accent_color":  "#3b82f6",
    "backend":       "local",
    "google_api_key": "",
    "initial_prompt": "",
    "output_action": "transcribe_only",
    "action_model": "built_in",
    "translate_target": "en",
    "input_device_index": None,
    "silence_trigger_sec": 0.8,
    "min_speech_sec": 1.5,
    "max_speech_sec": 25.0,
    "privacy_mode": False,
    "save_history": True,
    "analytics_enabled": False,
    "analytics_endpoint": "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/transcribe-analytics",
    "onboarding_done": False,
    "dismissed_update_version": "",   # remember which update tag the user dismissed
    "pending_update_version": "",
    "pending_update_body": "",
    "previous_version": "",
    "tray_hint_shown": False,         # whether we've shown the "I live in your tray" hint
}

storage.migrate_legacy_file(LEGACY_CONFIG_PATH, CONFIG_PATH)

def load_config():
    data = storage.read_json(CONFIG_PATH, DEFAULT)
    if not isinstance(data, dict):
        data = {}
    loaded = {**DEFAULT, **data}

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
        loaded["save_history"] = False
        loaded["analytics_enabled"] = False

    return loaded

def save_config(c):
    disk = {**c}
    if disk.get("privacy_mode"):
        disk["backend"] = "local"
        disk["save_history"] = False
        disk["analytics_enabled"] = False
    key = (disk.get("google_api_key") or "").strip()
    if storage.write_secret(storage.GOOGLE_API_KEY_SECRET, key):
        disk["google_api_key"] = ""
    storage.atomic_write_json(CONFIG_PATH, disk)

cfg = load_config()

def resource_path(*parts):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = base.joinpath(*parts)
    if candidate.exists():
        return str(candidate)
    return str(Path(__file__).resolve().parent.joinpath(*parts))

def load_tk_image(*parts, size=None):
    img = Image.open(resource_path(*parts)).convert("RGBA")
    if size:
        img = img.resize(size, Image.LANCZOS)
    return ImageTk.PhotoImage(img)

def attach_placeholder_entry(entry, variable, placeholder, normal_fg, placeholder_fg, secret_char=""):
    state = {"placeholder": False}

    def show_placeholder():
        if (variable.get() or "").strip():
            return
        state["placeholder"] = True
        entry.configure(fg=placeholder_fg, show="")
        entry.delete(0, tk.END)
        entry.insert(0, placeholder)

    def show_value():
        state["placeholder"] = False
        entry.configure(fg=normal_fg, show=secret_char)
        entry.delete(0, tk.END)
        value = variable.get() or ""
        if value:
            entry.insert(0, value)

    def on_focus_in(_event=None):
        if state["placeholder"]:
            state["placeholder"] = False
            entry.configure(fg=normal_fg, show=secret_char)
            entry.delete(0, tk.END)

    def on_key(_event=None):
        if not state["placeholder"]:
            variable.set(entry.get())

    def on_focus_out(_event=None):
        if not state["placeholder"]:
            variable.set(entry.get().strip())
        if not (variable.get() or "").strip():
            show_placeholder()

    if (variable.get() or "").strip():
        show_value()
    else:
        show_placeholder()

    entry.bind("<FocusIn>", on_focus_in)
    entry.bind("<KeyRelease>", on_key)
    entry.bind("<FocusOut>", on_focus_out)
    return entry

# ── Single-instance IPC ──────────────────────────────────────────────────────
# Bind a localhost TCP port. If the bind fails, another instance is already
# running — connect to it and send an action ("show_settings") so the existing
# tray-resident process surfaces a window. Then exit. This is what makes a
# second double-click of the desktop shortcut do something visible instead of
# silently spawning a duplicate background process.

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

MODELS = {
    "tiny":           {"min_ram": 2,  "speed": "~0.5s", "quality": "Good",        "size": "75 MB",   "armenian": None},
    "base":           {"min_ram": 4,  "speed": "~1s",   "quality": "Better",      "size": "140 MB",  "armenian": None},
    "small":          {"min_ram": 6,  "speed": "~3s",   "quality": "Great",       "size": "460 MB",  "armenian": None},
    "medium":         {"min_ram": 10, "speed": "~8s",   "quality": "Excellent",   "size": "1.4 GB",  "armenian": None},
    "large-v3-turbo": {"min_ram": 8,  "speed": "~5s",   "quality": "Best (fast)", "size": "1.6 GB",  "armenian": "Recommended for Armenian"},
    "large-v3":       {"min_ram": 16, "speed": "~15s",  "quality": "Best",        "size": "3 GB",    "armenian": None},
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

def model_ok(name):
    return RAM_GB >= MODELS[name]["min_ram"]

def model_downloaded(name):
    try:
        from faster_whisper.utils import download_model
        download_model(name, local_files_only=True)
        return True
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

# Detect CUDA GPU via ctranslate2 (already a dependency of faster-whisper)
HAS_GPU = False
try:
    import ctranslate2 as _ct2
    HAS_GPU = _ct2.get_cuda_device_count() > 0
except Exception:
    pass

# ── Glass effect ──────────────────────────────────────────────────────────────

class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_int), ("AnimationId", ctypes.c_int)]

class WCA_DATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.POINTER(ACCENT_POLICY)),
                ("SizeOfData", ctypes.c_int)]

def apply_glass(hwnd, tint="#111111e0"):
    try:
        r = int(tint[1:3],16); g = int(tint[3:5],16); b = int(tint[5:7],16)
        a = int(tint[7:9],16) if len(tint)==9 else 200
        accent = ACCENT_POLICY()
        accent.AccentState  = 4
        accent.GradientColor = (a<<24)|(b<<16)|(g<<8)|r
        data = WCA_DATA()
        data.Attribute = 19
        data.SizeOfData = ctypes.sizeof(accent)
        data.Data = ctypes.pointer(accent)
        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except:
        pass

# ── Audio Recorder ────────────────────────────────────────────────────────────

# VAD-triggered chunking constants (replaces fixed CHUNK_SEC timer)
SILENCE_TRIGGER_SEC = 0.8   # silence duration that triggers a transcription chunk
MIN_SPEECH_SEC      = 1.5   # minimum speech before a chunk is considered complete
MAX_SPEECH_SEC      = 25    # force-flush chunk after this duration regardless

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
        self.frames           = []
        self.audio            = pyaudio.PyAudio()
        self._model           = None
        self._model_name      = None
        self._model_lock      = threading.Lock()
        # use no-op defaults so callers never need to null-check
        self.on_levels        = lambda lvls: None
        self.on_lang_detected = lambda code, name: None
        self.on_partial       = lambda text: None
        # chunked streaming state — mutations always under _chunk_lock
        self._chunk_frames    = []
        self._chunk_results   = {}
        self._chunk_idx       = 0
        self._chunk_lock      = threading.Lock()
        self._samples_in_chunk= 0
        self._session_lang    = None   # cached detected language for this recording
        self._chunk_threads   = []     # active background chunk threads
        self._chunk_errors    = []
        self._record_error    = ""

    def load_model(self, name=None):
        name = name or cfg["whisper_model"]
        with self._model_lock:
            if self._model is None or self._model_name != name:
                self._model      = WhisperModel(name, device="cpu", compute_type="int8")
                self._model_name = name

    def unload_model(self, name=None):
        with self._model_lock:
            if name is None or self._model_name == name:
                self._model = None
                self._model_name = None

    def start_recording(self):
        self.recording = True
        self.frames    = []
        with self._chunk_lock:
            self._chunk_frames     = []
            self._chunk_results    = {}
            self._chunk_idx        = 0
            self._samples_in_chunk = 0
            self._chunk_errors     = []
        self._session_lang  = None   # reset on each new recording
        self._chunk_threads = []     # reset thread list each session
        self._record_error  = ""
        open_args = {
            "format": pyaudio.paFloat32,
            "channels": 1,
            "rate": cfg["sample_rate"],
            "input": True,
            "frames_per_buffer": cfg["chunk_size"],
        }
        device_index = cfg.get("input_device_index")
        if device_index not in (None, "", "default"):
            open_args["input_device_index"] = int(device_index)
        self.stream = self.audio.open(**open_args)
        threading.Thread(target=self._record, daemon=True).start()

    def _record(self):
        sr          = cfg["sample_rate"]
        frame_dur   = cfg["chunk_size"] / sr   # seconds per audio frame
        silence_trigger_sec = cfg_float("silence_trigger_sec", SILENCE_TRIGGER_SEC, 0.2, 5.0)
        min_speech_sec      = cfg_float("min_speech_sec", MIN_SPEECH_SEC, 0.2, 10.0)
        max_speech_sec      = cfg_float("max_speech_sec", MAX_SPEECH_SEC, 2.0, 120.0)

        vad_buf      = []    # frames for the current utterance
        speech_sec   = 0.0
        silence_sec  = 0.0
        noise_hist   = []    # sliding window for adaptive noise floor
        read_errors   = 0

        while self.recording:
            try:
                data = self.stream.read(cfg["chunk_size"], exception_on_overflow=False)
                read_errors = 0
                self.frames.append(data)

                arr = np.frombuffer(data, dtype=np.float32)
                rms = float(np.sqrt(np.mean(arr ** 2))) if len(arr) > 0 else 0.0

                # Adaptive noise floor: 10th-percentile of recent RMS history
                noise_hist.append(rms)
                if len(noise_hist) > 200:
                    noise_hist.pop(0)
                n10        = sorted(noise_hist)[max(0, len(noise_hist) // 10)]
                threshold  = max(n10 * 4.0, 0.006)
                is_speech  = rms > threshold

                # Audio level callback for overlay waveform
                if self.on_levels:
                    n, sz  = 20, max(len(arr) // 20, 1)
                    levels = [min(float(np.abs(arr[i*sz:(i+1)*sz]).mean()) * 20, 1.0)
                              for i in range(n)]
                    self.on_levels(levels)

                vad_buf.append(data)
                with self._chunk_lock:
                    self._chunk_frames.append(data)

                if is_speech:
                    speech_sec  += frame_dur
                    silence_sec  = 0.0
                elif speech_sec > 0:
                    silence_sec += frame_dur

                total_sec    = speech_sec + silence_sec
                should_chunk = (
                    (silence_sec >= silence_trigger_sec and speech_sec >= min_speech_sec)
                    or total_sec >= max_speech_sec
                )

                if should_chunk and speech_sec >= min_speech_sec:
                    with self._chunk_lock:
                        chunk_audio = np.frombuffer(
                            b"".join(vad_buf), dtype=np.float32).copy()
                        idx = self._chunk_idx
                        self._chunk_idx   += 1
                        self._chunk_frames = []
                    vad_buf     = []
                    speech_sec  = 0.0
                    silence_sec = 0.0
                    t = threading.Thread(target=self._transcribe_chunk,
                                         args=(chunk_audio, idx), daemon=True)
                    self._chunk_threads.append(t)
                    t.start()

            except Exception as e:
                read_errors += 1
                if read_errors >= 5:
                    print(f"[Audio] Stopping after repeated read errors: {e}")
                    self._record_error = f"!audio:{e}"
                    self.recording = False
                    break
                time.sleep(0.05)

    def _transcribe_chunk(self, audio, idx):
        try:
            if cfg["backend"] == "google" and cfg["google_api_key"]:
                text, lang = self._run_google(audio)
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
        except Exception as e:
            print(f"[Chunk {idx}] Transcription error: {e}")
            with self._chunk_lock:
                self._chunk_errors.append(f"!transcribe:{e}")

    def stop_recording(self):
        self.recording = False
        time.sleep(0.15)
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception as e:
            print(f"[Stop] Stream cleanup: {e}")

    def _float_to_wav(self, audio_float):
        int_data = (audio_float * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(cfg["sample_rate"])
            wf.writeframes(int_data.tobytes())
        return buf.getvalue()

    def transcribe(self):
        # Wait for all background chunk threads to finish before reading results.
        # Critical for Google backend: a 4-second chunk fires a ~1s network call.
        # If the user stops right at a chunk boundary, the thread won't have returned
        # yet and _chunk_results would be empty, causing the overlay to disappear.
        deadline = time.time() + 18   # generous timeout — Google can be slow
        for t in list(self._chunk_threads):
            wait = max(0.0, deadline - time.time())
            if wait > 0:
                t.join(timeout=wait)

        if self._record_error:
            return "", self._record_error
        with self._chunk_lock:
            if self._chunk_errors:
                return "", self._chunk_errors[0]

        # Transcribe remaining (last partial chunk) then merge
        remaining = np.frombuffer(b"".join(self._chunk_frames), dtype=np.float32).copy()
        last_text, detected = "", ""

        # Google minimum is 0.25s; local model needs 0.5s — use appropriate threshold
        min_samples = cfg["sample_rate"] // 4 if (cfg["backend"] == "google" and cfg["google_api_key"]) else cfg["sample_rate"] // 2
        if len(remaining) >= min_samples:
            if cfg["backend"] == "google" and cfg["google_api_key"]:
                last_text, detected = self._run_google(remaining)
            else:
                last_text, detected = self._run_local(remaining)

        # Propagate error signal immediately without merging
        if detected and detected.startswith("!google:"):
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

    def _run_local(self, audio):
        self.load_model()
        sr = cfg["sample_rate"]
        if len(audio) < sr // 2:
            return "", "en"

        with self._model_lock:
            model = self._model

        # ── Language resolution ──────────────────────────────────────────────
        lang_setting = cfg["language"]

        if lang_setting not in ("auto", "multi"):
            # Explicit language — use directly, no detection needed
            lang_arg = lang_setting
        elif lang_setting == "auto" and self._session_lang is not None:
            # Auto: cache the language detected on the first chunk so
            # subsequent chunks don't each pay for a detection pass.
            lang_arg = self._session_lang
        else:
            # "auto" (first chunk) or "multi" (every chunk independently).
            # Consume the detection generator fully before the second pass —
            # faster-whisper is lazy; not consuming it leaves CTranslate2
            # state unreleased, which corrupts the transcription pass.
            sample = audio[:sr * 8]
            segs_detect, detect_info = model.transcribe(
                sample, language=None, beam_size=1,
                vad_filter=False, without_timestamps=True
            )
            list(segs_detect)
            lang_arg = detect_info.language
            if lang_setting == "auto":
                self._session_lang = lang_arg  # cache only in single-lang auto mode

        is_hy = (lang_arg == "hy")

        # ── Build prompt ─────────────────────────────────────────────────────
        # Only use the user's custom vocabulary prompt. No automatic Armenian
        # seed — Whisper echoes the prompt in a repetition loop.
        # language="hy" is sufficient to force native Armenian script output.
        prompt = cfg.get("initial_prompt", "").strip() or None

        # ── Transcription ────────────────────────────────────────────────────
        # Key Armenian fixes (from benchmark research):
        #
        # vad_filter=False  — Silero VAD is not trained on Armenian; it treats
        #   Armenian fricatives/aspirates as silence, stripping most of the audio.
        #   Disable it for Armenian; the VAD-triggered chunking in _record() already
        #   handles silence detection before audio reaches this function.
        #
        # compression_ratio_threshold=None, log_prob_threshold=None — disable
        #   Whisper's post-hoc silence/repetition filters; they fire too aggressively
        #   on Armenian due to low confidence scores (low-resource language). Let
        #   the pre-chunk VAD handle silence instead.
        #
        # repetition_penalty=1.3, no_repeat_ngram_size=5 — prevent token loops
        #   without relying on compression_ratio_threshold.
        #
        # temperature list — Whisper falls back to higher temperatures automatically
        #   when the output still looks repetitive, breaking hallucination cycles.
        #
        # condition_on_previous_text=False — do not feed previous segment output
        #   back as context; each chunk is independent, and feeding prior text
        #   is the primary cause of the repetition loop.
        segs, _ = model.transcribe(
            audio,
            language=lang_arg,
            beam_size=5 if is_hy else 3,
            best_of=5,
            vad_filter=not is_hy,
            vad_parameters=None if is_hy else {
                "threshold": 0.3,
                "min_speech_duration_ms": 250,
                "min_silence_duration_ms": 300,
                "speech_pad_ms": 400,
            },
            no_speech_threshold=0.6,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            compression_ratio_threshold=None if is_hy else 2.4,
            log_prob_threshold=None if is_hy else -1.0,
            repetition_penalty=1.3 if is_hy else 1.0,
            no_repeat_ngram_size=5 if is_hy else 0,
            initial_prompt=prompt,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segs).strip(), lang_arg

    def _run_google(self, audio):
        if len(audio) < cfg["sample_rate"] // 4:   # < 0.25s — skip
            return "", ""
        wav = self._float_to_wav(audio)
        b64 = base64.b64encode(wav).decode()

        BCP = {"hy":"hy-AM","en":"en-US","ru":"ru-RU",
               "fr":"fr-FR","de":"de-DE","es":"es-ES","ar":"ar-AE"}
        lang_setting = cfg["language"]
        if lang_setting == "multi":
            # Multilingual: Google detects among all common languages per utterance
            primary, alts = "hy-AM", ["en-US", "ru-RU", "fr-FR"]
        elif lang_setting == "auto":
            primary, alts = "hy-AM", ["en-US", "ru-RU"]
        else:
            primary = BCP.get(lang_setting, "hy-AM")
            alts    = []

        gcfg = {
            "encoding":        "LINEAR16",
            "sampleRateHertz": cfg["sample_rate"],
            "languageCode":    primary,
        }
        if alts:                                     # never send an empty list
            gcfg["alternativeLanguageCodes"] = alts
        if primary in ("en-US", "fr-FR", "de-DE", "es-ES"):
            gcfg["enableAutomaticPunctuation"] = True

        payload = {"config": gcfg, "audio": {"content": b64}}
        try:
            resp = requests.post(
                f"https://speech.googleapis.com/v1/speech:recognize?key={cfg['google_api_key']}",
                json=payload, timeout=15)
            data = resp.json()
            if resp.status_code != 200:
                err = data.get("error", {}).get("message", str(resp.status_code))
                print(f"[Google] Error {resp.status_code}: {err}")
                return "", f"!google:{resp.status_code}: {err[:60]}"
            if "results" not in data:
                return "", ""    # silence / no speech — not an error
            text = " ".join(r["alternatives"][0]["transcript"]
                            for r in data["results"] if r.get("alternatives"))
            lang = data["results"][0].get("languageCode", "hy-AM").split("-")[0]
            return text.strip(), lang
        except Exception as e:
            print(f"[Google] Exception: {e}")
            return "", f"!google:{e}"

    def list_input_devices(self):
        devices = []
        try:
            for i in range(self.audio.get_device_count()):
                info = self.audio.get_device_info_by_index(i)
                if int(info.get("maxInputChannels", 0)) > 0:
                    devices.append({
                        "index": i,
                        "name": info.get("name", f"Input {i}"),
                    })
        except Exception as e:
            logger.warning("Could not list input devices: %s", e)
        return devices

    def test_input_level(self, device_index=None, seconds=0.7):
        stream = None
        try:
            open_args = {
                "format": pyaudio.paFloat32,
                "channels": 1,
                "rate": cfg["sample_rate"],
                "input": True,
                "frames_per_buffer": cfg["chunk_size"],
            }
            if device_index not in (None, "", "default"):
                open_args["input_device_index"] = int(device_index)
            stream = self.audio.open(**open_args)
            chunks = max(1, int(seconds * cfg["sample_rate"] / cfg["chunk_size"]))
            levels = []
            for _ in range(chunks):
                data = stream.read(cfg["chunk_size"], exception_on_overflow=False)
                arr = np.frombuffer(data, dtype=np.float32)
                if len(arr):
                    levels.append(float(np.sqrt(np.mean(arr ** 2))))
            return max(levels) if levels else 0.0, ""
        except Exception as e:
            return 0.0, str(e)
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    def shutdown(self):
        try:
            self.audio.terminate()
        except Exception as e:
            print(f"[Audio] Terminate error: {e}")

# ── Overlay ───────────────────────────────────────────────────────────────────

RECORDING    = "recording"
TRANSCRIBING = "transcribing"
DONE         = "done"

class Overlay:
    W, H = 340, 96

    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg="#0d0d0d")
        self._pos()

        self.canvas   = tk.Canvas(self.root, width=self.W, height=self.H,
                                  bg="#0d0d0d", highlightthickness=0)
        self.canvas.pack()

        self.state        = RECORDING
        self.levels       = [0.0]*20
        self._smooth      = [0.0]*20
        self._visible     = False
        self._alpha       = 0.0
        self._lang        = ""
        self._partial     = ""
        self._done_msg    = ""
        self._done_pasted = False
        self._done_error  = False
        self._hide_at     = None
        self._ui_tasks    = queue.Queue()

        self.root.withdraw()
        self._loop()

    def call_soon(self, func, *args, **kwargs):
        self._ui_tasks.put((func, args, kwargs))

    def _drain_ui_tasks(self):
        while True:
            try:
                func, args, kwargs = self._ui_tasks.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args, **kwargs)
            except Exception as e:
                print(f"[UI] Task error: {e}")

    def _has_child_windows(self):
        for child in self.root.winfo_children():
            try:
                if child.winfo_exists() and child.winfo_toplevel() is child:
                    return True
            except Exception:
                pass
        return False

    def _pos(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{self.W}x{self.H}+{sw-self.W-24}+{sh-self.H-60}")

    def show(self, state=RECORDING):
        self.state    = state
        self._lang    = ""
        self._visible = True
        self.root.deiconify()
        self._pos()
        self.root.update_idletasks()
        apply_glass(self.root.winfo_id(), "#0f0f0fe8")

    def hide(self):
        self._visible = False

    def set_state(self, s):
        self.state = s

    def set_lang(self, lang_name):
        self._lang = lang_name

    def set_partial(self, text):
        self._partial = text

    def update_levels(self, levels):
        self.levels = levels[:]

    def show_done(self, pasted: bool):
        self._done_pasted = pasted
        self._done_error  = False
        self._done_msg    = "Pasted to cursor" if pasted else "Copied to clipboard"
        self.state        = DONE
        self._visible     = True
        self._hide_at     = time.time() + 2.2

    def show_error(self, msg: str):
        self._done_error  = True
        self._done_pasted = False
        self._done_msg    = msg
        self.state        = DONE
        self._visible     = True
        self._hide_at     = time.time() + 4.0

    def _loop(self):
        self._drain_ui_tasks()

        if self._hide_at and time.time() >= self._hide_at:
            self._hide_at = None
            self._visible = False

        target      = 0.93 if self._visible else 0.0
        self._alpha += (target - self._alpha) * 0.3
        keep_root_awake = self._has_child_windows()
        if self._alpha < 0.02 and not self._visible and not keep_root_awake:
            self._alpha = 0.0
            self.root.withdraw()
        else:
            self.root.deiconify()
        self.root.attributes("-alpha", max(0.0, min(self._alpha, 1.0)))
        # smooth audio levels every frame
        for i in range(len(self._smooth)):
            self._smooth[i] += (self.levels[i] - self._smooth[i]) * 0.35
        self._draw()
        self.root.after(40, self._loop)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        W, H   = self.W, self.H
        accent = cfg["accent_color"]

        self._rrect(c, 1, 1, W-1, H-1, 14, "#161616")
        self._rrect_border(c, 1, 1, W-1, H-1, 14, "#2b2b2b")

        if self.state == RECORDING:
            self._draw_rec(c, W, H, accent)
        elif self.state == TRANSCRIBING:
            self._draw_loading(c, W, H, accent)
        else:
            self._draw_done(c, W, H)

    def _draw_rec(self, c, W, H, accent):
        t      = time.time()
        smooth = list(self._smooth)
        ar, ag, ab = int(accent[1:3],16), int(accent[3:5],16), int(accent[5:7],16)

        # ── Recording indicator: pulsing ring + solid dot ────────────────
        pulse  = 0.5 + 0.5 * math.sin(t * 3.5)
        dx, dy = 18, 26
        rr     = 7 + pulse * 3
        c.create_oval(dx-rr, dy-rr, dx+rr, dy+rr, outline="#ff3b3b", width=1)
        c.create_oval(dx-4,  dy-4,  dx+4,  dy+4,  fill="#ff3b3b", outline="")

        c.create_text(32, dy-7, anchor="w", text="Recording",
                      fill="#ffffff", font=("Segoe UI Semibold", 11))
        c.create_text(32, dy+7, anchor="w",
                      text=f"Enter or {cfg['hotkey']} to finish  ·  Esc to cancel",
                      fill="#333333", font=("Segoe UI", 8))

        # ── Symmetric bar waveform (up + down from centre) ───────────────
        PAD   = 14
        num   = 44
        avail = W - PAD * 2
        step  = avail / num
        bar_w = max(int(step * 0.55), 2)
        cy    = H - 20
        max_h = 16

        for i in range(num):
            idx_f = i / max(num - 1, 1) * (len(smooth) - 1)
            lo    = int(idx_f); hi = min(lo + 1, len(smooth) - 1)
            lv    = smooth[lo] * (1 - (idx_f - lo)) + smooth[hi] * (idx_f - lo)
            # gentle idle ripple so bars are never fully flat
            lv    = max(lv, 0.06 * abs(math.sin(t * 1.8 + i * 0.38)))
            bh    = max(int(lv * max_h), 2)
            x     = int(PAD + i * step)
            fac   = 0.3 + 0.7 * min(lv * 2, 1.0)
            col   = f"#{int(ar*fac):02x}{int(ag*fac):02x}{int(ab*fac):02x}"
            c.create_rectangle(x, cy - bh, x + bar_w, cy + bh, fill=col, outline="")

    def _draw_loading(self, c, W, H, accent):
        angle = -(time.time()*300) % 360
        cx, cy, r = 22, 28, 11
        c.create_oval(cx-r, cy-r, cx+r, cy+r, outline="#252525", width=2)
        c.create_arc(cx-r, cy-r, cx+r, cy+r,
                     start=angle, extent=250,
                     style=tk.ARC, outline=accent, width=2)

        dots  = "." * (int(time.time()*2) % 4)
        label = f"Transcribing · {self._lang}{dots}" if self._lang else f"Finalising{dots}"
        c.create_text(42, 22, anchor="w", text=label,
                      fill="#ffffff", font=("Segoe UI Semibold", 11))
        backend_label = "Google Cloud" if cfg["backend"]=="google" else "Local"
        c.create_text(42, 36, anchor="w",
                      text=f"via {backend_label}  ·  pasting when ready",
                      fill="#444444", font=("Segoe UI", 8))

        partial = self._partial  # local snapshot — avoids cross-thread mutation
        if partial:
            preview = partial[-55:].lstrip()
            if len(partial) > 55:
                preview = "…" + preview
            c.create_rectangle(10, 50, W-10, H-8, fill="#1a1a1a", outline="#2a2a2a")
            c.create_text(16, (50+H-8)//2, anchor="w", text=preview,
                          fill="#aaaaaa", font=("Segoe UI", 9))

    def _draw_done(self, c, W, H):
        cx, cy, r = 22, H//2, 13
        if self._done_error:
            # red circle with X
            c.create_oval(cx-r, cy-r, cx+r, cy+r, fill="#3a1a1a", outline="#ef4444", width=1)
            c.create_line(cx-5, cy-5, cx+5, cy+5, fill="#ef4444", width=2, capstyle=tk.ROUND)
            c.create_line(cx+5, cy-5, cx-5, cy+5, fill="#ef4444", width=2, capstyle=tk.ROUND)
            msg = self._done_msg if len(self._done_msg) <= 36 else self._done_msg[:34] + "…"
            c.create_text(42, H//2-9, anchor="w", text=msg,
                          fill="#ef4444", font=("Segoe UI Semibold", 10))
            c.create_text(42, H//2+9, anchor="w",
                          text="Check Settings → Test Key",
                          fill="#555555", font=("Segoe UI", 8))
        else:
            # green circle with checkmark
            c.create_oval(cx-r, cy-r, cx+r, cy+r, fill="#1a3a24", outline="#22c55e", width=1)
            c.create_line(cx-6, cy,   cx-1, cy+5, fill="#22c55e", width=2, capstyle=tk.ROUND)
            c.create_line(cx-1, cy+5, cx+7, cy-4, fill="#22c55e", width=2, capstyle=tk.ROUND)
            c.create_text(42, H//2-9, anchor="w", text=self._done_msg,
                          fill="#ffffff", font=("Segoe UI Semibold", 11))
            icon = "⌨" if self._done_pasted else "📋"
            c.create_text(42, H//2+9, anchor="w",
                          text=f"{icon}  Ready",
                          fill="#444444", font=("Segoe UI", 8))

    def _rrect(self, c, x1, y1, x2, y2, r, fill):
        c.create_rectangle(x1+r, y1,   x2-r, y2,   fill=fill, outline="")
        c.create_rectangle(x1,   y1+r, x2,   y2-r, fill=fill, outline="")
        for ox,oy in [(x1,y1),(x2-2*r,y1),(x1,y2-2*r),(x2-2*r,y2-2*r)]:
            c.create_oval(ox, oy, ox+2*r, oy+2*r, fill=fill, outline="")

    def _rrect_border(self, c, x1, y1, x2, y2, r, color):
        for s,e,ox,oy in [(90,90,x1,y1),(0,90,x2-2*r,y1),(180,90,x1,y2-2*r),(270,90,x2-2*r,y2-2*r)]:
            c.create_arc(ox, oy, ox+2*r, oy+2*r, start=s, extent=e, style=tk.ARC, outline=color)
        c.create_line(x1+r, y1,   x2-r, y1,   fill=color)
        c.create_line(x1+r, y2,   x2-r, y2,   fill=color)
        c.create_line(x1,   y1+r, x1,   y2-r, fill=color)
        c.create_line(x2,   y1+r, x2,   y2-r, fill=color)

    def run(self):
        self.root.mainloop()

# ── History Window ────────────────────────────────────────────────────────────

class HistoryWindow:
    BG   = "#0f0f0f"
    CARD = "#161616"
    SEP  = "#1e1e1e"
    FG   = "#ffffff"
    FG2  = "#555555"

    def __init__(self, root):
        self.root = root
        self.win  = None
        self.search_var = None
        self._list_outer = None
        self._count_label = None
        self._selection_label = None
        self._selected_indices = set()

    def open(self):
        self.root.deiconify()
        telemetry.track("history_opened", {}, cfg, APP_VERSION)
        if self.win and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            return

        self.win = tk.Toplevel(self.root)
        self.win.title("History")
        self.win.configure(bg=self.BG)
        self.win.geometry("520x600")
        self.win.resizable(False, True)
        self.win.attributes("-topmost", True)
        self.win.transient(self.root)
        self.win.update_idletasks()
        apply_glass(self.win.winfo_id(), "#0f0f0ff5")

        self._build()
        self.win.lift()
        self.win.focus_force()

    def _build(self):
        w = self.win
        for child in w.winfo_children():
            child.destroy()
        entries = hist.load()

        # Header
        hdr = tk.Frame(w, bg=self.BG)
        hdr.pack(fill="x", padx=20, pady=(18, 6))
        tk.Label(hdr, text="History", bg=self.BG, fg=self.FG,
                 font=("Segoe UI Semibold", 16)).pack(side="left")
        self._count_label = tk.Label(hdr, text=f"  {len(entries)} entries",
                                     bg=self.BG, fg=self.FG2, font=("Segoe UI", 9))
        self._count_label.pack(side="left", pady=4)
        self._selection_label = tk.Label(hdr, text="",
                                         bg=self.BG, fg=self.FG2, font=("Segoe UI", 9))
        self._selection_label.pack(side="left", pady=4)

        if entries:
            actions = tk.Frame(w, bg=self.BG)
            actions.pack(fill="x", padx=20, pady=(0, 8))
            for label, cmd in [
                ("Clear all", self._clear),
                ("Clear selected", self._clear_selection),
                ("Select visible", self._select_visible),
                ("Export TXT", lambda: self._export("txt")),
                ("Export CSV", lambda: self._export("csv")),
            ]:
                tk.Button(actions, text=label, command=cmd,
                          bg=self.BG, fg="#555555", activebackground=self.BG,
                          font=("Segoe UI", 9), relief="flat", cursor="hand2").pack(side="right", padx=(8, 0))

            self.search_var = tk.StringVar(value=self.search_var.get() if self.search_var else "")
            search_box = tk.Entry(w, textvariable=self.search_var, bg="#171717", fg=self.FG,
                                  insertbackground=self.FG, relief="flat",
                                  font=("Segoe UI", 10))
            search_box.pack(fill="x", padx=20, pady=(4, 10), ipady=7)
            search_box.bind("<KeyRelease>", lambda e: self._render_entries())

        tk.Frame(w, bg=self.SEP, height=1).pack(fill="x")

        if not entries:
            msg = ("History saving is off.\nNew transcriptions will not be saved."
                   if not cfg.get("save_history", True)
                   else "No transcriptions yet.\nPress your hotkey to start.")
            tk.Label(w, text=msg,
                     bg=self.BG, fg=self.FG2,
                     font=("Segoe UI", 11)).pack(expand=True)
            return

        # Scrollable list
        self._list_outer = tk.Frame(w, bg=self.BG)
        self._list_outer.pack(fill="both", expand=True)
        self._render_entries()

    def _filtered_entries(self):
        query = self.search_var.get() if self.search_var else ""
        query = (query or "").strip().lower()
        pairs = list(enumerate(hist.load()))
        if not query:
            return pairs
        return [
            (i, e) for i, e in pairs
            if query in e.get("text", "").lower()
            or query in e.get("language", "").lower()
            or query in e.get("backend", "").lower()
            or query in e.get("timestamp", "").lower()
        ]

    def _update_selection_label(self):
        if not self._selection_label or not self._selection_label.winfo_exists():
            return
        count = len(self._selected_indices)
        self._selection_label.configure(text=f"  {count} selected" if count else "")

    def _select_visible(self):
        for idx, _entry in self._filtered_entries():
            self._selected_indices.add(idx)
        self._render_entries()

    def _clear_selection(self):
        self._selected_indices.clear()
        self._render_entries()

    def _toggle_selection(self, idx, selected):
        if selected:
            self._selected_indices.add(idx)
        else:
            self._selected_indices.discard(idx)
        self._update_selection_label()

    def _entries_for_export(self):
        entries = hist.load()
        if self._selected_indices:
            selected = [
                entries[idx] for idx in sorted(self._selected_indices)
                if 0 <= idx < len(entries)
            ]
            return selected, "selected"
        filtered = [entry for _idx, entry in self._filtered_entries()]
        query = (self.search_var.get() if self.search_var else "").strip()
        return filtered, "visible" if query else "all"

    def _render_entries(self):
        if not self._list_outer or not self._list_outer.winfo_exists():
            return
        for child in self._list_outer.winfo_children():
            child.destroy()

        entries = self._filtered_entries()
        total = len(hist.load())
        if self._count_label and self._count_label.winfo_exists():
            suffix = f"  {len(entries)} of {total} entries" if len(entries) != total else f"  {total} entries"
            self._count_label.configure(text=suffix)
        self._selected_indices = {
            idx for idx in self._selected_indices
            if 0 <= idx < total
        }
        self._update_selection_label()

        if not entries:
            tk.Label(self._list_outer, text="No matches.",
                     bg=self.BG, fg=self.FG2,
                     font=("Segoe UI", 11)).pack(expand=True)
            return

        canvas = tk.Canvas(self._list_outer, bg=self.BG, highlightthickness=0)
        sb = tk.Scrollbar(self._list_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frame = tk.Frame(canvas, bg=self.BG)
        fw = canvas.create_window((0, 0), window=frame, anchor="nw")

        def _resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(fw, width=canvas.winfo_width())
        frame.bind("<Configure>", _resize)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(fw, width=e.width))
        self.win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        for display_idx, (original_idx, entry) in enumerate(entries):
            self._entry_card(frame, entry, original_idx, display_idx)

    def _entry_card(self, parent, entry, original_idx, display_idx):
        card = tk.Frame(parent, bg=self.CARD, padx=16, pady=12)
        card.pack(fill="x", padx=12, pady=(8 if display_idx == 0 else 0, 0))

        # Top row: timestamp + language + backend
        top = tk.Frame(card, bg=self.CARD)
        top.pack(fill="x")
        selected = tk.BooleanVar(value=original_idx in self._selected_indices)
        choose = tk.Checkbutton(top, variable=selected,
                                command=lambda idx=original_idx, var=selected: self._toggle_selection(idx, var.get()),
                                bg=self.CARD, activebackground=self.CARD,
                                selectcolor="#1e1e1e", fg=self.FG2,
                                highlightthickness=0, bd=0, cursor="hand2")
        choose.var = selected
        choose.pack(side="left", padx=(0, 6))
        tk.Label(top, text=entry["timestamp"], bg=self.CARD, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(side="left")

        lang = LANG_NAMES.get(entry.get("language",""), entry.get("language",""))
        backend = "☁" if entry.get("backend") == "google" else "⬡"
        tk.Label(top, text=f"  {backend} {lang}", bg=self.CARD, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(side="left")

        # Copy button
        copy_btn = tk.Button(top, text="Copy", command=lambda t=entry["text"]: pyperclip.copy(t),
                             bg="#1e1e1e", fg="#888888", activebackground="#2a2a2a",
                             font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                             cursor="hand2")
        copy_btn.pack(side="right")
        delete_btn = tk.Button(top, text="Delete", command=lambda idx=original_idx: self._delete(idx),
                               bg="#1e1e1e", fg="#888888", activebackground="#2a2a2a",
                               font=("Segoe UI", 8), relief="flat", padx=8, pady=2,
                               cursor="hand2")
        delete_btn.pack(side="right", padx=(0, 6))

        # Text
        tk.Label(card, text=entry["text"], bg=self.CARD, fg=self.FG,
                 font=("Segoe UI", 10), wraplength=450, justify="left",
                 anchor="w").pack(fill="x", pady=(6, 0))

        tk.Frame(parent, bg=self.SEP, height=1).pack(fill="x", padx=12)

    def _delete(self, idx):
        if hist.delete(idx):
            shifted = set()
            for selected_idx in self._selected_indices:
                if selected_idx == idx:
                    continue
                shifted.add(selected_idx - 1 if selected_idx > idx else selected_idx)
            self._selected_indices = shifted
            self._render_entries()

    def _export(self, kind):
        from tkinter import filedialog, messagebox
        entries, scope = self._entries_for_export()
        if not entries:
            messagebox.showinfo("Nothing to export", "There are no history items in the current selection.", parent=self.win)
            return
        if kind == "csv":
            path = filedialog.asksaveasfilename(
                parent=self.win,
                title="Export history as CSV",
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv")],
            )
            if path:
                count = hist.export_csv(path, entries)
                telemetry.track("history_exported", {"kind": "csv", "scope": scope, "count": count}, cfg, APP_VERSION)
                messagebox.showinfo("History exported", f"Exported {count} {scope} entries.", parent=self.win)
        else:
            path = filedialog.asksaveasfilename(
                parent=self.win,
                title="Export history as text",
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt")],
            )
            if path:
                count = hist.export_txt(path, entries)
                telemetry.track("history_exported", {"kind": "txt", "scope": scope, "count": count}, cfg, APP_VERSION)
                messagebox.showinfo("History exported", f"Exported {count} {scope} entries.", parent=self.win)

    def _clear(self):
        from tkinter import messagebox
        if not messagebox.askyesno("Clear history", "Delete all transcription history?", parent=self.win):
            return
        hist.clear()
        self._selected_indices.clear()
        telemetry.track("history_cleared", {}, cfg, APP_VERSION)
        self._build()

# ── Settings Window ───────────────────────────────────────────────────────────

PALETTE = {
    "Blue":   "#3b82f6",
    "Green":  "#22c55e",
    "Purple": "#a855f7",
    "Pink":   "#ec4899",
    "Orange": "#f97316",
    "White":  "#e5e5e5",
}

class Settings:
    BG     = "#f5f5f7"
    CARD   = "#ffffff"
    BORDER = "#e2e2e7"
    FG     = "#1d1d1f"
    FG2    = "#6e6e73"
    SEL_BG = "#eaf1ff"   # selected card tint

    TABS = ["General", "Model", "Actions", "Language", "Appearance"]

    def __init__(self, root, app):
        self.root         = root
        self.app          = app
        self.win          = None
        self._active_tab  = None
        self._tab_frames  = []
        self._tab_btns    = []
        self._device_choices_cache = None
        self._device_scan_running = False
        self._device_menu = None
        self._model_states = {}
        self._model_progress = {}
        self._model_scan_running = False
        self._action_model_states = {}
        self._action_model_progress = {}
        self._image_refs = []

    def open(self):
        self.root.deiconify()
        telemetry.track("settings_opened", {}, cfg, APP_VERSION)
        if self.win and self.win.winfo_exists():
            self.win.deiconify()
            self._center_window()
            self.win.lift()
            self.win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self.win = win
        win.title("Settings")
        win.resizable(False, False)
        win.configure(bg=self.BG)
        win.geometry("520x640")
        win.attributes("-topmost", True)
        win.transient(self.root)
        win.update_idletasks()

        self._build(win)
        self._center_window()
        win.lift()
        win.focus_force()

    def _center_window(self):
        if not self.win or not self.win.winfo_exists():
            return
        self.win.update_idletasks()
        width = self.win.winfo_width()
        height = self.win.winfo_height()
        x = max(0, (self.win.winfo_screenwidth() - width) // 2)
        y = max(0, (self.win.winfo_screenheight() - height) // 2)
        self.win.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self, win):
        # ── Title bar ─────────────────────────────────────────────────────────
        header = tk.Frame(win, bg=self.BG)
        header.pack(fill="x", padx=24, pady=(20, 0))
        tk.Label(header, text="Settings", bg=self.BG, fg=self.FG,
                 font=("Segoe UI Semibold", 17)).pack(side="left")
        tk.Label(header, text=f"{RAM_GB:.0f} GB RAM · {psutil.cpu_count()} cores",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 9)).pack(side="right", anchor="s", pady=3)

        # ── Tab bar ───────────────────────────────────────────────────────────
        tab_bar = tk.Frame(win, bg=self.BG)
        tab_bar.pack(fill="x", padx=16, pady=(10, 0))
        self._tab_btns = []
        for i, name in enumerate(self.TABS):
            btn = tk.Label(tab_bar, text=name, bg=self.BG, fg=self.FG2,
                           font=("Segoe UI", 10), padx=14, pady=7, cursor="hand2")
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda e, idx=i: self._switch_tab(idx))
            btn.bind("<Enter>",    lambda e, b=btn: b.configure(fg=self.FG))
            btn.bind("<Leave>",    lambda e, b=btn, idx=i: b.configure(
                fg=self.FG if self._active_tab == idx else self.FG2))
            self._tab_btns.append(btn)

        self._tab_indicator = tk.Frame(win, bg=self.BORDER, height=1)
        self._tab_indicator.pack(fill="x", padx=16)
        self._active_line   = tk.Frame(win, bg=cfg["accent_color"], height=2)

        # ── Content area (scrollable) ─────────────────────────────────────────
        content_outer = tk.Frame(win, bg=self.BG)
        content_outer.pack(fill="both", expand=True, pady=(4, 0))
        self._canvas = tk.Canvas(content_outer, bg=self.BG, highlightthickness=0)
        sb = tk.Scrollbar(content_outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._scroll_frame = tk.Frame(self._canvas, bg=self.BG)
        self._fw = self._canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")
        self._scroll_frame.bind("<Configure>", lambda e: (
            self._canvas.configure(scrollregion=self._canvas.bbox("all")),
            self._canvas.itemconfig(self._fw, width=self._canvas.winfo_width())
        ))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(self._fw, width=e.width))
        win.bind("<MouseWheel>", lambda e: self._canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        # ── Init state ────────────────────────────────────────────────────────
        self.backend_var = tk.StringVar(value=cfg["backend"])
        self.api_key_var = tk.StringVar(value=cfg["google_api_key"])
        self.model_var   = tk.StringVar(value=cfg["whisper_model"])
        self.lang_var    = tk.StringVar(value=cfg["language"])
        self.color_var   = tk.StringVar(value=self._color_name())
        self.device_var  = tk.StringVar(value="Default microphone")
        self.mic_result  = tk.StringVar(value="")
        self.silence_var = tk.StringVar(value=str(cfg.get("silence_trigger_sec", DEFAULT["silence_trigger_sec"])))
        self.min_speech_var = tk.StringVar(value=str(cfg.get("min_speech_sec", DEFAULT["min_speech_sec"])))
        self.max_speech_var = tk.StringVar(value=str(cfg.get("max_speech_sec", DEFAULT["max_speech_sec"])))
        self.privacy_mode_var = tk.BooleanVar(value=bool(cfg.get("privacy_mode", False)))
        self.save_history_var = tk.BooleanVar(value=bool(cfg.get("save_history", True)))
        self.analytics_enabled_var = tk.BooleanVar(value=bool(cfg.get("analytics_enabled", False)))
        self.action_mode_var = tk.StringVar(value=actions.normalize_action_mode(cfg.get("output_action")))
        self.action_model_var = tk.StringVar(value=actions.normalize_action_model(cfg.get("action_model")))
        self.translate_target_var = tk.StringVar(value=actions.normalize_translate_target(cfg.get("translate_target")))
        self._captured_hotkey  = cfg["hotkey"]
        self._capturing_hotkey = False
        self._device_map = {}
        self._prompt_value = cfg.get("initial_prompt", "")

        self.test_result = tk.StringVar(value="")

        # Hotkey binding (window-level)
        win.bind("<KeyPress>",  self._hk_keypress, add="+")
        win.bind("<Button-2>",  self._hk_mouse,    add="+")

        # ── Save bar ──────────────────────────────────────────────────────────
        save_bar = tk.Frame(win, bg=self.BG, pady=10)
        save_bar.pack(fill="x", side="bottom", padx=24)
        tk.Frame(save_bar, bg=self.BORDER, height=1).pack(fill="x", pady=(0, 10))
        save_btn = tk.Label(save_bar, text="Save & Apply",
                            bg=cfg["accent_color"], fg="#ffffff",
                            font=("Segoe UI Semibold", 10),
                            padx=24, pady=8, cursor="hand2")
        save_btn.pack(side="right")
        save_btn.bind("<Button-1>", lambda e: self._save())
        save_btn.bind("<Enter>", lambda e: save_btn.configure(bg=self._dim(cfg["accent_color"], 0.85)))
        save_btn.bind("<Leave>", lambda e: save_btn.configure(bg=cfg["accent_color"]))

        # Render first tab
        self._switch_tab(0)

    # ── Tab switching ─────────────────────────────────────────────────────────

    def _switch_tab(self, idx):
        if self._active_tab == idx and self._scroll_frame.winfo_children():
            return
        self._capture_prompt()
        self._active_tab = idx
        # Update tab button styles
        for i, btn in enumerate(self._tab_btns):
            btn.configure(fg=self.FG if i == idx else self.FG2,
                          font=("Segoe UI Semibold", 10) if i == idx else ("Segoe UI", 10))
        # Move underline indicator
        self._active_line.place_forget()
        btn = self._tab_btns[idx]
        btn.update_idletasks()
        x = btn.winfo_x() + btn.winfo_rootx() - self._tab_indicator.winfo_rootx()
        self._active_line.place(in_=self._tab_indicator, x=btn.winfo_x(), y=-2,
                                width=btn.winfo_width(), height=2)
        # Clear and rebuild scroll content
        for w in self._scroll_frame.winfo_children():
            w.destroy()
        [self._build_general, self._build_model, self._build_actions,
         self._build_language, self._build_appearance][idx](self._scroll_frame)
        telemetry.track(
            "settings_tab_opened",
            {"tab": self.TABS[idx].lower()},
            cfg,
            APP_VERSION,
        )
        self._canvas.yview_moveto(0)

    def _capture_prompt(self):
        if hasattr(self, "prompt_text"):
            try:
                if self.prompt_text.winfo_exists():
                    self._prompt_value = self.prompt_text.get("1.0", tk.END).strip()
            except Exception:
                pass

    # ── Tab: General ──────────────────────────────────────────────────────────

    def _build_general(self, f):
        self._build_hotkey_section(f)

        self._section(f, "Privacy")
        self._build_privacy_section(f)

        self._section(f, "Backend")
        bf = tk.Frame(f, bg=self.BG); bf.pack(fill="x", padx=20, pady=(4, 0))
        self._backend_cards_frame = bf
        self._render_backend_cards()

        # Google API key (shown conditionally)
        self.google_section = tk.Frame(f, bg=self.BG)
        self._build_google_section(self.google_section)
        self._toggle_google_section()

        self._section(f, "Microphone")
        self._build_microphone_section(f)

        self._section(f, "Silence Detection")
        self._build_silence_section(f)

        self._section(f, "History")
        self._build_history_section(f)

    def _build_privacy_section(self, parent):
        box = tk.Frame(parent, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
        box.pack(fill="x", padx=20, pady=(4, 16))

        row = tk.Frame(box, bg=self.CARD)
        row.pack(fill="x", padx=14, pady=(10, 2))
        privacy = tk.Checkbutton(row, variable=self.privacy_mode_var,
                                 command=self._privacy_toggled,
                                 bg=self.CARD, activebackground=self.CARD,
                                 selectcolor="#f8f8fb", highlightthickness=0, bd=0,
                                 cursor="hand2")
        privacy.pack(side="left", padx=(0, 8))
        tk.Label(row, text="Privacy Mode",
                 bg=self.CARD, fg=self.FG, font=("Segoe UI Semibold", 9)).pack(side="left")

        tk.Label(box,
                 text="Forces local transcription, disables Google Cloud, turns off analytics, and stops saving History.",
                 bg=self.CARD, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=430, justify="left").pack(anchor="w", padx=42, pady=(0, 10))

        analytics_row = tk.Frame(box, bg=self.CARD)
        analytics_row.pack(fill="x", padx=14, pady=(0, 2))
        self.analytics_check = tk.Checkbutton(analytics_row, variable=self.analytics_enabled_var,
                                              bg=self.CARD, activebackground=self.CARD,
                                              selectcolor="#f8f8fb", highlightthickness=0, bd=0,
                                              cursor="hand2")
        self.analytics_check.pack(side="left", padx=(0, 8))
        tk.Label(analytics_row, text="Share anonymous usage analytics",
                 bg=self.CARD, fg=self.FG, font=("Segoe UI", 9)).pack(side="left")

        tk.Label(box,
                 text="Analytics never includes audio, transcription text, clipboard content, API keys, file paths, or microphone names.",
                 bg=self.CARD, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=430, justify="left").pack(anchor="w", padx=42, pady=(0, 12))
        self._refresh_privacy_controls()

    def _privacy_toggled(self):
        if self.privacy_mode_var.get():
            self.backend_var.set("local")
            self.save_history_var.set(False)
            self.analytics_enabled_var.set(False)
            self.test_result.set("Privacy Mode uses local models only.")
        self._refresh_privacy_controls()
        if hasattr(self, "_backend_cards_frame") and self._backend_cards_frame.winfo_exists():
            self._render_backend_cards()
        if hasattr(self, "google_section") and self.google_section.winfo_exists():
            self._toggle_google_section()

    def _refresh_privacy_controls(self):
        private = bool(self.privacy_mode_var.get())
        if hasattr(self, "analytics_check") and self.analytics_check.winfo_exists():
            self.analytics_check.configure(state="disabled" if private else "normal")
        if hasattr(self, "history_check") and self.history_check.winfo_exists():
            self.history_check.configure(state="disabled" if private else "normal")

    def _build_hotkey_section(self, parent):
        self._section(parent, "Hotkey")
        tk.Label(parent, text="Click this row, then press your preferred shortcut. Esc cancels.",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8)
                 ).pack(anchor="w", padx=20, pady=(2, 6))

        hf = tk.Frame(parent, bg=self.CARD,
                      highlightthickness=1, highlightbackground=self.BORDER)
        hf.pack(fill="x", padx=20, pady=(0, 16))
        self.hotkey_badge = tk.Label(hf,
                                     text=self._fmt_hotkey(self._captured_hotkey),
                                     bg=self.CARD, fg=self.FG,
                                     font=("Segoe UI Semibold", 11),
                                     padx=16, pady=11, anchor="w", cursor="hand2")
        self.hotkey_badge.pack(fill="x")
        self.hotkey_badge.bind("<Button-1>", lambda e: self._start_capture())
        self.hotkey_badge.bind("<Enter>", lambda e: self.hotkey_badge.configure(bg="#f0f0f5"))
        self.hotkey_badge.bind("<Leave>", lambda e: self.hotkey_badge.configure(bg=self.CARD))

    def _render_backend_cards(self):
        """Rebuild ONLY the backend cards section. Avoids full-tab redraw flicker."""
        for w in self._backend_cards_frame.winfo_children():
            w.destroy()
        self._image_refs = []
        google_desc = (
            "Disabled in Privacy Mode"
            if self.privacy_mode_var.get()
            else "Online · bring your own API key · Google billing"
        )
        for val, title, desc, icon, logo in [
            ("local",  "Local (Offline)",  "Private · free · no internet needed", "PC", None),
            ("google", "Google Cloud",     google_desc, "", "google_logo.png"),
        ]:
            self._backend_card(self._backend_cards_frame, val, title, desc, icon, logo)
        self._toggle_google_section()

    def _backend_card(self, parent, val, title, desc, icon, logo_name=None):
        sel   = self.backend_var.get() == val
        disabled = val == "google" and self.privacy_mode_var.get()
        bg    = self.SEL_BG if sel else self.CARD
        bord  = cfg["accent_color"] if sel else self.BORDER
        fg_title = self.FG2 if disabled else self.FG

        card = tk.Frame(parent, bg=bord, padx=1, pady=1, cursor="arrow" if disabled else "hand2")
        card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(card, bg=bg, padx=14, pady=10)
        inner.pack(fill="x")

        row = tk.Frame(inner, bg=bg); row.pack(fill="x")
        dot_c = tk.Canvas(row, width=16, height=16, bg=bg, highlightthickness=0)
        dot_c.pack(side="left", padx=(0, 8))
        if sel:
            dot_c.create_oval(2, 2, 14, 14, outline=cfg["accent_color"], width=2)
            dot_c.create_oval(5, 5, 11, 11, fill=cfg["accent_color"], outline="")
        else:
            dot_c.create_oval(2, 2, 14, 14, outline="#b0b0ba", width=2)

        if logo_name:
            try:
                photo = load_tk_image("assets", logo_name, size=(22, 22))
                self._image_refs.append(photo)
                tk.Label(row, image=photo, bg=bg).pack(side="left", padx=(0, 8))
            except Exception:
                tk.Label(row, text="G", bg=bg, fg="#4285f4",
                         font=("Segoe UI Semibold", 10)).pack(side="left", padx=(0, 8))
        elif icon:
            tk.Label(row, text=icon, bg=bg, fg=self.FG2,
                     font=("Segoe UI Semibold", 9), width=3).pack(side="left", padx=(0, 6))
        tk.Label(row, text=title, bg=bg, fg=fg_title,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        tk.Label(inner, text=desc, bg=bg, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=24)

        def _select(e=None):
            if disabled:
                self.test_result.set("Privacy Mode uses local models only.")
                return
            if self.backend_var.get() == val:
                return
            self.backend_var.set(val)
            self._render_backend_cards()
            telemetry.track("backend_selected", {"backend": val}, cfg, APP_VERSION)
            if val == "google":
                self._prompt_google_api_key()
        for w in [card, inner, row] + list(row.winfo_children()) + list(inner.winfo_children()):
            w.bind("<Button-1>", _select)
            if hasattr(w, 'configure'):
                try: w.configure(cursor="arrow" if disabled else "hand2")
                except: pass

    def _prompt_google_api_key(self):
        if (self.api_key_var.get() or "").strip():
            return
        self.test_result.set("Paste your Google Cloud API key, then click Test Key.")
        if hasattr(self, "test_label") and self.test_label.winfo_exists():
            self.test_label.configure(fg="#f97316")
        if hasattr(self, "google_api_entry") and self.google_api_entry.winfo_exists():
            self.win.after(80, lambda: (
                self.google_api_entry.focus_set(),
                self.google_api_entry.icursor(tk.END),
            ))

    def _build_google_section(self, parent):
        self._section(parent, "Google API Key")
        tk.Label(parent,
                 text="Google Cloud requires your own API key. Transcribe does not include one.",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=460, justify="left").pack(anchor="w", padx=20, pady=(2, 6))
        kf = tk.Frame(parent, bg=self.CARD,
                      highlightthickness=1, highlightbackground=self.BORDER)
        kf.pack(fill="x", padx=20, pady=(4, 4))
        entry_row = tk.Frame(kf, bg=self.CARD); entry_row.pack(fill="x")
        self.google_api_entry = tk.Entry(entry_row,
                                         bg=self.CARD, fg=self.FG,
                                         insertbackground=self.FG,
                                         show="*", font=("Segoe UI", 10),
                                         relief="flat", bd=10)
        self.google_api_entry.pack(fill="x")
        attach_placeholder_entry(
            self.google_api_entry,
            self.api_key_var,
            "Paste your Google Cloud API key here",
            self.FG,
            "#a0a0aa",
            secret_char="*",
        )

        tr = tk.Frame(parent, bg=self.BG); tr.pack(anchor="w", padx=20, pady=(0, 8))
        key_btn = tk.Label(tr, text="Get API Key",
                           bg="#efefef", fg=self.FG,
                           font=("Segoe UI", 9), padx=12, pady=5, cursor="hand2")
        key_btn.pack(side="left", padx=(0, 8))
        key_btn.bind("<Button-1>", lambda e: webbrowser.open(GOOGLE_API_KEY_URL))
        key_btn.bind("<Enter>",    lambda e: key_btn.configure(bg="#e0e0e8"))
        key_btn.bind("<Leave>",    lambda e: key_btn.configure(bg="#efefef"))

        test_btn = tk.Label(tr, text="Test Key",
                            bg="#efefef", fg=self.FG,
                            font=("Segoe UI", 9), padx=12, pady=5, cursor="hand2")
        test_btn.pack(side="left")
        test_btn.bind("<Button-1>", lambda e: self._test_google())
        test_btn.bind("<Enter>",    lambda e: test_btn.configure(bg="#e0e0e8"))
        test_btn.bind("<Leave>",    lambda e: test_btn.configure(bg="#efefef"))
        self.test_label = tk.Label(tr, textvariable=self.test_result,
                                   bg=self.BG, fg=self.FG2, font=("Segoe UI", 9))
        self.test_label.pack(side="left", padx=(10, 0))

    def _toggle_google_section(self):
        if self.backend_var.get() == "google" and not self.privacy_mode_var.get():
            self.google_section.pack(fill="x")
        else:
            self.google_section.pack_forget()

    def _build_microphone_section(self, parent):
        box = tk.Frame(parent, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
        box.pack(fill="x", padx=20, pady=(4, 10))

        row = tk.Frame(box, bg=self.CARD)
        row.pack(fill="x", padx=12, pady=(10, 6))

        choices = self._load_device_choices()
        menu = tk.OptionMenu(row, self.device_var, *choices)
        self._device_menu = menu
        menu.configure(bg=self.CARD, fg=self.FG, activebackground="#f0f0f5",
                       relief="flat", highlightthickness=0, font=("Segoe UI", 9))
        menu.pack(side="left", fill="x", expand=True)

        test_btn = tk.Label(row, text="Test Mic", bg="#efefef", fg=self.FG,
                            font=("Segoe UI", 9), padx=12, pady=5, cursor="hand2")
        test_btn.pack(side="right", padx=(8, 0))
        test_btn.bind("<Button-1>", lambda e: self._test_microphone())
        test_btn.bind("<Enter>", lambda e: test_btn.configure(bg="#e0e0e8"))
        test_btn.bind("<Leave>", lambda e: test_btn.configure(bg="#efefef"))

        self.mic_label = tk.Label(box, textvariable=self.mic_result, bg=self.CARD, fg=self.FG2,
                                  font=("Segoe UI", 8), anchor="w")
        self.mic_label.pack(fill="x", padx=14, pady=(0, 10))

    def _ensure_device_choices_scan(self):
        if self._device_scan_running:
            return
        self._device_scan_running = True

        def _run():
            devices = self.app.recorder.list_input_devices()

            def _apply():
                self._device_scan_running = False
                if not self.win or not self.win.winfo_exists():
                    return
                self._device_choices_cache = devices
                if self._active_tab == 0:
                    self._refresh_device_menu()

            self.app.overlay.call_soon(_apply)

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_device_menu(self):
        if not self._device_menu or not self._device_menu.winfo_exists():
            return
        choices = self._device_choices_from_cache(start_scan=False)
        menu = self._device_menu["menu"]
        menu.delete(0, "end")
        for choice in choices:
            menu.add_command(label=choice, command=tk._setit(self.device_var, choice))

    def _load_device_choices(self):
        return self._device_choices_from_cache(start_scan=True)

    def _device_choices_from_cache(self, start_scan=True):
        self._device_map = {"Default microphone": None}
        current = cfg.get("input_device_index")
        current_label = self.device_var.get()
        selected = "Default microphone"

        if self._device_choices_cache is None:
            self._device_choices_cache = []
            if start_scan:
                self._ensure_device_choices_scan()

        for device in self._device_choices_cache:
            label = f"{device['index']}: {device['name']}"
            self._device_map[label] = device["index"]
            try:
                if current not in (None, "", "default") and int(current) == int(device["index"]):
                    selected = label
            except (TypeError, ValueError):
                selected = "Default microphone"
        choices = list(self._device_map.keys())
        if current_label != "Default microphone" and current_label in self._device_map:
            self.device_var.set(current_label)
        else:
            self.device_var.set(selected if selected in self._device_map else choices[0])
        return choices

    def _build_silence_section(self, parent):
        box = tk.Frame(parent, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
        box.pack(fill="x", padx=20, pady=(4, 16))
        for label, var, hint in [
            ("Stop after silence", self.silence_var, "seconds"),
            ("Minimum speech", self.min_speech_var, "seconds"),
            ("Force chunk after", self.max_speech_var, "seconds"),
        ]:
            row = tk.Frame(box, bg=self.CARD)
            row.pack(fill="x", padx=14, pady=(10 if label == "Stop after silence" else 2, 8))
            tk.Label(row, text=label, bg=self.CARD, fg=self.FG,
                     font=("Segoe UI", 9)).pack(side="left")
            tk.Label(row, text=hint, bg=self.CARD, fg=self.FG2,
                     font=("Segoe UI", 8)).pack(side="right", padx=(6, 0))
            tk.Entry(row, textvariable=var, bg="#f8f8fb", fg=self.FG,
                     insertbackground=self.FG, relief="flat", width=8,
                     justify="right", font=("Segoe UI", 9)).pack(side="right")

    def _build_history_section(self, parent):
        box = tk.Frame(parent, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
        box.pack(fill="x", padx=20, pady=(4, 16))

        row = tk.Frame(box, bg=self.CARD)
        row.pack(fill="x", padx=14, pady=(10, 2))
        self.history_check = tk.Checkbutton(row, variable=self.save_history_var,
                                            bg=self.CARD, activebackground=self.CARD,
                                            selectcolor="#f8f8fb", highlightthickness=0, bd=0,
                                            cursor="hand2")
        self.history_check.pack(side="left", padx=(0, 8))
        tk.Label(row, text="Save transcription history",
                 bg=self.CARD, fg=self.FG,
                 font=("Segoe UI", 9)).pack(side="left")

        tk.Label(box,
                 text="Turn this off to keep future transcriptions out of History.",
                 bg=self.CARD, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=430, justify="left").pack(anchor="w", padx=42, pady=(0, 10))

        clear = tk.Label(box, text="Clear Existing History",
                         bg="#efefef", fg=self.FG, font=("Segoe UI", 9),
                         padx=12, pady=5, cursor="hand2")
        clear.pack(anchor="w", padx=42, pady=(0, 12))
        clear.bind("<Button-1>", lambda e: self._clear_history_from_settings())
        clear.bind("<Enter>", lambda e: clear.configure(bg="#e0e0e8"))
        clear.bind("<Leave>", lambda e: clear.configure(bg="#efefef"))
        self._refresh_privacy_controls()

    def _clear_history_from_settings(self):
        from tkinter import messagebox
        if not messagebox.askyesno("Clear history", "Delete all transcription history?", parent=self.win):
            return
        hist.clear()
        if self.app.history.win and self.app.history.win.winfo_exists():
            self.app.history._selected_indices.clear()
            self.app.history._build()
        messagebox.showinfo("History cleared", "All saved transcription history was deleted.", parent=self.win)

    def _selected_device_index(self):
        return self._device_map.get(self.device_var.get())

    def _test_microphone(self):
        self.mic_result.set("Listening…")
        device_index = self._selected_device_index()

        def _run():
            level, err = self.app.recorder.test_input_level(device_index)
            if err:
                msg, col = f"✕ {err}", "#ef4444"
            elif level > 0.01:
                msg, col = "✓ Microphone is receiving audio", "#22c55e"
            else:
                msg, col = "No clear signal detected", "#f97316"

            def _apply():
                if self.win and self.win.winfo_exists():
                    self.mic_result.set(msg)
                    if hasattr(self, "mic_label") and self.mic_label.winfo_exists():
                        self.mic_label.configure(fg=col)
            self.app.overlay.call_soon(_apply)

        threading.Thread(target=_run, daemon=True).start()

    # ── Tab: Model ────────────────────────────────────────────────────────────

    def _build_model(self, f):
        if self.backend_var.get() == "google":
            self._build_google_model_notice(f)
            return

        # Armenian tip banner
        tip = tk.Frame(f, bg="#fffbeb", highlightthickness=1, highlightbackground="#fde68a")
        tip.pack(fill="x", padx=20, pady=(12, 4))
        tip_inner = tk.Frame(tip, bg="#fffbeb"); tip_inner.pack(fill="x", padx=12, pady=8)
        tk.Label(tip_inner, text="For Armenian:", bg="#fffbeb", fg="#92400e",
                 font=("Segoe UI Semibold", 9)).pack(side="left")
        tk.Label(tip_inner,
                 text="  large-v3-turbo is the recommended local model.",
                 bg="#fffbeb", fg="#78350f", font=("Segoe UI", 9)).pack(side="left")

        self._section(f, "Whisper Models")
        mf = tk.Frame(f, bg=self.BG); mf.pack(fill="x", padx=20, pady=(4, 0))
        self._model_cards_frame = mf
        self._render_model_cards()

        self._section(f, "GPU-Accelerated")
        gf = tk.Frame(f, bg=self.BG); gf.pack(fill="x", padx=20, pady=(4, 16))
        self._nemo_card(gf)

    def _build_google_model_notice(self, f):
        self._section(f, "Google Cloud")
        box = tk.Frame(f, bg=self.CARD, highlightthickness=1, highlightbackground=self.BORDER)
        box.pack(fill="x", padx=20, pady=(10, 16))

        tk.Label(box, text="Google Cloud Speech-to-Text",
                 bg=self.CARD, fg=self.FG, font=("Segoe UI Semibold", 11),
                 anchor="w").pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(box,
                 text="Transcribe sends 16 kHz LINEAR16 audio to Google's Speech-to-Text v1 API. "
                      "No local Whisper model is used or downloaded.",
                 bg=self.CARD, fg=self.FG2, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=440).pack(fill="x", padx=14, pady=(0, 12))

        details = [
            ("Model", "Google auto-selects the recognition model from the request config."),
            ("Why auto", "The app does not hard-code latest_long/latest_short so Armenian support stays broad."),
            ("Language", "Auto-detect starts with Armenian plus English/Russian alternatives."),
            ("Privacy", "Audio is sent to Google and billed to your Google Cloud project/API key."),
        ]
        for label, value in details:
            row = tk.Frame(box, bg=self.CARD); row.pack(fill="x", padx=14, pady=(0, 6))
            tk.Label(row, text=f"{label}: ", bg=self.CARD, fg=self.FG,
                     font=("Segoe UI Semibold", 8), width=10, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=self.CARD, fg=self.FG2,
                     font=("Segoe UI", 8), wraplength=340, justify="left",
                     anchor="w").pack(side="left", fill="x", expand=True)

        if not (self.api_key_var.get() or "").strip():
            warn = tk.Frame(box, bg="#fffbeb", highlightthickness=1, highlightbackground="#fde68a")
            warn.pack(fill="x", padx=14, pady=(4, 10))
            tk.Label(warn, text="API key needed before Google Cloud can be used.",
                     bg="#fffbeb", fg="#92400e", font=("Segoe UI Semibold", 9),
                     anchor="w").pack(fill="x", padx=10, pady=(8, 2))
            tk.Label(warn, text="Open General, paste your Google Cloud key, then click Test Key.",
                     bg="#fffbeb", fg="#78350f", font=("Segoe UI", 8),
                     anchor="w", justify="left", wraplength=390).pack(fill="x", padx=10, pady=(0, 8))

        btns = tk.Frame(box, bg=self.CARD); btns.pack(anchor="w", padx=14, pady=(0, 12))
        key_btn = tk.Label(btns, text="Add API Key",
                           bg=cfg["accent_color"], fg="#ffffff", font=("Segoe UI", 9),
                           padx=12, pady=6, cursor="hand2")
        key_btn.pack(side="left", padx=(0, 8))
        key_btn.bind("<Button-1>", lambda e: (self._switch_tab(0), self._prompt_google_api_key()))
        key_btn.bind("<Enter>", lambda e: key_btn.configure(bg=self._dim(cfg["accent_color"], 0.85)))
        key_btn.bind("<Leave>", lambda e: key_btn.configure(bg=cfg["accent_color"]))

        docs_btn = tk.Label(btns, text="Model Docs",
                            bg="#efefef", fg=self.FG, font=("Segoe UI", 9),
                            padx=12, pady=6, cursor="hand2")
        docs_btn.pack(side="left", padx=(0, 8))
        docs_btn.bind("<Button-1>", lambda e: webbrowser.open(GOOGLE_SPEECH_DOCS_URL))
        docs_btn.bind("<Enter>", lambda e: docs_btn.configure(bg="#e0e0e8"))
        docs_btn.bind("<Leave>", lambda e: docs_btn.configure(bg="#efefef"))

        btn = tk.Label(btns, text="Use Local Models",
                       bg="#efefef", fg=self.FG, font=("Segoe UI", 9),
                       padx=12, pady=6, cursor="hand2")
        btn.pack(side="left")
        btn.bind("<Button-1>", lambda e: self._switch_to_local_models())
        btn.bind("<Enter>", lambda e: btn.configure(bg="#e0e0e8"))
        btn.bind("<Leave>", lambda e: btn.configure(bg="#efefef"))

    def _switch_to_local_models(self):
        self.backend_var.set("local")
        self._switch_tab(1)

    def _ensure_model_state_scan(self):
        if self._model_scan_running:
            return
        self._model_scan_running = True

        def _run():
            results = {}
            for name in MODELS:
                if not model_ok(name):
                    results[name] = "locked"
                    continue
                results[name] = "downloaded" if model_downloaded(name) else "missing"

            def _apply():
                self._model_scan_running = False
                if not self.win or not self.win.winfo_exists():
                    return
                changed = False
                for name, state in results.items():
                    current = self._model_states.get(name)
                    if current in (None, "checking"):
                        self._model_states[name] = state
                        changed = True
                if changed and self._active_tab == 1 and hasattr(self, "_model_cards_frame"):
                    try:
                        if self._model_cards_frame.winfo_exists():
                            self._render_model_cards()
                    except Exception:
                        pass

            self.app.overlay.call_soon(_apply)

        threading.Thread(target=_run, daemon=True).start()

    def _model_state(self, name):
        if name not in self._model_states:
            self._model_states[name] = "checking"
            self._ensure_model_state_scan()
        return self._model_states[name]

    def _download_model(self, name):
        if self._model_states.get(name) == "downloading":
            return
        self._model_states[name] = "downloading"
        self._model_progress[name] = {"percent": 0, "downloaded": 0, "total": 0}
        self._render_model_cards()
        telemetry.track("model_download_started", {"model": name}, cfg, APP_VERSION)

        def _on_progress(percent, downloaded, total):
            def _apply():
                if not self.win or not self.win.winfo_exists():
                    return
                if self._model_states.get(name) != "downloading":
                    return
                self._model_progress[name] = {
                    "percent": percent,
                    "downloaded": downloaded,
                    "total": total,
                }
                if self._active_tab == 1 and hasattr(self, "_model_cards_frame"):
                    try:
                        if self._model_cards_frame.winfo_exists():
                            self._render_model_cards()
                    except Exception:
                        pass

            self.app.overlay.call_soon(_apply)

        def _run():
            try:
                download_whisper_model(name, on_progress=_on_progress)
                state = "downloaded"
            except Exception as e:
                logger.warning("Could not download model %s: %s", name, e)
                state = "failed"

            def _apply():
                self._model_states[name] = state
                if state == "downloaded":
                    self.model_var.set(name)
                    self._model_progress[name] = {"percent": 100, "downloaded": 0, "total": 0}
                    telemetry.track("model_download_completed", {"model": name}, cfg, APP_VERSION)
                    self.app.show_tray_hint(
                        "Model download complete",
                        f"{name} is ready to use in Transcribe.",
                    )
                elif state == "failed":
                    telemetry.track("model_download_failed", {"model": name}, cfg, APP_VERSION)
                    self.app.show_tray_hint(
                        "Model download failed",
                        f"Could not download {name}. Check your connection and try again.",
                    )
                if self.win and self.win.winfo_exists():
                    self._render_model_cards()

            self.app.overlay.call_soon(_apply)

        threading.Thread(target=_run, daemon=True).start()

    def _remove_model(self, name):
        if self._model_states.get(name) in ("downloading", "removing"):
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Remove local model",
            f"Remove the {name} model from this computer?\n\nYou can download it again later.",
            parent=self.win,
        ):
            return
        self._model_states[name] = "removing"
        self._render_model_cards()

        def _run():
            ok = False
            try:
                self.app.recorder.unload_model(name)
                ok = remove_whisper_model(name)
            except Exception as e:
                logger.warning("Could not remove model %s: %s", name, e)

            def _apply():
                if ok:
                    self._model_states[name] = "missing"
                    self._model_progress.pop(name, None)
                    telemetry.track("model_removed", {"model": name}, cfg, APP_VERSION)
                    self.app.show_tray_hint(
                        "Model removed",
                        f"{name} was removed from this computer.",
                    )
                else:
                    self._model_states[name] = "downloaded" if model_downloaded(name) else "missing"
                    self.app.show_tray_hint(
                        "Model removal failed",
                        f"Could not remove {name}. It may still be in use.",
                    )
                if self.win and self.win.winfo_exists():
                    self._render_model_cards()

            self.app.overlay.call_soon(_apply)

        threading.Thread(target=_run, daemon=True).start()

    def _render_model_cards(self):
        for w in self._model_cards_frame.winfo_children():
            w.destroy()
        for name, info in MODELS.items():
            self._model_card(self._model_cards_frame, name, info)

    def _format_bytes(self, value):
        try:
            value = float(value or 0)
        except (TypeError, ValueError):
            value = 0
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{value:.1f} GB"

    def _progress_bar(self, parent, percent, bg, width=116, height=8):
        canvas = tk.Canvas(parent, width=width, height=height, bg=bg, highlightthickness=0)
        canvas.pack(pady=(4, 2))
        canvas.create_rectangle(0, 0, width, height, fill="#e5e7eb", outline="")
        if percent is None:
            pulse_w = max(24, width // 3)
            phase = int((time.time() * 90) % (width + pulse_w)) - pulse_w
            canvas.create_rectangle(max(0, phase), 0, min(width, phase + pulse_w),
                                    height, fill=cfg["accent_color"], outline="")
        else:
            fill_w = int(width * max(0, min(100, percent)) / 100)
            canvas.create_rectangle(0, 0, fill_w, height, fill=cfg["accent_color"], outline="")

    def _model_card(self, parent, name, info):
        ram_ok = model_ok(name)
        state = self._model_state(name) if ram_ok else "locked"
        downloaded = state == "downloaded"
        selected = self.model_var.get() == name
        remove_btn = None

        bg = self.SEL_BG if selected else (self.CARD if ram_ok else "#f9f9f9")
        bord = cfg["accent_color"] if selected else (self.BORDER if ram_ok else "#ebebeb")
        fg = self.FG if ram_ok else "#b0b0ba"
        fg2 = self.FG2 if ram_ok else "#c8c8d0"

        card = tk.Frame(parent, bg=bord, padx=1, pady=1)
        card.pack(fill="x", pady=(0, 6))
        inner = tk.Frame(card, bg=bg, padx=14, pady=9); inner.pack(fill="x")

        left  = tk.Frame(inner, bg=bg); left.pack(side="left",  fill="x", expand=True)
        right = tk.Frame(inner, bg=bg); right.pack(side="right", anchor="center")

        name_row = tk.Frame(left, bg=bg); name_row.pack(anchor="w")
        tk.Label(name_row, text=name, bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        if selected and downloaded:
            tag, tag_fg = "  selected", cfg["accent_color"]
        elif not ram_ok:
            tag, tag_fg = "  not enough RAM", "#c8c8d0"
        elif state == "checking":
            tag, tag_fg = "  checking", "#6e6e73"
        elif state == "downloading":
            progress = self._model_progress.get(name, {})
            percent = progress.get("percent")
            suffix = f" {percent}%" if percent is not None else ""
            tag, tag_fg = f"  downloading{suffix}", "#f97316"
        elif state == "removing":
            tag, tag_fg = "  removing", "#f97316"
        elif state == "failed":
            tag, tag_fg = "  download failed", "#ef4444"
        elif downloaded:
            tag, tag_fg = "  downloaded", "#22c55e"
        elif selected:
            tag, tag_fg = "  selected, not downloaded", "#f97316"
        else:
            tag, tag_fg = "  not downloaded", "#f97316"
        tk.Label(name_row, text=tag, bg=bg, fg=tag_fg,
                 font=("Segoe UI", 8)).pack(side="left")

        subtitle = f"{info['quality']}  ·  {info['size']}"
        tk.Label(left, text=subtitle, bg=bg, fg=fg2, font=("Segoe UI", 8)).pack(anchor="w")

        hy_label = info.get("armenian")
        if hy_label and ram_ok:
            badge_bg, badge_fg = "#dbeafe", "#1e40af"
            tk.Label(left, text=hy_label,
                     bg=badge_bg, fg=badge_fg,
                     font=("Segoe UI", 7), padx=6, pady=2).pack(anchor="w", pady=(3, 0))

        if not ram_ok:
            tk.Label(right, text=info["speed"], bg=bg, fg=fg,
                     font=("Segoe UI Semibold", 13)).pack()
            tk.Label(right, text="per clip", bg=bg, fg=fg2,
                     font=("Segoe UI", 7)).pack()
            tk.Label(right, text=f"Need {info['min_ram']} GB RAM",
                     bg=bg, fg=fg2, font=("Segoe UI", 7)).pack()
        elif state == "checking":
            tk.Label(right, text="Checking...", bg=bg, fg=fg2,
                     font=("Segoe UI Semibold", 9)).pack()
        elif downloaded:
            tk.Label(right, text=info["speed"], bg=bg, fg=fg,
                     font=("Segoe UI Semibold", 13)).pack()
            tk.Label(right, text="per clip", bg=bg, fg=fg2,
                     font=("Segoe UI", 7)).pack()
            remove_btn = tk.Label(right, text="Remove",
                                  bg="#fee2e2", fg="#991b1b",
                                  font=("Segoe UI", 7), padx=8, pady=3,
                                  cursor="hand2")
            remove_btn.pack(pady=(4, 0))
            remove_btn.bind("<Button-1>", lambda e, n=name: self._remove_model(n))
            remove_btn.bind("<Enter>", lambda e: remove_btn.configure(bg="#fecaca"))
            remove_btn.bind("<Leave>", lambda e: remove_btn.configure(bg="#fee2e2"))
        elif state == "downloading":
            progress = self._model_progress.get(name, {})
            percent = progress.get("percent")
            downloaded_bytes = progress.get("downloaded", 0)
            total_bytes = progress.get("total", 0)
            text = f"{percent}%" if percent is not None else "Downloading"
            tk.Label(right, text=text, bg=bg, fg=cfg["accent_color"],
                     font=("Segoe UI Semibold", 10)).pack()
            self._progress_bar(right, percent, bg)
            if total_bytes >= 1024:
                detail = f"{self._format_bytes(downloaded_bytes)} / {self._format_bytes(total_bytes)}"
            elif total_bytes:
                detail = f"{int(downloaded_bytes)} / {int(total_bytes)} files"
            else:
                detail = "Starting..."
            tk.Label(right, text=detail, bg=bg, fg=fg2,
                     font=("Segoe UI", 7)).pack()
        elif state == "removing":
            tk.Label(right, text="Removing...", bg=bg, fg="#f97316",
                     font=("Segoe UI Semibold", 9)).pack()
        else:
            label = "Downloading..." if state == "downloading" else "Retry" if state == "failed" else "Download"
            btn_bg = "#e8eefc" if state == "downloading" else cfg["accent_color"]
            btn_fg = self.FG2 if state == "downloading" else "#ffffff"
            btn = tk.Label(right, text=label, bg=btn_bg, fg=btn_fg,
                           font=("Segoe UI Semibold", 9),
                           padx=12, pady=6, cursor="hand2")
            btn.pack()
            if state != "downloading":
                btn.bind("<Button-1>", lambda e, n=name: self._download_model(n))
                btn.bind("<Enter>", lambda e: btn.configure(bg=self._dim(cfg["accent_color"], 0.85)))
                btn.bind("<Leave>", lambda e: btn.configure(bg=cfg["accent_color"]))
                def _start_download(e=None, n=name):
                    self._download_model(n)
                for w in [card, inner, left, right, name_row] + \
                         list(left.winfo_children()) + list(name_row.winfo_children()):
                    try: w.configure(cursor="hand2")
                    except: pass
                    w.bind("<Button-1>", _start_download)

        if ram_ok and downloaded:
            def _pick(e=None, n=name):
                if self.model_var.get() == n:
                    return
                self.model_var.set(n)
                self._render_model_cards()
            for w in [card, inner, left, right, name_row] + \
                     list(left.winfo_children()) + list(name_row.winfo_children()) + \
                     list(right.winfo_children()):
                if w is remove_btn:
                    continue
                try: w.configure(cursor="hand2")
                except: pass
                w.bind("<Button-1>", _pick)
            if remove_btn is not None:
                remove_btn.configure(cursor="hand2")
                remove_btn.bind("<Button-1>", lambda e, n=name: self._remove_model(n))
            inner.bind("<Enter>", lambda e: inner.configure(bg=self._tint(bg)))
            inner.bind("<Leave>", lambda e: inner.configure(bg=bg))

    def _nemo_card(self, parent):
        gpu  = HAS_GPU
        bg   = "#f9f9f9" if not gpu else self.CARD
        bord = "#ebebeb"  if not gpu else self.BORDER
        fg   = "#b0b0ba"  if not gpu else self.FG
        fg2  = "#c8c8d0"  if not gpu else self.FG2

        card  = tk.Frame(parent, bg=bord, padx=1, pady=1); card.pack(fill="x")
        inner = tk.Frame(card, bg=bg, padx=14, pady=9);    inner.pack(fill="x")
        left  = tk.Frame(inner, bg=bg); left.pack(side="left",  fill="x", expand=True)
        right = tk.Frame(inner, bg=bg); right.pack(side="right", anchor="center")

        name_row = tk.Frame(left, bg=bg); name_row.pack(anchor="w")
        tk.Label(name_row, text="NeMo FastConformer", bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        badge_fg  = "#22c55e" if gpu else "#b0b0ba"
        badge_txt = "  GPU ready" if gpu else "  No GPU"
        tk.Label(name_row, text=badge_txt, bg=bg, fg=badge_fg,
                 font=("Segoe UI", 8)).pack(side="left")

        tk.Label(left, text="Experimental GPU-only model · coming soon",
                 bg=bg, fg=fg2, font=("Segoe UI", 8)).pack(anchor="w")

        tk.Label(right, text="~2s",    bg=bg, fg=fg, font=("Segoe UI Semibold", 13)).pack()
        tk.Label(right, text="per clip", bg=bg, fg=fg2, font=("Segoe UI", 7)).pack()
        tk.Label(right, text="Coming soon" if gpu else "GPU required",
                 bg=bg, fg=fg2, font=("Segoe UI", 7)).pack()

    # ── Tab: Actions ─────────────────────────────────────────────────────────

    def _build_actions(self, f):
        self._section(f, "Output Mode")
        tk.Label(f,
                 text="Transcribe only pastes the raw transcription. Action modes process it locally before copying and pasting.",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=460, justify="left").pack(anchor="w", padx=20, pady=(2, 6))

        self._action_cards_frame = tk.Frame(f, bg=self.BG)
        self._action_cards_frame.pack(fill="x", padx=20, pady=(4, 12))
        self._render_action_cards()

        self._translate_options = tk.Frame(f, bg=self.BG)
        self._section(self._translate_options, "Translate To")
        target_box = tk.Frame(self._translate_options, bg=self.CARD,
                              highlightthickness=1, highlightbackground=self.BORDER)
        target_box.pack(fill="x", padx=20, pady=(4, 10))
        labels = [f"{name} ({code})" for code, name in actions.TRANSLATE_TARGETS.items()]
        self._target_label_to_code = {
            f"{name} ({code})": code for code, name in actions.TRANSLATE_TARGETS.items()
        }
        current = actions.TRANSLATE_TARGETS[self.translate_target_var.get()]
        current_label = f"{current} ({self.translate_target_var.get()})"
        self.translate_target_label_var = tk.StringVar(value=current_label)
        menu = tk.OptionMenu(target_box, self.translate_target_label_var, *labels)
        menu.configure(bg=self.CARD, fg=self.FG, activebackground="#f0f0f5",
                       relief="flat", highlightthickness=0, font=("Segoe UI", 9))
        menu.pack(fill="x", padx=12, pady=10)
        tk.Label(self._translate_options,
                 text="Translation stays local only when Argos Translate and the matching offline language pack are installed.",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=460, justify="left").pack(anchor="w", padx=20, pady=(0, 12))
        self._sync_translate_options()

        self._section(f, "Local Action Engine")
        tk.Label(f,
                 text="Built-in local actions work now. Aibuben local LLM slots are prepared for future downloadable CPU/GPU models.",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=460, justify="left").pack(anchor="w", padx=20, pady=(2, 6))
        self._action_model_cards_frame = tk.Frame(f, bg=self.BG)
        self._action_model_cards_frame.pack(fill="x", padx=20, pady=(4, 16))
        self._render_action_model_cards()

    def _render_action_cards(self):
        for w in self._action_cards_frame.winfo_children():
            w.destroy()
        for code, info in actions.ACTION_MODES.items():
            self._action_card(self._action_cards_frame, code, info["label"], info["description"])

    def _action_card(self, parent, code, title, desc):
        sel = self.action_mode_var.get() == code
        bg = self.SEL_BG if sel else self.CARD
        bord = cfg["accent_color"] if sel else self.BORDER
        card = tk.Frame(parent, bg=bord, padx=1, pady=1, cursor="hand2")
        card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(card, bg=bg, padx=14, pady=10)
        inner.pack(fill="x")
        row = tk.Frame(inner, bg=bg); row.pack(fill="x")
        dot = tk.Canvas(row, width=16, height=16, bg=bg, highlightthickness=0)
        dot.pack(side="left", padx=(0, 8))
        if sel:
            dot.create_oval(2, 2, 14, 14, outline=cfg["accent_color"], width=2)
            dot.create_oval(5, 5, 11, 11, fill=cfg["accent_color"], outline="")
        else:
            dot.create_oval(2, 2, 14, 14, outline="#b0b0ba", width=2)
        tk.Label(row, text=title, bg=bg, fg=self.FG,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        tk.Label(inner, text=desc, bg=bg, fg=self.FG2,
                 font=("Segoe UI", 8), wraplength=420, justify="left").pack(anchor="w", padx=24)

        def _select(_event=None):
            if self.action_mode_var.get() == code:
                return
            self.action_mode_var.set(code)
            self._render_action_cards()
            self._sync_translate_options()
        for w in [card, inner, row] + list(row.winfo_children()) + list(inner.winfo_children()):
            w.bind("<Button-1>", _select)
            try: w.configure(cursor="hand2")
            except Exception: pass

    def _sync_translate_options(self):
        if not hasattr(self, "_translate_options"):
            return
        if self.action_mode_var.get() == actions.ACTION_TRANSLATE:
            if not self._translate_options.winfo_ismapped():
                self._translate_options.pack(fill="x", after=self._action_cards_frame)
        else:
            self._translate_options.pack_forget()

    def _render_action_model_cards(self):
        for w in self._action_model_cards_frame.winfo_children():
            w.destroy()
        for code, info in actions.ACTION_MODELS.items():
            state = self._action_model_state(code, info)
            self._action_model_card(
                self._action_model_cards_frame,
                code,
                info["label"],
                info["description"],
                info["available"],
                state,
            )

    def _action_model_state(self, code, info):
        if code == "built_in":
            return "ready"
        if not info.get("available"):
            return "coming_soon"
        if self._action_model_states.get(code) in ("downloading", "failed", "removing"):
            return self._action_model_states[code]
        if code == local_llm.QWEN_TINY_ID:
            state = "downloaded" if local_llm.model_downloaded(code) else "missing"
            self._action_model_states[code] = state
            return state
        return "coming_soon"

    def _download_action_model(self, code):
        if code != local_llm.QWEN_TINY_ID or self._action_model_states.get(code) == "downloading":
            return
        self._action_model_states[code] = "downloading"
        self._action_model_progress[code] = {"percent": 0, "downloaded": 0, "total": local_llm.QWEN_TINY_SIZE}
        self._render_action_model_cards()
        telemetry.track("model_download_started", {"model": code, "kind": "action_llm"}, cfg, APP_VERSION)

        def _on_progress(percent, downloaded, total):
            def _apply():
                if not self.win or not self.win.winfo_exists():
                    return
                if self._action_model_states.get(code) != "downloading":
                    return
                self._action_model_progress[code] = {
                    "percent": percent,
                    "downloaded": downloaded,
                    "total": total,
                }
                if self._active_tab == 2 and hasattr(self, "_action_model_cards_frame"):
                    self._render_action_model_cards()

            self.app.overlay.call_soon(_apply)

        def _run():
            try:
                local_llm.download_model(code, on_progress=_on_progress)
                state = "downloaded"
            except Exception as e:
                logger.warning("Could not download action model %s: %s", code, e)
                state = "failed"

            def _apply():
                self._action_model_states[code] = state
                if state == "downloaded":
                    self.action_model_var.set(code)
                    self._action_model_progress[code] = {"percent": 100, "downloaded": 0, "total": 0}
                    telemetry.track("model_download_completed", {"model": code, "kind": "action_llm"}, cfg, APP_VERSION)
                    self.app.show_tray_hint("Action model ready", "Qwen Tiny is ready for local actions.")
                else:
                    telemetry.track("model_download_failed", {"model": code, "kind": "action_llm"}, cfg, APP_VERSION)
                    self.app.show_tray_hint("Action model download failed", "Could not download Qwen Tiny. Check your connection.")
                if self.win and self.win.winfo_exists():
                    self._render_action_model_cards()

            self.app.overlay.call_soon(_apply)

        threading.Thread(target=_run, daemon=True).start()

    def _remove_action_model(self, code):
        if code != local_llm.QWEN_TINY_ID or self._action_model_states.get(code) in ("downloading", "removing"):
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Remove action model",
            "Remove Qwen Tiny from this computer?\n\nYou can download it again later.",
            parent=self.win,
        ):
            return
        self._action_model_states[code] = "removing"
        self._render_action_model_cards()

        def _run():
            ok = False
            try:
                ok = local_llm.remove_model(code)
            except Exception as e:
                logger.warning("Could not remove action model %s: %s", code, e)

            def _apply():
                self._action_model_states[code] = "missing" if ok else (
                    "downloaded" if local_llm.model_downloaded(code) else "missing"
                )
                self._action_model_progress.pop(code, None)
                if self.action_model_var.get() == code:
                    self.action_model_var.set("built_in")
                if ok:
                    telemetry.track("model_removed", {"model": code, "kind": "action_llm"}, cfg, APP_VERSION)
                    self.app.show_tray_hint("Action model removed", "Qwen Tiny was removed from this computer.")
                else:
                    self.app.show_tray_hint("Action model removal failed", "Qwen Tiny could not be removed.")
                if self.win and self.win.winfo_exists():
                    self._render_action_model_cards()

            self.app.overlay.call_soon(_apply)

        threading.Thread(target=_run, daemon=True).start()

    def _action_model_card(self, parent, code, title, desc, available, state):
        sel = self.action_model_var.get() == code
        usable = available and state in ("ready", "downloaded")
        downloadable = available and state in ("missing", "failed")
        bg = self.SEL_BG if sel else (self.CARD if available else "#f9f9f9")
        bord = cfg["accent_color"] if sel else (self.BORDER if available else "#ebebeb")
        card = tk.Frame(parent, bg=bord, padx=1, pady=1, cursor="hand2" if usable else "arrow")
        card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(card, bg=bg, padx=14, pady=10)
        inner.pack(fill="x")
        row = tk.Frame(inner, bg=bg); row.pack(fill="x")
        tk.Label(row, text=title, bg=bg, fg=self.FG if available else self.FG2,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        tag_map = {
            "ready": ("Ready", "#22c55e"),
            "downloaded": ("Ready", "#22c55e"),
            "missing": ("Not downloaded", "#f97316"),
            "failed": ("Download failed", "#ef4444"),
            "downloading": ("Downloading", "#f97316"),
            "removing": ("Removing", "#f97316"),
            "coming_soon": ("Coming soon", "#f97316"),
        }
        tag, tag_fg = tag_map.get(state, ("Coming soon", "#f97316"))
        tk.Label(row, text=tag, bg=bg, fg=tag_fg,
                 font=("Segoe UI", 8)).pack(side="right")
        tk.Label(inner, text=desc, bg=bg, fg=self.FG2,
                 font=("Segoe UI", 8), wraplength=420, justify="left").pack(anchor="w", pady=(4, 0))

        if state == "downloading":
            progress = self._action_model_progress.get(code, {})
            percent = progress.get("percent")
            downloaded = progress.get("downloaded", 0)
            total = progress.get("total", 0)
            self._progress_bar(inner, percent, bg)
            detail = (
                f"{self._format_bytes(downloaded)} / {self._format_bytes(total)}"
                if total else "Starting..."
            )
            tk.Label(inner, text=detail, bg=bg, fg=self.FG2,
                     font=("Segoe UI", 7)).pack(anchor="w", pady=(3, 0))
        elif downloadable:
            label = "Retry" if state == "failed" else "Download"
            btn = tk.Label(inner, text=label, bg=cfg["accent_color"], fg="#ffffff",
                           font=("Segoe UI Semibold", 9), padx=12, pady=5,
                           cursor="hand2")
            btn.pack(anchor="w", pady=(8, 0))
            btn.bind("<Button-1>", lambda e, c=code: self._download_action_model(c))
            btn.bind("<Enter>", lambda e: btn.configure(bg=self._dim(cfg["accent_color"], 0.85)))
            btn.bind("<Leave>", lambda e: btn.configure(bg=cfg["accent_color"]))
        elif state == "downloaded":
            remove = tk.Label(inner, text="Remove", bg="#fee2e2", fg="#991b1b",
                              font=("Segoe UI", 8), padx=10, pady=4,
                              cursor="hand2")
            remove.pack(anchor="w", pady=(8, 0))
            remove.bind("<Button-1>", lambda e, c=code: self._remove_action_model(c))
            remove.bind("<Enter>", lambda e: remove.configure(bg="#fecaca"))
            remove.bind("<Leave>", lambda e: remove.configure(bg="#fee2e2"))

        def _select(_event=None):
            if not usable:
                return
            self.action_model_var.set(code)
            self._render_action_model_cards()
        for w in [card, inner, row] + list(row.winfo_children()):
            w.bind("<Button-1>", _select)
            try: w.configure(cursor="hand2" if usable else "arrow")
            except Exception: pass

    # ── Tab: Language ─────────────────────────────────────────────────────────

    def _build_language(self, f):
        self._section(f, "Recognition Language")
        lf = tk.Frame(f, bg=self.BG); lf.pack(fill="x", padx=20, pady=(6, 0))
        self._lang_pills_frame = lf
        self._render_lang_pills()

        self._section(f, "Custom Vocabulary / Prompt")
        tk.Label(f, text="Words or phrases to improve recognition (names, technical terms, Armenian nouns)",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=460, justify="left").pack(anchor="w", padx=20, pady=(2, 6))
        pf = tk.Frame(f, bg=self.CARD,
                      highlightthickness=1, highlightbackground=self.BORDER)
        pf.pack(fill="x", padx=20, pady=(0, 16))
        self.prompt_text = tk.Text(pf, height=4, bg=self.CARD, fg=self.FG,
                                   insertbackground=self.FG, font=("Segoe UI", 9),
                                   relief="flat", padx=10, pady=8, wrap="word")
        self.prompt_text.pack(fill="x")
        self.prompt_text.insert("1.0", self._prompt_value)

    def _render_lang_pills(self):
        for w in self._lang_pills_frame.winfo_children():
            w.destroy()
        for i, (code, name) in enumerate(LANG_NAMES.items()):
            self._lang_pill(self._lang_pills_frame, code, name, i)

    def _lang_pill(self, parent, code, name, idx):
        sel = self.lang_var.get() == code
        bg  = cfg["accent_color"] if sel else self.CARD
        fg  = "#ffffff"           if sel else self.FG
        brd = cfg["accent_color"] if sel else self.BORDER

        col = idx % 4
        row = idx // 4
        cell = tk.Frame(parent, bg=self.BG)
        cell.grid(row=row, column=col, padx=(0, 8), pady=(0, 8), sticky="w")

        pill = tk.Label(cell, text=name, bg=bg, fg=fg,
                        font=("Segoe UI", 9), padx=14, pady=6,
                        cursor="hand2",
                        highlightthickness=1, highlightbackground=brd)
        pill.pack()

        def _select(e=None, c=code):
            if self.lang_var.get() == c:
                return
            self.lang_var.set(c)
            self._render_lang_pills()

        pill.bind("<Button-1>", _select)
        if not sel:
            pill.bind("<Enter>", lambda e: pill.configure(bg="#f0f0f5"))
            pill.bind("<Leave>", lambda e: pill.configure(bg=self.CARD))

    # ── Tab: Appearance ───────────────────────────────────────────────────────

    def _build_appearance(self, f):
        self._section(f, "Accent Color")
        tk.Label(f, text="Applied to the recording overlay and UI highlights",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8)).pack(anchor="w", padx=20, pady=(2, 8))
        cf = tk.Frame(f, bg=self.BG); cf.pack(anchor="w", padx=20)
        self._color_swatches_frame = cf
        self._render_color_swatches()

    def _render_color_swatches(self):
        for w in self._color_swatches_frame.winfo_children():
            w.destroy()
        for name, hex_v in PALETTE.items():
            self._color_swatch(self._color_swatches_frame, name, hex_v)

    def _color_swatch(self, parent, name, hex_v):
        sel  = self.color_var.get() == name
        outer = tk.Frame(parent, bg=self.BG); outer.pack(side="left", padx=(0, 10))

        dot = tk.Canvas(outer, width=36, height=36, bg=self.BG, highlightthickness=0,
                        cursor="hand2")
        dot.pack()
        dot.create_oval(4, 4, 32, 32, fill=hex_v, outline="")
        if sel:
            dot.create_oval(8, 8, 28, 28, fill="#ffffff", outline="")
            dot.create_oval(13, 13, 23, 23, fill=hex_v, outline="")

        tk.Label(outer, text=name, bg=self.BG, fg=self.FG2,
                 font=("Segoe UI", 8)).pack()

        def _pick(e=None, n=name):
            if self.color_var.get() == n:
                return
            self.color_var.set(n)
            self._render_color_swatches()
        dot.bind("<Button-1>", _pick)

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _section(self, parent, text):
        tk.Label(parent, text=text.upper(), bg=self.BG, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=20, pady=(16, 0))
        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", padx=20, pady=(4, 0))

    def _tint(self, base, amt=0.05):
        r = int(base[1:3], 16); g = int(base[3:5], 16); b = int(base[5:7], 16)
        r = max(0, r - int(amt * 255)); g = max(0, g - int(amt * 255))
        b = max(0, b - int(amt * 255))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _dim(self, hex_c, factor):
        r = int(int(hex_c[1:3], 16) * factor)
        g = int(int(hex_c[3:5], 16) * factor)
        b = int(int(hex_c[5:7], 16) * factor)
        return f"#{min(r,255):02x}{min(g,255):02x}{min(b,255):02x}"

    def _color_name(self):
        for n, h in PALETTE.items():
            if h == cfg["accent_color"]: return n
        return "Blue"

    # ── Hotkey capture ────────────────────────────────────────────────────────

    def _fmt_hotkey(self, hk):
        if hk.startswith("mouse:"):
            names = {
                "middle": "Mouse Middle Button",
                "left":   "Mouse Left Button",
                "right":  "Mouse Right Button",
                "x1":     "Mouse Back Button",
                "x2":     "Mouse Forward Button",
            }
            return f"🖱  {names.get(hk.split(':')[1], hk)}"
        caps = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "super": "Super"}
        return " + ".join(caps.get(p, p.upper() if len(p) == 1 else p.capitalize())
                          for p in hk.split("+"))

    def _start_capture(self):
        self._capturing_hotkey = True
        self.hotkey_badge.configure(
            text="⌨  Press any key combo or mouse button…", fg=self.FG2, bg="#fff8e1")
        self.win.focus_set()

    def _hk_keypress(self, event):
        if not self._capturing_hotkey:
            return
        key = event.keysym.lower()
        if key in ("control_l", "control_r", "alt_l", "alt_r", "shift_l", "shift_r",
                   "super_l", "super_r", "meta_l", "meta_r", "caps_lock", "num_lock"):
            return
        if key == "escape":
            self._capturing_hotkey = False
            self.hotkey_badge.configure(
                text=self._fmt_hotkey(self._captured_hotkey), fg=self.FG, bg=self.CARD)
            return
        mods = []
        if keyboard.is_pressed("ctrl"):  mods.append("ctrl")
        if keyboard.is_pressed("alt"):   mods.append("alt")
        if keyboard.is_pressed("shift"): mods.append("shift")
        self._hk_set("+".join(mods + [key]))

    def _hk_mouse(self, event):
        if not self._capturing_hotkey:
            return
        self._hk_set("mouse:middle")

    def _hk_set(self, combo):
        self._captured_hotkey  = combo
        self._capturing_hotkey = False
        self.hotkey_badge.configure(text=self._fmt_hotkey(combo), fg=self.FG, bg=self.CARD)

    def _hotkey_error(self, combo):
        combo = (combo or "").strip().lower()
        if combo in ("enter", "return", "esc", "escape"):
            return "Enter and Esc are reserved while recording."
        if combo in ("mouse:left", "mouse:right"):
            return "Left and right mouse buttons would trigger constantly during normal clicking."
        parts = [p for p in combo.split("+") if p]
        if len(parts) == 1 and len(parts[0]) == 1 and parts[0].isalnum():
            return "Use a modifier such as Ctrl, Alt, or Shift with single-letter hotkeys."
        return ""

    def _float_setting(self, var, label, default, minimum, maximum):
        try:
            value = float(var.get())
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number.")
        if value < minimum or value > maximum:
            raise ValueError(f"{label} must be between {minimum:g} and {maximum:g} seconds.")
        return value

    def _test_google(self):
        key = self.api_key_var.get().strip()
        if not key:
            self.test_result.set("⚠ No key entered")
            self.test_label.configure(fg="#f97316")
            return
        self.test_result.set("Testing…")
        self.test_label.configure(fg=self.FG2)
        self.win.update()

        def _run():
            try:
                silence = b"\x00\x00" * 1600
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1); wf.setsampwidth(2)
                    wf.setframerate(16000); wf.writeframes(silence)
                audio_b64 = base64.b64encode(buf.getvalue()).decode()
                payload = {
                    "config": {"encoding": "LINEAR16", "sampleRateHertz": 16000,
                               "languageCode": "en-US"},
                    "audio":  {"content": audio_b64},
                }
                resp = requests.post(
                    f"https://speech.googleapis.com/v1/speech:recognize?key={key}",
                    json=payload, timeout=10,
                )
                data = resp.json()
                if resp.status_code == 200:
                    msg, col = "✓ Key works!", "#22c55e"
                elif resp.status_code == 403:
                    msg, col = f"✗ 403: {data.get('error',{}).get('message','Forbidden')}", "#ef4444"
                elif resp.status_code == 400:
                    msg, col = f"✗ 400: {data.get('error',{}).get('message','Bad request')}", "#ef4444"
                else:
                    msg, col = f"✗ {resp.status_code}", "#ef4444"
            except Exception as e:
                msg, col = f"✗ {e}", "#ef4444"

            def _apply():
                if self.win and self.win.winfo_exists():
                    self.test_result.set(msg)
                    self.test_label.configure(fg=col)
            self.app.overlay.call_soon(_apply)

        threading.Thread(target=_run, daemon=True).start()

    def _save(self):
        self._capture_prompt()
        hotkey_error = self._hotkey_error(self._captured_hotkey or cfg["hotkey"])
        if hotkey_error:
            from tkinter import messagebox
            messagebox.showwarning("Hotkey not saved", hotkey_error, parent=self.win)
            return

        try:
            silence_trigger = self._float_setting(self.silence_var, "Stop after silence", 0.8, 0.2, 5.0)
            min_speech = self._float_setting(self.min_speech_var, "Minimum speech", 1.5, 0.2, 10.0)
            max_speech = self._float_setting(self.max_speech_var, "Force chunk after", 25.0, 2.0, 120.0)
        except ValueError as e:
            from tkinter import messagebox
            messagebox.showwarning("Recording setting not saved", str(e), parent=self.win)
            return

        privacy_mode = bool(self.privacy_mode_var.get())
        if privacy_mode:
            self.backend_var.set("local")
            self.save_history_var.set(False)
            self.analytics_enabled_var.set(False)

        if not privacy_mode and self.backend_var.get() == "google" and not (self.api_key_var.get() or "").strip():
            from tkinter import messagebox
            if self._active_tab != 0:
                self._switch_tab(0)
            self._prompt_google_api_key()
            messagebox.showwarning(
                "Google API key required",
                "Google Cloud needs your own API key before it can be saved as the backend.",
                parent=self.win,
            )
            return

        old_hk = cfg["hotkey"]
        new_hk = self._captured_hotkey or cfg["hotkey"]
        if old_hk != new_hk and not self.app._setup_hotkey(new_hk, remove_old=old_hk):
            from tkinter import messagebox
            messagebox.showwarning(
                "Hotkey not saved",
                "Transcribe could not register that hotkey. Please choose another one.",
                parent=self.win,
            )
            return

        old_privacy_mode = bool(cfg.get("privacy_mode", False))
        target_label = getattr(self, "translate_target_label_var", None)
        if target_label is not None:
            self.translate_target_var.set(
                getattr(self, "_target_label_to_code", {}).get(
                    target_label.get(),
                    self.translate_target_var.get(),
                )
            )

        cfg["hotkey"]         = new_hk
        cfg["whisper_model"]  = self.model_var.get()
        cfg["language"]       = self.lang_var.get()
        cfg["accent_color"]   = PALETTE.get(self.color_var.get(), PALETTE["Blue"])
        cfg["backend"]        = self.backend_var.get()
        cfg["google_api_key"] = self.api_key_var.get().strip()
        cfg["initial_prompt"] = self._prompt_value
        cfg["output_action"] = actions.normalize_action_mode(self.action_mode_var.get())
        cfg["action_model"] = actions.normalize_action_model(self.action_model_var.get())
        cfg["translate_target"] = actions.normalize_translate_target(self.translate_target_var.get())
        cfg["input_device_index"] = self._selected_device_index()
        cfg["silence_trigger_sec"] = silence_trigger
        cfg["min_speech_sec"] = min_speech
        cfg["max_speech_sec"] = max_speech
        cfg["privacy_mode"] = privacy_mode
        cfg["save_history"] = bool(self.save_history_var.get())
        cfg["analytics_enabled"] = bool(self.analytics_enabled_var.get())
        save_config(cfg)
        if privacy_mode and not old_privacy_mode:
            hist.clear()
        telemetry.track(
            "settings_saved",
            {
                "backend": cfg["backend"],
                "privacy_mode": cfg.get("privacy_mode", False),
                "analytics_enabled": cfg.get("analytics_enabled", False),
                "output_action": cfg.get("output_action", "transcribe_only"),
            },
            cfg,
            APP_VERSION,
        )

        if cfg["backend"] == "local":
            threading.Thread(
                target=lambda: self.app.recorder.load_model(cfg["whisper_model"]),
                daemon=True,
            ).start()
        self.win.destroy()

# ── Onboarding (first-run welcome) ────────────────────────────────────────────

class Onboarding:
    BG     = "#f5f5f7"
    CARD   = "#ffffff"
    BORDER = "#e2e2e7"
    FG     = "#1d1d1f"
    FG2    = "#6e6e73"
    SEL_BG = "#eaf1ff"

    def __init__(self, root, app):
        self.root = root
        self.app  = app
        self.win  = None
        self._image_refs = []

    def show(self):
        win = tk.Toplevel(self.root)
        self.win = win
        win.title("Welcome to Transcribe")
        win.configure(bg=self.BG)
        W, H = 560, 820
        win.geometry(f"{W}x{H}")
        win.resizable(False, True)
        win.minsize(540, 600)
        win.attributes("-topmost", True)
        win.transient(self.root)
        win.grab_set()
        win.update_idletasks()
        x = (win.winfo_screenwidth()  - W) // 2
        y = max(30, (win.winfo_screenheight() - H) // 2)
        win.geometry(f"{W}x{H}+{x}+{y}")
        win.protocol("WM_DELETE_WINDOW", self._finish)

        self._hotkey   = cfg["hotkey"]
        self._capturing = False
        self._lang     = tk.StringVar(value=cfg.get("language", "auto"))
        self._backend  = tk.StringVar(value=cfg.get("backend", "local"))
        self._model    = tk.StringVar(value=cfg.get("whisper_model", "base"))
        self._api_key  = tk.StringVar(value=cfg.get("google_api_key", ""))

        self._build()

        win.bind("<KeyPress>", self._on_key, add="+")
        win.bind("<Button-2>", self._on_mouse, add="+")

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build(self):
        # Reserve the bottom for the action bar FIRST so the scrollable body
        # below doesn't claim all available height (pack order matters here).
        self._action_bar = tk.Frame(self.win, bg=self.BG, pady=14)
        self._action_bar.pack(side="bottom", fill="x")
        self._build_action_bar(self._action_bar)

        # Scrollable container so smaller screens still see the Get Started button
        outer = tk.Frame(self.win, bg=self.BG)
        outer.pack(side="top", fill="both", expand=True)

        canvas = tk.Canvas(outer, bg=self.BG, highlightthickness=0)
        sb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        body = tk.Frame(canvas, bg=self.BG)
        fw = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: (
            canvas.configure(scrollregion=canvas.bbox("all")),
            canvas.itemconfig(fw, width=canvas.winfo_width())
        ))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(fw, width=e.width))
        self.win.bind("<MouseWheel>",
                      lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        # ── Hero ──────────────────────────────────────────────────────────
        hero = tk.Frame(body, bg=self.BG)
        hero.pack(fill="x", pady=(24, 4))

        logo = tk.Canvas(hero, width=72, height=72, bg=self.BG, highlightthickness=0)
        logo.pack()
        c = cfg["accent_color"]
        logo.create_oval(4, 4, 68, 68, fill=c, outline="")
        logo.create_rectangle(28, 16, 44, 42, fill="white", outline="")
        logo.create_oval(22, 36, 50, 54, fill="white", outline="")
        logo.create_rectangle(34, 52, 38, 60, fill="white", outline="")
        logo.create_rectangle(26, 60, 46, 64, fill="white", outline="")

        tk.Label(body, text="Welcome to Transcribe", bg=self.BG, fg=self.FG,
                 font=("Segoe UI Semibold", 20)).pack(pady=(6, 0))
        tk.Label(body, text="Speech to text, anywhere on your PC",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 11)).pack(pady=(2, 14))

        # ── 3-step guide ──────────────────────────────────────────────────
        guide = tk.Frame(body, bg=self.CARD,
                         highlightthickness=1, highlightbackground=self.BORDER)
        guide.pack(fill="x", padx=32, pady=(0, 14))
        for i, (icon, title, desc) in enumerate([
            ("⌨", "Press your hotkey",  "Anywhere — in any app, browser, or editor"),
            ("🎙", "Speak naturally",    "A small overlay shows you're recording"),
            ("📋", "Press Enter",        "Your words paste right where the cursor is"),
        ]):
            row = tk.Frame(guide, bg=self.CARD); row.pack(fill="x", padx=16,
                                                          pady=(12 if i == 0 else 6,
                                                                12 if i == 2 else 0))
            tk.Label(row, text=icon, bg=self.CARD, fg=cfg["accent_color"],
                     font=("Segoe UI", 18), width=2).pack(side="left", padx=(0, 10))
            txt = tk.Frame(row, bg=self.CARD); txt.pack(side="left", fill="x", expand=True)
            tk.Label(txt, text=title, bg=self.CARD, fg=self.FG,
                     font=("Segoe UI Semibold", 11)).pack(anchor="w")
            tk.Label(txt, text=desc, bg=self.CARD, fg=self.FG2,
                     font=("Segoe UI", 9)).pack(anchor="w")

        self._body = body

        # Hotkey
        self._section("YOUR HOTKEY")
        hf = tk.Frame(body, bg=self.CARD,
                      highlightthickness=1, highlightbackground=self.BORDER)
        hf.pack(fill="x", padx=32, pady=(4, 12))
        self.hotkey_btn = tk.Label(hf, text=self._fmt_hotkey(self._hotkey),
                                   bg=self.CARD, fg=self.FG,
                                   font=("Segoe UI Semibold", 11),
                                   padx=14, pady=10, anchor="w", cursor="hand2")
        self.hotkey_btn.pack(fill="x")
        self.hotkey_btn.bind("<Button-1>", lambda e: self._start_capture())
        self.hotkey_btn.bind("<Enter>", lambda e: self.hotkey_btn.configure(bg="#f0f0f5"))
        self.hotkey_btn.bind("<Leave>", lambda e: self.hotkey_btn.configure(
            bg="#fff8e1" if self._capturing else self.CARD))

        # Language
        self._section("LANGUAGE")
        lf = tk.Frame(body, bg=self.BG); lf.pack(fill="x", padx=32, pady=(4, 12))
        self._lang_pills_frame = lf
        self._render_lang_pills()

        # Backend
        self._section("BACKEND")
        bf = tk.Frame(body, bg=self.BG); bf.pack(fill="x", padx=32, pady=(4, 12))
        self._backend_cards_frame = bf
        self._render_backend_cards()

        self._google_key_section = tk.Frame(body, bg=self.BG)
        tk.Label(self._google_key_section, text="GOOGLE API KEY", bg=self.BG, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=0, pady=(2, 0))
        key_box = tk.Frame(self._google_key_section, bg=self.CARD,
                           highlightthickness=1, highlightbackground=self.BORDER)
        key_box.pack(fill="x", pady=(4, 4))
        key_entry = tk.Entry(key_box, bg=self.CARD, fg=self.FG,
                             insertbackground=self.FG, show="*",
                             font=("Segoe UI", 10), relief="flat", bd=10)
        self._google_key_entry = key_entry
        key_entry.pack(fill="x")
        attach_placeholder_entry(
            key_entry,
            self._api_key,
            "Paste your Google Cloud API key here",
            self.FG,
            "#a0a0aa",
            secret_char="*",
        )
        tk.Label(self._google_key_section,
                 text="Bring your own Google Cloud API key. Transcribe does not include one.",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8),
                 wraplength=480, justify="left").pack(anchor="w", pady=(0, 10))
        key_actions = tk.Frame(self._google_key_section, bg=self.BG)
        key_actions.pack(anchor="w", pady=(0, 10))
        get_key = tk.Label(key_actions, text="Get API Key",
                           bg="#efefef", fg=self.FG, font=("Segoe UI", 9),
                           padx=12, pady=5, cursor="hand2")
        get_key.pack(side="left")
        get_key.bind("<Button-1>", lambda e: webbrowser.open(GOOGLE_API_KEY_URL))
        get_key.bind("<Enter>", lambda e: get_key.configure(bg="#e0e0e8"))
        get_key.bind("<Leave>", lambda e: get_key.configure(bg="#efefef"))
        self._sync_google_key_area()

        # Model
        self._model_section_label = self._section("STARTING MODEL")
        self._model_hint = tk.Label(body,
                                    text="Base is recommended. Larger models give better Armenian accuracy "
                                         "but use more RAM and disk. You can change this anytime in Settings.",
                                    bg=self.BG, fg=self.FG2, font=("Segoe UI", 8),
                                    wraplength=480, justify="left")
        self._model_hint.pack(anchor="w", padx=32, pady=(2, 4))
        mf = tk.Frame(body, bg=self.BG); mf.pack(fill="x", padx=32, pady=(0, 14))
        self._model_cards_frame = mf
        self._render_model_cards()

        self._google_model_note = tk.Frame(body, bg=self.CARD,
                                           highlightthickness=1, highlightbackground=self.BORDER)
        tk.Label(self._google_model_note,
                 text="Google Cloud Speech-to-Text",
                 bg=self.CARD, fg=self.FG, font=("Segoe UI Semibold", 10),
                 anchor="w").pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(self._google_model_note,
                 text="Uses Google's Speech-to-Text v1 API with Google auto-selecting the recognition model. "
                      "Audio is sent online and billed to your Google Cloud project.",
                 bg=self.CARD, fg=self.FG2, font=("Segoe UI", 8),
                 anchor="w", justify="left", wraplength=440).pack(fill="x", padx=14, pady=(0, 12))
        self._sync_model_area()

        # Tray-location reminder
        tip = tk.Frame(body, bg="#eef6ff",
                       highlightthickness=1, highlightbackground="#bfdbfe")
        tip.pack(fill="x", padx=32, pady=(0, 18))
        tip_inner = tk.Frame(tip, bg="#eef6ff"); tip_inner.pack(fill="x", padx=12, pady=10)
        tk.Label(tip_inner, text="ℹ", bg="#eef6ff", fg="#1e40af",
                 font=("Segoe UI Semibold", 12)).pack(side="left", padx=(0, 8))
        tk.Label(tip_inner,
                 text="Transcribe lives in your system tray (next to the clock). "
                      "Right-click the microphone icon there for Settings, History, or Quit.",
                 bg="#eef6ff", fg="#1e3a8a", font=("Segoe UI", 9),
                 wraplength=440, justify="left").pack(side="left", anchor="w")

    def _build_action_bar(self, parent):
        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", pady=(0, 10))
        wrap = tk.Frame(parent, bg=self.BG); wrap.pack(fill="x", padx=32)
        btn = tk.Label(wrap, text="Get Started",
                       bg=cfg["accent_color"], fg="#ffffff",
                       font=("Segoe UI Semibold", 12),
                       padx=32, pady=11, cursor="hand2")
        btn.pack(fill="x")
        btn.bind("<Button-1>", lambda e: self._finish())
        btn.bind("<Enter>", lambda e: btn.configure(bg=self._dim(cfg["accent_color"], 0.85)))
        btn.bind("<Leave>", lambda e: btn.configure(bg=cfg["accent_color"]))

    def _section(self, text):
        label = tk.Label(self._body, text=text, bg=self.BG, fg=self.FG2,
                         font=("Segoe UI", 8))
        label.pack(anchor="w", padx=32, pady=(2, 0))
        return label

    def _render_lang_pills(self):
        for w in self._lang_pills_frame.winfo_children():
            w.destroy()
        common = [("auto", "Auto-detect"), ("hy", "Armenian"),
                  ("en", "English"),       ("ru", "Russian"),
                  ("multi", "Multilingual")]
        for i, (code, name) in enumerate(common):
            self._pill(self._lang_pills_frame, code, name, i)

    def _render_backend_cards(self):
        for w in self._backend_cards_frame.winfo_children():
            w.destroy()
        self._image_refs = []
        self._backend_card(self._backend_cards_frame, "local",  "Local (Offline)",
                           "Private, free, no internet needed", icon="PC")
        self._backend_card(self._backend_cards_frame, "google", "Google Cloud",
                           "Online, bring your own API key", logo_name="google_logo.png")

    def _render_model_cards(self):
        for w in self._model_cards_frame.winfo_children():
            w.destroy()
        # Curated short list — tiny / base / small. Anything bigger is a separate
        # download that's better surfaced in Settings, not blocking first-run.
        for code, label, desc, ram_ok in [
            ("tiny",  "Tiny",                "Fastest · 75 MB · lower accuracy",        model_ok("tiny")),
            ("base",  "Base  · recommended", "Balanced · 140 MB · solid for everyday",  model_ok("base")),
            ("small", "Small",               "Great quality · 460 MB · slower",         model_ok("small")),
        ]:
            self._model_card(self._model_cards_frame, code, label, desc, ram_ok)

    def _pill(self, parent, code, name, idx):
        sel = self._lang.get() == code
        bg  = cfg["accent_color"] if sel else self.CARD
        fg  = "#ffffff" if sel else self.FG
        brd = cfg["accent_color"] if sel else self.BORDER

        col, row = idx % 5, idx // 5
        cell = tk.Frame(parent, bg=self.BG)
        cell.grid(row=row, column=col, padx=(0, 6), pady=(0, 6), sticky="w")

        pill = tk.Label(cell, text=name, bg=bg, fg=fg,
                        font=("Segoe UI", 9), padx=12, pady=6, cursor="hand2",
                        highlightthickness=1, highlightbackground=brd)
        pill.pack()

        def _select(e=None, c=code):
            if self._lang.get() == c:
                return
            self._lang.set(c)
            self._render_lang_pills()
        pill.bind("<Button-1>", _select)
        if not sel:
            pill.bind("<Enter>", lambda e: pill.configure(bg="#f0f0f5"))
            pill.bind("<Leave>", lambda e: pill.configure(bg=self.CARD))

    def _backend_card(self, parent, val, title, desc, icon=None, logo_name=None):
        sel  = self._backend.get() == val
        bg   = self.SEL_BG if sel else self.CARD
        bord = cfg["accent_color"] if sel else self.BORDER

        card = tk.Frame(parent, bg=bord, padx=1, pady=1, cursor="hand2")
        card.pack(fill="x", pady=(0, 6))
        inner = tk.Frame(card, bg=bg, padx=14, pady=10); inner.pack(fill="x")

        row = tk.Frame(inner, bg=bg); row.pack(fill="x")
        dot = tk.Canvas(row, width=16, height=16, bg=bg, highlightthickness=0)
        dot.pack(side="left", padx=(0, 8))
        if sel:
            dot.create_oval(2, 2, 14, 14, outline=cfg["accent_color"], width=2)
            dot.create_oval(5, 5, 11, 11, fill=cfg["accent_color"], outline="")
        else:
            dot.create_oval(2, 2, 14, 14, outline="#b0b0ba", width=2)
        if logo_name:
            try:
                photo = load_tk_image("assets", logo_name, size=(22, 22))
                self._image_refs.append(photo)
                tk.Label(row, image=photo, bg=bg).pack(side="left", padx=(0, 8))
            except Exception:
                tk.Label(row, text="G", bg=bg, fg="#4285f4",
                         font=("Segoe UI Semibold", 10)).pack(side="left", padx=(0, 8))
        elif icon:
            tk.Label(row, text=icon, bg=bg, fg=self.FG2,
                     font=("Segoe UI Semibold", 9), width=3).pack(side="left", padx=(0, 6))
        tk.Label(row, text=title, bg=bg, fg=self.FG,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        tk.Label(inner, text=desc, bg=bg, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=24)

        def _select(e=None, v=val):
            if self._backend.get() == v:
                return
            self._backend.set(v)
            self._render_backend_cards()
            self._sync_google_key_area()
            self._sync_model_area()
        for w in [card, inner, row] + list(row.winfo_children()) + list(inner.winfo_children()):
            w.bind("<Button-1>", _select)
            try: w.configure(cursor="hand2")
            except: pass

    def _sync_google_key_area(self):
        if not hasattr(self, "_google_key_section"):
            return
        if self._backend.get() == "google":
            self._google_key_section.pack(fill="x", padx=32, pady=(0, 12), after=self._backend_cards_frame)
        else:
            self._google_key_section.pack_forget()

    def _sync_model_area(self):
        if not hasattr(self, "_model_cards_frame"):
            return
        if self._backend.get() == "google":
            self._model_section_label.configure(text="GOOGLE CLOUD")
            self._model_hint.pack_forget()
            self._model_cards_frame.pack_forget()
            self._google_model_note.pack(fill="x", padx=32, pady=(4, 14))
            return

        self._model_section_label.configure(text="STARTING MODEL")
        self._google_model_note.pack_forget()
        self._model_hint.pack(anchor="w", padx=32, pady=(2, 4))
        self._model_cards_frame.pack(fill="x", padx=32, pady=(0, 14))

    def _model_card(self, parent, code, title, desc, enabled):
        sel  = self._model.get() == code
        bg   = self.SEL_BG if sel else (self.CARD if enabled else "#f9f9f9")
        bord = cfg["accent_color"] if sel else (self.BORDER if enabled else "#ebebeb")
        fg   = self.FG if enabled else "#b0b0ba"
        fg2  = self.FG2 if enabled else "#c8c8d0"

        card = tk.Frame(parent, bg=bord, padx=1, pady=1)
        card.pack(fill="x", pady=(0, 6))
        inner = tk.Frame(card, bg=bg, padx=14, pady=9); inner.pack(fill="x")

        row = tk.Frame(inner, bg=bg); row.pack(fill="x")
        dot = tk.Canvas(row, width=16, height=16, bg=bg, highlightthickness=0)
        dot.pack(side="left", padx=(0, 8))
        if sel:
            dot.create_oval(2, 2, 14, 14, outline=cfg["accent_color"], width=2)
            dot.create_oval(5, 5, 11, 11, fill=cfg["accent_color"], outline="")
        else:
            dot.create_oval(2, 2, 14, 14, outline="#b0b0ba", width=2)
        tk.Label(row, text=title, bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        if not enabled:
            tk.Label(row, text="  not enough RAM", bg=bg, fg="#c8c8d0",
                     font=("Segoe UI", 8)).pack(side="left")
        tk.Label(inner, text=desc, bg=bg, fg=fg2,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=24)

        if enabled:
            def _select(e=None, c=code):
                if self._model.get() == c:
                    return
                self._model.set(c)
                self._render_model_cards()
            for w in [card, inner, row] + list(row.winfo_children()) + list(inner.winfo_children()):
                w.bind("<Button-1>", _select)
                try: w.configure(cursor="hand2")
                except: pass

    # ── Hotkey capture ────────────────────────────────────────────────────────

    def _fmt_hotkey(self, hk):
        if hk.startswith("mouse:"):
            names = {"middle":"Mouse Middle", "left":"Mouse Left", "right":"Mouse Right",
                     "x1":"Mouse Back", "x2":"Mouse Forward"}
            return f"🖱  {names.get(hk.split(':')[1], hk)}"
        caps = {"ctrl":"Ctrl","alt":"Alt","shift":"Shift","win":"Win","super":"Super"}
        return " + ".join(caps.get(p, p.upper() if len(p)==1 else p.capitalize())
                          for p in hk.split("+"))

    def _start_capture(self):
        self._capturing = True
        self.hotkey_btn.configure(text="⌨  Press any key combo or mouse button…",
                                  bg="#fff8e1", fg=self.FG2)
        self.win.focus_set()

    def _on_key(self, event):
        if not self._capturing: return
        key = event.keysym.lower()
        if key in ("control_l","control_r","alt_l","alt_r","shift_l","shift_r",
                   "super_l","super_r","meta_l","meta_r","caps_lock","num_lock"):
            return
        if key == "escape":
            self._capturing = False
            self.hotkey_btn.configure(text=self._fmt_hotkey(self._hotkey),
                                      fg=self.FG, bg=self.CARD)
            return
        mods = []
        if keyboard.is_pressed("ctrl"):  mods.append("ctrl")
        if keyboard.is_pressed("alt"):   mods.append("alt")
        if keyboard.is_pressed("shift"): mods.append("shift")
        self._set_hotkey("+".join(mods + [key]))

    def _on_mouse(self, event):
        if self._capturing:
            self._set_hotkey("mouse:middle")

    def _set_hotkey(self, combo):
        self._hotkey = combo
        self._capturing = False
        self.hotkey_btn.configure(text=self._fmt_hotkey(combo), fg=self.FG, bg=self.CARD)

    def _dim(self, hex_c, factor):
        r = int(int(hex_c[1:3],16)*factor); g = int(int(hex_c[3:5],16)*factor)
        b = int(int(hex_c[5:7],16)*factor)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _hotkey_error(self, combo):
        combo = (combo or "").strip().lower()
        if combo in ("enter", "return", "esc", "escape"):
            return "Enter and Esc are reserved while recording."
        if combo in ("mouse:left", "mouse:right"):
            return "Left and right mouse buttons would trigger constantly during normal clicking."
        parts = [p for p in combo.split("+") if p]
        if len(parts) == 1 and len(parts[0]) == 1 and parts[0].isalnum():
            return "Use a modifier such as Ctrl, Alt, or Shift with single-letter hotkeys."
        return ""

    def _finish(self):
        hotkey_error = self._hotkey_error(self._hotkey)
        if hotkey_error:
            from tkinter import messagebox
            messagebox.showwarning("Hotkey not saved", hotkey_error, parent=self.win)
            return

        if self._backend.get() == "google" and not (self._api_key.get() or "").strip():
            from tkinter import messagebox
            self._sync_google_key_area()
            try:
                self._google_key_entry.focus_set()
            except Exception:
                pass
            messagebox.showwarning(
                "Google API key required",
                "Google Cloud needs your own API key before it can be selected.",
                parent=self.win,
            )
            return

        old_hk = cfg["hotkey"]
        old_model = cfg.get("whisper_model", "base")
        if old_hk != self._hotkey and not self.app._setup_hotkey(self._hotkey, remove_old=old_hk):
            from tkinter import messagebox
            messagebox.showwarning(
                "Hotkey not saved",
                "Transcribe could not register that hotkey. Please choose another one.",
                parent=self.win,
            )
            return

        cfg["hotkey"]          = self._hotkey
        cfg["language"]        = self._lang.get()
        cfg["backend"]         = self._backend.get()
        cfg["google_api_key"]  = self._api_key.get().strip()
        cfg["whisper_model"]   = self._model.get()
        cfg["onboarding_done"] = True
        save_config(cfg)
        if old_model != cfg["whisper_model"] and cfg["backend"] == "local":
            threading.Thread(
                target=lambda: self.app.recorder.load_model(cfg["whisper_model"]),
                daemon=True).start()
        try:
            self.win.grab_release()
            self.win.destroy()
        except Exception:
            pass
        # Notify the user the app is now in the system tray (Windows balloon).
        try:
            self.app.show_tray_hint(
                "Transcribe is running",
                f"Press {self.app._fmt_hotkey(cfg['hotkey'])} anywhere to dictate. "
                "Right-click the tray microphone icon for Settings.")
        except Exception:
            pass

# ── Main App ──────────────────────────────────────────────────────────────────

class App:
    def __init__(self, overlay: Overlay):
        self.overlay  = overlay
        self.recorder = AudioRecorder()
        self.recorder.on_levels        = self._on_levels
        self.recorder.on_lang_detected = self._on_lang
        self.recorder.on_partial       = self._on_partial
        self.kbd            = Controller()
        self.is_rec         = False
        self._mouse_listener = None
        self.settings = Settings(overlay.root, self)
        self.history  = HistoryWindow(overlay.root)
        self._tray_icon = None     # set by run_tray() once pystray.Icon is created

        self._setup_hotkey(cfg["hotkey"])
        keyboard.add_hotkey("enter", self._on_enter)
        keyboard.add_hotkey("esc",   self._on_escape)
        telemetry.track(
            "app_started",
            {
                "privacy_mode": cfg.get("privacy_mode", False),
                "backend": cfg.get("backend", "local"),
                "output_action": cfg.get("output_action", "transcribe_only"),
            },
            cfg,
            APP_VERSION,
        )

    def show_tray_hint(self, title, message):
        """Send a Windows tray balloon. Used post-onboarding so users know
        where the app actually lives."""
        icon = self._tray_icon
        if not icon:
            return
        try:
            icon.notify(message, title)
        except Exception:
            pass

    def _fmt_hotkey(self, hk):
        if hk.startswith("mouse:"):
            names = {"middle": "Mouse Middle", "left": "Mouse Left",
                     "right": "Mouse Right", "x1": "Mouse Back", "x2": "Mouse Forward"}
            return names.get(hk.split(":")[1], hk)
        caps = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift",
                "win": "Win", "super": "Super"}
        return " + ".join(caps.get(p, p.upper() if len(p) == 1 else p.capitalize())
                          for p in hk.split("+"))

    def _setup_hotkey(self, hotkey, remove_old=None):
        old_listener = self._mouse_listener
        try:
            if hotkey.startswith("mouse:"):
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
                        self._on_hotkey()
                new_listener = pynput_mouse.Listener(on_click=_on_click)
                new_listener.daemon = True
                new_listener.start()
                self._mouse_listener = new_listener
            else:
                keyboard.add_hotkey(hotkey, self._on_hotkey)
        except Exception as e:
            logger.warning("Could not register hotkey %s: %s", hotkey, e)
            return False

        if remove_old and remove_old != hotkey:
            if remove_old.startswith("mouse:"):
                if old_listener:
                    try: old_listener.stop()
                    except Exception: pass
                    if self._mouse_listener is old_listener:
                        self._mouse_listener = None
            else:
                try: keyboard.remove_hotkey(remove_old)
                except Exception: pass
        elif old_listener and hotkey.startswith("mouse:") and old_listener is not self._mouse_listener:
            try: old_listener.stop()
            except Exception: pass

        return True

    def _on_levels(self, levels):
        self.overlay.call_soon(self.overlay.update_levels, levels)

    def _on_lang(self, code, name):
        self.overlay.call_soon(self.overlay.set_lang, name)

    def _on_partial(self, text):
        self.overlay.call_soon(self.overlay.set_partial, text)

    def _on_hotkey(self):
        if not self.is_rec:
            self.overlay.call_soon(self._start)
        else:
            threading.Thread(target=self._stop, daemon=True).start()

    def _on_enter(self):
        if self.is_rec:
            threading.Thread(target=self._stop, daemon=True).start()

    def _on_escape(self):
        if self.is_rec:
            threading.Thread(target=self._cancel, daemon=True).start()

    def _start(self):
        if self.is_rec:
            return
        try:
            self.is_rec = True
            self.overlay._partial = ""
            self.overlay.show(RECORDING)
            self.recorder.start_recording()
            if cfg["backend"] == "local":
                threading.Thread(
                    target=self.recorder.load_model,
                    daemon=True,
                ).start()
        except Exception as e:
            self.is_rec = False
            self.overlay.show_error(f"Microphone error: {e}")

    def _cancel(self):
        self.recorder.stop_recording()
        self.is_rec = False
        self.overlay.call_soon(self.overlay.hide)

    def _stop(self):
        self.recorder.stop_recording()
        self.overlay.call_soon(self.overlay.set_state, TRANSCRIBING)
        text, lang = self.recorder.transcribe()
        self.is_rec = False

        if not text:
            if lang and lang.startswith("!"):
                err_msg = lang[1:]
                if ":" in err_msg:
                    err_msg = err_msg.split(":", 1)[1]
                self.overlay.call_soon(self.overlay.show_error, err_msg)
            else:
                self.overlay.call_soon(self.overlay.hide)
            return

        action_mode = actions.normalize_action_mode(cfg.get("output_action"))
        output_text = text
        if action_mode != actions.ACTION_TRANSCRIBE_ONLY:
            try:
                output_text = actions.process(
                    text,
                    action_mode,
                    source_lang=lang,
                    target_lang=cfg.get("translate_target", "en"),
                    model=cfg.get("action_model", "built_in"),
                )
                telemetry.track(
                    "action_completed",
                    {
                        "action": action_mode,
                        "model": cfg.get("action_model", "built_in"),
                        "language": lang,
                        "output_length_bucket": _bucket_count(len(output_text)),
                    },
                    cfg,
                    APP_VERSION,
                )
            except actions.ActionError as e:
                telemetry.track(
                    "action_failed",
                    {"action": action_mode, "reason": str(e)[:60]},
                    cfg,
                    APP_VERSION,
                )
                self.overlay.call_soon(self.overlay.show_error, str(e))
                return

        if cfg.get("save_history", True) and not cfg.get("privacy_mode", False):
            hist.save_entry(output_text, lang, cfg["backend"])

        telemetry.track(
            "transcription_completed",
            {
                "backend": cfg.get("backend", "local"),
                "language": lang,
                "action": action_mode,
                "input_length_bucket": _bucket_count(len(text)),
                "output_length_bucket": _bucket_count(len(output_text)),
            },
            cfg,
            APP_VERSION,
        )

        time.sleep(0.35)
        pyperclip.copy(output_text)

        pasted = False
        try:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            self.kbd.press(mod); self.kbd.press("v")
            self.kbd.release("v"); self.kbd.release(mod)
            pasted = True
        except Exception as e:
            print(f"[Paste] Error: {e}")

        self.overlay.call_soon(self.overlay.show_done, pasted)

    def open_settings(self):
        self.overlay.call_soon(self.settings.open)

    def open_history(self):
        self.overlay.call_soon(self.history.open)

    def shutdown(self):
        if self._mouse_listener:
            try: self._mouse_listener.stop()
            except Exception: pass
        self.recorder.shutdown()

# ── Update checker ────────────────────────────────────────────────────────────

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

def _bucket_count(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "0"
    for limit in (25, 100, 250, 500, 1000, 2500):
        if value <= limit:
            return f"<= {limit}"
    return "> 2500"

def remember_pending_update(tag, body):
    cfg["pending_update_version"] = tag or ""
    cfg["pending_update_body"] = body or ""
    cfg["previous_version"] = APP_VERSION
    save_config(cfg)

def clear_pending_update():
    cfg["pending_update_version"] = ""
    cfg["pending_update_body"] = ""
    cfg["previous_version"] = ""
    save_config(cfg)

def consume_pending_update_notice():
    pending = (cfg.get("pending_update_version") or "").strip()
    if not pending:
        return None
    notice = {
        "tag": pending,
        "body": cfg.get("pending_update_body", ""),
        "previous_version": cfg.get("previous_version", ""),
        "success": _parse_version(APP_VERSION) >= _parse_version(pending),
    }
    clear_pending_update()
    return notice

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
        "body": data.get("body", "") or "",
        "notes_url": data.get("html_url", RELEASES_URL),
        "source": "api",
    }

def _enrich_update_notes(info):
    if not info or info.get("body"):
        return info
    try:
        resp = requests.get(RELEASES_API,
                            headers={"Accept": "application/vnd.github+json"},
                            timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("tag_name") == info.get("tag"):
                info["body"] = data.get("body", "") or info.get("body", "")
                info["notes_url"] = data.get("html_url", info.get("notes_url", RELEASES_URL))
    except Exception as e:
        logger.info("Could not load release notes for update: %s", e)
    return info

def fetch_update_info(ignore_dismissed=False):
    try:
        manifest_resp = requests.get(RELEASES_MANIFEST_URL, timeout=5)
        if manifest_resp.status_code == 200:
            info = _update_info_from_manifest(manifest_resp.json(), ignore_dismissed=ignore_dismissed)
            if info:
                return _enrich_update_notes(info)
    except Exception as e:
        logger.info("Release manifest unavailable, falling back to API: %s", e)

    resp = requests.get(RELEASES_API,
                        headers={"Accept": "application/vnd.github+json"},
                        timeout=8)
    if resp.status_code != 200:
        raise RuntimeError(f"GitHub returned {resp.status_code}")
    return _update_info_from_release(resp.json(), ignore_dismissed=ignore_dismissed)

def check_for_update(on_update_found, on_no_update=None, on_error=None, ignore_dismissed=False):
    """Background check. Calls on_update_found(info) only when a newer version exists."""
    def _run():
        try:
            info = fetch_update_info(ignore_dismissed=ignore_dismissed)
            if info:
                on_update_found(info)
            elif on_no_update:
                on_no_update()
        except Exception as e:
            logger.warning("Update check failed: %s", e)
            if on_error:
                on_error(e)
    threading.Thread(target=_run, daemon=True).start()

def show_changelog_window(root, tag, body, primary_label=None, primary_action=None):
    """Lightweight window listing what's new in a release. Body is GitHub markdown
    (we render as plain text — links visible as URLs)."""
    root.deiconify()
    win = tk.Toplevel(root)
    win.title(f"What's new in {tag}")
    win.configure(bg="#f5f5f7")
    W, H = 520, 460
    sw = win.winfo_screenwidth(); sh = win.winfo_screenheight()
    win.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")
    win.attributes("-topmost", True)
    win.transient(root)

    tk.Label(win, text=f"What's new in {tag}", bg="#f5f5f7", fg="#1d1d1f",
             font=("Segoe UI Semibold", 16)).pack(anchor="w", padx=20, pady=(18, 2))
    tk.Label(win, text=f"You're running v{APP_VERSION}",
             bg="#f5f5f7", fg="#6e6e73", font=("Segoe UI", 9)
             ).pack(anchor="w", padx=20, pady=(0, 10))

    body_frame = tk.Frame(win, bg="#ffffff",
                          highlightthickness=1, highlightbackground="#e2e2e7")
    body_frame.pack(fill="both", expand=True, padx=20, pady=(0, 12))
    txt = tk.Text(body_frame, bg="#ffffff", fg="#1d1d1f", relief="flat",
                  font=("Segoe UI", 10), padx=12, pady=10, wrap="word")
    txt.pack(fill="both", expand=True)
    sb = tk.Scrollbar(body_frame, orient="vertical", command=txt.yview)
    sb.pack(side="right", fill="y")
    txt.configure(yscrollcommand=sb.set)
    txt.insert("1.0", (body or "No release notes were included with this release.").strip())
    txt.configure(state="disabled")

    btns = tk.Frame(win, bg="#f5f5f7"); btns.pack(fill="x", padx=20, pady=(0, 16))
    close = tk.Label(btns, text="Close", bg="#efefef", fg="#1d1d1f",
                     font=("Segoe UI", 10), padx=18, pady=7, cursor="hand2")
    close.pack(side="right")
    close.bind("<Button-1>", lambda e: win.destroy())
    close.bind("<Enter>", lambda e: close.configure(bg="#e0e0e8"))
    close.bind("<Leave>", lambda e: close.configure(bg="#efefef"))

    if primary_label and primary_action:
        def _primary(_event=None):
            win.destroy()
            primary_action()

        primary = tk.Label(btns, text=primary_label, bg=cfg["accent_color"], fg="#ffffff",
                           font=("Segoe UI Semibold", 10), padx=18, pady=7, cursor="hand2")
        primary.pack(side="right", padx=(0, 8))
        primary.bind("<Button-1>", _primary)
        primary.bind("<Enter>", lambda e: primary.configure(bg="#2563eb"))
        primary.bind("<Leave>", lambda e: primary.configure(bg=cfg["accent_color"]))

def show_simple_window(root, title, message, primary_label=None, primary_action=None):
    root.deiconify()
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg="#f5f5f7")
    W, H = 420, 220
    sw = win.winfo_screenwidth(); sh = win.winfo_screenheight()
    win.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")
    win.attributes("-topmost", True)
    win.transient(root)
    win.resizable(False, False)

    tk.Label(win, text=title, bg="#f5f5f7", fg="#1d1d1f",
             font=("Segoe UI Semibold", 15)).pack(anchor="w", padx=22, pady=(20, 6))
    tk.Label(win, text=message, bg="#f5f5f7", fg="#6e6e73",
             font=("Segoe UI", 10), wraplength=370, justify="left"
             ).pack(anchor="w", padx=22, pady=(0, 18))

    btns = tk.Frame(win, bg="#f5f5f7"); btns.pack(side="bottom", fill="x", padx=22, pady=(0, 18))

    close = tk.Label(btns, text="Close", bg="#efefef", fg="#1d1d1f",
                     font=("Segoe UI", 10), padx=18, pady=7, cursor="hand2")
    close.pack(side="right")
    close.bind("<Button-1>", lambda e: win.destroy())
    close.bind("<Enter>", lambda e: close.configure(bg="#e0e0e8"))
    close.bind("<Leave>", lambda e: close.configure(bg="#efefef"))

    if primary_label and primary_action:
        def _primary(_event=None):
            win.destroy()
            primary_action()

        primary = tk.Label(btns, text=primary_label, bg=cfg["accent_color"], fg="#ffffff",
                           font=("Segoe UI Semibold", 10), padx=18, pady=7, cursor="hand2")
        primary.pack(side="right", padx=(0, 8))
        primary.bind("<Button-1>", _primary)
        primary.bind("<Enter>", lambda e: primary.configure(bg="#2563eb"))
        primary.bind("<Leave>", lambda e: primary.configure(bg=cfg["accent_color"]))

def show_update_progress_window(root, tag):
    root.deiconify()
    win = tk.Toplevel(root)
    win.title(f"Updating to {tag}")
    win.configure(bg="#f5f5f7")
    W, H = 440, 210
    sw = win.winfo_screenwidth(); sh = win.winfo_screenheight()
    win.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")
    win.attributes("-topmost", True)
    win.transient(root)
    win.resizable(False, False)

    tk.Label(win, text=f"Updating to {tag}", bg="#f5f5f7", fg="#1d1d1f",
             font=("Segoe UI Semibold", 15)).pack(anchor="w", padx=22, pady=(20, 6))
    message_var = tk.StringVar(value="Preparing download...")
    percent_var = tk.StringVar(value="0%")
    tk.Label(win, textvariable=message_var, bg="#f5f5f7", fg="#6e6e73",
             font=("Segoe UI", 10), wraplength=390, justify="left"
             ).pack(anchor="w", padx=22, pady=(0, 14))

    bar = ttk.Progressbar(win, orient="horizontal", mode="determinate", maximum=100)
    bar.pack(fill="x", padx=22, pady=(0, 8))
    tk.Label(win, textvariable=percent_var, bg="#f5f5f7", fg="#6e6e73",
             font=("Segoe UI", 9)).pack(anchor="e", padx=22)

    return {"win": win, "bar": bar, "message": message_var, "percent": percent_var}

def update_progress_window(ui, percent=None, message=None):
    if not ui:
        return
    win = ui.get("win")
    if not win or not win.winfo_exists():
        return
    if message is not None:
        ui["message"].set(message)
    if percent is not None:
        percent = max(0, min(100, int(percent)))
        ui["bar"]["value"] = percent
        ui["percent"].set(f"{percent}%")

def show_post_update_window(root, notice):
    if notice.get("success"):
        previous = notice.get("previous_version")
        body = notice.get("body") or "No release notes were included with this release."
        if previous:
            body = f"Updated from v{previous} to v{APP_VERSION}.\n\n{body}"
        else:
            body = f"Updated to v{APP_VERSION}.\n\n{body}"
        show_changelog_window(
            root,
            f"v{APP_VERSION}",
            body,
            primary_label="Open Releases",
            primary_action=lambda: webbrowser.open(RELEASES_URL),
        )
        return

    show_simple_window(
        root,
        "Update did not complete",
        f"Transcribe is still on v{APP_VERSION}. The installer did not finish, so the previous version stayed in place.",
        primary_label="Open Releases",
        primary_action=lambda: webbrowser.open(RELEASES_URL),
    )

def show_about_window(root):
    root.deiconify()
    win = tk.Toplevel(root)
    win.title("About Transcribe")
    win.configure(bg="#f5f5f7")
    W, H = 520, 440
    sw = win.winfo_screenwidth(); sh = win.winfo_screenheight()
    win.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//2}")
    win.attributes("-topmost", True)
    win.transient(root)
    win.resizable(False, False)

    tk.Label(win, text="About Transcribe", bg="#f5f5f7", fg="#1d1d1f",
             font=("Segoe UI Semibold", 17)).pack(anchor="w", padx=24, pady=(22, 2))
    tk.Label(win, text=f"Version {APP_VERSION}",
             bg="#f5f5f7", fg="#6e6e73", font=("Segoe UI", 9)
             ).pack(anchor="w", padx=24, pady=(0, 14))

    box = tk.Frame(win, bg="#ffffff", highlightthickness=1, highlightbackground="#e2e2e7")
    box.pack(fill="both", expand=True, padx=24, pady=(0, 16))

    sections = [
        (
            "Open source, privacy first",
            "Transcribe is an open-source speech-to-text project built to keep your workflow private. "
            "The aim is to run models locally whenever possible, let people choose the best model for "
            "their computer, and make dictation work anywhere on the desktop."
        ),
        (
            "Local models, practical control",
            "You can use offline Whisper models, download or remove local models, and choose cloud "
            "speech only when you explicitly bring your own Google Cloud API key."
        ),
        (
            "Aibuben.xyz",
            "Created by Aram Adamyan, Founder of Aibuben.xyz. Aibuben is an AI community for learning, "
            "building, and sharing practical AI tools and ideas."
        ),
        (
            "Contribute",
            "Contributions, bug reports, feature ideas, and code improvements are welcome on GitHub."
        ),
    ]
    for i, (title, text) in enumerate(sections):
        tk.Label(box, text=title, bg="#ffffff", fg="#1d1d1f",
                 font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=16, pady=(14 if i == 0 else 10, 2))
        tk.Label(box, text=text, bg="#ffffff", fg="#6e6e73",
                 font=("Segoe UI", 9), wraplength=450, justify="left"
                 ).pack(anchor="w", padx=16)

    btns = tk.Frame(win, bg="#f5f5f7")
    btns.pack(fill="x", padx=24, pady=(0, 20))

    def _button(label, bg, fg, action, side="right", padx=(8, 0)):
        btn = tk.Label(btns, text=label, bg=bg, fg=fg,
                       font=("Segoe UI Semibold" if bg == cfg["accent_color"] else "Segoe UI", 10),
                       padx=16, pady=7, cursor="hand2")
        btn.pack(side=side, padx=padx)
        btn.bind("<Button-1>", lambda e: action())
        hover = "#2563eb" if bg == cfg["accent_color"] else "#e0e0e8"
        btn.bind("<Enter>", lambda e: btn.configure(bg=hover))
        btn.bind("<Leave>", lambda e: btn.configure(bg=bg))
        return btn

    _button("Close", "#efefef", "#1d1d1f", win.destroy)
    _button("Open Aibuben.xyz", "#efefef", "#1d1d1f", lambda: webbrowser.open(AIBUBEN_URL))
    _button("Open GitHub", cfg["accent_color"], "#ffffff", lambda: webbrowser.open(PROJECT_GITHUB_URL))

def download_and_install_update(installer_url, checksum_url=None, expected_sha256=None,
                                on_progress=None, on_phase=None, before_launch=None,
                                on_done=None, on_error=None):
    """Download installer to temp and launch it with silent flags. App exits when launched."""
    def _run():
        try:
            import tempfile, os, subprocess
            if not _trusted_update_url(installer_url):
                raise ValueError("Untrusted update URL")
            tmp = os.path.join(tempfile.gettempdir(), "TranscribeApp-Setup.exe")
            digest = hashlib.sha256()
            if on_phase:
                on_phase("Downloading installer")
            with requests.get(installer_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0))
                got = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            f.write(chunk)
                            digest.update(chunk)
                            got += len(chunk)
                            if on_progress:
                                on_progress(got, total)
            expected = (expected_sha256 or "").strip().lower()
            if checksum_url:
                if not _trusted_update_url(checksum_url):
                    raise ValueError("Untrusted checksum URL")
                if on_phase:
                    on_phase("Verifying checksum")
                check_resp = requests.get(checksum_url, timeout=15)
                check_resp.raise_for_status()
                expected = expected or _sha256_from_checksum_text(check_resp.text)
                if not expected:
                    raise ValueError("Release checksum was unreadable")
            if expected:
                if on_phase:
                    on_phase("Verifying installer")
                actual = digest.hexdigest()
                if actual != expected:
                    raise ValueError("Downloaded installer checksum did not match the release checksum")
            # Launch installer; /SILENT shows progress bar, /CLOSEAPPLICATIONS handles us,
            # /RESTARTAPPLICATIONS re-launches us after install.
            if before_launch:
                before_launch()
            if on_phase:
                on_phase("Starting installer")
            subprocess.Popen([tmp, "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
                             creationflags=0x00000010 if sys.platform == "win32" else 0)
            if on_done:
                on_done()
        except Exception as e:
            if on_error:
                on_error(e)
    threading.Thread(target=_run, daemon=True).start()

# ── Tray ──────────────────────────────────────────────────────────────────────

def make_icon(color="#3b82f6"):
    img = Image.new("RGBA", (64,64), (0,0,0,0))
    d = ImageDraw.Draw(img)
    r,g,b = int(color[1:3],16), int(color[3:5],16), int(color[5:7],16)
    d.ellipse([2,2,62,62], fill=(r,g,b))
    d.rectangle([24,12,40,40], fill="white")
    d.ellipse([18,32,46,52],   fill="white")
    d.rectangle([30,50,34,60], fill="white")
    d.rectangle([22,58,42,62], fill="white")
    return img

def run_tray(app: App):
    _update_state = {
        "tag": None,
        "url": None,
        "checksum_url": None,
        "checksum": "",
        "body": "",
        "installing": False,
        "checking": False,
        "last_check": 0.0,
        "phase": "",
        "progress_percent": 0,
        "progress_ui": None,
        "last_progress_bucket": -1,
    }

    def on_settings(icon, _): app.open_settings()
    def on_history(icon, _):  app.open_history()
    def on_about(icon, _):
        app.overlay.call_soon(show_about_window, app.overlay.root)
    def on_quit(icon, _):
        icon.stop()
        app.shutdown()
        app.overlay.call_soon(app.overlay.root.quit)

    def on_show_changelog(icon, _):
        if _update_state["tag"]:
            app.overlay.call_soon(
                show_changelog_window,
                app.overlay.root,
                _update_state["tag"],
                _update_state["body"],
            )

    def on_dismiss_update(icon, _):
        tag = _update_state["tag"]
        if not tag:
            return
        cfg["dismissed_update_version"] = tag
        save_config(cfg)
        _update_state["tag"] = None
        _update_state["url"] = None
        _update_state["checksum_url"] = None
        _update_state["checksum"] = ""
        _update_state["body"] = ""
        icon.menu = _build_menu()

    def _set_install_progress(icon, percent=None, message=None, notify=False):
        if percent is not None:
            _update_state["progress_percent"] = max(0, min(100, int(percent)))
        if message:
            _update_state["phase"] = message
        bucket = _update_state["progress_percent"] // 5
        if bucket != _update_state["last_progress_bucket"] or message:
            _update_state["last_progress_bucket"] = bucket
            icon.menu = _build_menu()
        app.overlay.call_soon(
            update_progress_window,
            _update_state.get("progress_ui"),
            _update_state["progress_percent"],
            _update_state.get("phase") or "Preparing update...",
        )
        if notify:
            try:
                icon.notify(_update_state.get("phase") or "Updating Transcribe", "Installing update")
            except Exception:
                pass

    def _begin_install_update(icon):
        if _update_state["installing"]:
            return
        if _update_state["url"] and sys.platform == "win32":
            _update_state["installing"] = True
            _update_state["phase"] = "Preparing update..."
            _update_state["progress_percent"] = 0
            _update_state["last_progress_bucket"] = -1
            icon.menu = _build_menu()
            app.overlay.call_soon(
                lambda: _update_state.update({
                    "progress_ui": show_update_progress_window(app.overlay.root, _update_state["tag"])
                })
            )
            try:
                icon.notify("Downloading update. The app will restart automatically after install.",
                            "Installing update")
            except Exception:
                pass

            def _on_progress(got, total):
                if total:
                    percent = int((got / total) * 100)
                    msg = f"Downloading update... {percent}% ({_format_bytes(got)} / {_format_bytes(total)})"
                else:
                    percent = _update_state["progress_percent"]
                    msg = f"Downloading update... {_format_bytes(got)}"
                notify = total and percent in (25, 50, 75, 100)
                _set_install_progress(icon, percent, msg, notify=notify)

            def _on_phase(message):
                percent = 100 if message == "Starting installer" else _update_state["progress_percent"]
                _set_install_progress(icon, percent, message, notify=message in ("Verifying installer", "Starting installer"))

            def _before_launch():
                remember_pending_update(_update_state["tag"], _update_state["body"])

            def _on_done():
                _set_install_progress(icon, 100, "Installer is running. Transcribe will restart.", notify=True)

            def _on_error(e):
                _update_state["installing"] = False
                _update_state["phase"] = ""
                _update_state["progress_percent"] = 0
                clear_pending_update()
                icon.menu = _build_menu()
                try:
                    icon.notify(f"Update failed: {e}", "Update error")
                except Exception:
                    pass
                app.overlay.call_soon(
                    update_progress_window,
                    _update_state.get("progress_ui"),
                    0,
                    f"Update failed: {e}",
                )
                app.overlay.call_soon(
                    show_simple_window,
                    app.overlay.root,
                    "Update failed",
                    f"Transcribe stayed on v{APP_VERSION}. No downloaded update was installed.\n\n{e}",
                    "Open Releases",
                    lambda: webbrowser.open(RELEASES_URL),
                )

            download_and_install_update(_update_state["url"],
                                        checksum_url=_update_state["checksum_url"],
                                        expected_sha256=_update_state["checksum"],
                                        on_progress=_on_progress,
                                        on_phase=_on_phase,
                                        before_launch=_before_launch,
                                        on_done=_on_done, on_error=_on_error)
        else:
            webbrowser.open(RELEASES_URL)

    def on_install_update(icon, _):
        if _update_state["installing"]:
            return
        if _update_state["url"] and sys.platform == "win32":
            app.overlay.call_soon(
                show_changelog_window,
                app.overlay.root,
                _update_state["tag"],
                _update_state["body"],
                "Install Update",
                lambda: _begin_install_update(icon),
            )
        else:
            webbrowser.open(RELEASES_URL)

    def _set_update_info(info):
        _update_state["tag"] = info["tag"]
        _update_state["url"] = info.get("installer_url")
        _update_state["checksum_url"] = info.get("checksum_url")
        _update_state["checksum"] = info.get("checksum", "")
        _update_state["body"] = info.get("body") or f"Release notes: {info.get('notes_url', RELEASES_URL)}"
        _update_state["last_check"] = time.time()
        icon.menu = _build_menu()
        try:
            msg = (f"Transcribe {info['tag']} is available. Right-click the tray icon to install."
                   if info.get("installer_url") and sys.platform == "win32"
                   else f"Transcribe {info['tag']} is available. Open downloads to update.")
            icon.notify(msg, "Update available")
        except Exception:
            pass

    def _check_updates(manual=False):
        if _update_state["checking"]:
            if manual:
                app.overlay.call_soon(
                    show_simple_window,
                    app.overlay.root,
                    "Already checking",
                    "Transcribe is already checking for updates. The result will appear here shortly.",
                )
            return
        _update_state["checking"] = True
        icon.menu = _build_menu()

        def _found(info):
            _update_state["checking"] = False
            _set_update_info(info)
            if manual:
                can_install = bool(info.get("installer_url")) and sys.platform == "win32"
                if can_install:
                    app.overlay.call_soon(
                        show_changelog_window,
                        app.overlay.root,
                        info["tag"],
                        _update_state["body"],
                        "Install Update",
                        lambda: _begin_install_update(icon),
                    )
                else:
                    app.overlay.call_soon(
                        show_simple_window,
                        app.overlay.root,
                        "Update available",
                        f"Transcribe {info['tag']} is available. You are running v{APP_VERSION}.",
                        "Open Releases",
                        lambda: webbrowser.open(RELEASES_URL),
                    )

        def _none():
            _update_state["checking"] = False
            _update_state["last_check"] = time.time()
            icon.menu = _build_menu()
            if manual:
                try: icon.notify("You're already on the latest version.", "No update found")
                except Exception: pass
                app.overlay.call_soon(
                    show_simple_window,
                    app.overlay.root,
                    "No update found",
                    f"You're already on the latest version: v{APP_VERSION}.",
                )

        def _error(e):
            _update_state["checking"] = False
            _update_state["last_check"] = time.time()
            icon.menu = _build_menu()
            if manual:
                try: icon.notify(f"Could not check for updates: {e}", "Update check failed")
                except Exception: pass
                app.overlay.call_soon(
                    show_simple_window,
                    app.overlay.root,
                    "Update check failed",
                    f"Transcribe could not contact the update server: {e}",
                    "Open Releases",
                    lambda: webbrowser.open(RELEASES_URL),
                )

        check_for_update(
            _found,
            on_no_update=_none,
            on_error=_error,
            ignore_dismissed=manual,
        )

    def on_check_updates(icon, _):
        try: icon.notify("Checking GitHub for the latest Transcribe release.", "Checking for updates")
        except Exception: pass
        _check_updates(manual=True)

    def _periodic_update_check():
        while True:
            time.sleep(6 * 60 * 60)
            _check_updates(manual=False)

    def _build_menu():
        items = [
            pystray.MenuItem(f"Transcribe  v{APP_VERSION}", None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        if _update_state["installing"]:
            phase = _update_state.get("phase") or "Installing update"
            percent = _update_state.get("progress_percent", 0)
            label = f"{phase} ({percent}%)" if percent else phase
            items += [
                pystray.MenuItem(label, None, enabled=False),
                pystray.Menu.SEPARATOR,
            ]
        elif _update_state["checking"]:
            items += [
                pystray.MenuItem("Checking for updates…", None, enabled=False),
                pystray.Menu.SEPARATOR,
            ]
        elif _update_state["tag"]:
            label = (f"⬆  Install update: {_update_state['tag']}"
                     if _update_state["url"] and sys.platform == "win32"
                     else f"⬆  Download update: {_update_state['tag']}")
            items += [
                pystray.MenuItem(label, on_install_update),
                pystray.MenuItem(f"What's new in {_update_state['tag']}…",
                                 on_show_changelog),
                pystray.MenuItem("Dismiss this update",       on_dismiss_update),
                pystray.Menu.SEPARATOR,
            ]
        items += [
            pystray.MenuItem("Settings",
                             on_settings, default=True),
            pystray.MenuItem("History",  on_history),
            pystray.MenuItem("About Transcribe", on_about),
            pystray.MenuItem("Check for Updates", on_check_updates),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        ]
        return pystray.Menu(*items)

    icon = pystray.Icon("transcribe", make_icon(cfg["accent_color"]),
                        f"Transcribe  ·  {cfg['hotkey']}",
                        menu=_build_menu())
    app._tray_icon = icon  # let App.show_tray_hint use it

    _check_updates(manual=False)
    threading.Thread(target=_periodic_update_check, daemon=True).start()
    icon.run()

def _apply_taskbar_icon(root):
    try:
        import tempfile, os
        ico_path = os.path.join(tempfile.gettempdir(), "transcribe_app.ico")
        img = make_icon(cfg["accent_color"]).resize((256, 256), Image.LANCZOS)
        img.save(ico_path, format="ICO", sizes=[(256,256),(64,64),(32,32),(16,16)])
        root.iconbitmap(ico_path)
    except Exception:
        pass

def _launch_action_from_args():
    args = {a.lower() for a in sys.argv[1:]}
    if args & {"--background", "--startup", "--tray"}:
        return "background"
    if args & {"--show-settings", "--settings"}:
        return "show_settings"
    # A normal double-click should surface the app instead of silently
    # becoming another invisible tray process.
    return "show_settings"

def main():
    # Single-instance: if another copy is already running, ask it to show
    # Settings (so a second desktop double-click does something visible),
    # then exit immediately.
    launch_action = _launch_action_from_args()
    lock_sock = acquire_single_instance_lock()
    if lock_sock is None:
        if launch_action == "show_settings":
            if signal_running_instance("show_settings"):
                sys.exit(0)

    overlay = Overlay()
    _apply_taskbar_icon(overlay.root)
    app     = App(overlay)
    post_update_notice = consume_pending_update_notice()

    def _on_ipc(action):
        if action == "show_settings":
            app.open_settings()
        elif action == "show_onboarding":
            overlay.call_soon(lambda: Onboarding(overlay.root, app).show())
    if lock_sock is not None:
        start_ipc_server(lock_sock, _on_ipc)

    threading.Thread(target=run_tray, args=(app,), daemon=True).start()
    if not cfg.get("onboarding_done"):
        overlay.root.after(400, lambda: Onboarding(overlay.root, app).show())
    elif launch_action == "show_settings":
        overlay.call_soon(app.settings.open)
    if post_update_notice:
        overlay.root.after(900, lambda n=post_update_notice: show_post_update_window(overlay.root, n))
    overlay.run()

if __name__ == "__main__":
    main()
