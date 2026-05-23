# Modern Transparent Visualizer Overlay in PySide6

import math
import time
from PySide6.QtCore import Qt, QTimer, QRectF, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QPainterPath
from PySide6.QtWidgets import QWidget, QApplication

# Overlay States
RECORDING = "recording"
TRANSCRIBING = "transcribing"
DONE = "done"

class Overlay(QWidget):
    W, H = 340, 96

    def __init__(self, main_app=None):
        super().__init__()
        self.app = main_app
        
        # Transparent, frameless overlay setup
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.SubWindow)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        
        self.setFixedSize(self.W, self.H)
        
        # Application State variables
        self.overlay_state = RECORDING
        self.levels = [0.0] * 20
        self._smooth = [0.0] * 20
        self._visible = False
        self._alpha = 0.0
        self._lang = ""
        self._partial = ""
        self._done_msg = ""
        self._done_pasted = False
        self._done_error = False
        self._hide_at = None
        
        # Timing Loop for Redrawing and Interpolating Alpha (equivalent to Tkinter after loop)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._loop)
        self.timer.start(30) # ~33 FPS
        
        # Position initialisation
        self.reposition()

    def reposition(self):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.geometry()
        # Bottom-right placement with safe margins
        self.move(geo.width() - self.W - 24, geo.height() - self.H - 80)

    def show_overlay(self, state=RECORDING):
        self.overlay_state = state
        self._lang = ""
        self._visible = True
        self.reposition()
        self.show()
        self.raise_()

    def hide_overlay(self):
        self._visible = False

    def set_state(self, s):
        self.overlay_state = s

    def set_lang(self, lang_name):
        self._lang = lang_name

    def set_partial(self, text):
        self._partial = text

    def update_levels(self, levels):
        self.levels = levels[:]

    def show_done(self, pasted: bool):
        self._done_pasted = pasted
        self._done_error = False
        self._done_msg = "Pasted to cursor" if pasted else "Copied to clipboard"
        self.overlay_state = DONE
        self._visible = True
        self._hide_at = time.time() + 2.2

    def show_error(self, msg: str):
        self._done_error = True
        self._done_pasted = False
        self._done_msg = msg
        self.overlay_state = DONE
        self._visible = True
        self._hide_at = time.time() + 4.0

    def call_soon(self, func, *args, **kwargs):
        # PySide6 handles cross-thread UI updates safely if we schedule it via QTimer.singleShot
        QTimer.singleShot(0, lambda: func(*args, **kwargs))

    def _loop(self):
        # Auto-hide timer
        if self._hide_at and time.time() >= self._hide_at:
            self._hide_at = None
            self._visible = False

        # Fade calculations
        target = 0.95 if self._visible else 0.0
        self._alpha += (target - self._alpha) * 0.25
        
        if self._alpha < 0.02 and not self._visible:
            self._alpha = 0.0
            self.hide()
        else:
            self.setWindowOpacity(self._alpha)
            if not self.isVisible() and self._visible:
                self.show()

        # Interpolate and smooth visualizer levels
        for i in range(len(self._smooth)):
            if i < len(self.levels):
                self._smooth[i] += (self.levels[i] - self._smooth[i]) * 0.35
        
        # Redraw
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        accent_color = self.app.cfg.get("accent_color", "#3b82f6") if self.app else "#3b82f6"
        
        # 1. Base Glassmorphic Panel Background
        path = QPainterPath()
        path.addRoundedRect(QRectF(1, 1, self.W - 2, self.H - 2), 14, 14)
        
        painter.fillPath(path, QBrush(QColor(22, 22, 24, 230)))
        
        # Border
        pen = QPen(QColor(43, 43, 47, 200))
        pen.setWidth(1)
        if self.overlay_state == RECORDING:
            border_color = QColor(accent_color)
            border_color.setAlpha(120)
            pen = QPen(border_color, 1)
        painter.strokePath(path, pen)

        # Draw corresponding state representation
        if self.overlay_state == RECORDING:
            self._draw_rec(painter, accent_color)
        elif self.overlay_state == TRANSCRIBING:
            self._draw_loading(painter, accent_color)
        elif self.overlay_state == DONE:
            self._draw_done(painter)

    def _draw_rec(self, painter, accent_color):
        t = time.time()
        
        # Pulsing Red dot
        pulse = 0.5 + 0.5 * math.sin(t * 3.5)
        dx, dy = 20, 28
        rr = 7 + pulse * 2.5
        
        # Outgoing soft pulse ring
        painter.setPen(QPen(QColor(255, 59, 59, 100), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPoint(dx, dy), rr, rr)
        
        # Main solid core
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(255, 59, 59)))
        painter.drawEllipse(QPoint(dx, dy), 4.5, 4.5)
        
        # Labels
        painter.setPen(QColor(255, 255, 255))
        font = QFont("Segoe UI", 11, QFont.Bold)
        painter.setFont(font)
        painter.drawText(38, 25, "Recording")
        
        painter.setPen(QColor(115, 115, 125))
        font = QFont("Segoe UI", 8)
        painter.setFont(font)
        hotkey_str = self.app.cfg.get("hotkey", "Alt+R") if self.app else "Alt+R"
        painter.drawText(38, 39, f"Enter or {hotkey_str} to finish  ·  Esc to cancel")

        # ── Symmetric vertical bar waveform ─────────────────────────────
        PAD = 14
        num = 44
        avail = self.W - PAD * 2
        step = avail / num
        bar_w = max(int(step * 0.55), 2)
        cy = self.H - 20
        max_h = 16

        ar, ag, ab = QColor(accent_color).red(), QColor(accent_color).green(), QColor(accent_color).blue()

        for i in range(num):
            idx_f = i / max(num - 1, 1) * (len(self._smooth) - 1)
            lo = int(idx_f)
            hi = min(lo + 1, len(self._smooth) - 1)
            lv = self._smooth[lo] * (1 - (idx_f - lo)) + self._smooth[hi] * (idx_f - lo)
            
            # Idle gentle ripples when silent
            lv = max(lv, 0.05 * abs(math.sin(t * 1.8 + i * 0.38)))
            
            bh = max(int(lv * max_h), 2)
            x = int(PAD + i * step)
            fac = 0.35 + 0.65 * min(lv * 2.0, 1.0)
            
            col = QColor(int(ar * fac), int(ag * fac), int(ab * fac))
            
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(col))
            painter.drawRect(x, cy - bh, bar_w, bh * 2)

    def _draw_loading(self, painter, accent_color):
        t = time.time()
        
        # Rotating arc wheel
        angle = int(-(t * 260) % 360) * 16 # 1/16th of a degree in Qt
        cx, cy, r = 22, 28, 10
        
        # Dull track circle
        painter.setPen(QPen(QColor(40, 40, 45), 2.5))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPoint(cx, cy), r, r)
        
        # Glowing loader arc
        painter.setPen(QPen(QColor(accent_color), 2.5))
        painter.drawArc(cx - r, cy - r, r * 2, r * 2, angle, 240 * 16)
        
        # Labels
        dots = "." * (int(t * 2.5) % 4)
        label = f"Transcribing · {self._lang}{dots}" if self._lang else f"Finalising{dots}"
        
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
        painter.drawText(42, 25, label)
        
        backend = "Google Cloud" if self.app and self.app.cfg.get("backend") == "google" else "Local AI"
        painter.setPen(QColor(90, 90, 100))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(42, 39, f"via {backend}  ·  pasting when ready")

        # Partial Preview Card
        if self._partial:
            preview = self._partial[-55:].lstrip()
            if len(self._partial) > 55:
                preview = "…" + preview
            
            # Preview box container
            painter.setPen(QPen(QColor(39, 39, 42), 1))
            painter.setBrush(QBrush(QColor(18, 18, 20)))
            painter.drawRoundedRect(QRectF(10, 52, self.W - 20, 36), 6, 6)
            
            # Preview text
            painter.setPen(QColor(148, 163, 184))
            painter.setFont(QFont("Segoe UI", 9))
            painter.drawText(18, 74, preview)

    def _draw_done(self, painter):
        cx, cy, r = 24, self.H // 2, 12
        
        if self._done_error:
            # Circle
            painter.setPen(QPen(QColor(239, 68, 68), 1.5))
            painter.setBrush(QBrush(QColor(58, 26, 26)))
            painter.drawEllipse(QPoint(cx, cy), r, r)
            # Red X lines
            painter.setPen(QPen(QColor(239, 68, 68), 2, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(cx - 4, cy - 4, cx + 4, cy + 4)
            painter.drawLine(cx + 4, cy - 4, cx - 4, cy + 4)
            
            # Error Messages
            msg = self._done_msg if len(self._done_msg) <= 36 else self._done_msg[:34] + "…"
            painter.setPen(QColor(239, 68, 68))
            painter.setFont(QFont("Segoe UI", 10, QFont.Bold))
            painter.drawText(46, self.H // 2 - 2, msg)
            
            painter.setPen(QColor(115, 115, 125))
            painter.setFont(QFont("Segoe UI", 8))
            painter.drawText(46, self.H // 2 + 12, "Check Settings → Test Key")
        else:
            # Green checkmark circle
            painter.setPen(QPen(QColor(34, 197, 94), 1.5))
            painter.setBrush(QBrush(QColor(26, 58, 36)))
            painter.drawEllipse(QPoint(cx, cy), r, r)
            # Checkmark lines
            painter.setPen(QPen(QColor(34, 197, 94), 2.5, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(cx - 5, cy, cx - 1, cy + 4)
            painter.drawLine(cx - 1, cy + 4, cx + 6, cy - 4)
            
            # Text Success
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
            painter.drawText(46, self.H // 2 + 5, self._done_msg)
