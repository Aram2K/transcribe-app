"""Small painted icons shared across windows.

Painted with QPainter (not font glyphs) so they render identically on every
Windows install - same reasoning as the feedback thumbnails' remove badge.
"""

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_ICON_CACHE = {}

_SLATE = QColor("#475569")


def _painter(pm):
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    return p


def _draw_mic(p, rect, color=_SLATE):
    """Classic studio mic: capsule + cradle arc + stand."""
    w = rect.width()
    pen = QPen(color)
    pen.setWidthF(max(1.6, w * 0.07))
    pen.setCapStyle(Qt.RoundCap)

    # Capsule (filled rounded rect).
    cap_w = w * 0.30
    cap_h = rect.height() * 0.46
    cap_x = rect.x() + (w - cap_w) / 2
    cap_y = rect.y() + rect.height() * 0.06
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(cap_x, cap_y, cap_w, cap_h), cap_w / 2, cap_w / 2)

    # Cradle arc around the capsule's lower half.
    p.setBrush(Qt.NoBrush)
    p.setPen(pen)
    arc_w = w * 0.56
    arc_h = rect.height() * 0.52
    arc_x = rect.x() + (w - arc_w) / 2
    arc_y = cap_y + cap_h - arc_h * 0.62
    p.drawArc(QRectF(arc_x, arc_y, arc_w, arc_h), 200 * 16, 140 * 16)

    # Stand: vertical stem + base.
    cx = rect.x() + w / 2
    stem_top = arc_y + arc_h * 0.92
    base_y = rect.y() + rect.height() * 0.94
    p.drawLine(QPointF(cx, stem_top), QPointF(cx, base_y))
    p.drawLine(QPointF(cx - w * 0.18, base_y), QPointF(cx + w * 0.18, base_y))


def _draw_speaker(p, rect, color=_SLATE):
    """Speaker: box + cone + two sound arcs."""
    w = rect.width()
    h = rect.height()
    x0 = rect.x()
    y0 = rect.y()

    # Cone (driver box + flare) as one filled path.
    path = QPainterPath()
    box_x = x0 + w * 0.06
    box_w = w * 0.20
    box_y = y0 + h * 0.34
    box_h = h * 0.32
    path.moveTo(box_x, box_y)
    path.lineTo(box_x + box_w, box_y)
    path.lineTo(x0 + w * 0.52, y0 + h * 0.10)   # flare top
    path.lineTo(x0 + w * 0.52, y0 + h * 0.90)   # flare bottom
    path.lineTo(box_x + box_w, box_y + box_h)
    path.lineTo(box_x, box_y + box_h)
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(path)

    # Sound waves.
    pen = QPen(color)
    pen.setWidthF(max(1.6, w * 0.07))
    pen.setCapStyle(Qt.RoundCap)
    p.setBrush(Qt.NoBrush)
    p.setPen(pen)
    cx = x0 + w * 0.52
    cy = y0 + h / 2
    for r in (w * 0.18, w * 0.32):
        p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), -50 * 16, 100 * 16)


def eye_icon(open_=True, size=22, color=_SLATE):
    """Eye for password fields: open (click to reveal) or slashed (click to hide)."""
    key = ("eye", open_, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
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
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = _painter(pm)
    full = QRectF(0, 0, size, size)
    if mode == "default_mic":
        _draw_mic(p, full.adjusted(size * 0.14, size * 0.06, -size * 0.14, -size * 0.06))
    elif mode == "system_only":
        _draw_speaker(p, full.adjusted(size * 0.10, size * 0.12, -size * 0.10, -size * 0.12))
    else:  # smart_meeting: mic left + speaker right
        _draw_mic(p, QRectF(size * 0.02, size * 0.10, size * 0.46, size * 0.84))
        _draw_speaker(p, QRectF(size * 0.50, size * 0.22, size * 0.48, size * 0.60))
    p.end()
    icon = QIcon(pm)
    _ICON_CACHE[key] = icon
    return icon
