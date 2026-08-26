"""Transcribe Files tab: drop an audio file, get a Word document.

Product shape (the CTO reasoning, so future edits keep the contract):
* Everything runs LOCALLY through the same tuned Whisper pipeline as dictation
  (recorder.transcribe_segments), so quality matches the rest of the app and
  no audio leaves the machine.
* "Auto" picks the best model this computer can realistically run: the largest
  downloaded model on CUDA, a fast small model on CPU (a 1-hour file on
  medium/CPU would take hours - never default users into that).
* Progress is honest: distinct stages (read -> download model if needed ->
  transcribe -> speakers) drive one determinate bar, with a live time-left
  estimate derived from actual throughput. Cancel works mid-transcription.
* Free covers files up to 1 hour; Pro raises the cap to 5 hours. The limit is
  enforced on the DECODED duration, not the file size.
* Output is a real .docx (docx_export) - speaker labels, timestamps, title -
  because "transcript in Word" is the artifact people actually send around.
"""
import os
import threading
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QCheckBox, QFrame, QTextEdit, QProgressBar, QFileDialog, QMessageBox,
    QApplication,
)

import docx_export

FREE_MAX_SEC = 60 * 60          # 1 hour
PRO_MAX_SEC = 5 * 60 * 60      # 5 hours

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
              ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4b", ".amr", ".3gp"}

# Progress-bar allocation per stage (percent).
_P_READ_END = 6
_P_DOWNLOAD_END = 22
_P_TRANSCRIBE_END_WITH_SPK = 78
_P_TRANSCRIBE_END_NO_SPK = 96
_P_SPEAKERS_END = 96


def probe_duration(path):
    """Container-metadata duration in seconds (fast, no decode), or None."""
    try:
        import av
        with av.open(path) as c:
            if c.duration:
                return c.duration / 1_000_000.0
            for s in c.streams:
                if s.duration and s.time_base:
                    return float(s.duration * s.time_base)
    except Exception:
        pass
    return None


def pick_auto_model(recorder=None):
    """Best model for THIS machine: quality-first on CUDA, speed-first on CPU,
    preferring what is already downloaded so Auto never surprises the user
    with a multi-GB download."""
    from main import model_downloaded, AudioRecorder
    dev = AudioRecorder._whisper_device()[0]
    if recorder is not None and getattr(recorder, "_cuda_usable", None) is False:
        # A CUDA device exists but its runtime libs are proven broken - the
        # model WILL run on CPU, so choosing a large model here would turn a
        # one-hour file into an hours-long job.
        dev = "cpu"
    if dev == "cuda":
        order = ["large-v3-turbo", "large-v3", "medium", "small", "base", "tiny"]
        fallback = "large-v3-turbo"
    else:
        order = ["small", "base", "tiny"]
        fallback = "base"
    for m in order:
        if model_downloaded(m):
            return m
    return fallback


def max_seconds(is_pro):
    return PRO_MAX_SEC if is_pro else FREE_MAX_SEC


