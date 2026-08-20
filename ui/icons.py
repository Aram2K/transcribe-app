"""Small painted icons shared across windows.

Painted with QPainter (not font glyphs) so they render identically on every
Windows install - same reasoning as the feedback thumbnails' remove badge.
"""

import os
import sys

from PySide6.QtCore import Qt, QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_ICON_CACHE = {}

_SLATE = QColor("#475569")
SLATE = _SLATE          # public alias: the app's standard icon grey

# Qt draws a combo-box item's icon hard against its text. There is no styleable
# icon-to-text gap, so the space is baked into the pixmap as a transparent
# gutter on the right - and the widget's iconSize must match that padded aspect
# or Qt scales the whole thing down and the gap disappears with it.
MEETING_ICON_H = 22
_MEETING_GUTTER = 0.40


def meeting_icon_qsize():
    """The iconSize to pair with :func:`meeting_mode_icon`."""
    return QSize(int(MEETING_ICON_H * (1 + _MEETING_GUTTER)), MEETING_ICON_H)


# Meeting-capture labels as (glyph, text) runs, so each glyph can be painted
# beside the word it stands for instead of both being bunched into one leading
# icon. A combo item carries a single icon that Qt always draws hard-left, so
# an inline glyph has to be painted by hand - see ui/mode_combo.py. Defined
# once here and shared by Settings and the Record Meeting window.
MEETING_MODE_SEGMENTS = {
    "smart_meeting": (
        ("speaker", "System sound"),
        (None, "+"),
        ("mic", "Microphone"),
        (None, "(best for meetings)"),
    ),
    "default_mic": (
        ("mic", "Microphone only"),
    ),
    "system_only": (
        ("speaker", "System sound only"),
        (None, "(no microphone)"),
    ),
}


# Drop-in artwork: if assets/mic.png / assets/speaker.png exist they are used
# instead of the painted glyphs, scaled smoothly to the needed size and tinted
# to the current colour (so they still read on a selected blue row). Missing
# files simply fall back to the painter, so nothing breaks either way.
_GLYPH_FILES = {"mic": "mic.png", "speaker": "speaker.png"}


def _assets_dir():
    base = getattr(sys, "_MEIPASS", None)          # PyInstaller one-folder build
    if base:
        return os.path.join(base, "assets")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets")


def _glyph_pixmap(kind, size, color):
    """Scaled + tinted artwork for ``kind``, or None when there is no file."""
    key = ("glyphpm", kind, size, color.name())
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    name = _GLYPH_FILES.get(kind)
    if not name:
        return None
    path = os.path.join(_assets_dir(), name)
    if not os.path.isfile(path):
        return None
    src = QPixmap(path)
    if src.isNull():
        return None
    scaled = src.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    # Recolour through the alpha channel: the art is a flat silhouette, so
    # painting the colour over it with SourceIn keeps the shape and drops the
    # baked-in grey.
    tinted = QPixmap(scaled.size())
    tinted.fill(Qt.transparent)
    tp = QPainter(tinted)
    tp.setRenderHint(QPainter.Antialiasing, True)
    tp.drawPixmap(0, 0, scaled)
    tp.setCompositionMode(QPainter.CompositionMode_SourceIn)
    tp.fillRect(tinted.rect(), color)
    tp.end()
    _ICON_CACHE[key] = tinted
    return tinted


def draw_mode_glyph(painter, kind, rect, color=_SLATE):
    """Paint one meeting-mode glyph into ``rect``. Unknown kinds draw nothing."""
    pm = _glyph_pixmap(kind, int(round(max(rect.width(), rect.height()))), color)
    if pm is not None:
        x = rect.x() + (rect.width() - pm.width()) / 2.0
        y = rect.y() + (rect.height() - pm.height()) / 2.0
        painter.drawPixmap(int(round(x)), int(round(y)), pm)
        return
    if kind == "mic":
        _draw_mic(painter, rect, color)
    elif kind == "speaker":
        _draw_speaker(painter, rect, color)


def _dpr():
    """The screen's device-pixel ratio (1.0 at 100% scaling, 1.5 at 150%...)."""
    try:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        return float(screen.devicePixelRatio()) if screen else 1.0
    except Exception:
        return 1.0


def _new_pixmap(width, height):
    """A pixmap sized in LOGICAL units but backed by real device pixels.

    Painting a fixed-size pixmap and letting Qt rescale it at draw time is what
    made the dropdown pills look soft: they were drawn 2x then squeezed back
    down, and squeezed again on a scaled display. Tagging the pixmap with the
    device-pixel ratio instead means it is rendered at native resolution and
    blitted 1:1, while the painter still works in logical coordinates.
    """
    dpr = _dpr()
    pm = QPixmap(max(1, int(round(width * dpr))), max(1, int(round(height * dpr))))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    return pm


def _painter(pm):
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    return p


