# Modern Tabbed Settings Panel in PySide6

import os
import threading
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QCheckBox, QTabWidget, QWidget, QLineEdit, QTextEdit, QFrame, QScrollArea,
    QMessageBox, QGridLayout, QProgressBar, QStackedWidget, QRadioButton,
    QButtonGroup, QSizePolicy,
)
from PySide6.QtGui import QFont, QColor
import local_llm
import history as hist
import telemetry
import action_api
import actions

class DownloadProgressSignal(QObject):
    progress = Signal(str, int, int, int) # model_name, percent, downloaded, total
    finished = Signal(str, str)          # model_name, state ("downloaded" / "failed")

class Settings(QDialog):
    def __init__(self, parent=None, main_app=None):
        super().__init__(parent)
        self.app = main_app
        
        import copy
        self.cfg_working = copy.deepcopy(self.app.cfg if self.app else {})
        
        self.setWindowTitle("Settings")
        self.setMinimumSize(580, 680)
        self.resize(600, 760)
        
        # Apply global stylesheet
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)

        # Thread-safe downloader signals
        self.downloader_signals = DownloadProgressSignal()
        self.downloader_signals.progress.connect(self._on_download_progress)
        self.downloader_signals.finished.connect(self._on_download_finished)

        # Local cache states
        self._model_states = {}
        self._model_progress = {}
        self._local_llm_states = {}
        self._local_llm_progress = {}
        self.capturing = False
        
        # Whisper model card controls references
        self.whisper_cards = {}
        self.llm_cards = {}

        # Scan model downloaded statuses initially
        self._scan_model_statuses()
        
        self._build_ui()
        self._set_backend_layout(self.cfg_working.get("action_api_provider", "api_openai_compatible") if self.app else "api_openai_compatible")
        self._load_values_into_widgets()

    def showEvent(self, event):
        super().showEvent(event)
        import copy
        if self.app:
            self.cfg_working = copy.deepcopy(self.app.cfg)
        self._scan_model_statuses()
        self._load_values_into_widgets()
        for name in list(self.whisper_cards.keys()):
            self._update_whisper_card_ui(name)
        for name in list(self.llm_cards.keys()):
            self._update_llm_card_ui(name)

    def _on_save_clicked(self):
        self._sync_action_settings_from_widgets()
        if self.app:
            self.app.cfg.update(self.cfg_working)
            self.app.save_config()
            self.app.apply_tray_bindings()
        self.accept()

    def _sync_action_settings_from_widgets(self):
        """Persist output mode + engine from widgets (not only on toggle signals)."""
        if hasattr(self, "rb_smart"):
            self.cfg_working["output_action"] = (
                actions.ACTION_SMART_AUTO if self.rb_smart.isChecked()
                else actions.ACTION_TRANSCRIBE_ONLY
            )
        if hasattr(self, "combo_engine"):
            self._save_action_configs()

    def _load_values_into_widgets(self):
        # 1. Hotkey
        hk = self.cfg_working.get("hotkey", "alt+r")
        self.btn_hotkey.setText(self._fmt_hotkey(None, hk).upper())
        self.capturing = False
        self.btn_hotkey.setStyleSheet("font-weight: bold; min-height: 36px; border-color: #3b82f6;")

        # 1b. Output mode (Transcribe only vs Smart actions)
        if hasattr(self, "rb_smart"):
            current_mode = self.cfg_working.get("output_action", "transcribe_only")
            self.rb_smart.blockSignals(True)
            self.rb_transcribe.blockSignals(True)
            if current_mode == actions.ACTION_SMART_AUTO:
                self.rb_smart.setChecked(True)
            else:
                self.rb_transcribe.setChecked(True)
            self.rb_smart.blockSignals(False)
            self.rb_transcribe.blockSignals(False)
            if hasattr(self, "engine_section"):
                self.engine_section.setEnabled(self.rb_smart.isChecked())
        
        # 2. Spoken Language
        idx = self.combo_lang.findData(self.cfg_working.get("language", "auto"))
        if idx >= 0:
            self.combo_lang.blockSignals(True)
            self.combo_lang.setCurrentIndex(idx)
            self.combo_lang.blockSignals(False)
            
        # 3. Custom Vocab
        self.vocab_input.blockSignals(True)
        self.vocab_input.setPlainText(self.cfg_working.get("initial_prompt", ""))
        self.vocab_input.blockSignals(False)
        
        # 4. Privacy Mode
        self.chk_privacy.blockSignals(True)
        self.chk_privacy.setChecked(bool(self.cfg_working.get("privacy_mode", False)))
        self.chk_privacy.blockSignals(False)
        
        # 5. Engine Provider
        provider = self.cfg_working.get("action_model", "rule_based")
        import local_llm
        if provider in (local_llm.QWEN_TINY_ID, local_llm.QWEN_3B_ID, local_llm.QWEN_7B_ID, local_llm.GEMMA_2B_ID):
            provider = "local_llm"
        idx = self.combo_engine.findData(provider)
        if idx >= 0:
            self.combo_engine.blockSignals(True)
            self.combo_engine.setCurrentIndex(idx)
            self.combo_engine.blockSignals(False)
            
        # 6. Local Model
        model = self.cfg_working.get("action_model", local_llm.QWEN_TINY_ID)
        if model not in local_llm.MODEL_CATALOG:
            model = local_llm.QWEN_TINY_ID
        idx = self.combo_local_model.findData(model)
        if idx >= 0:
            self.combo_local_model.blockSignals(True)
            self.combo_local_model.setCurrentIndex(idx)
            self.combo_local_model.blockSignals(False)
            
        # 7. Stack layout based on engine
        self._set_backend_layout(provider)
        
        # 8. Telemetry
        self.chk_telemetry.blockSignals(True)
        self.chk_telemetry.setChecked(bool(self.cfg_working.get("analytics_enabled", True)))
        self.chk_telemetry.blockSignals(False)
        
        # 9. About Update button
        if self.app and self.cfg_working.get("pending_update_version"):
            tag = self.cfg_working.get("pending_update_version")
            self.btn_update.setText(f"Install Update {tag}")
            self.btn_update.setObjectName("primaryButton")
        else:
            self.btn_update.setText("Check for Updates")
            self.btn_update.setObjectName("")
        self.btn_update.style().unpolish(self.btn_update)
        self.btn_update.style().polish(self.btn_update)

    def _scan_model_statuses(self):
        # Scan whisper models
        from main import model_downloaded, MODELS
        for name in MODELS:
            self._model_states[name] = "downloaded" if model_downloaded(name) else "missing"
            
        # Scan local LLMs
        for name in local_llm.MODEL_CATALOG:
            self._local_llm_states[name] = "downloaded" if local_llm.model_downloaded(name) else "missing"

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Title Header
        title_label = QLabel("Settings", self)
        title_label.setObjectName("titleLabel")
        layout.addWidget(title_label)

        # Tabs Container
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._create_general_tab(), "General")
        self.tabs.addTab(self._create_models_tab(), "Local Models")
        self.tabs.addTab(self._create_actions_tab(), "AI Actions")
        self.tabs.addTab(self._create_history_tab(), "History")
        
        about_title = "About"
        if self.app and self.cfg_working.get("pending_update_version"):
            about_title = f"About (Update v{self.cfg_working.get('pending_update_version').replace('v', '')}!)"
            
        self.tabs.addTab(self._create_about_tab(), about_title)
        layout.addWidget(self.tabs)

        # Bottom Close Row
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()
        
        btn_cancel = QPushButton("Cancel", self)
        btn_cancel.clicked.connect(self.reject)
        bottom_layout.addWidget(btn_cancel)
        
        btn_save = QPushButton("Save", self)
        btn_save.setObjectName("primaryButton")
        btn_save.clicked.connect(self._on_save_clicked)
        bottom_layout.addWidget(btn_save)
        
        layout.addLayout(bottom_layout)

    # ── TAB 1: General Settings ──────────────────────────────────────────────
    def _create_general_tab(self):
        from main import LANG_NAMES
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        # Hotkey Configuration
        hotkey_frame = QFrame(tab)
        hotkey_frame.setObjectName("cardFrame")
        hk_lay = QVBoxLayout(hotkey_frame)
        hk_lay.addWidget(QLabel("Dictation Hotkey", hotkey_frame))
        
        self.btn_hotkey = QPushButton(self.cfg_working.get("hotkey", "alt+r").upper() if self.app else "ALT+R", hotkey_frame)
        self.btn_hotkey.clicked.connect(self._toggle_capture)
        self.btn_hotkey.setStyleSheet("font-weight: bold; min-height: 36px; border-color: #3b82f6;")
        hk_lay.addWidget(self.btn_hotkey)
        layout.addWidget(hotkey_frame)

        # Spoken Language
        lang_frame = QFrame(tab)
        lang_frame.setObjectName("cardFrame")
        lang_lay = QVBoxLayout(lang_frame)
        lang_lay.addWidget(QLabel("Default Spoken Language", lang_frame))
        
        self.combo_lang = QComboBox(lang_frame)
        for val, label in LANG_NAMES.items():
            self.combo_lang.addItem(label, val)
        if self.app:
            idx = self.combo_lang.findData(self.cfg_working.get("language", "auto"))
            if idx >= 0:
                self.combo_lang.setCurrentIndex(idx)
        self.combo_lang.currentIndexChanged.connect(self._save_general_configs)
        self._configure_dropdown(self.combo_lang, show_all_items=True)
        lang_lay.addWidget(self.combo_lang)
        layout.addWidget(lang_frame)

        # Custom Vocabulary Prompt
        vocab_frame = QFrame(tab)
        vocab_frame.setObjectName("cardFrame")
        vocab_lay = QVBoxLayout(vocab_frame)
        vocab_lay.addWidget(QLabel("Custom Vocabulary (Prompt)", vocab_frame))
        
        self.vocab_input = QTextEdit(vocab_frame)
        self.vocab_input.setPlaceholderText("Add names or complex terms (e.g. Aram, Aibuben, PySide6) to guide Whisper's script.")
        self.vocab_input.setMaximumHeight(80)
        if self.app:
            self.vocab_input.setPlainText(self.cfg_working.get("initial_prompt", ""))
        self.vocab_input.textChanged.connect(self._save_general_configs)
        vocab_lay.addWidget(self.vocab_input)
        layout.addWidget(vocab_frame)

        # Privacy mode checkbox
        self.chk_privacy = QCheckBox("Privacy Mode (Disable local history, force offline local models)", tab)
        if self.app:
            self.chk_privacy.setChecked(bool(self.cfg_working.get("privacy_mode", False)))
        self.chk_privacy.stateChanged.connect(self._save_general_configs)
        layout.addWidget(self.chk_privacy)

        layout.addStretch()
        return tab

    # ── TAB 2: Local Whisper Models ──────────────────────────────────────────
    def _create_models_tab(self):
        from main import MODELS
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        
        scroll = QScrollArea(tab)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        
        scroll_content = QWidget()
        scroll_lay = QVBoxLayout(scroll_content)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(10)
        
        # Header Whisper notice
        whisper_notice = QLabel("Whisper local AI models run offline on your machine.\nSelect your preferred model size based on your system RAM.")
        whisper_notice.setObjectName("subtitleLabel")
        whisper_notice.setStyleSheet("margin-bottom: 8px;")
        scroll_lay.addWidget(whisper_notice)

        # Build custom model card frames dynamically (never redrawn or destroyed!)
        for name, info in MODELS.items():
            card = self._build_whisper_card(name, info)
            self.whisper_cards[name] = card
            scroll_lay.addWidget(card)
            self._update_whisper_card_ui(name)
            
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        return tab

    def _build_whisper_card(self, name, info):
        card = QFrame()
        card.setObjectName("cardFrame")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(14, 14, 14, 14)
        card_lay.setSpacing(8)

        # Title Row
        title_row = QHBoxLayout()
        lbl_name = QLabel(f"Whisper {name.upper()}", card)
        lbl_name.setFont(QFont("Segoe UI", 11, QFont.Bold))
        title_row.addWidget(lbl_name)
        
        lbl_size = QLabel(info.get("size", "N/A"), card)
        lbl_size.setObjectName("badgeLabel")
        title_row.addWidget(lbl_size)
        
        title_row.addStretch()
        
        # State label
        lbl_state = QLabel("", card)
        lbl_state.setObjectName("subtitleLabel")
        card.lbl_state = lbl_state # Store reference in QFrame subclass
        title_row.addWidget(lbl_state)
        card_lay.addLayout(title_row)

        # Specs row
        specs = f"RAM: min {info.get('min_ram')} GB  ·  Speed: {info.get('speed')}"
        if info.get("armenian"):
            specs += f"  ·  {info.get('armenian')}"
        lbl_specs = QLabel(specs, card)
        lbl_specs.setObjectName("subtitleLabel")
        card_lay.addWidget(lbl_specs)

        # Progress bar
        pbar = QProgressBar(card)
        pbar.setVisible(False)
        pbar.setRange(0, 100)
        card.progress_bar = pbar # Reference
        card_lay.addWidget(pbar)

        # Action Buttons
        btn_lay = QHBoxLayout()
        btn_lay.addStretch()
        
        btn_action = QPushButton("Download", card)
        btn_action.clicked.connect(lambda: self._download_whisper(name))
        card.btn_action = btn_action # Reference
        btn_lay.addWidget(btn_action)
        
        btn_remove = QPushButton("Remove", card)
        btn_remove.clicked.connect(lambda: self._remove_whisper(name))
        card.btn_remove = btn_remove # Reference
        btn_lay.addWidget(btn_remove)
        
        card_lay.addLayout(btn_lay)

        return card

    def _update_whisper_card_ui(self, name):
        card = self.whisper_cards.get(name)
        if not card:
            return
            
        state = self._model_states.get(name, "missing")
        is_active = False
        if self.app and self.cfg_working.get("whisper_model") == name:
            is_active = True
        
        # Configure matching visuals
        if state == "downloaded":
            card.btn_remove.setVisible(True)
            card.progress_bar.setVisible(False)
            if is_active:
                card.setObjectName("activeCardFrame")
                card.lbl_state.setText("Active")
                card.lbl_state.setStyleSheet("color: #22c55e; font-weight: bold;")
                card.btn_action.setText("Currently Active")
                card.btn_action.setEnabled(False)
                card.btn_action.setObjectName("")
            else:
                card.setObjectName("cardFrame")
                card.lbl_state.setText("Downloaded")
                card.lbl_state.setStyleSheet("color: #3b82f6; font-weight: bold;")
                card.btn_action.setText("Use Model")
                card.btn_action.setEnabled(True)
                card.btn_action.setObjectName("primaryButton")
        elif state == "downloading":
            card.setObjectName("cardFrame")
            progress = self._model_progress.get(name, {"percent": 0})
            card.lbl_state.setText("Downloading...")
            card.lbl_state.setStyleSheet("color: #3b82f6;")
            card.btn_action.setEnabled(False)
            card.btn_remove.setVisible(False)
            card.progress_bar.setVisible(True)
            card.progress_bar.setValue(progress.get("percent", 0))
        else: # missing or failed
            card.setObjectName("cardFrame")
            card.lbl_state.setText("Not Downloaded")
            card.lbl_state.setStyleSheet("color: #64748b;")
            card.btn_action.setText("Download")
            card.btn_action.setEnabled(True)
            card.btn_action.setObjectName("")
            card.btn_remove.setVisible(False)
            card.progress_bar.setVisible(False)

        # Repolish stylesheet elements
        card.style().unpolish(card)
        card.style().polish(card)
        card.btn_action.style().unpolish(card.btn_action)
        card.btn_action.style().polish(card.btn_action)

    @staticmethod
    def _configure_dropdown(combo, *, show_all_items=False, min_width=280):
        """Consistent combo sizing; avoid clipped / blank popup rows on Windows."""
        combo.setMinimumHeight(40)
        combo.setMinimumWidth(min_width)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if show_all_items and combo.count() > 0:
            combo.setMaxVisibleItems(combo.count())
        view = combo.view()
        view.setUniformItemSizes(True)
        view.setSpacing(0)

    # ── TAB 3: AI Action Engines ─────────────────────────────────────────────
    def _create_actions_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(tab)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        section_label = QLabel("OUTPUT MODE", content)
        section_label.setObjectName("subtitleLabel")
        section_label.setStyleSheet("font-weight: 600; letter-spacing: 0.5px;")
        layout.addWidget(section_label)

        self.mode_section = QWidget(content)
        self.mode_section.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._mode_cards_layout = QVBoxLayout(self.mode_section)
        self._mode_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._mode_cards_layout.setSpacing(12)

        self.output_mode_group = QButtonGroup(content)

        self.card_transcribe = self._build_mode_card(
            self.mode_section,
            "Transcribe only",
            "Paste my exact words. Fast, predictable, no AI processing.",
            checked=False,
        )
        self.rb_transcribe = self.card_transcribe.radio
        self.output_mode_group.addButton(self.rb_transcribe, 0)
        self._mode_cards_layout.addWidget(self.card_transcribe, 1)

        self.card_smart = self._build_mode_card(
            self.mode_section,
            "Smart actions",
            "Detect intent from voice — e.g. say \"translate to russian: …\", "
            "\"write email to John\", or \"make a todo list\". The AI produces "
            "only the result.",
            checked=False,
        )
        self.rb_smart = self.card_smart.radio
        self.rb_smart.setToolTip(
            "When enabled, your dictation is run through a language model "
            "that detects whether you want a translation, email, todo "
            "list, summary, or rewrite — and produces only that output. "
            "If you don't ask for anything specific, your words are pasted "
            "as-is."
        )
        self.output_mode_group.addButton(self.rb_smart, 1)
        self._mode_cards_layout.addWidget(self.card_smart, 1)

        layout.addWidget(self.mode_section, 1)

        current_mode = (self.cfg_working.get("output_action") if self.app else "transcribe_only")
        if current_mode == actions.ACTION_SMART_AUTO:
            self.rb_smart.setChecked(True)
        else:
            self.rb_transcribe.setChecked(True)

        self.rb_transcribe.toggled.connect(self._on_output_mode_changed)
        self.rb_smart.toggled.connect(self._on_output_mode_changed)
        # Make clicking anywhere on the card select that option.
        self.card_transcribe.mousePressEvent = lambda _e: self.rb_transcribe.setChecked(True)
        self.card_smart.mousePressEvent = lambda _e: self.rb_smart.setChecked(True)

        # Engine picker + config (only meaningful when Smart actions is on)
        self.engine_section = QWidget(content)
        engine_section_lay = QVBoxLayout(self.engine_section)
        engine_section_lay.setContentsMargins(0, 0, 0, 0)
        engine_section_lay.setSpacing(12)

        notice = QLabel(
            "Choose which language model will handle Smart actions. "
            "Cloud APIs are fastest and highest-quality; local Qwen / "
            "Gemma keeps everything on your machine.",
            self.engine_section,
        )
        notice.setObjectName("subtitleLabel")
        notice.setWordWrap(True)
        engine_section_lay.addWidget(notice)

        # Primary engine choice
        engine_frame = QFrame(self.engine_section)
        engine_frame.setObjectName("cardFrame")
        engine_lay = QVBoxLayout(engine_frame)
        engine_lay.addWidget(QLabel("Primary Action Engine", engine_frame))
        
        self.combo_engine = QComboBox(engine_frame)
        self.combo_engine.addItem("Rule-based Formatter (Fast, Local, Offline)", actions.RULE_BASED_ID)
        self.combo_engine.addItem("Google Gemini API (Cloud Engine)", actions.API_GEMINI_ID)
        self.combo_engine.addItem("OpenAI-compatible API (Cloud Engine)", actions.API_OPENAI_ID)
        self.combo_engine.addItem("Anthropic Claude API (Cloud Engine)", actions.API_ANTHROPIC_ID)
        self.combo_engine.addItem("Local Offline LLM Engine (Qwen / Gemma)", "local_llm")
        
        if self.app:
            provider = self.app.cfg.get("action_model", "rule_based")
            # Convert legacy values if present
            if provider in (local_llm.QWEN_TINY_ID, local_llm.QWEN_3B_ID, local_llm.QWEN_7B_ID, local_llm.GEMMA_2B_ID):
                provider = "local_llm"
            idx = self.combo_engine.findData(provider)
            if idx >= 0:
                self.combo_engine.setCurrentIndex(idx)
        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        self._configure_dropdown(self.combo_engine, min_width=320)
        engine_lay.addWidget(self.combo_engine)
        engine_section_lay.addWidget(engine_frame)

        # Stacked Panels for Dynamic Engine configurations
        self.engine_stack = QStackedWidget(self.engine_section)
        self.engine_stack.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        
        # Card 0: Rule-based (Empty config)
        self.card_rule = QWidget()
        lay_rule = QVBoxLayout(self.card_rule)
        lay_rule.setContentsMargins(4, 8, 4, 8)
        lay_rule.addWidget(
            QLabel(
                "No configuration needed for Rule-Based formatting.\n"
                "It executes instantly and offline.",
                self.card_rule,
            )
        )
        self.engine_stack.addWidget(self.card_rule)

        # Card 1: Cloud API Config Panels (Unified look)
        self.card_cloud = QFrame()
        self.card_cloud.setObjectName("cardFrame")
        self.lay_cloud = QVBoxLayout(self.card_cloud)
        self.lay_cloud.setContentsMargins(14, 14, 14, 14)
        self.lay_cloud.setSpacing(10)
        
        self.lay_cloud.addWidget(QLabel("API Key", self.card_cloud))
        self.cloud_api_key = QLineEdit(self.card_cloud)
        self.cloud_api_key.setEchoMode(QLineEdit.Password)
        self.cloud_api_key.textChanged.connect(self._save_action_configs)
        self.lay_cloud.addWidget(self.cloud_api_key)

        self.lbl_cloud_url = QLabel("API Base URL (Optional for standard endpoints)", self.card_cloud)
        self.lay_cloud.addWidget(self.lbl_cloud_url)
        self.cloud_api_url = QLineEdit(self.card_cloud)
        self.cloud_api_url.textChanged.connect(self._save_action_configs)
        self.lay_cloud.addWidget(self.cloud_api_url)

        self.lay_cloud.addWidget(QLabel("API Model Identifier", self.card_cloud))
        self.cloud_api_model = QComboBox(self.card_cloud)
        self.cloud_api_model.setEditable(False)
        self._configure_dropdown(self.cloud_api_model, min_width=320)
        self.cloud_api_model.currentTextChanged.connect(self._on_model_changed)
        self.lay_cloud.addWidget(self.cloud_api_model)
        self.lay_cloud.addStretch(1)

        self.engine_stack.addWidget(self.card_cloud)

        # Card 2: Local LLM Configuration (GGUFs)
        self.card_local_llm = QWidget()
        lay_llm = QVBoxLayout(self.card_local_llm)
        lay_llm.setContentsMargins(0, 0, 0, 0)
        
        # Local LLM selection combo
        llm_pick_frame = QFrame(self.card_local_llm)
        llm_pick_frame.setObjectName("cardFrame")
        llm_p_lay = QVBoxLayout(llm_pick_frame)
        llm_p_lay.addWidget(QLabel("Select Local AI Model", llm_pick_frame))
        
        self.combo_local_model = QComboBox(llm_pick_frame)
        for val, info in local_llm.MODEL_CATALOG.items():
            self.combo_local_model.addItem(info["label"], val)
            
        if self.app:
            model = self.app.cfg.get("action_model", local_llm.QWEN_TINY_ID)
            # Default fallback if cloud was active previously
            if model not in local_llm.MODEL_CATALOG:
                model = local_llm.QWEN_TINY_ID
            idx = self.combo_local_model.findData(model)
            if idx >= 0:
                self.combo_local_model.setCurrentIndex(idx)
        self.combo_local_model.currentIndexChanged.connect(self._on_local_llm_model_changed)
        self._configure_dropdown(self.combo_local_model, show_all_items=True)
        llm_p_lay.addWidget(self.combo_local_model)
        lay_llm.addWidget(llm_pick_frame)

        # GGUF model cards — scroll with the whole tab (no nested mini-scroll)
        llm_cards_label = QLabel("Local model downloads", self.card_local_llm)
        llm_cards_label.setObjectName("subtitleLabel")
        llm_cards_label.setStyleSheet("font-weight: 600; color: #475569; margin-top: 4px;")
        lay_llm.addWidget(llm_cards_label)

        for name, info in local_llm.MODEL_CATALOG.items():
            card = self._build_llm_card(name, info)
            self.llm_cards[name] = card
            lay_llm.addWidget(card)
            self._update_llm_card_ui(name)

        self.engine_stack.addWidget(self.card_local_llm)

        engine_section_lay.addWidget(self.engine_stack)
        layout.addWidget(self.engine_section)
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)

        class _ActionsTabWidthSync(QObject):
            def __init__(self, viewport, target):
                super().__init__(viewport)
                self._target = target

            def eventFilter(self, obj, event):
                if event.type() == QEvent.Type.Resize:
                    w = obj.width()
                    if w > 0:
                        self._target.setMinimumWidth(w)
                return super().eventFilter(obj, event)

        self._actions_width_sync = _ActionsTabWidthSync(scroll.viewport(), content)
        scroll.viewport().installEventFilter(self._actions_width_sync)
        QTimer.singleShot(0, lambda: content.setMinimumWidth(scroll.viewport().width()))

        self._refresh_mode_cards_layout()
        # Initial enabled state: engine controls active only in Smart mode.
        if hasattr(self, "engine_section"):
            self.engine_section.setEnabled(self.rb_smart.isChecked())
        return tab

    def _on_output_mode_changed(self):
        is_smart = self.rb_smart.isChecked()
        # Engine controls stay visible at all times; they're just disabled
        # (grayed out) when Transcribe-only is selected. No hide/expand so the
        # layout never shifts.
        if hasattr(self, "engine_section"):
            self.engine_section.setEnabled(is_smart)
        new_mode = actions.ACTION_SMART_AUTO if is_smart else actions.ACTION_TRANSCRIBE_ONLY
        self.cfg_working["output_action"] = new_mode
        for card, sel in (
            (getattr(self, "card_transcribe", None), not is_smart),
            (getattr(self, "card_smart", None), is_smart),
        ):
            if card is not None:
                card.setStyleSheet(self._mode_card_style(sel))

    def _refresh_mode_cards_layout(self):
        """Keep the two mode cards compact and fixed-height.

        The engine section is always visible now (just enabled/disabled), so
        the cards no longer need to expand to fill space when Transcribe-only
        is selected — that previously caused the giant-empty-card layout.
        """
        if not hasattr(self, "card_transcribe"):
            return
        compact_h = 96
        for card in (self.card_transcribe, self.card_smart):
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            card.setMinimumHeight(compact_h)
            card.setMaximumHeight(140)
        if hasattr(self, "_mode_cards_layout"):
            self._mode_cards_layout.setStretch(0, 0)
            self._mode_cards_layout.setStretch(1, 0)

    def _build_mode_card(self, parent, title, description, checked=False):
        """Full-width selectable mode card (radio + title + description)."""
        card = QFrame(parent)
        card.setObjectName("cardFrame")
        card.setStyleSheet(self._mode_card_style(checked))
        card.setCursor(Qt.PointingHandCursor)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        card.setMinimumHeight(120)

        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(0)

        row_host = QWidget(card)
        row_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        row = QHBoxLayout(row_host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)

        radio = QRadioButton(row_host)
        radio.setChecked(checked)
        row.addWidget(radio, 0, Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(4)

        lbl_title = QLabel(title, row_host)
        lbl_title.setStyleSheet("font-size: 15px; font-weight: 600; color: #0f172a;")
        lbl_title.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        text_col.addWidget(lbl_title)

        lbl_desc = QLabel(description, row_host)
        lbl_desc.setObjectName("subtitleLabel")
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet("color: #64748b; font-size: 13px;")
        lbl_desc.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        text_col.addWidget(lbl_desc)

        row.addLayout(text_col, 1)
        outer.addWidget(row_host, 0, Qt.AlignTop)
        outer.addStretch(1)
        card.radio = radio
        return card

    @staticmethod
    def _mode_card_style(selected):
        if selected:
            return (
                "QFrame#cardFrame {"
                "  border: 1.5px solid #3b82f6;"
                "  border-radius: 8px;"
                "  background: #eff6ff;"
                "}"
            )
        return (
            "QFrame#cardFrame {"
            "  border: 1px solid #e2e8f0;"
            "  border-radius: 8px;"
            "  background: #ffffff;"
            "}"
        )

    def _build_llm_card(self, name, info):
        card = QFrame()
        card.setObjectName("cardFrame")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(14, 14, 14, 14)
        card_lay.setSpacing(8)

        # Title row
        t_row = QHBoxLayout()
        lbl_name = QLabel(info.get("label", "N/A"), card)
        lbl_name.setFont(QFont("Segoe UI", 11, QFont.Bold))
        t_row.addWidget(lbl_name)
        
        lbl_size = QLabel(f"{info.get('size', 0) // 1000000} MB", card)
        lbl_size.setObjectName("badgeLabel")
        t_row.addWidget(lbl_size)
        
        t_row.addStretch()
        
        lbl_state = QLabel("", card)
        lbl_state.setObjectName("subtitleLabel")
        card.lbl_state = lbl_state
        t_row.addWidget(lbl_state)
        card_lay.addLayout(t_row)

        # Info details
        lbl_desc = QLabel(info.get("description", ""), card)
        lbl_desc.setObjectName("subtitleLabel")
        lbl_desc.setWordWrap(True)
        card_lay.addWidget(lbl_desc)

        # Specs details
        specs = f"RAM: min {info.get('min_ram')} GB  ·  GPU Acceleration: {'Yes' if info.get('gpu_recommended') else 'No'}"
        lbl_specs = QLabel(specs, card)
        lbl_specs.setObjectName("subtitleLabel")
        card_lay.addWidget(lbl_specs)

        # Progress bar
        pbar = QProgressBar(card)
        pbar.setVisible(False)
        pbar.setRange(0, 100)
        card.progress_bar = pbar
        card_lay.addWidget(pbar)

        # Action Buttons
        btn_lay = QHBoxLayout()
        btn_lay.addStretch()
        
        btn_action = QPushButton("Download", card)
        btn_action.clicked.connect(lambda: self._download_llm(name))
        card.btn_action = btn_action
        btn_lay.addWidget(btn_action)
        
        btn_remove = QPushButton("Remove", card)
        btn_remove.clicked.connect(lambda: self._remove_llm(name))
        card.btn_remove = btn_remove
        btn_lay.addWidget(btn_remove)
        
        card_lay.addLayout(btn_lay)

        return card

    def _update_llm_card_ui(self, name):
        card = self.llm_cards.get(name)
        if not card:
            return
            
        state = self._local_llm_states.get(name, "missing")
        is_active = False
        if self.app and self.cfg_working.get("action_model") == name:
            is_active = True
        
        if state == "downloaded":
            card.btn_remove.setVisible(True)
            card.progress_bar.setVisible(False)
            if is_active:
                card.setObjectName("activeCardFrame")
                card.lbl_state.setText("Active")
                card.lbl_state.setStyleSheet("color: #22c55e; font-weight: bold;")
                card.btn_action.setText("Currently Active")
                card.btn_action.setEnabled(False)
                card.btn_action.setObjectName("")
            else:
                card.setObjectName("cardFrame")
                card.lbl_state.setText("Downloaded")
                card.lbl_state.setStyleSheet("color: #3b82f6; font-weight: bold;")
                card.btn_action.setText("Use Model")
                card.btn_action.setEnabled(True)
                card.btn_action.setObjectName("primaryButton")
        elif state == "downloading":
            card.setObjectName("cardFrame")
            progress = self._local_llm_progress.get(name, {"percent": 0})
            card.lbl_state.setText("Downloading...")
            card.lbl_state.setStyleSheet("color: #3b82f6;")
            card.btn_action.setEnabled(False)
            card.btn_remove.setVisible(False)
            card.progress_bar.setVisible(True)
            card.progress_bar.setValue(progress.get("percent", 0))
        else: # missing or failed
            card.setObjectName("cardFrame")
            card.lbl_state.setText("Not Downloaded")
            card.lbl_state.setStyleSheet("color: #64748b;")
            card.btn_action.setText("Download")
            card.btn_action.setEnabled(True)
            card.btn_action.setObjectName("")
            card.btn_remove.setVisible(False)
            card.progress_bar.setVisible(False)

        # Repolish
        card.style().unpolish(card)
        card.style().polish(card)
        card.btn_action.style().unpolish(card.btn_action)
        card.btn_action.style().polish(card.btn_action)

    def _on_engine_changed(self, idx):
        provider = self.combo_engine.itemData(idx)
        self._set_backend_layout(provider)
        self._save_action_configs()

    def _on_local_llm_model_changed(self, idx):
        model_id = self.combo_local_model.itemData(idx)
        if self.app:
            self.cfg_working["action_model"] = model_id

    def _set_backend_layout(self, provider):
        if provider == actions.RULE_BASED_ID:
            self.engine_stack.setCurrentIndex(0)
        elif provider == "local_llm":
            self.engine_stack.setCurrentIndex(2)
        else:
            self.engine_stack.setCurrentIndex(1)
            
            # Block signals to prevent cascade updates during text entry
            self.cloud_api_key.blockSignals(True)
            self.cloud_api_url.blockSignals(True)
            self.cloud_api_model.blockSignals(True)
            
            self.cloud_api_model.clear()
            
            if provider == actions.API_GEMINI_ID:
                self.lbl_cloud_url.setVisible(False)
                self.cloud_api_url.setVisible(False)
                models_info = [
                    ("gemini-2.5-flash (~$0.075 / 1M tokens)", "gemini-2.5-flash"),
                    ("gemini-2.5-flash-lite (~$0.0375 / 1M tokens)", "gemini-2.5-flash-lite"),
                    ("gemini-2.5-pro (~$1.25 / 1M tokens)", "gemini-2.5-pro"),
                    ("gemini-3.5-flash (~$0.075 / 1M tokens)", "gemini-3.5-flash"),
                    ("gemini-3.1-flash-lite (~$0.0375 / 1M tokens)", "gemini-3.1-flash-lite"),
                    ("gemini-1.5-flash (~$0.075 / 1M tokens)", "gemini-1.5-flash"),
                ]
                for label, model_id in models_info:
                    self.cloud_api_model.addItem(label, model_id)
                self.cloud_api_model.addItem("Custom Model...")
                self.cloud_api_key.setText(self.cfg_working.get("google_api_key", ""))
                
                saved_model = self.cfg_working.get("action_api_model", "") or "gemini-2.5-flash"
                idx = self.cloud_api_model.findData(saved_model)
                if idx < 0:
                    idx = self.cloud_api_model.findText(saved_model)
                    if idx < 0 and saved_model != "Custom Model...":
                        insert_idx = max(0, self.cloud_api_model.count() - 1)
                        self.cloud_api_model.insertItem(insert_idx, saved_model)
                        idx = insert_idx
                if idx >= 0:
                    self.cloud_api_model.setCurrentIndex(idx)
                
            elif provider == actions.API_ANTHROPIC_ID:
                self.lbl_cloud_url.setVisible(False)
                self.cloud_api_url.setVisible(False)
                models_info = [
                    ("claude-3-5-sonnet-latest (~$3.00 / 1M tokens)", "claude-3-5-sonnet-latest"),
                    ("claude-3-haiku-20240307 (~$0.25 / 1M tokens)", "claude-3-haiku-20240307"),
                    ("claude-3-opus-20240229 (~$15.00 / 1M tokens)", "claude-3-opus-20240229"),
                ]
                for label, model_id in models_info:
                    self.cloud_api_model.addItem(label, model_id)
                self.cloud_api_model.addItem("Custom Model...")
                self.cloud_api_key.setText(self.cfg_working.get("action_api_key", ""))
                
                saved_model = self.cfg_working.get("action_api_model", "") or "claude-3-5-sonnet-latest"
                idx = self.cloud_api_model.findData(saved_model)
                if idx < 0:
                    idx = self.cloud_api_model.findText(saved_model)
                    if idx < 0 and saved_model != "Custom Model...":
                        insert_idx = max(0, self.cloud_api_model.count() - 1)
                        self.cloud_api_model.insertItem(insert_idx, saved_model)
                        idx = insert_idx
                if idx >= 0:
                    self.cloud_api_model.setCurrentIndex(idx)
                
            else: # OpenAI
                self.lbl_cloud_url.setVisible(True)
                self.cloud_api_url.setVisible(True)
                self.cloud_api_url.setPlaceholderText("https://api.openai.com/v1")
                models_info = [
                    ("gpt-4o-mini (~$0.15 / 1M tokens)", "gpt-4o-mini"),
                    ("gpt-4o (~$2.50 / 1M tokens)", "gpt-4o"),
                    ("gpt-5.4-mini (~$0.10 / 1M tokens)", "gpt-5.4-mini"),
                    ("gpt-4.8 (~$2.00 / 1M tokens)", "gpt-4.8"),
                    ("gpt-4.6 (~$1.80 / 1M tokens)", "gpt-4.6"),
                    ("gpt-4.5 (~$1.50 / 1M tokens)", "gpt-4.5"),
                    ("gpt-3.5-turbo (~$0.50 / 1M tokens)", "gpt-3.5-turbo"),
                ]
                for label, model_id in models_info:
                    self.cloud_api_model.addItem(label, model_id)
                self.cloud_api_model.addItem("Custom Model...")
                self.cloud_api_key.setText(self.cfg_working.get("action_api_key", ""))
                self.cloud_api_url.setText(self.cfg_working.get("action_api_base_url", ""))
                
                saved_model = self.cfg_working.get("action_api_model", "") or "gpt-4o-mini"
                idx = self.cloud_api_model.findData(saved_model)
                if idx < 0:
                    idx = self.cloud_api_model.findText(saved_model)
                    if idx < 0 and saved_model != "Custom Model...":
                        insert_idx = max(0, self.cloud_api_model.count() - 1)
                        self.cloud_api_model.insertItem(insert_idx, saved_model)
                        idx = insert_idx
                if idx >= 0:
                    self.cloud_api_model.setCurrentIndex(idx)
                
            self.cloud_api_key.blockSignals(False)
            self.cloud_api_url.blockSignals(False)
            self.cloud_api_model.blockSignals(False)

    def _on_model_changed(self, text):
        if text == "Custom Model...":
            from PySide6.QtWidgets import QInputDialog
            self.cloud_api_model.blockSignals(True)
            custom_model, ok = QInputDialog.getText(
                self,
                "Custom Model",
                "Enter valid API model identifier (e.g. 'deepseek-chat'):\n\n"
                "Warning: Entering a random name like 'mango' will cause API requests to fail.\n"
                "Please make sure it matches a model identifier supported by your API provider.",
                text=""
            )
            custom_model = custom_model.strip() if ok else ""
            if ok and custom_model:
                idx = self.cloud_api_model.findText(custom_model)
                if idx < 0:
                    insert_idx = max(0, self.cloud_api_model.count() - 1)
                    self.cloud_api_model.insertItem(insert_idx, custom_model)
                    idx = insert_idx
                self.cloud_api_model.setCurrentIndex(idx)
            else:
                prev_model = self.cfg_working.get("action_api_model", "")
                idx = self.cloud_api_model.findData(prev_model)
                if idx < 0:
                    idx = self.cloud_api_model.findText(prev_model)
                if idx >= 0:
                    self.cloud_api_model.setCurrentIndex(idx)
                else:
                    self.cloud_api_model.setCurrentIndex(0)
            self.cloud_api_model.blockSignals(False)
            
        self._save_action_configs()

    def _save_general_configs(self):
        if not self.app:
            return
        self.cfg_working["language"] = self.combo_lang.currentData()
        self.cfg_working["initial_prompt"] = self.vocab_input.toPlainText().strip()
        self.cfg_working["privacy_mode"] = self.chk_privacy.isChecked()
        if self.chk_privacy.isChecked():
            self.cfg_working["save_history"] = False
            if hasattr(self, "chk_history"):
                self.chk_history.blockSignals(True)
                self.chk_history.setChecked(False)
                self.chk_history.blockSignals(False)

    def _save_action_configs(self):
        if not self.app:
            return
        provider = self.combo_engine.currentData()
        
        if provider == "local_llm":
            self.cfg_working["action_model"] = self.combo_local_model.currentData()
        elif provider == actions.RULE_BASED_ID:
            self.cfg_working["action_model"] = actions.RULE_BASED_ID
        else: # Cloud API config saving
            self.cfg_working["action_model"] = provider
            key_text = self.cloud_api_key.text().strip()
            
            if provider == actions.API_GEMINI_ID:
                self.cfg_working["google_api_key"] = key_text
            else:
                self.cfg_working["action_api_key"] = key_text
                self.cfg_working["action_api_base_url"] = self.cloud_api_url.text().strip()
                
            saved_val = self.cloud_api_model.currentData()
            if not saved_val:
                saved_val = self.cloud_api_model.currentText().strip()
            self.cfg_working["action_api_model"] = saved_val
            self.cfg_working["action_api_provider"] = action_api.normalize_provider(provider)

    # ── TAB 4: History & Telemetry ───────────────────────────────────────────
    def _create_history_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        hist_frame = QFrame(tab)
        hist_frame.setObjectName("cardFrame")
        h_lay = QVBoxLayout(hist_frame)
        h_lay.addWidget(QLabel("Transcription History Database", hist_frame))
        
        btn_lay = QHBoxLayout()
        btn_clear = QPushButton("Clear All History", hist_frame)
        btn_clear.clicked.connect(self._clear_all_history)
        btn_lay.addWidget(btn_clear)
        
        btn_exp_csv = QPushButton("Export CSV", hist_frame)
        btn_exp_csv.clicked.connect(lambda: self._export_history("csv"))
        btn_lay.addWidget(btn_exp_csv)
        
        btn_exp_txt = QPushButton("Export TXT", hist_frame)
        btn_exp_txt.clicked.connect(lambda: self._export_history("txt"))
        btn_lay.addWidget(btn_exp_txt)
        h_lay.addLayout(btn_lay)
        layout.addWidget(hist_frame)

        # History toggle checkbox
        self.chk_history = QCheckBox("Save local transcription history", tab)
        if self.app:
            self.chk_history.setChecked(bool(self.cfg_working.get("save_history", True)))
        self.chk_history.stateChanged.connect(self._save_history_config)
        layout.addWidget(self.chk_history)

        # Telemetry Consent checkbox
        self.chk_telemetry = QCheckBox("Share anonymous usage metrics to improve Armenian AI models", tab)
        if self.app:
            self.chk_telemetry.setChecked(bool(self.app.cfg.get("analytics_enabled", True)))
        self.chk_telemetry.stateChanged.connect(self._save_telemetry_config)
        layout.addWidget(self.chk_telemetry)

        layout.addStretch()
        return tab

    def _clear_all_history(self):
        reply = QMessageBox.question(
            self, "Clear History",
            "Are you sure you want to permanently delete all transcription history?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            hist.clear()
            QMessageBox.information(self, "Success", "History has been cleared successfully.")

    def _export_history(self, fmt):
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
                    count = hist.export_csv(path)
                else:
                    count = hist.export_txt(path)
                QMessageBox.information(self, "Success", f"Exported {count} entries successfully!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export history: {e}")

    def _save_history_config(self):
        if not self.app:
            return
        self.cfg_working["save_history"] = self.chk_history.isChecked()

    def _save_telemetry_config(self):
        if not self.app:
            return
        self.cfg_working["analytics_enabled"] = self.chk_telemetry.isChecked()

    # ── TAB 5: About & Updates ───────────────────────────────────────────────
    def _create_about_tab(self):
        from main import APP_VERSION
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        logo_frame = QFrame(tab)
        logo_frame.setObjectName("cardFrame")
        l_lay = QVBoxLayout(logo_frame)
        l_lay.setAlignment(Qt.AlignCenter)
        
        lbl_logo = QLabel("Transcribe", logo_frame)
        lbl_logo.setObjectName("titleLabel")
        lbl_logo.setStyleSheet("font-size: 22px; color: #3b82f6; font-weight: bold;")
        l_lay.addWidget(lbl_logo)
        
        lbl_ver = QLabel(f"Version {APP_VERSION}", logo_frame)
        lbl_ver.setObjectName("subtitleLabel")
        l_lay.addWidget(lbl_ver)
        layout.addWidget(logo_frame)

        desc_label = QLabel(
            "An always-on private dictation tool optimized for Armenian, English, and Russian speakers.\n"
            "Runs offline using Whisper AI or integrates with Google Cloud Speech API.",
            tab
        )
        desc_label.setWordWrap(True)
        desc_label.setObjectName("subtitleLabel")
        desc_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc_label)

        # Update checking button
        self.btn_update = QPushButton("Check for Updates", tab)
        if self.app and self.cfg_working.get("pending_update_version"):
            tag = self.cfg_working.get("pending_update_version")
            self.btn_update.setText(f"Install Update {tag}")
            self.btn_update.setObjectName("primaryButton")
        self.btn_update.clicked.connect(self._check_for_updates)
        layout.addWidget(self.btn_update)

        layout.addStretch()
        return tab

    def _check_for_updates(self):
        if self.app and self.cfg_working.get("pending_update_version"):
            tag = self.cfg_working.get("pending_update_version")
            self._on_download_finished("update", f"available:{tag}")
            return

        from main import APP_VERSION
        self.btn_update.setText("Checking...")
        self.btn_update.setEnabled(False)
        
        def _check():
            import requests
            try:
                resp = requests.get("https://api.github.com/repos/Aram2K/transcribe-app/releases/latest", timeout=8, proxies={"http": None, "https": None})
                if resp.status_code == 200:
                    data = resp.json()
                    tag = data.get("tag_name", "")
                    curr = APP_VERSION
                    
                    from main import _parse_version
                    if tag and _parse_version(tag) > _parse_version(curr):
                        self.downloader_signals.finished.emit("update", f"available:{tag}")
                        return
                    self.downloader_signals.finished.emit("update", "latest")
                else:
                    self.downloader_signals.finished.emit("update", "error")
            except Exception as e:
                import logging
                logging.error("Updater request error: %s", e)
                self.downloader_signals.finished.emit("update", "error")
                
        threading.Thread(target=_check, daemon=True).start()

    # ── Model Downloaders & Handlers (Thread-Safe integration) ───────────────
    def _download_whisper(self, name):
        if self._model_states.get(name) == "downloaded":
            if self.app:
                self.cfg_working["whisper_model"] = name
            for m_name in list(self.whisper_cards.keys()):
                self._update_whisper_card_ui(m_name)
            return

        if self._model_states.get(name) == "downloading":
            return
            
        self._model_states[name] = "downloading"
        self._model_progress[name] = {"percent": 0}
        self._update_whisper_card_ui(name)
        
        def _on_prog(pct, got, total):
            self.downloader_signals.progress.emit(name, pct or 0, got, total)
            
        def _run():
            from main import download_whisper_model
            try:
                download_whisper_model(name, on_progress=_on_prog)
                self.downloader_signals.finished.emit(name, "downloaded")
            except Exception as e:
                self.downloader_signals.finished.emit(name, "failed")
                
        threading.Thread(target=_run, daemon=True).start()

    def _remove_whisper(self, name):
        from main import remove_whisper_model
        reply = QMessageBox.question(
            self, "Remove Model",
            f"Remove the local Whisper {name.upper()} model files from this computer?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            if self.app:
                self.app.recorder.unload_model(name)
            ok = remove_whisper_model(name)
            if ok:
                self._model_states[name] = "missing"
                self._update_whisper_card_ui(name)
                QMessageBox.information(self, "Success", f"Whisper {name.upper()} removed successfully.")

    def _download_llm(self, name):
        if self._local_llm_states.get(name) == "downloaded":
            if self.app:
                self.cfg_working["action_model"] = name
            if hasattr(self, "combo_local_model"):
                idx = self.combo_local_model.findData(name)
                if idx >= 0:
                    self.combo_local_model.blockSignals(True)
                    self.combo_local_model.setCurrentIndex(idx)
                    self.combo_local_model.blockSignals(False)
            for m_name in list(self.llm_cards.keys()):
                self._update_llm_card_ui(m_name)
            return

        if self._local_llm_states.get(name) == "downloading":
            return
            
        self._local_llm_states[name] = "downloading"
        self._local_llm_progress[name] = {"percent": 0}
        self._update_llm_card_ui(name)
        
        def _on_prog(pct, got, total):
            self.downloader_signals.progress.emit(f"llm:{name}", pct or 0, got, total)
            
        def _run():
            try:
                local_llm.download_model(name, on_progress=_on_prog)
                self.downloader_signals.finished.emit(f"llm:{name}", "downloaded")
            except Exception:
                self.downloader_signals.finished.emit(f"llm:{name}", "failed")
                
        threading.Thread(target=_run, daemon=True).start()

    def _remove_llm(self, name):
        reply = QMessageBox.question(
            self, "Remove Local LLM",
            f"Remove the local {local_llm.MODEL_CATALOG[name]['label']} model files from this computer?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            local_llm.remove_model(name)
            self._local_llm_states[name] = "missing"
            self._update_llm_card_ui(name)
            QMessageBox.information(self, "Success", "Local model files removed successfully.")

    # ── Signal Handlers for thread-safe UI updates ────────────────────────────
    def _on_download_progress(self, model_name, percent, downloaded, total):
        if model_name.startswith("llm:"):
            name = model_name.replace("llm:", "")
            self._local_llm_progress[name] = {"percent": percent}
            self._update_llm_card_ui(name)
        else:
            self._model_progress[model_name] = {"percent": percent}
            self._update_whisper_card_ui(model_name)

    def _on_download_finished(self, model_name, state):
        from main import PROJECT_GITHUB_URL
        if model_name == "update":
            self.btn_update.setEnabled(True)
            self.btn_update.setText("Check for Updates")
            if state.startswith("available:"):
                tag = state.split(":")[1]
                reply = QMessageBox.question(
                    self, "Update Available",
                    f"A new version ({tag}) of Transcribe is available!\n\n"
                    "Would you like to automatically download and apply the update now?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
                )
                if reply == QMessageBox.Yes:
                    self.btn_update.setText("Downloading Update...")
                    self.btn_update.setEnabled(False)
                    
                    def _download_and_install():
                        import urllib.request
                        import tempfile
                        import shutil
                        import os
                        import logging
                        
                        try:
                            # Construct the setup url
                            setup_url = f"{PROJECT_GITHUB_URL}/releases/download/{tag}/TranscribeApp-Windows-Setup.exe"
                            
                            # Create a temporary directory in the workspace or standard temp
                            temp_dir = tempfile.gettempdir()
                            dest_path = os.path.join(temp_dir, "TranscribeApp-Windows-Setup.exe")
                            
                            # Fetch the installer
                            req = urllib.request.Request(
                                setup_url,
                                headers={'User-Agent': 'Mozilla/5.0'}
                            )
                            with urllib.request.urlopen(req) as response:
                                with open(dest_path, 'wb') as out_file:
                                    shutil.copyfileobj(response, out_file)
                            
                            # Execute the setup.exe natively
                            os.startfile(dest_path)
                            
                            # Clear pending update from configuration
                            if self.app:
                                self.app.cfg["pending_update_version"] = ""
                                self.app.save_config()
                            
                            # Quit the running instance so that the installer can overwrite the files
                            if self.app:
                                QTimer.singleShot(0, self.app.qapp.quit)
                        except Exception as e:
                            logging.error("Failed to download and execute update installer: %s", e)
                            # Show error in main thread
                            def _show_err():
                                self.btn_update.setText("Check for Updates")
                                self.btn_update.setEnabled(True)
                                QMessageBox.critical(
                                    self, "Update Error",
                                    f"Failed to download the update automatically:\n{e}\n\n"
                                    f"Please update manually from:\n{PROJECT_GITHUB_URL}/releases"
                                )
                            QTimer.singleShot(0, _show_err)
                            
                    threading.Thread(target=_download_and_install, daemon=True).start()
            elif state == "latest":
                QMessageBox.information(self, "No Updates", "You are running the latest version of Transcribe.")
            else:
                QMessageBox.warning(self, "Error", "Could not reach GitHub updates API. Try again later.")
            return

        if model_name.startswith("llm:"):
            name = model_name.replace("llm:", "")
            self._local_llm_states[name] = state
            self._update_llm_card_ui(name)
            if state == "downloaded":
                if self.app:
                    self.cfg_working["action_model"] = name
                # Select it in combo
                idx = self.combo_local_model.findData(name)
                if idx >= 0:
                    self.combo_local_model.setCurrentIndex(idx)
                QMessageBox.information(self, "Download Complete", f"{local_llm.MODEL_CATALOG[name]['label']} is ready for action modes!")
        else:
            self._model_states[model_name] = state
            self._update_whisper_card_ui(model_name)
            if state == "downloaded":
                if self.app:
                    self.cfg_working["whisper_model"] = model_name
                QMessageBox.information(self, "Download Complete", f"Whisper {model_name.upper()} model is ready for local offline dictation!")

    # ── Hotkey Capturing Logic ───────────────────────────────────────────────
    def _toggle_capture(self):
        if self.capturing:
            self.capturing = False
            self.btn_hotkey.setText(self.cfg_working.get("hotkey", "alt+r").upper() if self.app else "ALT+R")
            self.btn_hotkey.setStyleSheet("font-weight: bold; min-height: 36px; border-color: #3b82f6;")
        else:
            self.capturing = True
            self.btn_hotkey.setText("PRESS KEY COMBINATION...")
            self.btn_hotkey.setStyleSheet("font-weight: bold; min-height: 36px; border-color: #ef4444; color: #ef4444;")
            self.setFocus() # Pull focus away so keyPressEvent captures keypresses

    def keyPressEvent(self, event):
        if not self.capturing:
            super().keyPressEvent(event)
            return

        key = event.key()
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return
            
        mods = []
        qt_mods = event.modifiers()
        if qt_mods & Qt.ControlModifier:
            mods.append("ctrl")
        if qt_mods & Qt.AltModifier:
            mods.append("alt")
        if qt_mods & Qt.ShiftModifier:
            mods.append("shift")
        if qt_mods & Qt.MetaModifier:
            mods.append("win")

        KEY_MAP = {
            Qt.Key_Space: "space",
            Qt.Key_Tab: "tab",
            Qt.Key_Enter: "enter",
            Qt.Key_Return: "enter",
            Qt.Key_Escape: "esc",
            Qt.Key_Backspace: "backspace",
            Qt.Key_Delete: "delete",
            Qt.Key_Insert: "insert",
            Qt.Key_Home: "home",
            Qt.Key_End: "end",
            Qt.Key_PageUp: "page up",
            Qt.Key_PageDown: "page down",
            Qt.Key_Up: "up",
            Qt.Key_Down: "down",
            Qt.Key_Left: "left",
            Qt.Key_Right: "right",
            Qt.Key_F1: "f1", Qt.Key_F2: "f2", Qt.Key_F3: "f3", Qt.Key_F4: "f4",
            Qt.Key_F5: "f5", Qt.Key_F6: "f6", Qt.Key_F7: "f7", Qt.Key_F8: "f8",
            Qt.Key_F9: "f9", Qt.Key_F10: "f10", Qt.Key_F11: "f11", Qt.Key_F12: "f12",
        }
        
        key_str = ""
        if key in KEY_MAP:
            key_str = KEY_MAP[key]
        elif 48 <= key <= 90: # letters and digits
            key_str = chr(key).lower()
        else:
            return

        if not mods:
            QMessageBox.warning(self, "Invalid Hotkey", "Dictation hotkeys require at least one modifier key (e.g. Alt or Ctrl).")
            return
            
        hotkey_str = "+".join(mods + [key_str])
        
        if self.app:
            self.cfg_working["hotkey"] = hotkey_str
            
        self.capturing = False
        self.btn_hotkey.setText(hotkey_str.upper())
        self.btn_hotkey.setStyleSheet("font-weight: bold; min-height: 36px; border-color: #3b82f6;")

    @staticmethod
    def _fmt_hotkey(self_or_none, hk):
        if hk.startswith("mouse:"):
            names = {"middle": "Mouse Middle", "left": "Mouse Left",
                     "right": "Mouse Right", "x1": "Mouse Back", "x2": "Mouse Forward"}
            return names.get(hk.split(":")[1], hk)
        caps = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift",
                "win": "Win", "super": "Super"}
        return " + ".join(caps.get(p, p.upper() if len(p) == 1 else p.capitalize())
                          for p in hk.split("+"))
