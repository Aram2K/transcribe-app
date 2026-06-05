"""
Supabase auth + Pro entitlement client for the Transcribe desktop app.

Security design (this whole module is built around "never trust the client"):

  * Login uses the OAuth **PKCE** flow through the user's system browser and a
    localhost loopback redirect. No OAuth client secret ever lives in the app.
  * Only the **publishable** Supabase key is embedded (it is designed to be
    public; Row Level Security on the database is what actually protects data).
  * The **refresh token** is stored in the OS keyring (Windows Credential
    Manager / macOS Keychain) via storage.write_secret - never in a plaintext
    file. The short-lived access token is kept in memory only.
  * "Are you Pro?" is answered by the server-side `is_pro()` RPC, which reads
    the user's own subscription rows. The client can never self-grant Pro.

The Qt layer should treat every method here as potentially slow/blocking and
call the network ones from a worker thread, using `on_state_changed` to refresh
the UI on the main thread.
"""

import base64
import hashlib
import http.server
import logging
import secrets
import threading
import time
import urllib.parse
import webbrowser

import requests

import storage

logger = logging.getLogger("transcribe.auth")

# ── Project configuration (safe to ship) ──────────────────────────────────────
SUPABASE_URL = "https://hftcelxzfoubheqeoool.supabase.co"
SUPABASE_PUBLISHABLE_KEY = "sb_publishable_aVAVHwDgUygeu_1QHyxg5w_3GGP_k5p"

# Loopback redirect used for the OAuth round-trip. This exact URL must be added
# to Supabase → Authentication → URL Configuration → Redirect URLs.
REDIRECT_PORT = 53682
REDIRECT_PATH = "/auth/callback"
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}{REDIRECT_PATH}"

# Keyring secret name for the refresh token (uses storage.SECRET_SERVICE).
REFRESH_TOKEN_SECRET = "auth_refresh_token"

_HTTP_TIMEOUT = 10
_LOGIN_TIMEOUT = 180  # seconds the user has to complete the browser login

