"""Live Prompter: a private, liquid-glass copilot overlay for live calls.

What it is (product contract):
* Floats above Zoom/Teams/Meet/anything, always on top, draggable, with a
  compact pill state and an expanded card. Its own Listen button starts the
  meeting capture (system audio + mic) so the overlay is self-sufficient.
* Shows the live transcript tail and the rolling summary from the meeting
  pipeline, and - on demand (Say next / Follow-ups / Recap / a typed question)
  or automatically - an AI suggestion. With Screen on, a screenshot rides
  along so the AI can answer about what is on the user's screen.
* PRIVATE by default: excluded from screen capture (ui/glass.py) so it never
  appears on a shared screen or in a recording while staying visible on the
  user's own monitor. A privacy feature for the user's private notes - never
  marketed as a way to deceive anyone; recording-consent guidance applies.
* Liquid glass: because the window is excluded from capture, the pixels
  BEHIND it can be sampled (QScreen.grabWindow honours the exclusion) and
  rendered back through the shape - real blur, an edge refraction ring, a
  cursor-tracked specular highlight, rim light and a soft shadow. Where
  sampling isn't safe (exclusion unconfirmed, macOS, RDP) it falls back to
  the OS acrylic backdrop, then to a painted translucent plate.

Threading: suggestion requests run on a worker thread and report back via
signals; every widget touch happens on the GUI thread.
"""
import base64
import logging
import re
import threading
import time

from PySide6.QtCore import Qt, QBuffer, QIODevice, QPointF, QRectF, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QFont, QLinearGradient, QPainter, QPainterPath,
    QPen, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
)

import actions
from ui import glass

logger = logging.getLogger("transcribe")

SHADOW = 16                      # transparent margin around the card (drop shadow)
RADIUS = 22
CARD_W = 480
# Window = card + shadow margins. Width is shared by both states so
# collapse/expand never jumps sideways.
EXPANDED_W, EXPANDED_H = CARD_W + 2 * SHADOW, 560 + 2 * SHADOW
COMPACT_W, COMPACT_H = CARD_W + 2 * SHADOW, 52 + 2 * SHADOW
TAIL_CHARS = 2600                # ~3-4 minutes of speech fed to the model
AUTO_SUGGEST_EVERY_SEC = 25      # when Auto is on and new speech arrived
SAMPLE_MS_REST, SAMPLE_MS_DRAG = 150, 40

_THEMES = {
    "light": {
        "tint_liquid": QColor(255, 255, 255, 108),  # over the sampled backdrop
        "tint_blur": QColor(255, 255, 255, 150),    # over OS acrylic
        "tint_flat": QColor(255, 255, 255, 228),    # painted fallback
        "border": QColor(15, 23, 42, 40),
        "text": "#0f172a", "muted": "#475569", "faint": "#64748b",
        "card": "rgba(255,255,255,0.72)", "card_border": "rgba(15,23,42,0.10)",
        "accent": "#2563eb", "spec": 95,
    },
    "dark": {
        "tint_liquid": QColor(17, 24, 39, 128),
        "tint_blur": QColor(17, 24, 39, 150),
        "tint_flat": QColor(17, 24, 39, 232),
        "border": QColor(255, 255, 255, 46),
        "text": "#f8fafc", "muted": "#cbd5e1", "faint": "#94a3b8",
        "card": "rgba(17,24,39,0.62)", "card_border": "rgba(255,255,255,0.14)",
        "accent": "#60a5fa", "spec": 45,
    },
}

QUICK_ACTIONS = (
    ("Say next", ""),
    ("Follow-ups", "Give me 3 sharp follow-up questions I could ask right now, "
                   "one line each."),
    ("Recap", "Recap the last few minutes of the conversation in 3 short bullets."),
)


def clamp_to_rects(x, y, w, h, rects, margin=8):
    """Keep a saved window position reachable: if (x, y) isn't on any current
    screen (monitor unplugged, resolution changed) pull it onto the first one.
    ``rects`` are (left, top, right, bottom) tuples."""
    if not rects:
        return x, y
    for l, t, r, b in rects:
        if l <= x < r - 40 and t <= y < b - 40:
            return x, y
    l, t, r, b = rects[0]
    return (max(l + margin, min(x, r - w - margin)),
            max(t + margin, min(y, b - h - margin)))


def private_state(wanted, supported, remote, excluded):
    """(key, chip text) - always TRUTHFUL about what the OS actually did. The
    badge is the whole privacy promise; it must never say hidden when the
    window is in fact capturable (macOS, old Windows, RDP, API refusal)."""
    # Short on purpose: the chip shares the header with Listen/Stop and three
    # icon buttons; the full explanation lives in its tooltip.
    if not wanted:
        return "off", "VISIBLE in share"
    if remote:
        return "unavailable", "PRIVATE n/a · remote"
    if not supported:
        return "unavailable", "PRIVATE n/a here"
    if excluded:
        return "on", "PRIVATE · not in share"
    return "failed", "NOT PRIVATE · visible"


_LANG_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish",
               "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
               "hy": "Armenian", "tr": "Turkish", "zh": "Chinese", "ja": "Japanese"}


