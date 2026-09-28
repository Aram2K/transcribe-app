"""Dictation HUD: the small liquid-glass capsule shown while the user dictates,
then while the words are transcribed and pasted.

Design (researched against Wispr Flow's Flow Bar, VoiceInk's and Handy's
recorders, Apple's Dynamic Island / Liquid Glass guidance and Windows voice
typing):

* No words while recording. A red dot with a sonar ring says "recording", its
  glow and the waveform follow the voice ("I can hear you"), and a timer
  ticks. For the first dictations of a run the Enter / Esc keycaps show for a
  moment, then hand their place to the timer and the capsule settles.
* Bottom-centre of the screen the user is typing on, just above the taskbar -
  or wherever the user dragged it (remembered; double-click to send it home).
* Colour means status only: red recording, green done, red error. Glyphs
  and spinners are neutral ink.
* Glass that adapts to what is behind it, as Apple's does ("auto", the
  default): frosted light glass over bright content, smoked dark glass over
  dark content, picked per dictation; the system light/dark theme when there
  is no backdrop to look at. A blurred LIVE sample of what is behind, a tint
  that gets denser where the glyphs need it, a crisp one-pixel edge and a
  soft shadow - nothing hazy.
* Live, because a snapshot goes stale: whatever moves behind the capsule, or
  the capsule itself being dragged, would leave it showing the wrong picture
  (it read as a foggy grey rim). Once visible, Windows includes this window in
  every screen grab (checked on this project's Win11 build: BitBlt with and
  without CAPTUREBLT, and QScreen.grabWindow), so - like Live Assistance - the
  HUD is excluded from screen capture while it samples ("overlay_private",
  on by default: it stays out of shares and recordings). A worker thread does
  the grabbing; a grab waits for the next DWM frame (~17 ms) and would stutter
  the animation on the GUI thread. Without exclusion (setting off, old
  Windows, Remote Desktop) it paints a smoked plate instead; solid when
  Windows transparency effects are off.
* The glass is composed once per capsule size / backdrop sample and the static
  content pieces are cached, so a frame is a couple of copies plus the moving
  parts - it is painted on the GUI thread while the recorder thread needs the
  GIL.
* The capsule morphs between states on a spring; the content crossfades.
  Decorative motion stops when Windows animation effects are off.
* Never focusable (WS_EX_NOACTIVATE): dragging it can't pull focus away from
  the field the text is about to be pasted into. Only the capsule takes the
  mouse - the rest of the window lets clicks through.
"""

import logging
import math
import threading
import time

from PySide6.QtCore import Qt, QTimer, QPoint, QPointF, QRectF, Signal
from PySide6.QtGui import (
    QBrush, QColor, QConicalGradient, QCursor, QFont, QFontMetricsF, QImage,
    QInputDevice, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QWidget, QApplication

from ui import glass

logger = logging.getLogger("transcribe")

# Overlay States
RECORDING = "recording"
TRANSCRIBING = "transcribing"
PROCESSING = "processing"   # smart action / LLM running after transcription
DONE = "done"


GOOGLE_STT_DEFAULT = "gemini-2.5-flash"
MISTRAL_STT_DEFAULT = "voxtral-mini-latest"


def _configured_model_name(cfg, key, fallback):
    value = (cfg or {}).get(key, "")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _transcription_ai_label(cfg):
    cfg = cfg or {}
    backend = cfg.get("backend", "local")

    if backend == "google":
        return f"{_configured_model_name(cfg, 'google_stt_model', GOOGLE_STT_DEFAULT)} AI"

    if backend == "mistral":
        return f"{_configured_model_name(cfg, 'mistral_stt_model', MISTRAL_STT_DEFAULT)} AI"

    if backend == "managed":
        provider = str(cfg.get("managed_provider", "gemini")).lower()
        if provider == "mistral":
            model = _configured_model_name(cfg, "mistral_stt_model", MISTRAL_STT_DEFAULT)
        else:
            model = _configured_model_name(cfg, "google_stt_model", GOOGLE_STT_DEFAULT)
        return f"{model} AI"

    return "Local AI"


def _error_hint(msg):
    """A short, contextual next step for an error message."""
    low = (msg or "").lower()
    if "model.bin" in low or "model" in low and "open" in low:
        return "Whisper cache issue - restart app or redownload model"
    if "api" in low or "key" in low or "auth" in low or "401" in low or "403" in low:
        return "Check Settings → API key"
    if "microphone" in low or "input" in low or "audio" in low:
        return "Check microphone access / device"
    if "timeout" in low or "timed out" in low:
        return "Try a shorter clip or smaller model"
    return "Try again or restart Transcribe"


# ── geometry (logical px) ──
WIN_W, WIN_H = 440, 124        # fixed transparent window; the capsule is painted inside
PILL_H = 40
PILL_H_ERROR = 54
PILL_MAX_W = WIN_W - 40
REC_W_HINT = 204               # recording, while the Enter/Esc keycaps show
REC_W = 180                    # recording, once the timer has taken their place
BOTTOM_PAD = 20                # window room under the capsule (its shadow)
TASKBAR_GAP = 16               # capsule bottom above the work area's bottom edge
ICON_X = 21                    # centre of the leading icon, from the capsule's left
TEXT_X = 38                    # text start, from the capsule's left
N_BARS = 15
BAR_W, BAR_GAP = 3.0, 3.0
SAMPLE_MARGIN = 32             # backdrop sampled this far around the window, so the
                               # glass stays pinned to the world between samples
DRAG_SLOP = 3                  # a press must travel this far to become a drag

# ── timing (s) ──
HINT_S = 2.6                   # the keycaps show this long...
HINT_RUNS = 5                  # ...on this many dictations per app run
SLOW_S = 4.0                   # busy this long -> "still working…"
DONE_S = 1.4                   # "Pasted to cursor"
COPIED_S = 2.6                 # "Copied to clipboard": the user still has to paste
ERROR_S = 4.5
HOLD_S = 0.9                   # done/error stay this long after the cursor leaves
SONAR_S = 1.8                  # sonar ring period
SAMPLE_REST_S = 0.25           # live glass re-sample interval at rest (every frame when moving)
FIRST_SAMPLE_S = 0.12          # the fade-in waits at most this long for the first sample
# Frame pacing: 60 fps only while something springs, fades or moves; the
# steady bars / spinner are smooth at 30; a settled "done" barely repaints.
# (A bare 60 Hz timer alone costs ~5 % of a core in PySide.)
FRAME_MS, STEADY_MS, IDLE_MS = 16, 33, 100

# Per-bar oscillators (irrational spacing so the bars never march in step) and
# a centre-weighted envelope: the middle bars move most, like a voice blob.
_BAR_FREQ = [5.3 + 3.4 * ((i * 0.618034) % 1.0) for i in range(N_BARS)]
_BAR_PHASE = [i * 2.39996 for i in range(N_BARS)]
_BAR_ENV = [0.3 + 0.7 * math.exp(-(((i - (N_BARS - 1) / 2) / ((N_BARS - 1) / 2)) ** 2) / 0.3)
            for i in range(N_BARS)]

_PALETTES = {
    # Smoked graphite glass with white glyphs: over dark content, like
    # Dynamic Island or Wispr Flow's bar.
    "dark": {
        "glass": (18, 20, 26), "solid": (44, 44, 44),
        "alpha": (0.55, 0.82),         # tint over a black ... white backdrop
        "plate": 0.92,                 # no live backdrop: a smoked plate
        "ink": (255, 255, 255),
        # Apple's 2025 system colours (dark).
        "rec": "#FF4245", "ok": "#30D158", "err": "#FF4245",
        "edge": (46, 14),              # crisp inner edge: white alpha at the top, the bottom
        "hairline": QColor(0, 0, 0, 80),
        "shadow": QColor(0, 0, 0, 110),
        "key_fill": 24, "key_line": 72,
    },
    # Frosted white glass with slate glyphs: over bright content.
    "light": {
        "glass": (250, 250, 252), "solid": (249, 249, 249),
        "alpha": (0.80, 0.62),         # denser over DARK backdrops here
        "plate": 0.94,
        "ink": (15, 23, 42),
        # Deeper red and green: >= 3:1 against the light glass (Apple's
        # standard ones fall to 2-2.5:1 there).
        "rec": "#E9152D", "ok": "#16A34A", "err": "#C42B1C",
        "edge": (230, 90),
        "hairline": QColor(15, 23, 42, 38),
        "shadow": QColor(15, 23, 42, 70),
        "key_fill": 14, "key_line": 60,
    },
}

AUTO_LIGHT_LUMA = 0.62         # "auto": light glass above this backdrop brightness


def _system_prefers_light():
    """The OS light/dark app theme (Qt reads it on Windows and macOS)."""
    try:
        return QApplication.styleHints().colorScheme() == Qt.ColorScheme.Light
    except Exception:
        return False


def _native_windows():
    """Real Win32 windows - not the offscreen/minimal Qt platforms the tests
    use, whose fake window ids must never reach a Win32 call."""
    try:
        return glass.IS_WINDOWS and QApplication.platformName() == "windows"
    except Exception:
        return False


def _smoothstep(e0, e1, x):
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0)))
    return t * t * (3.0 - 2.0 * t)


