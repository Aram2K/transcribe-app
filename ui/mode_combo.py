"""A combo box that paints small glyphs inline, beside the words they describe.

"System sound + Microphone" wants a speaker next to "System sound" and a mic
next to "Microphone". Qt gives a combo item exactly one icon and always draws it
hard-left, so both glyphs ended up bunched at the start of the line. Emoji in
the label would place them correctly but render in full colour, clashing with
the flat slate line-art used everywhere else.

So the label is painted: a run of (glyph, text) segments laid out left to right,
in the same slate as the rest of the app's icons. Two paths are needed because
Qt renders the two states through different code:

* the popup list goes through an item delegate,
* the closed box paints itself, so the widget overrides ``paintEvent``.

Both share :func:`draw_segments`, so they cannot drift apart.
"""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication, QComboBox, QStyle, QStyleOptionComboBox, QStyleOptionViewItem,
    QStylePainter, QStyledItemDelegate,
)

from ui.icons import MEETING_MODE_SEGMENTS, SLATE, draw_mode_glyph

GLYPH_GAP = 5      # glyph -> its own word
SEGMENT_GAP = 7    # end of a word -> next glyph


def _glyph_size(fm):
    """Glyph box, tied to the font so it scales with display DPI. Kept even so
    the centred strokes land on whole pixels rather than half ones; the 16px
    floor keeps the mic's cradle from collapsing into mush at small fonts."""
    size = max(16, int(fm.height() * 1.15))
    return size + (size % 2)


def segments_for(mode):
    return MEETING_MODE_SEGMENTS.get(mode, ())


def segments_width(fm, mode):
    g = _glyph_size(fm)
    width = 0
    for i, (kind, text) in enumerate(segments_for(mode)):
        if i:
            width += SEGMENT_GAP
        if kind:
            width += g + GLYPH_GAP
        if text:
            width += fm.horizontalAdvance(text)
    return width


def draw_segments(painter, rect, mode, color, fm, glyph_color=None):
    """Paint ``mode``'s glyph/text runs left-aligned and vertically centred.

    ``glyph_color`` defaults to the app's slate, so the glyphs read as quiet
    supporting marks rather than competing with the label text. On a selected
    (blue) row the caller passes the highlight colour instead, or the glyph
    would sink into the background.
    """
    segs = segments_for(mode)
    if not segs:
        return
    if glyph_color is None:
        glyph_color = SLATE
    g = _glyph_size(fm)
    x = float(rect.left())
    mid = rect.center().y() + 1
    painter.save()
    # Never spill past the field - on a narrow combo the tail would otherwise
    # be drawn straight over the dropdown arrow.
    painter.setClipRect(rect)
    painter.setPen(color)
    for i, (kind, text) in enumerate(segs):
        if i:
            x += SEGMENT_GAP
        if kind:
            # Snap to whole pixels: a thin stroke landing on a half pixel gets
            # smeared across two by antialiasing, which reads as blur.
            draw_mode_glyph(painter, kind,
                            QRectF(round(x), round(mid - g / 2.0), g, g),
                            glyph_color)
            x += g + GLYPH_GAP
        if text:
            w = fm.horizontalAdvance(text)
            painter.setPen(color)
            painter.drawText(QRectF(x, rect.top(), w + 2, rect.height()),
                             int(Qt.AlignLeft | Qt.AlignVCenter), text)
            x += w
    painter.restore()


class MeetingModeDelegate(QStyledItemDelegate):
    """Draws the popup rows: stock background/selection, hand-painted label."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        mode = index.data(Qt.UserRole)
        if not segments_for(mode):
            super().paint(painter, option, index)
            return
        opt.text = ""                      # background/selection only
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        selected = bool(opt.state & QStyle.State_Selected)
        role = QPalette.HighlightedText if selected else QPalette.Text
        text_color = opt.palette.color(role)
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, widget)
        draw_segments(painter, rect.adjusted(2, 0, 0, 0), mode, text_color,
                      opt.fontMetrics,
                      # Grey normally; on the blue row it must follow the text
                      # or it disappears into the highlight.
                      glyph_color=text_color if selected else None)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        mode = index.data(Qt.UserRole)
        if segments_for(mode):
            fm = option.fontMetrics
            size.setWidth(segments_width(fm, mode) + 24)
            size.setHeight(max(size.height(), _glyph_size(fm) + 12))
        return size


class EnginePillDelegate(QStyledItemDelegate):
    """Right-aligned PRO / LOCAL / CLOUD pill on AI-engine dropdown rows, so the
    cost and privacy of each choice is visible before picking it. Qt draws item
    icons hard-left, so the pill is painted by hand."""

    PILL_ROLE = Qt.UserRole + 78
    # Widths MUST match the icon factories' defaults in ui/icons.py: the pill is
    # rendered at native resolution for exactly that size, so drawing it at any
    # other size reintroduces the rescale that made it blurry.
    PILL_H = 20
    _SPECS = {"pro": ("pro_pill_icon", 40),
              "local": ("local_pill_icon", 48),
              "cloud": ("cloud_pill_icon", 50)}

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        spec = self._SPECS.get(index.data(self.PILL_ROLE))
        if not spec:
            return
        import ui.icons as icons
        icon, w, h = getattr(icons, spec[0])(), spec[1], self.PILL_H
        r = option.rect
        icon.paint(painter, r.right() - w - 12, r.top() + (r.height() - h) // 2,
                   w, h, Qt.AlignCenter)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if self._SPECS.get(index.data(self.PILL_ROLE)):
            size.setWidth(size.width() + 68)   # keep the pill off the label
        return size


def engine_pill_kind(model_id):
    """Which pill an action-engine id deserves, or None."""
    import actions
    kind = actions.ACTION_MODELS.get(model_id, {}).get("kind")
    if kind == "managed":
        return "pro"
    if kind == "local_llm":
        return "local"
    if kind == "cloud":
        return "cloud"
    return None      # rule_based is neither cloud nor a model - no pill


def tag_engine_combo(combo):
    """Attach the pill delegate and tag every row. Safe to call repeatedly."""
    view = combo.view()
    if not isinstance(view.itemDelegate(), EnginePillDelegate):
        view.setItemDelegate(EnginePillDelegate(view))
    model = combo.model()
    for i in range(combo.count()):
        item = model.item(i) if hasattr(model, "item") else None
        if item is not None:
            item.setData(engine_pill_kind(combo.itemData(i)),
                         EnginePillDelegate.PILL_ROLE)


class MeetingModeComboBox(QComboBox):
    """Combo whose closed state paints the same inline-glyph label."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(MeetingModeDelegate(self))

    def paintEvent(self, event):
        mode = self.currentData()
        if not segments_for(mode):
            super().paintEvent(event)
            return
        p = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        p.drawComplexControl(QStyle.CC_ComboBox, opt)
        opt.currentText = ""               # frame + arrow, no stock label
        opt.currentIcon = self.itemIcon(-1)
        p.drawControl(QStyle.CE_ComboBoxLabel, opt)
        rect = self.style().subControlRect(
            QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, self)
        draw_segments(p, rect.adjusted(4, 0, -2, 0), mode,
                      self.palette().color(QPalette.Text), self.fontMetrics())