_SUCCESS_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Transcribe</title>"
    "<style>body{font-family:Segoe UI,system-ui,sans-serif;background:#0f0f12;"
    "color:#fff;display:flex;height:100vh;align-items:center;justify-content:center;"
    "margin:0}.card{text-align:center}.c{color:#f59e0b;font-size:42px}</style></head>"
    "<body><div class='card'><div class='c'>&#10003;</div>"
    "<h2>You're signed in to Transcribe</h2>"
    "<p>You can close this tab and return to the app.</p></div></body></html>"
)
_ERROR_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Transcribe</title></head>"
    "<body style='font-family:sans-serif'><h2>Sign-in failed</h2>"
    "<p>Please return to the app and try again.</p></body></html>"
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _make_pkce_pair():
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot handler that captures ?code=&state= from the OAuth redirect."""

    server_version = "TranscribeAuth/1.0"

    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = (params.get("code") or [None])[0]
        self.server.auth_state = (params.get("state") or [None])[0]
        self.server.auth_error = (params.get("error_description") or params.get("error") or [None])[0]

        body = _SUCCESS_HTML if self.server.auth_code else _ERROR_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):  # silence default stderr logging
        return


class AuthManager:
    """Holds session + entitlement state and runs the login/refresh flows."""

    def __init__(self, on_state_changed=None):
        self._lock = threading.RLock()
        self._on_state_changed = on_state_changed

        self._access_token = None
        self._access_expires_at = 0.0
        self._refresh_token = None

        self.user_email = None
        self.user_name = None
        self.user_id = None
        self.is_pro = False
        self.plan = None              # 'monthly' | 'annual' | None
        self.period_end = None        # ISO string or None
        self.cancel_at_period_end = False
        self.trial_available = False   # true if the one-time trial was never started
        self.is_admin = False          # server-authoritative admin flag

    # ── public state helpers ─────────────────────────────────────────────────
    @property
    def is_authenticated(self):
        return bool(self._refresh_token)

    def _notify(self):
        cb = self._on_state_changed
        if cb:
            try:
                cb()
            except Exception:
                logger.debug("auth state callback failed", exc_info=True)

    def _headers(self, with_auth=False):
        h = {
            "apikey": SUPABASE_PUBLISHABLE_KEY,
            "Content-Type": "application/json",
        }
        if with_auth and self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    # ── session bootstrap (call on app start, in a thread) ───────────────────
    def load_session(self):
        """Restore a session from the stored refresh token, if any."""
        rt = ""
        try:
            rt = storage.read_secret(REFRESH_TOKEN_SECRET) or ""
        except Exception:
            rt = ""
        if not rt:
            return False
        with self._lock:
            self._refresh_token = rt
        ok = self._refresh_access_token()
        if ok:
            self.refresh_entitlement()
        self._notify()
        return ok

    # ── Google login via system browser + loopback (call in a thread) ────────
    def sign_in_with_google(self):
        """Run the full PKCE OAuth flow. Returns True on success.

        Blocking - call from a worker thread. The browser opens; we wait for the
        loopback redirect, exchange the code, persist the refresh token, and
        load entitlement."""
        verifier, challenge = _make_pkce_pair()
        state = secrets.token_urlsafe(24)

        query = urllib.parse.urlencode({
            "provider": "google",
            "redirect_to": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "s256",
        })
        authorize_url = f"{SUPABASE_URL}/auth/v1/authorize?{query}"

        try:
            server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
        except OSError as e:
            logger.error("Could not bind loopback port %d: %s", REDIRECT_PORT, e)
            return False
        server.timeout = _LOGIN_TIMEOUT
        server.auth_code = None
        server.auth_state = None
        server.auth_error = None

        try:
            webbrowser.open(authorize_url)
            # Wait (bounded) for the single callback request.
            deadline = time.time() + _LOGIN_TIMEOUT
            while server.auth_code is None and server.auth_error is None and time.time() < deadline:
                server.handle_request()
        finally:
            try:
                server.server_close()
            except Exception:
                pass

        if server.auth_error:
            logger.warning("OAuth returned error: %s", server.auth_error)
            return False
        if not server.auth_code:
            logger.warning("OAuth timed out with no code")
            return False
        if server.auth_state and state and server.auth_state != state:
            # GoTrue manages its own state internally; only reject on a real mismatch.
            logger.debug("OAuth state mismatch (non-fatal): %s", server.auth_state)

        return self._exchange_code(server.auth_code, verifier)

    def _exchange_code(self, auth_code, verifier):
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=pkce",
                headers=self._headers(),
                json={"auth_code": auth_code, "code_verifier": verifier},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.error("PKCE token exchange failed: %s", e)
            return False
        if resp.status_code != 200:
            logger.error("PKCE exchange HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        self._apply_session(resp.json())
        self.refresh_entitlement()
        self._notify()
        return True

    # ── Email / password ─────────────────────────────────────────────────────
    def sign_in_email(self, email, password):
        """Sign in with email + password.
        Returns (status, message): ("ok", "") or ("error", <message>)."""
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                headers=self._headers(),
                json={"email": email, "password": password},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return ("error", "Network error - check your connection and try again.")
        if resp.status_code == 200:
            self._apply_session(resp.json())
            self.refresh_entitlement()
            self._notify()
            return ("ok", "")
        msg = self._error_message(resp)
        # Friendlier copy for the two most common cases.
        low = msg.lower()
        if "not confirmed" in low or "not been confirmed" in low:
            return ("error", "Please verify your email first - check your inbox.")
        if "invalid" in low or "credential" in low:
            return ("error", "Incorrect email or password.")
        return ("error", msg)

    def sign_up_email(self, email, password, name=None):
        """Create an account with email + password (and optional display name).
        Returns (status, message):
          ("ok", "")        - signed in immediately (confirmations disabled)
          ("verify", email) - a confirmation email was sent
          ("error", <msg>)  - failed."""
        body = {"email": email, "password": password}
        if name:
            body["data"] = {"full_name": name}
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/auth/v1/signup",
                headers=self._headers(),
                json=body,
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return ("error", "Network error - check your connection and try again.")
        if resp.status_code in (200, 201):
            data = resp.json() or {}
            session = data.get("session") or data
            if session.get("access_token"):
                self._apply_session(session)
                self.refresh_entitlement()
                self._notify()
                return ("ok", "")
            return ("verify", email)
        return ("error", self._error_message(resp))

    def send_password_reset(self, email):
        """Send a password-recovery email (Supabase /recover)."""
        try:
            requests.post(
                f"{SUPABASE_URL}/auth/v1/recover",
                headers=self._headers(),
                json={"email": email},
                timeout=_HTTP_TIMEOUT,
            )
            return True
        except requests.RequestException:
            return False

    def resend_verification(self, email):
        try:
            requests.post(
                f"{SUPABASE_URL}/auth/v1/resend",
                headers=self._headers(),
                json={"type": "signup", "email": email},
                timeout=_HTTP_TIMEOUT,
            )
            return True
        except requests.RequestException:
            return False

    @staticmethod
    def _error_message(resp, fallback="Something went wrong. Please try again."):
        try:
            data = resp.json()
            return (
                data.get("msg")
                or data.get("error_description")
                or data.get("message")
                or data.get("error")
                or fallback
            )
        except Exception:
            return fallback

    # ── token plumbing ───────────────────────────────────────────────────────
    def _apply_session(self, data):
        with self._lock:
            self._access_token = data.get("access_token")
            expires_in = data.get("expires_in") or 3600
            self._access_expires_at = time.time() + float(expires_in) - 60
            rt = data.get("refresh_token")
            if rt:
                self._refresh_token = rt
                try:
                    storage.write_secret(REFRESH_TOKEN_SECRET, rt)
                except Exception:
                    logger.debug("Could not persist refresh token", exc_info=True)
            user = data.get("user") or {}
            meta = user.get("user_metadata") or {}
            self.user_id = user.get("id")
            self.user_email = user.get("email") or meta.get("email")
            self.user_name = (
                meta.get("full_name")
                or meta.get("name")
                or (self.user_email.split("@")[0] if self.user_email else None)
            )

    def _refresh_access_token(self):
        with self._lock:
            rt = self._refresh_token
        if not rt:
            return False
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token",
                headers=self._headers(),
                json={"refresh_token": rt},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.warning("Token refresh failed (network): %s", e)
            return False
        if resp.status_code != 200:
            logger.warning("Token refresh HTTP %s - clearing session", resp.status_code)
            # Refresh token is invalid/revoked: sign out locally.
            self._clear_local()
            self._notify()
            return False
        self._apply_session(resp.json())
        return True

    def get_access_token(self):
        """Return a valid access token, refreshing if it's near expiry."""
        with self._lock:
            token = self._access_token
            exp = self._access_expires_at
        if token and time.time() < exp:
            return token
        if self._refresh_access_token():
            with self._lock:
                return self._access_token
        return None

    # ── entitlement (server-side truth) ──────────────────────────────────────
    def refresh_entitlement(self):
        """Ask the server whether this user is Pro and pull plan details."""
        token = self.get_access_token()
        if not token:
            self._set_entitlement(False, None, None, False)
            return False
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/rpc/my_entitlement",
                headers=self._headers(with_auth=True),
                json={},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.warning("Entitlement check failed (network): %s", e)
            return False
        if resp.status_code != 200:
            logger.warning("Entitlement HTTP %s: %s", resp.status_code, resp.text[:200])
            return False
        rows = resp.json() or []
        if rows:
            row = rows[0]
            self._set_entitlement(
                bool(row.get("is_pro")),
                row.get("plan"),
                row.get("current_period_end"),
                bool(row.get("cancel_at_period_end")),
                bool(row.get("trial_available", False)),
                bool(row.get("is_admin", False)),
            )
        else:
            self._set_entitlement(False, None, None, False, False, False)
        self._notify()
        return True

    # The in-app, no-card trial was retired. Pro trials now run exclusively
    # through Stripe (3-day trial on the subscription, card on file), so there is
    # no client path to grant Pro; the server RPC is locked down to match.

    def _set_entitlement(self, is_pro, plan, period_end, cancel_at_period_end,
                         trial_available=False, is_admin=False):
        with self._lock:
            self.is_pro = is_pro
            self.plan = plan
            self.period_end = period_end
            self.cancel_at_period_end = cancel_at_period_end
            self.trial_available = trial_available
            self.is_admin = is_admin

    # ── sign out ─────────────────────────────────────────────────────────────
    def sign_out(self):
        token = None
        with self._lock:
            token = self._access_token
        if token:
            try:
                requests.post(
                    f"{SUPABASE_URL}/auth/v1/logout",
                    headers=self._headers(with_auth=True),
                    timeout=_HTTP_TIMEOUT,
                )
            except requests.RequestException:
                pass
        self._clear_local()
        self._notify()

    def _clear_local(self):
        with self._lock:
            self._access_token = None
            self._access_expires_at = 0.0
            self._refresh_token = None
            self.user_email = None
            self.user_name = None
            self.user_id = None
            self.is_pro = False
            self.plan = None
            self.period_end = None
            self.cancel_at_period_end = False
            self.trial_available = False
            self.is_admin = False
        try:
            storage.write_secret(REFRESH_TOKEN_SECRET, "")  # delete from keyring
        except Exception:
            pass
