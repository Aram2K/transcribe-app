"""Live Assist: a private, glass-styled copilot overlay for live calls.

What it is (product contract):
* Floats above Zoom/Teams/Meet/anything, always on top, draggable, with a
  compact pill state and an expanded card.
* Shows the live transcript tail and the rolling summary that the Record
  Meeting window already produces, and - on demand or automatically - an AI
  suggestion: what was just asked of the user and what to say next, or the
  answer to a question the user types.
* PRIVATE by default: excluded from screen capture (ui/glass.py) so it never
  appears on a shared screen or in a recording, while staying visible on the
  user's own monitor. This is a privacy feature for the user's private notes.
  It is not, and must never be marketed as, a way to deceive anyone: the app
  keeps its recording-consent guidance, and the overlay does nothing the
  user couldn't do with a paper notepad beside the laptop.
* "Liquid glass" look: real backdrop blur when Windows provides it, with a
  painted translucent fallback; adaptive light/dark tint; specular top edge.

Threading: the suggestion request runs on a worker thread and reports back
through sig_suggestion; every widget touch happens on the GUI thread.
"""
import logging
import re
import threading
import time

from PySide6.QtCore import Qt, QPoint, QRectF, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSlider, QTextEdit, QVBoxLayout, QWidget,
)

import actions
from ui import glass

logger = logging.getLogger("transcribe")

# Width is shared by both states so collapse/expand never jumps sideways; it
# must fit the header (title + privacy chip + 3 icon buttons) and the quick-
# action row (section label + Say next / Follow-ups / Recap).
EXPANDED_W, EXPANDED_H = 480, 560
COMPACT_W, COMPACT_H = 480, 52
TAIL_CHARS = 2600            # ~3-4 minutes of speech fed to the model
AUTO_SUGGEST_EVERY_SEC = 25  # when Auto is on and new speech arrived

