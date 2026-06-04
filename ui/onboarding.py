# Modern Onboarding Walkthrough Wizard in PySide6

import threading
from PySide6.QtCore import Qt, QEvent, Signal, QSize
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QCheckBox, QStackedWidget, QWidget, QLineEdit, QMessageBox, QFrame
)
from PySide6.QtGui import QFont, QColor, QIcon
import local_llm

class Onboarding(QDialog):
    # Emitted from the email-auth worker thread → handled on the GUI thread.
    auth_result = Signal(str, str)  # (status, message)

    def __init__(self, main_app=None, account_only=False):
        super().__init__()
        self.app = main_app
        # account_only: a one-time gate shown to EXISTING users on the update that
        # introduces accounts. Shows just the sign-in / guest choice (no setup
        # steps, since they're already configured).
        self.account_only = account_only

        self.setWindowTitle("Sign in to Transcribe" if account_only else "Welcome to Transcribe")
        self.setFixedSize(520, 680)
        
        # Apply style sheet
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)
            
        self.current_page = 0
        self.capturing = False
        self.guest_mode = True  # default to guest until they choose to sign in
        self._auth_mode = "signin"
        self._pending_verify_email = None
        self.auth_result.connect(self._on_auth_result)
        
        # Onboarding State Config
        self.hotkey_val = self.app.cfg.get("hotkey", "alt+r") if self.app else "alt+r"
        self.lang_val = self.app.cfg.get("language", "auto") if self.app else "auto"
        self.backend_val = self.app.cfg.get("backend", "local") if self.app else "local"
        self.model_val = self.app.cfg.get("whisper_model", "base") if self.app else "base"
        self.google_key_val = self.app.cfg.get("google_api_key", "") if self.app else ""
        self.mistral_key_val = self.app.cfg.get("mistral_api_key", "") if self.app else ""
        self.analytics_val = bool(self.app.cfg.get("analytics_enabled", True)) if self.app else True
        
        self._build_ui()
        self.btn_hotkey.installEventFilter(self)
        self.installEventFilter(self)

    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(24, 24, 24, 24)
        self.main_layout.setSpacing(16)

        # ── Title & Progress Indicator ──
        self.step_label = QLabel("STEP 1 OF 3", self)
        self.step_label.setObjectName("subtitleLabel")
        self.step_label.setStyleSheet("color: #3b82f6; font-weight: bold; font-size: 11px;")
        self.main_layout.addWidget(self.step_label)

        # ── Stacked pages ──
        self.stack = QStackedWidget(self)
        self._create_pages()
        self.main_layout.addWidget(self.stack)

        # ── Bottom Action Bar ──
        action_layout = QHBoxLayout()
        
        self.btn_back = QPushButton("Back", self)
        self.btn_back.clicked.connect(self._back)
        self.btn_back.setEnabled(False) # Step 1 starts disabled
        
        self.btn_next = QPushButton("Next", self)
        self.btn_next.setObjectName("primaryButton")
        self.btn_next.clicked.connect(self._next)
        
        action_layout.addWidget(self.btn_back)
        action_layout.addStretch()
        action_layout.addWidget(self.btn_next)

        self.main_layout.addLayout(action_layout)
        self._update_nav()

    def _create_account_page(self):
        """First-run gate: a polished sign in / sign up screen (email + password
        and Google), plus a low-friction guest path. Guest keeps activation high
        while the account capture drives the funnel."""
        self.page_account = QWidget()
        lay = QVBoxLayout(self.page_account)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(10)

        title = QLabel("Welcome to Transcribe", self.page_account)
        title.setObjectName("titleLabel")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        sub = QLabel("Private, instant voice-to-text anywhere you type.", self.page_account)
        sub.setObjectName("subtitleLabel")
        sub.setAlignment(Qt.AlignCenter)
        sub.setWordWrap(True)
        lay.addWidget(sub)

        card = QFrame(self.page_account)
        card.setObjectName("glassCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(22, 20, 22, 20)
        cl.setSpacing(11)

        # Segmented toggle - Sign in | Sign up
        seg = QHBoxLayout()
        seg.setSpacing(6)
        self._tab_signin = QPushButton("Sign in", card)
        self._tab_signin.setMinimumHeight(34)
        self._tab_signin.clicked.connect(lambda: self._set_auth_mode("signin"))
        self._tab_signup = QPushButton("Sign up", card)
        self._tab_signup.setMinimumHeight(34)
        self._tab_signup.clicked.connect(lambda: self._set_auth_mode("signup"))
        seg.addWidget(self._tab_signin)
        seg.addWidget(self._tab_signup)
        cl.addLayout(seg)

        # Name (sign up only)
        self._name_input = QLineEdit(card)
        self._name_input.setPlaceholderText("Your name")
        self._name_input.setMinimumHeight(38)
        self._name_input.setVisible(False)
        cl.addWidget(self._name_input)

        # Email
        self._email_input = QLineEdit(card)
        self._email_input.setPlaceholderText("you@email.com")
        self._email_input.setMinimumHeight(38)
        cl.addWidget(self._email_input)

        # Password + show/hide toggle
        pw_row = QHBoxLayout()
        pw_row.setSpacing(6)
        self._password_input = QLineEdit(card)
        self._password_input.setPlaceholderText("Password")
        self._password_input.setEchoMode(QLineEdit.Password)
        self._password_input.setMinimumHeight(38)
        self._password_input.returnPressed.connect(self._submit_email_auth)
        pw_row.addWidget(self._password_input)
        self._show_pw_btn = QPushButton(card)
        self._show_pw_btn.setFixedSize(40, 38)
        self._show_pw_btn.setIcon(QIcon(self._asset("eye.svg")))
        self._show_pw_btn.setToolTip("Show password")
        self._show_pw_btn.setCursor(Qt.PointingHandCursor)
        self._show_pw_btn.setStyleSheet(
            "border: 1px solid #cbd5e1; border-radius: 8px; background: #ffffff;"
        )
        self._show_pw_btn.clicked.connect(self._toggle_password_visibility)
        pw_row.addWidget(self._show_pw_btn)
        cl.addLayout(pw_row)

        # Inline message (errors / success)
        self._msg_label = QLabel("", card)
        self._msg_label.setWordWrap(True)
        self._msg_label.setVisible(False)
        cl.addWidget(self._msg_label)

        # Primary submit
        self._primary_btn = QPushButton("Sign in", card)
        self._primary_btn.setObjectName("primaryButton")
        self._primary_btn.setMinimumHeight(42)
        self._primary_btn.clicked.connect(self._submit_email_auth)
        cl.addWidget(self._primary_btn)

        # Resend verification (hidden until a sign-up needs confirmation)
        self._resend_btn = QPushButton("Resend verification email", card)
        self._resend_btn.setFlat(True)
        self._resend_btn.setStyleSheet("color: #3b82f6; border: none; font-weight: 600;")
        self._resend_btn.setVisible(False)
        self._resend_btn.clicked.connect(self._resend_verification)
        cl.addWidget(self._resend_btn, alignment=Qt.AlignCenter)

        # Forgot password (sign-in only)
        self._forgot_btn = QPushButton("Forgot password?", card)
        self._forgot_btn.setFlat(True)
        self._forgot_btn.setStyleSheet("color: #3b82f6; border: none;")
        self._forgot_btn.clicked.connect(self._forgot_password)
        cl.addWidget(self._forgot_btn, alignment=Qt.AlignCenter)

        # Divider + optional Google social login
        div = QHBoxLayout(); div.setSpacing(8)
        ll = QFrame(card); ll.setFrameShape(QFrame.HLine); ll.setStyleSheet("color:#cbd5e1; max-height:1px;")
        lr = QFrame(card); lr.setFrameShape(QFrame.HLine); lr.setStyleSheet("color:#cbd5e1; max-height:1px;")
        orl = QLabel("or", card); orl.setObjectName("subtitleLabel")
        div.addWidget(ll, 1); div.addWidget(orl); div.addWidget(lr, 1)
        cl.addLayout(div)
        self.btn_google = QPushButton("   Sign in with Google", card)
        self.btn_google.setIcon(QIcon(self._asset("google_g.svg")))
        self.btn_google.setIconSize(QSize(20, 20))
        self.btn_google.setMinimumHeight(44)
        self.btn_google.setCursor(Qt.PointingHandCursor)
        self.btn_google.setStyleSheet(
            "QPushButton { background: #ffffff; color: #3c4043; border: 1px solid #dadce0;"
            " border-radius: 8px; font-size: 14px; font-weight: 600; padding: 0 12px; }"
            "QPushButton:hover { background: #f7faff; border-color: #d2e3fc; }"
        )
        self.btn_google.clicked.connect(self._choose_google)
        cl.addWidget(self.btn_google)

        lay.addWidget(card)

        # Guest
        self.btn_guest = QPushButton("Continue as guest  →", self.page_account)
        self.btn_guest.setFlat(True)
        self.btn_guest.setStyleSheet("color: #475569; border: none; font-weight: 600;")
        self.btn_guest.clicked.connect(self._choose_guest)
        lay.addWidget(self.btn_guest, alignment=Qt.AlignCenter)

        guest_note = QLabel(
            "Guest mode: 10 minutes of free recording, all models - no account needed.",
            self.page_account,
        )
        guest_note.setObjectName("subtitleLabel")
        guest_note.setAlignment(Qt.AlignCenter)
        guest_note.setWordWrap(True)
        guest_note.setStyleSheet("font-size: 11px; color: #94a3b8;")
        lay.addWidget(guest_note)

        lay.addStretch()
        self._set_auth_mode("signin")
        self.stack.addWidget(self.page_account)

    # ── Auth form behaviour ──────────────────────────────────────────────────
    def _set_auth_mode(self, mode):
        self._auth_mode = mode
        is_signup = (mode == "signup")
        self._name_input.setVisible(is_signup)
        if hasattr(self, "_forgot_btn"):
            self._forgot_btn.setVisible(not is_signup)
        self._primary_btn.setText("Create account" if is_signup else "Sign in")
        self._tab_signin.setObjectName("primaryButton" if not is_signup else "")
        self._tab_signup.setObjectName("primaryButton" if is_signup else "")
        for b in (self._tab_signin, self._tab_signup):
            b.style().unpolish(b); b.style().polish(b)
        self._msg_label.setVisible(False)
        self._resend_btn.setVisible(False)

    @staticmethod
    def _asset(name):
        try:
            from main import resource_path
            return resource_path("assets", name)
        except Exception:
            import os
            return os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", name
            )

    def _toggle_password_visibility(self):
        if self._password_input.echoMode() == QLineEdit.Password:
            self._password_input.setEchoMode(QLineEdit.Normal)
            self._show_pw_btn.setIcon(QIcon(self._asset("eye-off.svg")))
            self._show_pw_btn.setToolTip("Hide password")
        else:
            self._password_input.setEchoMode(QLineEdit.Password)
            self._show_pw_btn.setIcon(QIcon(self._asset("eye.svg")))
            self._show_pw_btn.setToolTip("Show password")

    def _show_msg(self, text, error=False):
        self._msg_label.setText(text)
        self._msg_label.setStyleSheet(
            f"font-size: 12px; font-weight: 600; color: {'#dc2626' if error else '#16a34a'};"
        )
        self._msg_label.setVisible(True)

    def _set_form_busy(self, busy):
        widgets = [self._name_input, self._email_input, self._password_input,
                   self._primary_btn, self._tab_signin, self._tab_signup, self._show_pw_btn]
        if hasattr(self, "btn_google"):
            widgets.append(self.btn_google)
        for w in widgets:
            w.setEnabled(not busy)
        if busy:
            self._primary_btn.setText("Please wait…")
        else:
            self._primary_btn.setText("Create account" if self._auth_mode == "signup" else "Sign in")

    def _submit_email_auth(self):
        email = self._email_input.text().strip()
        pw = self._password_input.text()
        if "@" not in email or "." not in email.split("@")[-1]:
            self._show_msg("Please enter a valid email address.", error=True)
            return
        if len(pw) < 6:
            self._show_msg("Password must be at least 6 characters.", error=True)
            return
        name = self._name_input.text().strip()
        self._msg_label.setVisible(False)
        self._resend_btn.setVisible(False)
        self._set_form_busy(True)
        mode = self._auth_mode
        auth = getattr(self.app, "auth", None)

        def _run():
            if not auth:
                self.auth_result.emit("error", "Sign-in is unavailable right now.")
                return
            if mode == "signup":
                status, msg = auth.sign_up_email(email, pw, name or None)
            else:
                status, msg = auth.sign_in_email(email, pw)
            self.auth_result.emit(status, msg)

        threading.Thread(target=_run, daemon=True).start()

    def _on_auth_result(self, status, message):
        self._set_form_busy(False)
        if status == "ok":
            self.guest_mode = False
            self._proceed_after_auth()
        elif status == "verify":
            self._pending_verify_email = message
            self._set_auth_mode("signin")
            self._show_msg(
                f"We emailed a verification link to {message}. Confirm it, then sign in.",
                error=False,
            )
            self._resend_btn.setVisible(True)
        else:
            self._show_msg(message or "Something went wrong. Please try again.", error=True)

    def _resend_verification(self):
        email = self._pending_verify_email or self._email_input.text().strip()
        auth = getattr(self.app, "auth", None)
        if email and auth:
            threading.Thread(target=lambda: auth.resend_verification(email), daemon=True).start()
            self._show_msg("Verification email resent - check your inbox.", error=False)

    def _forgot_password(self):
        email = self._email_input.text().strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            self._show_msg("Enter your email above first, then tap Forgot password.", error=True)
            return
        auth = getattr(self.app, "auth", None)
        if auth and hasattr(auth, "send_password_reset"):
            threading.Thread(target=lambda: auth.send_password_reset(email), daemon=True).start()
            self._show_msg(f"If an account exists for {email}, a reset link is on its way.", error=False)

    def _proceed_after_auth(self):
        if self.account_only:
            self._finish_account_only()
        else:
            self._go_to(1)

    def _create_pages(self):
        # Page 0: Account gate (sign in / continue as guest)
        self._create_account_page()

        # Page 1: Welcome & Hotkey setup
        self.page_welcome = QWidget()
        layout_wel = QVBoxLayout(self.page_welcome)
        layout_wel.setContentsMargins(0, 0, 0, 0)
        layout_wel.setSpacing(16)
        
        welcome_title = QLabel("Welcome to Transcribe", self.page_welcome)
        welcome_title.setObjectName("titleLabel")
        welcome_title.setAlignment(Qt.AlignCenter)
        layout_wel.addWidget(welcome_title)
        
        welcome_desc = QLabel(
            "Transcribe is an always-on assistant that lives in your system tray.\n"
            "Press your hotkey, speak naturally, and your words appear instantly\n"
            "at your active typing cursor.",
            self.page_welcome
        )
        welcome_desc.setAlignment(Qt.AlignCenter)
        welcome_desc.setWordWrap(True)
        welcome_desc.setObjectName("subtitleLabel")
        layout_wel.addWidget(welcome_desc)

        # Card container for Hotkey configuration
        hotkey_card = QFrame(self.page_welcome)
        hotkey_card.setObjectName("cardFrame")
        layout_card = QVBoxLayout(hotkey_card)
        layout_card.setContentsMargins(20, 20, 20, 20)
        layout_card.setSpacing(12)
        
        card_title = QLabel("1. Setup Dictation Hotkey", hotkey_card)
        card_title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        layout_card.addWidget(card_title)
        
        card_desc = QLabel(
            "Click the button below and press any key combination (e.g., Alt+R) "
            "to set your dictation trigger.",
            hotkey_card
        )
        card_desc.setObjectName("subtitleLabel")
        card_desc.setWordWrap(True)
        layout_card.addWidget(card_desc)
        
        self.btn_hotkey = QPushButton(self.hotkey_val.upper(), hotkey_card)
        self.btn_hotkey.clicked.connect(self._toggle_capture)
        self.btn_hotkey.setStyleSheet("font-size: 15px; font-weight: bold; min-height: 40px; border-color: #3b82f6;")
        layout_card.addWidget(self.btn_hotkey)
        
        layout_wel.addWidget(hotkey_card)
        layout_wel.addStretch()
        self.stack.addWidget(self.page_welcome)

        # Page 1: Language Selection
        self.page_lang = QWidget()
        layout_lang = QVBoxLayout(self.page_lang)
        layout_lang.setContentsMargins(0, 0, 0, 0)
        layout_lang.setSpacing(16)
        
        lang_title = QLabel("Choose Language", self.page_lang)
        lang_title.setObjectName("titleLabel")
        lang_title.setAlignment(Qt.AlignCenter)
        layout_lang.addWidget(lang_title)
        
        lang_desc = QLabel(
            "Select your spoken language. Auto-detect will analyze the first few seconds "
            "of speech, which is perfect for multilingual dictation.",
            self.page_lang
        )
        lang_desc.setAlignment(Qt.AlignCenter)
        lang_desc.setWordWrap(True)
        lang_desc.setObjectName("subtitleLabel")
        layout_lang.addWidget(lang_desc)
        
        lang_card = QFrame(self.page_lang)
        lang_card.setObjectName("cardFrame")
        layout_lcard = QVBoxLayout(lang_card)
        layout_lcard.setContentsMargins(20, 20, 20, 20)
        layout_lcard.setSpacing(12)
        
        lcard_label = QLabel("2. Select Your Primary Language", lang_card)
        lcard_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        layout_lcard.addWidget(lcard_label)
        
        self.combo_lang = QComboBox(lang_card)
        langs = [
            ("auto", "Auto-detect"),
            ("multi", "Multilingual"),
            ("hy", "Armenian"),
            ("en", "English"),
            ("ru", "Russian"),
            ("fr", "French"),
            ("de", "German"),
            ("es", "Spanish"),
            ("ar", "Arabic")
        ]
        for val, label in langs:
            self.combo_lang.addItem(label, val)
        # Select matching item
        idx = self.combo_lang.findData(self.lang_val)
        if idx >= 0:
            self.combo_lang.setCurrentIndex(idx)
        layout_lcard.addWidget(self.combo_lang)
        
        layout_lang.addWidget(lang_card)
        layout_lang.addStretch()
        self.stack.addWidget(self.page_lang)

        # Page 2: Engine Backend & AI Settings
        self.page_engine = QWidget()
        layout_eng = QVBoxLayout(self.page_engine)
        layout_eng.setContentsMargins(0, 0, 0, 0)
        layout_eng.setSpacing(16)
        
        eng_title = QLabel("Choose Dictation Backend", self.page_engine)
        eng_title.setObjectName("titleLabel")
        eng_title.setAlignment(Qt.AlignCenter)
        layout_eng.addWidget(eng_title)
        
        # Local Backend Option Card
        self.local_card = QFrame(self.page_engine)
        self.local_card.setObjectName("cardFrame")
        self.local_card.setFrameShape(QFrame.StyledPanel)
        self.local_card.setStyleSheet("QFrame#cardFrame { border-color: #3b82f6; }") # default selection highlight
        layout_l = QVBoxLayout(self.local_card)
        layout_l.setContentsMargins(16, 16, 16, 16)
        layout_l.setSpacing(8)
        
        self.btn_sel_local = QPushButton("Local Offline AI (Whisper)", self.local_card)
        self.btn_sel_local.setObjectName("primaryButton")
        self.btn_sel_local.clicked.connect(lambda: self._set_backend("local"))
        layout_l.addWidget(self.btn_sel_local)
        
        local_desc = QLabel(
            "Transcribe offline entirely on this machine. "
            "Ensures 100% privacy with zero network delays.",
            self.local_card
        )
        local_desc.setObjectName("subtitleLabel")
        local_desc.setWordWrap(True)
        layout_l.addWidget(local_desc)
        
        layout_eng.addWidget(self.local_card)
        
        # Cloud Backend Option Card
        self.cloud_card = QFrame(self.page_engine)
        self.cloud_card.setObjectName("cardFrame")
        layout_c = QVBoxLayout(self.cloud_card)
        layout_c.setContentsMargins(16, 16, 16, 16)
        layout_c.setSpacing(8)
        
        self.btn_sel_cloud = QPushButton("Google Cloud Engine", self.cloud_card)
        self.btn_sel_cloud.clicked.connect(lambda: self._set_backend("google"))
        layout_c.addWidget(self.btn_sel_cloud)
        
        cloud_desc = QLabel(
            "Ideal for low-end laptops. Leverages Google Cloud Speech-to-Text "
            "for superior accuracy (specifically for Armenian). Requires a free API Key.",
            self.cloud_card
        )
        cloud_desc.setObjectName("subtitleLabel")
        cloud_desc.setWordWrap(True)
        layout_c.addWidget(cloud_desc)
        
        layout_eng.addWidget(self.cloud_card)
        
        # Mistral Backend Option Card
        self.mistral_card = QFrame(self.page_engine)
        self.mistral_card.setObjectName("cardFrame")
        layout_m = QVBoxLayout(self.mistral_card)
        layout_m.setContentsMargins(16, 16, 16, 16)
        layout_m.setSpacing(8)
        
        self.btn_sel_mistral = QPushButton("Mistral AI Voxtral STT", self.mistral_card)
        self.btn_sel_mistral.clicked.connect(lambda: self._set_backend("mistral"))
        layout_m.addWidget(self.btn_sel_mistral)
        
        mistral_desc = QLabel(
            "Leverages Mistral's highly accurate Voxtral STT model. "
            "Perfect for multilingual meetings and developer workflows. Requires a Mistral API key.",
            self.mistral_card
        )
        mistral_desc.setObjectName("subtitleLabel")
        mistral_desc.setWordWrap(True)
        layout_m.addWidget(mistral_desc)
        
        layout_eng.addWidget(self.mistral_card)
        
        # Google API Input Box (Hidden initially)
        self.google_input_frame = QFrame(self.page_engine)
        self.google_input_frame.setVisible(False)
        layout_gi = QVBoxLayout(self.google_input_frame)
        layout_gi.setContentsMargins(0, 0, 0, 0)
        layout_gi.setSpacing(6)
        
        layout_gi.addWidget(QLabel("Paste Google Cloud API Key:", self.google_input_frame))
        self.google_key_input = QLineEdit(self.google_input_frame)
        self.google_key_input.setPlaceholderText("AIzaSy...")
        self.google_key_input.setText(self.google_key_val)
        layout_gi.addWidget(self.google_key_input)
        layout_eng.addWidget(self.google_input_frame)

        # Mistral API Input Box (Hidden initially)
        self.mistral_input_frame = QFrame(self.page_engine)
        self.mistral_input_frame.setVisible(False)
        layout_mi = QVBoxLayout(self.mistral_input_frame)
        layout_mi.setContentsMargins(0, 0, 0, 0)
        layout_mi.setSpacing(6)
        
        layout_mi.addWidget(QLabel("Paste Mistral API Key:", self.mistral_input_frame))
        self.mistral_key_input = QLineEdit(self.mistral_input_frame)
        self.mistral_key_input.setPlaceholderText("mistral-key...")
        self.mistral_key_input.setText(self.mistral_key_val)
        layout_mi.addWidget(self.mistral_key_input)
        layout_eng.addWidget(self.mistral_input_frame)

        # Telemetry Consent
        self.chk_telemetry = QCheckBox("Share anonymous usage metrics to improve Armenian AI models", self.page_engine)
        self.chk_telemetry.setChecked(self.analytics_val)
        layout_eng.addWidget(self.chk_telemetry)

        layout_eng.addStretch()
        self.stack.addWidget(self.page_engine)

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
                self.hotkey_val = hotkey_str
                self.capturing = False
                self.btn_hotkey.setText(self.hotkey_val.upper().replace("MOUSE:", "MOUSE "))
                self.btn_hotkey.setStyleSheet("font-size: 15px; font-weight: bold; min-height: 40px; border-color: #3b82f6;")
                return True # swallow event
        return super().eventFilter(obj, event)

    def _toggle_capture(self):
        if self.capturing:
            self.capturing = False
            self.btn_hotkey.setText(self.hotkey_val.upper())
            self.btn_hotkey.setStyleSheet("font-size: 15px; font-weight: bold; min-height: 40px; border-color: #3b82f6;")
        else:
            self.capturing = True
            self.btn_hotkey.setText("PRESS ANY HOTKEY COMBINATION...")
            self.btn_hotkey.setStyleSheet("font-size: 15px; font-weight: bold; min-height: 40px; border-color: #ef4444; color: #ef4444;")
            self.setFocus() # Pull focus away from button so keyPressEvent captures correctly

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if not self.capturing:
                event.ignore()
                return

        if not self.capturing:
            super().keyPressEvent(event)
            return

        key = event.key()
        
        # Disallow simple single keys unless they are modifiers + something
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

        # Map special key codes
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
        elif 48 <= key <= 90: # A-Z and 0-9
            key_str = chr(key).lower()
        else:
            return # ignore weird keys

        # Modifier-less binding is only safe for Function keys (F1-F12); a bare
        # typing/navigation key would fire during normal use.
        is_function_key = len(key_str) >= 2 and key_str[0] == "f" and key_str[1:].isdigit()
        if not mods and not is_function_key:
            QMessageBox.warning(
                self, "Modifier Required",
                "Use at least one modifier (Ctrl, Alt, Shift, or Win), a Function "
                "key (F1-F12), or a mouse button.\n\nA single typing key can't be a "
                "hotkey because it would trigger while you type."
            )
            return

        self.hotkey_val = "+".join(mods + [key_str]) if mods else key_str
        self._toggle_capture()

    def _set_backend(self, backend):
        self.backend_val = backend
        
        self.local_card.setStyleSheet(f"QFrame#cardFrame {{ border-color: {'#3b82f6' if backend == 'local' else '#27272a'}; }}")
        self.cloud_card.setStyleSheet(f"QFrame#cardFrame {{ border-color: {'#3b82f6' if backend == 'google' else '#27272a'}; }}")
        self.mistral_card.setStyleSheet(f"QFrame#cardFrame {{ border-color: {'#3b82f6' if backend == 'mistral' else '#27272a'}; }}")
        
        self.google_input_frame.setVisible(backend == "google")
        self.mistral_input_frame.setVisible(backend == "mistral")
        
        self.btn_sel_local.setObjectName("primaryButton" if backend == "local" else "")
        self.btn_sel_cloud.setObjectName("primaryButton" if backend == "google" else "")
        self.btn_sel_mistral.setObjectName("primaryButton" if backend == "mistral" else "")
        
        for btn in (self.btn_sel_local, self.btn_sel_cloud, self.btn_sel_mistral):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        for card in (self.local_card, self.cloud_card, self.mistral_card):
            card.style().unpolish(card)
            card.style().polish(card)

    def _go_to(self, page):
        self.current_page = page
        self.stack.setCurrentIndex(page)
        self._update_nav()

    def _update_nav(self):
        # Page 0 is the account gate (its own buttons drive the flow); pages
        # 1-3 are the 3-step setup wizard with the Back/Next bar.
        p = self.current_page
        if p == 0:
            self.step_label.setText("WELCOME")
            self.btn_back.setVisible(False)
            self.btn_next.setVisible(False)
        else:
            self.step_label.setText(f"STEP {p} OF 3")
            self.btn_back.setVisible(True)
            self.btn_next.setVisible(True)
            self.btn_back.setEnabled(p > 1)
            self.btn_next.setText("Get Started" if p == 3 else "Next")

    def _choose_google(self):
        self.guest_mode = False
        if self.app and hasattr(self.app, "start_google_login"):
            self.app.start_google_login()
        if self.account_only:
            self._finish_account_only()
        else:
            self._go_to(1)

    def _choose_guest(self):
        self.guest_mode = True
        if self.account_only:
            self._finish_account_only()
        else:
            self._go_to(1)

    def _finish_account_only(self):
        if self.app:
            self.app.cfg["account_gate_seen"] = True
            self.app.save_config()
        self.accept()

    def _back(self):
        if self.current_page > 1:
            self._go_to(self.current_page - 1)

    def _next(self):
        if self.current_page < 3:
            self._go_to(self.current_page + 1)
        else:
            self._save_onboarding()

    def _save_onboarding(self):
        # Capture configurations
        self.lang_val = self.combo_lang.currentData()
        self.google_key_val = self.google_key_input.text().strip()
        self.mistral_key_val = self.mistral_key_input.text().strip()
        self.analytics_val = self.chk_telemetry.isChecked()
        
        if self.backend_val == "google" and not self.google_key_val:
            reply = QMessageBox.question(
                self, "Missing API Key",
                "You selected Google Cloud but didn't provide an API key.\n\n"
                "Do you want to go back and add it, or default to offline Local AI?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                return
            else:
                self._set_backend("local")
                return

        if self.backend_val == "mistral" and not self.mistral_key_val:
            reply = QMessageBox.question(
                self, "Missing API Key",
                "You selected Mistral AI but didn't provide an API key.\n\n"
                "Do you want to go back and add it, or default to offline Local AI?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                return
            else:
                self._set_backend("local")
                return

        # Save to main app configuration
        if self.app:
            self.app.cfg["hotkey"] = self.hotkey_val
            self.app.cfg["language"] = self.lang_val
            self.app.cfg["backend"] = self.backend_val
            self.app.cfg["google_api_key"] = self.google_key_val
            self.app.cfg["mistral_api_key"] = self.mistral_key_val
            self.app.cfg["mistral_stt_model"] = "voxtral-mini-latest"
            self.app.cfg["analytics_enabled"] = self.analytics_val
            self.app.cfg["onboarding_done"] = True
            self.app.cfg["account_gate_seen"] = True

            self.app.save_config()
            self.app.apply_tray_bindings()
            
        self.accept()
