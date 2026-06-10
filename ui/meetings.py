# Modern Meeting Dictation Workspace in PySide6

import os
import time
import json
import re
import threading

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, 
    QLineEdit, QTextEdit, QFrame, QMessageBox, QSplitter, QProgressBar,
    QStackedWidget, QFileDialog, QWidget, QApplication
)
from PySide6.QtGui import QFont, QColor
import storage
import actions
import telemetry

class MeetingProcessingSignal(QObject):
    finished = Signal(str, str) # notes, error_msg


def ensure_recording_consent(parent, app):
    """One-time notice before the first meeting recording: capturing other
    people's voices may require their consent depending on jurisdiction. The
    user must acknowledge once; we persist the acknowledgement. Returns True
    when recording may proceed."""
    cfg = getattr(app, "cfg", None)
    if cfg is None or cfg.get("meeting_consent_ack"):
        return True
    box = QMessageBox(parent)
    box.setWindowTitle("Before you record")
    box.setIcon(QMessageBox.Information)
    box.setText("You're about to record a meeting - including other people's voices.")
    box.setInformativeText(
        "Depending on your country or state, recording a conversation may "
        "require the consent of all participants. By continuing you confirm "
        "you have any consent required where you are.\n\n"
        "This notice is shown only once."
    )
    ok = box.addButton("I understand - continue", QMessageBox.AcceptRole)
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is ok:
        cfg["meeting_consent_ack"] = True
        try:
            app.save_config()
        except Exception:
            pass
        return True
    return False

