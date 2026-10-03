"""Live Assistance: a private, liquid-glass copilot overlay for live calls.

What it is (product contract):
* Floats above Zoom/Teams/Meet/anything, always on top, draggable, with a
  compact pill state and an expanded card. Its own Listen button starts the
  meeting capture (system audio + mic) so the overlay is self-sufficient.
* Shows the live transcript tail and the rolling summary from the meeting
  pipeline, and - on demand (Say next / Follow-ups / Recap / a typed question)
  or automatically - an AI suggestion. With Screen on, a screenshot rides
  along so the AI can answer about what is on the user's screen.
* A session context: before (or during) a call the user writes what it is
  about - who they are, who they're talking to, what they want. It's locked
  in for the session: every answer is shaped by it.
* PRIVATE by default: excluded from screen capture (ui/glass.py) so it never
  appears on a shared screen or in a recording while staying visible on the
  user's own monitor. A privacy feature for the user's private notes - never
  marketed as a way to deceive anyone; recording-consent guidance applies.
* An image can go with a question: a screenshot cropped inside the card
  itself (nothing new appears on the screen) or one pasted with Ctrl+V.
* Liquid glass: because the window is excluded from capture, the pixels
  BEHIND it can be sampled (QScreen.grabWindow honours the exclusion) and
  rendered back through the shape - real blur, an edge refraction ring, a
  cursor-tracked specular highlight, rim light and a soft shadow. Where
  sampling isn't safe (Visible mode, exclusion unconfirmed, macOS, RDP) it
  paints a darker rounded plate instead - never the OS acrylic, which blurs
  the whole square window and shows as a rectangle around the card.

Threading: suggestion requests run on a worker thread and report back via
signals; every widget touch happens on the GUI thread.
"""
import base64
import logging
import re
import sys
import threading
import time

from PySide6.QtCore import (
    Qt, QBuffer, QEvent, QIODevice, QObject, QPointF, QRect, QRectF, QSize, QTimer, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QIcon, QImage, QKeySequence, QLinearGradient, QPainter,
    QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

import actions
from live_context import (  # noqa: F401 - re-exported for callers of this module
    CONTEXT_CHARS, HISTORY_TURNS, SOLVE_SCREEN, TAIL_CHARS, clip_context, last_question,
    looks_like_question, rolling_context, should_attach_screen,
)
from ui import glass

logger = logging.getLogger("transcribe")

SHADOW = 16                      # transparent margin around the card (drop shadow)
RADIUS = 22
CARD_W = 480
# Window = card + shadow margins. Width is shared by both states so
# collapse/expand never jumps sideways.
EXPANDED_W, EXPANDED_H = CARD_W + 2 * SHADOW, 560 + 2 * SHADOW
COMPACT_W, COMPACT_H = CARD_W + 2 * SHADOW, 52 + 2 * SHADOW
SAMPLE_MS_REST, SAMPLE_MS_DRAG = 150, 40

_THEMES = {
    "light": {
        # Cool slate rather than milky white: darker glass, and what's behind
        # doesn't bleed through the text.
        "tint_liquid": QColor(214, 222, 233, 165),  # over the sampled backdrop
        "tint_blur": QColor(214, 222, 233, 190),    # (OS acrylic - no longer used)
        "tint_flat": QColor(206, 215, 227, 242),    # painted plate (Visible mode)
        "border": QColor(15, 23, 42, 40),
        "text": "#0f172a", "muted": "#475569", "faint": "#64748b",
        "card": "rgba(255,255,255,0.72)", "card_border": "rgba(15,23,42,0.10)",
        "accent": "#2563eb", "spec": 95,
    },
    "dark": {
        "tint_liquid": QColor(17, 24, 39, 160),
        "tint_blur": QColor(17, 24, 39, 180),
        "tint_flat": QColor(15, 20, 33, 240),
        "border": QColor(255, 255, 255, 46),
        "text": "#f8fafc", "muted": "#cbd5e1", "faint": "#94a3b8",
        "card": "rgba(17,24,39,0.62)", "card_border": "rgba(255,255,255,0.14)",
        "accent": "#60a5fa", "spec": 45,
    },
}

# After an answer the user asked for, how long an automatic one waits before
# it may replace it (they are still reading it, or saying it).
USER_ANSWER_HOLD_S = 12

ASK_PLACEHOLDER = "Ask anything about the call or your screen…"
ASK_SENT = "✓ Sent - answering…"

# Sent with an image and no typed question.
SNIP_QUESTION = "Answer or solve what's in the attached image. Give the answer directly."

CONTEXT_EMPTY = "+ Add context - what's this call about?"
CONTEXT_PLACEHOLDER = (
    "What's this call about? Who you are, who you're talking to, what you want "
    "from it. Every answer this session uses it.\n\n"
    "e.g. Call with Acme's data team about moving their reports to our platform. "
    "I'm the solutions engineer; they care most about cost and security. Keep "
    "answers short, in my voice.")

QUICK_ACTIONS = (
    ("Answer", ""),
    ("Follow-ups", "Give me 3 sharp follow-up questions I could ask right now, "
                   "one line each."),
    ("Solve screen", SOLVE_SCREEN),
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


def mac_partial_tip(macos26=False):
    """The macOS badge tooltip: the sharingType flag (what Electron's
    setContentProtection sets) is honoured by some capture paths, not all -
    see glass.capture_exclusion_supported. Never claims more."""
    caveat = "Not yet verified on macOS 26." if macos26 else "Apple doesn't guarantee it."
    return ("Uses macOS's private-window setting. Screenshots skip it, and so do some "
            "screen shares (reportedly Zoom's \u201c\u2026with window filtering\u201d capture "
            "modes), but QuickTime and \u2318\u21e75 recordings show it, and so can other "
            "apps and settings. " + caveat + " Test from a second device before relying on "
            "it, or share a single window. Click to make it visible.")


def private_state(wanted, supported, remote, excluded, partial=False):
    """(key, chip text) - always TRUTHFUL about what the OS actually did. The
    badge is the whole privacy promise; it must never say hidden when the
    window is in fact capturable (macOS, old Windows, RDP, API refusal).

    ``partial``: macOS, where the window's sharingType is set to none and
    reads back so - some capture paths honour it, some don't (QuickTime,
    Zoom's default capture mode), and Apple promises nothing. Never "Private"."""
    # Short on purpose: the chip shares the header with Listen/Stop and three
    # icon buttons; the full explanation lives in its tooltip.
    if not wanted:
        return "off", "VISIBLE in share"
    if remote:
        return "unavailable", "PRIVATE n/a · remote"
    if not supported:
        if partial:
            return "partial", "MAY be hidden"
        return "unavailable", "PRIVATE n/a here"
    if excluded:
        return "on", "PRIVATE · not in share"
    return "failed", "NOT PRIVATE · visible"


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


def jpeg_b64(img, max_w=1600, quality=80):
    """A QPixmap/QImage as base64 JPEG, downscaled for upload - a third of the
    PNG size, so it reaches the model faster, yet sharp enough for code and
    small UI text. "" for an empty image."""
    if img is None or img.isNull():
        return ""
    if isinstance(img, QImage) and img.hasAlphaChannel():
        # JPEG has no alpha: transparent areas would turn black.
        flat = QImage(img.size(), QImage.Format_RGB32)
        flat.fill(QColor("#ffffff"))
        p = QPainter(flat)
        p.drawImage(0, 0, img)
        p.end()
        img = flat
    if img.width() > max_w:
        img = img.scaledToWidth(max_w, Qt.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "JPG", quality)
    return base64.b64encode(bytes(buf.data())).decode("ascii")


def capture_screen_b64(screen, max_w=1600, quality=80):
    """Screenshot of ``screen`` as base64 JPEG (see jpeg_b64). The overlay
    itself is absent when capture exclusion (Private) is active."""
    try:
        return jpeg_b64(screen.grabWindow(0), max_w, quality)
    except Exception as e:
        logger.debug("screen capture failed: %s", e)
        return ""


def screen_to_capture(overlay_screen):
    """The monitor the user is working on: the one under the mouse, since a
    question about "this" is about what they're looking at - not necessarily
    the monitor the overlay sits on."""
    try:
        return QApplication.screenAt(QCursor.pos()) or overlay_screen \
            or QApplication.primaryScreen()
    except Exception:
        return overlay_screen or QApplication.primaryScreen()


def _send_icon(size=18, color="#ffffff"):
    """A chat-style send icon (paper plane pointing right), painted as a path
    so it's crisp at any DPI."""
    scale = 3                                   # draw big, let Qt scale down smoothly
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size * scale / 24.0, size * scale / 24.0)
    path = QPainterPath(QPointF(3.4, 20.4))     # the classic send glyph on a 24 grid
    path.lineTo(QPointF(21.0, 12.0))
    path.lineTo(QPointF(3.4, 3.6))
    path.lineTo(QPointF(3.4, 10.2))
    path.lineTo(QPointF(15.0, 12.0))
    path.lineTo(QPointF(3.4, 13.8))
    path.closeSubpath()
    p.fillPath(path, QColor(color))
    p.end()
    pm.setDevicePixelRatio(scale)
    return QIcon(pm)


def _lock_pixmap(size=12, color="#2563eb"):
    """A small padlock - the session context is locked in - painted as a path
    so it's crisp at any DPI."""
    scale = 3
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size * scale / 24.0, size * scale / 24.0)
    pen = QPen(QColor(color), 2.6)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    shackle = QPainterPath(QPointF(7.5, 11.5))
    shackle.lineTo(QPointF(7.5, 8.0))
    shackle.arcTo(QRectF(7.5, 3.5, 9.0, 9.0), 180, -180)
    shackle.lineTo(QPointF(16.5, 11.5))
    p.drawPath(shackle)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(4.5, 10.5, 15.0, 11.0), 2.5, 2.5)
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


