# Premium Light Mode Stylesheet for Transcribe PySide6 Overhaul

STYLESHEET = """
/* Global Styles */
QWidget {
    background-color: #f8fafc;
    color: #1e293b;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, "Outfit", "Inter", Roboto, sans-serif;
    font-size: 13px;
}

/* Main Window and Dialogs - soft gradient backdrop gives the glass panels depth */
QMainWindow, QDialog {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #eaf0f8, stop:0.5 #f4f7fb, stop:1 #e7eef7);
}

/* Card / Group Panel Frames - frosted "liquid glass": translucent gradient fill
   with a subtle highlight border so panels read as layered glass. */
QFrame#cardFrame {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255, 255, 255, 230), stop:1 rgba(241, 245, 249, 195));
    border: 1px solid rgba(203, 213, 225, 140);
    border-radius: 16px;
}
QFrame#cardFrame:disabled {
    background-color: rgba(248, 250, 252, 160);
    border-color: rgba(203, 213, 225, 110);
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
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255, 255, 255, 225), stop:1 rgba(248, 250, 252, 195));
    border: 1px solid rgba(255, 255, 255, 210);
    border-radius: 20px;
}
/* Pro account card - warm frosted glass with a faint gold edge */
QFrame#glassCard {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(255, 251, 235, 230), stop:1 rgba(254, 243, 199, 170));
    border: 1px solid rgba(245, 158, 11, 90);
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
    subcontrol-position: center right;
    width: 30px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: url("assets/chevron.svg");
    width: 12px;
    height: 8px;
    margin-right: 12px;
}
QComboBox::down-arrow:on {
    /* keep the same chevron while the popup is open */
    image: url("assets/chevron.svg");
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

/* Scroll areas - visible scrollbars for long settings panels */
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

/* Checkboxes */
QCheckBox {
    spacing: 8px;
    color: #334155;
    font-weight: 500;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border: 1.5px solid #cbd5e1;
    border-radius: 6px;
    background-color: #ffffff;
}
QCheckBox::indicator:hover {
    border-color: #3b82f6;
    background-color: #f8fafc;
}
QCheckBox::indicator:checked {
    border-color: #3b82f6;
    background-color: #3b82f6;
    image: url("assets/check.svg");
}
QCheckBox::indicator:checked:hover {
    border-color: #2563eb;
    background-color: #2563eb;
}
QCheckBox::indicator:disabled {
    background-color: #f1f5f9;
    border-color: #e2e8f0;
}

/* ── Tier pills ───────────────────────────────────────────────────────────── */
/* PRO - purple glass pill. */
QLabel#proBadge {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(168, 85, 247, 235), stop:1 rgba(126, 34, 206, 235));
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 150);
    border-radius: 9px;
    font-size: 11px;
    font-weight: bold;
    padding: 2px 10px;
}
/* FREE - green pill. */
QLabel#freeBadge {
    background-color: rgba(34, 197, 94, 45);
    color: #15803d;
    border: 1px solid rgba(34, 197, 94, 120);
    border-radius: 9px;
    font-size: 11px;
    font-weight: bold;
    padding: 2px 10px;
}
/* GUEST - neutral gray pill. */
QLabel#guestBadge {
    background-color: rgba(148, 163, 184, 45);
    color: #475569;
    border: 1px solid rgba(148, 163, 184, 90);
    border-radius: 9px;
    font-size: 11px;
    font-weight: bold;
    padding: 2px 10px;
}

/* Hero call-to-action: the one shiny button per screen (Transcribe Files). */
QPushButton#heroButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #3b82f6, stop:1 #8b5cf6);
    color: #ffffff;
    font-size: 14px;
    font-weight: bold;
    border: none;
    border-radius: 10px;
    padding: 10px 22px;
}
QPushButton#heroButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #2563eb, stop:1 #7c3aed);
}
QPushButton#heroButton:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                stop:0 #1d4ed8, stop:1 #6d28d9);
}
QPushButton#heroButton:disabled {
    background: #cbd5e1;
    color: #f8fafc;
}

/* File drop zone on the Transcribe Files tab. */
QFrame#dropZone {
    background-color: rgba(59, 130, 246, 14);
    border: 2px dashed #93c5fd;
    border-radius: 12px;
}
QFrame#dropZone:hover {
    background-color: rgba(59, 130, 246, 28);
    border-color: #3b82f6;
}
"""
