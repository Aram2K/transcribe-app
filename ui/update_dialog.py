"""The small "Updating Transcribe" window: download progress, then a clear
"installing - Transcribe reopens by itself" step, or what went wrong with a
way forward. Shown for every update, so it never looks like nothing happened.
The work itself is updater.py, driven by AppController.start_update."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLayout, QProgressBar, QPushButton, QVBoxLayout,
)


class UpdateDialog(QDialog):
    cancel_requested = Signal()
    retry_requested = Signal()
    website_requested = Signal()

    def __init__(self, tag, parent=None, style=None):
        super().__init__(parent)
        self.tag = tag
        self.setWindowTitle("Updating Transcribe")
        # Not always-on-top: it would cover Settings' own question boxes
        # (application-modal - everything would look frozen). It's brought to
        # the front at every stage instead.
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        if style:
            self.setStyleSheet(style)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        # Height follows the (wrapped) text of each stage.
        lay.setSizeConstraint(QLayout.SetFixedSize)
        self.lbl_title = QLabel(f"Updating to {tag}", self)
        self.lbl_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        lay.addWidget(self.lbl_title)
        self.lbl_status = QLabel("Starting the download…", self)
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMinimumWidth(400)
        # A failure names its log file: copyable.
        self.lbl_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.lbl_status)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 0)                        # busy until sizes are known
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)
        lay.addWidget(self.bar)
        self.lbl_detail = QLabel("", self)
        self.lbl_detail.setObjectName("subtitleLabel")
        lay.addWidget(self.lbl_detail)
        row = QHBoxLayout()
        row.addStretch()
        self.btn_website = QPushButton("Download from the website", self)
        self.btn_website.clicked.connect(self.website_requested.emit)
        row.addWidget(self.btn_website)
        self.btn_retry = QPushButton("Try again", self)
        self.btn_retry.setObjectName("primaryButton")
        self.btn_retry.clicked.connect(self.retry_requested.emit)
        row.addWidget(self.btn_retry)
        self.btn_cancel = QPushButton("Cancel", self)
        self.btn_cancel.clicked.connect(self.cancel_requested.emit)
        row.addWidget(self.btn_cancel)
        self.btn_close = QPushButton("Close", self)
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.stage = "downloading"
        self._show_buttons(cancel=True)

    def _show_buttons(self, cancel=False, retry=False, website=False, close=False):
        self.btn_cancel.setVisible(cancel)
        self.btn_retry.setVisible(retry)
        self.btn_website.setVisible(website)
        self.btn_close.setVisible(close)

    def show_downloading(self):
        self.stage = "downloading"
        self.lbl_status.setText("Downloading the update…")
        self.lbl_detail.setText("")
        self.bar.setRange(0, 0)
        self._show_buttons(cancel=True)

    def show_progress(self, done, total):
        if self.stage != "downloading":
            return
        self.lbl_status.setText("Downloading the update…")
        if total > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(min(100, int(done * 100 / total)))
            self.lbl_detail.setText(f"{done / 2**20:.0f} of {total / 2**20:.0f} MB")
        else:
            self.lbl_detail.setText(f"{done / 2**20:.0f} MB")

    def show_installing(self, reopens=True):
        self.stage = "installing"
        self.lbl_status.setText(
            "Installing. Transcribe will close now and reopen by itself in about "
            "a minute - you'll see the installer's progress meanwhile." if reopens else
            "Installing. Transcribe will close now - open it again from the Start "
            "menu when the installer has finished.")
        self.lbl_detail.setText("")
        self.bar.setRange(0, 0)
        self._show_buttons()

    def show_waiting(self, text=None):
        text = text or ("Ready to install. It starts as soon as your recording or "
                        "transcription finishes - Transcribe then closes and "
                        "reopens by itself.")
        if self.stage == "waiting" and self.lbl_status.text() == text:
            return                                     # asked every 2 s: no flicker
        self.stage = "waiting"
        self.lbl_status.setText(text)
        self.lbl_detail.setText("")
        self.bar.setRange(0, 0)
        self._show_buttons(cancel=True)

    def show_failed(self, message):
        self.stage = "failed"
        message = (message or "Please try again.").strip()
        self.lbl_status.setText(f"The update didn't install. {message[:1].upper()}{message[1:]}")
        self.lbl_detail.setText("Your current version keeps working.")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self._show_buttons(retry=True, website=True, close=True)

    def reject(self):
        # Esc and the window's X (QDialog.closeEvent calls this): mid-download
        # or while waiting to install it's a cancel - never a hidden download
        # that later closes the app out of nowhere. While installing the
        # window stays: the app is about to close for the installer anyway.
        if self.stage == "installing":
            return
        if self.stage in ("downloading", "waiting"):
            self.cancel_requested.emit()
        super().reject()
