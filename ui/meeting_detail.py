"""A saved meeting, opened from History: Summary and Transcript tabs, Resume
session, Save recording (MP3/WAV), Save as Word, Copy notes, Open folder."""
import html as html_mod
import logging
import os
import re
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTabWidget, QTextEdit, QVBoxLayout, QApplication,
)

import audio_export
import docx_export
import meeting_store

logger = logging.getLogger("transcribe")

_SPEAKER_RE = re.compile(r"^(Speaker (?:\d+|\?)|You):")


def transcript_html(transcript):
    """Bold slate speaker labels, readable line spacing."""
    rows = []
    for line in (transcript or "").split("\n"):
        esc = html_mod.escape(line)
        m = _SPEAKER_RE.match(esc)
        if m:
            esc = ("<span style='color:#334155; font-weight:600;'>"
                   + m.group(1) + ":</span>" + esc[m.end():])
        if line.strip().startswith("—") and line.strip().endswith("—"):
            esc = f"<span style='color:#64748b; font-style:italic;'>{esc}</span>"
        rows.append(esc)
    return ("<div style='line-height:140%; font-size:13px; color:#0f172a;'>"
            + "<br>".join(rows) + "</div>")


def transcript_paragraphs(transcript):
    """docx_export paragraphs from a saved transcript: one per non-empty
    line, speaker index parsed from 'Speaker N:' when present."""
    paras = []
    for line in (transcript or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        spk = None
        m = re.match(r"^Speaker (\d+):\s*(.*)$", s)
        if m:
            spk = int(m.group(1)) - 1
            s = m.group(2)
        paras.append({"start": 0, "speaker": spk, "text": s})
    return paras


class MeetingDetailDialog(QDialog):
    sig_export_done = Signal(str, str)      # path, error

    def __init__(self, folder, main_app=None, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.folder = str(folder)
        self.meta = meeting_store.load_meta(folder)
        self.notes = meeting_store.load_notes(folder)
        self.transcript = meeting_store.load_transcript(folder)
        self.parts = meeting_store.audio_parts(folder)
        self.setWindowTitle(self.meta.get("title") or "Meeting")
        self.resize(820, 640)
        self.sig_export_done.connect(self._on_export_done)
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)

        title = QLabel(self.meta.get("title") or "Untitled Meeting", self)
        title.setObjectName("titleLabel")
        lay.addWidget(title)
        bits = [self.meta.get("timestamp") or "",
                meeting_store.format_duration(self.meta.get("duration_sec") or 0)]
        if self.meta.get("attendees"):
            bits.append(self.meta["attendees"])
        if self.parts:
            secs = audio_export.duration_seconds(self.parts)
            bits.append(f"recording {meeting_store.format_duration(secs)}"
                        + (f" in {len(self.parts)} parts" if len(self.parts) > 1 else ""))
        meta = QLabel("   ·   ".join(b for b in bits if b), self)
        meta.setObjectName("subtitleLabel")
        lay.addWidget(meta)

        tabs = QTabWidget(self)
        self.txt_summary = QTextEdit(self)
        self.txt_summary.setReadOnly(True)
        if self.notes.strip():
            try:
                self.txt_summary.setMarkdown(self.notes)
            except Exception:
                self.txt_summary.setPlainText(self.notes)
        else:
            self.txt_summary.setPlainText(
                "No notes were generated for this meeting yet. Resume the session "
                "and stop it to generate notes, or open Record Meeting → Retry Summary.")
        tabs.addTab(self.txt_summary, "Summary")
        self.txt_transcript = QTextEdit(self)
        self.txt_transcript.setReadOnly(True)
        if self.transcript.strip():
            self.txt_transcript.setHtml(transcript_html(self.transcript))
        else:
            self.txt_transcript.setPlainText("No transcript was saved for this meeting.")
        tabs.addTab(self.txt_transcript, "Transcript")
        lay.addWidget(tabs, 1)

        row = QHBoxLayout()
        self.btn_resume = QPushButton("Resume session", self)
        self.btn_resume.setObjectName("primaryButton")
        self.btn_resume.setToolTip("Continue recording this meeting; the new part is "
                                   "appended and the notes are regenerated.")
        self.btn_resume.clicked.connect(self._resume)
        row.addWidget(self.btn_resume)
        self.btn_audio = QPushButton("Save recording…", self)
        self.btn_audio.setEnabled(bool(self.parts))
        self.btn_audio.setToolTip("Export the raw audio as MP3 or WAV"
                                  if self.parts else "No audio was saved for this meeting")
        self.btn_audio.clicked.connect(self._save_recording)
        row.addWidget(self.btn_audio)
        btn_docx = QPushButton("Save as Word…", self)
        btn_docx.clicked.connect(self._save_docx)
        row.addWidget(btn_docx)
        btn_copy = QPushButton("Copy notes", self)
        btn_copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.notes or self.transcript))
        row.addWidget(btn_copy)
        btn_folder = QPushButton("Open folder", self)
        btn_folder.clicked.connect(self._open_folder)
        row.addWidget(btn_folder)
        row.addStretch()
        self.lbl_status = QLabel("", self)
        self.lbl_status.setObjectName("subtitleLabel")
        row.addWidget(self.lbl_status)
        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.accept)
        row.addWidget(btn_close)
        lay.addLayout(row)

    # ── actions ──
    def _resume(self):
        if not self.app:
            return
        if hasattr(self.app, "is_pro") and not self.app.is_pro():
            if hasattr(self.app, "_pro_upsell"):
                self.app._pro_upsell("Meeting recording")
            return
        mw = getattr(self.app, "meetings_win", None)
        if mw is None:
            return
        self.accept()
        mw.resume_meeting(self.folder)

    def _default_stem(self):
        return docx_export.safe_filename(
            f"{self.meta.get('title') or 'Meeting'} {(self.meta.get('timestamp') or '')[:10]}".strip())

    def _save_recording(self):
        if not self.parts:
            return
        suggested = os.path.join(os.path.expanduser("~"), f"{self._default_stem()}.mp3")
        path, chosen = QFileDialog.getSaveFileName(
            self, "Save recording", suggested,
            "MP3 audio (*.mp3);;WAV audio (*.wav)")
        if not path:
            return
        if "wav" in chosen.lower() and not path.lower().endswith(".wav"):
            path += ".wav"
        elif not path.lower().endswith((".mp3", ".wav")):
            path += ".mp3"
        self.btn_audio.setEnabled(False)
        self.lbl_status.setText("Exporting recording…")
        parts = list(self.parts)

        def _worker():
            try:
                out = audio_export.export_recording(parts, path)
                self.sig_export_done.emit(str(out) if out else "", "" if out else "Nothing to export")
            except Exception as e:
                self.sig_export_done.emit("", str(e)[:200])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_export_done(self, path, error):
        self.btn_audio.setEnabled(True)
        if error:
            self.lbl_status.setText("")
            QMessageBox.warning(self, "Export failed", error)
            return
        self.lbl_status.setText(f"Saved {os.path.basename(path)}")
        box = QMessageBox(self)
        box.setWindowTitle("Recording saved")
        box.setText(f"Recording saved:\n{path}")
        open_btn = box.addButton("Open folder", QMessageBox.AcceptRole)
        box.addButton("Done", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            try:
                os.startfile(os.path.dirname(path))
            except Exception:
                pass

    def _save_docx(self):
        suggested = os.path.join(os.path.expanduser("~"), f"{self._default_stem()}.docx")
        path, _ = QFileDialog.getSaveFileName(self, "Save Word document", suggested,
                                              "Word document (*.docx)")
        if not path:
            return
        paras = transcript_paragraphs(self.transcript)
        notes = (self.notes or "").strip()
        # Notes first (as plain paragraphs), then the transcript.
        note_paras = [{"start": 0, "speaker": None, "text": ln.strip()}
                      for ln in notes.splitlines() if ln.strip()]
        if note_paras and paras:
            note_paras.append({"start": 0, "speaker": None, "text": "— Transcript —"})
        try:
            docx_export.save_docx(
                path, self.meta.get("title") or "Meeting", note_paras + paras,
                meta_line=f"{self.meta.get('timestamp') or ''} · "
                          f"{meeting_store.format_duration(self.meta.get('duration_sec') or 0)}"
                          " · Transcribe App",
                include_timestamps=False)
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.lbl_status.setText(f"Saved {os.path.basename(path)}")

    def _open_folder(self):
        try:
            os.startfile(self.folder)
        except Exception as e:
            QMessageBox.information(self, "Folder", f"{self.folder}\n\n{e}")
