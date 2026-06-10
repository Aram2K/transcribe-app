# Interactive "Go Pro" upgrade dialog - benefits + plan picker + checkout.

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

        eyebrow = QLabel("TRANSCRIBE PRO", self)
        eyebrow.setAlignment(Qt.AlignCenter)
        eyebrow.setStyleSheet("font-size: 11px; font-weight: 800; letter-spacing: 2px; color: #a855f7;")
        root.addWidget(eyebrow)

        title = QLabel("Everything you get with Pro", self)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 22px; font-weight: 800; color: #0f172a;")
        root.addWidget(title)

        if feature:
            sub = QLabel(f"{feature} is a Pro feature.", self)
            sub.setAlignment(Qt.AlignCenter)
            sub.setObjectName("subtitleLabel")
            sub.setWordWrap(True)
            root.addWidget(sub)

        # ── Benefits (clean checkmarks, no emojis) ──
        card = QFrame(self)
        card.setObjectName("glassCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.setSpacing(10)
        for head, desc in (
            ("Meeting recording with AI notes", "Record any call and get clean, summarized minutes."),
            ("Smart Actions", "Translate, rewrite, summarize and draft emails by voice."),
            ("Fast cloud transcription", "Accurate, with no API key, no setup and no timeouts."),
            ("Priority models and support", "The best models first, and a direct line to us."),
        ):
            row = QHBoxLayout()
            row.setSpacing(12)
            ic = QLabel("✓", card)
            ic.setStyleSheet("font-size: 16px; font-weight: 800; color: #a855f7;")
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

        # ── Plan picker ──
        plans = QHBoxLayout()
        plans.setSpacing(10)
        self.card_monthly = self._plan_card("Monthly", "€7.99", "/mo", None, None, "monthly")
        self.card_annual = self._plan_card("Annual", "€59", "/yr", "€96", "Save 38%", "annual")
        plans.addWidget(self.card_monthly)
        plans.addWidget(self.card_annual)
        root.addLayout(plans)

        # ── CTA ──
        self.btn_go = QPushButton("Start 3-day free trial", self)
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

    def _plan_card(self, name, price, per, original, badge, plan_id):
        card = QFrame(self)
        card.setObjectName("cardFrame")
        card.setCursor(Qt.PointingHandCursor)
        card.setMinimumHeight(94)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(6)

        top = QLabel(name, card)
        top.setStyleSheet("font-weight: 700; color: #334155; font-size: 13px;")
        v.addWidget(top)

        # Price row, with an optional struck-through "was" price before it.
        prow = QHBoxLayout()
        prow.setSpacing(5)
        if original:
            o = QLabel(f"<s>{original}</s>", card)
            o.setStyleSheet("color: #94a3b8; font-size: 15px;")
            prow.addWidget(o, 0, Qt.AlignBottom)
        p = QLabel(price, card)
        p.setStyleSheet("font-size: 30px; font-weight: 800; color: #0f172a;")
        prow.addWidget(p)
        u = QLabel(per, card)
        u.setObjectName("subtitleLabel")
        prow.addWidget(u, 0, Qt.AlignBottom)
        prow.addStretch()
        v.addLayout(prow)

        # Bottom slot, aligned across both cards: a "Save" pill or a plain note.
        if badge:
            b = QLabel(badge, card)
            b.setObjectName("proBadge")
        else:
            b = QLabel("billed monthly", card)
            b.setObjectName("subtitleLabel")
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
            self.note.setText("3 days free, then your plan. Cancel anytime before it ends - no charge. Secure checkout by Stripe.")
        else:
            self.note.setText("You'll create or sign in to your account first, then start your free trial.")

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