class _Superseded(BaseException):
    """Raised from a stream's token callback when a newer question took over.
    A BaseException so the providers' per-chunk ``except Exception`` parsing
    can't swallow it - the stale stream stops at its next token."""


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
        elif self._kind == "snip":
            # Viewfinder corners - "capture a part of the screen".
            s, k = r.width() * 0.22, r.width() * 0.11
            for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                x, y = cx + dx * s, cy + dy * s
                p.drawLine(QPointF(x, y), QPointF(x - dx * k, y))
                p.drawLine(QPointF(x, y), QPointF(x, y - dy * k))
        elif self._kind == "stop":
            # Red rounded square - the universal "stop recording".
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ef4444"))
            p.drawRoundedRect(QRectF(cx - 4.5, cy - 4.5, 9, 9), 2.2, 2.2)


class _ElidedLabel(QLabel):
    """One line that shortens itself with "…" when the bar is tight (fonts
    run wider on macOS) instead of being cut off mid-letter."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._full = text

    def setText(self, text):
        self._full = text or ""
        self._elide()

    def text(self):
        return self._full

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4,
                     super().sizeHint().height())

    def minimumSizeHint(self):
        return QSize(28, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event):
        super().changeEvent(event)
        self._elide()                     # a style/font change re-measures

    def _elide(self):
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(0, self.width()))
        if shown != QLabel.text(self):
            QLabel.setText(self, shown)


class _AskEdit(QLineEdit):
    """The ask box. Ctrl+V (⌘V) with an image on the clipboard attaches the
    image instead of pasting text."""
    image_pasted = Signal(QImage)

    _IMAGE_FILES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.Paste):
            img = self._clipboard_image()
            if img is not None and not img.isNull():
                self.image_pasted.emit(img)
                return
        super().keyPressEvent(e)

    def _clipboard_image(self):
        """An image on the clipboard - a screenshot, or an image file copied in
        Explorer / Finder - else None."""
        cb = QApplication.clipboard()
        mime = cb.mimeData()
        if mime is None:
            return None
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile() if url.isLocalFile() else ""
                if path.lower().endswith(self._IMAGE_FILES):
                    return QImage(path)
        if mime.hasImage() and not mime.hasText():
            # (Excel and others put a picture next to copied text - text wins.)
            return cb.image()
        return None


class _ImageChip(QPushButton):
    """The attached image: a rounded thumbnail with a small x - click to
    remove it."""

    def __init__(self, parent):
        super().__init__(parent)
        self._thumb = QPixmap()
        self.setFixedSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setStyleSheet("background: transparent; border: none;")
        self.setToolTip("Image attached - it goes with your next question. Click to remove.")

    def set_image(self, img):
        dpr = self.devicePixelRatioF() or 1.0
        side = int(34 * dpr)
        pm = QPixmap.fromImage(img).scaled(side, side, Qt.KeepAspectRatioByExpanding,
                                           Qt.SmoothTransformation)
        x, y = max(0, (pm.width() - side) // 2), max(0, (pm.height() - side) // 2)
        self._thumb = pm.copy(x, y, side, side)
        self._thumb.setDevicePixelRatio(dpr)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.setClipPath(clip)
        if not self._thumb.isNull():
            p.drawPixmap(r.toRect(), self._thumb)
        p.setClipping(False)
        p.setPen(QPen(QColor(15, 23, 42, 60), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 8, 8)
        # The remove badge, top right.
        badge = QRectF(r.right() - 13, r.top() + 1, 12, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 200))
        p.drawEllipse(badge)
        pen = QPen(QColor("#ffffff"), 1.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        c = badge.center()
        p.drawLine(QPointF(c.x() - 2.5, c.y() - 2.5), QPointF(c.x() + 2.5, c.y() + 2.5))
        p.drawLine(QPointF(c.x() + 2.5, c.y() - 2.5), QPointF(c.x() - 2.5, c.y() + 2.5))


class _PopupPrivacy(QObject):
    """Tooltips, right-click menus and other popups are windows of their own,
    so the card's capture exclusion doesn't cover them - a tooltip over the
    card would show in a share. Each one opened from the card gets the card's
    privacy as it shows (before it's on screen), and gives it back if Qt
    reuses it for another window.

    A menu's parent is the widget it belongs to; Qt 6's tooltip window has no
    parent, so a tooltip is the card's when the last tooltip REQUEST (the
    QEvent.ToolTip sent to the widget under the mouse) came from the card."""

    def __init__(self, card):
        super().__init__(card)
        self._card = card

    def eventFilter(self, obj, event):
        t = event.type()
        if t == QEvent.ToolTip and isinstance(obj, QWidget):
            mine = obj.window() is self._card
            self._card._tip_from_card = mine
            if mine:
                # The tooltip window may already be up (Qt reuses it while a
                # tooltip shows): guard it too, once Qt has placed the text.
                QTimer.singleShot(0, self._card._guard_visible_tips)
        elif t == QEvent.Show and obj is not self._card \
                and isinstance(obj, QWidget) and obj.isWindow() \
                and obj.windowType() in (Qt.ToolTip, Qt.Popup):
            self._card._guard_popup(obj)
        return False


class _CropView(QWidget):
    """The screenshot, inside the card: drag over the part to send. Nothing
    opens on the screen itself, so a shared screen shows nothing new (no
    full-screen picker, no crosshair sweeping across it) - the card is the
    only thing that changes, and it's private. Enter or a double-click
    attaches, Esc cancels."""
    changed = Signal()
    accepted = Signal()
    cancelled = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self._shot = QPixmap()
        self._origin = None                # drag start, 0..1 image coordinates
        self._sel = None                   # QRectF in 0..1 image coordinates
        self._sel_before_press = None      # a double-click's first click clears
        self._dbl = False                  # the press was a double-click's second
        self._dbl_restore = None
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.CrossCursor)
        self.setMinimumHeight(120)
        # As tall as the screenshot's shape needs (no empty bands above and
        # below it), capped so a portrait monitor still fits the card.
        pol = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        pol.setHeightForWidth(True)
        self.setSizePolicy(pol)

    MAX_H = 380
    CLICK_SLOP = 5                     # px on screen: less is a click, not a drag

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        if self._shot.isNull() or self._shot.width() <= 0:
            return 240
        return max(120, min(self.MAX_H, round(w * self._shot.height() / self._shot.width())))

    def sizeHint(self):
        return QSize(452, self.heightForWidth(452))

    def set_shot(self, pm):
        self._shot = pm
        self._sel = self._origin = None
        self.updateGeometry()                 # a new shape (another monitor)
        self.changed.emit()
        self.update()

    def has_selection(self):
        return self._sel is not None

    def _target(self):
        """Where the screenshot is drawn: fitted and centred."""
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._shot.isNull() or r.width() <= 0 or r.height() <= 0:
            return r
        iw, ih = self._shot.width(), self._shot.height()
        scale = min(r.width() / iw, r.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF(r.x() + (r.width() - w) / 2, r.y() + (r.height() - h) / 2, w, h)

    def _norm(self, pos):
        t = self._target()
        if t.width() <= 0 or t.height() <= 0:
            return QPointF(0, 0)
        return QPointF(min(1.0, max(0.0, (pos.x() - t.x()) / t.width())),
                       min(1.0, max(0.0, (pos.y() - t.y()) / t.height())))

    @staticmethod
    def _span(a, b):
        return QRectF(QPointF(min(a.x(), b.x()), min(a.y(), b.y())),
                      QPointF(max(a.x(), b.x()), max(a.y(), b.y())))

    def crop_rect(self):
        """The selection in the screenshot's device pixels (the whole shot
        when nothing is selected)."""
        iw, ih = self._shot.width(), self._shot.height()
        if self._sel is None:
            return QRect(0, 0, iw, ih)
        s = self._sel
        x, y = round(s.x() * iw), round(s.y() * ih)
        return QRect(x, y, max(1, min(iw - x, round(s.width() * iw))),
                     max(1, min(ih - y, round(s.height() * ih))))

    def crop_image(self):
        if self._shot.isNull():
            return QImage()
        return self._shot.copy(self.crop_rect()).toImage()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        t = self._target()
        clip = QPainterPath()
        clip.addRoundedRect(t, 8, 8)
        p.setClipPath(clip)
        if not self._shot.isNull():
            p.drawPixmap(t, self._shot, QRectF(self._shot.rect()))
        if self._sel is not None:
            sel = QRectF(t.x() + self._sel.x() * t.width(), t.y() + self._sel.y() * t.height(),
                         self._sel.width() * t.width(), self._sel.height() * t.height())
            outside = QPainterPath()
            outside.addRect(t)
            outside.addRect(sel)                          # odd-even fill: a hole
            p.fillPath(outside, QColor(8, 12, 20, 130))
            p.setClipping(False)
            p.setPen(QPen(QColor("#60a5fa"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRect(sel)
        p.setClipping(False)
        p.setPen(QPen(QColor(15, 23, 42, 70), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(t, 8, 8)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._dbl = False
            self._sel_before_press = self._sel
            self._origin = self._norm(e.position())
            self._sel = None
            self.changed.emit()
            self.update()
        elif e.button() == Qt.RightButton:
            self.cancelled.emit()

    def mouseMoveEvent(self, e):
        if self._origin is not None:
            self._sel = self._span(self._origin, self._norm(e.position()))
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._origin is None:
            return
        sel = self._span(self._origin, self._norm(e.position()))
        self._origin = None
        t = self._target()
        # A click (no real drag) clears the selection: the whole screen again.
        # Measured on screen, not in screenshot pixels: a 4K shot is drawn ~8x
        # smaller here, so a 1 px wobble would otherwise count as a crop. One
        # long side is enough: a thin drag over one line of text is a crop.
        dragged = max(sel.width() * t.width(), sel.height() * t.height()) >= self.CLICK_SLOP
        if self._dbl:
            self._dbl = False
            if not dragged:
                # A real double-click: attach what was picked before it.
                self._sel = self._dbl_restore
                self.changed.emit()
                self.update()
                self.accepted.emit()
                return
        self._sel = sel if dragged else None
        self.changed.emit()
        self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            # Qt delivers a quick second press as this, with no press event.
            # Decided on release: released in place it's a double-click
            # (attach); moved, it's a drag begun right after a click.
            self._dbl = True
            self._dbl_restore = self._sel_before_press
            self._origin = self._norm(e.position())
            self._sel = None
            self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.cancelled.emit()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accepted.emit()
        else:
            super().keyPressEvent(e)


class _ContextChip(QFrame):
    """The line under the header: the session context the answers use, or an
    invitation to add one. A click opens the editor."""
    clicked = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("laCtxChip")
        self.setAttribute(Qt.WA_Hover, True)          # :hover in the stylesheet
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(26)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 0, 9, 0)
        lay.setSpacing(6)
        self.lock = QLabel(self)
        self.lock.hide()
        lay.addWidget(self.lock)
        self.tag = QLabel("CONTEXT", self)
        self.tag.setObjectName("laCtxTag")
        lay.addWidget(self.tag)
        self.text = _ElidedLabel("", self)
        self.text.setObjectName("laCtxText")
        lay.addWidget(self.text, 1)
        self.edit_hint = QLabel("Edit", self)
        self.edit_hint.setObjectName("laCtxEditHint")
        lay.addWidget(self.edit_hint)

    def mousePressEvent(self, e):
        e.accept()                                    # so the release comes here

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class _ContextEdit(QPlainTextEdit):
    """The session-context editor. Ctrl+Enter (⌘↩ on a Mac) or Esc finishes;
    a plain Enter is a new line."""
    finished = Signal()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape or (e.key() in (Qt.Key_Return, Qt.Key_Enter)
                                        and e.modifiers() & Qt.ControlModifier):
            self.finished.emit()
            return
        super().keyPressEvent(e)


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
    # The int is the request generation: a newer question supersedes an answer
    # still streaming, and the stale one's late signals are dropped.
    sig_suggestion = Signal(str, str, int)   # text, error, generation
    sig_status = Signal(str)                 # short footer note from the worker
    sig_partial = Signal(str, int)           # streamed text so far, generation
    sig_export_done = Signal(str, str)       # saved path, error

    def __init__(self, main_app=None):
        super().__init__()
        self.app = main_app
        cfg = self.app.cfg if self.app else {}
        self._theme_name = cfg.get("live_assist_theme", "light")
        if self._theme_name not in _THEMES:
            self._theme_name = "light"
        self._private = bool(cfg.get("live_assist_private", True))
        # Auto = answer the moment someone asks a question (on by default: an
        # instant companion is the point of the feature).
        self._auto = bool(cfg.get("live_assist_auto_answer", True))
        self._gen = 0                      # bumped by every request
        self._pending_auto = False
        self._qa_history = []              # (asked, answer) of this session
        self._audio_folder = None          # last session's meeting folder
        self._audio_ready = False
        self._blur_mode = ""
        self._glass_mode = "flat"          # "liquid" | "acrylic" | "flat"
        self._bd_body = None               # sampled backdrop, strong blur
        self._bd_ring = None               # sampled backdrop, mild blur (refraction)
        self._bd_refr = self._bd_glow = None
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
        self._image_b64 = ""               # an image attached to the next question
        self._image = None
        self._crop_shots = []              # [(screen, pixmap)] while the crop view is open
        self._crop_idx = 0
        self._crop_safe = None             # index of the card's own monitor in the shots
        # What the user wrote about the call: every answer is shaped by it.
        self._context = clip_context(str(cfg.get("live_assist_context") or ""))
        self._exclusion_ok = False
        self._mac_partial = False          # macOS: sharingType none is set (best effort)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # macOS hides Qt.Tool windows when the app loses focus - which is
        # exactly when the user is in Zoom. No-op elsewhere.
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setWindowTitle("Live Assistance")
        self.setFixedSize(EXPANDED_W, EXPANDED_H)

        self.sig_suggestion.connect(self._on_suggestion)
        self.sig_status.connect(self._set_status)
        self.sig_partial.connect(self._on_partial)
        self.sig_export_done.connect(self._on_export_done)
        self._first_token_at = 0.0
        self._build()
        self._tip_from_card = False
        self._popup_privacy = _PopupPrivacy(self)
        if QApplication.instance():
            QApplication.instance().installEventFilter(self._popup_privacy)
        self._richify_tooltips()
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
        self.lbl_title = _ElidedLabel("Live Assistance", self.bar)
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
        # Screen context, ON by default: every answer sees the screen you're
        # working on (the tooltip text is set by _on_screen_toggled).
        self.btn_screen = _IconButton("screen", self.bar, "")
        self.btn_screen.setCheckable(True)
        self.btn_screen.setChecked(
            bool((self.app.cfg if self.app else {}).get("live_assist_screen_auto", True)))
        self.btn_screen.toggled.connect(self._on_screen_toggled)
        self._set_screen_tooltip(self.btn_screen.isChecked())
        bl.addWidget(self.btn_screen)
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

        # Session context: one line showing what the answers are told about
        # this call; a click swaps it (and the transcript card) for an editor.
        self.ctx_chip = _ContextChip(self.body)
        self.ctx_chip.clicked.connect(self._open_context_editor)
        body.addWidget(self.ctx_chip)
        self.ctx_panel = QWidget(self.body)
        cp = QVBoxLayout(self.ctx_panel)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.setSpacing(8)
        ctx_head = QHBoxLayout()
        ctx_head.setSpacing(6)
        self.lbl_ctx_head = QLabel("SESSION CONTEXT", self.ctx_panel)
        ctx_head.addWidget(self.lbl_ctx_head)
        ctx_head.addStretch()
        self.lbl_ctx_count = QLabel("", self.ctx_panel)
        self.lbl_ctx_count.setObjectName("laFoot")
        ctx_head.addWidget(self.lbl_ctx_count)
        self.btn_ctx_clear = QPushButton("Clear", self.ctx_panel)
        self.btn_ctx_clear.setFixedHeight(22)
        self.btn_ctx_clear.setCursor(Qt.PointingHandCursor)
        self.btn_ctx_clear.clicked.connect(self._clear_context_text)
        ctx_head.addWidget(self.btn_ctx_clear)
        self.btn_ctx_done = QPushButton("Done", self.ctx_panel)
        self.btn_ctx_done.setObjectName("laCtxDone")
        self.btn_ctx_done.setFixedHeight(22)
        self.btn_ctx_done.setCursor(Qt.PointingHandCursor)
        self.btn_ctx_done.setToolTip("Lock it in for this session (Ctrl+Enter)"
                                     if sys.platform != "darwin"
                                     else "Lock it in for this session (⌘↩)")
        self.btn_ctx_done.clicked.connect(self._finish_context_edit)
        ctx_head.addWidget(self.btn_ctx_done)
        cp.addLayout(ctx_head)
        self.ctx_edit = _ContextEdit(self.ctx_panel)
        self.ctx_edit.setObjectName("laCtxEdit")
        self.ctx_edit.setPlaceholderText(CONTEXT_PLACEHOLDER)
        self.ctx_edit.setTabChangesFocus(True)
        # Exactly the chip + transcript slot it replaces: nothing below moves.
        self.ctx_edit.setFixedHeight(136)
        self.ctx_edit.textChanged.connect(self._on_context_typed)
        self.ctx_edit.finished.connect(self._finish_context_edit)
        cp.addWidget(self.ctx_edit)
        self.ctx_panel.hide()
        body.addWidget(self.ctx_panel)

        self.lbl_now_head = QLabel("NOW", self.body)
        body.addWidget(self.lbl_now_head)
        self.txt_now = QLabel("", self.body)
        self.txt_now.setWordWrap(True)
        self.txt_now.setObjectName("laNow")
        body.addWidget(self.txt_now)
        # Nothing to show before a session - no placeholder copy.
        self.lbl_now_head.hide()
        self.txt_now.hide()

        # The live transcript itself (not a summary): what was just said, so
        # the answer below can be checked against it at a glance.
        self.lbl_sum_head = QLabel("LIVE TRANSCRIPT", self.body)
        body.addWidget(self.lbl_sum_head)
        self.txt_summary = QTextEdit(self.body)
        self.txt_summary.setReadOnly(True)
        self.txt_summary.setObjectName("laCard")
        self.txt_summary.setMaximumHeight(110)
        self.txt_summary.setPlaceholderText("Press Start - what's said appears here.")
        body.addWidget(self.txt_summary)

        head_row = QHBoxLayout()
        self.lbl_sug_head = QLabel("ANSWER", self.body)
        self.lbl_sug_head.setToolTip("AI answers can be wrong - check anything important.")
        head_row.addWidget(self.lbl_sug_head)
        head_row.addStretch()
        # One-tap actions: each is a canned question through the same path.
        self.quick_buttons = []
        for label, question in QUICK_ACTIONS:
            b = QPushButton(label, self.body)
            b.setFixedHeight(22)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, q=question: self.suggest(
                q, force_screen=(q == SOLVE_SCREEN)))
            head_row.addWidget(b)
            self.quick_buttons.append(b)
        self.btn_auto = QPushButton("Auto", self.body)
        self.btn_auto.setCheckable(True)
        self.btn_auto.setChecked(self._auto)
        self.btn_auto.setToolTip("Answer automatically the moment someone asks a question.")
        self.btn_auto.toggled.connect(self._on_auto_toggled)
        self.btn_auto.setFixedHeight(22)
        head_row.addWidget(self.btn_auto)
        body.addLayout(head_row)
        self.txt_suggestion = QTextEdit(self.body)
        self.txt_suggestion.setReadOnly(True)
        self.txt_suggestion.setObjectName("laCard")
        self.txt_suggestion.setPlaceholderText(
            "Answers appear here the moment someone asks you something. Answer "
            "re-answers the latest question, Solve screen solves what's on your "
            "screen - or type your own question below.")
        body.addWidget(self.txt_suggestion, 1)

        ask_row = QHBoxLayout()
        ask_row.setSpacing(8)
        self.img_chip = _ImageChip(self.body)
        self.img_chip.clicked.connect(self._clear_image)
        self.img_chip.hide()
        ask_row.addWidget(self.img_chip)
        self.input_ask = _AskEdit(self.body)
        self.input_ask.setPlaceholderText(ASK_PLACEHOLDER)
        self.input_ask.returnPressed.connect(self._ask)
        self.input_ask.image_pasted.connect(self._attach_image)
        ask_row.addWidget(self.input_ask, 1)
        # Snip a part of the screen to send with the question.
        self.btn_snip = _IconButton("snip", self.body,
                                    "Screenshot: crop it right here in the card and send it "
                                    "with your question. Nothing new appears on your screen. "
                                    "Or paste an image with "
                                    + ("⌘V." if sys.platform == "darwin" else "Ctrl+V."))
        self.btn_snip.setFixedSize(34, 34)
        self.btn_snip.clicked.connect(self._start_snip)
        ask_row.addWidget(self.btn_snip)
        # A round send button with a paper-plane icon, like a chat app.
        self.btn_suggest = QPushButton("", self.body)
        self.btn_suggest.setObjectName("laSuggest")
        self.btn_suggest.setCursor(Qt.PointingHandCursor)
        self.btn_suggest.setIcon(_send_icon(18))
        self.btn_suggest.setIconSize(QSize(18, 18))
        self.btn_suggest.setFixedSize(34, 34)
        self.btn_suggest.setToolTip("Send (Enter) - with nothing typed, answers the "
                                    "latest question")
        self.btn_suggest.clicked.connect(self._ask_or_answer)
        ask_row.addWidget(self.btn_suggest)
        body.addLayout(ask_row)

        foot_row = QHBoxLayout()
        foot_row.setSpacing(8)
        self.lbl_status = QLabel("", self.body)
        self.lbl_status.setObjectName("laFoot")
        # Wrap: a long note must never widen the layout past the card.
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMaximumHeight(32)
        foot_row.addWidget(self.lbl_status, 1)
        # Appears once a session's recording is on disk.
        self.btn_audio = QPushButton("Download audio", self.body)
        self.btn_audio.setFixedHeight(22)
        self.btn_audio.setCursor(Qt.PointingHandCursor)
        self.btn_audio.setToolTip("Save this session's recording (MP3 or WAV). It's also "
                                  "kept with the meeting in History.")
        self.btn_audio.clicked.connect(self._save_audio)
        self.btn_audio.hide()
        foot_row.addWidget(self.btn_audio)
        body.addLayout(foot_row)

        root.addWidget(self.body, 1)

        # Screenshot crop view: replaces the body while it's open (see
        # _start_snip) - the whole flow stays inside this private card.
        self.crop_panel = QWidget(self)
        crl = QVBoxLayout(self.crop_panel)
        crl.setContentsMargins(0, 0, 0, 0)
        crl.setSpacing(8)
        crop_head = QHBoxLayout()
        crop_head.setSpacing(6)
        self.lbl_crop_head = QLabel("SCREENSHOT", self.crop_panel)
        crop_head.addWidget(self.lbl_crop_head)
        crop_head.addStretch()
        self.btn_crop_screen = QPushButton("", self.crop_panel)
        self.btn_crop_screen.setFixedHeight(22)
        self.btn_crop_screen.setCursor(Qt.PointingHandCursor)
        self.btn_crop_screen.setToolTip("Show the next monitor")
        self.btn_crop_screen.clicked.connect(self._crop_next_screen)
        crop_head.addWidget(self.btn_crop_screen)
        self.btn_crop_cancel = QPushButton("Cancel", self.crop_panel)
        self.btn_crop_cancel.setFixedHeight(22)
        self.btn_crop_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_crop_cancel.clicked.connect(self._end_snip)
        crop_head.addWidget(self.btn_crop_cancel)
        self.btn_crop_attach = QPushButton("", self.crop_panel)
        self.btn_crop_attach.setObjectName("laCtxDone")
        self.btn_crop_attach.setFixedHeight(22)
        self.btn_crop_attach.setCursor(Qt.PointingHandCursor)
        self.btn_crop_attach.setToolTip("Attach it to your next question (Enter)")
        self.btn_crop_attach.clicked.connect(self._crop_attach)
        crop_head.addWidget(self.btn_crop_attach)
        crl.addLayout(crop_head)
        self.crop_view = _CropView(self.crop_panel)
        self.crop_view.changed.connect(self._refresh_crop_controls)
        self.crop_view.accepted.connect(self._crop_attach)
        self.crop_view.cancelled.connect(self._end_snip)
        crl.addWidget(self.crop_view)
        self.lbl_crop_hint = QLabel("", self.crop_panel)
        self.lbl_crop_hint.setObjectName("laFoot")
        self.lbl_crop_hint.setWordWrap(True)
        crl.addWidget(self.lbl_crop_hint)
        crl.addStretch(1)
        self.crop_panel.hide()
        root.addWidget(self.crop_panel, 1)

        cfg = self.app.cfg if self.app else {}
        try:
            self.setWindowOpacity(min(1.0, max(0.55, float(cfg.get("live_assist_opacity", 0.96)))))
        except (TypeError, ValueError):
            self.setWindowOpacity(0.96)

    def _richify_tooltips(self):
        """Qt word-wraps rich-text tooltips but not plain ones: a long plain
        tooltip becomes a single ~1000 px line. Wrap every tooltip in <p>."""
        import html as html_mod
        for w in [self] + self.findChildren(QWidget):
            tip = w.toolTip()
            if tip and not tip.lstrip().startswith("<"):
                w.setToolTip(f"<p style='white-space:normal'>{html_mod.escape(tip)}</p>")

    # ── theme / glass ──
    def _apply_theme(self):
        t = _THEMES[self._theme_name]
        accent = t["accent"]
        self.setStyleSheet(f"""
            QLabel {{ color: {t['text']}; background: transparent; font-size: 12px; }}
            QToolTip {{
                background-color: #0f172a; color: #f8fafc; border: 1px solid rgba(255,255,255,0.18);
                border-radius: 6px; padding: 6px 9px; font-size: 12px;
            }}
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
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3b82f6, stop:1 #8b5cf6);
                border: none; padding: 0; border-radius: 17px;
            }}
            QPushButton#laSuggest:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2563eb, stop:1 #7c3aed);
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
            QFrame#laCtxChip {{
                background: {t['card']}; border: 1px solid {t['card_border']};
                border-radius: 9px;
            }}
            QFrame#laCtxChip:hover {{ border-color: {accent}; }}
            QFrame#laCtxChip[empty="true"] {{ border-style: dashed; }}
            QLabel#laCtxTag {{
                color: {accent}; font-size: 10px; font-weight: 700; letter-spacing: 1px;
            }}
            QLabel#laCtxText {{ color: {t['text']}; font-size: 12px; }}
            QLabel#laCtxText[empty="true"] {{ color: {t['muted']}; }}
            QLabel#laCtxEditHint {{ color: {accent}; font-size: 11.5px; }}
            QPlainTextEdit#laCtxEdit {{
                background: {t['card']}; border: 1px solid {accent};
                border-radius: 10px; color: {t['text']}; font-size: 12.5px; padding: 4px;
            }}
            QPushButton#laCtxDone {{
                background: {accent}; color: white; border-color: {accent}; font-weight: 600;
            }}
        """)
        for lbl in (self.lbl_now_head, self.lbl_sum_head, self.lbl_sug_head,
                    self.lbl_ctx_head, self.lbl_crop_head):
            lbl.setStyleSheet(f"color: {t['faint']}; font-size: 10px; font-weight: 700; "
                              "letter-spacing: 1px; background: transparent;")
        self.lbl_title.setStyleSheet(f"color: {t['text']}; font-weight: 700; font-size: 13px;")
        for b in (self.btn_theme, self.btn_collapse, self.btn_close, self.btn_screen,
                  self.btn_stop, self.btn_snip):
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
            # Visible in shares (or exclusion unavailable): the backdrop can't be
            # sampled, so paint a darker rounded plate ourselves. Not the OS
            # acrylic - Windows blurs the whole square window, shadow margin
            # included, which drew a rectangle around the rounded card.
            self._sample_timer.stop()
            self._spec_timer.stop()
            self._bd_body = self._bd_ring = self._bd_refr = self._bd_glow = None
            if self._blur_mode:
                glass.remove_backdrop_blur(self)
                self._blur_mode = ""
            self._glass_mode = "flat"
        self.update()

    def _apply_private(self):
        remote = glass.is_remote_session()
        supported = glass.capture_exclusion_supported()
        excluded = False
        self._mac_partial = False
        if (getattr(self, "_crop_shots", None) and self._crop_idx != self._crop_safe
                and not (self._private and supported and not remote)):
            self._crop_to_safe()               # before the exclusion is lifted
        if self._private and supported and not remote:
            excluded = glass.exclude_from_capture(self, True)
        elif self._private and glass.IS_MAC:
            # macOS: some capture paths honour it (the same flag Electron's
            # setContentProtection sets) - not all, so the chip says "May be
            # hidden", never "Private".
            self._mac_partial = bool(glass.exclude_from_capture(self, True)) and not remote
        else:
            glass.exclude_from_capture(self, False)
        self._exclusion_ok = excluded
        self._refresh_private_chip(remote, supported, excluded)
        if getattr(self, "_crop_shots", None):
            self._refresh_crop_controls()      # privacy changed mid-crop

    def _guard_popup(self, w):
        """A tooltip/menu about to show: hidden like the card when it belongs
        to the card and the card is hidden; restored if it was ours before."""
        parent = w.parentWidget()
        if parent is not None:
            mine = parent.window() is self
        else:
            mine = w.windowType() == Qt.ToolTip and self._tip_from_card
        hide = mine and self._private and (self._exclusion_ok or self._mac_partial)
        if hide or w.property("la_private"):
            try:
                w.winId()                      # the native window, before it maps
                glass.exclude_from_capture(w, hide)
                w.setProperty("la_private", hide)
            except RuntimeError:
                pass                           # already being deleted

    def _guard_visible_tips(self):
        for w in QApplication.topLevelWidgets():
            if w.windowType() == Qt.ToolTip and w.isVisible():
                self._guard_popup(w)

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
        "partial": ("rgba(245,158,11,0.16)", "#b45309", "rgba(245,158,11,0.5)"),
    }

    def _refresh_private_chip(self, remote=None, supported=None, excluded=None):
        if remote is None:
            remote = glass.is_remote_session()
        if supported is None:
            supported = glass.capture_exclusion_supported()
        if excluded is None:
            excluded = glass.is_excluded_from_capture(self) if self.isVisible() \
                else self._exclusion_ok
        mac = sys.platform == "darwin"
        # macOS: hiding can't be promised, but the flag may well be set.
        partial = mac and not supported and bool(excluded or self._mac_partial)
        key, _ = private_state(self._private, supported, remote, excluded, partial=partial)
        text = {"on": "Private", "off": "Visible", "failed": "Not private",
                "partial": "May be hidden",
                "unavailable": "Not hidden" if mac else "Private n/a"}[key]
        bg, fg, border = self._CHIP_COLORS[key]
        try:
            from ui.icons import eye_icon
            from PySide6.QtCore import QSize
            # Eye-off while hidden from the share, open eye when it shows.
            self.btn_private.setIcon(eye_icon(open_=(key not in ("on", "partial")), size=16,
                                              color=QColor(fg)))
            self.btn_private.setIconSize(QSize(16, 16))
        except Exception:
            pass
        self.btn_private.setText(text)
        self.btn_private.setStyleSheet(
            f"QPushButton#laPrivate {{ background: {bg}; color: {fg}; border: 1px solid "
            f"{border}; border-radius: 11px; padding: 2px 10px 2px 8px; font-size: 12px; "
            "font-weight: 600; }")
        tips = {
            "on": ("Hidden from Zoom/Teams/Meet shares, recordings and screenshots on "
                   "this PC (not from phone cameras). Click to make it visible - e.g. "
                   "to include it in your own recording."),
            "off": "This card WILL show on a shared screen. Click to hide it from shares.",
            "failed": ("macOS" if mac else "Windows") + " refused to hide this window - "
                      "assume it is visible in a share.",
            "partial": mac_partial_tip(glass.macos_26_or_later()),
            "unavailable": ("macOS didn't take the private-window setting for this "
                            "card, so assume it shows in screen shares and recordings. "
                            "Share a single window - your browser or the document - to "
                            "keep it out." if mac else
                            "Screen-share privacy needs Windows 10 2004+ and a local "
                            "(non-remote) session."),
        }
        # Rich text so Qt wraps it (a plain tooltip is one ~1000 px line).
        import html as html_mod
        self.btn_private.setToolTip("<p style='white-space:normal'>"
                                    + html_mod.escape(tips[key], quote=False) + "</p>")

    def _refresh_live_controls(self):
        live = self._meeting_active
        self.btn_start.setVisible(not live)
        self.lbl_timer.setVisible(live)
        self.btn_stop.setVisible(live)
        if live:
            self._update_timer()
        self._refresh_context_chip()

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
        # Keeps sampling during a video hole too, so it returns to liquid glass.
        if self._glass_mode not in ("liquid", "liquid-video") or not self.isVisible():
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
                # a black bar through the glass. Paint the plain rounded plate
                # while it's there (not the OS acrylic: that blurs the square
                # window and shows a rectangle); the next sample re-tests.
                if self._glass_mode == "liquid":
                    self._glass_mode = "liquid-video"
                    self._bd_body = self._bd_ring = self._bd_refr = self._bd_glow = None
                    self.update()
                return
            if self._glass_mode == "liquid-video":
                self._glass_mode = "liquid"
            body = body.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            ring = img.scaled(max(1, w // 5), max(1, h // 5), Qt.IgnoreAspectRatio,
                              Qt.SmoothTransformation)
            ring = ring.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            self._bd_refr, self._bd_glow = self._masked_refraction(ring, w, h)
            self._bd_body, self._bd_ring = body, ring
            self.update()
        except Exception as e:
            logger.debug("backdrop sample failed: %s", e)

    def _masked_refraction(self, ring, w, h):
        """The edge refraction as a pre-composed layer: the backdrop magnified
        about the centre, kept only near the card edge through a BLURRED mask
        that fades to nothing toward the middle. A hard ring boundary read as
        a stripe (very visibly on the 52 px pill); a soft mask reads as thick
        glass bending what's behind it. Returns (refraction, edge_glow)."""
        card = QRectF(SHADOW, SHADOW, w - 2 * SHADOW, h - 2 * SHADOW)
        edge = max(4.0, min(22.0, card.height() / 2 - 3, card.width() / 2 - 3))
        mask = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        mask.fill(Qt.transparent)
        mp = QPainter(mask)
        mp.setRenderHint(QPainter.Antialiasing, True)
        outer = QPainterPath()
        outer.addRoundedRect(card, RADIUS, RADIUS)
        inner = QPainterPath()
        inner.addRoundedRect(card.adjusted(edge, edge, -edge, -edge),
                             max(2.0, RADIUS - edge * 0.6), max(2.0, RADIUS - edge * 0.6))
        mp.fillPath(outer.subtracted(inner), QColor(255, 255, 255, 255))
        mp.end()
        small = mask.scaled(max(1, w // 6), max(1, h // 6), Qt.IgnoreAspectRatio,
                            Qt.SmoothTransformation)
        mask = small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

        refr = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        refr.fill(Qt.transparent)
        rp = QPainter(refr)
        rp.setRenderHint(QPainter.SmoothPixmapTransform, True)
        cx, cy = w / 2.0, h / 2.0
        rp.translate(cx, cy + 2)
        rp.scale(1.09, 1.09)
        rp.translate(-cx, -cy)
        rp.drawImage(QRect(0, 0, w, h), ring)
        rp.resetTransform()
        rp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        rp.drawImage(0, 0, mask)
        rp.end()

        glow = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        glow.fill(QColor(255, 255, 255, 34))
        gp = QPainter(glow)
        gp.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        gp.drawImage(0, 0, mask)
        gp.end()
        return refr, glow

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
            # Edge refraction, pre-composed with a soft mask in
            # _masked_refraction - no boundary anywhere, at any card size.
            refr = getattr(self, "_bd_refr", None)
            if refr is not None:
                p.drawImage(0, 0, refr)
                p.drawImage(0, 0, self._bd_glow)
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
        self._end_snip()
        self._sample_timer.stop()
        self._spec_timer.stop()
        self.hide()

    def toggle(self):
        if self.isVisible():
            self.hide_overlay()
        else:
            self.show_overlay()
            if self.app:
                self.app.track("live_prompter_opened")

    def hideEvent(self, event):
        self._end_snip()                       # no stale screenshot next time
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_glass)

    def set_expanded(self, expanded):
        self._expanded = bool(expanded)
        if not self._expanded:
            self._end_snip()
        self.body.setVisible(self._expanded)
        self.setFixedSize(EXPANDED_W if self._expanded else COMPACT_W,
                          EXPANDED_H if self._expanded else COMPACT_H)
        self.btn_collapse.set_alt(not self._expanded)
        self._shadow_key = None
        QTimer.singleShot(0, self._apply_glass)

    # ── session context ──
    def _refresh_context_chip(self):
        chip, ctx = self.ctx_chip, self._context
        locked = bool(ctx) and self._meeting_active
        chip.lock.setVisible(locked)
        if locked:
            chip.lock.setPixmap(_lock_pixmap(12, _THEMES[self._theme_name]["accent"]))
        chip.tag.setVisible(bool(ctx))
        chip.tag.setText("LOCKED IN" if locked else "CONTEXT")
        chip.text.setText(" ".join(ctx.split())[:300] if ctx else CONTEXT_EMPTY)
        chip.edit_hint.setVisible(bool(ctx))
        empty = "false" if ctx else "true"
        for w in (chip, chip.text):
            if w.property("empty") != empty:
                w.setProperty("empty", empty)          # restyle: dashed / muted
                w.style().unpolish(w)
                w.style().polish(w)
        if not ctx:
            tip = ("Tell the assistant what this call is about - who you are, who "
                   "you're talking to, what you want from it. Every answer this "
                   "session uses it.")
            chip.setToolTip(f"<p style='white-space:normal'>{tip}</p>")
            return
        import html as html_mod
        head = ("<b>Locked in for this session</b> - every answer uses it. Click to change it."
                if locked else
                "<b>Session context</b> - every answer uses it until you change it. "
                "Click to edit.")
        shown = ctx if len(ctx) <= 700 else ctx[:700] + "…"
        chip.setToolTip(f"<p style='white-space:normal'>{head}</p>"
                        f"<p style='white-space:pre-wrap'>{html_mod.escape(shown)}</p>")

    def _open_context_editor(self):
        if not self.ctx_panel.isHidden():
            return
        self.ctx_edit.blockSignals(True)
        self.ctx_edit.setPlainText(self._context)
        self.ctx_edit.blockSignals(False)
        self._on_context_typed()
        self.ctx_chip.hide()
        self.lbl_sum_head.hide()
        self.txt_summary.hide()
        self.ctx_panel.show()
        self.activateWindow()                  # typing needs the keyboard focus
        self.ctx_edit.setFocus()
        self.ctx_edit.moveCursor(self.ctx_edit.textCursor().MoveOperation.End)

    def _on_context_typed(self):
        text = self.ctx_edit.toPlainText()
        if len(text) > CONTEXT_CHARS:
            # A pasted document: keep what the model will get.
            pos = min(self.ctx_edit.textCursor().position(), CONTEXT_CHARS)
            self.ctx_edit.blockSignals(True)
            self.ctx_edit.setPlainText(text[:CONTEXT_CHARS])
            self.ctx_edit.blockSignals(False)
            cur = self.ctx_edit.textCursor()
            cur.setPosition(pos)
            self.ctx_edit.setTextCursor(cur)
            text = text[:CONTEXT_CHARS]
        n = len(text)
        self.lbl_ctx_count.setText(f"{n:,} / {CONTEXT_CHARS:,}"
                                   if n >= CONTEXT_CHARS * 0.8 else "")

    def _clear_context_text(self):
        self.ctx_edit.clear()
        self.ctx_edit.setFocus()

    def _finish_context_edit(self):
        if self.ctx_panel.isHidden():
            return
        self.ctx_panel.hide()
        self.ctx_chip.show()
        self.lbl_sum_head.show()
        self.txt_summary.show()
        self._set_context(self.ctx_edit.toPlainText())

    def _set_context(self, text):
        text = clip_context(text)
        if text == self._context:
            return
        self._context = text
        if self.app:
            self.app.cfg["live_assist_context"] = text
            self.app.save_config()
        self._refresh_context_chip()
        if not text:
            self._set_status("Context cleared.")
        elif self._meeting_active:
            self._set_status("✓ Context locked in - every answer from now on uses it.")
        else:
            self._set_status("✓ Context saved - every answer uses it.")

    # ── start / stop (drives the meeting recorder) ──
    def _start_listening(self):
        # An open context editor: what's typed is this session's context.
        self._finish_context_edit()
        mw = getattr(self.app, "meetings_win", None) if self.app else None
        if mw is None:
            self._set_status("Meeting recorder isn't available.")
            return
        try:
            state = getattr(mw, "state", None)
            if state == mw.STATE_RECORDING:
                return
            if state == getattr(mw, "STATE_PROCESSING", "processing"):
                # Restarting the shared recorder now would lose the last recording.
                self._set_status("Still saving the last session - try again in a moment.")
                return
            if hasattr(self.app, "is_pro") and not self.app.is_pro():
                if hasattr(self.app, "_pro_upsell"):
                    self.app._pro_upsell("Live Assistance")
                return
            if state == getattr(mw, "STATE_DONE", "done") and hasattr(mw, "_reset"):
                # The last session's Done page: its title, attendees and any
                # resume state belong to THAT meeting, not this one.
                mw._reset()
            if hasattr(mw, "input_title") and not mw.input_title.text().strip():
                mw.input_title.setText("Live session " + time.strftime("%H:%M"))
            # Transcription language for this session (default English), passed
            # to the meeting - the saved dictation language is never touched.
            lang = (self.app.cfg.get("live_assist_language") or "en").strip()
            # Answers can only be as live as the transcript: short speech pieces
            # for this session (the meeting's final transcript is redone in
            # full afterwards, so accuracy of the notes is unaffected).
            self._set_fast_chunks(True)
            # Wake the answer engine now, so the first question isn't a cold start.
            threading.Thread(target=self._warm_up_engine, daemon=True).start()
            # Both sides of the call, as the Start button promises: mic +
            # system sound, whatever Record Meeting is set to (when this
            # system can capture both).
            mw._start_meeting(language=lang if lang and lang != "auto" else None,
                              audio_mode="smart_meeting")
            if getattr(mw, "state", None) != mw.STATE_RECORDING:
                self._set_fast_chunks(False)
                self._set_status("Couldn't start listening - see Record Meeting.")
            else:
                self.app.track("live_prompter_started", {
                    "language": lang or "auto",
                    "screen_auto": self.btn_screen.isChecked(),
                    "auto_suggest": self._auto,
                    "context": bool(self._context),
                })
        except Exception as e:
            logger.warning("Live Assistance start failed: %s", e, exc_info=True)
            self._set_fast_chunks(False)
            self._set_status(f"Couldn't start: {str(e)[:80]}")

    def _set_fast_chunks(self, on):
        rec = getattr(self.app, "recorder", None) if self.app else None
        if rec is not None:
            rec.fast_live_chunks = bool(on)

    def _warm_up_engine(self):
        # Worker thread. A scale-to-zero GPU endpoint (a dedicated Modal model) can take
        # a while to boot; this request starts it while the call gets going.
        try:
            engine, cfg = self._resolve_engine()
            if engine is not None:
                actions.warm_up(engine, cfg)
        except Exception:
            logger.debug("engine warm-up failed", exc_info=True)

    def _stop_listening(self):
        mw = getattr(self.app, "meetings_win", None) if self.app else None
        if mw is None or getattr(mw, "state", None) != mw.STATE_RECORDING:
            return
        try:
            mw._stop_meeting()
            self._set_status("Stopped - the notes are generated in Record Meeting.")
        except Exception as e:
            logger.warning("Live Assistance stop failed: %s", e, exc_info=True)
            self._set_status(f"Couldn't stop: {str(e)[:80]}")

    # ── data feed (GUI thread) ──
    def set_meeting_active(self, active, title="", attendees=""):
        self._meeting_active = bool(active)
        if active:
            # Started from Record Meeting with the editor open: what's typed
            # is this session's context.
            self._finish_context_edit()
            self._live_since = time.time()
            self._live_text = ""
            self._summary = ""
            self._qa_history = []
            self._audio_folder, self._audio_ready, self._audio_size = None, False, -1
            self._audio_polls = 0
            self.btn_audio.hide()
            self._title, self._attendees = title or "", attendees or ""
            self.txt_summary.clear()
            self.txt_summary.setPlaceholderText("Listening…")
            self.lbl_status.setText("Context locked in for this session." if self._context
                                    else "")
        else:
            self._set_fast_chunks(False)
            # The recording lands in the meeting folder moments after Stop;
            # _on_tick shows Download audio once THIS session's part is fully
            # written - a resumed meeting already has earlier parts, so count
            # them now (the save worker hasn't started yet).
            mw = getattr(self.app, "meetings_win", None) if self.app else None
            self._audio_folder = getattr(mw, "_meeting_dir", None)
            self._audio_base = len(self._audio_parts()) if self._audio_folder else 0
            self._audio_polls, self._audio_size = 0, -1
            self.lbl_status.setText("Stopped - saving the recording and the notes…")
        self._refresh_live_controls()

    def discard_session_audio(self):
        """The session was discarded: nothing to offer for download."""
        self._audio_folder, self._audio_ready = None, False
        self.btn_audio.hide()
        self._set_status("Session discarded.")

    def feed_transcript(self, piece, answer=True):
        """A newly transcribed piece of the call. ``answer=False`` for bulk
        catch-up text (a resumed meeting, opening mid-call) - old questions in
        it must not trigger answers."""
        piece = (piece or "").strip()
        if not piece:
            return
        self._live_text = (self._live_text + " " + piece).strip()
        self._text_since_suggest += len(piece)
        tail = self._live_text[-700:]
        if len(self._live_text) > 700 and " " in tail:
            tail = tail.split(" ", 1)[1]
        self.txt_summary.setPlainText(tail)
        sb = self.txt_summary.verticalScrollBar()
        sb.setValue(sb.maximum())
        # The companion part: someone just asked something - answer right away.
        # Only while the overlay is on screen: a hidden overlay must never
        # grab screenshots or send the call to an AI behind the user's back
        # (it stays connected to every meeting once it has been opened).
        if (answer and self._auto and self._meeting_active and self.isVisible()
                and looks_like_question(piece)):
            self.suggest("", auto=True)

    def set_summary(self, text):
        # The meeting window's rolling recap isn't shown here: the overlay is
        # for answers, and the live transcript beats a recap mid-call.
        self._summary = text or ""

    # ── suggestions ──
    def _set_status(self, text):
        self.lbl_status.setText(text or "")

    def _ask(self):
        q = self.input_ask.text().strip()
        image, image_b64 = self._image, self._image_b64
        if not q and not image_b64:
            return
        gen = self._gen
        self.suggest(q or SNIP_QUESTION, image_b64=image_b64)
        if self._gen == gen:
            return                            # nothing went out
        # Sent: empty the box at once and say so - the answer streams below.
        # (If it fails, the question and image come back.)
        self._sent_question = q
        self._sent_image = image if image_b64 else None
        self._clear_image()
        self.input_ask.clear()
        self.input_ask.setPlaceholderText(ASK_SENT)
        what = q or "the image"
        extra = " + image" if (q and image_b64) else ""
        self._set_status("✓ Sent: " + (what if len(what) <= 62 else what[:59] + "…") + extra)
        self.txt_suggestion.setPlainText(f"You asked: {q or 'about the image'}{extra}\n\nThinking…")

    def _ask_or_answer(self):
        # The Ask button: the typed question and/or the attached image if there
        # is one, else answer the latest question of the call.
        if self.input_ask.text().strip() or self._image_b64:
            self._ask()
        else:
            self.suggest("")

    # ── images: snip a part of the screen, or paste one ──
    def _images_blocked(self):
        if self.app and self.app.cfg.get("privacy_mode"):
            self._set_status("Images can't be sent in Privacy Mode - answers stay on this "
                             "computer.")
            return True
        return False

    def _attach_image(self, img):
        if img is None or img.isNull() or self._images_blocked():
            return
        b64 = jpeg_b64(img, 1600, 90)
        if not b64:
            return
        self._image, self._image_b64 = QImage(img), b64
        self.img_chip.set_image(img)
        self.img_chip.show()
        self._set_status("✓ Image attached - ask about it, or just press send.")
        if self.crop_panel.isHidden():
            # Not while cropping: an image handed back by a failed answer
            # would move Enter/Esc from the crop view to the hidden ask box.
            self.input_ask.setFocus()

    def _clear_image(self):
        self._image, self._image_b64 = None, ""
        self.img_chip.hide()

    def _start_snip(self):
        """Screenshot, cropped inside the card. The grab is silent (like
        Solve screen) and the card itself is left out of it while Private."""
        if self._crop_shots or self._images_blocked():
            return
        shots = []
        for scr in QApplication.screens():
            try:
                pm = scr.grabWindow(0)
            except Exception:
                pm = QPixmap()
            if not pm.isNull():
                shots.append((scr, pm))
        if not shots:
            self._set_status("Couldn't capture the screen - try again.")
            return
        # Only the card's own monitor may show in the card while a share can
        # see the card (a share of that monitor shows it anyway); another one
        # - e.g. the one under the cursor - only while the card is hidden.
        own = self.screen()
        safe = next((i for i, (scr, _pm) in enumerate(shots) if scr is own), None)
        if safe is None and not self._exclusion_ok:
            self._set_status("Couldn't capture this screen - try again.")
            return
        under = screen_to_capture(own)
        start = safe
        if self._exclusion_ok:
            start = next((i for i, (scr, _pm) in enumerate(shots) if scr is under),
                         0 if safe is None else safe)
        self._crop_shots = shots
        self._crop_safe = safe
        self._crop_idx = start
        if not self._expanded:
            self.set_expanded(True)
        self.body.hide()
        self.crop_panel.show()
        self._show_crop_shot()
        self.activateWindow()                  # Enter / Esc go to the crop view
        self.crop_view.setFocus()

    def _show_crop_shot(self):
        self.crop_view.set_shot(self._crop_shots[self._crop_idx][1])
        n = len(self._crop_shots)
        self.btn_crop_screen.setVisible(n > 1)
        self.btn_crop_screen.setText(f"Screen {self._crop_idx + 1} of {n}")
        self._refresh_crop_controls()

    def _crop_next_screen(self):
        if self._crop_shots:
            self._crop_idx = (self._crop_idx + 1) % len(self._crop_shots)
            self._show_crop_shot()
            self.crop_view.setFocus()

    def _crop_to_safe(self):
        """Back to the card's own monitor (or out of the crop if that one
        couldn't be captured), painted NOW - before the card can be seen."""
        if self._crop_safe is None:
            self._end_snip()
            self._set_status("Screenshot closed - this card is visible in screen sharing now.")
            return
        self._crop_idx = self._crop_safe
        self._show_crop_shot()
        self.crop_view.repaint()

    def _refresh_crop_controls(self):
        if self._crop_shots and not self._exclusion_ok and self._crop_idx != self._crop_safe:
            # The card is visible to a share while it shows another monitor.
            self._crop_to_safe()               # comes back here, or ends the crop
            return
        # Showing ANOTHER monitor's screenshot in a card that a share can see
        # would put an unshared screen into the share: only while hidden.
        self.btn_crop_screen.setEnabled(self._exclusion_ok)
        self.btn_crop_screen.setToolTip(
            "Show the next monitor" if self._exclusion_ok else
            "Only while this card is hidden from screen sharing - otherwise that "
            "monitor would show in your share.")
        picked = self.crop_view.has_selection()
        self.btn_crop_attach.setText("Attach selection" if picked else "Attach whole screen")
        hint = ("Drag again to change it, or click once for the whole screen."
                if picked else "Drag over the part you want, or attach the whole screen.")
        if not self._exclusion_ok:
            hint += ("  Heads-up: this card may show in some screen shares and recordings."
                     if self._mac_partial else
                     "  Heads-up: this card isn't hidden from screen sharing right now, "
                     "so this screenshot shows in your share too.")
        self.lbl_crop_hint.setText(hint)

    def _crop_attach(self):
        if not self._crop_shots:
            return
        self._on_snip_picked(self.crop_view.crop_image())

    def _end_snip(self):
        if not self._crop_shots and self.crop_panel.isHidden():
            return
        self._crop_shots = []
        self._crop_safe = None
        self.crop_view.set_shot(QPixmap())     # don't keep the screen in memory
        self.body.setVisible(self._expanded)
        self.crop_panel.hide()
        # Hiding the focused crop view hands focus to the next widget in the
        # chain - the header's Stop/Start button, where a Space would stop or
        # start the recording. Put it back in the ask box.
        if self._expanded and self.isVisible():
            self.input_ask.setFocus()

    def _on_snip_picked(self, img):
        self._end_snip()
        self._attach_image(img)
        self.activateWindow()                  # type the question right away

    def suggest(self, question="", auto=False, force_screen=False, image_b64=""):
        """Answer now. A newer request supersedes one still streaming - the
        freshest question is the one that matters in a live call.
        ``image_b64``: an image the user attached (a snip or a pasted one) -
        sent instead of the automatic screenshot."""
        if not self.app:
            self._on_suggestion("", "No app context.")
            return
        if image_b64 and self.app.cfg.get("privacy_mode"):
            self._images_blocked()
            return
        if auto and ((self._suggesting and not getattr(self, "_suggest_auto", False))
                     or time.time() < getattr(self, "_hold_until", 0.0)):
            # The user asked for the answer on screen (typed, Solve screen,
            # Follow-ups...): a question heard meanwhile waits its turn
            # (_on_tick) instead of cutting that answer off or replacing it.
            self._pending_auto = True
            return
        screen_on = self.btn_screen.isChecked()
        if not self._live_text.strip() and not question:
            if screen_on:
                # Nothing said yet: the screen is the only context there is.
                question, force_screen = SOLVE_SCREEN, True
            else:
                self.txt_suggestion.setPlainText(
                    "Nothing has been said yet - press Start, or type a question.")
                return
        attached = bool(image_b64)
        # Privacy Mode is on-device only: answers come from a local, text-only
        # model, so a screenshot could only ever leave via cloud OCR - none.
        if (not attached and should_attach_screen(screen_on, force_screen)
                and not self.app.cfg.get("privacy_mode")):
            image_b64 = capture_screen_b64(screen_to_capture(self.screen()))
        if force_screen and not image_b64:
            # Asking a model to "solve the screen" without one only gets a
            # confident, made-up answer.
            self.txt_suggestion.setPlainText(
                "Solve screen is off in Privacy Mode - the screen never leaves this "
                "computer." if self.app.cfg.get("privacy_mode")
                else "Couldn't capture the screen - try again.")
            return
        self._gen += 1
        gen = self._gen
        self._pending_auto = False
        self._screen_note = ""                # this answer's screen status, if any
        self._suggest_auto = auto
        self._suggest_asked = bool(question)
        self._sent_question = ""              # _ask() sets it for a typed question
        self._sent_image = None
        self._suggest_question = question or last_question(self._live_text)
        self._last_attached_screen = bool(image_b64)
        self._suggesting = True
        self._suggest_started = time.time()
        self._first_token_at = 0.0
        self._text_since_suggest = 0
        self.txt_suggestion.setPlainText("Thinking…")
        context = rolling_context(
            self._live_text, question, self._title, self._attendees,
            screen="snip" if attached else bool(image_b64),
            output_lang=(self.app.cfg.get("live_assist_output_language") or "en"),
            history=self._qa_history, session_context=self._context)
        threading.Thread(target=self._suggest_worker, args=(context, image_b64, gen),
                         daemon=True).start()

    def _resolve_engine(self):
        """Engine for answers (see actions.live_engine): the Pro cloud for a Pro
        user even if a small offline model is set for Smart Actions, a cloud
        engine with its own key, else a downloaded local model. (None, cfg)
        when no REAL engine can run - there is deliberately no rule-based
        fallback."""
        engine, cfg = self.app._resolve_action_engine()
        return actions.live_engine(engine, cfg), cfg

    def _suggest_worker(self, context, image_b64, gen):
        try:
            engine, cfg = self._resolve_engine()
            if engine is None:
                self.sig_suggestion.emit("", actions.NO_ENGINE_MESSAGE, gen)
                return
            kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
            cfg = dict(cfg or {})
            image_status = {}
            if image_b64 and kind in ("cloud", "managed"):
                cfg["_image_png_b64"] = image_b64
                cfg["_image_status"] = image_status   # set if the model refused it
            elif image_b64:
                # Text-only engine (local model): read the screen with Mistral
                # OCR when a Mistral key exists, so screen context still works -
                # never in Privacy Mode (belt and braces: suggest() already
                # doesn't capture then).
                mkey = ("" if cfg.get("privacy_mode")
                        else (cfg.get("mistral_api_key") or "").strip())
                ocr = actions.action_api.mistral_ocr(image_b64, mkey) if mkey else ""
                # The model gets no image: drop the note that says it does.
                context = actions.action_api._SCREEN_NOTE.sub("", context)
                self._last_attached_screen = bool(ocr)
                if ocr:
                    screen = "Text visible on the user's screen (OCR):\n" + ocr[:3000]
                    self._note_screen(gen, "Screen read via OCR")
                else:
                    screen = actions.action_api._NO_SCREEN_NOTE
                    self._note_screen(gen, "Screen needs a cloud AI engine (Pro or your own "
                                           "key) - answered from the transcript only.")
                head, sep, question = context.rpartition("\n\nUser's question: ")
                context = (f"{head}\n\n{screen}{sep}{question}" if sep
                           else f"{context}\n\n{screen}")
            acc = []
            last_emit = [0.0]

            def on_token(delta):
                if gen != self._gen:
                    raise _Superseded()           # a newer question took over
                if isinstance(delta, actions.action_api.ReplaceText):
                    acc[:] = [str(delta)]         # what streamed so far was reasoning
                    last_emit[0] = 0.0
                    # "First words" counts the answer, not the reasoning before it.
                    self._first_token_at = time.time() if str(delta).strip() else 0.0
                else:
                    acc.append(delta)
                    if not self._first_token_at and delta.strip():
                        self._first_token_at = time.time()
                now = time.time()
                if now - last_emit[0] >= 0.05:      # ~20 UI updates/s
                    last_emit[0] = now
                    self.sig_partial.emit("".join(acc), gen)

            text = actions.process_stream(context, actions.ACTION_LIVE_ASSIST, on_token,
                                          model=engine, config=cfg)
            if image_status.get("dropped"):
                # The engine can't read images: the answer came from the
                # conversation alone - don't claim "screen seen".
                self._last_attached_screen = False
                self._note_screen(gen, "Screen not read - this AI engine can't see images; "
                                       "answered from the conversation only.")
            self.sig_suggestion.emit((text or "".join(acc)).strip(), "", gen)
        except _Superseded:
            return
        except Exception as e:
            self.sig_suggestion.emit("", str(e)[:240], gen)

    def _note_screen(self, gen, text):
        """Worker thread: a screen status for answer ``gen`` only - it shows
        in place of that answer's "Answered..." line and is gone with the next
        question (an older, superseded worker changes nothing)."""
        if gen != self._gen:
            return
        self._screen_note = text
        self.sig_status.emit(text)

    def _on_partial(self, text, gen=None):
        if not self._suggesting or (gen is not None and gen != self._gen):
            return
        # (_first_token_at is set by the worker, from answer text only.)
        self.txt_suggestion.setPlainText(text)
        sb = self.txt_suggestion.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_suggestion(self, text, error, gen=None):
        if gen is not None and gen != self._gen:
            return      # the answer to an older question - superseded
        self._suggesting = False
        self.input_ask.setPlaceholderText(ASK_PLACEHOLDER)
        sent, self._sent_question = getattr(self, "_sent_question", ""), ""
        self._last_suggest_at = time.time()
        took = time.time() - self._suggest_started if self._suggest_started else 0
        if self.app:
            self.app.track("live_prompter_suggestion", {
                "ok": not error,
                "auto": bool(getattr(self, "_suggest_auto", False)),
                "asked": getattr(self, "_suggest_asked", False),
                "screen": bool(getattr(self, "_last_attached_screen", False)),
                "first_words_seconds": (round(self._first_token_at - self._suggest_started, 1)
                                        if self._first_token_at else None),
                "seconds": round(took, 1),
            })
        if error:
            self.txt_suggestion.setPlainText(f"Couldn't get an answer: {error}")
            if sent and not self.input_ask.text():
                self.input_ask.setText(sent)  # nothing lost: Enter sends it again
            sent_image = getattr(self, "_sent_image", None)
            self._sent_image = None
            if sent_image is not None and not self._image_b64:
                self._attach_image(sent_image)
            return
        render_markdown(self.txt_suggestion, text.strip() or "(no answer)")
        if text.strip():
            self._qa_history = (self._qa_history
                                + [(getattr(self, "_suggest_question", ""), text.strip())]
                                )[-HISTORY_TURNS:]
            if not getattr(self, "_suggest_auto", False):
                # Give the user time to read what they asked for before an
                # automatic answer may replace it.
                self._hold_until = time.time() + USER_ANSWER_HOLD_S
        note = getattr(self, "_screen_note", "")
        if note:
            self.lbl_status.setText(note)
        else:
            first = (f"first words {self._first_token_at - self._suggest_started:.1f}s · "
                     if self._first_token_at else "")
            shot = "screen seen · " if getattr(self, "_last_attached_screen", False) else ""
            self.lbl_status.setText(
                f"Answered {time.strftime('%H:%M:%S')} · {first}done {took:.1f}s · {shot}"
                "AI can be wrong - check facts")

    def _on_auto_toggled(self, on):
        self._auto = bool(on)
        if self.app:
            self.app.cfg["live_assist_auto_answer"] = self._auto
            self.app.save_config()

    def _set_screen_tooltip(self, on):
        self.btn_screen.setToolTip(
            "Screen context: On. Every answer sees the screen you're working on "
            "(this card left out), so a question, task or error on it gets solved. "
            "Needs an AI engine that can read images. Click to turn off." if on else
            "Screen context: Off. Answers never see your screen - except when you "
            "press Solve screen. Click to turn on.")

    def _on_screen_toggled(self, on):
        self.btn_screen.update()
        self._set_screen_tooltip(on)
        self._richify_tooltips()
        if self.app:
            self.app.cfg["live_assist_screen_auto"] = bool(on)
            self.app.save_config()

    # ── the session's recording ──
    def _audio_parts(self):
        try:
            import meeting_store
            return meeting_store.audio_parts(self._audio_folder)
        except Exception:
            return []

    def _check_audio_ready(self):
        """Show Download audio once THIS session's recording is on disk and
        has stopped growing (it's written right after Stop). Gives up after a
        couple of minutes - a session too short to save writes nothing."""
        try:
            parts = self._audio_parts()
            if len(parts) <= getattr(self, "_audio_base", 0):
                self._audio_polls = getattr(self, "_audio_polls", 0) + 1
                if self._audio_polls > 150:
                    self._audio_folder = None
                return
            size = sum(p.stat().st_size for p in parts)
        except Exception:
            self._audio_folder = None
            return
        if size and size == getattr(self, "_audio_size", -1):
            self._audio_ready = True
            self.btn_audio.show()
        self._audio_size = size

    def _save_audio(self):
        import meeting_store
        from ui.meeting_detail import recording_save_path
        parts = meeting_store.audio_parts(self._audio_folder) if self._audio_folder else []
        if not parts:
            self._set_status("No recording found for this session.")
            return
        path = recording_save_path(self, "Live session " + time.strftime("%Y-%m-%d %H-%M"))
        if not path:
            return
        self.btn_audio.setEnabled(False)
        self._set_status("Saving the recording…")

        def _worker():
            try:
                import audio_export
                out = audio_export.export_recording(parts, path)
                self.sig_export_done.emit(str(out) if out else "", "" if out else "Nothing to export")
            except Exception as e:
                self.sig_export_done.emit("", str(e)[:200])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_export_done(self, path, error):
        self.btn_audio.setEnabled(True)
        if error:
            self._set_status(f"Couldn't save the recording: {error}")
            return
        import os
        self._set_status(f"Recording saved: {os.path.basename(path)}")
        if self.app:
            self.app.track("meeting_exported", {"target": "audio", "ok": True})

    def _on_tick(self):
        if (getattr(self, "_pending_auto", False) and not self._suggesting
                and time.time() >= getattr(self, "_hold_until", 0.0)):
            self._pending_auto = False
            if self._auto and self._meeting_active and self.isVisible():
                self.suggest("", auto=True)
        # Watchdog: Qt can recreate the native window (flag/parent changes)
        # and the exclusion lives on the HWND - re-apply if it went missing.
        if (self._private and self.isVisible()
                and (glass.capture_exclusion_supported() or glass.IS_MAC)
                and not glass.is_remote_session()
                and not glass.is_excluded_from_capture(self)):
            self._apply_glass()
        if self._meeting_active:
            self._update_timer()
        if self._audio_folder and not self._audio_ready:
            self._check_audio_ready()
        if self._suggesting:
            el = int(time.time() - self._suggest_started)
            if el >= 8 and not self._first_token_at:
                # A scale-to-zero model endpoint booting after a pause.
                self.txt_suggestion.setPlainText(
                    f"Waking up the AI model… {el}s (only the first answer after a "
                    "pause is slow)")
            elif el >= 3 and not self._first_token_at:
                self.txt_suggestion.setPlainText(f"Thinking… {el}s")