class MeetingsWindow(QDialog):
    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"
    STATE_DONE = "done"

    # Emitted from the background transcription worker thread with each finished
    # chunk of text. Connected to a main-thread slot so the live transcript
    # QTextEdit is only ever touched on the Qt GUI thread (touching widgets from
    # worker threads causes intermittent crashes on Windows).
    sig_chunk = Signal(str)

    def __init__(self, parent=None, main_app=None):
        super().__init__(parent)
        self.app = main_app
        
        self.setWindowTitle("Record Meeting")
        self.setMinimumSize(640, 500)
        self.resize(800, 650)
        self.setSizeGripEnabled(True)
        
        # Apply global styling
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)
            
        self.state = self.STATE_IDLE
        self._chunks = []
        self._chunks_lock = threading.Lock()
        self._record_started_at = None
        self._meeting_dir = None
        self._chunks_path = None
        self._final_notes = ""
        self._final_transcript = ""
        
        # Test stubs variables for backwards compatibility
        self._summary_var = ""
        self._transcript_var = ""
        
        # Signals for thread-safe processing
        self.proc_signals = MeetingProcessingSignal()
        self.proc_signals.finished.connect(self._on_processing_finished)
        # Live transcript chunks arrive on a worker thread; marshal to GUI thread.
        self.sig_chunk.connect(self._append_live_line)
        
        # Timer for duration and visualizer ticking
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50) # 20 FPS updates


        self._build_ui()
        self._populate_audio_devices()

    def showEvent(self, event):
        super().showEvent(event)
        from ui.winfit import fit_on_screen, size_to_screen
        if not getattr(self, "_fit_positioned", False):
            size_to_screen(self, 0.44, 0.64, 680, 520, 900, 820)
        fit_on_screen(self)

    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(20, 20, 20, 20)
        self.main_layout.setSpacing(14)

        # ── Stack/Dynamic UI Containers based on State ──
        self.container = QStackedWidget(self)
        
        # 1. Idle Page: Setup Meeting details
        self.page_idle = QWidget()
        self._build_page_idle()
        self.container.addWidget(self.page_idle)
        
        # 2. Recording Page: Live notes & transcripts
        self.page_recording = QWidget()
        self._build_page_recording()
        self.container.addWidget(self.page_recording)
        
        # 3. Processing Page: Loading summaries
        self.page_processing = QWidget()
        self._build_page_processing()
        self.container.addWidget(self.page_processing)
        
        # 4. Done Page: View final summaries & notes
        self.page_done = QWidget()
        self._build_page_done()
        self.container.addWidget(self.page_done)

        self.main_layout.addWidget(self.container)
        self.container.setCurrentIndex(0)

    # ── Idle View: Inputs & setup ──
    def _build_page_idle(self):
        lay = QVBoxLayout(self.page_idle)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        
        title = QLabel("Record Meeting", self.page_idle)
        title.setObjectName("titleLabel")
        lay.addWidget(title)
        
        desc = QLabel("Capture any Zoom, Google Meet, or conference call. Transcribe App will record system loopback audio and generate beautiful AI-summarized minutes.", self.page_idle)
        desc.setObjectName("subtitleLabel")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        # Title Card
        meta_frame = QFrame(self.page_idle)
        meta_frame.setObjectName("cardFrame")
        m_lay = QVBoxLayout(meta_frame)
        m_lay.setContentsMargins(18, 18, 18, 18)
        m_lay.setSpacing(12)
        
        m_lay.addWidget(QLabel("Meeting Title", meta_frame))
        self.input_title = QLineEdit(meta_frame)
        self.input_title.setPlaceholderText("Product Sync / Sprint Review")
        m_lay.addWidget(self.input_title)
        
        m_lay.addWidget(QLabel("Attendees (Comma separated)", meta_frame))
        self.input_attendees = QLineEdit(meta_frame)
        self.input_attendees.setPlaceholderText("Aram Adamyan, Anna Sargsyan, David")
        m_lay.addWidget(self.input_attendees)
        lay.addWidget(meta_frame)

        # Devices Picker Card
        dev_frame = QFrame(self.page_idle)
        dev_frame.setObjectName("cardFrame")
        d_lay = QVBoxLayout(dev_frame)
        d_lay.setContentsMargins(18, 18, 18, 18)
        d_lay.setSpacing(12)
        
        d_lay.addWidget(QLabel("Meeting Recording Mode", dev_frame))
        self.combo_device = QComboBox(dev_frame)
        d_lay.addWidget(self.combo_device)
        lay.addWidget(dev_frame)

        # Start button row
        btn_lay = QHBoxLayout()
        btn_lay.addStretch()
        
        btn_start = QPushButton("Start Meeting", self.page_idle)
        btn_start.setObjectName("primaryButton")
        btn_start.setMinimumHeight(40)
        btn_start.clicked.connect(self._start_meeting)
        btn_lay.addWidget(btn_start)
        lay.addLayout(btn_lay)

    # ── Recording View: Dual Note Editor ──
    def _build_page_recording(self):
        lay = QVBoxLayout(self.page_recording)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # Active Rec banner
        rec_banner = QFrame(self.page_recording)
        rec_banner.setStyleSheet("background-color: #3b1818; border: 1px solid #ef4444; border-radius: 8px;")
        rb_lay = QHBoxLayout(rec_banner)
        rb_lay.setContentsMargins(12, 8, 12, 8)
        
        self.lbl_rec_timer = QLabel("REC  ·  00:00  ·  Recording system audio...", rec_banner)
        self.lbl_rec_timer.setStyleSheet("color: #ef4444; font-weight: bold; font-size: 13px;")
        rb_lay.addWidget(self.lbl_rec_timer)
        rb_lay.addStretch()
        lay.addWidget(rec_banner)

        # Split Pane Workspace
        splitter = QSplitter(Qt.Horizontal, self.page_recording)
        splitter.setStyleSheet("QSplitter::handle { background-color: #27272a; width: 2px; }")
        
        # Left Pane: Live transcript chunks
        trans_frame = QFrame(splitter)
        trans_frame.setObjectName("cardFrame")
        t_lay = QVBoxLayout(trans_frame)
        t_lay.addWidget(QLabel("Live Transcription", trans_frame))
        self.live_trans_log = QTextEdit(trans_frame)
        self.live_trans_log.setReadOnly(True)
        t_lay.addWidget(self.live_trans_log)
        splitter.addWidget(trans_frame)

        # Right Pane: User's typed notes
        notes_frame = QFrame(splitter)
        notes_frame.setObjectName("cardFrame")
        n_lay = QVBoxLayout(notes_frame)
        n_lay.addWidget(QLabel("Your Notes (type bullets during meeting)", notes_frame))
        self.input_live_notes = QTextEdit(notes_frame)
        self.input_live_notes.setPlaceholderText("- Decided to use PySide6 for desktop client\n- Aram to finalize setup instructions\n- Sprint ends on Monday")
        n_lay.addWidget(self.input_live_notes)
        splitter.addWidget(notes_frame)
        
        splitter.setSizes([380, 380])
        lay.addWidget(splitter)

        # Bottom Recording Control row
        btn_lay = QHBoxLayout()
        btn_lay.addStretch()
        
        btn_stop = QPushButton("Stop & Generate Notes", self.page_recording)
        btn_stop.setObjectName("primaryButton")
        btn_stop.setStyleSheet("background-color: #ef4444; border-color: #dc2626;")
        btn_stop.clicked.connect(self._stop_meeting)
        btn_lay.addWidget(btn_stop)
        lay.addLayout(btn_lay)

    # ── Processing View: Loading spinner overlay ──
    def _build_page_processing(self):
        lay = QVBoxLayout(self.page_processing)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(20)
        
        lbl_proc = QLabel("Generating AI Meeting Notes...", self.page_processing)
        lbl_proc.setFont(QFont("Segoe UI", 16, QFont.Bold))
        lbl_proc.setAlignment(Qt.AlignCenter)
        lay.addWidget(lbl_proc)

        pbar = QProgressBar(self.page_processing)
        pbar.setRange(0, 0) # Infinite spinner progress bar style
        pbar.setFixedWidth(300)
        lay.addWidget(pbar)

        desc = QLabel("Processing transcript chunks and applying context rules through your configured AI action engine.\nThis can take 10-40 seconds depending on meeting length.", self.page_processing)
        desc.setObjectName("subtitleLabel")
        desc.setAlignment(Qt.AlignCenter)
        desc.setWordWrap(True)
        lay.addWidget(desc)

    # ── Done View: Notes preview & export ──
    def _build_page_done(self):
        lay = QVBoxLayout(self.page_done)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        
        title_row = QHBoxLayout()
        self.lbl_done_title = QLabel("Meeting Notes Generated!", self.page_done)
        self.lbl_done_title.setObjectName("titleLabel")
        title_row.addWidget(self.lbl_done_title)
        
        title_row.addStretch()
        
        btn_copy = QPushButton("Copy Markdown", self.page_done)
        btn_copy.clicked.connect(self._copy_markdown)
        title_row.addWidget(btn_copy)
        
        btn_save = QPushButton("Save to File", self.page_done)
        btn_save.clicked.connect(self._save_to_file)
        title_row.addWidget(btn_save)
        
        btn_reset = QPushButton("Reset", self.page_done)
        btn_reset.setObjectName("primaryButton")
        btn_reset.clicked.connect(self._reset)
        title_row.addWidget(btn_reset)
        
        lay.addLayout(title_row)

        # Split preview between Notes & full transcript
        splitter = QSplitter(Qt.Horizontal, self.page_done)
        splitter.setStyleSheet("QSplitter::handle { background-color: #27272a; width: 2px; }")

        # Left: Generated summary & action items
        summary_frame = QFrame(splitter)
        summary_frame.setObjectName("cardFrame")
        s_lay = QVBoxLayout(summary_frame)
        s_lay.addWidget(QLabel("AI Summary & Action Items", summary_frame))
        self.txt_summary = QTextEdit(summary_frame)
        s_lay.addWidget(self.txt_summary)
        splitter.addWidget(summary_frame)

        # Right: Clean Full Transcript
        trans_frame = QFrame(splitter)
        trans_frame.setObjectName("cardFrame")
        t_lay = QVBoxLayout(trans_frame)
        t_lay.addWidget(QLabel("Full Meeting Transcript", trans_frame))
        self.txt_transcript = QTextEdit(trans_frame)
        self.txt_transcript.setReadOnly(True)
        t_lay.addWidget(self.txt_transcript)
        splitter.addWidget(trans_frame)

        splitter.setSizes([460, 300])
        lay.addWidget(splitter)

    # ── Audio Device Scan ──
    def _populate_audio_devices(self):
        from ui.icons import meeting_mode_icon
        # Only offer system-audio capture where loopback genuinely exists
        # (Windows/WASAPI). Elsewhere - notably macOS - those modes would
        # silently record the mic anyway, so they're hidden.
        try:
            import main as _m
            has_loopback = bool(getattr(_m, "HAS_LOOPBACK", False))
        except Exception:
            has_loopback = False
        self.combo_device.clear()
        if has_loopback:
            self.combo_device.addItem(
                meeting_mode_icon("smart_meeting"),
                "System sound + Microphone (best for meetings)", "smart_meeting")
        self.combo_device.addItem(
            meeting_mode_icon("default_mic"),
            "Microphone only", "default_mic")
        if has_loopback:
            self.combo_device.addItem(
                meeting_mode_icon("system_only"),
                "System sound only (no microphone)", "system_only")

        # Get from active config (heal modes this system can't capture)
        valid = ("smart_meeting", "default_mic", "system_only") if has_loopback else ("default_mic",)
        default_mode = "smart_meeting" if has_loopback else "default_mic"
        current_dev = default_mode
        if self.app:
            current_dev = self.app.cfg.get("meeting_audio_mode", default_mode)
            if current_dev not in valid:
                current_dev = default_mode

        idx = self.combo_device.findData(str(current_dev))
        if idx >= 0:
            self.combo_device.setCurrentIndex(idx)
        else:
            self.combo_device.setCurrentIndex(0)

    # ── State Machine Triggers ──
    def _start_meeting(self):
        if not self.app or not self.app.recorder:
            return

        # One-time legal notice: recording other participants may require their
        # consent. Must be acknowledged before the first recording ever starts.
        if not ensure_recording_consent(self, self.app):
            return

        # A plain Alt-R dictation shares this same AudioRecorder + audio device.
        # If one is running, stop it first (and tell the user) so the two don't
        # fight over the device - which crashes - or leak dictation audio into
        # the meeting recording.
        dictation_was_running = False
        try:
            if getattr(self.app, "is_rec", False) and hasattr(self.app, "force_stop_dictation"):
                dictation_was_running = bool(self.app.force_stop_dictation())
        except Exception:
            pass

        self._meeting_title = self.input_title.text().strip() or "Untitled Meeting"
        self._meeting_attendees = self.input_attendees.text().strip()

        # Capture configurations. This is the meeting capture mode
        # ("smart_meeting"/"default_mic"), stored under its own key so it stays
        # independent of the dictation input device.
        meeting_mode = self.combo_device.currentData()
        self.app.cfg["meeting_audio_mode"] = meeting_mode
        self.app.save_config()

        # Build local timestamp folder for auto-save recovery
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            self._meeting_dir = storage.path_for("meetings") / timestamp
            self._meeting_dir.mkdir(parents=True, exist_ok=True)
            self._chunks_path = self._meeting_dir / "chunks.jsonl"
        except OSError as e:
            QMessageBox.critical(self, "Recording Error",
                                 f"Could not create the meeting folder:\n{e}")
            return

        # Save the recorder's existing (dictation/overlay) callbacks so they can
        # be restored when the meeting ends - otherwise dictation's live level
        # meter and partials stay broken after a meeting. While the meeting owns
        # the recorder, silence the overlay callbacks so the dictation overlay
        # never pops up mid-meeting.
        self._saved_on_chunk   = self.app.recorder.on_chunk_complete
        self._saved_on_levels  = self.app.recorder.on_levels
        self._saved_on_partial = self.app.recorder.on_partial
        self._saved_on_lang    = self.app.recorder.on_lang_detected

        self.app.recorder.on_chunk_complete = self._on_chunk_transcribed
        self.app.recorder.on_levels         = self._on_audio_levels
        self.app.recorder.on_partial        = lambda *a, **k: None
        self.app.recorder.on_lang_detected  = lambda *a, **k: None

        # Commit to the recording state before starting so early chunks aren't
        # dropped by the state guard.
        self.state = self.STATE_RECORDING
        self._chunks = []
        self.live_trans_log.clear()
        self.input_live_notes.clear()
        self._record_started_at = time.time()

        # Start recording - guard against device-open failures (busy mic, no
        # loopback device, driver errors) so a failure never crashes the app or
        # leaves the window stuck in a fake "recording" state.
        try:
            self.app.recorder.start_recording(capture_mode=meeting_mode)
        except Exception as e:
            self._restore_recorder_callbacks()
            self.state = self.STATE_IDLE
            self._record_started_at = None
            self.container.setCurrentIndex(0)
            QMessageBox.critical(
                self, "Recording Error",
                f"Could not start the meeting recording:\n\n{e}\n\n"
                "Close any other app that may be using the microphone or audio "
                "device and try again."
            )
            return

        # Swap view tab
        self.container.setCurrentIndex(1)

        if dictation_was_running and hasattr(self.app, "show_tray_hint"):
            self.app.show_tray_hint(
                "Dictation Stopped",
                "Your Alt-R dictation was stopped so it isn't mixed into this "
                "meeting recording."
            )

        from main import APP_VERSION
        telemetry.track("meeting_recording_started", {}, self.app.cfg, APP_VERSION)

    def _on_chunk_transcribed(self, idx, text, lang):
        # NOTE: this runs on a background transcription worker thread.
        if self.state != self.STATE_RECORDING:
            return

        # Log to in-memory cache
        chunk = {
            "index": idx,
            "text": text,
            "language": lang,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with self._chunks_lock:
            self._chunks.append(chunk)

        # Recoverable write to JSONL log
        try:
            with open(self._chunks_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(chunk, ensure_ascii=False) + "\n")
        except OSError:
            pass

        # Hand the text to the GUI thread via a signal. Touching the QTextEdit
        # directly from this worker thread is what was crashing the meeting
        # recorder on Windows.
        if text:
            try:
                self.sig_chunk.emit(text)
            except RuntimeError:
                pass  # window already destroyed

    def _append_live_line(self, text):
        # Runs on the Qt GUI thread (connected to sig_chunk).
        if self.state != self.STATE_RECORDING:
            return
        self.live_trans_log.append(f"[{time.strftime('%H:%M:%S')}]  {text}")

    def _restore_recorder_callbacks(self):
        """Restore the recorder callbacks that were active before the meeting
        took over (the dictation/overlay handlers). Falls back to safe no-ops so
        a missing saved handler can never leave a dangling reference."""
        if not (self.app and getattr(self.app, "recorder", None)):
            return
        rec = self.app.recorder
        rec.on_chunk_complete = getattr(self, "_saved_on_chunk", None) or (lambda idx, text, lang: None)
        rec.on_levels         = getattr(self, "_saved_on_levels", None) or (lambda lvls: None)
        rec.on_partial        = getattr(self, "_saved_on_partial", None) or (lambda text: None)
        rec.on_lang_detected  = getattr(self, "_saved_on_lang", None) or (lambda code, name: None)

    def _on_audio_levels(self, levels):
        pass # Optional viz level capture if visual painting needed

    def _tick(self):
        if self.state != self.STATE_RECORDING or not self._record_started_at:
            return
            
        dur = int(time.time() - self._record_started_at)
        m, s = divmod(dur, 60)
        h, m = divmod(m, 60)
        dur_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        
        self.lbl_rec_timer.setText(f"REC  ·  {dur_str}  ·  Capturing active conversation chunks...")

    def _stop_meeting(self):
        if not self.app or self.state != self.STATE_RECORDING:
            return
            
        self.state = self.STATE_PROCESSING
        self.container.setCurrentIndex(2)
        
        # Stop recording
        self.app.recorder.stop_recording()

        # Retrieve notes text in-meeting notepad
        self._user_notes = self.input_live_notes.toPlainText().strip()

        # Restore the recorder's dictation/overlay callbacks so plain Alt-R
        # dictation works normally after the meeting.
        self._restore_recorder_callbacks()

        # Start LLM summaries generator thread safely
        threading.Thread(target=self._process_meeting_notes, daemon=True).start()

    def _process_meeting_notes(self):
        try:
            # 1. Wait briefly to drain active audio queue and transcription threads
            text, detected_lang = self.app.recorder.transcribe()
            
            # Combine transcript chunk lists
            full_chunks_text = []
            with self._chunks_lock:
                for c in self._chunks:
                    full_chunks_text.append(c.get("text", ""))
            
            if text and text not in full_chunks_text:
                full_chunks_text.append(text)
                
            self._final_transcript = "\n\n".join(full_chunks_text).strip()
            
            if not self._final_transcript:
                self.proc_signals.finished.emit("", "No transcription recorded. The meeting is empty.")
                return

            # Add custom note bullet context if present
            note_context = self._final_transcript
            if self._user_notes:
                note_context += f"\n\nAdditional visual meeting notes provided by attendee:\n{self._user_notes}"

            # 2. Run summarizer logic via Action engine processor
            engine = self.app.cfg.get("action_model", "rule_based")
            notes = actions.process(
                note_context, 
                actions.ACTION_MEETING_NOTES,
                source_lang=detected_lang, 
                target_lang="en", 
                model=engine, 
                config=self.app.cfg
            )
            
            # 3. Save finalized artifacts to timestamp directory
            try:
                with open(self._meeting_dir / "transcript.txt", "w", encoding="utf-8") as f:
                    f.write(self._final_transcript)
                with open(self._meeting_dir / "notes.md", "w", encoding="utf-8") as f:
                    f.write(notes)
                with open(self._meeting_dir / "meta.json", "w", encoding="utf-8") as f:
                    json.dump({
                        "title": self._meeting_title,
                        "attendees": self._meeting_attendees,
                        "duration_sec": int(time.time() - self._record_started_at) if self._record_started_at else 0,
                        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                    }, f)
            except OSError:
                pass
                
            self.proc_signals.finished.emit(notes, "")
        except Exception as e:
            self.proc_signals.finished.emit("", str(e))

    def _on_processing_finished(self, notes, error_msg):
        if error_msg:
            QMessageBox.critical(self, "AI Summary Error", f"Could not generate meeting notes: {error_msg}")
            self._reset()
            return
            
        self.state = self.STATE_DONE
        self._final_notes = notes
        
        self.lbl_done_title.setText(self._meeting_title or "Meeting Notes")
        self.txt_summary.setPlainText(notes)
        self.txt_transcript.setPlainText(self._final_transcript)
        
        self.container.setCurrentIndex(3)
        from main import APP_VERSION
        telemetry.track("meeting_notes_completed", {}, self.app.cfg, APP_VERSION)

    def _copy_markdown(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self._final_notes)
        QMessageBox.information(self, "Copied", "AI Meeting notes copied to clipboard in Markdown formatting.")

    def _save_to_file(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Meeting Notes",
            os.path.expanduser(f"~/Documents/{self._meeting_title.replace(' ', '_')}_Notes.md"),
            "Markdown Files (*.md)"
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self._final_notes)
                QMessageBox.information(self, "Saved", f"Successfully saved meeting notes to {os.path.basename(path)}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save notes: {e}")

    def _reset(self):
        self.state = self.STATE_IDLE
        self.input_title.clear()
        self.input_attendees.clear()
        self.combo_device.setCurrentIndex(0)
        self.container.setCurrentIndex(0)

    def _abort(self):
        if self.app and self.app.recorder:
            self.app.recorder.stop_recording()
            self._restore_recorder_callbacks()
        self._reset()

    def closeEvent(self, event):
        if self.state == self.STATE_RECORDING:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Record Meeting Active")
            msg_box.setText("A meeting is actively being recorded. What would you like to do?")
            
            btn_minimize = msg_box.addButton("Minimize to Tray (Keep Recording)", QMessageBox.AcceptRole)
            btn_stop = msg_box.addButton("Stop & Generate Notes", QMessageBox.AcceptRole)
            btn_abort = msg_box.addButton("Abort & Discard", QMessageBox.DestructiveRole)
            btn_cancel = msg_box.addButton("Cancel", QMessageBox.RejectRole)
            
            msg_box.setDefaultButton(btn_minimize)
            msg_box.exec()
            
            clicked = msg_box.clickedButton()
            if clicked == btn_minimize:
                self.hide()
                if self.app and hasattr(self.app, "show_tray_hint"):
                    self.app.show_tray_hint(
                        "Meeting Recording Active",
                        "Transcribe is still recording in the background. Right-click the tray icon to restore."
                    )
                event.ignore()
            elif clicked == btn_stop:
                self._stop_meeting()
                event.ignore()
            elif clicked == btn_abort:
                self._abort()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.ignore()
        else:
            super().keyPressEvent(event)

    def _build_transcript_with_markers(self, chunks):
        parts = []
        for i, chunk in enumerate(chunks):
            text = chunk.get("text", "").strip()
            if not text:
                continue
            if i > 0 and chunk.get("silence_before", 0) >= 1.4:
                text = f"[speaker change] {text}"
            parts.append(text)
        return " ".join(parts)

    def _build_llm_input(self, transcript):
        if not self._meeting_title and not self._meeting_attendees and not self._user_notes:
            return transcript
        parts = []
        if self._meeting_title:
            parts.append(f"Meeting Title: {self._meeting_title}")
        if self._meeting_attendees:
            parts.append(f"Attendees: {self._meeting_attendees}")
        if self._user_notes:
            parts.append(f"User Notes:\n{self._user_notes}")
        parts.append(f"Transcript:\n{transcript}")
        return "\n\n".join(parts)

    @staticmethod
    def _strip_markdown(md):
        lines = []
        for line in (md or "").splitlines():
            line = re.sub(r"^#+\s+", "", line)
            if line.strip().startswith("- [ ]") or line.strip().startswith("- "):
                line = re.sub(r"^\s*-\s*(\[\s*\])?\s*", "• ", line)
            line = line.replace("**", "")
            line = line.replace("`", "")
            lines.append(line)
        return "\n".join(lines)

    def _format_share(self, format_type):
        summary = self._summary_var or ""
        title = self._meeting_title or "Meeting"
        
        if format_type == "markdown":
            return summary
            
        if format_type == "email":
            clean_body = self._strip_markdown(summary)
            return (
                f"Subject: {title} - Notes\n\n"
                "Hi,\n\n"
                f"{clean_body}\n\n"
                "Best,"
            )
            
        if format_type == "slack":
            slack = re.sub(r"^#+\s+(.*)$", r"*\1*", summary, flags=re.MULTILINE)
            slack = slack.replace("**", "*")
            return f"*{title} - Notes*\n\n{slack}"
            
        return summary

