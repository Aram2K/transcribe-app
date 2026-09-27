# Modern Searchable Transcription History View in PySide6

import os
import threading
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QMenu, QMessageBox, QFileDialog, QWidget, QApplication
)
from PySide6.QtGui import QFont, QAction, QIcon, QClipboard
import history as hist
import telemetry


def entry_audio_parts(entry):
    """The recording files of a history entry (meetings saved with
    ``audio_dir``); [] when there's none or it was deleted since."""
    folder = (entry or {}).get("audio_dir")
    if not folder:
        return []
    try:
        import meeting_store
        return meeting_store.audio_parts(folder)
    except Exception:
        return []


class HistoryWindow(QDialog):
    sig_audio_done = Signal(str, str)     # saved path, error

    def __init__(self, parent=None, main_app=None):
        super().__init__(parent)
        self.app = main_app
        
        self.setWindowTitle("History")
        # QDialogs only get a Close button on Windows - add minimize/maximize
        # so the window behaves like a normal app window.
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowMinimizeButtonHint
                            | Qt.WindowMaximizeButtonHint)
        self.setMinimumSize(440, 420)
        self.resize(540, 650)
        self.setSizeGripEnabled(True)
        
        # Apply the global stylesheet
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)
        
        self._selected_indices = set()
        self.all_entries = []
        self.displayed_entries = [] # Map index of displayed item to index in hist.load() list
        
        self._build_ui()
        self.refresh_list()

    def showEvent(self, event):
        super().showEvent(event)
        from ui.winfit import settle_on_screen, size_to_screen
        if not getattr(self, "_fit_positioned", False):
            size_to_screen(self, 0.28, 0.62, 480, 480, 620, 800)
        settle_on_screen(self)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Header Row
        header_layout = QHBoxLayout()
        self.title_label = QLabel("History", self)
        self.title_label.setObjectName("titleLabel")
        header_layout.addWidget(self.title_label)
        
        self.count_label = QLabel("0 entries", self)
        self.count_label.setObjectName("subtitleLabel")
        self.count_label.setStyleSheet("padding-left: 8px; margin-top: 6px;")
        header_layout.addWidget(self.count_label)
        header_layout.addStretch()
        
        layout.addLayout(header_layout)

        # Actions Row (Buttons)
        actions_layout = QHBoxLayout()
        
        self.btn_export_csv = QPushButton("Export CSV", self)
        self.btn_export_csv.clicked.connect(lambda: self._export("csv"))
        
        self.btn_export_txt = QPushButton("Export TXT", self)
        self.btn_export_txt.clicked.connect(lambda: self._export("txt"))
        
        self.btn_clear_sel = QPushButton("Delete Selected", self)
        self.btn_clear_sel.clicked.connect(self._clear_selection)
        
        self.btn_clear_all = QPushButton("Clear All", self)
        self.btn_clear_all.clicked.connect(self._clear)

        # Enabled when the selected entry is a meeting with its recording.
        self.btn_audio = QPushButton("Download Audio", self)
        self.btn_audio.setToolTip("Save the selected meeting's recording (MP3 or WAV)")
        self.btn_audio.setEnabled(False)
        self.btn_audio.clicked.connect(self._download_selected_audio)
        self.sig_audio_done.connect(self._on_audio_done)

        actions_layout.addWidget(self.btn_clear_all)
        actions_layout.addWidget(self.btn_clear_sel)
        actions_layout.addStretch()
        actions_layout.addWidget(self.btn_audio)
        actions_layout.addWidget(self.btn_export_txt)
        actions_layout.addWidget(self.btn_export_csv)
        layout.addLayout(actions_layout)

        # Search Bar
        self.search_input = QLineEdit(self)
        self.search_input.setPlaceholderText("Search past transcriptions, dates, languages...")
        self.search_input.textChanged.connect(self.refresh_list)
        layout.addWidget(self.search_input)

        # History List Widget
        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_widget.itemDoubleClicked.connect(self._item_double_clicked)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        self.list_widget.currentItemChanged.connect(lambda *_: self._refresh_audio_button())
        layout.addWidget(self.list_widget)
        
        # Hints
        hint_label = QLabel("Double-click an item to copy it to your cursor.", self)
        hint_label.setObjectName("subtitleLabel")
        hint_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint_label)

    def refresh_list(self):
        self.list_widget.clear()
        query = self.search_input.text().strip()
        
        self.all_entries = hist.load()
        self.displayed_entries = []
        
        query_lower = query.lower()
        
        for idx, entry in enumerate(self.all_entries):
            # Apply search filter
            if query:
                text = entry.get("text", "").lower()
                lang = entry.get("language", "").lower()
                backend = entry.get("backend", "").lower()
                timestamp = entry.get("timestamp", "").lower()
                if (query_lower not in text and 
                    query_lower not in lang and 
                    query_lower not in backend and 
                    query_lower not in timestamp):
                    continue
            
            self.displayed_entries.append(idx)
            
            # Create a stylized custom QListWidgetItem
            item = QListWidgetItem()
            
            # Format text snippet
            snippet = entry.get("text", "").strip()
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            
            audio = "  ·  with recording" if entry_audio_parts(entry) else ""
            display_text = (
                f"[{entry.get('timestamp', '')}]  ·  {entry.get('language', '').upper()} ({entry.get('backend', '')}){audio}\n"
                f"{snippet}"
            )
            item.setText(display_text)
            
            # Give secondary styling to the timestamp header
            font = QFont("Segoe UI", 9)
            item.setFont(font)
            
            self.list_widget.addItem(item)

        self.count_label.setText(f"{len(self.displayed_entries)} entries displayed")
        self._refresh_audio_button()

    def _entry_for_item(self, item):
        row = self.list_widget.row(item) if item is not None else -1
        if 0 <= row < len(self.displayed_entries):
            return self.all_entries[self.displayed_entries[row]]
        return None

    def _refresh_audio_button(self):
        entry = self._entry_for_item(self.list_widget.currentItem())
        self.btn_audio.setEnabled(bool(entry_audio_parts(entry)))

    def _download_selected_audio(self):
        self._download_audio(self._entry_for_item(self.list_widget.currentItem()))

    def _download_audio(self, entry):
        parts = entry_audio_parts(entry)
        if not parts:
            QMessageBox.information(self, "No recording",
                                    "This entry has no saved recording.")
            return
        from ui.meeting_detail import recording_save_path
        stem = "Meeting " + (entry.get("timestamp", "")[:16].replace(":", "-"))
        path = recording_save_path(self, stem)
        if not path:
            return
        self.btn_audio.setEnabled(False)

        def _worker():
            try:
                import audio_export
                out = audio_export.export_recording(parts, path)
                self.sig_audio_done.emit(str(out) if out else "", "" if out else "Nothing to export")
            except Exception as e:
                self.sig_audio_done.emit("", str(e)[:200])

        threading.Thread(target=_worker, daemon=True).start()

    def _on_audio_done(self, path, error):
        self._refresh_audio_button()
        if error:
            QMessageBox.warning(self, "Export failed", error)
            return
        if self.app and hasattr(self.app, "track"):
            self.app.track("meeting_exported", {"target": "audio", "ok": True})
        QMessageBox.information(self, "Recording saved", f"Recording saved:\n{path}")

    def _item_double_clicked(self, item):
        row = self.list_widget.row(item)
        if 0 <= row < len(self.displayed_entries):
            original_idx = self.displayed_entries[row]
            entry = self.all_entries[original_idx]
            text = entry.get("text", "")
            
            # Copy to clipboard
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            
            # Trigger smart paste or tray message
            if self.app:
                # Trigger pasting logic if parent is active
                self.app.paste_text(text)
                self.app.show_tray_hint("Text Copied & Pasted", "Transcription was sent to clipboard and active cursor.")
            else:
                QMessageBox.information(self, "Copied", "Transcription copied to clipboard!")

    def _clear(self):
        if not self.all_entries:
            return

        reply = QMessageBox.question(
            self, "Clear All History",
            "Are you sure you want to permanently delete ALL transcription "
            "history?\n\nThis clears every entry, regardless of what is "
            "currently selected or filtered.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        count = len(self.all_entries)

        # Clear the underlying store first so the action always completes.
        hist.clear()

        # Telemetry must never be able to break the clear/refresh. (The previous
        # code referenced a non-existent ``self.app.version`` here, which raised
        # AttributeError *after* clearing but *before* refreshing - so the list
        # appeared to "not work" even though the data was already gone.)
        try:
            from main import APP_VERSION
            version = getattr(self.app, "version", APP_VERSION) if self.app else APP_VERSION
            telemetry.track(
                "history_cleared",
                {"count": count},
                self.app.cfg if self.app else {},
                version,
            )
        except Exception:
            pass

        # Drop any active selection so a highlighted row can't keep stale state,
        # then repaint the now-empty list.
        self.list_widget.clearSelection()
        self.list_widget.setCurrentItem(None)
        self.refresh_list()

    def _clear_selection(self):
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            return
        
        reply = QMessageBox.question(
            self, "Delete Items",
            f"Are you sure you want to delete the {len(selected_items)} selected entries?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            # Sort original indices in descending order so deletion doesn't offset subsequent indices
            indices_to_delete = []
            for item in selected_items:
                row = self.list_widget.row(item)
                if 0 <= row < len(self.displayed_entries):
                    indices_to_delete.append(self.displayed_entries[row])
            
            indices_to_delete.sort(reverse=True)
            
            # Load, delete, and save
            entries = hist.load()
            for idx in indices_to_delete:
                if 0 <= idx < len(entries):
                    del entries[idx]
            hist.save_all(entries)
            
            self.refresh_list()

    def _show_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return
            
        menu = QMenu(self)
        
        action_copy = QAction("Copy Text", self)
        action_copy.triggered.connect(lambda: self._copy_selected_text(item))
        menu.addAction(action_copy)

        entry = self._entry_for_item(item)
        if entry_audio_parts(entry):
            action_audio = QAction("Download Audio", self)
            action_audio.triggered.connect(lambda: self._download_audio(entry))
            menu.addAction(action_audio)

        action_delete = QAction("Delete Entry", self)
        action_delete.triggered.connect(lambda: self._delete_specific_item(item))
        menu.addAction(action_delete)
        
        menu.exec_(self.list_widget.mapToGlobal(pos))

    def _copy_selected_text(self, item):
        row = self.list_widget.row(item)
        if 0 <= row < len(self.displayed_entries):
            original_idx = self.displayed_entries[row]
            text = self.all_entries[original_idx].get("text", "")
            QApplication.clipboard().setText(text)

    def _delete_specific_item(self, item):
        row = self.list_widget.row(item)
        if 0 <= row < len(self.displayed_entries):
            original_idx = self.displayed_entries[row]
            hist.delete(original_idx)
            self.refresh_list()

    def _export(self, fmt):
        if not self.displayed_entries:
            QMessageBox.warning(self, "Export Failed", "There are no entries to export.")
            return
            
        # Get active entries (currently displayed after filter)
        export_list = [self.all_entries[idx] for idx in self.displayed_entries]
        
        default_name = f"transcribe_history.{fmt}"
        file_filter = "CSV Files (*.csv)" if fmt == "csv" else "Text Files (*.txt)"
        
        path, _ = QFileDialog.getSaveFileName(
            self, "Export History",
            os.path.expanduser(f"~/Documents/{default_name}"),
            file_filter
        )
        
        if path:
            try:
                if fmt == "csv":
                    count = hist.export_csv(path, export_list)
                else:
                    count = hist.export_txt(path, export_list)
                if self.app and hasattr(self.app, "track"):
                    self.app.track("history_exported", {"format": fmt, "count": count, "from": "history"})

                QMessageBox.information(
                    self, "Export Complete",
                    f"Successfully exported {count} entries to {os.path.basename(path)}!"
                )
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Failed to export history: {e}")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            event.ignore()
        else:
            super().keyPressEvent(event)