def rolling_context(live_text, question="", title="", attendees="",
                    tail_chars=TAIL_CHARS, screen=False, output_lang="en"):
    """The text handed to the model: recent conversation + optional question.
    Pure, so it is unit-testable. Cuts at a sentence boundary when it can so
    the model doesn't start mid-word."""
    tail = (live_text or "").strip()
    if len(tail) > tail_chars:
        tail = tail[-tail_chars:]
        cut = re.search(r"[.!?]\s+", tail)
        if cut and cut.end() < len(tail) // 2:
            tail = tail[cut.end():]
    parts = []
    if title or attendees:
        meta = []
        if title:
            meta.append(f"Meeting: {title}")
        if attendees:
            meta.append(f"Attendees: {attendees}")
        parts.append("\n".join(meta))
    parts.append("Conversation (latest part):\n" + (tail or "(nothing transcribed yet)"))
    if screen:
        parts.append("(A screenshot of the user's screen is attached - use it as context.)")
    if output_lang and output_lang != "auto":
        parts.append(f"(Respond in {_LANG_NAMES.get(output_lang, output_lang)}.)")
    if (question or "").strip():
        parts.append("User's question: " + question.strip())
    return "\n\n".join(parts)


def _looks_like_capture_hole(small_img, black_max=12, band_frac=0.14):
    """True when a horizontal band of the (downscaled) sample is pure black -
    the signature of a hardware video surface that BitBlt cannot read. A
    genuinely dark desktop is not a contiguous pure-black band."""
    try:
        w, h = small_img.width(), small_img.height()
        if w < 4 or h < 4:
            return False
        black_rows = 0
        for y in range(h):
            dark = 0
            for x in range(w):
                c = small_img.pixelColor(x, y)
                if c.red() <= black_max and c.green() <= black_max and c.blue() <= black_max:
                    dark += 1
            if dark >= w * 0.9:
                black_rows += 1
        return black_rows >= max(2, int(h * band_frac))
    except Exception:
        return False


_CODE_CSS = ("pre, code { font-family: 'Cascadia Mono', Consolas, 'Courier New', monospace; "
             "font-size: 12px; } pre { background: rgba(15,23,42,0.08); border-radius: 6px; "
             "padding: 6px; }")


def render_markdown(text_edit, md):
    """Show Markdown in a QTextEdit with monospace, boxed code blocks. Qt's
    setMarkdown has no styling hook, so the document is converted to HTML
    first and re-set with a default stylesheet for pre/code."""
    try:
        doc = text_edit.document()
        doc.setDefaultStyleSheet(_CODE_CSS)
        doc.setMarkdown(md or "")
        html = doc.toHtml()
        text_edit.setHtml(html)
    except Exception:
        text_edit.setPlainText(md or "")


def capture_screen_png_b64(screen, max_w=1280):
    """Screenshot of ``screen`` as base64 PNG, downscaled for upload. The
    overlay itself is absent when capture exclusion is active."""
    try:
        pm = screen.grabWindow(0)
        if pm.isNull():
            return ""
        if pm.width() > max_w:
            pm = pm.scaledToWidth(max_w, Qt.SmoothTransformation)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        pm.save(buf, "PNG")
        return base64.b64encode(bytes(buf.data())).decode("ascii")
    except Exception as e:
        logger.debug("screen capture failed: %s", e)
        return ""


