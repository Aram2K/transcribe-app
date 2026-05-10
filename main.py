import sys, threading, time, json, math, wave, io, struct
import tkinter as tk
from tkinter import ttk
import psutil, pyaudio, numpy as np, keyboard, pyperclip, pystray, requests, base64, ctypes
from PIL import Image, ImageDraw
from pynput.keyboard import Controller, Key
from pynput import mouse as pynput_mouse
from faster_whisper import WhisperModel
import history as hist

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = "config.json"
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
}

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return {**DEFAULT, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        return DEFAULT.copy()

def save_config(c):
    with open(CONFIG_PATH, "w") as f:
        json.dump(c, f, indent=2)

cfg = load_config()

# ── System Info ───────────────────────────────────────────────────────────────

RAM_GB = psutil.virtual_memory().total / (1024 ** 3)

MODELS = {
    "tiny":           {"min_ram": 2,  "speed": "~0.5s", "quality": "Good",        "size": "75 MB"},
    "base":           {"min_ram": 4,  "speed": "~1s",   "quality": "Better",      "size": "140 MB"},
    "small":          {"min_ram": 6,  "speed": "~3s",   "quality": "Great",       "size": "460 MB"},
    "medium":         {"min_ram": 10, "speed": "~8s",   "quality": "Excellent",   "size": "1.4 GB"},
    "large-v3":       {"min_ram": 16, "speed": "~15s",  "quality": "Best",        "size": "3 GB"},
    "large-v3-turbo": {"min_ram": 8,  "speed": "~5s",   "quality": "Best (fast)", "size": "1.6 GB"},
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

    def load_model(self, name=None):
        name = name or cfg["whisper_model"]
        with self._model_lock:
            if self._model is None or self._model_name != name:
                self._model      = WhisperModel(name, device="cpu", compute_type="int8")
                self._model_name = name

    def start_recording(self):
        self.recording = True
        self.frames    = []
        with self._chunk_lock:
            self._chunk_frames     = []
            self._chunk_results    = {}
            self._chunk_idx        = 0
            self._samples_in_chunk = 0
        self._session_lang  = None   # reset on each new recording
        self._chunk_threads = []     # reset thread list each session
        self.stream = self.audio.open(
            format=pyaudio.paFloat32, channels=1,
            rate=cfg["sample_rate"], input=True,
            frames_per_buffer=cfg["chunk_size"]
        )
        threading.Thread(target=self._record, daemon=True).start()

    def _record(self):
        sr          = cfg["sample_rate"]
        frame_dur   = cfg["chunk_size"] / sr   # seconds per audio frame

        vad_buf      = []    # frames for the current utterance
        speech_sec   = 0.0
        silence_sec  = 0.0
        noise_hist   = []    # sliding window for adaptive noise floor

        while self.recording:
            try:
                data = self.stream.read(cfg["chunk_size"], exception_on_overflow=False)
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
                    (silence_sec >= SILENCE_TRIGGER_SEC and speech_sec >= MIN_SPEECH_SEC)
                    or total_sec >= MAX_SPEECH_SEC
                )

                if should_chunk and speech_sec >= MIN_SPEECH_SEC:
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

            except Exception:
                pass

    def _transcribe_chunk(self, audio, idx):
        try:
            if cfg["backend"] == "google" and cfg["google_api_key"]:
                text, lang = self._run_google(audio)
            else:
                text, lang = self._run_local(audio)

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

        self.root.withdraw()
        self._loop()

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
        if self._hide_at and time.time() >= self._hide_at:
            self._hide_at = None
            self._visible = False

        target      = 0.93 if self._visible else 0.0
        self._alpha += (target - self._alpha) * 0.3
        if self._alpha < 0.02 and not self._visible:
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

    def open(self):
        if self.win and self.win.winfo_exists():
            self.win.lift(); return

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

    def _build(self):
        w = self.win
        entries = hist.load()

        # Header
        hdr = tk.Frame(w, bg=self.BG)
        hdr.pack(fill="x", padx=20, pady=(18, 6))
        tk.Label(hdr, text="History", bg=self.BG, fg=self.FG,
                 font=("Segoe UI Semibold", 16)).pack(side="left")
        tk.Label(hdr, text=f"  {len(entries)} entries",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 9)).pack(side="left", pady=4)

        if entries:
            tk.Button(hdr, text="Clear all", command=self._clear,
                      bg=self.BG, fg="#555555", activebackground=self.BG,
                      font=("Segoe UI", 9), relief="flat", cursor="hand2").pack(side="right")

        tk.Frame(w, bg=self.SEP, height=1).pack(fill="x")

        if not entries:
            tk.Label(w, text="No transcriptions yet.\nPress your hotkey to start.",
                     bg=self.BG, fg=self.FG2,
                     font=("Segoe UI", 11)).pack(expand=True)
            return

        # Scrollable list
        outer = tk.Frame(w, bg=self.BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=self.BG, highlightthickness=0)
        sb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
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
        w.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        for i, entry in enumerate(entries):
            self._entry_card(frame, entry, i)

    def _entry_card(self, parent, entry, idx):
        card = tk.Frame(parent, bg=self.CARD, padx=16, pady=12)
        card.pack(fill="x", padx=12, pady=(8 if idx == 0 else 0, 0))

        # Top row: timestamp + language + backend
        top = tk.Frame(card, bg=self.CARD)
        top.pack(fill="x")
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

        # Text
        tk.Label(card, text=entry["text"], bg=self.CARD, fg=self.FG,
                 font=("Segoe UI", 10), wraplength=450, justify="left",
                 anchor="w").pack(fill="x", pady=(6, 0))

        tk.Frame(parent, bg=self.SEP, height=1).pack(fill="x", padx=12)

    def _clear(self):
        hist.clear()
        self.win.destroy()
        self.win = None

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

    TABS = ["General", "Model", "Language", "Appearance"]

    def __init__(self, root, app):
        self.root         = root
        self.app          = app
        self.win          = None
        self._active_tab  = 0
        self._tab_frames  = []
        self._tab_btns    = []

    def open(self):
        if self.win and self.win.winfo_exists():
            self.win.lift(); return

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
        self._captured_hotkey  = cfg["hotkey"]
        self._capturing_hotkey = False

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
        [self._build_general, self._build_model,
         self._build_language, self._build_appearance][idx](self._scroll_frame)
        self._canvas.yview_moveto(0)

    # ── Tab: General ──────────────────────────────────────────────────────────

    def _build_general(self, f):
        self._section(f, "Backend")
        bf = tk.Frame(f, bg=self.BG); bf.pack(fill="x", padx=20, pady=(4, 0))

        self._backend_cards = {}
        for val, title, desc, icon in [
            ("local",  "Local (Offline)",  "Private · free · no internet needed", "💻"),
            ("google", "Google Cloud",     "Best accuracy for Armenian · 60 min/mo free", "☁"),
        ]:
            self._backend_card(bf, val, title, desc, icon)

        # Google API key (shown conditionally)
        self.google_section = tk.Frame(f, bg=self.BG)
        self._build_google_section(self.google_section)
        self._toggle_google_section()

        self._section(f, "Hotkey")
        tk.Label(f, text="Click the badge then press any key combo or mouse button  ·  Esc cancels",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 8)
                 ).pack(anchor="w", padx=20, pady=(2, 6))

        hf = tk.Frame(f, bg=self.CARD,
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

    def _backend_card(self, parent, val, title, desc, icon):
        sel   = self.backend_var.get() == val
        bg    = self.SEL_BG if sel else self.CARD
        bord  = cfg["accent_color"] if sel else self.BORDER

        card = tk.Frame(parent, bg=bord, padx=1, pady=1, cursor="hand2")
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

        tk.Label(row, text=f"{icon}  {title}", bg=bg, fg=self.FG,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        tk.Label(inner, text=desc, bg=bg, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=24)

        def _select(e=None):
            self.backend_var.set(val)
            self._switch_tab(self._active_tab)
        for w in [card, inner, row] + list(row.winfo_children()) + list(inner.winfo_children()):
            w.bind("<Button-1>", _select)
            if hasattr(w, 'configure'):
                try: w.configure(cursor="hand2")
                except: pass

    def _build_google_section(self, parent):
        self._section(parent, "Google API Key")
        kf = tk.Frame(parent, bg=self.CARD,
                      highlightthickness=1, highlightbackground=self.BORDER)
        kf.pack(fill="x", padx=20, pady=(4, 4))
        entry_row = tk.Frame(kf, bg=self.CARD); entry_row.pack(fill="x")
        tk.Entry(entry_row, textvariable=self.api_key_var,
                 bg=self.CARD, fg=self.FG, insertbackground=self.FG,
                 show="•", font=("Segoe UI", 10), relief="flat",
                 bd=10).pack(fill="x")

        tr = tk.Frame(parent, bg=self.BG); tr.pack(anchor="w", padx=20, pady=(0, 8))
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
        if self.backend_var.get() == "google":
            self.google_section.pack(fill="x")
        else:
            self.google_section.pack_forget()

    # ── Tab: Model ────────────────────────────────────────────────────────────

    def _build_model(self, f):
        self._section(f, "Whisper Models")
        mf = tk.Frame(f, bg=self.BG); mf.pack(fill="x", padx=20, pady=(4, 0))
        for name, info in MODELS.items():
            self._model_card(mf, name, info)

        self._section(f, "GPU-Accelerated  (Best Armenian Quality)")
        gf = tk.Frame(f, bg=self.BG); gf.pack(fill="x", padx=20, pady=(4, 16))
        self._nemo_card(gf)

    def _model_card(self, parent, name, info):
        ok  = model_ok(name)
        sel = self.model_var.get() == name

        bg   = self.SEL_BG   if sel  else (self.CARD    if ok else "#f9f9f9")
        bord = cfg["accent_color"] if sel else (self.BORDER if ok else "#ebebeb")
        fg   = self.FG  if ok else "#b0b0ba"
        fg2  = self.FG2 if ok else "#c8c8d0"

        card = tk.Frame(parent, bg=bord, padx=1, pady=1)
        card.pack(fill="x", pady=(0, 6))
        inner = tk.Frame(card, bg=bg, padx=14, pady=9); inner.pack(fill="x")

        left  = tk.Frame(inner, bg=bg); left.pack(side="left",  fill="x", expand=True)
        right = tk.Frame(inner, bg=bg); right.pack(side="right", anchor="center")

        name_row = tk.Frame(left, bg=bg); name_row.pack(anchor="w")
        tk.Label(name_row, text=name, bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        if sel:
            tk.Label(name_row, text="  ✓", bg=bg, fg=cfg["accent_color"],
                     font=("Segoe UI", 10)).pack(side="left")
        elif not ok:
            tk.Label(name_row, text="  locked", bg=bg, fg="#c8c8d0",
                     font=("Segoe UI", 8)).pack(side="left")

        tk.Label(left, text=f"{info['quality']}  ·  {info['size']}",
                 bg=bg, fg=fg2, font=("Segoe UI", 8)).pack(anchor="w")

        tk.Label(right, text=info["speed"], bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 13)).pack()
        tk.Label(right, text="per clip", bg=bg, fg=fg2,
                 font=("Segoe UI", 7)).pack()
        if not ok:
            tk.Label(right, text=f"Need {info['min_ram']} GB RAM",
                     bg=bg, fg=fg2, font=("Segoe UI", 7)).pack()

        if ok:
            def _pick(e=None, n=name):
                self.model_var.set(n)
                self._switch_tab(self._active_tab)
            for w in [card, inner, left, right, name_row] + \
                     list(left.winfo_children()) + list(name_row.winfo_children()) + \
                     list(right.winfo_children()):
                try: w.configure(cursor="hand2")
                except: pass
                w.bind("<Button-1>", _pick)
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

        tk.Label(left, text="Armenian best · 9.9% WER · GPU required",
                 bg=bg, fg=fg2, font=("Segoe UI", 8)).pack(anchor="w")

        tk.Label(right, text="~2s",    bg=bg, fg=fg, font=("Segoe UI Semibold", 13)).pack()
        tk.Label(right, text="per clip", bg=bg, fg=fg2, font=("Segoe UI", 7)).pack()
        tk.Label(right, text="Coming soon" if gpu else "GPU required",
                 bg=bg, fg=fg2, font=("Segoe UI", 7)).pack()

    # ── Tab: Language ─────────────────────────────────────────────────────────

    def _build_language(self, f):
        self._section(f, "Recognition Language")
        lf = tk.Frame(f, bg=self.BG); lf.pack(fill="x", padx=20, pady=(6, 0))
        self._lang_pills = {}
        for i, (code, name) in enumerate(LANG_NAMES.items()):
            self._lang_pill(lf, code, name, i)

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
        self.prompt_text.insert("1.0", cfg.get("initial_prompt", ""))

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
            self.lang_var.set(c)
            self._switch_tab(self._active_tab)

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
        for name, hex_v in PALETTE.items():
            self._color_swatch(cf, name, hex_v)

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
            self.color_var.set(n)
            self._switch_tab(self._active_tab)
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

            self.win.after(0, lambda: self.test_result.set(msg))
            self.win.after(0, lambda: self.test_label.configure(fg=col))

        threading.Thread(target=_run, daemon=True).start()

    def _save(self):
        old_hk = cfg["hotkey"]
        cfg["hotkey"]         = self._captured_hotkey or cfg["hotkey"]
        cfg["whisper_model"]  = self.model_var.get()
        cfg["language"]       = self.lang_var.get()
        cfg["accent_color"]   = PALETTE.get(self.color_var.get(), PALETTE["Blue"])
        cfg["backend"]        = self.backend_var.get()
        cfg["google_api_key"] = self.api_key_var.get().strip()
        cfg["initial_prompt"] = self.prompt_text.get("1.0", tk.END).strip() \
            if hasattr(self, "prompt_text") and self.prompt_text.winfo_exists() else \
            cfg.get("initial_prompt", "")
        save_config(cfg)

        if old_hk != cfg["hotkey"]:
            self.app._setup_hotkey(cfg["hotkey"], remove_old=old_hk)

        threading.Thread(
            target=lambda: self.app.recorder.load_model(cfg["whisper_model"]),
            daemon=True,
        ).start()
        self.win.destroy()

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

        self._setup_hotkey(cfg["hotkey"])
        keyboard.add_hotkey("enter", self._on_enter)
        keyboard.add_hotkey("esc",   self._on_escape)
        threading.Thread(target=self.recorder.load_model, daemon=True).start()

    def _setup_hotkey(self, hotkey, remove_old=None):
        if remove_old:
            if remove_old.startswith("mouse:"):
                if self._mouse_listener:
                    self._mouse_listener.stop()
                    self._mouse_listener = None
            else:
                try: keyboard.remove_hotkey(remove_old)
                except: pass

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
            self._mouse_listener = pynput_mouse.Listener(on_click=_on_click)
            self._mouse_listener.daemon = True
            self._mouse_listener.start()
        else:
            keyboard.add_hotkey(hotkey, self._on_hotkey)

    def _on_levels(self, levels):
        self.overlay.root.after(0, lambda: self.overlay.update_levels(levels))

    def _on_lang(self, code, name):
        self.overlay.root.after(0, lambda: self.overlay.set_lang(name))

    def _on_partial(self, text):
        self.overlay.root.after(0, lambda: self.overlay.set_partial(text))

    def _on_hotkey(self):
        if not self.is_rec:
            self.overlay.root.after(0, self._start)
        else:
            threading.Thread(target=self._stop, daemon=True).start()

    def _on_enter(self):
        if self.is_rec:
            threading.Thread(target=self._stop, daemon=True).start()

    def _on_escape(self):
        if self.is_rec:
            threading.Thread(target=self._cancel, daemon=True).start()

    def _start(self):
        self.is_rec = True
        self.overlay._partial = ""
        self.overlay.show(RECORDING)
        self.recorder.start_recording()

    def _cancel(self):
        self.recorder.stop_recording()
        self.is_rec = False
        self.overlay.root.after(0, self.overlay.hide)

    def _stop(self):
        self.recorder.stop_recording()
        self.overlay.root.after(0, lambda: self.overlay.set_state(TRANSCRIBING))
        text, lang = self.recorder.transcribe()
        self.is_rec = False

        if not text:
            if lang and lang.startswith("!google:"):
                err_msg = lang[8:]          # strip "!google:" prefix
                self.overlay.root.after(0, lambda m=err_msg: self.overlay.show_error(m))
            else:
                self.overlay.root.after(0, self.overlay.hide)
            return

        hist.save_entry(text, lang, cfg["backend"])

        time.sleep(0.35)
        pyperclip.copy(text)

        pasted = False
        try:
            mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
            self.kbd.press(mod); self.kbd.press("v")
            self.kbd.release("v"); self.kbd.release(mod)
            pasted = True
        except Exception as e:
            print(f"[Paste] Error: {e}")

        self.overlay.root.after(0, lambda: self.overlay.show_done(pasted))

    def open_settings(self):
        self.overlay.root.after(0, self.settings.open)

    def open_history(self):
        self.overlay.root.after(0, self.history.open)

    def shutdown(self):
        if self._mouse_listener:
            try: self._mouse_listener.stop()
            except Exception: pass
        self.recorder.shutdown()

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
    def on_settings(icon, _): app.open_settings()
    def on_history(icon, _):  app.open_history()
    def on_quit(icon, _):
        icon.stop()
        app.shutdown()
        app.overlay.root.after(0, app.overlay.root.quit)

    pystray.Icon("transcribe", make_icon(cfg["accent_color"]),
        f"Transcribe  ·  {cfg['hotkey']}",
        menu=pystray.Menu(
            pystray.MenuItem("Transcribe", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("History",  on_history),
            pystray.MenuItem("Settings", on_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",     on_quit),
        )).run()

def _apply_taskbar_icon(root):
    try:
        import tempfile, os
        ico_path = os.path.join(tempfile.gettempdir(), "transcribe_app.ico")
        img = make_icon(cfg["accent_color"]).resize((256, 256), Image.LANCZOS)
        img.save(ico_path, format="ICO", sizes=[(256,256),(64,64),(32,32),(16,16)])
        root.iconbitmap(ico_path)
    except Exception:
        pass

def main():
    overlay = Overlay()
    _apply_taskbar_icon(overlay.root)
    app     = App(overlay)
    threading.Thread(target=run_tray, args=(app,), daemon=True).start()
    overlay.run()

if __name__ == "__main__":
    main()
