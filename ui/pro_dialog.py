# Interactive "Go Pro" upgrade dialog — benefits + plan picker + checkout.

import threading
import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame
)


class ProDialog(QDialog):
    """A friendly, visual upgrade dialog. Shows what Pro unlocks, lets the user
    pick Monthly / Annual, and sends them to Stripe checkout (after sign-in)."""

    def __init__(self, main_app=None, feature=None):
        super().__init__()
        self.app = main_app
        self._plan = "annual"  # default to the best-value plan
        self.setWindowTitle("Transcribe Pro")
        self.setMinimumWidth(440)
        if self.app and hasattr(self.app, "style_content"):
            self.setStyleSheet(self.app.style_content)
        self._build(feature)

    def _build(self, feature):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(12)

        crown = QLabel("✦", self)
        crown.setAlignment(Qt.AlignCenter)
        crown.setStyleSheet("font-size: 38px; color: #a855f7;")
        root.addWidget(crown)

        title = QLabel("Go Pro", self)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 24px; font-weight: 800; color: #7e22ce;")
        root.addWidget(title)

        sub = QLabel(
            (f"{feature} is a Pro feature.\n" if feature else "")
            + "Unlock the full power of Transcribe.",
            self,
        )
        sub.setAlignment(Qt.AlignCenter)
        sub.setObjectName("subtitleLabel")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # ── Benefits ──
        card = QFrame(self)
        card.setObjectName("glassCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.setSpacing(12)
        for icon, head, desc in (
            ("🎤", "Meeting recording + AI notes", "Record any call and get clean, summarized minutes."),
            ("🧠", "Smart Actions", "Translate, rewrite, summarize, draft emails — just by voice."),
            ("⚡", "Managed cloud transcription", "Fast and accurate — no API key, no setup, no timeouts."),
            ("⭐", "Priority models & support", "The best models first, and a direct line to us."),
        ):
            row = QHBoxLayout()
            row.setSpacing(12)
            ic = QLabel(icon, card)
            ic.setStyleSheet("font-size: 20px;")
            row.addWidget(ic, 0, Qt.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(1)
            h = QLabel(head, card)
            h.setStyleSheet("font-weight: 700; font-size: 14px; color: #0f172a;")
            h.setWordWrap(True)
            d = QLabel(desc, card)
            d.setObjectName("subtitleLabel")
            d.setWordWrap(True)
            col.addWidget(h)
            col.addWidget(d)
            row.addLayout(col, 1)
            cl.addLayout(row)
        root.addWidget(card)

        # ── Free trial (only for signed-in users who haven't used it) ──
        authed = bool(self.app and getattr(self.app, "auth", None) and self.app.auth.is_authenticated)
        if authed and getattr(self.app.auth, "trial_available", False):
            self.btn_trial = QPushButton("🎁  Start 3-day free trial — no card needed", self)
            self.btn_trial.setMinimumHeight(44)
            self.btn_trial.setCursor(Qt.PointingHandCursor)
            self.btn_trial.setStyleSheet(
                "background-color: #22c55e; border: 1px solid #16a34a; color: white;"
                "font-weight: 700; border-radius: 10px;"
            )
            self.btn_trial.clicked.connect(self._start_trial)
            root.addWidget(self.btn_trial)
            orlbl = QLabel("or subscribe now", self)
            orlbl.setObjectName("subtitleLabel")
            orlbl.setAlignment(Qt.AlignCenter)
            root.addWidget(orlbl)

        # ── Plan picker ──
        plans = QHBoxLayout()
        plans.setSpacing(10)
        self.card_monthly = self._plan_card("Monthly", "€7.99", "/mo", None, "monthly")
        self.card_annual = self._plan_card("Annual", "€59", "/yr", "Save 38%", "annual")
        plans.addWidget(self.card_monthly)
        plans.addWidget(self.card_annual)
        root.addLayout(plans)

        # ── CTA ──
        self.btn_go = QPushButton("Go Pro", self)
        self.btn_go.setObjectName("primaryButton")
        self.btn_go.setMinimumHeight(46)
        self.btn_go.setStyleSheet(
            "font-size: 15px; font-weight: 700;"
            "background-color: #a855f7; border-color: #9333ea;"
        )
        self.btn_go.clicked.connect(self._go)
        root.addWidget(self.btn_go)

        self.note = QLabel("", self)
        self.note.setObjectName("subtitleLabel")
        self.note.setAlignment(Qt.AlignCenter)
        self.note.setWordWrap(True)
        root.addWidget(self.note)

        later = QPushButton("Maybe later", self)
        later.setFlat(True)
        later.setStyleSheet("color: #64748b; border: none;")
        later.clicked.connect(self.reject)
        root.addWidget(later, alignment=Qt.AlignCenter)

        self._select_plan("annual")
        self._refresh_note()

    def _plan_card(self, name, price, per, badge, plan_id):
        card = QFrame(self)
        card.setObjectName("cardFrame")
        card.setCursor(Qt.PointingHandCursor)
        card.setMinimumHeight(92)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(3)
        top = QLabel(name, card)
        top.setStyleSheet("font-weight: 700; color: #334155;")
        v.addWidget(top)
        prow = QHBoxLayout()
        prow.setSpacing(2)
        p = QLabel(price, card)
        p.setStyleSheet("font-size: 22px; font-weight: 800; color: #0f172a;")
        u = QLabel(per, card)
        u.setObjectName("subtitleLabel")
        prow.addWidget(p)
        prow.addWidget(u, 0, Qt.AlignBottom)
        prow.addStretch()
        v.addLayout(prow)
        if badge:
            b = QLabel(badge, card)
            b.setObjectName("proBadge")
            v.addWidget(b, 0, Qt.AlignLeft)
        card.mousePressEvent = lambda _e, pid=plan_id: self._select_plan(pid)
        return card

    def _select_plan(self, plan_id):
        self._plan = plan_id
        for card, pid in ((self.card_monthly, "monthly"), (self.card_annual, "annual")):
            selected = (pid == plan_id)
            card.setStyleSheet(
                "QFrame#cardFrame { border: 2px solid %s; border-radius: 12px; background: %s; }"
                % (
                    "#a855f7" if selected else "#e2e8f0",
                    "#faf5ff" if selected else "rgba(255, 255, 255, 200)",
                )
            )

    def _refresh_note(self):
        authed = bool(
            self.app
            and getattr(self.app, "auth", None)
            and self.app.auth.is_authenticated
        )
        if authed:
            self.note.setText("Secure checkout via Stripe · cancel anytime.")
        else:
            self.note.setText("You'll create or sign in to your account first, then checkout.")

    def _start_trial(self):
        if not (self.app and getattr(self.app, "auth", None)):
            return
        self.btn_trial.setEnabled(False)
        self.btn_trial.setText("Starting your trial…")

        def _run():
            try:
                self.app.auth.start_trial()
            except Exception:
                pass
            try:
                self.app.sig_auth_changed.emit()
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
        try:
            import telemetry
            import main as m
            telemetry.track("trial_started", {}, self.app.cfg, m.APP_VERSION)
        except Exception:
            pass
        self.accept()

    def _go(self):
        if not self.app:
            return
        authed = getattr(self.app, "auth", None) and self.app.auth.is_authenticated
        if not authed:
            # A subscription must be linked to an account.
            self.accept()
            if hasattr(self.app, "show_auth_gate"):
                self.app.show_auth_gate()
            return
        import main as m
        url = m.PRO_ANNUAL_URL if self._plan == "annual" else m.PRO_MONTHLY_URL
        if hasattr(self.app, "_checkout_url"):
            url = self.app._checkout_url(url)
        webbrowser.open(url)
        try:
            import telemetry
            telemetry.track("checkout_opened", {"plan": self._plan}, self.app.cfg, m.APP_VERSION)
        except Exception:
            pass
        self.accept()
