# Modern Searchable Transcription History View in PySide6

import os
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QMenu, QMessageBox, QFileDialog, QWidget
)
from PySide6.QtGui import QFont, QAction, QIcon, QClipboard
import history as hist
import telemetry

class HistoryWindow(QDialog):
    def __init__(self, parent=None, main_app=None):
        super().__init__(parent)
        self.app = main_app
        
        self.setWindowTitle("History")
        self.setMinimumSize(540, 600)
        self.resize(540, 650)
        
        # Apply the global stylesheet
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)
        
        self._selected_indices = set()
        self.all_entries = []
        self.displayed_entries = [] # Map index of displayed item to index in hist.load() list
        
        self._build_ui()
        self.refresh_list()

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
        
        actions_layout.addWidget(self.btn_clear_all)
        actions_layout.addWidget(self.btn_clear_sel)
        actions_layout.addStretch()
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
            
            display_text = (
                f"[{entry.get('timestamp', '')}]  ·  {entry.get('language', '').upper()} ({entry.get('backend', '')})\n"
                f"{snippet}"
            )
            item.setText(display_text)
            
            # Give secondary styling to the timestamp header
            font = QFont("Segoe UI", 9)
            item.setFont(font)
            
            self.list_widget.addItem(item)
            
        self.count_label.setText(f"{len(self.displayed_entries)} entries displayed")

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
            self, "Clear History",
            "Are you sure you want to permanently delete all transcription history?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            hist.clear()
            telemetry.track("history_cleared", {"count": len(self.all_entries)}, self.app.cfg if self.app else {}, self.app.version if self.app else "1.0.0")
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
                    
                QMessageBox.information(
                    self, "Export Complete",
                    f"Successfully exported {count} entries to {os.path.basename(path)}!"
                )
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Failed to export history: {e}")