def duration_error(duration_sec, is_pro):
    """"" when the file is allowed, else the user-facing refusal."""
    limit = max_seconds(is_pro)
    if duration_sec <= limit + 1:
        return ""
    mins = int(duration_sec // 60)
    if is_pro:
        return (f"This file is about {mins} minutes long - above the "
                f"{PRO_MAX_SEC // 3600}-hour limit. Split it and transcribe "
                "the parts separately.")
    return (f"This file is about {mins} minutes long. Free covers up to "
            "1 hour per file - upgrade to Transcribe Pro for files up to "
            f"{PRO_MAX_SEC // 3600} hours.")


def _fmt_dur(sec):
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else (f"{m} min {s:02d} s" if m else f"{s} s")


def _fmt_eta(sec):
    if sec < 50:
        return "under a minute left"
    return f"about {max(1, int(round(sec / 60.0)))} min left"


class FileTranscribeTab(QWidget):
    sig_progress = Signal(int, str, str)     # percent, stage, eta text
    sig_finished = Signal(object, str)       # result dict | None, error
    sig_file_info = Signal(str)              # probed duration/size line

    def __init__(self, parent=None, main_app=None):
        super().__init__(parent)
        self.app = main_app
        self._path = ""
        self._running = False
        self._cancel = False
        self._result = None
        self.setAcceptDrops(True)

        self.sig_progress.connect(self._on_progress)
        self.sig_finished.connect(self._on_finished)
        self.sig_file_info.connect(self._on_file_info)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        title = QLabel("Audio file → Word document", self)
        title.setObjectName("titleLabel")
        lay.addWidget(title)
        sub = QLabel("Interviews, lectures, voice memos - transcribed on this "
                     "computer with speaker labels, delivered as a .docx you "
                     "can send to anyone. Nothing is uploaded.", self)
        sub.setObjectName("subtitleLabel")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        # Drop zone
        self.drop = QFrame(self)
        self.drop.setObjectName("dropZone")
        self.drop.setMinimumHeight(110)
        self.drop.setCursor(Qt.PointingHandCursor)
        dz = QVBoxLayout(self.drop)
        dz.setAlignment(Qt.AlignCenter)
        self.lbl_drop = QLabel("Drop an audio file here  ·  or click to browse", self.drop)
        self.lbl_drop.setAlignment(Qt.AlignCenter)
        self.lbl_drop.setStyleSheet("font-size: 14px; font-weight: 600; color: #334155;")
        dz.addWidget(self.lbl_drop)
        self.lbl_file = QLabel("MP3, WAV, M4A, FLAC, video files…", self.drop)
        self.lbl_file.setAlignment(Qt.AlignCenter)
        self.lbl_file.setStyleSheet("color: #64748b; font-size: 11px;")
        dz.addWidget(self.lbl_file)
        self.drop.mousePressEvent = lambda e: self._browse()
        lay.addWidget(self.drop)

        # Options
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Model", self))
        self.combo_model = QComboBox(self)
        self._populate_models()
        opts.addWidget(self.combo_model, 1)
        self.chk_speakers = QCheckBox("Speaker labels", self)
        self.chk_speakers.setChecked(True)
        self.chk_speakers.setToolTip(
            "Identify who is speaking (Speaker 1/2/3) - runs locally, adds a "
            "few minutes for long files.")
        opts.addWidget(self.chk_speakers)
        self.chk_timestamps = QCheckBox("Timestamps", self)
        self.chk_timestamps.setChecked(True)
        opts.addWidget(self.chk_timestamps)
        lay.addLayout(opts)

        self.lbl_limits = QLabel("", self)
        self.lbl_limits.setObjectName("subtitleLabel")
        lay.addWidget(self.lbl_limits)
        self._refresh_limits_label()

        self.btn_go = QPushButton("Transcribe file", self)
        self.btn_go.setObjectName("heroButton")
        self.btn_go.setMinimumHeight(42)
        self.btn_go.setEnabled(False)
        self.btn_go.clicked.connect(self._start)
        lay.addWidget(self.btn_go)

        # Progress
        self.prog_box = QWidget(self)
        pv = QVBoxLayout(self.prog_box)
        pv.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.lbl_stage = QLabel("", self.prog_box)
        row.addWidget(self.lbl_stage)
        row.addStretch()
        self.lbl_eta = QLabel("", self.prog_box)
        self.lbl_eta.setStyleSheet("color: #64748b;")
        row.addWidget(self.lbl_eta)
        self.btn_cancel = QPushButton("Cancel", self.prog_box)
        self.btn_cancel.clicked.connect(self._request_cancel)
        row.addWidget(self.btn_cancel)
        pv.addLayout(row)
        self.bar = QProgressBar(self.prog_box)
        self.bar.setRange(0, 100)
        pv.addWidget(self.bar)
        self.prog_box.hide()
        lay.addWidget(self.prog_box)

        # Result
        self.result_box = QWidget(self)
        rv = QVBoxLayout(self.result_box)
        rv.setContentsMargins(0, 0, 0, 0)
        self.preview = QTextEdit(self.result_box)
        self.preview.setReadOnly(True)
        rv.addWidget(self.preview, 1)
        btns = QHBoxLayout()
        self.btn_save = QPushButton("Save Word document (.docx)", self.result_box)
        self.btn_save.setObjectName("heroButton")
        self.btn_save.setMinimumHeight(38)
        self.btn_save.clicked.connect(self._save_docx)
        btns.addWidget(self.btn_save)
        self.btn_copy = QPushButton("Copy text", self.result_box)
        self.btn_copy.clicked.connect(self._copy_text)
        btns.addWidget(self.btn_copy)
        btns.addStretch()
        rv.addLayout(btns)
        self.result_box.hide()
        lay.addWidget(self.result_box, 1)
        lay.addStretch()

    # ── file picking ──
    def dragEnterEvent(self, event):
        if self._running:
            return
        urls = event.mimeData().urls()
        if urls and os.path.splitext(urls[0].toLocalFile())[1].lower() in AUDIO_EXTS:
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self._set_file(urls[0].toLocalFile())

    def _browse(self):
        if self._running:
            return
        exts = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an audio file", "", f"Audio files ({exts});;All files (*)")
        if path:
            self._set_file(path)

    def _set_file(self, path):
        if not path or not os.path.isfile(path):
            return
        self._path = path
        self._result = None
        self.result_box.hide()
        name = os.path.basename(path)
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
            info = f"{name}  ·  {size_mb:.0f} MB" if size_mb >= 1 else name
        except OSError:
            info = name
        self.lbl_drop.setText(name)
        self.lbl_file.setText(info + "  ·  reading duration…")
        self.btn_go.setEnabled(True)
        threading.Thread(target=self._probe_duration, args=(path, info),
                         daemon=True).start()

    def _probe_duration(self, path, info):
        """Show duration + plan fit right after picking, via the fast probe."""
        line = info
        dur = probe_duration(path)
        if dur:
            line = f"{info}  ·  {_fmt_dur(dur)}"
            if duration_error(dur, self._is_pro()):
                line += "  ·  ⚠ over your plan limit"
        try:
            self.sig_file_info.emit(line)
        except RuntimeError:
            pass

    def _on_file_info(self, line):
        self.lbl_file.setText(line)

    # ── helpers ──
    def _is_pro(self):
        try:
            return bool(self.app and self.app.is_pro())
        except Exception:
            return False

    def _refresh_limits_label(self):
        if self._is_pro():
            self.lbl_limits.setText(
                f"Your Pro plan covers files up to {PRO_MAX_SEC // 3600} hours.")
        else:
            self.lbl_limits.setText(
                "Free covers files up to 1 hour  ·  Pro extends that to "
                f"{PRO_MAX_SEC // 3600} hours.")

    def _populate_models(self):
        from main import MODELS, model_downloaded
        self.combo_model.clear()
        self.combo_model.addItem("Auto - best for this computer (recommended)", None)
        for name, info in MODELS.items():
            state = "downloaded" if model_downloaded(name) else "will download"
            self.combo_model.addItem(
                f"{name}  ·  {info['quality']}, {info['size']}  ·  {state}", name)

    # ── job control ──
    def _start(self):
        if self._running or not self._path:
            return
        if self.app is not None:
            # The recorder (and its GPU/model) is shared with live dictation
            # and meetings - never fight them for it.
            if getattr(self.app, "is_rec", False) or (
                    hasattr(self.app, "_is_meeting_busy") and self.app._is_meeting_busy()):
                QMessageBox.information(
                    self, "Recording in progress",
                    "Finish the current recording first - file transcription "
                    "shares the speech model with it.")
                return
        self._running = True
        self._cancel = False
        self._result = None
        # Two-way exclusion: dictation/meetings refuse to start while this flag
        # is up (they'd otherwise block on the shared model's inference lock
        # for the entire file - potentially an hour of "Finalising…").
        if self.app is not None:
            self.app._file_job_running = True
        self.result_box.hide()
        self.prog_box.show()
        self.btn_cancel.setEnabled(True)
        self.btn_go.setEnabled(False)
        self.bar.setValue(0)
        self.lbl_stage.setText("Starting…")
        self.lbl_eta.setText("")
        args = (self._path, self.combo_model.currentData(),
                self.chk_speakers.isChecked(), self.chk_timestamps.isChecked(),
                self._is_pro())
        threading.Thread(target=self._run_job, args=args, daemon=True).start()

    def _request_cancel(self):
        self._cancel = True
        self.btn_cancel.setEnabled(False)
        self.lbl_stage.setText("Stopping…")

    def _emit(self, pct, stage, eta=""):
        try:
            self.sig_progress.emit(int(pct), stage, eta)
        except RuntimeError:
            pass

    # ── the worker (background thread; UI only via signals) ──
    def _run_job(self, path, model_choice, want_speakers, want_ts, is_pro):
        try:
            # Cheap metadata gate BEFORE the expensive decode: a free user with
            # a 6-hour file shouldn't wait for (or pay the RAM of) a full
            # decode just to be told no. The decoded duration below stays the
            # authoritative check for files with missing/lying metadata.
            probed = probe_duration(path)
            if probed:
                err = duration_error(probed, is_pro)
                if err:
                    self.sig_finished.emit(None, "limit:" + err)
                    return

            self._emit(1, "Reading audio file…")
            from faster_whisper import decode_audio
            import numpy as np
            audio = np.asarray(decode_audio(path, sampling_rate=16000),
                               dtype=np.float32)
            duration = len(audio) / 16000.0
            if duration < 0.5:
                self.sig_finished.emit(None, "No audio could be read from this file.")
                return
            err = duration_error(duration, is_pro)
            if err:
                self.sig_finished.emit(None, "limit:" + err)
                return
            if self._cancel:
                self.sig_finished.emit(None, "cancelled")
                return
            self._emit(_P_READ_END, f"Audio loaded ({_fmt_dur(duration)})")

            import main as _m
            recorder = self.app.recorder if self.app else _m.AudioRecorder()
            model = model_choice or pick_auto_model(recorder)
            # Decide the stage layout NOW - after the download the same check
            # would always say "downloaded" and misplace the bar.
            did_download = not _m.model_downloaded(model)
            if did_download:
                label = f"Downloading the {model} model (one time)…"
                self._emit(_P_READ_END + 1, label)

                def dl_progress(pct, got, total):
                    if pct is not None:
                        span = _P_DOWNLOAD_END - _P_READ_END
                        self._emit(_P_READ_END + span * pct / 100.0, label)
                _m.download_whisper_model(model, on_progress=dl_progress)

            if self._cancel:
                self.sig_finished.emit(None, "cancelled")
                return

            t_start = _P_DOWNLOAD_END if did_download else _P_READ_END
            t_end = (_P_TRANSCRIBE_END_WITH_SPK if want_speakers
                     else _P_TRANSCRIBE_END_NO_SPK)

            # Load the model HERE with a real error message. Inside
            # transcribe_segments a load failure comes back as [], which the
            # code below would misreport as "no speech in this file".
            self._emit(t_start, f"Loading the {model} model…")
            try:
                recorder.load_model(model)
            except Exception as e:
                self.sig_finished.emit(
                    None, f"The {model} model couldn't be loaded: {str(e)[:200]}")
                return

            stage_t0 = time.time()

            def on_tr_progress(done, total):
                frac = done / total if total else 0.0
                eta = ""
                elapsed = time.time() - stage_t0
                if frac > 0.03 and elapsed > 5:
                    eta = _fmt_eta(elapsed * (1 - frac) / frac)
                self._emit(t_start + (t_end - t_start) * frac,
                           f"Transcribing with {model}…", eta)

            segments = recorder.transcribe_segments(
                audio, model_name=model,
                on_progress=on_tr_progress,
                should_cancel=lambda: self._cancel)
            if segments is None or self._cancel:
                self.sig_finished.emit(None, "cancelled")
                return
            if not segments:
                self.sig_finished.emit(
                    None, "No speech was detected in this file.")
                return

            if want_speakers:
                import diarization
                self._emit(t_end, "Identifying speakers…")
                if diarization.sherpa_installed() and not diarization.models_downloaded():
                    self._emit(t_end, "Downloading the speaker model (one time)…")
                    diarization.download_models()
                if diarization.is_available():
                    def on_sp_progress(done, total):
                        frac = done / total if total else 0.0
                        self._emit(t_end + (_P_SPEAKERS_END - t_end) * frac,
                                   "Identifying speakers…")
                    turns = diarization.diarize(audio, on_progress=on_sp_progress)
                    if turns:
                        segments = diarization.attribute_segments(segments, turns)

            # A cancel pressed during the speakers stage must not surface a
            # result the user already walked away from.
            if self._cancel:
                self.sig_finished.emit(None, "cancelled")
                return

            self._emit(97, "Building the document…")
            paragraphs = docx_export.group_segments(segments)
            speakers = sorted({p["speaker"] for p in paragraphs
                               if p.get("speaker") is not None})
            result = {
                "path": path,
                "duration": duration,
                "paragraphs": paragraphs,
                "timestamps": want_ts,
                "speaker_count": len(speakers),
                "model": model,
            }
            self.sig_finished.emit(result, "")
        except Exception as e:
            self.sig_finished.emit(None, str(e)[:300])

    # ── GUI-thread slots ──
    def _on_progress(self, pct, stage, eta):
        self.bar.setValue(pct)
        self.lbl_stage.setText(stage)
        self.lbl_eta.setText(eta)

    def _on_finished(self, result, error):
        self._running = False
        if self.app is not None:
            self.app._file_job_running = False
        self.prog_box.hide()
        self.btn_go.setEnabled(bool(self._path))
        if error == "cancelled":
            return
        if error:
            if error.startswith("limit:"):
                QMessageBox.information(self, "File too long", error[6:])
                if self.app and hasattr(self.app, "_pro_upsell") and not self._is_pro():
                    try:
                        self.app._pro_upsell("long_files")
                    except Exception:
                        pass
            else:
                QMessageBox.warning(self, "Transcription failed", error)
            return
        self._result = result
        self._show_preview(result)
        self.result_box.show()

    def _preview_paragraph_lines(self, result):
        lines = []
        for p in result["paragraphs"]:
            bits = []
            if result["timestamps"]:
                bits.append(f"[{docx_export.format_timestamp(p['start'])}]")
            label = docx_export.speaker_name(p.get("speaker"))
            if label:
                bits.append(f"{label}:")
            prefix = " ".join(bits)
            lines.append((prefix, p["text"]))
        return lines

    def _show_preview(self, result):
        import html as html_mod
        n_par = len(result["paragraphs"])
        spk = result["speaker_count"]
        head = (f"{_fmt_dur(result['duration'])} of audio · "
                f"{n_par} paragraphs" + (f" · {spk} speakers" if spk else "")
                + f" · {result['model']} model")
        rows = [f"<p style='color:#64748b; font-size:11px;'>{html_mod.escape(head)}</p>"]
        for prefix, text in self._preview_paragraph_lines(result):
            row = ""
            if prefix:
                row += ("<span style='color:#334155; font-weight:600;'>"
                        + html_mod.escape(prefix) + "</span> ")
            row += html_mod.escape(text)
            rows.append(f"<p style='line-height:135%;'>{row}</p>")
        self.preview.setHtml("".join(rows))

    def _plain_text(self):
        if not self._result:
            return ""
        return "\n\n".join(
            (f"{prefix} {text}".strip())
            for prefix, text in self._preview_paragraph_lines(self._result))

    def _copy_text(self):
        QApplication.clipboard().setText(self._plain_text())

    def _save_docx(self):
        if not self._result:
            return
        src = self._result["path"]
        stem = docx_export.safe_filename(os.path.splitext(os.path.basename(src))[0])
        suggested = os.path.join(os.path.dirname(src), f"{stem} transcript.docx")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Word document", suggested, "Word document (*.docx)")
        if not path:
            return
        title = os.path.splitext(os.path.basename(src))[0]
        meta = (f"Transcribed from {os.path.basename(src)} · "
                f"{_fmt_dur(self._result['duration'])} · Transcribe App")
        try:
            docx_export.save_docx(
                path, title, self._result["paragraphs"], meta_line=meta,
                include_timestamps=self._result["timestamps"])
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))
            return
        box = QMessageBox(self)
        box.setWindowTitle("Saved")
        box.setText(f"Word document saved:\n{path}")
        open_btn = box.addButton("Open document", QMessageBox.AcceptRole)
        box.addButton("Done", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            try:
                os.startfile(path)      # Windows
            except Exception:
                pass