_THEMES = {
    "light": {
        "tint_blur": QColor(255, 255, 255, 150),   # over real backdrop blur
        "tint_flat": QColor(255, 255, 255, 228),   # painted fallback
        "border": QColor(15, 23, 42, 34),
        "highlight": QColor(255, 255, 255, 190),
        "text": "#0f172a", "muted": "#475569", "faint": "#64748b",
        "card": "rgba(255,255,255,0.64)", "card_border": "rgba(15,23,42,0.10)",
        "accent": "#2563eb",
    },
    "dark": {
        "tint_blur": QColor(17, 24, 39, 150),
        "tint_flat": QColor(17, 24, 39, 232),
        "border": QColor(255, 255, 255, 40),
        "highlight": QColor(255, 255, 255, 70),
        "text": "#f8fafc", "muted": "#cbd5e1", "faint": "#94a3b8",
        "card": "rgba(255,255,255,0.08)", "card_border": "rgba(255,255,255,0.14)",
        "accent": "#60a5fa",
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
    if not wanted:
        return "off", "VISIBLE in screen share"
    if remote:
        return "unavailable", "PRIVATE unavailable in a remote session"
    if not supported:
        return "unavailable", "PRIVATE unavailable on this system"
    if excluded:
        return "on", "PRIVATE · not in your screen share"
    return "failed", "PRIVATE failed · visible in screen share"


def rolling_context(live_text, question="", title="", attendees="", tail_chars=TAIL_CHARS):
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
    if (question or "").strip():
        parts.append("User's question: " + question.strip())
    return "\n\n".join(parts)


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

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # macOS hides Qt.Tool windows when the app loses focus - which is
        # exactly when the user is in Zoom. No-op elsewhere.
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowTitle("Live Prompter")
        self.setFixedSize(EXPANDED_W, EXPANDED_H)
        self._exclusion_ok = False

        self.sig_suggestion.connect(self._on_suggestion)
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

    # ── build ──
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 12)
        root.setSpacing(8)

        self.bar = _DragBar(self)
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(4, 2, 0, 2)
        bl.setSpacing(8)
        self.lbl_dot = QLabel("●", self.bar)
        bl.addWidget(self.lbl_dot)
        self.lbl_title = QLabel("Live Prompter", self.bar)
        bl.addWidget(self.lbl_title)
        self.lbl_private = QLabel("", self.bar)
        self.lbl_private.setCursor(Qt.PointingHandCursor)
        self.lbl_private.mousePressEvent = lambda e: self.set_private(not self._private)
        bl.addWidget(self.lbl_private)
        bl.addStretch()
        self.btn_theme = QPushButton("◐", self.bar)
        self.btn_theme.setToolTip("Light / dark glass")
        self.btn_theme.clicked.connect(self._toggle_theme)
        bl.addWidget(self.btn_theme)
        self.btn_collapse = QPushButton("—", self.bar)
        self.btn_collapse.setToolTip("Collapse to a pill (double-click the bar too)")
        self.btn_collapse.clicked.connect(lambda: self.set_expanded(not self._expanded))
        bl.addWidget(self.btn_collapse)
        self.btn_close = QPushButton("✕", self.bar)
        self.btn_close.setToolTip("Hide (your hotkey brings it back)")
        self.btn_close.clicked.connect(self.hide_overlay)
        bl.addWidget(self.btn_close)
        for b in (self.btn_theme, self.btn_collapse, self.btn_close):
            b.setFixedSize(26, 22)
            b.setCursor(Qt.PointingHandCursor)
        root.addWidget(self.bar)

        self.body = QWidget(self)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        self.lbl_now_head = QLabel("NOW", self.body)
        body.addWidget(self.lbl_now_head)
        self.txt_now = QLabel("Start a meeting recording and the last thing said "
                              "will appear here.", self.body)
        self.txt_now.setWordWrap(True)
        self.txt_now.setObjectName("laNow")
        body.addWidget(self.txt_now)

        self.lbl_sum_head = QLabel("SO FAR", self.body)
        body.addWidget(self.lbl_sum_head)
        self.txt_summary = QTextEdit(self.body)
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setObjectName("laCard")
        self.txt_summary.setMaximumHeight(120)
        body.addWidget(self.txt_summary)

        head_row = QHBoxLayout()
        self.lbl_sug_head = QLabel("SUGGESTION · CHECK FACTS", self.body)
        self.lbl_sug_head.setToolTip("AI suggestions can be wrong - treat them as notes, "
                                     "not facts.")
        head_row.addWidget(self.lbl_sug_head)
        head_row.addStretch()
        # One-tap actions (the part of Cluely's UX worth copying): each is just
        # a canned question through the same suggestion path.
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
            "Press Suggest (or ask a question below) and a short answer or "
            "talking points appear here.")
        body.addWidget(self.txt_suggestion, 1)

        ask_row = QHBoxLayout()
        self.input_ask = QLineEdit(self.body)
        self.input_ask.setPlaceholderText("Ask about the conversation…")
        self.input_ask.returnPressed.connect(self._ask)
        ask_row.addWidget(self.input_ask, 1)
        self.btn_suggest = QPushButton("Suggest", self.body)
        self.btn_suggest.setObjectName("laSuggest")
        self.btn_suggest.setCursor(Qt.PointingHandCursor)
        self.btn_suggest.clicked.connect(lambda: self.suggest(""))
        ask_row.addWidget(self.btn_suggest)
        body.addLayout(ask_row)

        foot = QHBoxLayout()
        self.lbl_status = QLabel("", self.body)
        foot.addWidget(self.lbl_status, 1)
        foot.addWidget(QLabel("Opacity", self.body))
        self.slider = QSlider(Qt.Horizontal, self.body)
        self.slider.setRange(55, 100)
        cfg = self.app.cfg if self.app else {}
        self.slider.setValue(int(float(cfg.get("live_assist_opacity", 0.96)) * 100))
        self.slider.setFixedWidth(90)
        self.slider.valueChanged.connect(self._on_opacity)
        foot.addWidget(self.slider)
        body.addLayout(foot)

        root.addWidget(self.body, 1)
        self._on_opacity(self.slider.value())

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
                border-radius: 8px; color: {t['text']}; padding: 3px 10px; font-size: 12px;
            }}
            QPushButton:hover {{ border-color: {accent}; }}
            QPushButton:checked {{ background: {accent}; color: white; border-color: {accent}; }}
            QPushButton#laSuggest {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #8b5cf6);
                color: white; font-weight: bold; border: none; padding: 6px 16px; border-radius: 9px;
            }}
            QPushButton#laSuggest:disabled {{ background: #94a3b8; }}
            QSlider::groove:horizontal {{ height: 4px; background: {t['card_border']}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; border-radius: 6px; background: {accent}; }}
        """)
        for lbl in (self.lbl_now_head, self.lbl_sum_head, self.lbl_sug_head):
            lbl.setStyleSheet(f"color: {t['faint']}; font-size: 10px; font-weight: 700; "
                              "letter-spacing: 1px; background: transparent;")
        self.lbl_title.setStyleSheet(f"color: {t['text']}; font-weight: 700; font-size: 13px;")
        self.lbl_status.setStyleSheet(f"color: {t['faint']}; font-size: 11px;")
        self._refresh_private_chip()
        self._refresh_dot()
        self.update()

    def _toggle_theme(self):
        self._theme_name = "dark" if self._theme_name == "light" else "light"
        if self.app:
            self.app.cfg["live_assist_theme"] = self._theme_name
            self.app.save_config()
        self._apply_theme()
        self._apply_glass()

    def _apply_glass(self):
        """Backdrop blur + rounded corners + capture exclusion. Called after
        every show(): Qt can recreate the native window when flags change,
        and the effects live on the HWND."""
        if not self.isVisible():
            return
        t = _THEMES[self._theme_name]
        tint = (t["tint_blur"].red(), t["tint_blur"].green(), t["tint_blur"].blue(), 0x20)
        self._blur_mode = glass.apply_backdrop_blur(self, tint)
        glass.round_corners(self)
        self._apply_private()
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
        self._apply_private()

    _CHIP_STYLES = {
        "on": ("rgba(34,197,94,0.18)", "#15803d", "rgba(34,197,94,0.45)"),
        "off": ("rgba(239,68,68,0.16)", "#b91c1c", "rgba(239,68,68,0.45)"),
        "failed": ("rgba(239,68,68,0.16)", "#b91c1c", "rgba(239,68,68,0.45)"),
        "unavailable": ("rgba(245,158,11,0.18)", "#b45309", "rgba(245,158,11,0.5)"),
    }

    def _refresh_private_chip(self, remote=None, supported=None, excluded=None):
        if remote is None:
            remote = glass.is_remote_session()
        if supported is None:
            supported = glass.capture_exclusion_supported()
        if excluded is None:
            excluded = glass.is_excluded_from_capture(self) if self.isVisible() \
                else self._exclusion_ok
        key, text = private_state(self._private, supported, remote, excluded)
        bg, fg, border = self._CHIP_STYLES[key]
        self.lbl_private.setText(f"  {text}  ")
        self.lbl_private.setStyleSheet(
            f"background: {bg}; color: {fg}; border: 1px solid {border}; "
            "border-radius: 9px; font-size: 10px; font-weight: 700;")
        tips = {
            "on": "Hidden from Zoom/Teams/Meet shares, recordings and screenshots on "
                  "this PC. Not hidden from phone cameras. Click to turn off (e.g. to "
                  "include it in your own recording).",
            "off": "This overlay WILL show on a shared screen. Click to make it private.",
            "failed": "Windows refused to hide this window - assume it is visible in a share.",
            "unavailable": "Screen-share privacy needs Windows 10 2004+ and a local "
                           "(non-remote) session.",
        }
        self.lbl_private.setToolTip(tips[key])

    def _refresh_dot(self):
        color = "#ef4444" if self._meeting_active else _THEMES[self._theme_name]["faint"]
        self.lbl_dot.setStyleSheet(f"color: {color}; font-size: 12px;")

    def _on_opacity(self, v):
        self.setWindowOpacity(v / 100.0)
        if self.app:
            self.app.cfg["live_assist_opacity"] = v / 100.0

    def _drag_began(self):
        # Acrylic lags behind a moving window on Windows 10; lift it while
        # dragging there. Windows 11 keeps up.
        if self._blur_mode and glass.windows_build() < 22000:
            glass.remove_backdrop_blur(self)

    def _drag_ended(self):
        if self.app:
            self.app.cfg["live_assist_pos"] = [self.x(), self.y()]
            self.app.save_config()
        if self._blur_mode and glass.windows_build() < 22000:
            self._apply_glass()

    # ── painting: the glass surface ──
    def paintEvent(self, event):
        t = _THEMES[self._theme_name]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        path = QPainterPath()
        path.addRoundedRect(r, 20, 20)
        p.fillPath(path, QBrush(t["tint_blur"] if self._blur_mode else t["tint_flat"]))
        # Specular: a soft light sheen across the top - the "liquid" cue.
        sheen = QLinearGradient(r.topLeft(), QPoint(int(r.left()), int(r.top() + 90)))
        sheen.setColorAt(0.0, QColor(255, 255, 255, 70 if self._theme_name == "light" else 30))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, QBrush(sheen))
        p.setPen(QPen(t["border"], 1))
        p.drawPath(path)
        inner = QPainterPath()
        inner.addRoundedRect(r.adjusted(1, 1, -1, -1), 19, 19)
        p.setPen(QPen(t["highlight"], 1))
        p.drawPath(inner)

    # ── visibility / geometry ──
    def _default_position(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        g = screen.availableGeometry()
        self.move(g.right() - self.width() - 24, g.top() + 80)

    def show_overlay(self):
        self.show()
        self.raise_()
        QTimer.singleShot(0, self._apply_glass)

    def hide_overlay(self):
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
        self.btn_collapse.setText("—" if self._expanded else "▢")
        QTimer.singleShot(0, self._apply_glass)

    # ── data feed (GUI thread) ──
    def set_meeting_active(self, active, title="", attendees=""):
        self._meeting_active = bool(active)
        if active:
            self._live_text = ""
            self._summary = ""
            self._title, self._attendees = title or "", attendees or ""
            self.txt_now.setText("Listening… the last thing said will appear here.")
            self.txt_summary.clear()
            self.lbl_status.setText("")
        else:
            self.lbl_status.setText("Meeting ended - suggestions use the final transcript.")
        self._refresh_dot()

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
        # Keep it scannable: the first few bullet-ish lines, no headings.
        keep = [ln for ln in lines if not ln.lstrip().startswith("#")][:6]
        self.txt_summary.setPlainText("\n".join(keep))

    # ── suggestions ──
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
        if not self._live_text.strip() and not question:
            self.txt_suggestion.setPlainText(
                "Nothing has been said yet - start a meeting recording first.")
            return
        self._suggesting = True
        self._suggest_started = time.time()
        self._text_since_suggest = 0
        self.btn_suggest.setEnabled(False)
        self.txt_suggestion.setPlainText("Thinking…")
        context = rolling_context(self._live_text, question, self._title, self._attendees)
        threading.Thread(target=self._suggest_worker, args=(context,), daemon=True).start()

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

    def _suggest_worker(self, context):
        try:
            engine, cfg = self._resolve_engine()
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
            self.lbl_status.setText("")
            return
        self.txt_suggestion.setPlainText(text.strip() or "(no suggestion)")
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
            self._apply_private()
        if self._suggesting:
            el = int(time.time() - self._suggest_started)
            if el >= 3:
                self.txt_suggestion.setPlainText(f"Thinking… {el}s")
            return
        if (self._auto and self._meeting_active and self.isVisible()
                and self._text_since_suggest > 120
                and time.time() - self._last_suggest_at >= AUTO_SUGGEST_EVERY_SEC):
            self.suggest("")
