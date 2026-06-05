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
import entitlements

class DownloadProgressSignal(QObject):
    progress = Signal(str, int, int, int) # model_name, percent, downloaded, total
    finished = Signal(str, str)          # model_name, state ("downloaded" / "failed")

class Settings(QDialog):
    mistral_test_finished = Signal(bool, str)
    google_test_finished = Signal(bool, str)
    specs_ready = Signal(str)  # GPU name detected on a worker thread

    def __init__(self, parent=None, main_app=None):
        super().__init__(parent)
        self.app = main_app
        
        import copy
        self.cfg_working = copy.deepcopy(self.app.cfg if self.app else {})
        
        self.setWindowTitle("Settings")
        # Wider so the cloud model cards (badge + state + buttons) are not clipped
        # on the right.
        self.setMinimumSize(660, 680)
        self.resize(720, 780)
        
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
        
        # Cloud API key verification states
        self._mistral_key_verified = None
        self._google_key_verified = None
        
        # Connect test signals
        self.mistral_test_finished.connect(self._on_mistral_test_finished)
        self.google_test_finished.connect(self._on_google_test_finished)
        self.specs_ready.connect(self._on_specs_ready)
        
        # Whisper model card controls references
        self.whisper_cards = {}
        self.llm_cards = {}

        # Scan model downloaded statuses initially
        self._scan_model_statuses()
        
        self._build_ui()
        self.btn_hotkey.installEventFilter(self)
        self.installEventFilter(self)
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
        if hasattr(self, "mistral_cards"):
            for name in list(self.mistral_cards.keys()):
                self._update_mistral_card_ui(name)
        self._update_google_card_ui()
        for name in list(self.llm_cards.keys()):
            self._update_llm_card_ui(name)
        self._update_privacy_ui_state()
        self._populate_history_list()

    def _on_save_clicked(self):
        self._sync_action_settings_from_widgets()
        
        backend = self.cfg_working.get("backend", "local")
        if backend == "mistral":
            mistral_key = self.cfg_working.get("mistral_api_key", "").strip()
            if hasattr(self, "mistral_key_input"):
                mistral_key = self.mistral_key_input.text().strip()
            if not mistral_key:
                self.tabs.setCurrentIndex(1)  # Switch to 'Models' tab
                if hasattr(self, "mistral_key_input"):
                    self.mistral_key_input.setFocus()
                    self.mistral_key_input.setStyleSheet("border: 2px solid #ef4444; background-color: #fef2f2;")
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "API Key Required",
                    "Mistral STT requires a valid Mistral API Key. Please enter it below before saving."
                )
                return
            if getattr(self, "_mistral_key_verified", False) is False:
                self.tabs.setCurrentIndex(1)
                if hasattr(self, "mistral_key_input"):
                    self.mistral_key_input.setFocus()
                    self.mistral_key_input.setStyleSheet("border: 2px solid #ef4444; background-color: #fef2f2;")
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "Invalid API Key",
                    "The provided Mistral API Key failed connection tests. Please test a valid API key before saving."
                )
                return
        elif backend == "google":
            google_key = self.cfg_working.get("google_api_key", "").strip()
            if hasattr(self, "google_key_input"):
                google_key = self.google_key_input.text().strip()
            if not google_key:
                self.tabs.setCurrentIndex(1)  # Switch to 'Models' tab
                if hasattr(self, "google_key_input"):
                    self.google_key_input.setFocus()
                    self.google_key_input.setStyleSheet("border: 2px solid #ef4444; background-color: #fef2f2;")
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "API Key Required",
                    "Google Gemini Speech requires a valid Google AI Studio (Gemini) API key. Please enter it below before saving."
                )
                return
            if getattr(self, "_google_key_verified", False) is False:
                self.tabs.setCurrentIndex(1)
                if hasattr(self, "google_key_input"):
                    self.google_key_input.setFocus()
                    self.google_key_input.setStyleSheet("border: 2px solid #ef4444; background-color: #fef2f2;")
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "Invalid API Key",
                    "The provided Google AI Studio (Gemini) API key failed connection tests. Please test a valid key before saving."
                )
                return

        if self.app:
            self.app.cfg.update(self.cfg_working)
            self.app.save_config()
            self.app.apply_tray_bindings()
        # Save no longer closes the window - just confirm with a small toast.
        self._show_saved_toast()

    def _show_saved_toast(self, text="✓  Settings saved"):
        self._saved_toast.setText(text)
        self._saved_toast.setVisible(True)
        QTimer.singleShot(2000, lambda: self._saved_toast.setVisible(False))

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
        # Initialize verification states from saved config keys
        m_key = self.cfg_working.get("mistral_api_key", "").strip()
        self._mistral_key_verified = True if m_key else False
        if hasattr(self, "lbl_status_mistral"):
            self.lbl_status_mistral.setText("✓ Working (Saved)" if m_key else "Not Configured")
            self.lbl_status_mistral.setStyleSheet("color: #22c55e; font-size: 11px;" if m_key else "color: #64748b; font-size: 11px;")
            
        g_key = self.cfg_working.get("google_api_key", "").strip()
        self._google_key_verified = True if g_key else False
        if hasattr(self, "lbl_status_google"):
            self.lbl_status_google.setText("✓ Working (Saved)" if g_key else "Not Configured")
            self.lbl_status_google.setStyleSheet("color: #22c55e; font-size: 11px;" if g_key else "color: #64748b; font-size: 11px;")

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
                self.engine_section.setEnabled(True)  # always selectable, even in transcribe-only
        
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

        # Header: "Welcome, {name}" + a tier badge (purple Pro / green Free / gray Guest)
        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        self._welcome_label = QLabel("Welcome", self)
        self._welcome_label.setObjectName("titleLabel")
        header_row.addWidget(self._welcome_label)
        self._header_badge = QLabel("GUEST", self)
        self._header_badge.setObjectName("guestBadge")
        header_row.addWidget(self._header_badge, 0, Qt.AlignVCenter)
        header_row.addStretch()
        # Top-right CTA: upgrade (free/trial) or sign-up (guest).
        self._header_cta = QPushButton("", self)
        self._header_cta.setCursor(Qt.PointingHandCursor)
        self._header_cta.setStyleSheet(
            "background-color: #a855f7; border: 1px solid #9333ea; color: white;"
            "font-weight: 700; border-radius: 8px; padding: 6px 14px;"
        )
        self._header_cta.clicked.connect(self._header_cta_clicked)
        self._header_cta.setVisible(False)
        header_row.addWidget(self._header_cta, 0, Qt.AlignVCenter)
        layout.addLayout(header_row)

        # Tabs Container
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._create_general_tab(), "General")
        self.tabs.addTab(self._create_models_tab(), "Models")
        self.tabs.addTab(self._create_actions_tab(), "AI Actions")
        self.tabs.addTab(self._create_history_tab(), "History")
        
        about_title = "About"
        if self.app and self.cfg_working.get("pending_update_version"):
            about_title = f"About (Update v{self.cfg_working.get('pending_update_version').replace('v', '')}!)"
            
        self.tabs.addTab(self._create_about_tab(), about_title)
        self.tabs.addTab(self._create_account_tab(), "Account")
        layout.addWidget(self.tabs)

        # Bottom row - Save stays open + shows a small toast; Close dismisses.
        # Privacy Mode lives here (not in a tab) so it's reachable from anywhere.
        bottom_layout = QHBoxLayout()
        self.chk_privacy = QCheckBox("Privacy Mode", self)
        self.chk_privacy.setToolTip(
            "Keep everything on your device: turns off cloud transcription and "
            "stops saving local history."
        )
        self.chk_privacy.setChecked(bool(self.cfg_working.get("privacy_mode", False)))
        self.chk_privacy.stateChanged.connect(self._on_privacy_toggled)
        bottom_layout.addWidget(self.chk_privacy)
        bottom_layout.addStretch()

        self._saved_toast = QLabel("", self)
        self._saved_toast.setStyleSheet("color: #16a34a; font-weight: 700;")
        self._saved_toast.setVisible(False)
        bottom_layout.addWidget(self._saved_toast)

        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.reject)
        bottom_layout.addWidget(btn_close)

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

        # Your device - so people know their specs and which models fit.
        specs_frame = QFrame(tab)
        specs_frame.setObjectName("cardFrame")
        sp_lay = QVBoxLayout(specs_frame)
        sp_head = QLabel("Your Device", specs_frame)
        sp_head.setStyleSheet("font-weight: 600;")
        sp_lay.addWidget(sp_head)
        self._specs_label = QLabel(self._quick_specs(), specs_frame)
        self._specs_label.setObjectName("subtitleLabel")
        self._specs_label.setWordWrap(True)
        sp_lay.addWidget(self._specs_label)
        layout.addWidget(specs_frame)
        self._detect_gpu_async()

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

        # Meeting Audio Device
        dev_frame = QFrame(tab)
        dev_frame.setObjectName("cardFrame")
        dev_lay = QVBoxLayout(dev_frame)
        dev_head = QHBoxLayout()
        dev_head.addWidget(QLabel("Default Meeting Audio Device", dev_frame))
        dev_head.addStretch()
        self._meeting_pro_badge = QLabel("PRO", dev_frame)
        self._meeting_pro_badge.setObjectName("proBadge")
        self._meeting_pro_badge.setVisible(not self._is_pro())
        dev_head.addWidget(self._meeting_pro_badge)
        dev_lay.addLayout(dev_head)
        
        self.combo_device = QComboBox(dev_frame)
        self._populate_audio_devices()
        self.combo_device.currentIndexChanged.connect(self._save_general_configs)
        self._configure_dropdown(self.combo_device, show_all_items=True)
        dev_lay.addWidget(self.combo_device)
        
        # Start meeting launch button
        self.btn_launch_meeting = QPushButton("Start Smart Meeting Transcription", dev_frame)
        self.btn_launch_meeting.setObjectName("primaryButton")
        self.btn_launch_meeting.setMinimumHeight(38)
        self.btn_launch_meeting.clicked.connect(self._launch_smart_meeting)
        dev_lay.addWidget(self.btn_launch_meeting)
        
        layout.addWidget(dev_frame)

        # Fast cloud transcription (Pro) backend - off by default.
        cloud_frame = QFrame(tab)
        cloud_frame.setObjectName("cardFrame")
        cf_lay = QVBoxLayout(cloud_frame)
        cf_lay.setContentsMargins(18, 12, 18, 12)
        cf_lay.setSpacing(4)
        ch_row = QHBoxLayout()
        self.chk_managed = QCheckBox("Fast cloud transcription", cloud_frame)
        self.chk_managed.setChecked(self.cfg_working.get("backend") == "managed")
        self.chk_managed.stateChanged.connect(self._on_managed_toggled)
        ch_row.addWidget(self.chk_managed)
        self._cloud_pro_badge = QLabel("PRO", cloud_frame)
        self._cloud_pro_badge.setObjectName("proBadge")
        ch_row.addWidget(self._cloud_pro_badge)
        ch_row.addStretch()
        cf_lay.addLayout(ch_row)
        cloud_desc = QLabel("Transcribe on our servers - no setup, no API key.", cloud_frame)
        cloud_desc.setObjectName("subtitleLabel")
        cloud_desc.setWordWrap(True)
        cf_lay.addWidget(cloud_desc)
        layout.addWidget(cloud_frame)

        layout.addStretch()
        return tab

    def _populate_audio_devices(self):
        self.combo_device.clear()
        self.combo_device.addItem("Smart Meeting Mode (record computer sound + microphone)", "smart_meeting")
        self.combo_device.addItem("Standard Mode (record microphone only)", "default_mic")
        
        # Set to current saved meeting capture mode
        current_dev = self.cfg_working.get("meeting_audio_mode", "smart_meeting")
        if current_dev not in ("smart_meeting", "default_mic"):
            current_dev = "smart_meeting"
            
        idx = self.combo_device.findData(str(current_dev))
        if idx >= 0:
            self.combo_device.setCurrentIndex(idx)
        else:
            self.combo_device.setCurrentIndex(0)

    def _on_managed_toggled(self, _state):
        # Managed cloud is Pro-only. Non-Pro toggling on → revert + upsell.
        if self.chk_managed.isChecked():
            if not self._is_pro():
                self.chk_managed.blockSignals(True)
                self.chk_managed.setChecked(False)
                self.chk_managed.blockSignals(False)
                if self.app and hasattr(self.app, "_pro_upsell"):
                    self.app._pro_upsell("Managed cloud transcription")
                return
            self.cfg_working["backend"] = "managed"
            # Cloud and Privacy Mode are mutually exclusive (cloud sends audio off
            # the device; Privacy forces everything local).
            if hasattr(self, "chk_privacy") and self.chk_privacy.isChecked():
                self.chk_privacy.blockSignals(True)
                self.chk_privacy.setChecked(False)
                self.chk_privacy.blockSignals(False)
                self.cfg_working["privacy_mode"] = False
        elif self.cfg_working.get("backend") == "managed":
            self.cfg_working["backend"] = "local"

    def _launch_smart_meeting(self):
        # Meeting recording is Pro-only - prompt to upgrade instead of launching.
        if not self._is_pro():
            if self.app and hasattr(self.app, "_pro_upsell"):
                self.app._pro_upsell("Meeting recording")
            return
        self._save_general_configs()
        if self.app:
            self.app.cfg.update(self.cfg_working)
            self.app.save_config()
            self.app.apply_tray_bindings()
            self.accept()
            self.app.show_meeting()

    # ── System specs ─────────────────────────────────────────────────────────
    def _quick_specs(self, gpu=None):
        """A clean, plain-language one-liner: friendly CPU name + cores, RAM, and
        (only if a real GPU is present) the GPU. No threads, no CUDA noise."""
        try:
            import psutil
            physical = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 0
            ram = psutil.virtual_memory().total / (1024 ** 3)
            cpu = self._cpu_name() or "Processor"
            if physical:
                cpu = f"{cpu} · {physical} cores"
            lbl = lambda t: f"<b style='color:#475569'>{t}</b>&nbsp;&nbsp;"
            gap = "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
            parts = [f"{lbl('CPU')}{cpu}", f"{lbl('RAM')}{ram:.0f} GB"]
            if gpu:
                parts.append(f"{lbl('GPU')}{gpu}")
            return gap.join(parts)
        except Exception:
            return "System information unavailable."

    def _on_specs_ready(self, gpu):
        if hasattr(self, "_specs_label"):
            # Empty string → no discrete GPU → omit the GPU part entirely.
            self._specs_label.setText(self._quick_specs(gpu or None))

    def _speed_phrase(self, rank):
        """Plain-language speed estimate adjusted for this machine's hardware
        (whether a CUDA GPU is usable), instead of a meaningless fixed '~Ns'."""
        if getattr(self, "_cuda", None) is None:
            try:
                import ctranslate2
                self._cuda = ctranslate2.get_cuda_device_count() > 0
            except Exception:
                self._cuda = False
        try:
            rank = int(rank)
        except (TypeError, ValueError):
            rank = 3
        if self._cuda:
            words = {1: "Instant", 2: "Instant", 3: "Very fast", 4: "Very fast", 5: "Fast", 6: "Fast"}
            return f"{words.get(rank, 'Fast')} on your GPU"
        words = {1: "Very fast", 2: "Fast", 3: "Fast", 4: "Moderate", 5: "Slow", 6: "Slower"}
        return f"{words.get(rank, 'Moderate')} on your CPU"

    def _detect_gpu_async(self):
        threading.Thread(target=lambda: self.specs_ready.emit(self._detect_gpu()), daemon=True).start()

    @staticmethod
    def _cpu_name():
        """Friendly processor name, e.g. 'Intel Core i7-14700' or 'AMD Ryzen 7'."""
        import sys, re
        name = None
        try:
            if sys.platform == "win32":
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                    name = winreg.QueryValueEx(k, "ProcessorNameString")[0]
            elif sys.platform == "darwin":
                import subprocess
                name = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                      capture_output=True, text=True, timeout=4).stdout.strip()
            else:
                with open("/proc/cpuinfo", encoding="utf-8") as f:
                    for line in f:
                        if "model name" in line:
                            name = line.split(":", 1)[1].strip()
                            break
        except Exception:
            name = None
        if not name:
            return None
        # Strip marketing noise: (R)/(TM), "CPU"/"Processor", "@ 2.10GHz", "16-Core".
        name = re.sub(r"\(R\)|\(TM\)|\(tm\)", "", name)
        name = re.sub(r"\bCPU\b|\bProcessor\b|\d+-Core", "", name, flags=re.I)
        name = re.sub(r"@.*$", "", name)
        name = re.sub(r"\s+", " ", name).strip()
        return name or None

    @staticmethod
    def _detect_gpu():
        """The primary discrete GPU's name, cleaned. Empty string when the machine
        has only integrated graphics (so the caller omits the GPU line)."""
        import sys, subprocess, re
        names = []
        try:
            if sys.platform == "win32":
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance Win32_VideoController).Name -join '|'"],
                    capture_output=True, text=True, timeout=6,
                )
                names = [n.strip() for n in (out.stdout or "").split("|") if n.strip()]
            elif sys.platform == "darwin":
                out = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                     capture_output=True, text=True, timeout=6)
                for line in (out.stdout or "").splitlines():
                    if "Chipset Model" in line:
                        names.append(line.split(":", 1)[1].strip())
        except Exception:
            names = []

        def clean(n):
            n = re.sub(r"\(R\)|\(TM\)|\(tm\)", "", n)
            return re.sub(r"\s+", " ", n).strip()

        # Apple Silicon / Intel Macs: the chipset model is the GPU worth showing.
        if sys.platform == "darwin":
            return clean(names[0]) if names else ""

        # Windows/Linux: show the discrete GPU; skip integrated/basic adapters.
        discrete = ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla",
                    "radeon", "arc", "instinct")
        for n in names:
            if any(k in n.lower() for k in discrete):
                return clean(n)
        return ""

    # ── TAB 2: Models Configurator ──────────────────────────────────────────
    def _create_models_tab(self):
        from main import MODELS
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        
        scroll = QScrollArea(tab)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        scroll_content = QWidget()
        scroll_lay = QVBoxLayout(scroll_content)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(14)
        
        # Section 1: Local Offline Models
        section_local = QLabel("Local Speech Models (Runs 100% Offline)")
        section_local.setFont(QFont("Segoe UI", 12, QFont.Bold))
        section_local.setStyleSheet("color: #3b82f6; margin-top: 8px; margin-bottom: 2px;")
        scroll_lay.addWidget(section_local)
        
        whisper_notice = QLabel("Whisper local AI models run offline on your machine.\nSelect your preferred model size based on your system RAM.")
        whisper_notice.setObjectName("subtitleLabel")
        whisper_notice.setStyleSheet("margin-bottom: 8px;")
        scroll_lay.addWidget(whisper_notice)

        # Build Whisper cards
        for name, info in MODELS.items():
            card = self._build_whisper_card(name, info)
            self.whisper_cards[name] = card
            scroll_lay.addWidget(card)
            self._update_whisper_card_ui(name)
            
        # Section 2: Mistral AI Cloud STT Models
        section_mistral = QLabel("Mistral AI Voxtral STT Models")
        section_mistral.setFont(QFont("Segoe UI", 12, QFont.Bold))
        section_mistral.setStyleSheet("color: #3b82f6; margin-top: 14px; margin-bottom: 2px;")
        scroll_lay.addWidget(section_mistral)
        
        mistral_notice = QLabel("Mistral's state-of-the-art Voxtral models run via the cloud (API Key Required).")
        mistral_notice.setObjectName("subtitleLabel")
        mistral_notice.setStyleSheet("margin-bottom: 8px;")
        scroll_lay.addWidget(mistral_notice)
        
        # Define Mistral STT catalog
        self.mistral_cards = {}
        self.mistral_model_catalog = {
            "voxtral-mini-latest": {
                "name": "Voxtral Mini",
                "badge": "Fast / Low Cost",
                "specs": "Fastest  ·  smart dictation and audio understanding",
                "description": "Optimized for basic edge and standard transcription tasks."
            },
            "voxtral-small-latest": {
                "name": "Voxtral Small",
                "badge": "Balanced",
                "specs": "Balanced  ·  high accuracy, multilingual",
                "description": "Production-scale high-capability model for balanced performance."
            },
            "voxtral-large-latest": {
                "name": "Voxtral Large",
                "badge": "Highest Quality",
                "specs": "Highest quality  ·  SOTA transcription and understanding",
                "description": "Mistral's flagship, largest, and most capable voice understanding model."
            }
        }
        
        for name, info in self.mistral_model_catalog.items():
            card = self._build_mistral_card(name, info)
            self.mistral_cards[name] = card
            scroll_lay.addWidget(card)
            self._update_mistral_card_ui(name)
            
        # Section 3: Google Cloud STT
        section_google = QLabel("Google Gemini Speech (AI Studio)")
        section_google.setFont(QFont("Segoe UI", 12, QFont.Bold))
        section_google.setStyleSheet("color: #3b82f6; margin-top: 14px; margin-bottom: 2px;")
        scroll_lay.addWidget(section_google)
        
        google_info = {
            "name": "Google Gemini Speech",
            "badge": "AI Studio key",
            "specs": "Fast cloud transcription  ·  120+ languages",
            "description": "Uses your free Google AI Studio (Gemini) API key. Paste it in Cloud API Credentials below."
        }
        self.google_card = self._build_google_card(google_info)
        scroll_lay.addWidget(self.google_card)
        self._update_google_card_ui()
        
        # Section 4: Cloud API Key Credentials
        section_keys = QLabel("Cloud API Credentials")
        section_keys.setFont(QFont("Segoe UI", 12, QFont.Bold))
        section_keys.setStyleSheet("color: #3b82f6; margin-top: 18px; margin-bottom: 2px;")
        scroll_lay.addWidget(section_keys)
        
        self.keys_frame = QFrame(scroll_content)
        self.keys_frame.setObjectName("cardFrame")
        kf_lay = QVBoxLayout(self.keys_frame)
        kf_lay.setContentsMargins(14, 14, 14, 14)
        kf_lay.setSpacing(10)
        
        kf_lay.addWidget(QLabel("Mistral API Key", self.keys_frame))
        self.mistral_key_input = QLineEdit(self.keys_frame)
        self.mistral_key_input.setPlaceholderText("mistral-key...")
        self.mistral_key_input.setEchoMode(QLineEdit.Password)
        self.mistral_key_input.setText(self.cfg_working.get("mistral_api_key", ""))
        self.mistral_key_input.textChanged.connect(self._save_general_configs)
        kf_lay.addWidget(self.mistral_key_input)
        
        m_test_lay = QHBoxLayout()
        self.btn_test_mistral = QPushButton("Test API Key", self.keys_frame)
        self.btn_test_mistral.setObjectName("secondaryButton")
        self.btn_test_mistral.setStyleSheet("padding: 4px 10px; font-size: 11px;")
        self.btn_test_mistral.clicked.connect(self._test_mistral_key)
        self.lbl_status_mistral = QLabel("Not Tested", self.keys_frame)
        self.lbl_status_mistral.setWordWrap(True)
        self.lbl_status_mistral.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lbl_status_mistral.setStyleSheet("color: #64748b; font-size: 11px;")
        m_test_lay.addWidget(self.btn_test_mistral)
        m_test_lay.addWidget(self.lbl_status_mistral)
        kf_lay.addLayout(m_test_lay)
        
        kf_lay.addWidget(QLabel("Google AI Studio (Gemini) API Key", self.keys_frame))
        google_hint = QLabel(
            "Get a free key at aistudio.google.com/apikey (this is a Gemini key, "
            "not a Google Cloud Speech key).", self.keys_frame)
        google_hint.setObjectName("subtitleLabel")
        google_hint.setWordWrap(True)
        kf_lay.addWidget(google_hint)
        self.google_key_input = QLineEdit(self.keys_frame)
        self.google_key_input.setPlaceholderText("AIzaSy...")
        self.google_key_input.setEchoMode(QLineEdit.Password)
        self.google_key_input.setText(self.cfg_working.get("google_api_key", ""))
        self.google_key_input.textChanged.connect(self._save_general_configs)
        kf_lay.addWidget(self.google_key_input)

        g_test_lay = QHBoxLayout()
        self.btn_test_google = QPushButton("Test API Key", self.keys_frame)
        self.btn_test_google.setObjectName("secondaryButton")
        self.btn_test_google.setStyleSheet("padding: 4px 10px; font-size: 11px;")
        self.btn_test_google.clicked.connect(self._test_google_key)
        self.lbl_status_google = QLabel("Not Tested", self.keys_frame)
        self.lbl_status_google.setWordWrap(True)
        self.lbl_status_google.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.lbl_status_google.setStyleSheet("color: #64748b; font-size: 11px;")
        g_test_lay.addWidget(self.btn_test_google)
        g_test_lay.addWidget(self.lbl_status_google)
        kf_lay.addLayout(g_test_lay)
        
        scroll_lay.addWidget(self.keys_frame)
            
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        return tab

    def _build_mistral_card(self, name, info):
        card = QFrame()
        card.setObjectName("cardFrame")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(14, 14, 14, 14)
        card_lay.setSpacing(8)

        # Title Row
        title_row = QHBoxLayout()
        lbl_name = QLabel(info["name"], card)
        lbl_name.setFont(QFont("Segoe UI", 11, QFont.Bold))
        title_row.addWidget(lbl_name)
        
        lbl_badge = QLabel(info["badge"], card)
        lbl_badge.setObjectName("badgeLabel")
        title_row.addWidget(lbl_badge)
        
        title_row.addStretch()
        
        # State label
        lbl_state = QLabel("", card)
        lbl_state.setObjectName("subtitleLabel")
        card.lbl_state = lbl_state
        title_row.addWidget(lbl_state)
        card_lay.addLayout(title_row)

        # Specs row
        lbl_specs = QLabel(info["specs"], card)
        lbl_specs.setObjectName("subtitleLabel")
        lbl_specs.setWordWrap(True)
        card_lay.addWidget(lbl_specs)
        
        # Description
        lbl_desc = QLabel(info["description"], card)
        lbl_desc.setWordWrap(True)
        lbl_desc.setObjectName("subtitleLabel")
        card_lay.addWidget(lbl_desc)

        # Action Buttons
        btn_lay = QHBoxLayout()
        btn_lay.addStretch()
        
        btn_action = QPushButton("Use Model", card)
        btn_action.clicked.connect(lambda: self._use_mistral(name))
        card.btn_action = btn_action
        btn_lay.addWidget(btn_action)
        
        card_lay.addLayout(btn_lay)
        return card

    def _use_mistral(self, name):
        if self.app:
            self.cfg_working["backend"] = "mistral"
            self.cfg_working["mistral_stt_model"] = name
            
            # Repopulate UIs
            for m_name in list(self.whisper_cards.keys()):
                self._update_whisper_card_ui(m_name)
            for m_name in list(self.mistral_cards.keys()):
                self._update_mistral_card_ui(m_name)
            self._update_google_card_ui()

    def _privacy_on(self):
        return bool(getattr(self, "chk_privacy", None) and self.chk_privacy.isChecked())

    def _apply_disabled_cloud_card(self, card, state_text="Off in Privacy Mode"):
        """Clean, readable 'deactivated' look for a cloud STT card: pale card, muted
        state, and a clearly disabled (non-clickable) button - no dashed borders."""
        card.setObjectName("cardFrame")
        card.setEnabled(True)  # keep the text readable; only the button is disabled
        card.setStyleSheet("QFrame#cardFrame { background-color: #f8fafc; border: 1px solid #e9eef5; }")
        if hasattr(card, "lbl_state"):
            card.lbl_state.setText(state_text)
            card.lbl_state.setStyleSheet("color: #94a3b8; font-weight: 600;")
        if hasattr(card, "btn_action"):
            card.btn_action.setText("Use Model")
            card.btn_action.setEnabled(False)
            card.btn_action.setObjectName("")
            card.btn_action.setStyleSheet(
                "color:#94a3b8; background-color:#eef2f7; border:1px solid #e2e8f0; border-radius:6px;")
        card.style().unpolish(card); card.style().polish(card)
        if hasattr(card, "btn_action"):
            card.btn_action.style().unpolish(card.btn_action)
            card.btn_action.style().polish(card.btn_action)

    def _update_mistral_card_ui(self, name):
        card = self.mistral_cards.get(name)
        if not card:
            return
        if self._privacy_on():
            self._apply_disabled_cloud_card(card)
            return

        # Clear any privacy styling left over from a previous pass.
        card.setStyleSheet("")
        card.btn_action.setStyleSheet("")

        is_selected = (
            self.cfg_working.get("backend") == "mistral" and
            self.cfg_working.get("mistral_stt_model") == name
        )

        is_verified = getattr(self, "_mistral_key_verified", False)
        is_active = is_selected and is_verified

        if is_active:
            card.setObjectName("activeCardFrame")
            card.lbl_state.setText("Active")
            card.lbl_state.setStyleSheet("color: #22c55e; font-weight: bold;")
            card.btn_action.setText("Currently Active")
            card.btn_action.setEnabled(False)
            card.btn_action.setObjectName("")
        else:
            card.setObjectName("cardFrame")
            if is_selected:
                card.lbl_state.setText("Selected · needs key")
                card.lbl_state.setStyleSheet("color: #f97316; font-weight: bold;")
            else:
                card.lbl_state.setText("Cloud Model")
                card.lbl_state.setStyleSheet("color: #64748b;")
            card.btn_action.setText("Use Model")
            card.btn_action.setEnabled(True)
            card.btn_action.setObjectName("primaryButton")

        card.style().unpolish(card)
        card.style().polish(card)
        card.btn_action.style().unpolish(card.btn_action)
        card.btn_action.style().polish(card.btn_action)

    def _build_google_card(self, info):
        card = QFrame()
        card.setObjectName("cardFrame")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(14, 14, 14, 14)
        card_lay.setSpacing(8)

        # Title Row
        title_row = QHBoxLayout()
        lbl_name = QLabel(info["name"], card)
        lbl_name.setFont(QFont("Segoe UI", 11, QFont.Bold))
        title_row.addWidget(lbl_name)
        
        lbl_badge = QLabel(info["badge"], card)
        lbl_badge.setObjectName("badgeLabel")
        title_row.addWidget(lbl_badge)
        
        title_row.addStretch()
        
        # State label
        lbl_state = QLabel("", card)
        lbl_state.setObjectName("subtitleLabel")
        card.lbl_state = lbl_state
        title_row.addWidget(lbl_state)
        card_lay.addLayout(title_row)

        # Specs row
        lbl_specs = QLabel(info["specs"], card)
        lbl_specs.setObjectName("subtitleLabel")
        lbl_specs.setWordWrap(True)
        card_lay.addWidget(lbl_specs)
        
        # Description
        lbl_desc = QLabel(info["description"], card)
        lbl_desc.setWordWrap(True)
        lbl_desc.setObjectName("subtitleLabel")
        card_lay.addWidget(lbl_desc)

        # Action Buttons
        btn_lay = QHBoxLayout()
        btn_lay.addStretch()
        
        btn_action = QPushButton("Use Model", card)
        btn_action.clicked.connect(self._use_google)
        card.btn_action = btn_action
        btn_lay.addWidget(btn_action)
        
        card_lay.addLayout(btn_lay)
        return card

    def _use_google(self):
        if self.app:
            self.cfg_working["backend"] = "google"
            
            # Repopulate UIs
            for m_name in list(self.whisper_cards.keys()):
                self._update_whisper_card_ui(m_name)
            if hasattr(self, "mistral_cards"):
                for m_name in list(self.mistral_cards.keys()):
                    self._update_mistral_card_ui(m_name)
            self._update_google_card_ui()

    def _update_google_card_ui(self):
        card = getattr(self, "google_card", None)
        if not card:
            return
        if self._privacy_on():
            self._apply_disabled_cloud_card(card)
            return

        # Clear any privacy styling left over from a previous pass.
        card.setStyleSheet("")
        card.btn_action.setStyleSheet("")

        is_selected = (self.cfg_working.get("backend") == "google")
        is_verified = getattr(self, "_google_key_verified", False)
        is_active = is_selected and is_verified

        if is_active:
            card.setObjectName("activeCardFrame")
            card.lbl_state.setText("Active")
            card.lbl_state.setStyleSheet("color: #22c55e; font-weight: bold;")
            card.btn_action.setText("Currently Active")
            card.btn_action.setEnabled(False)
            card.btn_action.setObjectName("")
        else:
            card.setObjectName("cardFrame")
            if is_selected:
                card.lbl_state.setText("Selected · needs key")
                card.lbl_state.setStyleSheet("color: #f97316; font-weight: bold;")
            else:
                card.lbl_state.setText("Cloud Model")
                card.lbl_state.setStyleSheet("color: #64748b;")
            card.btn_action.setText("Use Model")
            card.btn_action.setEnabled(True)
            card.btn_action.setObjectName("primaryButton")

        card.style().unpolish(card)
        card.style().polish(card)
        if hasattr(card, "btn_action"):
            card.btn_action.style().unpolish(card.btn_action)
            card.btn_action.style().polish(card.btn_action)

    def _test_mistral_key(self):
        key = self.mistral_key_input.text().strip()
        if not key:
            self.lbl_status_mistral.setText("✗ Error: Key is empty")
            self.lbl_status_mistral.setStyleSheet("color: #ef4444; font-size: 11px; font-weight: bold;")
            return
            
        self.lbl_status_mistral.setText("Testing...")
        self.lbl_status_mistral.setStyleSheet("color: #3b82f6; font-size: 11px; font-weight: bold;")
        self.btn_test_mistral.setEnabled(False)
        
        def worker():
            import requests
            try:
                headers = {"Authorization": f"Bearer {key}"}
                resp = requests.get("https://api.mistral.ai/v1/models", headers=headers, timeout=10)
                if resp.status_code == 200:
                    self.mistral_test_finished.emit(True, "Working!")
                else:
                    err_msg = f"HTTP {resp.status_code}"
                    try:
                        err_json = resp.json()
                        if "message" in err_json:
                            err_msg = err_json["message"]
                        elif "detail" in err_json:
                            err_msg = str(err_json["detail"])
                    except:
                        if resp.text:
                            err_msg = resp.text[:50]
                    self.mistral_test_finished.emit(False, f"Invalid: {err_msg}")
            except Exception as e:
                self.mistral_test_finished.emit(False, f"Connection error: {str(e)[:50]}")
                
        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _test_google_key(self):
        key = self.google_key_input.text().strip()
        if not key:
            self.lbl_status_google.setText("✗ Error: Key is empty")
            self.lbl_status_google.setStyleSheet("color: #ef4444; font-size: 11px; font-weight: bold;")
            return
            
        self.lbl_status_google.setText("Testing...")
        self.lbl_status_google.setStyleSheet("color: #3b82f6; font-size: 11px; font-weight: bold;")
        self.btn_test_google.setEnabled(False)
        
        def worker():
            import requests
            try:
                # Google AI Studio (Gemini) keys are validated against the models
                # endpoint, which accepts a plain API key (unlike Cloud Speech).
                url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
                resp = requests.get(url, timeout=10)
                if resp.status_code == 200:
                    self.google_test_finished.emit(True, "Working!")
                else:
                    err_msg = ""
                    try:
                        err_msg = resp.json().get("error", {}).get("message", "")
                    except Exception:
                        err_msg = (resp.text or "")[:80]
                    low = err_msg.lower()
                    if "api key not valid" in low or "api_key_invalid" in low or resp.status_code == 400:
                        self.google_test_finished.emit(False, "Invalid API key")
                    else:
                        self.google_test_finished.emit(False, f"HTTP {resp.status_code}: {err_msg[:50]}")
            except Exception as e:
                self.google_test_finished.emit(False, f"Connection error: {str(e)[:50]}")

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _on_mistral_test_finished(self, success, message):
        self.btn_test_mistral.setEnabled(True)
        if success:
            self._mistral_key_verified = True
            self.lbl_status_mistral.setText("✓ Working")
            self.lbl_status_mistral.setStyleSheet("color: #22c55e; font-size: 11px; font-weight: bold;")
        else:
            self._mistral_key_verified = False
            self.lbl_status_mistral.setText(f"✗ {message}")
            self.lbl_status_mistral.setStyleSheet("color: #ef4444; font-size: 11px; font-weight: bold;")
            
        if hasattr(self, "mistral_cards"):
            for m_name in list(self.mistral_cards.keys()):
                self._update_mistral_card_ui(m_name)

    def _on_google_test_finished(self, success, message):
        self.btn_test_google.setEnabled(True)
        if success:
            self._google_key_verified = True
            self.lbl_status_google.setText("✓ Working")
            self.lbl_status_google.setStyleSheet("color: #22c55e; font-size: 11px; font-weight: bold;")
        else:
            self._google_key_verified = False
            self.lbl_status_google.setText(f"✗ {message}")
            self.lbl_status_google.setStyleSheet("color: #ef4444; font-size: 11px; font-weight: bold;")
            
        self._update_google_card_ui()

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
        specs = f"Needs ~{info.get('min_ram')} GB RAM  ·  {self._speed_phrase(info.get('speed_rank', 3))}"
        if info.get("armenian"):
            specs += f"  ·  {info.get('armenian')}"
        lbl_specs = QLabel(specs, card)
        lbl_specs.setObjectName("subtitleLabel")
        lbl_specs.setWordWrap(True)
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
        if self.app and self.cfg_working.get("backend") == "local" and self.cfg_working.get("whisper_model") == name:
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
            "Detect intent from voice - e.g. say \"translate to russian: …\", "
            "\"write email to John\", or \"make a todo list\". The AI produces "
            "only the result.",
            checked=False,
        )
        self.rb_smart = self.card_smart.radio
        self._smart_pro_badge = getattr(self.card_smart, "pro_badge", None)
        if self._smart_pro_badge is not None:
            self._smart_pro_badge.setVisible(not self._is_pro())
        self.rb_smart.setToolTip(
            "When enabled, your dictation is run through a language model "
            "that detects whether you want a translation, email, todo "
            "list, summary, or rewrite - and produces only that output. "
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
        self.combo_engine.addItem("Local Offline LLM Engine (Qwen / Gemma)", "local_llm")
        self.combo_engine.addItem("Rule-based Formatter (Fast, Local, Offline)", actions.RULE_BASED_ID)
        self.combo_engine.addItem("Google Gemini API (Cloud Engine)", actions.API_GEMINI_ID)
        self.combo_engine.addItem("OpenAI-compatible API (Cloud Engine)", actions.API_OPENAI_ID)
        self.combo_engine.addItem("Anthropic Claude API (Cloud Engine)", actions.API_ANTHROPIC_ID)
        
        if self.app:
            provider = self.cfg_working.get("action_model", "local_llm")
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

        # GGUF model cards - scroll with the whole tab (no nested mini-scroll)
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
            self.engine_section.setEnabled(True)  # always selectable, even in transcribe-only
        return tab

    def _is_pro(self):
        return bool(self.app and hasattr(self.app, "is_pro") and self.app.is_pro())

    def _can_use_smart(self):
        try:
            auth = getattr(self.app, "auth", None) if self.app else None
            cfg = self.app.cfg if self.app else None
            return entitlements.can_use_smart_action(auth, cfg)
        except Exception:
            return True

    def _on_output_mode_changed(self):
        is_smart = self.rb_smart.isChecked()

        # Smart Actions: Pro = unlimited; non-Pro can select it while they still
        # have free tries left. Only bounce + upsell once the 5 tries are gone.
        if is_smart and self.app and not self._is_pro() and not self._can_use_smart():
            self.rb_transcribe.blockSignals(True)
            self.rb_smart.blockSignals(True)
            self.rb_transcribe.setChecked(True)
            self.rb_smart.setChecked(False)
            self.rb_transcribe.blockSignals(False)
            self.rb_smart.blockSignals(False)
            is_smart = False
            if hasattr(self.app, "_pro_upsell"):
                self.app._pro_upsell("Smart Actions")

        # Engine controls stay visible at all times; they're just disabled
        # (grayed out) when Transcribe-only is selected. No hide/expand so the
        # layout never shifts.
        if hasattr(self, "engine_section"):
            self.engine_section.setEnabled(True)
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
        is selected - that previously caused the giant-empty-card layout.
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

        # Hidden PRO pill - shown on Pro-only modes for non-Pro users.
        pro_badge = QLabel("PRO", row_host)
        pro_badge.setObjectName("proBadge")
        pro_badge.setVisible(False)
        row.addWidget(pro_badge, 0, Qt.AlignTop)

        outer.addWidget(row_host, 0, Qt.AlignTop)
        outer.addStretch(1)
        card.radio = radio
        card.pro_badge = pro_badge
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
            
            gemini_models = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-1.5-flash"]
            anthropic_models = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-3-5-sonnet-latest", "claude-3-5-haiku-latest", "claude-3-5-opus-latest", "claude-3-haiku-20240307", "claude-3-opus-20240229"]
            openai_models = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"]
            
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
                if saved_model in set(anthropic_models + openai_models):
                    saved_model = "gemini-2.5-flash"
                
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
                    ("claude-opus-4-8 (~$5.00 / 1M tokens)", "claude-opus-4-8"),
                    ("claude-sonnet-4-6 (~$3.00 / 1M tokens)", "claude-sonnet-4-6"),
                    ("claude-haiku-4-5 (~$1.00 / 1M tokens)", "claude-haiku-4-5-20251001"),
                    ("claude-3-5-sonnet-latest (~$3.00 / 1M tokens)", "claude-3-5-sonnet-latest"),
                    ("claude-3-5-haiku-latest (~$0.80 / 1M tokens)", "claude-3-5-haiku-latest"),
                    ("claude-3-5-opus-latest (~$15.00 / 1M tokens)", "claude-3-5-opus-latest"),
                    ("claude-3-haiku-20240307 (~$0.25 / 1M tokens)", "claude-3-haiku-20240307"),
                    ("claude-3-opus-20240229 (~$15.00 / 1M tokens)", "claude-3-opus-20240229"),
                ]
                for label, model_id in models_info:
                    self.cloud_api_model.addItem(label, model_id)
                self.cloud_api_model.addItem("Custom Model...")
                self.cloud_api_key.setText(self.cfg_working.get("action_api_key", ""))
                
                saved_model = self.cfg_working.get("action_api_model", "") or "claude-sonnet-4-6"
                if saved_model in set(gemini_models + openai_models):
                    saved_model = "claude-sonnet-4-6"
                
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
                    ("gpt-5.5 (~$5.00 / 1M tokens)", "gpt-5.5"),
                    ("gpt-5.4 (~$2.50 / 1M tokens)", "gpt-5.4"),
                    ("gpt-5.4-mini (~$0.75 / 1M tokens)", "gpt-5.4-mini"),
                    ("gpt-4o (~$2.50 / 1M tokens)", "gpt-4o"),
                    ("gpt-4o-mini (~$0.15 / 1M tokens)", "gpt-4o-mini"),
                    ("gpt-3.5-turbo (~$0.50 / 1M tokens)", "gpt-3.5-turbo"),
                ]
                for label, model_id in models_info:
                    self.cloud_api_model.addItem(label, model_id)
                self.cloud_api_model.addItem("Custom Model...")
                self.cloud_api_key.setText(self.cfg_working.get("action_api_key", ""))
                self.cloud_api_url.setText(self.cfg_working.get("action_api_base_url", ""))
                
                saved_model = self.cfg_working.get("action_api_model", "") or "gpt-5.4-mini"
                if saved_model in set(gemini_models + anthropic_models):
                    saved_model = "gpt-5.4-mini"
                
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

    def _on_privacy_toggled(self, _state=None):
        # Privacy Mode is global and immediate (mirrors the tray toggle): apply it
        # to the live app config and rebuild the tray now, not only on Save, so the
        # in-app checkbox and the tray switch always agree.
        self._save_general_configs()
        if self.app and hasattr(self.app, "set_privacy_mode"):
            self.app.set_privacy_mode(self.chk_privacy.isChecked(), notify=False)

    def _save_general_configs(self):
        if not self.app:
            return
        self.cfg_working["language"] = self.combo_lang.currentData()
        self.cfg_working["initial_prompt"] = self.vocab_input.toPlainText().strip()
        self.cfg_working["privacy_mode"] = self.chk_privacy.isChecked()
        # Privacy Mode and Cloud transcription are mutually exclusive.
        if self.chk_privacy.isChecked() and hasattr(self, "chk_managed") and self.chk_managed.isChecked():
            self.chk_managed.blockSignals(True)
            self.chk_managed.setChecked(False)
            self.chk_managed.blockSignals(False)
            if self.cfg_working.get("backend") == "managed":
                self.cfg_working["backend"] = "local"
        if hasattr(self, "combo_device"):
            self.cfg_working["meeting_audio_mode"] = self.combo_device.currentData()
        if hasattr(self, "google_key_input"):
            val = self.google_key_input.text().strip()
            prev_val = self.cfg_working.get("google_api_key", "").strip()
            self.cfg_working["google_api_key"] = val
            self.google_key_input.setStyleSheet("")
            if val != prev_val:
                self._google_key_verified = True if val else False
                if hasattr(self, "lbl_status_google"):
                    self.lbl_status_google.setText("Not Tested" if val else "Not Configured")
                    self.lbl_status_google.setStyleSheet("color: #64748b; font-size: 11px;")
                self._update_google_card_ui()
        if hasattr(self, "mistral_key_input"):
            val = self.mistral_key_input.text().strip()
            prev_val = self.cfg_working.get("mistral_api_key", "").strip()
            self.cfg_working["mistral_api_key"] = val
            self.mistral_key_input.setStyleSheet("")
            if val != prev_val:
                self._mistral_key_verified = True if val else False
                if hasattr(self, "lbl_status_mistral"):
                    self.lbl_status_mistral.setText("Not Tested" if val else "Not Configured")
                    self.lbl_status_mistral.setStyleSheet("color: #64748b; font-size: 11px;")
                if hasattr(self, "mistral_cards"):
                    for m_name in list(self.mistral_cards.keys()):
                        self._update_mistral_card_ui(m_name)
        if self.chk_privacy.isChecked():
            self.cfg_working["save_history"] = False
            if hasattr(self, "chk_history"):
                self.chk_history.blockSignals(True)
                self.chk_history.setChecked(False)
                self.chk_history.blockSignals(False)
            
            # Force backend to local offline Whisper if privacy is on
            if self.cfg_working.get("backend") in ("mistral", "google"):
                self.cfg_working["backend"] = "local"
                if not self.cfg_working.get("whisper_model"):
                    self.cfg_working["whisper_model"] = "small"
                for m_name in list(self.whisper_cards.keys()):
                    self._update_whisper_card_ui(m_name)
                if hasattr(self, "mistral_cards"):
                    for m_name in list(self.mistral_cards.keys()):
                        self._update_mistral_card_ui(m_name)
                self._update_google_card_ui()

            # Force action model to local if privacy is on and a cloud LLM was active
            current_action_model = self.cfg_working.get("action_model", "rule_based")
            if actions.ACTION_MODELS.get(actions.normalize_action_model(current_action_model), {}).get("kind") == "cloud":
                self.cfg_working["action_model"] = actions.RULE_BASED_ID
                if hasattr(self, "combo_engine"):
                    self.combo_engine.blockSignals(True)
                    idx = self.combo_engine.findData(actions.RULE_BASED_ID)
                    if idx >= 0:
                        self.combo_engine.setCurrentIndex(idx)
                    self.combo_engine.blockSignals(False)
                    self._on_engine_changed(idx)
        self._update_privacy_ui_state()

    def _update_privacy_ui_state(self):
        is_private = self.chk_privacy.isChecked() if hasattr(self, "chk_privacy") else False
        PALE = "QFrame { background-color: #f8fafc; border: 1px solid #e9eef5; border-radius: 10px; }"

        # Fast cloud transcription sends audio off-device, so Privacy Mode turns it
        # off and locks it. The PRO badge stays visible but pales (not hidden).
        if hasattr(self, "chk_managed"):
            self.chk_managed.setEnabled(not is_private)
            self.chk_managed.setToolTip(
                "Disabled by Privacy Mode (everything stays on your device)" if is_private else ""
            )
            if is_private and self.chk_managed.isChecked():
                self.chk_managed.blockSignals(True)
                self.chk_managed.setChecked(False)
                self.chk_managed.blockSignals(False)
                if self.cfg_working.get("backend") == "managed":
                    self.cfg_working["backend"] = "local"
        if hasattr(self, "_cloud_pro_badge"):
            self._cloud_pro_badge.setStyleSheet(
                "background-color:#e2e8f0; color:#94a3b8; border-radius:6px; padding:1px 6px; font-weight:700;"
                if is_private else ""
            )

        # Cloud STT cards render their own privacy-aware disabled look (pale card +
        # disabled button), so just refresh them - no dashed overlay.
        if hasattr(self, "mistral_cards"):
            for name in list(self.mistral_cards.keys()):
                self._update_mistral_card_ui(name)
        if getattr(self, "google_card", None):
            self._update_google_card_ui()

        # Cloud API credentials + cloud action config frames: clean pale lock.
        for attr in ("keys_frame", "card_cloud"):
            frame = getattr(self, attr, None)
            if frame:
                frame.setEnabled(not is_private)
                frame.setStyleSheet(PALE if is_private else "")

        # Cloud engines in the AI Actions dropdown (indexes 2,3,4).
        if getattr(self, "combo_engine", None):
            model = self.combo_engine.model()
            for i in (2, 3, 4):
                item = model.item(i)
                if item:
                    item.setEnabled(not is_private)
                    item.setForeground(QColor("#cbd5e1") if is_private else QColor("#1e293b"))
            if is_private and self.combo_engine.currentIndex() in (2, 3, 4):
                self.combo_engine.setCurrentIndex(1)  # back to Rule-based Formatter

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
        layout.setSpacing(12)

        # Actions row (clear / export).
        hist_frame = QFrame(tab)
        hist_frame.setObjectName("cardFrame")
        h_lay = QVBoxLayout(hist_frame)
        h_lay.addWidget(QLabel("Transcription History", hist_frame))

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

        # Search box.
        self._history_search = QLineEdit(tab)
        self._history_search.setPlaceholderText("Search your transcripts...")
        self._history_search.setClearButtonEnabled(True)
        self._history_search.setMinimumHeight(34)
        self._history_search.textChanged.connect(lambda _t: self._populate_history_list())
        layout.addWidget(self._history_search)

        # Scrollable list of past transcripts and AI outputs (newest first).
        self._history_scroll = QScrollArea(tab)
        self._history_scroll.setWidgetResizable(True)
        self._history_scroll.setFrameShape(QFrame.NoFrame)
        self._history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._history_container = QWidget()
        self._history_vlay = QVBoxLayout(self._history_container)
        self._history_vlay.setContentsMargins(0, 0, 0, 0)
        self._history_vlay.setSpacing(8)
        self._history_scroll.setWidget(self._history_container)
        layout.addWidget(self._history_scroll, 1)
        self._populate_history_list()

        # History toggle checkbox.
        self.chk_history = QCheckBox("Save local transcription history", tab)
        if self.app:
            self.chk_history.setChecked(bool(self.cfg_working.get("save_history", True)))
        self.chk_history.stateChanged.connect(self._save_history_config)
        layout.addWidget(self.chk_history)

        # Telemetry Consent checkbox.
        self.chk_telemetry = QCheckBox("Share anonymous usage metrics to improve Armenian AI models", tab)
        if self.app:
            self.chk_telemetry.setChecked(bool(self.app.cfg.get("analytics_enabled", True)))
        self.chk_telemetry.stateChanged.connect(self._save_telemetry_config)
        layout.addWidget(self.chk_telemetry)

        return tab

    def _populate_history_list(self):
        """(Re)build the scrollable transcript list from saved history."""
        if not hasattr(self, "_history_vlay"):
            return
        while self._history_vlay.count():
            item = self._history_vlay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        query = self._history_search.text().strip() if hasattr(self, "_history_search") else ""
        try:
            entries = hist.search(query) if query else hist.load()
        except Exception:
            entries = []

        if not entries:
            if query:
                msg = "No transcripts match your search."
            elif not self.cfg_working.get("save_history", True):
                msg = ("History saving is off. Turn on “Save local transcription "
                       "history” below to keep a searchable log here.")
            else:
                msg = "No transcripts yet. Your dictations and AI outputs will appear here."
            empty = QLabel(msg)
            empty.setObjectName("subtitleLabel")
            empty.setAlignment(Qt.AlignCenter)
            empty.setWordWrap(True)
            empty.setContentsMargins(0, 28, 0, 28)
            self._history_vlay.addWidget(empty)
            self._history_vlay.addStretch()
            return

        for e in entries[:100]:
            card = QFrame()
            card.setObjectName("cardFrame")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 10, 12, 10)
            cl.setSpacing(3)
            meta_bits = [b for b in (e.get("timestamp", ""), e.get("language", ""),
                                     e.get("backend", "")) if b]
            meta = QLabel("   ·   ".join(meta_bits), card)
            meta.setStyleSheet("color: #94a3b8; font-size: 11px;")
            cl.addWidget(meta)
            body = QLabel(e.get("text", ""), card)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            cl.addWidget(body)
            self._history_vlay.addWidget(card)
        self._history_vlay.addStretch()

    def _clear_all_history(self):
        reply = QMessageBox.question(
            self, "Clear History",
            "Are you sure you want to permanently delete all transcription history?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            hist.clear()
            self._populate_history_list()
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
        self._populate_history_list()

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
        
        scroll = QScrollArea(tab)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        scroll_content = QWidget()
        scroll_lay = QVBoxLayout(scroll_content)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setSpacing(12)

        logo_frame = QFrame(scroll_content)
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
        scroll_lay.addWidget(logo_frame)

        desc_label = QLabel(
            "An always-on private dictation tool optimized for Armenian, English, and Russian speakers.\n"
            "Runs offline using Whisper AI or integrates with Google Cloud Speech API.",
            scroll_content
        )
        desc_label.setWordWrap(True)
        desc_label.setObjectName("subtitleLabel")
        desc_label.setAlignment(Qt.AlignCenter)
        scroll_lay.addWidget(desc_label)

        # AIBUBEN Community & Founder Card
        aibuben_frame = QFrame(scroll_content)
        aibuben_frame.setObjectName("cardFrame")
        aibuben_frame.setMinimumHeight(180)
        aibuben_lay = QVBoxLayout(aibuben_frame)
        aibuben_lay.setContentsMargins(18, 18, 18, 18)
        aibuben_lay.setSpacing(10)

        import os
        from PySide6.QtGui import QPixmap

        lbl_aibuben_logo = QLabel(aibuben_frame)
        # Resolve the asset in a PyInstaller-safe way: bundled builds put assets
        # under sys._MEIPASS, while running from source uses the repo path. Using
        # the same resource_path() helper as main.py means the logo renders in the
        # packaged app instead of falling back to text.
        try:
            from main import resource_path
            logo_path = resource_path("assets", "aibuben_logo.png")
        except Exception:
            logo_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "assets", "aibuben_logo.png",
            )
        pixmap = QPixmap(logo_path)
        if not pixmap.isNull():
            # Bound to a tidy box, preserving the wordmark's aspect ratio.
            lbl_aibuben_logo.setPixmap(
                pixmap.scaled(280, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        else:
            lbl_aibuben_logo.setText("Powered by AIBUBEN")
            lbl_aibuben_logo.setStyleSheet("font-size: 14px; font-weight: bold; color: #3b82f6;")
        lbl_aibuben_logo.setAlignment(Qt.AlignCenter)
        aibuben_lay.addWidget(lbl_aibuben_logo)

        lbl_aibuben_desc = QLabel(
            "This project is proud to be part of the <b>AIBUBEN</b> AI community in Yerevan-"
            "empowering AI builders, creators, and students to learn, connect, and build state-of-the-art products.",
            aibuben_frame
        )
        lbl_aibuben_desc.setWordWrap(True)
        lbl_aibuben_desc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lbl_aibuben_desc.setStyleSheet("font-size: 12px; color: #475569;")
        lbl_aibuben_desc.setAlignment(Qt.AlignCenter)
        aibuben_lay.addWidget(lbl_aibuben_desc)

        # Clickable Social Links Row
        links_layout = QHBoxLayout()
        links_layout.setAlignment(Qt.AlignCenter)
        links_layout.setSpacing(24)

        lbl_web_link = QLabel(aibuben_frame)
        lbl_web_link.setText("<a href='https://aibuben.xyz' style='color: #3b82f6; text-decoration: none; font-weight: bold;'>Visit aibuben.xyz</a>")
        lbl_web_link.setOpenExternalLinks(True)
        lbl_web_link.setStyleSheet("font-size: 12px;")
        links_layout.addWidget(lbl_web_link)

        lbl_linkedin_link = QLabel(aibuben_frame)
        lbl_linkedin_link.setText("<a href='https://www.linkedin.com/in/aram-adamyan-2k/' style='color: #0077b5; text-decoration: none; font-weight: bold;'>Aram Adamyan on LinkedIn</a>")
        lbl_linkedin_link.setOpenExternalLinks(True)
        lbl_linkedin_link.setStyleSheet("font-size: 12px;")
        links_layout.addWidget(lbl_linkedin_link)

        aibuben_lay.addLayout(links_layout)
        scroll_lay.addWidget(aibuben_frame)

        # Open Source & Contribution Card
        contrib_frame = QFrame(scroll_content)
        contrib_frame.setObjectName("cardFrame")
        contrib_lay = QVBoxLayout(contrib_frame)
        contrib_lay.setContentsMargins(18, 18, 18, 18)
        contrib_lay.setSpacing(10)

        lbl_contrib_title = QLabel("Open Source & Contributions", contrib_frame)
        lbl_contrib_title.setStyleSheet("font-size: 14px; font-weight: bold; color: #22c55e;")
        lbl_contrib_title.setAlignment(Qt.AlignCenter)
        contrib_lay.addWidget(lbl_contrib_title)

        lbl_contrib_desc = QLabel(
            "Transcribe App is <b>100% Open Source</b>! We are passionate about community-driven "
            "development and welcome developers, designers, and testers to collaborate. "
            "Help us build the next generation of private voice-to-text tools!",
            contrib_frame
        )
        lbl_contrib_desc.setWordWrap(True)
        lbl_contrib_desc.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lbl_contrib_desc.setStyleSheet("font-size: 12px; color: #475569;")
        lbl_contrib_desc.setAlignment(Qt.AlignCenter)
        contrib_lay.addWidget(lbl_contrib_desc)

        lbl_github_link = QLabel(contrib_frame)
        lbl_github_link.setText(
            "<a href='https://github.com/Aram2K/transcribe-app' style='color: #22c55e; text-decoration: none; font-weight: bold;'>Contribute on GitHub / Aram2K/transcribe-app</a>"
        )
        lbl_github_link.setOpenExternalLinks(True)
        lbl_github_link.setStyleSheet("font-size: 12px;")
        lbl_github_link.setAlignment(Qt.AlignCenter)
        contrib_lay.addWidget(lbl_github_link)

        scroll_lay.addWidget(contrib_frame)

        # Update checking button
        self.btn_update = QPushButton("Check for Updates", scroll_content)
        if self.app and self.cfg_working.get("pending_update_version"):
            tag = self.cfg_working.get("pending_update_version")
            self.btn_update.setText(f"Install Update {tag}")
            self.btn_update.setObjectName("primaryButton")
        self.btn_update.clicked.connect(self._check_for_updates)
        scroll_lay.addWidget(self.btn_update)

        scroll_lay.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        return tab

    # ── TAB 6: Account & Billing ─────────────────────────────────────────────
    def _create_account_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Frosted-glass account card
        card = QFrame(tab)
        card.setObjectName("glassCard")
        card.setMinimumHeight(150)
        c = QVBoxLayout(card)
        c.setContentsMargins(20, 20, 20, 20)
        c.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("Your Account", card)
        title.setObjectName("titleLabel")
        head.addWidget(title)
        head.addStretch()
        self._acct_badge = QLabel("FREE", card)
        self._acct_badge.setObjectName("freeBadge")
        head.addWidget(self._acct_badge)
        c.addLayout(head)

        self._acct_status_label = QLabel("Not signed in", card)
        self._acct_status_label.setObjectName("subtitleLabel")
        c.addWidget(self._acct_status_label)

        self._acct_plan_label = QLabel("", card)
        self._acct_plan_label.setObjectName("subtitleLabel")
        self._acct_plan_label.setWordWrap(True)
        c.addWidget(self._acct_plan_label)

        btns = QHBoxLayout()
        self._acct_signin_btn = QPushButton("Sign in / Create account", card)
        self._acct_signin_btn.setObjectName("primaryButton")
        self._acct_signin_btn.clicked.connect(self._acct_sign_in)
        btns.addWidget(self._acct_signin_btn)

        self._acct_upgrade_btn = QPushButton("Upgrade to Pro", card)
        self._acct_upgrade_btn.setObjectName("primaryButton")
        self._acct_upgrade_btn.clicked.connect(self._acct_upgrade)
        btns.addWidget(self._acct_upgrade_btn)

        self._acct_manage_btn = QPushButton("Manage subscription", card)
        self._acct_manage_btn.clicked.connect(self._acct_manage)
        btns.addWidget(self._acct_manage_btn)

        self._acct_signout_btn = QPushButton("Sign out", card)
        self._acct_signout_btn.clicked.connect(self._acct_sign_out)
        btns.addWidget(self._acct_signout_btn)

        btns.addStretch()
        c.addLayout(btns)

        # Super-admin only: force a tier locally (for testing / control).
        self._admin_row = QWidget(card)
        ar = QHBoxLayout(self._admin_row)
        ar.setContentsMargins(0, 6, 0, 0)
        ar.setSpacing(8)
        admin_lbl = QLabel("Admin · Force tier:", self._admin_row)
        admin_lbl.setObjectName("subtitleLabel")
        ar.addWidget(admin_lbl)
        self._admin_tier_combo = QComboBox(self._admin_row)
        for label, val in (("Auto", "auto"), ("Guest", "guest"), ("Free", "free"), ("Pro", "pro")):
            self._admin_tier_combo.addItem(label, val)
        self._admin_tier_combo.currentIndexChanged.connect(self._on_admin_tier_changed)
        ar.addWidget(self._admin_tier_combo)
        ar.addStretch()
        self._admin_row.setVisible(False)
        c.addWidget(self._admin_row)

        layout.addWidget(card)

        self._acct_perks = QLabel(
            "<div style='font-size:11px; line-height:150%;'>"
            "<b>Transcribe Pro</b> includes:<br>"
            "<span style='color:#a855f7;font-weight:800'>&#10003;</span>&nbsp; <b>Unlimited Smart Actions</b> - rewrite, translate, summarize and draft emails by voice<br>"
            "<span style='color:#a855f7;font-weight:800'>&#10003;</span>&nbsp; <b>Smart meeting recording</b> with AI-generated notes and summaries<br>"
            "<span style='color:#a855f7;font-weight:800'>&#10003;</span>&nbsp; <b>Fast cloud transcription</b> - no setup, no API key, no timeouts<br>"
            "<span style='color:#a855f7;font-weight:800'>&#10003;</span>&nbsp; <b>Priority access</b> to new models, plus direct support"
            "</div>",
            tab,
        )
        self._acct_perks.setWordWrap(True)
        self._acct_perks.setObjectName("subtitleLabel")
        layout.addWidget(self._acct_perks)
        layout.addStretch()

        self.refresh_pro_state()
        return tab

    def _acct_sign_in(self):
        if self.app and hasattr(self.app, "show_auth_gate"):
            self.app.show_auth_gate()

    def _acct_sign_out(self):
        if self.app and hasattr(self.app, "sign_out"):
            self.app.sign_out()

    def _acct_upgrade(self):
        if self.app and hasattr(self.app, "_pro_upsell"):
            self.app._pro_upsell()

    def _acct_manage(self):
        if self.app and hasattr(self.app, "open_billing"):
            self.app.open_billing()

    def _on_admin_tier_changed(self):
        if getattr(self, "_suppress_admin_combo", False):
            return
        if self.app and hasattr(self.app, "set_admin_tier_override"):
            self.app.set_admin_tier_override(self._admin_tier_combo.currentData())

    def _header_cta_clicked(self):
        if not self.app:
            return
        auth = getattr(self.app, "auth", None)
        if not (auth and auth.is_authenticated):
            if hasattr(self.app, "show_auth_gate"):
                self.app.show_auth_gate()
        elif hasattr(self.app, "_pro_upsell"):
            self.app._pro_upsell()

    @staticmethod
    def _trial_days_left(auth):
        try:
            import math
            from datetime import datetime, timezone
            end = getattr(auth, "period_end", None)
            if not end:
                return 0
            dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            secs = (dt - datetime.now(timezone.utc)).total_seconds()
            return max(0, math.ceil(secs / 86400.0))
        except Exception:
            return 0

    def refresh_pro_state(self):
        """Update the Account tab to reflect the current auth/entitlement state.
        Safe to call from main.py's auth-changed handler (and before the tab is
        built)."""
        if not hasattr(self, "_acct_badge"):
            return
        auth = getattr(self.app, "auth", None) if self.app else None
        authed = bool(auth and auth.is_authenticated)

        # Effective tier honors the super-admin override.
        if self.app and hasattr(self.app, "current_tier"):
            t = self.app.current_tier()
        else:
            t = entitlements.tier(auth)
        is_pro = (t == entitlements.TIER_PRO)

        # Tier badge: purple PRO / green FREE / gray GUEST.
        if is_pro:
            self._acct_badge.setText("PRO"); self._acct_badge.setObjectName("proBadge")
        elif t == entitlements.TIER_FREE:
            self._acct_badge.setText("FREE"); self._acct_badge.setObjectName("freeBadge")
        else:
            self._acct_badge.setText("GUEST"); self._acct_badge.setObjectName("guestBadge")
        self._acct_badge.style().unpolish(self._acct_badge)
        self._acct_badge.style().polish(self._acct_badge)

        # Header: "Welcome, {name}" + matching tier badge.
        if hasattr(self, "_welcome_label"):
            name = None
            if authed and auth:
                name = getattr(auth, "user_name", None) or (
                    auth.user_email.split("@")[0] if auth.user_email else None
                )
            self._welcome_label.setText(f"Welcome, {name}" if name else "Welcome")
            if is_pro:
                self._header_badge.setText("PRO"); self._header_badge.setObjectName("proBadge")
            elif t == entitlements.TIER_FREE:
                self._header_badge.setText("FREE"); self._header_badge.setObjectName("freeBadge")
            else:
                self._header_badge.setText("GUEST"); self._header_badge.setObjectName("guestBadge")
            self._header_badge.style().unpolish(self._header_badge)
            self._header_badge.style().polish(self._header_badge)

        # Smart-action lock pill follows Pro state.
        # Smart Actions: Pro = unlimited (no badge); non-Pro shows a "N/5 FREE"
        # counter, and once exhausted the option locks (PRO badge + disabled).
        if getattr(self, "_smart_pro_badge", None) is not None:
            if is_pro:
                self._smart_pro_badge.setVisible(False)
                if hasattr(self, "rb_smart"):
                    self.rb_smart.setEnabled(True)
            else:
                try:
                    rem = entitlements.smart_actions_remaining(None, self.app.cfg if self.app else None)
                except Exception:
                    rem = entitlements.FREE_SMART_ACTION_TRIES
                if rem > 0:
                    self._smart_pro_badge.setText(f"{rem}/5 FREE")
                    self._smart_pro_badge.setObjectName("freeBadge")
                    if hasattr(self, "rb_smart"):
                        self.rb_smart.setEnabled(True)
                else:
                    self._smart_pro_badge.setText("PRO")
                    self._smart_pro_badge.setObjectName("proBadge")
                    if hasattr(self, "rb_smart"):
                        self.rb_smart.setEnabled(False)
                        if self.rb_smart.isChecked():
                            self.rb_smart.blockSignals(True)
                            self.rb_transcribe.setChecked(True)
                            self.rb_smart.setChecked(False)
                            self.rb_smart.blockSignals(False)
                            self.cfg_working["output_action"] = actions.ACTION_TRANSCRIBE_ONLY
                self._smart_pro_badge.setVisible(True)
                self._smart_pro_badge.style().unpolish(self._smart_pro_badge)
                self._smart_pro_badge.style().polish(self._smart_pro_badge)

        # Meeting controls: locked but readable for non-Pro (clicking prompts upgrade).
        if hasattr(self, "btn_launch_meeting"):
            if is_pro:
                self.btn_launch_meeting.setText("Start Smart Meeting Transcription")
                self.btn_launch_meeting.setStyleSheet("")  # default primary style
            else:
                self.btn_launch_meeting.setText("Smart Meeting Transcription   (Pro)")
                self.btn_launch_meeting.setStyleSheet(
                    "QPushButton { background-color: #eef2f7; color: #475569;"
                    " border: 1px solid #cbd5e1; border-radius: 8px; font-weight: 600; }"
                    "QPushButton:hover { background-color: #e2e8f0; }"
                )
            self.btn_launch_meeting.setEnabled(True)
        if hasattr(self, "combo_device"):
            self.combo_device.setEnabled(is_pro)
        if hasattr(self, "_meeting_pro_badge"):
            self._meeting_pro_badge.setVisible(not is_pro)

        # Privacy Mode (can be toggled from the tray) deactivates cloud options
        # with a clear reason so users know why they're disabled.
        privacy = bool(self.app and self.app.cfg.get("privacy_mode"))
        if hasattr(self, "chk_privacy") and self.chk_privacy.isChecked() != privacy:
            self.chk_privacy.blockSignals(True)
            self.chk_privacy.setChecked(privacy)
            self.chk_privacy.blockSignals(False)
            self.cfg_working["privacy_mode"] = privacy
        if hasattr(self, "chk_managed"):
            self.chk_managed.setEnabled(not privacy)
            self.chk_managed.setToolTip(
                "Disabled by Privacy Mode (everything stays on your device)" if privacy else ""
            )
            if privacy and self.chk_managed.isChecked():
                self.chk_managed.blockSignals(True)
                self.chk_managed.setChecked(False)
                self.chk_managed.blockSignals(False)
                self.cfg_working["backend"] = "local"
        if hasattr(self, "_cloud_pro_badge"):
            self._cloud_pro_badge.setVisible(not privacy)

        plan = getattr(auth, "plan", None) if auth else None
        on_trial = is_pro and plan == "trial"

        if authed:
            self._acct_status_label.setText(f"Signed in as {auth.user_email or 'your account'}")
            if on_trial:
                days = self._trial_days_left(auth)
                self._acct_plan_label.setText(
                    f"Pro trial - {days} day(s) left. Subscribe to keep Pro after it ends."
                )
            elif is_pro:
                plan_name = (plan or "Pro").capitalize()
                renew = ""
                if getattr(auth, "period_end", None):
                    verb = "Cancels" if auth.cancel_at_period_end else "Renews"
                    renew = f"  ·  {verb} {str(auth.period_end)[:10]}"
                self._acct_plan_label.setText(f"Plan: {plan_name}{renew}")
            else:
                self._acct_plan_label.setText(
                    "Plan: Free - upgrade to unlock Meetings, Smart Actions, and fast cloud transcription."
                )
        else:
            self._acct_status_label.setText("Not signed in  ·  Guest")
            try:
                mins = entitlements.guest_minutes_remaining()
                self._acct_plan_label.setText(
                    f"Guest trial: ~{mins} min of free recording left. "
                    "Create a free account to keep dictating + get 3 days of Pro."
                )
            except Exception:
                self._acct_plan_label.setText("Sign in to manage your subscription and unlock Pro.")

        # Top-right CTA: neutral "Sign up" for guests, purple "Upgrade" for free/trial.
        if hasattr(self, "_header_cta"):
            neutral = (
                "QPushButton { background: #ffffff; border: 1px solid #cbd5e1; color: #334155;"
                " font-weight: 600; border-radius: 8px; padding: 6px 14px; }"
                "QPushButton:hover { border-color: #3b82f6; color: #1e293b; }"
            )
            purple = (
                "QPushButton { background-color: #a855f7; border: 1px solid #9333ea; color: white;"
                " font-weight: 700; border-radius: 8px; padding: 6px 14px; }"
                "QPushButton:hover { background-color: #9333ea; }"
            )
            if not authed:
                try:
                    mins = entitlements.guest_minutes_remaining()
                except Exception:
                    mins = 0
                self._header_cta.setText(f"Sign up   ·   {mins} min left")
                self._header_cta.setStyleSheet(neutral)
                self._header_cta.setVisible(True)
            elif on_trial:
                self._header_cta.setText(f"Upgrade   ·   Trial {self._trial_days_left(auth)}d left")
                self._header_cta.setStyleSheet(purple)
                self._header_cta.setVisible(True)
            elif is_pro:
                self._header_cta.setVisible(False)
            else:
                self._header_cta.setText("Upgrade to Pro")
                self._header_cta.setStyleSheet(purple)
                self._header_cta.setVisible(True)

        # Super-admin tier override row (visible only to admins).
        if hasattr(self, "_admin_row"):
            is_admin = entitlements.is_super_admin(auth)
            self._admin_row.setVisible(is_admin)
            if is_admin and self.app:
                ov = self.app.cfg.get("admin_tier_override", "auto")
                idx = self._admin_tier_combo.findData(ov)
                if idx >= 0:
                    self._suppress_admin_combo = True
                    self._admin_tier_combo.setCurrentIndex(idx)
                    self._suppress_admin_combo = False

        self._acct_signin_btn.setVisible(not authed)
        self._acct_upgrade_btn.setVisible(authed and not is_pro)
        self._acct_manage_btn.setVisible(authed and is_pro)
        self._acct_signout_btn.setVisible(authed)
        # Pro users already have everything, so don't sell them the perks list.
        if hasattr(self, "_acct_perks"):
            self._acct_perks.setVisible(not is_pro)

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
                self.cfg_working["backend"] = "local"
                self.cfg_working["whisper_model"] = name
            for m_name in list(self.whisper_cards.keys()):
                self._update_whisper_card_ui(m_name)
            if hasattr(self, "mistral_cards"):
                for m_name in list(self.mistral_cards.keys()):
                    self._update_mistral_card_ui(m_name)
            self._update_google_card_ui()
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
    def eventFilter(self, obj, event):
        if self.capturing and event.type() == QEvent.MouseButtonPress:
            btn = event.button()
            btn_map = {
                Qt.MiddleButton: "mouse:middle",
                Qt.BackButton: "mouse:x1",
                Qt.ForwardButton: "mouse:x2"
            }
            if btn in btn_map:
                hotkey_str = btn_map[btn]
                if self.app:
                    self.cfg_working["hotkey"] = hotkey_str
                self.capturing = False
                self.btn_hotkey.setText(self._fmt_hotkey(None, hotkey_str).upper())
                self.btn_hotkey.setStyleSheet("font-weight: bold; min-height: 36px; border-color: #3b82f6;")
                return True # swallow event
        return super().eventFilter(obj, event)

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
        if event.key() == Qt.Key_Escape:
            if not self.capturing:
                event.ignore()
                return

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

        # Modifier-less binding is only safe for Function keys (F1-F12); a bare
        # typing/navigation key would fire during normal use. Require a modifier
        # otherwise (mouse buttons are handled separately in eventFilter).
        is_function_key = len(key_str) >= 2 and key_str[0] == "f" and key_str[1:].isdigit()
        if not mods and not is_function_key:
            QMessageBox.warning(
                self, "Modifier Required",
                "Use at least one modifier (Ctrl, Alt, Shift, or Win), a Function "
                "key (F1-F12), or a mouse button.\n\nA single typing key can't be a "
                "hotkey because it would trigger while you type."
            )
            return

        hotkey_str = "+".join(mods + [key_str]) if mods else key_str

        if self.app:
            self.cfg_working["hotkey"] = hotkey_str
            
        self.capturing = False
        self.btn_hotkey.setText(self._fmt_hotkey(None, hotkey_str).upper())
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