def _draw_mic(p, rect, color=_SLATE):
    """Classic studio mic: capsule + cradle arc + stand."""
    w = rect.width()
    pen = QPen(color)
    pen.setWidthF(max(1.6, w * 0.07))
    pen.setCapStyle(Qt.RoundCap)

    h = rect.height()
    cx = rect.x() + w / 2

    # Capsule: tall and solid, filling most of the upper two thirds.
    cap_w = w * 0.38
    cap_h = h * 0.66
    cap_x = cx - cap_w / 2
    cap_y = rect.y() + h * 0.02
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(cap_x, cap_y, cap_w, cap_h), cap_w / 2, cap_w / 2)

    # Cradle: a wide U cupping the capsule's lower half.
    p.setBrush(Qt.NoBrush)
    p.setPen(pen)
    arc_w = w * 0.66
    arc_h = h * 0.60
    arc_y = rect.y() + h * 0.36
    p.drawArc(QRectF(cx - arc_w / 2, arc_y, arc_w, arc_h), 180 * 16, 180 * 16)

    # Short stem down to a wide base bar.
    base_y = rect.y() + h * 0.96
    p.drawLine(QPointF(cx, arc_y + arc_h * 0.5), QPointF(cx, base_y))
    p.drawLine(QPointF(cx - w * 0.19, base_y), QPointF(cx + w * 0.19, base_y))


def _draw_speaker(p, rect, color=_SLATE):
    """Speaker: box + cone + two sound arcs."""
    w = rect.width()
    h = rect.height()
    x0 = rect.x()
    y0 = rect.y()

    # Cone (driver box + flare) as one filled path.
    path = QPainterPath()
    box_x = x0 + w * 0.04
    box_w = w * 0.18
    box_y = y0 + h * 0.32
    box_h = h * 0.36
    flare_x = x0 + w * 0.46
    path.moveTo(box_x, box_y)
    path.lineTo(box_x + box_w, box_y)
    path.lineTo(flare_x, y0 + h * 0.04)         # flare top
    path.lineTo(flare_x, y0 + h * 0.96)         # flare bottom
    path.lineTo(box_x + box_w, box_y + box_h)
    path.lineTo(box_x, box_y + box_h)
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(path)

    # Two open sound arcs to the right of the cone.
    pen = QPen(color)
    pen.setWidthF(max(1.7, w * 0.085))
    pen.setCapStyle(Qt.RoundCap)
    p.setBrush(Qt.NoBrush)
    p.setPen(pen)
    cx = flare_x
    cy = y0 + h / 2
    for r in (w * 0.24, w * 0.44):
        p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), -55 * 16, 110 * 16)


def _text_pill_icon(text, bg, fg="#ffffff", width=44, height=20):
    """Small colored pill with short uppercase text, for marking dropdown items.

    Rendered at the screen's native resolution (see :func:`_new_pixmap`) and
    drawn at exactly ``width`` x ``height``, so there is no rescale anywhere in
    the path and the text stays sharp.
    """
    key = ("pill", text, bg, width, height, _dpr())
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    pm = _new_pixmap(width, height)
    p = _painter(pm)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(bg))
    p.drawRoundedRect(QRectF(0, 0, width, height), height / 2.0, height / 2.0)
    f = p.font()
    f.setBold(True)
    f.setPixelSize(max(9, int(height * 0.55)))
    p.setFont(f)
    p.setPen(QColor(fg))
    p.drawText(QRectF(0, 0, width, height), Qt.AlignCenter, text)
    p.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon


def pro_pill_icon(width=40, height=20):
    """Purple 'PRO' pill - marks Pro-only choices."""
    return _text_pill_icon("PRO", "#a855f7", width=width, height=height)


def local_pill_icon(width=48, height=20):
    """Green 'LOCAL' pill - marks fully on-device choices (privacy-friendly)."""
    return _text_pill_icon("LOCAL", "#16a34a", width=width, height=height)


def cloud_pill_icon(width=50, height=20):
    """Blue 'CLOUD' pill - marks bring-your-own-key cloud engines."""
    return _text_pill_icon("CLOUD", "#0284c7", width=width, height=height)


def eye_icon(open_=True, size=22, color=_SLATE):
    """Eye for password fields: open (click to reveal) or slashed (click to hide)."""
    key = ("eye", open_, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    pm = _new_pixmap(size, size)
    p = _painter(pm)
    w = float(size)
    cy = w / 2
    pen = QPen(color)
    pen.setWidthF(max(1.4, w * 0.085))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    # Eye outline (flattened ellipse) + filled pupil.
    p.drawEllipse(QRectF(w * 0.08, cy - w * 0.22, w * 0.84, w * 0.44))
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(w / 2, cy), w * 0.11, w * 0.11)
    if not open_:
        p.setPen(pen)
        p.drawLine(QPointF(w * 0.16, w * 0.86), QPointF(w * 0.84, w * 0.14))
    p.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon


def meeting_mode_icon(mode, size=32):
    """Icon for a meeting capture mode: 'smart_meeting' (mic + speaker),
    'default_mic' (mic only), 'system_only' (speaker only)."""
    key = (mode, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    # Wider than tall: the glyphs occupy the leading square, the remainder is a
    # transparent gutter that keeps the icon off the label text.
    pm = QPixmap(int(size * (1 + _MEETING_GUTTER)), size)
    pm.fill(Qt.transparent)
    p = _painter(pm)
    full = QRectF(0, 0, size, size)
    if mode == "default_mic":
        _draw_mic(p, full.adjusted(size * 0.14, size * 0.06, -size * 0.14, -size * 0.06))
    elif mode == "system_only":
        _draw_speaker(p, full.adjusted(size * 0.10, size * 0.12, -size * 0.10, -size * 0.12))
    else:
        # smart_meeting reads "System sound + Microphone", so the speaker comes
        # first and the mic second - matching the words left to right.
        _draw_speaker(p, QRectF(size * 0.00, size * 0.22, size * 0.48, size * 0.60))
        _draw_mic(p, QRectF(size * 0.54, size * 0.10, size * 0.46, size * 0.84))
    p.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon
