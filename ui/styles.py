# Premium Light Mode Stylesheet for Transcribe PySide6 Overhaul

STYLESHEET = """
/* Global Styles */
QWidget {
    background-color: #f8fafc;
    color: #1e293b;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, "Outfit", "Inter", Roboto, sans-serif;
    font-size: 13px;
}

/* Main Window and Dialogs */
QMainWindow, QDialog {
    background-color: #f1f5f9;
}

/* Card / Group Panel Frames */
QFrame#cardFrame {
    background-color: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
}
QFrame#cardFrame:disabled {
    background-color: #f8fafc;
    border-color: #cbd5e1;
}
QFrame#activeCardFrame {
    background-color: #ffffff;
    border: 2px solid #3b82f6;
    border-radius: 12px;
}
QFrame#activeCardFrame:disabled {
    background-color: #f8fafc;
    border-color: #cbd5e1;
}
QFrame#overlayCard {
    background-color: rgba(255, 255, 255, 240);
    border: 1px solid rgba(59, 130, 246, 120);
    border-radius: 16px;
}

/* Labels */
QLabel {
    background: transparent;
}
QLabel:disabled {
    color: #94a3b8;
}
QLabel#titleLabel {
    font-size: 20px;
    font-weight: bold;
    color: #0f172a;
}
QLabel#titleLabel:disabled {
    color: #94a3b8;
}
QLabel#subtitleLabel {
    font-size: 12px;
    color: #64748b;
}
QLabel#subtitleLabel:disabled {
    color: #cbd5e1;
}
QLabel#badgeLabel {
    background-color: rgba(59, 130, 246, 15);
    color: #1d4ed8;
    border: 1px solid rgba(59, 130, 246, 50);
    border-radius: 4px;
    font-size: 11px;
    font-weight: bold;
    padding: 2px 6px;
}

/* Buttons */
QPushButton {
    background-color: #ffffff;
    color: #334155;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #f8fafc;
    border-color: #3b82f6;
    color: #1e293b;
}
QPushButton:pressed {
    background-color: #f1f5f9;
}
QPushButton:disabled {
    background-color: #f1f5f9;
    color: #94a3b8;
    border-color: #e2e8f0;
}

/* Primary Accent Buttons */
QPushButton#primaryButton {
    background-color: #3b82f6;
    color: #ffffff;
    border: 1px solid #2563eb;
}
QPushButton#primaryButton:hover {
    background-color: #2563eb;
    border-color: #60a5fa;
}
QPushButton#primaryButton:pressed {
    background-color: #1d4ed8;
}
QPushButton#primaryButton:disabled {
    background-color: rgba(59, 130, 246, 40);
    color: rgba(255, 255, 255, 180);
    border-color: transparent;
}

/* Input Fields */
QLineEdit, QTextEdit, QPlainTextEdit {
    background-color: #ffffff;
    color: #0f172a;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 8px 12px;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
    border: 1px solid #3b82f6;
    background-color: #ffffff;
}
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled {
    background-color: #f1f5f9;
    color: #94a3b8;
    border-color: #e2e8f0;
}

/* Combobox (Dropdown) - Fully Custom Styled to Fix Windows Glitches */
QComboBox {
    background-color: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 8px 36px 8px 12px;
    min-width: 120px;
    color: #1e293b;
}
QComboBox:hover {
    border-color: #3b82f6;
}
QComboBox:focus {
    border-color: #3b82f6;
}
QComboBox:disabled {
    background-color: #f1f5f9;
    color: #94a3b8;
    border-color: #e2e8f0;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 32px;
    border-left: 1px solid #cbd5e1;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
}
QComboBox::drop-down:disabled {
    border-left-color: #e2e8f0;
}
QComboBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #64748b;
    margin-right: 10px;
}
QComboBox::down-arrow:disabled {
    border-top-color: #cbd5e1;
}
QComboBox QAbstractItemView {
    background-color: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    selection-background-color: #3b82f6;
    selection-color: #ffffff;
    outline: 0;
    padding: 2px;
    color: #1e293b;
}
QComboBox QAbstractItemView::item {
    min-height: 34px;
    padding: 6px 10px;
    border-radius: 4px;
}
QComboBox QAbstractItemView::item:selected {
    background-color: #3b82f6;
    color: #ffffff;
}
QComboBox QAbstractItemView::item:hover {
    background-color: #f1f5f9;
    color: #0f172a;
}

/* Scroll areas — visible scrollbars for long settings panels */
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    background: #f1f5f9;
    width: 10px;
    margin: 2px;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: #94a3b8;
    min-height: 28px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background: #64748b;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

/* Tab Widget and Bar */
QTabWidget::pane {
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    background-color: #ffffff;
    top: -1px;
}
QTabBar::tab {
    background-color: transparent;
    color: #64748b;
    padding: 10px 20px;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}
QTabBar::tab:hover {
    color: #0f172a;
}
QTabBar::tab:selected {
    color: #3b82f6;
    border-bottom: 2px solid #3b82f6;
}

/* Progress Bar */
QProgressBar {
    background-color: #f1f5f9;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    text-align: center;
    color: #1e293b;
    font-weight: bold;
}
QProgressBar::chunk {
    background-color: QLinearGradient(x1: 0, y1: 0, x2: 1, y2: 0, stop: 0 #3b82f6, stop: 1 #60a5fa);
    border-radius: 5px;
}

/* Scrollbars */
QScrollBar:vertical {
    background-color: #f8fafc;
    width: 8px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background-color: #cbd5e1;
    min-height: 20px;
    border-radius: 4px;
}
QScrollBar::handle:vertical:hover {
    background-color: #3b82f6;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar:horizontal {
    background-color: #f8fafc;
    height: 8px;
    margin: 0px;
}
QScrollBar::handle:horizontal {
    background-color: #cbd5e1;
    min-width: 20px;
    border-radius: 4px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #3b82f6;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* Lists */
QListWidget {
    background-color: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 4px;
}
QListWidget::item {
    padding: 8px 12px;
    border-radius: 6px;
    margin-bottom: 2px;
    color: #334155;
}
QListWidget::item:hover {
    background-color: #f1f5f9;
    color: #0f172a;
}
QListWidget::item:selected {
    background-color: #2563eb;
    color: #ffffff;
}
"""