def _ink(pal, alpha):
    r, g, b = pal["ink"]
    return QColor(r, g, b, max(0, min(255, int(alpha))))


def _spring(x, v, target, dt, k, c):
    """Damped spring step (semi-implicit Euler, sub-stepped to stay stable)."""
    steps = int(dt / 0.008) + 1
    h = dt / steps
    for _ in range(steps):
        v += (k * (target - x) - c * v) * h
        x += v * h
    return x, v


def _blur(img, factor, w, h):
    """Cheap, good-looking blur: scale down, then back up to w x h."""
    small = img.scaled(max(1, int(img.width() / factor)), max(1, int(img.height() / factor)),
                       Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _luma_under(img, margin):
    """Mean brightness (0..1) of a backdrop sample under the recording
    capsule; the sample starts ``margin`` logical px up-left of the window."""
    d = img.devicePixelRatio()
    x0 = int((margin + (WIN_W - REC_W) / 2) * d)
    y0 = int((margin + WIN_H - BOTTOM_PAD - PILL_H) * d)
    small = img.copy(x0, y0, int(REC_W * d), int(PILL_H * d)).scaled(
        24, 6, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    total = 0.0
    for y in range(small.height()):
        for x in range(small.width()):
            c = small.pixelColor(x, y)
            total += 0.2126 * c.redF() + 0.7152 * c.greenF() + 0.0722 * c.blueF()
    return total / max(1, small.width() * small.height())


def _font(px, weight=QFont.Normal, tabular=False):
    f = QFont("Segoe UI")
    f.setPixelSize(px)
    f.setWeight(weight)
    if tabular:
        try:
            # Segoe UI's digits are proportional: without this the timer
            # jiggles every second.
            f.setFeature(QFont.Tag("tnum"), 1)
        except Exception:
            pass
    return f


class _Sampler(threading.Thread):
    """Samples what is behind the HUD on a worker thread. Idle unless the HUD
    is shown and excluded from capture; every frame while it moves."""

    def __init__(self, hud):
        super().__init__(name="hud-backdrop", daemon=True)
        self.hud = hud
        self.wake = threading.Event()
        self.active = False
        self.fast = False

    def run(self):
        while True:
            if not self.active:
                self.wake.wait()
            elif not self.fast:
                self.wake.wait(SAMPLE_REST_S)
            self.wake.clear()
            if not self.active:
                continue
            gen = self.hud._glass_gen
            try:
                sample = self.hud._grab()
            except Exception as e:
                logger.debug("hud backdrop sample failed: %s", e)
                sample = None
            if sample is not None and self.active:
                try:
                    self.hud._sig_backdrop.emit((gen,) + sample)
                except RuntimeError:
                    return              # the HUD is gone (app shutting down)
            elif sample is None and self.fast:
                time.sleep(0.02)        # never spin if grabbing fails


class Overlay(QWidget):
    # Cross-thread invoker: emit to dispatch a callable onto the Qt main thread.
    # QueuedConnection guarantees the slot runs on this QObject's owning thread
    # regardless of which thread emits - so worker threads (audio recorder,
    # transcription thread) can safely update UI state.
    _invoke = Signal(object)
    # Backdrop samples from the _Sampler thread.
    _sig_backdrop = Signal(object)

    def __init__(self, main_app=None):
        super().__init__()
        self.app = main_app
        self._invoke.connect(self._run_callable, Qt.QueuedConnection)
        self._sig_backdrop.connect(self._on_backdrop, Qt.QueuedConnection)

        # Frameless, on top, no taskbar entry, and never focusable: the app
        # the user types in keeps focus even while the capsule is dragged.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # macOS hides Qt.Tool windows while the app is inactive - and a tray
        # app always is. No-op elsewhere.
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setFixedSize(WIN_W, WIN_H)

        # Application state
        self.overlay_state = RECORDING
        self.levels = [0.0] * 20
        self._visible = False
        self._lang = ""
        self._partial = ""
        self._done_msg = ""
        self._done_pasted = False
        self._done_error = False
        self._hide_at = None
        self._err_split = None
        self._runs = 0                    # dictations shown this app run

        # Animation state
        now = time.monotonic()
        self._motion = True
        self._show = 0.0                  # appear spring: 0 hidden .. 1 shown
        self._show_v = 0.0
        self._pw, self._ph = float(REC_W), float(PILL_H)
        self._pw_v = self._ph_v = 0.0
        self._cur_vis = "rec"
        self._from_vis = None
        self._xfade = 1.0
        self._vis_at = now
        self._rec_started = now
        self._last_frame = now
        self._level = 0.0                 # smoothed loudness 0..1
        self._level_target = 0.0
        self._level_at = 0.0
        self._tex = [0.0] * N_BARS
        self._bars = [0.0] * N_BARS

        # Glass
        self._screen = None
        self._solid = False               # Windows transparency effects off
        self._excluded = False            # out of screen capture right now
        self._live = False                # backdrop sampled live (needs exclusion)
        self._bd = None                   # (blurred sample, physical origin, dpr)
        self._bd_seq = 0
        self._bd_luma = None              # backdrop brightness under the capsule
        self._sampler = None
        self._glass_gen = 0               # bumped per appearance; older samples are dropped
        self._waiting = False             # shown, but invisible until the first sample
        self._shown_at = 0.0
        self._theme = "dark"
        self._dpr = 1.0
        self._glass_cache = {}            # composed glass per capsule size / sample
        self._sprites = {}                # static content pieces
        self._widths = {}                 # text widths
        self._last_box = None             # last frame's repaint area
        self._bar_inks = {}
        self._fit_w = {}                  # final capsule width per state, for text fitting

        # Moving it
        self._hover = None                # cursor over the capsule (None: re-apply)
        self._press = None                # (global press point, window pos at press)
        self._press_mouse = True          # the press came from a real mouse (not touch/pen)
        self._dragging = False
        self._glide = None                # target of an animated move home

        self._f_title = _font(13, QFont.DemiBold)
        self._f_body = _font(12)
        self._f_small = _font(11)
        self._f_timer = _font(12, QFont.DemiBold, tabular=True)
        self._f_key = _font(10, QFont.DemiBold)

        # Frame clock - runs only while the HUD is on screen.
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self._loop)

        self.reposition()
        # Create the native window now: the first dictation shouldn't pay for it.
        self._hwnd = int(self.winId()) if _native_windows() else 0
        if self._hwnd:
            glass.set_no_activate(self)

    # ── placement ──
    def _target_screen(self):
        """The monitor the user is typing on: the foreground window's, else
        the cursor's, else the primary."""
        name = glass.foreground_monitor_name()
        if name:
            for s in QApplication.screens():
                if s.name() == name:
                    return s
        try:
            s = QApplication.screenAt(QCursor.pos())
        except Exception:
            s = None
        return s or QApplication.primaryScreen()

    def _home(self, screen):
        """Default spot: bottom-centre, the capsule TASKBAR_GAP above the taskbar."""
        a = screen.availableGeometry()
        return QPoint(a.x() + (a.width() - WIN_W) // 2,
                      a.y() + a.height() - TASKBAR_GAP - (WIN_H - BOTTOM_PAD))

    def _saved_pos(self):
        """(window pos, screen) the user dragged it to, if the capsule would
        still land on a screen (monitors come and go); else None."""
        pos = (self.app.cfg if self.app else {}).get("overlay_pos")
        if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
            return None
        try:
            p = QPoint(int(pos[0]), int(pos[1]))
        except (TypeError, ValueError):
            return None
        c = (self._capsule(float(REC_W), float(PILL_H)).center() + QPointF(p)).toPoint()
        for s in QApplication.screens():
            if s.availableGeometry().contains(c):
                return p, s
        return None

    def reposition(self):
        saved = self._saved_pos()
        if saved is not None:
            self._screen = saved[1]
            self.move(saved[0])
            return
        screen = self._target_screen()
        if not screen:
            return
        self._screen = screen
        self.move(self._home(screen))

    # ── public API (main thread; workers go through call_soon) ──
    def show_overlay(self, state=RECORDING):
        self._lang = ""
        self._hide_at = None
        self.overlay_state = state
        if state == RECORDING:
            self._runs += 1
            self._rec_started = time.monotonic()
            self._level = self._level_target = 0.0
            self._bars = [0.0] * N_BARS
        self._appear()

    def hide_overlay(self):
        self._visible = False
        self._hide_at = None
        self._kick()

    def set_state(self, s):
        self.overlay_state = s
        self._kick()

    def set_lang(self, lang_name):
        self._lang = lang_name
        self._kick()

    def set_partial(self, text):
        self._partial = text or ""
        self._kick()

    def update_levels(self, levels):
        self.levels = list(levels or [])
        if not self.levels:
            return
        peak = max(self.levels)
        avg = sum(self.levels) / len(self.levels)
        # The recorder already normalises to a rolling peak; gate the noise
        # floor off so a quiet room reads as calm, then lift the mids.
        loud = min(1.0, 0.55 * avg + 0.55 * peak)
        loud = max(0.0, loud - 0.06) / 0.94
        self._level_target = loud ** 0.85
        self._level_at = time.monotonic()
        n, inv = len(self.levels), 1.0 / (peak + 1e-6)
        self._tex = [self.levels[min(n - 1, i * n // N_BARS)] * inv for i in range(N_BARS)]

    def show_done(self, pasted: bool):
        self._done_pasted = pasted
        self._done_error = False
        self._done_msg = "Pasted to cursor" if pasted else "Copied to clipboard"
        self.overlay_state = DONE
        self._hide_at = time.monotonic() + (DONE_S if pasted else COPIED_S)
        self._appear()

    def show_error(self, msg: str):
        self._done_error = True
        self._done_pasted = False
        self._done_msg = msg
        self.overlay_state = DONE
        self._hide_at = time.monotonic() + ERROR_S
        self._appear()

    def call_soon(self, func, *args, **kwargs):
        # Marshal onto Qt main thread via Signal - works reliably from any
        # thread. (QTimer.singleShot from a non-Qt thread is documented
        # thread-unsafe and was silently dropping calls.)
        self._invoke.emit(lambda: func(*args, **kwargs))

    def _run_callable(self, fn):
        try:
            fn()
        except Exception:
            pass

    # ── visibility and the frame loop ──
    def _visual(self):
        s = self.overlay_state
        if s == DONE:
            return "error" if self._done_error else "done"
        if s == RECORDING:
            return "rec"
        if s == PROCESSING:
            return "processing"
        return "transcribing"

    def _appear(self):
        self._visible = True
        if not self.isVisible():
            self.reposition()
            self._motion = glass.animations_enabled()
            self._solid = not glass.transparency_enabled()
            # _show starts at 0 even without motion (the loop snaps it to 1):
            # the window draws nothing until the first backdrop sample lands,
            # so that sample can't contain the HUD.
            self._show, self._show_v = 0.0, 0.0
            self._cur_vis, self._from_vis, self._xfade = self._visual(), None, 1.0
            self._vis_at = time.monotonic()
            self._pw, self._ph = self._target_size()
            self._pw_v = self._ph_v = 0.0
            self._last_box = None
            self._hover = None
            self._press, self._dragging, self._glide = None, False, None
            self.show()
            self.raise_()
            self._start_glass()
        if not self.timer.isActive():
            self._last_frame = time.monotonic()
            self.timer.start(FRAME_MS)
        self._kick()

    def _kick(self):
        """Something changed: animate at full rate until it settles."""
        if self.timer.isActive() and self.timer.interval() != FRAME_MS:
            self.timer.setInterval(FRAME_MS)

    def _loop(self):
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._last_frame))
        self._last_frame = now

        self._update_hover()
        self._follow_mouse()
        if self._hide_at and (self._hover or self._press is not None):
            # Don't vanish from under the cursor.
            self._hide_at = max(self._hide_at, now + HOLD_S)
        if self._hide_at and now >= self._hide_at:
            self._hide_at = None
            self._visible = False

        vis = self._visual()
        if vis != self._cur_vis:
            self._from_vis, self._cur_vis = self._cur_vis, vis
            self._xfade = 0.0
            self._vis_at = now
        self._xfade = min(1.0, self._xfade + dt / 0.32)

        if self._visible:
            if self._waiting and now - self._shown_at < FIRST_SAMPLE_S:
                pass                      # invisible until the glass knows what is behind it
            else:
                if self._waiting:         # no sample in time: go with the provisional theme
                    self._waiting = False
                    if not self._live and self._sampler is not None:
                        self._sampler.active = False
                if self._motion:
                    self._show, self._show_v = _spring(self._show, self._show_v, 1.0, dt,
                                                       300.0, 24.0)
                else:
                    self._show = 1.0
        else:
            self._show_v = 0.0
            self._show = max(0.0, self._show - dt / (0.16 if self._motion else 0.1))
            if self._show <= 0.0:
                self._finish_hide()
                return

        self._glide_step(dt)
        tw, th = self._target_size()
        if self._motion:
            self._pw, self._pw_v = _spring(self._pw, self._pw_v, tw, dt, 380.0, 30.0)
            self._ph, self._ph_v = _spring(self._ph, self._ph_v, th, dt, 380.0, 30.0)
        else:
            self._pw, self._ph = tw, th

        moving = (not self._visible or self._xfade < 1.0 or now - self._vis_at < 0.5
                  or self._press is not None or self._glide is not None
                  or abs(self._show - 1.0) > 0.002 or abs(self._show_v) > 0.02
                  or abs(self._pw - tw) > 0.5 or abs(self._ph - th) > 0.5
                  or abs(self._pw_v) > 2.0 or abs(self._ph_v) > 2.0)
        still = not moving and vis in ("done", "error")
        interval = FRAME_MS if moving else (IDLE_MS if still else STEADY_MS)
        if self.timer.interval() != interval:
            self.timer.setInterval(interval)
        if still:
            return                        # nothing on screen changes until it hides

        # Loudness: fast attack, slower release; falls to rest if the feed stalls.
        if now - self._level_at > 0.3:
            self._level_target = 0.0
        rate = 22.0 if self._level_target > self._level else 7.0
        self._level += (self._level_target - self._level) * (1.0 - math.exp(-dt * rate))
        self._update_bars(now, dt)
        self._repaint_capsule()

    def _repaint_capsule(self):
        """Repaint only what changed: the capsule's box, plus last frame's
        while it shrinks."""
        box = self._glass_box(self._capsule(float(round(self._pw)), float(round(self._ph))))
        box = box.toAlignedRect().adjusted(-1, -1, 1, 1)
        self.update(box.united(self._last_box) if self._last_box is not None else box)
        self._last_box = box

    def _finish_hide(self):
        self.timer.stop()
        if self._press is not None:
            self._end_press()
        self._glide = None
        self.hide()
        self._hover = None
        self._stop_glass()

    def _update_bars(self, now, dt):
        lv = self._level
        for i in range(N_BARS):
            osc = 0.5 + 0.5 * math.sin(now * _BAR_FREQ[i] + _BAR_PHASE[i])
            target = lv * _BAR_ENV[i] * (0.42 + 0.38 * osc + 0.3 * self._tex[i])
            if self._motion:
                # Silence: a slow ripple, so the bars never look frozen.
                target = max(target, 0.08 * _BAR_ENV[i] * (0.5 + 0.5 * math.sin(now * 2.6 - i * 0.62)))
            target = min(1.0, target)
            cur = self._bars[i]
            rate = 26.0 if target > cur else 9.0
            self._bars[i] = cur + (target - cur) * (1.0 - math.exp(-dt * rate))

    # ── moving it ──
    def _capsule_now(self):
        return self._capsule(float(round(self._pw)), float(round(self._ph)))

    def _update_hover(self):
        """Only the capsule takes the mouse; everywhere else in this window
        clicks fall through to the app underneath."""
        over = self._press is not None
        if not over and self._visible and self._show > 0.3:
            try:
                over = self._capsule_now().contains(QPointF(self.mapFromGlobal(QCursor.pos())))
            except Exception:
                over = False
        if over != self._hover:
            self._hover = over
            if self._hwnd:
                glass.set_click_through(self, not over)
            self.setCursor(Qt.OpenHandCursor if over else Qt.ArrowCursor)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self._capsule_now().contains(e.position()):
            e.ignore()
            return
        self._press = (e.globalPosition().toPoint(), self.pos())
        self._press_mouse = self._from_mouse(e)
        self._dragging = False
        self._glide = None
        self.setCursor(Qt.ClosedHandCursor)
        self._kick()
        e.accept()

    def mouseMoveEvent(self, e):
        if self._press is not None:
            self._drag_to(e.globalPosition().toPoint())

    def mouseReleaseEvent(self, e):
        if self._press is not None and e.button() == Qt.LeftButton:
            self._drag_to(e.globalPosition().toPoint())
            self._end_press()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and self._capsule_now().contains(e.position()):
            self._press = None
            self._go_home()

    @staticmethod
    def _from_mouse(e):
        try:
            dev = e.pointingDevice()
            return dev is None or dev.type() == QInputDevice.DeviceType.Mouse
        except Exception:
            return True

    def _follow_mouse(self):
        """A window that isn't active only gets mouse moves while the cursor
        is over it, so the drag also follows the cursor from the frame loop -
        and, for a real mouse, notices a release that happened outside the
        window (touch and pen don't show up as a held mouse button)."""
        if self._press is None or not self._hwnd:
            return
        if self._press_mouse and not glass.primary_button_down():
            self._end_press()
        elif self._dragging:
            self._drag_to(QCursor.pos())

    def _drag_to(self, gpos):
        p0, w0 = self._press
        delta = gpos - p0
        if not self._dragging:
            if delta.manhattanLength() < DRAG_SLOP:
                return
            self._dragging = True
            self._sample_fast(True)
        self.move(self._clamped(w0 + delta))

    def _end_press(self):
        if self._dragging:
            self._remember(self.pos())
            self._sample_fast(False)
        self._press, self._dragging = None, False
        self.setCursor(Qt.OpenHandCursor if self._hover else Qt.ArrowCursor)

    def _clamped(self, pos):
        """Keep the capsule on the screen under the cursor."""
        scr = QApplication.screenAt(QCursor.pos()) or self._screen or QApplication.primaryScreen()
        if scr is None:
            return pos
        self._screen = scr
        cap = self._capsule_now()
        a = QRectF(scr.availableGeometry())
        x = min(max(float(pos.x()), a.left() - cap.left()), a.right() - cap.right())
        y = min(max(float(pos.y()), a.top() - cap.top()), a.bottom() - cap.bottom())
        return QPoint(int(round(x)), int(round(y)))

    def _go_home(self):
        """Double-click: forget the dragged position and glide back."""
        self._remember(None)
        scr = QApplication.screenAt(self.geometry().center()) or self._target_screen()
        if scr is None:
            return
        self._screen = scr
        self._glide = self._home(scr)
        self._sample_fast(True)
        self._kick()

    def _glide_step(self, dt):
        if self._glide is None or self._press is not None:
            return
        cur, dst = self.pos(), self._glide
        dx, dy = dst.x() - cur.x(), dst.y() - cur.y()
        if abs(dx) <= 1 and abs(dy) <= 1:
            self.move(dst)
            self._glide = None
            self._sample_fast(False)
            return
        k = 1.0 - math.exp(-dt * (16.0 if self._motion else 1e6))

        def step(d):
            # Eased, but never a zero step short of the target (rounding
            # would otherwise park it a couple of pixels away).
            s = int(round(d * k))
            return s if s or not d else (1 if d > 0 else -1)

        self.move(QPoint(cur.x() + step(dx), cur.y() + step(dy)))

    def _remember(self, pos):
        if not self.app:
            return
        self.app.cfg["overlay_pos"] = [pos.x(), pos.y()] if pos is not None else None
        try:
            self.app.save_config()
        except Exception as e:
            logger.debug("overlay position not saved: %s", e)

    # ── sizes and copy ──
    def _hint_phase(self):
        """True while the Enter/Esc keycaps show (first dictations of a run)."""
        return self._runs <= HINT_RUNS and time.monotonic() - self._rec_started < HINT_S

    def _busy_label(self, vis):
        slow = time.monotonic() - self._vis_at >= SLOW_S and self._cur_vis == vis
        if vis == "processing":
            return "Thinking", ("still working…" if slow else "")
        cfg = self.app.cfg if self.app else {}
        return "Transcribing", ("still working…" if slow else
                                (self._lang or _transcription_ai_label(cfg)))

    def _partial_line(self):
        return " ".join(self._partial.split())

    def _target_size(self):
        vis = self._visual()
        if vis == "rec":
            return float(REC_W_HINT if self._hint_phase() else REC_W), float(PILL_H)
        if vis in ("transcribing", "processing"):
            line = self._partial_line() if vis == "transcribing" else ""
            if line:
                w = self._measure(self._f_body, line)
            else:
                label, suffix = self._busy_label(vis)
                w = self._measure(self._f_title, label)
                if suffix:
                    w += self._measure(self._f_small, "  ·  " + suffix)
            return float(max(164.0, min(PILL_MAX_W, TEXT_X + w + 18))), float(PILL_H)
        if vis == "done":
            w = self._measure(self._f_title, self._done_msg)
            return float(max(150.0, TEXT_X + w + 20)), float(PILL_H)
        title, detail = self._error_lines()
        tw = self._measure(self._f_title, title)
        hw = self._measure(self._f_small, detail)
        return float(max(220.0, min(PILL_MAX_W, TEXT_X + 2 + max(tw, hw) + 20))), float(PILL_H_ERROR)

    def _error_lines(self):
        """(title, detail): the message plus a contextual hint when it fits on
        one line; a long message is split in two instead (at its own " - " /
        ": " when it has one) so the detail line never repeats it."""
        msg = " ".join((self._done_msg or "").split())
        if self._err_split and self._err_split[0] == msg:
            return self._err_split[1]
        self._err_split = (msg, self._split_error(msg))
        return self._err_split[1]

    def _split_error(self, msg):
        fm = QFontMetricsF(self._f_title)
        room = PILL_MAX_W - TEXT_X - 22
        if fm.horizontalAdvance(msg) <= room:
            return msg, _error_hint(msg)
        for sep in (" - ", " — ", ": "):
            head, _, tail = msg.partition(sep)
            if tail and fm.horizontalAdvance(head) <= room:
                return head, tail[:1].upper() + tail[1:]
        words, line = msg.split(), ""
        for i, word in enumerate(words):
            if line and fm.horizontalAdvance(line + " " + word) > room:
                return line, " ".join(words[i:])
            line = (line + " " + word).strip()
        return msg, _error_hint(msg)

    # ── glass ──
    def _start_glass(self):
        """Right after show(): out of screen capture (per "overlay_private"),
        then the worker's first sample - which picks the theme - and live
        sampling. Nothing here blocks: show_overlay() runs just before the
        recorder starts. Until the first sample lands the window draws
        nothing, so that sample can't contain the HUD either way."""
        self._glass_gen += 1
        self._bd, self._bd_luma = None, None
        self._glass_cache.clear()
        self._excluded = False
        if self._hwnd:
            glass.set_no_activate(self)       # Qt may rebuild styles on show
            private = bool((self.app.cfg if self.app else {}).get("overlay_private", True))
            if private and glass.capture_exclusion_supported() and not glass.is_remote_session():
                self._excluded = glass.exclude_from_capture(self, True)
            else:
                glass.exclude_from_capture(self, False)
        self._live = self._excluded and not self._solid
        self._theme = self._pick_theme()      # provisional: pinned, or the system theme
        self._shown_at = time.monotonic()
        # Even without live glass one sample is taken, for the "auto" theme.
        self._waiting = bool(self._hwnd) and not self._solid
        if self._waiting:
            if self._sampler is None:
                self._sampler = _Sampler(self)
                self._sampler.start()
                qapp = QApplication.instance()
                if qapp is not None:
                    qapp.aboutToQuit.connect(self._stop_glass)
            self._sampler.fast = False
            self._sampler.active = True
            self._sampler.wake.set()

    def _stop_glass(self):
        if self._sampler is not None:
            self._sampler.active = False
            self._sampler.fast = False
        self._waiting = False
        self._live = False
        self._bd = None
        self._glass_cache.clear()

    def _sample_fast(self, on):
        if self._sampler is not None and self._live:
            self._sampler.fast = bool(on)
            self._sampler.wake.set()

    def _grab(self):
        """What is behind the HUD right now: (blurred sample, physical
        origin, device pixel ratio, brightness under the capsule), or None.
        Any thread."""
        wr = glass.window_rect(self._hwnd)
        if not wr:
            return None
        l, t, r, b = wr
        if r <= l or b <= t:
            return None
        d = (r - l) / float(WIN_W)
        m = int(round(SAMPLE_MARGIN * d))
        x, y, w, h = l - m, t - m, (r - l) + 2 * m, (b - t) + 2 * m
        raw = glass.grab_screen(x, y, w, h)
        if raw is None:
            return None
        img = QImage(raw, w, h, w * 4, QImage.Format_RGB32).copy()
        body = _blur(img, 10 * d, w, h)
        body.setDevicePixelRatio(d)
        return body, (x, y), d, _luma_under(body, m / d)

    def _on_backdrop(self, sample):
        gen, sample = sample[0], sample[1:]
        if gen != self._glass_gen or not self.isVisible():
            return                            # from an earlier appearance
        if self._waiting:
            self._waiting = False
            self._bd_luma = sample[3]
            self._theme = self._pick_theme()
            if not self._live:
                self._sampler.active = False  # that was the one sample for the theme
        if not self._live:
            return
        self._bd, self._bd_luma = sample[:3], sample[3]
        self._bd_seq += 1
        if self._last_box is not None and self._press is None and self._glide is None:
            self.update(self._last_box)       # moving: the frame loop repaints anyway

    def _backdrop_origin(self):
        """Where the latest sample's top-left sits in window-local logical
        px - so the glass stays pinned to the world while the window moves
        and the next sample is still on its way."""
        _, (sx, sy), d = self._bd
        wr = glass.window_rect(self._hwnd)
        if not wr:
            return QPointF(-SAMPLE_MARGIN, -SAMPLE_MARGIN)
        return QPointF((sx - wr[0]) / d, (sy - wr[1]) / d)

    def _pick_theme(self):
        """"dark" / "light" pin the glass; anything else is "auto" (the
        default): light over bright content, dark over dark - or the system
        theme when there is no sample (macOS, transparency effects off)."""
        pref = str((self.app.cfg if self.app else {}).get("overlay_theme", "auto")).lower()
        if pref in _PALETTES:
            return pref
        if self._bd_luma is not None:
            return "light" if self._bd_luma > AUTO_LIGHT_LUMA else "dark"
        return "light" if _system_prefers_light() else "dark"

    def _image(self, w, h):
        """A transparent image covering w x h logical px at device resolution."""
        d = self._dpr
        img = QImage(max(1, int(round(w * d))), max(1, int(round(h * d))),
                     QImage.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(d)
        img.fill(Qt.transparent)
        return img

    def _blit(self, p, x, y, img):
        """Draw a device-resolution image on the device-pixel grid: a plain
        copy, no resampling, crisp text."""
        d = self._dpr
        p.drawImage(QPointF(round(x * d) / d, round(y * d) / d), img)

    def _capsule(self, w, h):
        """The unscaled capsule rect: horizontally centred, bottom anchored."""
        cx, cy = WIN_W / 2.0, WIN_H - BOTTOM_PAD - h / 2.0
        return QRectF(cx - w / 2.0, cy - h / 2.0, w, h)

    def _glass_box(self, rect):
        """The capsule plus room for its shadow, snapped to the pixel grid."""
        d = self._dpr
        x0 = math.floor((rect.left() - 16) * d) / d
        y0 = math.floor((rect.top() - 12) * d) / d
        x1 = math.ceil((rect.right() + 16) * d) / d
        y1 = math.ceil(min(WIN_H, rect.bottom() + BOTTOM_PAD) * d) / d
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def _glass(self, w, h, pal, origin):
        """The glass - shadow, blurred backdrop, tint, a crisp edge - composed
        once per capsule size and backdrop sample into one device-pixel image,
        so a steady frame costs a single copy."""
        d = self._dpr
        luma = self._bd_luma if self._bd_luma is not None else 0.5
        key = (w, h, self._theme, d, self._solid,
               None if origin is None else (self._bd_seq, round(origin.x() * d), round(origin.y() * d)),
               round(luma * 40))
        hit = self._glass_cache.get(key)
        if hit is not None:
            return hit
        if len(self._glass_cache) > 24:
            self._glass_cache.clear()
        rect = self._capsule(w, h)
        r = h / 2.0
        box = self._glass_box(rect)
        img = self._image(box.width(), box.height())
        q = QPainter(img)
        q.setRenderHint(QPainter.Antialiasing, True)
        q.setRenderHint(QPainter.SmoothPixmapTransform, True)
        q.translate(-box.left(), -box.top())
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)

        # Soft drop shadow: a tiny silhouette, scaled up (bilinear = blur).
        f = 6.0
        sm = QImage(max(1, int(box.width() / f)), max(1, int(box.height() / f)),
                    QImage.Format_ARGB32_Premultiplied)
        sm.fill(Qt.transparent)
        sp = QPainter(sm)
        sp.setRenderHint(QPainter.Antialiasing, True)
        sp.scale(sm.width() / box.width(), sm.height() / box.height())
        sp.translate(-box.left(), -box.top())
        sp.setPen(Qt.NoPen)
        sp.setBrush(pal["shadow"])
        sp.drawRoundedRect(rect.adjusted(4, 5, -4, 5), r, r)
        sp.end()
        q.drawImage(box, sm)

        q.save()
        q.setClipPath(path)
        if self._solid:
            # Transparency effects off: the Windows solid fallback colour.
            q.fillPath(path, QColor(*pal["solid"]))
        else:
            if origin is not None:
                body = self._bd[0]
                q.drawImage(origin, body)
                lo, hi = pal["alpha"]
                alpha = lo + (hi - lo) * _smoothstep(0.08, 0.9, luma)
            else:
                alpha = pal["plate"]
            tint = pal["glass"]
            q.fillPath(path, QColor(tint[0], tint[1], tint[2], int(alpha * 255)))
        q.restore()
        # One crisp device pixel of edge: a highlight inside (brighter at the
        # top, where the light comes from) and a hairline outside it.
        top_a, bottom_a = pal["edge"]
        edge = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        edge.setColorAt(0.0, QColor(255, 255, 255, top_a))
        edge.setColorAt(1.0, QColor(255, 255, 255, bottom_a))
        px = 1.0 / d
        inner = QPainterPath()
        inner.addRoundedRect(rect.adjusted(px * 1.5, px * 1.5, -px * 1.5, -px * 1.5),
                             r - px * 1.5, r - px * 1.5)
        q.setBrush(Qt.NoBrush)
        q.setPen(QPen(QBrush(edge), px))
        q.drawPath(inner)
        outer = QPainterPath()
        outer.addRoundedRect(rect.adjusted(px * 0.5, px * 0.5, -px * 0.5, -px * 0.5),
                             r - px * 0.5, r - px * 0.5)
        q.setPen(QPen(pal["hairline"], px))
        q.drawPath(outer)
        q.end()
        hit = (img, box)
        self._glass_cache[key] = hit
        return hit

    # ── cached content pieces ──
    def _sprite(self, key, w, h, draw):
        """A small static drawing, rendered once at device resolution."""
        key = (key, self._theme, self._dpr)
        img = self._sprites.get(key)
        if img is None:
            if len(self._sprites) > 96:
                self._sprites.clear()
            img = self._image(w, h)
            sp = QPainter(img)
            sp.setRenderHint(QPainter.Antialiasing, True)
            sp.setRenderHint(QPainter.TextAntialiasing, True)
            draw(sp)
            sp.end()
            self._sprites[key] = img
        return img

    def _text(self, text, font, color, w, h, align=Qt.AlignLeft | Qt.AlignVCenter, fade=False):
        """Text as a cached sprite; ``fade`` feathers its left edge in."""
        def draw(sp):
            sp.setFont(font)
            if fade:
                g = QLinearGradient(QPointF(0, 0), QPointF(28.0, 0))
                faint = QColor(color)
                faint.setAlpha(40)
                g.setColorAt(0.0, faint)
                g.setColorAt(1.0, color)
                sp.setPen(QPen(QBrush(g), 1.0))
            else:
                sp.setPen(color)
            sp.drawText(QRectF(0, 0, w, h), align, text)
        key = ("text", text, id(font), color.rgba(), round(w), round(h), align, fade)
        return self._sprite(key, w, h, draw)

    def _measure(self, font, text):
        key = (id(font), text)
        w = self._widths.get(key)
        if w is None:
            if len(self._widths) > 256:
                self._widths.clear()
            w = self._widths[key] = QFontMetricsF(font).horizontalAdvance(text)
        return w

    # ── painting ──
    def paintEvent(self, event):
        if self._show <= 0.0:
            return
        dpr = self.devicePixelRatioF()
        if dpr != self._dpr:
            self._dpr = dpr
            self._glass_cache.clear()
            self._sprites.clear()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        pal = _PALETTES[self._theme]

        # Whole pixels: the glass is cached per size, and sub-pixel steps at
        # the end of a spring are invisible anyway.
        w, h = float(round(self._pw)), float(round(self._ph))
        rect = self._capsule(w, h)
        scale = (0.84 + 0.16 * self._show) if self._motion else 1.0
        scaled = abs(scale - 1.0) > 0.002
        p.setOpacity(max(0.0, min(1.0, self._show)))
        if scaled:
            c = rect.center()
            p.translate(c)
            p.scale(scale, scale)
            p.translate(-c)

        origin = self._backdrop_origin() if (self._live and self._bd is not None) else None
        img, box = self._glass(w, h, pal, origin)
        if scaled:
            p.drawImage(box, img)
        else:
            self._blit(p, box.left(), box.top(), img)

        # Clip the content to the capsule only while something is moving -
        # at rest everything sits well inside it.
        tw, th = self._target_size()
        if scaled or self._xfade < 1.0 or abs(self._pw - tw) > 0.5 or abs(self._ph - th) > 0.5:
            clip = QPainterPath()
            clip.addRoundedRect(rect, h / 2.0, h / 2.0)
            p.setClipPath(clip)
        now = time.monotonic()
        op = p.opacity()
        # Text is fitted to each state's FINAL width: while the capsule
        # springs, it reveals the words instead of re-cutting them per frame.
        self._fit_w[self._cur_vis] = tw
        # Sequential crossfade - the old content leaves before the new one
        # arrives, so the two never pile up mid-morph.
        if self._from_vis and self._xfade < 0.45:
            p.setOpacity(op * (1.0 - _smoothstep(0.0, 0.45, self._xfade)))
            self._paint_content(p, self._from_vis, rect, pal, now, self._fit_w.get(self._from_vis, w))
        p.setOpacity(op * (_smoothstep(0.3, 1.0, self._xfade) if self._from_vis else 1.0))
        self._paint_content(p, self._cur_vis, rect, pal, now, tw)
        p.end()

    def _paint_content(self, p, vis, rect, pal, now, fit_w):
        if vis == "rec":
            self._paint_recording(p, rect, pal, now)
        elif vis in ("transcribing", "processing"):
            self._paint_busy(p, rect, pal, now, vis, fit_w)
        elif vis == "done":
            self._paint_done(p, rect, pal, now, fit_w)
        else:
            self._paint_error(p, rect, pal, now, fit_w)

    def _paint_rec_dot(self, p, c, pal, now):
        red = QColor(pal["rec"])
        lv = self._level
        op = p.opacity()

        def draw_glow(sp):
            g = QRadialGradient(QPointF(16, 16), 16)
            g0, g1 = QColor(red), QColor(red)
            g0.setAlpha(185)
            g1.setAlpha(0)
            g.setColorAt(0.0, g0)
            g.setColorAt(1.0, g1)
            sp.setPen(Qt.NoPen)
            sp.setBrush(QBrush(g))
            sp.drawEllipse(QPointF(16, 16), 16, 16)

        # Glow that swells with the voice.
        gr = 8.0 + 7.0 * lv
        p.setOpacity(op * (95 + 90 * lv) / 185.0)
        p.drawImage(QRectF(c.x() - gr, c.y() - gr, 2 * gr, 2 * gr),
                    self._sprite(("glow", pal["rec"]), 32, 32, draw_glow))
        p.setOpacity(op)
        # Sonar ring: a steady heartbeat, so "live" reads even in silence.
        if self._motion:
            ph = ((now - self._rec_started) % SONAR_S) / SONAR_S
            ease = 1.0 - (1.0 - ph) ** 3
            ring = QColor(red)
            ring.setAlpha(int(160 * (1.0 - ph) ** 1.6))
            p.setPen(QPen(ring, 1.4))
            p.setBrush(Qt.NoBrush)
            rr = 5.5 + 8.5 * ease
            p.drawEllipse(c, rr, rr)

        def draw_core(sp):
            core = QRadialGradient(QPointF(4.5, 4.2), 10.0)
            core.setColorAt(0.0, red.lighter(140))
            core.setColorAt(1.0, red)
            sp.setPen(Qt.NoPen)
            sp.setBrush(QBrush(core))
            sp.drawEllipse(QPointF(6, 6), 6, 6)

        # Core, lit from the top-left.
        cr = 4.8 + 0.9 * lv
        p.drawImage(QRectF(c.x() - cr, c.y() - cr, 2 * cr, 2 * cr),
                    self._sprite(("core", pal["rec"]), 12, 12, draw_core))

    def _keycaps(self, pal):
        """Enter and Esc keycaps, 51 x 17."""
        def draw(sp):
            h = 17.0
            fill = _ink(pal, pal["key_fill"])
            line = QPen(_ink(pal, pal["key_line"]), 1.0)
            glyph = _ink(pal, 215)
            ent = QRectF(0.5, 0.5, 19.0, h - 1.0)
            esc = QRectF(25.5, 0.5, 25.0, h - 1.0)
            for cap in (ent, esc):
                sp.setPen(line)
                sp.setBrush(fill)
                sp.drawRoundedRect(cap, 5.0, 5.0)
            sp.setFont(self._f_key)
            sp.setPen(glyph)
            sp.drawText(esc.adjusted(0, -1, 0, 0), Qt.AlignCenter, "esc")
            # Return-arrow glyph, drawn so no font fallback can turn it into a box.
            cy = h / 2.0
            x1, x0, y0, y1 = ent.right() - 5.5, ent.left() + 5.5, cy - 3.5, cy + 2.0
            arrow = QPainterPath(QPointF(x1, y0))
            arrow.lineTo(x1, y1)
            arrow.lineTo(x0, y1)
            arrow.moveTo(x0 + 2.8, y1 - 2.8)
            arrow.lineTo(x0, y1)
            arrow.lineTo(x0 + 2.8, y1 + 2.8)
            sp.setPen(QPen(glyph, 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            sp.setBrush(Qt.NoBrush)
            sp.drawPath(arrow)
        return self._sprite(("keys",), 51, 17, draw)

    def _paint_recording(self, p, rect, pal, now):
        cy = rect.center().y()
        self._paint_rec_dot(p, QPointF(rect.left() + ICON_X, cy), pal, now)

        x0 = rect.left() + TEXT_X
        max_h = rect.height() - 18.0
        inks = self._bar_inks.get(self._theme)
        if inks is None:
            # Louder bars are brighter: 12 steps from 55 % to full ink.
            inks = self._bar_inks[self._theme] = [
                QBrush(_ink(pal, 255 * (0.55 + 0.45 * k / 11))) for k in range(12)]
        p.setPen(Qt.NoPen)
        for i, v in enumerate(self._bars):
            bh = 3.0 + v * (max_h - 3.0)
            p.setBrush(inks[int(min(1.0, v * 1.8) * 11 + 0.5)])
            p.drawRoundedRect(QRectF(x0 + i * (BAR_W + BAR_GAP), cy - bh / 2.0, BAR_W, bh),
                              BAR_W / 2.0, BAR_W / 2.0)

        # Right zone: the keycaps first (early dictations), then the timer.
        # The keycaps are gone before the capsule settles narrower at HINT_S,
        # and the timer arrives once it has.
        zone_r = rect.right() - 14.0
        elapsed = now - self._rec_started
        keys, clock = 0.0, 1.0
        if self._runs <= HINT_RUNS:
            keys = 1.0 - _smoothstep(HINT_S - 0.3, HINT_S, elapsed)
            clock = _smoothstep(HINT_S + 0.12, HINT_S + 0.42, elapsed)
        op = p.opacity()
        if keys > 0.0:
            p.setOpacity(op * keys)
            self._blit(p, zone_r - 51.0, cy - 8.5, self._keycaps(pal))
        if clock > 0.0:
            p.setOpacity(op * clock)
            m, s = divmod(int(elapsed), 60)
            self._blit(p, zone_r - 51.0, cy - 10.0,
                       self._text("%d:%02d" % (m, s), self._f_timer, _ink(pal, 220), 51, 20,
                                  Qt.AlignRight | Qt.AlignVCenter))
        p.setOpacity(op)

    def _paint_busy(self, p, rect, pal, now, vis, fit_w):
        cy = rect.center().y()
        c = QPointF(rect.left() + ICON_X, cy)
        # Spinner: a faint track and a comet arc with a fading tail, in ink.
        p.setPen(QPen(_ink(pal, 40), 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, 7.5, 7.5)
        head = -(now * 380.0) % 360.0
        comet = QConicalGradient(c, head)
        comet.setColorAt(0.0, _ink(pal, 240))
        comet.setColorAt(0.72, _ink(pal, 0))
        comet.setColorAt(1.0, _ink(pal, 0))
        p.setPen(QPen(QBrush(comet), 2.2, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(c.x() - 7.5, c.y() - 7.5, 15.0, 15.0), int(head * 16), int(260 * 16))

        x = rect.left() + TEXT_X
        avail = max(0.0, fit_w - TEXT_X - 16.0)
        line = self._partial_line() if vis == "transcribing" else ""
        if line:
            # Live words: the newest tail, feathered in from the left once cut.
            cut = self._measure(self._f_body, line) > avail
            if cut:
                fm = QFontMetricsF(self._f_body)
                while line and fm.horizontalAdvance("… " + line) > avail:
                    line = line[1:]
                line = "… " + line.lstrip()
            self._blit(p, x, rect.top(), self._text(line, self._f_body, _ink(pal, 235), avail,
                                                    rect.height(), fade=cut))
            return

        label, suffix = self._busy_label(vis)
        lw = self._measure(self._f_title, label)
        # Shimmer: a highlight sweeping across the label while we wait.
        ph = ((now - self._vis_at) % 1.8) / 1.8
        bx = x - 30.0 + (lw + 60.0) * ph
        shimmer = QLinearGradient(QPointF(bx - 30.0, 0), QPointF(bx + 30.0, 0))
        shimmer.setColorAt(0.0, _ink(pal, 150))
        shimmer.setColorAt(0.5, _ink(pal, 255))
        shimmer.setColorAt(1.0, _ink(pal, 150))
        p.setFont(self._f_title)
        p.setPen(QPen(QBrush(shimmer), 1.0))
        p.drawText(QRectF(x, rect.top(), lw + 2.0, rect.height()), Qt.AlignLeft | Qt.AlignVCenter, label)
        if suffix:
            sw = max(0.0, avail - lw)
            text = "  ·  " + suffix
            if self._measure(self._f_small, text) > sw + 2.0:
                text = QFontMetricsF(self._f_small).elidedText(text, Qt.ElideRight, sw)
            self._blit(p, x + lw, rect.top(), self._text(text, self._f_small, _ink(pal, 135), sw + 4.0,
                                                         rect.height()))

    def _paint_done(self, p, rect, pal, now, fit_w):
        cy = rect.center().y()
        c = QPointF(rect.left() + ICON_X, cy)
        t = now - self._vis_at if self._cur_vis == "done" else 1.0
        # The badge pops in, then the check draws itself.
        pop = 1.0 - (1.0 - _smoothstep(0.0, 0.28, t)) ** 2 if self._motion else 1.0
        r = 9.0 * (0.55 + 0.45 * pop)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["ok"]))
        p.drawEllipse(c, r, r)
        prog = _smoothstep(0.1, 0.36, t) if self._motion else 1.0
        if prog > 0.0:
            a = QPointF(c.x() - 4.2, c.y() + 0.3)
            b = QPointF(c.x() - 1.3, c.y() + 3.2)
            e = QPointF(c.x() + 4.4, c.y() - 3.3)
            check = QPainterPath(a)
            check.lineTo(b)
            check.lineTo(e)
            p.setPen(QPen(QColor(255, 255, 255), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
            if prog >= 1.0:
                p.drawPath(check)
            else:
                part = QPainterPath(a)
                d = prog * check.length()
                if d > math.hypot(b.x() - a.x(), b.y() - a.y()):
                    part.lineTo(b)
                part.lineTo(check.pointAtPercent(check.percentAtLength(d)))
                p.drawPath(part)
        tw = fit_w - TEXT_X - 14.0
        self._blit(p, rect.left() + TEXT_X, rect.top(),
                   self._text(self._done_msg, self._f_title, _ink(pal, 240), tw, rect.height()))

    def _paint_error(self, p, rect, pal, now, fit_w):
        cy = rect.center().y()
        c = QPointF(rect.left() + ICON_X + 1.0, cy)

        def draw_badge(sp):
            sp.setPen(Qt.NoPen)
            sp.setBrush(QColor(pal["err"]))
            sp.drawEllipse(QPointF(10, 10), 9.5, 9.5)
            sp.setPen(QPen(QColor(255, 255, 255), 2.2, Qt.SolidLine, Qt.RoundCap))
            sp.drawLine(QPointF(10, 5.6), QPointF(10, 10.6))
            sp.setPen(Qt.NoPen)
            sp.setBrush(QColor(255, 255, 255))
            sp.drawEllipse(QPointF(10, 14.2), 1.25, 1.25)

        self._blit(p, c.x() - 10.0, cy - 10.0, self._sprite(("err-badge", pal["err"]), 20, 20, draw_badge))
        x = rect.left() + TEXT_X + 2.0
        w = max(0.0, fit_w - TEXT_X - 2.0 - 18.0)
        title, detail = self._error_lines()
        title = QFontMetricsF(self._f_title).elidedText(title, Qt.ElideRight, w)
        detail = QFontMetricsF(self._f_small).elidedText(detail, Qt.ElideRight, w)
        self._blit(p, x, cy - 18.0, self._text(title, self._f_title, _ink(pal, 245), w, 18.0))
        self._blit(p, x, cy + 1.0, self._text(detail, self._f_small, _ink(pal, 165), w, 16.0))
