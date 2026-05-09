import sys, threading, time, json, math, wave, io, struct
import tkinter as tk
from tkinter import ttk
import psutil, pyaudio, numpy as np, keyboard, pyperclip, pystray, requests, base64, ctypes
from PIL import Image, ImageDraw
from pynput.keyboard import Controller, Key
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
}

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return {**DEFAULT, **json.load(f)}
    except:
        return DEFAULT.copy()

def save_config(c):
    with open(CONFIG_PATH, "w") as f:
        json.dump(c, f, indent=2)

cfg = load_config()

# ── System Info ───────────────────────────────────────────────────────────────

RAM_GB = psutil.virtual_memory().total / (1024 ** 3)

MODELS = {
    "tiny":     {"min_ram": 2,  "speed": "~0.5s",  "quality": "Good",      "size": "75 MB"},
    "base":     {"min_ram": 4,  "speed": "~1s",    "quality": "Better",    "size": "140 MB"},
    "small":    {"min_ram": 6,  "speed": "~3s",    "quality": "Great",     "size": "460 MB"},
    "medium":   {"min_ram": 10, "speed": "~8s",    "quality": "Excellent", "size": "1.4 GB"},
    "large-v3": {"min_ram": 16, "speed": "~15s",   "quality": "Best",      "size": "3 GB"},
}

LANG_NAMES = {
    "auto": "Auto-detect",
    "hy":   "Armenian",
    "en":   "English",
    "ru":   "Russian",
    "fr":   "French",
    "de":   "German",
    "es":   "Spanish",
    "ar":   "Arabic",
}

def model_ok(name):
    return RAM_GB >= MODELS[name]["min_ram"]

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

CHUNK_SEC = 4  # transcribe every N seconds in background