class _IconButton(QPushButton):
    """Round glass button with a painted vector icon (theme / collapse /
    close). Text glyphs rendered inconsistently across fonts; a path is
    crisp at any size and follows the theme ink."""

    def __init__(self, kind, parent, tip=""):
        super().__init__(parent)
        self._kind = kind
        self._alt = False          # collapse: True when the card is collapsed
        self._hover = False
        self._theme = _THEMES["light"]
        self.setFixedSize(26, 26)
        self.setToolTip(tip)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("background: transparent; border: none;")

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def set_alt(self, alt):
        self._alt = bool(alt)
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        light = self._theme is _THEMES["light"]
        on = self.isCheckable() and self.isChecked()
        if on:
            fill = QColor(self._theme["accent"])
            border = fill
        else:
            fill = QColor(255, 255, 255, (92 if self._hover else 58) if light
                          else (46 if self._hover else 26))
            border = QColor(15, 23, 42, 34) if light else QColor(255, 255, 255, 50)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawEllipse(r)
        ink = QColor("#ffffff") if on else QColor(self._theme["text"])
        pen = QPen(ink, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        cx, cy = r.center().x(), r.center().y()
        if self._kind == "close":
            p.drawLine(QPointF(cx - 3.5, cy - 3.5), QPointF(cx + 3.5, cy + 3.5))
            p.drawLine(QPointF(cx + 3.5, cy - 3.5), QPointF(cx - 3.5, cy + 3.5))
        elif self._kind == "collapse":
            d = 2.2 if not self._alt else -2.2      # chevron up (collapse) / down (expand)
            path = QPainterPath(QPointF(cx - 4.2, cy + d))
            path.lineTo(QPointF(cx, cy - d))
            path.lineTo(QPointF(cx + 4.2, cy + d))
            p.drawPath(path)
        elif self._kind == "theme":
            circle = QRectF(cx - 4.6, cy - 4.6, 9.2, 9.2)
            p.drawEllipse(circle)
            p.setBrush(ink)
            p.setPen(Qt.NoPen)
            p.drawPie(circle, 90 * 16, 180 * 16)
        elif self._kind == "screen":
            # A monitor: rounded screen + stand.
            p.drawRoundedRect(QRectF(cx - 5.5, cy - 4.5, 11, 7.5), 1.6, 1.6)
            p.drawLine(QPointF(cx, cy + 3), QPointF(cx, cy + 5.2))
            p.drawLine(QPointF(cx - 3, cy + 5.2), QPointF(cx + 3, cy + 5.2))
        elif self._kind == "stop":
            # Red rounded square - the universal "stop recording".
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ef4444"))
            p.drawRoundedRect(QRectF(cx - 4.5, cy - 4.5, 9, 9), 2.2, 2.2)


class _DragBar(QFrame):
    """Header strip that drags the whole overlay."""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner
        self._press = None
        self.setCursor(Qt.OpenHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint() - self._owner.frameGeometry().topLeft()
            self.setCursor(Qt.ClosedHandCursor)
            self._owner._drag_began()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None:
            self._owner.move(e.globalPosition().toPoint() - self._press)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._press is not None:
            self._press = None
            self.setCursor(Qt.OpenHandCursor)
            self._owner._drag_ended()
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        self._owner.set_expanded(not self._owner._expanded)


class LiveAssistOverlay(QWidget):
    sig_suggestion = Signal(str, str)     # text, error
    sig_status = Signal(str)              # short footer note from the worker

    def __init__(self, main_app=None):
        super().__init__()
        self.app = main_app
        cfg = self.app.cfg if self.app else {}
        self._theme_name = cfg.get("live_assist_theme", "light")
        if self._theme_name not in _THEMES:
            self._theme_name = "light"
        self._private = bool(cfg.get("live_assist_private", True))
        self._auto = bool(cfg.get("live_assist_auto", False))
        self._blur_mode = ""
        self._glass_mode = "flat"          # "liquid" | "acrylic" | "flat"
        self._bd_body = None               # sampled backdrop, strong blur
        self._bd_ring = None               # sampled backdrop, mild blur (refraction)
        self._spec_pos = QPointF(SHADOW + 120, SHADOW + 8)
        self._expanded = True
        self._meeting_active = False
        self._live_text = ""
        self._summary = ""
        self._suggesting = False
        self._suggest_started = 0.0
        self._last_suggest_at = 0.0
        self._text_since_suggest = 0
        self._title = ""
        self._attendees = ""
        self._exclusion_ok = False

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # macOS hides Qt.Tool windows when the app loses focus - which is
        # exactly when the user is in Zoom. No-op elsewhere.
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowTitle("Live Prompter")
        self.setFixedSize(EXPANDED_W, EXPANDED_H)

        self.sig_suggestion.connect(self._on_suggestion)
        self.sig_status.connect(self._set_status)
        self._build()
        self._apply_theme()

        pos = cfg.get("live_assist_pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            rects = [(s.availableGeometry().left(), s.availableGeometry().top(),
                      s.availableGeometry().right(), s.availableGeometry().bottom())
                     for s in QApplication.screens()]
            x, y = clamp_to_rects(int(pos[0]), int(pos[1]), EXPANDED_W, EXPANDED_H, rects)
            self.move(x, y)
        else:
            self._default_position()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(1000)
        self._sample_timer = QTimer(self)
        self._sample_timer.timeout.connect(self._sample_backdrop)
        self._sample_timer.setInterval(SAMPLE_MS_REST)
        self._spec_timer = QTimer(self)
        self._spec_timer.timeout.connect(self._track_specular)
        self._spec_timer.setInterval(60)

    # ── build ──
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(SHADOW + 14, SHADOW + 10, SHADOW + 14, SHADOW + 12)
        root.setSpacing(8)

        self.bar = _DragBar(self)
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(4, 2, 0, 2)
        bl.setSpacing(8)
        self.lbl_title = QLabel("Live Prompter", self.bar)
        bl.addWidget(self.lbl_title)
        # Idle: a solid Start button. Live: a green timer + a red stop button.
        self.btn_start = QPushButton("Start", self.bar)
        self.btn_start.setObjectName("laStart")
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setToolTip("Start live transcription of this call (system audio + "
                                  "microphone).")
        self.btn_start.clicked.connect(self._start_listening)
        bl.addWidget(self.btn_start)
        self.lbl_timer = QLabel("● 00:00", self.bar)
        self.lbl_timer.setObjectName("laTimer")
        self.lbl_timer.hide()
        bl.addWidget(self.lbl_timer)
        self.btn_stop = _IconButton("stop", self.bar, "Stop listening and generate the notes")
        self.btn_stop.clicked.connect(self._stop_listening)
        self.btn_stop.hide()
        bl.addWidget(self.btn_stop)
        # Private toggle: eye-off + "Private" when hidden from screen share,
        # eye + "Visible" when not. Always truthful (see private_state).
        self.btn_private = QPushButton("", self.bar)
        self.btn_private.setObjectName("laPrivate")
        self.btn_private.setCursor(Qt.PointingHandCursor)
        self.btn_private.clicked.connect(lambda: self.set_private(not self._private))
        bl.addWidget(self.btn_private)
        bl.addStretch()
        self.btn_theme = _IconButton("theme", self.bar, "Light / dark glass")
        self.btn_theme.clicked.connect(self._toggle_theme)
        bl.addWidget(self.btn_theme)
        self.btn_collapse = _IconButton("collapse", self.bar,
                                        "Collapse to a pill (double-click the bar too)")
        self.btn_collapse.clicked.connect(lambda: self.set_expanded(not self._expanded))
        bl.addWidget(self.btn_collapse)
        self.btn_close = _IconButton("close", self.bar, "Hide (your hotkey brings it back)")
        self.btn_close.clicked.connect(self.hide_overlay)
        bl.addWidget(self.btn_close)
        root.addWidget(self.bar)

        self.body = QWidget(self)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        self.lbl_now_head = QLabel("NOW", self.body)
        body.addWidget(self.lbl_now_head)
        self.txt_now = QLabel("Press Listen and the last thing said in the call "
                              "will appear here.", self.body)
        self.txt_now.setWordWrap(True)
        self.txt_now.setObjectName("laNow")
        body.addWidget(self.txt_now)

        self.lbl_sum_head = QLabel("SO FAR", self.body)
        body.addWidget(self.lbl_sum_head)
        self.txt_summary = QTextEdit(self.body)
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setObjectName("laCard")
        self.txt_summary.setMaximumHeight(110)
        body.addWidget(self.txt_summary)

        head_row = QHBoxLayout()
        self.lbl_sug_head = QLabel("SUGGESTION · CHECK FACTS", self.body)
        self.lbl_sug_head.setToolTip("AI suggestions can be wrong - treat them as notes, "
                                     "not facts.")
        head_row.addWidget(self.lbl_sug_head)
        head_row.addStretch()
        # One-tap actions: each is a canned question through the same path.
        self.quick_buttons = []
        for label, question in QUICK_ACTIONS:
            b = QPushButton(label, self.body)
            b.setFixedHeight(22)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, q=question: self.suggest(q))
            head_row.addWidget(b)
            self.quick_buttons.append(b)
        self.btn_auto = QPushButton("Auto", self.body)
        self.btn_auto.setCheckable(True)
        self.btn_auto.setChecked(self._auto)
        self.btn_auto.setToolTip("Refresh the suggestion by itself every ~25 s while "
                                 "people talk (uses your notes AI engine).")
        self.btn_auto.toggled.connect(self._on_auto_toggled)
        self.btn_auto.setFixedHeight(22)
        head_row.addWidget(self.btn_auto)
        body.addLayout(head_row)
        self.txt_suggestion = QTextEdit(self.body)
        self.txt_suggestion.setReadOnly(True)
        self.txt_suggestion.setObjectName("laCard")
        self.txt_suggestion.setPlaceholderText(
            "Say next answers what was just asked of you; Follow-ups and Recap do "
            "what they say; or type a question below. Turn on Screen to include "
            "what's on your screen.")
        body.addWidget(self.txt_suggestion, 1)

        ask_row = QHBoxLayout()
        ask_row.setSpacing(8)
        # Screen context as an "attach" affordance at the left of the ask bar,
        # like a paperclip in a chat app: a monitor icon, accent-filled when on.
        self.btn_screen = _IconButton(
            "screen", self.body,
            "Attach a screenshot of your screen to the next suggestion (this overlay "
            "is left out of it). Needs a cloud AI engine - Transcribe Pro or your own key.")
        self.btn_screen.setCheckable(True)
        self.btn_screen.setFixedSize(30, 30)
        self.btn_screen.toggled.connect(lambda _on: self.btn_screen.update())
        ask_row.addWidget(self.btn_screen)
        self.input_ask = QLineEdit(self.body)
        self.input_ask.setPlaceholderText("Ask about the conversation or your screen…")
        self.input_ask.returnPressed.connect(self._ask)
        ask_row.addWidget(self.input_ask, 1)
        self.btn_suggest = QPushButton("Suggest", self.body)
        self.btn_suggest.setObjectName("laSuggest")
        self.btn_suggest.setCursor(Qt.PointingHandCursor)
        self.btn_suggest.clicked.connect(lambda: self.suggest(""))
        ask_row.addWidget(self.btn_suggest)
        body.addLayout(ask_row)

        self.lbl_status = QLabel("", self.body)
        self.lbl_status.setObjectName("laFoot")
        # Wrap: a long note must never widen the layout past the card.
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMaximumHeight(32)
        body.addWidget(self.lbl_status)

        root.addWidget(self.body, 1)
        cfg = self.app.cfg if self.app else {}
        try:
            self.setWindowOpacity(min(1.0, max(0.55, float(cfg.get("live_assist_opacity", 0.96)))))
        except (TypeError, ValueError):
            self.setWindowOpacity(0.96)

    # ── theme / glass ──
    def _apply_theme(self):
        t = _THEMES[self._theme_name]
        accent = t["accent"]
        self.setStyleSheet(f"""
            QLabel {{ color: {t['text']}; background: transparent; font-size: 12px; }}
            QLabel#laNow {{ font-size: 13px; color: {t['text']}; }}
            QTextEdit#laCard {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 10px; color: {t['text']}; font-size: 13.5px; padding: 6px;
            }}
            QLineEdit {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 9px; color: {t['text']}; padding: 6px 10px; font-size: 12.5px;
            }}
            QPushButton {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 11px; color: {t['text']}; padding: 2px 11px; font-size: 12px;
            }}
            QPushButton:hover {{ border-color: {accent}; }}
            QPushButton:checked {{ background: {accent}; color: white; border-color: {accent}; }}
            QPushButton#laSuggest {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #8b5cf6);
                color: white; font-weight: bold; border: none; padding: 7px 18px; border-radius: 15px;
            }}
            QPushButton#laSuggest:disabled {{ background: #94a3b8; }}
            QPushButton#laStart {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #6366f1);
                color: white; font-weight: 700; border: none; border-radius: 11px;
                padding: 3px 16px; font-size: 12px;
            }}
            QPushButton#laStart:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2563eb, stop:1 #4f46e5);
            }}
            QLabel#laTimer {{
                color: #15803d; background: rgba(34,197,94,0.16); border: 1px solid rgba(34,197,94,0.45);
                border-radius: 11px; padding: 2px 10px; font-size: 12px; font-weight: 700;
            }}
            QLabel#laFoot {{ color: {t['faint']}; font-size: 11px; }}
        """)
        for lbl in (self.lbl_now_head, self.lbl_sum_head, self.lbl_sug_head):
            lbl.setStyleSheet(f"color: {t['faint']}; font-size: 10px; font-weight: 700; "
                              "letter-spacing: 1px; background: transparent;")
        self.lbl_title.setStyleSheet(f"color: {t['text']}; font-weight: 700; font-size: 13px;")
        for b in (self.btn_theme, self.btn_collapse, self.btn_close, self.btn_screen,
                  self.btn_stop):
            b.set_theme(t)
        self._shadow_key = None          # shadow tint depends on theme
        self._refresh_private_chip()
        self._refresh_live_controls()
        self.update()

    def _toggle_theme(self):
        self._theme_name = "dark" if self._theme_name == "light" else "light"
        if self.app:
            self.app.cfg["live_assist_theme"] = self._theme_name
            self.app.save_config()
        self._apply_theme()
        self._apply_glass()

    def _apply_glass(self):
        """Capture exclusion first (it decides whether sampling the backdrop is
        safe), then the glass tier. Called after every show(): Qt can recreate
        the native window when flags change, and the effects live on the HWND."""
        if not self.isVisible():
            return
        self._apply_private()
        if glass.IS_WINDOWS and self._exclusion_ok:
            # Liquid: we paint the (blurred, refracted) backdrop ourselves from
            # a screen sample that cannot contain this window.
            if self._glass_mode != "liquid":
                glass.remove_backdrop_blur(self)
                self._blur_mode = ""
            self._glass_mode = "liquid"
            self._sample_backdrop()
            self._sample_timer.start()
            self._spec_timer.start()
        else:
            self._sample_timer.stop()
            self._spec_timer.stop()
            self._bd_body = self._bd_ring = None
            t = _THEMES[self._theme_name]
            tint = (t["tint_blur"].red(), t["tint_blur"].green(), t["tint_blur"].blue(), 0x20)
            self._blur_mode = glass.apply_backdrop_blur(self, tint)
            self._glass_mode = "acrylic" if self._blur_mode else "flat"
        self.update()

    def _apply_private(self):
        remote = glass.is_remote_session()
        supported = glass.capture_exclusion_supported()
        excluded = False
        if self._private and supported and not remote:
            excluded = glass.exclude_from_capture(self, True)
        else:
            glass.exclude_from_capture(self, False)
        self._exclusion_ok = excluded
        self._refresh_private_chip(remote, supported, excluded)

    def set_private(self, on):
        self._private = bool(on)
        if self.app:
            self.app.cfg["live_assist_private"] = self._private
            self.app.save_config()
        # Sampling is only safe while excluded, so the glass tier may change.
        self._apply_glass()

    _CHIP_COLORS = {
        "on": ("rgba(34,197,94,0.16)", "#15803d", "rgba(34,197,94,0.45)"),
        "off": ("rgba(239,68,68,0.14)", "#b91c1c", "rgba(239,68,68,0.45)"),
        "failed": ("rgba(239,68,68,0.14)", "#b91c1c", "rgba(239,68,68,0.45)"),
        "unavailable": ("rgba(245,158,11,0.16)", "#b45309", "rgba(245,158,11,0.5)"),
    }

    def _refresh_private_chip(self, remote=None, supported=None, excluded=None):
        if remote is None:
            remote = glass.is_remote_session()
        if supported is None:
            supported = glass.capture_exclusion_supported()
        if excluded is None:
            excluded = glass.is_excluded_from_capture(self) if self.isVisible() \
                else self._exclusion_ok
        key, _ = private_state(self._private, supported, remote, excluded)
        text = {"on": "Private", "off": "Visible", "failed": "Not private",
                "unavailable": "Private n/a"}[key]
        bg, fg, border = self._CHIP_COLORS[key]
        try:
            from ui.icons import eye_icon
            from PySide6.QtCore import QSize
            # Eye-off while hidden from the share, open eye when it shows.
            self.btn_private.setIcon(eye_icon(open_=(key != "on"), size=16, color=QColor(fg)))
            self.btn_private.setIconSize(QSize(16, 16))
        except Exception:
            pass
        self.btn_private.setText(text)
        self.btn_private.setStyleSheet(
            f"QPushButton#laPrivate {{ background: {bg}; color: {fg}; border: 1px solid "
            f"{border}; border-radius: 11px; padding: 2px 10px 2px 8px; font-size: 12px; "
            "font-weight: 600; }")
        tips = {
            "on": "Hidden from Zoom/Teams/Meet shares, recordings and screenshots on "
                  "this PC (not from phone cameras). Click to make it visible - e.g. "
                  "to include it in your own recording.",
            "off": "This card WILL show on a shared screen. Click to hide it from shares.",
            "failed": "Windows refused to hide this window - assume it is visible in a share.",
            "unavailable": "Screen-share privacy needs Windows 10 2004+ and a local "
                           "(non-remote) session.",
        }
        self.btn_private.setToolTip(tips[key])

    def _refresh_live_controls(self):
        live = self._meeting_active
        self.btn_start.setVisible(not live)
        self.lbl_timer.setVisible(live)
        self.btn_stop.setVisible(live)
        if live:
            self._update_timer()

    def _update_timer(self):
        since = getattr(self, "_live_since", None)
        if not since:
            return
        s = int(time.time() - since)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        self.lbl_timer.setText(("● %d:%02d:%02d" % (h, m, sec)) if h else ("● %02d:%02d" % (m, sec)))

    def _drag_began(self):
        if self._glass_mode == "liquid":
            self._sample_timer.setInterval(SAMPLE_MS_DRAG)
        elif self._blur_mode and glass.windows_build() < 22000:
            # Acrylic lags behind a moving window on Windows 10; lift it.
            glass.remove_backdrop_blur(self)

    def _drag_ended(self):
        if self.app:
            self.app.cfg["live_assist_pos"] = [self.x(), self.y()]
            self.app.save_config()
        if self._glass_mode == "liquid":
            self._sample_timer.setInterval(SAMPLE_MS_REST)
            self._sample_backdrop()
        elif self._blur_mode and glass.windows_build() < 22000:
            self._apply_glass()

    # ── liquid glass: sample what is behind the window ──
    def _sample_backdrop(self):
        if self._glass_mode != "liquid" or not self.isVisible():
            return
        try:
            scr = self.screen() or QApplication.primaryScreen()
            if scr is None:
                return
            g, sg = self.frameGeometry(), scr.geometry()
            pm = scr.grabWindow(0, g.x() - sg.x(), g.y() - sg.y(), g.width(), g.height())
            if pm.isNull():
                return
            img = pm.toImage()
            w, h = max(1, self.width()), max(1, self.height())
            # Down-then-up scaling is a cheap, good-looking blur: /12 for the
            # body (soft), /5 for the refraction ring (content stays legible).
            body = img.scaled(max(1, w // 12), max(1, h // 12), Qt.IgnoreAspectRatio,
                              Qt.SmoothTransformation)
            if _looks_like_capture_hole(body):
                # Hardware-accelerated video (a call's webcam strip, a player)
                # comes back from BitBlt as solid BLACK. Refracting that paints
                # a black bar through the glass. Let DWM's acrylic compose those
                # frames instead; the next sample re-tests.
                if self._glass_mode == "liquid":
                    self._glass_mode = "acrylic-video"
                    t = _THEMES[self._theme_name]
                    tint = (t["tint_blur"].red(), t["tint_blur"].green(),
                            t["tint_blur"].blue(), 0x20)
                    self._blur_mode = glass.apply_backdrop_blur(self, tint)
                    self._bd_body = self._bd_ring = None
                    self.update()
                return
            if self._glass_mode == "acrylic-video":
                glass.remove_backdrop_blur(self)
                self._blur_mode = ""
                self._glass_mode = "liquid"
            body = body.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            ring = img.scaled(max(1, w // 5), max(1, h // 5), Qt.IgnoreAspectRatio,
                              Qt.SmoothTransformation)
            ring = ring.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            self._bd_body, self._bd_ring = body, ring
            self.update()
        except Exception as e:
            logger.debug("backdrop sample failed: %s", e)

    def _track_specular(self):
        """The highlight drifts toward the cursor while it is over the card -
        the 'light moves across the glass' cue."""
        target = QPointF(SHADOW + CARD_W * 0.25, SHADOW + 8)
        try:
            gp = QCursor.pos()
            if self.frameGeometry().contains(gp):
                lp = self.mapFromGlobal(gp)
                target = QPointF(lp.x(), lp.y())
        except Exception:
            pass
        dx, dy = target.x() - self._spec_pos.x(), target.y() - self._spec_pos.y()
        if abs(dx) + abs(dy) > 0.8:
            self._spec_pos = QPointF(self._spec_pos.x() + dx * 0.25,
                                     self._spec_pos.y() + dy * 0.25)
            self.update()

    # ── painting: the glass surface ──
    def _shadow_image(self):
        """A genuinely blurred drop shadow (rendered once per size/theme):
        paint the card silhouette, then down/up-scale it - the same cheap
        blur as the backdrop. Rings of strokes showed their steps at the
        corners; this doesn't."""
        key = (self.width(), self.height(), self._theme_name)
        if getattr(self, "_shadow_key", None) == key:
            return self._shadow_img
        w, h = self.width(), self.height()
        from PySide6.QtGui import QImage
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 120 if self._theme_name == "light" else 170))
        sp = QPainterPath()
        sp.addRoundedRect(QRectF(SHADOW + 2, SHADOW + 7, w - 2 * SHADOW - 4, h - 2 * SHADOW - 4),
                          RADIUS, RADIUS)
        p.drawPath(sp)
        p.end()
        small = img.scaled(max(1, w // 7), max(1, h // 7), Qt.IgnoreAspectRatio,
                           Qt.SmoothTransformation)
        self._shadow_img = small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._shadow_key = key
        return self._shadow_img

    def paintEvent(self, event):
        t = _THEMES[self._theme_name]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        card = QRectF(SHADOW + 0.5, SHADOW + 0.5,
                      self.width() - 2 * SHADOW - 1, self.height() - 2 * SHADOW - 1)
        path = QPainterPath()
        path.addRoundedRect(card, RADIUS, RADIUS)

        p.drawImage(0, 0, self._shadow_image())

        p.save()
        p.setClipPath(path)
        liquid = self._glass_mode == "liquid" and self._bd_body is not None
        if liquid:
            p.drawImage(self.rect(), self._bd_body)
            # Refraction ring: the backdrop magnified toward the edges, as if
            # bent through thick glass.
            inner = QPainterPath()
            inner.addRoundedRect(card.adjusted(18, 18, -18, -18), RADIUS - 12, RADIUS - 12)
            ring = path.subtracted(inner)
            p.save()
            p.setClipPath(ring, Qt.IntersectClip)
            cx, cy = self.width() / 2.0, self.height() / 2.0
            p.translate(cx, cy + 2)
            p.scale(1.09, 1.09)
            p.translate(-cx, -cy)
            p.setOpacity(0.92)
            p.drawImage(self.rect(), self._bd_ring)
            p.restore()
            p.fillPath(ring, QColor(255, 255, 255, 22))
            p.fillPath(path, t["tint_liquid"])
        else:
            p.fillPath(path, t["tint_blur"] if self._blur_mode else t["tint_flat"])
        # Specular: a radial highlight that follows the cursor.
        rad = QRadialGradient(self._spec_pos, card.width() * 0.55)
        rad.setColorAt(0.0, QColor(255, 255, 255, t["spec"]))
        rad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(rad))
        # Top sheen.
        sheen = QLinearGradient(card.topLeft(), QPointF(card.left(), card.top() + 80))
        sheen.setColorAt(0.0, QColor(255, 255, 255, 60 if self._theme_name == "light" else 26))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(sheen))
        p.restore()

        # Rim light, clipped INSIDE the shape so it never doubles up with the
        # outer hairline: brightest at the top-left edge, fading around, with
        # a faint lift again at the bottom-right (light bouncing back in).
        p.save()
        p.setClipPath(path)
        rim = QLinearGradient(card.topLeft(), card.bottomRight())
        light = self._theme_name == "light"
        rim.setColorAt(0.0, QColor(255, 255, 255, 235 if light else 150))
        rim.setColorAt(0.45, QColor(255, 255, 255, 60 if light else 35))
        rim.setColorAt(1.0, QColor(255, 255, 255, 120 if light else 70))
        p.setPen(QPen(QBrush(rim), 2.0))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.restore()
        p.setPen(QPen(t["border"], 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    # ── visibility / geometry ──
    def _default_position(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        g = screen.availableGeometry()
        self.move(g.right() - self.width() - 16, g.top() + 70)

    def show_overlay(self):
        self.show()
        self.raise_()
        QTimer.singleShot(0, self._apply_glass)

    def hide_overlay(self):
        self._sample_timer.stop()
        self._spec_timer.stop()
        self.hide()

    def toggle(self):
        if self.isVisible():
            self.hide_overlay()
        else:
            self.show_overlay()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_glass)

    def set_expanded(self, expanded):
        self._expanded = bool(expanded)
        self.body.setVisible(self._expanded)
        self.setFixedSize(EXPANDED_W if self._expanded else COMPACT_W,
                          EXPANDED_H if self._expanded else COMPACT_H)
        self.btn_collapse.set_alt(not self._expanded)
        self._shadow_key = None
        QTimer.singleShot(0, self._apply_glass)

    # ── start / stop (drives the meeting recorder) ──
    def _start_listening(self):
        mw = getattr(self.app, "meetings_win", None) if self.app else None
        if mw is None:
            self._set_status("Meeting recorder isn't available.")
            return
        try:
            if getattr(mw, "state", None) == mw.STATE_RECORDING:
                return
            if hasattr(self.app, "is_pro") and not self.app.is_pro():
                if hasattr(self.app, "_pro_upsell"):
                    self.app._pro_upsell("Live Prompter")
                return
            if hasattr(mw, "input_title") and not mw.input_title.text().strip():
                mw.input_title.setText("Live session " + time.strftime("%H:%M"))
            # Transcription language for this session (default English) - the
            # recorder reads cfg["language"]; the previous value is restored
            # when the session ends so dictation keeps its own setting.
            lang = (self.app.cfg.get("live_assist_language") or "en").strip()
            if lang and lang != "auto":
                self._prev_language = self.app.cfg.get("language", "auto")
                self.app.cfg["language"] = lang
            mw._start_meeting()
            if getattr(mw, "state", None) != mw.STATE_RECORDING:
                self._restore_language()
                self._set_status("Couldn't start listening - see Record Meeting.")
        except Exception as e:
            logger.warning("Live Prompter start failed: %s", e, exc_info=True)
            self._restore_language()
            self._set_status(f"Couldn't start: {str(e)[:80]}")

    def _stop_listening(self):
        mw = getattr(self.app, "meetings_win", None) if self.app else None
        if mw is None or getattr(mw, "state", None) != mw.STATE_RECORDING:
            return
        try:
            mw._stop_meeting()
            self._set_status("Stopped - the notes are generated in Record Meeting.")
        except Exception as e:
            logger.warning("Live Prompter stop failed: %s", e, exc_info=True)
            self._set_status(f"Couldn't stop: {str(e)[:80]}")

    def _restore_language(self):
        prev = getattr(self, "_prev_language", None)
        if prev is not None and self.app:
            self.app.cfg["language"] = prev
            self._prev_language = None

    # ── data feed (GUI thread) ──
    def set_meeting_active(self, active, title="", attendees=""):
        self._meeting_active = bool(active)
        if active:
            self._live_since = time.time()
            self._live_text = ""
            self._summary = ""
            self._title, self._attendees = title or "", attendees or ""
            self.txt_now.setText("Listening… the last thing said will appear here.")
            self.txt_summary.clear()
            self.lbl_status.setText("")
        else:
            self._restore_language()
            self.lbl_status.setText("Stopped - suggestions use the final transcript.")
        self._refresh_live_controls()

    def feed_transcript(self, piece):
        piece = (piece or "").strip()
        if not piece:
            return
        self._live_text = (self._live_text + " " + piece).strip()
        self._text_since_suggest += len(piece)
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", self._live_text) if s]
        self.txt_now.setText(" ".join(sentences[-2:])[-260:])

    def set_summary(self, text):
        self._summary = text or ""
        lines = [ln for ln in self._summary.splitlines() if ln.strip()]
        keep = [ln for ln in lines if not ln.lstrip().startswith("#")][:6]
        self.txt_summary.setPlainText("\n".join(keep))

    # ── suggestions ──
    def _set_status(self, text):
        self.lbl_status.setText(text or "")

    def _ask(self):
        q = self.input_ask.text().strip()
        if q:
            self.suggest(q)

    def suggest(self, question=""):
        if self._suggesting:
            return
        if not self.app:
            self._on_suggestion("", "No app context.")
            return
        want_screen = self.btn_screen.isChecked()
        if not self._live_text.strip() and not question:
            if want_screen:
                question = "Describe what is on my screen and what I should do next."
            else:
                self.txt_suggestion.setPlainText(
                    "Nothing has been said yet - press Listen first, or turn on Screen "
                    "and ask about what's on your screen.")
                return
        image_b64 = ""
        if want_screen:
            image_b64 = capture_screen_png_b64(self.screen() or QApplication.primaryScreen())
        self._suggesting = True
        self._suggest_started = time.time()
        self._text_since_suggest = 0
        self.btn_suggest.setEnabled(False)
        self.txt_suggestion.setPlainText("Thinking…")
        context = rolling_context(
            self._live_text, question, self._title, self._attendees,
            screen=bool(image_b64),
            output_lang=(self.app.cfg.get("live_assist_output_language") or "en"))
        threading.Thread(target=self._suggest_worker, args=(context, image_b64),
                         daemon=True).start()

    def _resolve_engine(self):
        """Engine for suggestions: the configured notes engine with Pro routing;
        if that engine can't run (managed without Pro, cloud without its key),
        prefer a downloaded local model, else the built-in basic mode."""
        engine, cfg = self.app._resolve_action_engine()
        info = actions.ACTION_MODELS.get(engine, {})
        kind = info.get("kind")
        has_key = bool((self.app.cfg.get("action_api_key") or "").strip())
        has_token = bool((cfg or {}).get("_managed_token"))
        if (kind == "cloud" and not has_key) or (kind == "managed" and not has_token):
            try:
                import local_llm
                for mid in local_llm.MODEL_CATALOG:
                    if local_llm.model_downloaded(mid):
                        return mid, cfg
            except Exception:
                pass
            return actions.RULE_BASED_ID, cfg
        return engine, cfg

    def _suggest_worker(self, context, image_b64):
        try:
            engine, cfg = self._resolve_engine()
            kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
            cfg = dict(cfg or {})
            if image_b64 and kind in ("cloud", "managed"):
                cfg["_image_png_b64"] = image_b64
            elif image_b64:
                self.sig_status.emit("Screen needs a cloud AI engine (Pro or your own "
                                     "key) - answered from the transcript only.")
            text = actions.process(context, actions.ACTION_LIVE_ASSIST,
                                   model=engine, config=cfg)
            self.sig_suggestion.emit(text or "", "")
        except Exception as e:
            self.sig_suggestion.emit("", str(e)[:240])

    def _on_suggestion(self, text, error):
        self._suggesting = False
        self._last_suggest_at = time.time()
        self.btn_suggest.setEnabled(True)
        took = time.time() - self._suggest_started if self._suggest_started else 0
        if error:
            self.txt_suggestion.setPlainText(f"Couldn't get a suggestion: {error}")
            return
        render_markdown(self.txt_suggestion, text.strip() or "(no suggestion)")
        if not self.lbl_status.text().startswith("Screen"):
            self.lbl_status.setText(f"Updated {time.strftime('%H:%M:%S')} · {took:.1f}s")
        self.input_ask.clear()

    def _on_auto_toggled(self, on):
        self._auto = bool(on)
        if self.app:
            self.app.cfg["live_assist_auto"] = self._auto
            self.app.save_config()

    def _on_tick(self):
        # Watchdog: Qt can recreate the native window (flag/parent changes)
        # and the exclusion lives on the HWND - re-apply if it went missing.
        if (self._private and self.isVisible() and glass.capture_exclusion_supported()
                and not glass.is_remote_session()
                and not glass.is_excluded_from_capture(self)):
            self._apply_glass()
        if self._meeting_active:
            self._update_timer()
        if self._suggesting:
            el = int(time.time() - self._suggest_started)
            if el >= 3:
                self.txt_suggestion.setPlainText(f"Thinking… {el}s")
            return
        if (self._auto and self._meeting_active and self.isVisible()
                and self._text_since_suggest > 120
                and time.time() - self._last_suggest_at >= AUTO_SUGGEST_EVERY_SEC):
            self.suggest("")
