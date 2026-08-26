"""A QTabWidget whose designated tab gets a shiny gradient background.

Qt stylesheets cannot address ONE tab (QTabBar::tab styles them all), so the
highlight has to be painted: a custom QTabBar draws every ordinary tab through
the normal style path - keeping the app stylesheet's look - and paints the
designated tab as a rounded gradient pill: light blue-violet tint at rest,
full gradient with white text when selected. This is the "look here" treatment
for a newly shipped tab, without literally writing NEW on it.
"""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QStyle, QStyleOptionTab, QStylePainter, QTabBar, QTabWidget,
)


class ShinyTabBar(QTabBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._shiny = set()          # tab texts to highlight
        self.setMouseTracking(True)  # hover state for the pill tint

    def set_shiny(self, text):
        self._shiny.add(text)
        self.update()

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        if self.tabText(index) in self._shiny:
            # The pill text renders semi-bold - wider than the normal-weight
            # metrics the base hint used - and wants breathing room.
            size.setWidth(size.width() + 16)
        return size

    def paintEvent(self, event):
        painter = QStylePainter(self)
        opt = QStyleOptionTab()
        for i in range(self.count()):
            self.initStyleOption(opt, i)
            if self.tabText(i) in self._shiny:
                self._paint_shiny(painter, opt, i)
            else:
                painter.drawControl(QStyle.CE_TabBarTab, opt)

    def _paint_shiny(self, painter, opt, index):
        rect = self.tabRect(index)
        selected = bool(opt.state & QStyle.State_Selected)
        hovered = bool(opt.state & QStyle.State_MouseOver)

        pill = QRectF(rect.adjusted(4, 6, -4, -8))
        grad = QLinearGradient(pill.topLeft(), pill.topRight())
        if selected:
            grad.setColorAt(0.0, QColor("#3b82f6"))
            grad.setColorAt(1.0, QColor("#8b5cf6"))
            text_color = QColor("#ffffff")
        else:
            alpha = 60 if hovered else 36
            grad.setColorAt(0.0, QColor(59, 130, 246, alpha))
            grad.setColorAt(1.0, QColor(139, 92, 246, alpha))
            text_color = QColor("#6d28d9")

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(pill, 9.0, 9.0)
        font = QFont(self.font())
        font.setWeight(QFont.Bold if selected else QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(text_color)
        painter.drawText(rect, Qt.AlignCenter, self.tabText(index))
        painter.restore()


class ShinyTabWidget(QTabWidget):
    """Drop-in QTabWidget; call ``set_shiny_tab(text)`` after adding tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shiny_bar = ShinyTabBar(self)
        self.setTabBar(self._shiny_bar)

    def set_shiny_tab(self, text):
        self._shiny_bar.set_shiny(text)