class AudioRecorder:
    def __init__(self):
        self.recording        = False
        self.frames           = []
        self.audio            = pyaudio.PyAudio()
        self._model           = None
        self._model_name      = None
        self.on_levels        = None
        self.on_lang_detected = None
        self.on_partial       = None   # callback(partial_text)
        # chunked streaming state
        self._chunk_frames    = []
        self._chunk_results   = {}
        self._chunk_idx       = 0
        self._chunk_lock      = threading.Lock()
        self._samples_in_chunk= 0

    def load_model(self, name=None):
        name = name or cfg["whisper_model"]
        if self._model is None or self._model_name != name:
            self._model      = WhisperModel(name, device="cpu", compute_type="int8")
            self._model_name = name

    def start_recording(self):
        self.recording         = True
        self.frames            = []
        self._chunk_frames     = []
        self._chunk_results    = {}
        self._chunk_idx        = 0
        self._samples_in_chunk = 0
        self.stream = self.audio.open(
            format=pyaudio.paFloat32, channels=1,
            rate=cfg["sample_rate"], input=True,
            frames_per_buffer=cfg["chunk_size"]
        )
        threading.Thread(target=self._record, daemon=True).start()

    def _record(self):
        samples_per_chunk = cfg["sample_rate"] * CHUNK_SEC
        while self.recording:
            try:
                data = self.stream.read(cfg["chunk_size"], exception_on_overflow=False)
                self.frames.append(data)
                self._chunk_frames.append(data)

                arr = np.frombuffer(data, dtype=np.float32)
                self._samples_in_chunk += len(arr)

                if self.on_levels:
                    n, sz  = 20, max(len(arr)//20, 1)
                    levels = [min(float(np.abs(arr[i*sz:(i+1)*sz]).mean())*20, 1.0) for i in range(n)]
                    self.on_levels(levels)

                # every CHUNK_SEC, dispatch a background transcription
                if self._samples_in_chunk >= samples_per_chunk:
                    chunk_audio  = np.frombuffer(b"".join(self._chunk_frames), dtype=np.float32).copy()
                    idx          = self._chunk_idx
                    self._chunk_idx    += 1
                    self._chunk_frames  = []
                    self._samples_in_chunk = 0
                    threading.Thread(target=self._transcribe_chunk,
                                     args=(chunk_audio, idx), daemon=True).start()
            except:
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
            if lang and self.on_lang_detected:
                self.on_lang_detected(lang, LANG_NAMES.get(lang, lang.upper()))
        except:
            pass

    def stop_recording(self):
        self.recording = False
        time.sleep(0.15)
        try:
            self.stream.stop_stream()
            self.stream.close()
        except:
            pass

    def _float_to_wav(self, audio_float):
        int_data = (audio_float * 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(cfg["sample_rate"])
            wf.writeframes(int_data.tobytes())
        return buf.getvalue()

    def transcribe(self):
        # Transcribe remaining (last partial chunk) then merge
        remaining = np.frombuffer(b"".join(self._chunk_frames), dtype=np.float32).copy()
        last_text, detected = "", "en"

        if len(remaining) >= cfg["sample_rate"] // 2:
            if cfg["backend"] == "google" and cfg["google_api_key"]:
                last_text, detected = self._run_google(remaining)
            else:
                last_text, detected = self._run_local(remaining)

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
        return full_text.strip(), detected

    def _run_local(self, audio):
        self.load_model()
        if len(audio) < cfg["sample_rate"] // 2:
            return "", "en"

        lang_arg = cfg["language"] if cfg["language"] != "auto" else None

        # Two-pass: detect language first, then transcribe with explicit lang.
        # This forces Whisper to output native script (e.g. Armenian letters,
        # not Latin transliteration).
        if lang_arg is None:
            sample = audio[:cfg["sample_rate"] * 8]
            _, detect_info = self._model.transcribe(
                sample, language=None, beam_size=1,
                vad_filter=False, without_timestamps=True
            )
            lang_arg = detect_info.language

        segs, info = self._model.transcribe(
            audio, language=lang_arg, beam_size=3, vad_filter=True
        )
        return " ".join(s.text.strip() for s in segs).strip(), lang_arg

    def _run_google(self, audio):
        wav    = self._float_to_wav(audio)
        b64    = base64.b64encode(wav).decode()
        if cfg["language"] == "auto":
            primary, alts = "hy-AM", ["en-US", "ru-RU"]
        else:
            bcp = {"hy":"hy-AM","en":"en-US","ru":"ru-RU",
                   "fr":"fr-FR","de":"de-DE","es":"es-ES","ar":"ar-AE"}
            primary, alts = bcp.get(cfg["language"], "hy-AM"), []
        payload = {
            "config": {"encoding":"LINEAR16","sampleRateHertz":cfg["sample_rate"],
                       "languageCode":primary,"alternativeLanguageCodes":alts,
                       "enableAutomaticPunctuation":True,"model":"latest_long"},
            "audio":  {"content": b64}
        }
        try:
            resp = requests.post(
                f"https://speech.googleapis.com/v1/speech:recognize?key={cfg['google_api_key']}",
                json=payload, timeout=15)
            data = resp.json()
            if "results" not in data:
                return "", "?"
            text = " ".join(r["alternatives"][0]["transcript"]
                            for r in data["results"] if r.get("alternatives"))
            lang = data["results"][0].get("languageCode","hy-AM").split("-")[0]
            return text.strip(), lang
        except Exception as e:
            return f"[error: {e}]", "?"

    def __del__(self):
        try:
            self.audio.terminate()
        except:
            pass

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
        self._done_msg    = "Pasted to cursor" if pasted else "Copied to clipboard"
        self.state        = DONE
        self._visible     = True
        self._hide_at     = time.time() + 2.2

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
        t  = time.time()
        p  = 0.5 + 0.5*math.sin(t*4)
        r  = 4 + p*2
        cy = H//2 - 14
        c.create_oval(18-r, cy-r, 18+r, cy+r, fill="#ff3b3b", outline="")
        c.create_text(32, cy, anchor="w", text="Recording",
                      fill="#ffffff", font=("Segoe UI Semibold", 11))
        c.create_text(32, cy+15, anchor="w",
                      text=f"Enter or {cfg['hotkey']} to stop  ·  Esc to cancel",
                      fill="#454545", font=("Segoe UI", 8))

        # waveform — fills full width, tall bars
        pad_x   = 14
        num     = 36
        avail   = W - pad_x*2
        step    = avail / num
        bar_w   = max(int(step * 0.55), 1)
        cy_wave = H - 14
        max_h   = 14
        ar, ag, ab = int(accent[1:3],16), int(accent[3:5],16), int(accent[5:7],16)

        smooth = self._smooth
        for i in range(num):
            t_idx  = i / max(num-1,1) * (len(smooth)-1)
            lo     = int(t_idx)
            hi     = min(lo+1, len(smooth)-1)
            lv     = smooth[lo]*(1-(t_idx-lo)) + smooth[hi]*(t_idx-lo)
            bh     = max(int(lv * max_h), 2)
            x      = int(pad_x + i*step)
            fac    = 0.4 + 0.6*lv
            col    = f"#{int(ar*fac):02x}{int(ag*fac):02x}{int(ab*fac):02x}"
            c.create_rectangle(x, cy_wave-bh, x+bar_w, cy_wave+bh,
                               fill=col, outline="")

    def _draw_loading(self, c, W, H, accent):
        angle = -(time.time()*300) % 360
        cx, cy, r = 22, 28, 11
        c.create_oval(cx-r, cy-r, cx+r, cy+r, outline="#252525", width=2)
        c.create_arc(cx-r, cy-r, cx+r, cy+r,
                     start=angle, extent=250,
                     style=tk.ARC, outline=accent, width=2)

        dots  = "." * (int(time.time()*2) % 4)
        label = f"Transcribing in {self._lang}{dots}" if self._lang else f"Finalising{dots}"
        c.create_text(42, 22, anchor="w", text=label,
                      fill="#ffffff", font=("Segoe UI Semibold", 11))
        backend_label = "Google Cloud" if cfg["backend"]=="google" else "Local"
        c.create_text(42, 36, anchor="w",
                      text=f"via {backend_label}  ·  pasting when ready",
                      fill="#444444", font=("Segoe UI", 8))

        if self._partial:
            # show last ~55 chars of partial so it fits in the bar
            preview = self._partial[-55:].lstrip()
            if len(self._partial) > 55:
                preview = "…" + preview
            c.create_rectangle(10, 50, W-10, H-8, fill="#1a1a1a", outline="#2a2a2a")
            c.create_text(16, (50+H-8)//2, anchor="w", text=preview,
                          fill="#aaaaaa", font=("Segoe UI", 9))

    def _draw_done(self, c, W, H):
        # green circle with checkmark
        cx, cy, r = 22, H//2, 13
        c.create_oval(cx-r, cy-r, cx+r, cy+r, fill="#1a3a24", outline="#22c55e", width=1)
        # checkmark lines
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
    BG   = "#0f0f0f"
    CARD = "#181818"
    SEP  = "#212121"
    FG   = "#ffffff"
    FG2  = "#555555"

    def __init__(self, root, app):
        self.root = root
        self.app  = app
        self.win  = None

    def open(self):
        if self.win and self.win.winfo_exists():
            self.win.lift(); return

        win = tk.Toplevel(self.root)
        self.win = win
        win.title("Settings")
        win.resizable(False, False)
        win.configure(bg=self.BG)
        win.geometry("480x680")
        win.attributes("-topmost", True)
        win.update_idletasks()
        apply_glass(win.winfo_id(), "#0f0f0ff2")

        # scrollable canvas
        outer = tk.Frame(win, bg=self.BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=self.BG, highlightthickness=0)
        sb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frame = tk.Frame(canvas, bg=self.BG)
        fw = canvas.create_window((0,0), window=frame, anchor="nw")

        def _resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(fw, width=canvas.winfo_width())
        frame.bind("<Configure>", _resize)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(fw, width=e.width))
        win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        self._build(frame)

    def _build(self, f):
        # Header
        tk.Label(f, text="Settings", bg=self.BG, fg=self.FG,
                 font=("Segoe UI Semibold", 18)).pack(anchor="w", padx=24, pady=(22,2))
        tk.Label(f, text=f"{RAM_GB:.0f} GB RAM  ·  {psutil.cpu_count()} CPU cores",
                 bg=self.BG, fg=self.FG2, font=("Segoe UI", 9)).pack(anchor="w", padx=24)
        self._sep(f)

        # ── Backend ──────────────────────────────────────────────────────────
        self._label(f, "BACKEND")
        self.backend_var = tk.StringVar(value=cfg["backend"])
        bf = tk.Frame(f, bg=self.BG)
        bf.pack(anchor="w", padx=24, pady=(8,4))
        for val, title, desc in [
            ("local",  "Local",  "Offline · private · free"),
            ("google", "Google Cloud", "Online · best for Armenian · 60 min/mo free"),
        ]:
            brd = cfg["accent_color"] if self.backend_var.get()==val else self.SEP
            bg  = "#1a2233" if self.backend_var.get()==val else self.CARD
            fr  = tk.Frame(bf, bg=brd, padx=1, pady=1)
            fr.pack(side="left", padx=(0,10))
            inn = tk.Frame(fr, bg=bg, padx=16, pady=10); inn.pack()
            tk.Radiobutton(inn, text=title, variable=self.backend_var, value=val,
                           bg=bg, fg=self.FG, selectcolor=bg,
                           activebackground=bg, font=("Segoe UI Semibold", 11),
                           cursor="hand2",
                           command=self._on_backend_change).pack(anchor="w")
            tk.Label(inn, text=desc, bg=bg, fg=self.FG2,
                     font=("Segoe UI", 8)).pack(anchor="w")

        self._sep(f)

        # ── Google section (hidden when local) ───────────────────────────────
        self.google_frame = tk.Frame(f, bg=self.BG)
        self._label(self.google_frame, "GOOGLE API KEY")
        self.api_key_var = tk.StringVar(value=cfg["google_api_key"])
        kf = tk.Frame(self.google_frame, bg=self.CARD,
                      highlightthickness=1, highlightbackground=self.SEP)
        kf.pack(fill="x", padx=24, pady=(4,4))
        tk.Entry(kf, textvariable=self.api_key_var, bg=self.CARD,
                 fg=self.FG, insertbackground=self.FG, show="•",
                 font=("Segoe UI", 10), relief="flat", bd=8).pack(fill="x")
        self.test_result = tk.StringVar(value="")
        tr = tk.Frame(self.google_frame, bg=self.BG)
        tr.pack(anchor="w", padx=24, pady=(0,4))
        tk.Button(tr, text="Test Key", command=self._test_google,
                  bg="#1e1e1e", fg="#aaaaaa", activebackground="#2a2a2a",
                  font=("Segoe UI", 9), relief="flat", padx=10, pady=4,
                  cursor="hand2").pack(side="left")
        self.test_label = tk.Label(tr, textvariable=self.test_result,
                                   bg=self.BG, fg="#888888", font=("Segoe UI", 9))
        self.test_label.pack(side="left", padx=(10,0))
        sep_g = tk.Frame(self.google_frame, bg=self.SEP, height=1)
        sep_g.pack(fill="x", pady=(8,0))

        # ── Local model section (hidden when google) ─────────────────────────
        self.local_frame = tk.Frame(f, bg=self.BG)
        self._label(self.local_frame, "MODEL")
        self.model_var = tk.StringVar(value=cfg["whisper_model"])
        mf = tk.Frame(self.local_frame, bg=self.BG)
        mf.pack(fill="x", padx=24, pady=(6,4))
        for name, info in MODELS.items():
            self._model_row(mf, name, info)
        tk.Frame(self.local_frame, bg=self.SEP, height=1).pack(fill="x", pady=(8,0))

        # Show correct section
        self._on_backend_change()

        # ── Hotkey ───────────────────────────────────────────────────────────
        self._label(f, "HOTKEY")
        self.hotkey_var = tk.StringVar(value=cfg["hotkey"])
        hf = tk.Frame(f, bg=self.CARD, highlightthickness=1, highlightbackground=self.SEP)
        hf.pack(fill="x", padx=24, pady=(4,0))
        tk.Entry(hf, textvariable=self.hotkey_var, bg=self.CARD,
                 fg=self.FG, insertbackground=self.FG,
                 font=("Segoe UI", 11), relief="flat", bd=8).pack(fill="x")
        self._sep(f)

        # ── Language ─────────────────────────────────────────────────────────
        self._label(f, "LANGUAGE")
        self.lang_var = tk.StringVar(value=cfg["language"])
        lf = tk.Frame(f, bg=self.BG)
        lf.pack(anchor="w", padx=24, pady=(6,4))
        for i, (code, name) in enumerate(LANG_NAMES.items()):
            tk.Radiobutton(lf, text=name, variable=self.lang_var, value=code,
                           bg=self.BG, fg="#cccccc", selectcolor=self.CARD,
                           activebackground=self.BG, activeforeground=self.FG,
                           font=("Segoe UI", 10), cursor="hand2"
                           ).grid(row=i//3, column=i%3, sticky="w", padx=(0,20), pady=2)
        self._sep(f)

        # ── Color ─────────────────────────────────────────────────────────────
        self._label(f, "ACCENT COLOR")
        self.color_var = tk.StringVar(value=self._color_name())
        cf = tk.Frame(f, bg=self.BG)
        cf.pack(anchor="w", padx=24, pady=(6,16))
        for name, hex_v in PALETTE.items():
            self._color_swatch(cf, name, hex_v)

        # ── Save ──────────────────────────────────────────────────────────────
        tk.Button(f, text="Save & Apply", command=self._save,
                  bg=cfg["accent_color"], fg="#ffffff",
                  activebackground=cfg["accent_color"],
                  font=("Segoe UI Semibold", 11), relief="flat",
                  bd=0, padx=32, pady=10, cursor="hand2").pack(pady=(4,28))

    def _on_backend_change(self):
        if self.backend_var.get() == "google":
            self.local_frame.pack_forget()
            self.google_frame.pack(fill="x", after=self._sep_ref() or self.google_frame)
        else:
            self.google_frame.pack_forget()
            self.local_frame.pack(fill="x", after=self._sep_ref() or self.local_frame)

    def _sep_ref(self):
        return None

    def _radio_card(self, parent, var, val, title, desc):
        sel  = var.get() == val
        bg   = "#1e2a3a" if sel else self.CARD
        bord = cfg["accent_color"] if sel else self.SEP
        f    = tk.Frame(parent, bg=bord, padx=1, pady=1)
        f.pack(side="left", padx=(0,10))
        inner = tk.Frame(f, bg=bg, padx=14, pady=10)
        inner.pack()
        tk.Radiobutton(inner, text=title, variable=var, value=val,
                       bg=bg, fg=self.FG, selectcolor=bg,
                       activebackground=bg, activeforeground=self.FG,
                       font=("Segoe UI Semibold", 10), cursor="hand2").pack(anchor="w")
        tk.Label(inner, text=desc, bg=bg, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(anchor="w")

    def _model_row(self, parent, name, info):
        ok   = model_ok(name)
        sel  = self.model_var.get() == name
        fg   = self.FG  if ok else "#333333"
        fg2  = "#888888" if ok else "#2a2a2a"
        bg   = "#1a2233" if sel else (self.CARD if ok else "#111111")
        bord = cfg["accent_color"] if sel else (self.SEP if ok else "#151515")

        f = tk.Frame(parent, bg=bord, padx=1, pady=1)
        f.pack(fill="x", pady=(0,6))
        inner = tk.Frame(f, bg=bg, padx=14, pady=8)
        inner.pack(fill="x")

        left = tk.Frame(inner, bg=bg); left.pack(side="left", fill="x", expand=True)
        right = tk.Frame(inner, bg=bg); right.pack(side="right")

        top = tk.Frame(left, bg=bg); top.pack(anchor="w")
        lock = " 🔒" if not ok else (" ✓" if sel else "")
        lock_color = "#333" if not ok else cfg["accent_color"]
        tk.Label(top, text=name, bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 11)).pack(side="left")
        tk.Label(top, text=lock, bg=bg, fg=lock_color,
                 font=("Segoe UI", 10)).pack(side="left")
        tk.Label(left, text=f"{info['quality']}  ·  {info['size']}",
                 bg=bg, fg=fg2, font=("Segoe UI", 8)).pack(anchor="w")

        tk.Label(right, text=info["speed"], bg=bg, fg=fg,
                 font=("Segoe UI Semibold", 12)).pack()
        tk.Label(right, text="per clip", bg=bg, fg=fg2,
                 font=("Segoe UI", 7)).pack()

        if ok:
            for w in [f, inner, left, right, top] + \
                     list(left.winfo_children()) + list(top.winfo_children()) + \
                     list(right.winfo_children()):
                w.configure(cursor="hand2")
                w.bind("<Button-1>", lambda e, n=name: self._pick_model(n))

        if not ok:
            tk.Label(right, text=f"Need {info['min_ram']}GB RAM",
                     bg=bg, fg="#2a1a1a", font=("Segoe UI", 7)).pack()

    def _pick_model(self, name):
        self.model_var.set(name)
        # refresh rows
        for w in self.win.winfo_children():
            pass  # full rebuild not needed; save will apply

    def _color_swatch(self, parent, name, hex_v):
        sel  = self.color_var.get() == name
        bord = hex_v if sel else "#1e1e1e"
        f = tk.Frame(parent, bg=bord, padx=2, pady=2); f.pack(side="left", padx=(0,6))
        btn = tk.Label(f, text=name, bg="#181818", fg=hex_v,
                       font=("Segoe UI", 9), padx=10, pady=5, cursor="hand2")
        btn.pack()
        btn.bind("<Button-1>", lambda e, n=name: self._pick_color(n))

    def _pick_color(self, name):
        self.color_var.set(name)

    def _color_name(self):
        for n, h in PALETTE.items():
            if h == cfg["accent_color"]: return n
        return "Blue"

    def _label(self, parent, text):
        tk.Label(parent, text=text, bg=self.BG, fg=self.FG2,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=24, pady=(14,0))

    def _sep(self, parent):
        tk.Frame(parent, bg=self.SEP, height=1).pack(fill="x", pady=(10,0))

    def _test_google(self):
        key = self.api_key_var.get().strip()
        if not key:
            self.test_result.set("⚠ No key entered")
            self.test_label.configure(fg="#f97316")
            return
        self.test_result.set("Testing...")
        self.test_label.configure(fg="#888888")
        self.win.update()

        def _run():
            try:
                # Send a minimal silent audio clip just to test auth
                silence = b"\x00\x00" * 1600  # 0.1s of silence
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1); wf.setsampwidth(2)
                    wf.setframerate(16000); wf.writeframes(silence)
                audio_b64 = base64.b64encode(buf.getvalue()).decode()
                payload = {
                    "config": {"encoding":"LINEAR16","sampleRateHertz":16000,
                               "languageCode":"en-US"},
                    "audio":  {"content": audio_b64}
                }
                resp = requests.post(
                    f"https://speech.googleapis.com/v1/speech:recognize?key={key}",
                    json=payload, timeout=10
                )
                data = resp.json()
                if resp.status_code == 200:
                    msg, col = "✓ Key works!", "#22c55e"
                elif resp.status_code == 403:
                    err = data.get("error", {}).get("message", "Forbidden")
                    msg, col = f"✗ 403: {err}", "#ef4444"
                elif resp.status_code == 400:
                    err = data.get("error", {}).get("message", "Bad request")
                    msg, col = f"✗ 400: {err}", "#ef4444"
                else:
                    msg, col = f"✗ {resp.status_code}: {data}", "#ef4444"
            except Exception as e:
                msg, col = f"✗ {e}", "#ef4444"

            self.win.after(0, lambda: self.test_result.set(msg))
            self.win.after(0, lambda: self.test_label.configure(fg=col))

        threading.Thread(target=_run, daemon=True).start()

    def _save(self):
        old_hk = cfg["hotkey"]
        cfg["hotkey"]         = self.hotkey_var.get().strip() or cfg["hotkey"]
        cfg["whisper_model"]  = self.model_var.get()
        cfg["language"]       = self.lang_var.get()
        cfg["accent_color"]   = PALETTE[self.color_var.get()]
        cfg["backend"]        = self.backend_var.get()
        cfg["google_api_key"] = self.api_key_var.get().strip()
        save_config(cfg)

        if old_hk != cfg["hotkey"]:
            try: keyboard.remove_hotkey(old_hk)
            except: pass
            keyboard.add_hotkey(cfg["hotkey"], self.app._on_hotkey)

        threading.Thread(
            target=lambda: self.app.recorder.load_model(cfg["whisper_model"]),
            daemon=True
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
        self.kbd      = Controller()
        self.is_rec   = False
        self.settings = Settings(overlay.root, self)
        self.history  = HistoryWindow(overlay.root)

        keyboard.add_hotkey(cfg["hotkey"], self._on_hotkey)
        keyboard.add_hotkey("enter",       self._on_enter)
        keyboard.add_hotkey("esc",         self._on_escape)
        threading.Thread(target=self.recorder.load_model, daemon=True).start()

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
        text, _ = self.recorder.transcribe()
        self.is_rec = False

        if not text:
            self.overlay.root.after(0, self.overlay.hide)
            return

        hist.save_entry(text, lang, cfg["backend"])

        time.sleep(0.35)
        pyperclip.copy(text)

        pasted = False
        try:
            self.kbd.press(Key.ctrl); self.kbd.press("v")
            self.kbd.release("v");    self.kbd.release(Key.ctrl)
            pasted = True
        except:
            pass

        self.overlay.root.after(0, lambda: self.overlay.show_done(pasted))

    def open_settings(self):
        self.overlay.root.after(0, self.settings.open)

    def open_history(self):
        self.overlay.root.after(0, self.history.open)

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

def main():
    overlay = Overlay()
    app     = App(overlay)
    threading.Thread(target=run_tray, args=(app,), daemon=True).start()
    overlay.run()

if __name__ == "__main__":
    main()
