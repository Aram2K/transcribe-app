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
import re
import secrets
import socket
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
_LOGIN_POLL = 0.5     # handle_request() slice, so a superseded sign-in stops quickly

# Restoring a saved session that failed transiently (offline at launch, a
# Supabase 5xx/429): retry after these delays, then every _SESSION_RETRY_EVERY.
_SESSION_RETRY_DELAYS = (15, 30, 60, 120)
_SESSION_RETRY_EVERY = 300

# GoTrue's answers that mean the refresh token itself is dead (revoked, rotated
# away, session deleted) - the only refresh failures that sign the user out.
_REFRESH_TOKEN_DEAD = frozenset({
    "refresh_token_not_found",
    "refresh_token_already_used",
    "session_not_found",
    "session_expired",
    "invalid_grant",
})

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
    # A browser preconnect that never sends a request must not wedge
    # handle_request() past the login deadline or a newer attempt's stop.
    timeout = 10

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


class _LoopbackServer(http.server.HTTPServer):
    """The OAuth redirect listener, bound exclusively.

    Stock HTTPServer sets SO_REUSEADDR, which on Windows lets a second socket
    bind a port that is still listening: a retried sign-in bound "fine" while
    the browser's redirect went to the stale listener (wrong PKCE verifier -
    the page said signed in, the app wasn't). SO_EXCLUSIVEADDRUSE makes a real
    conflict fail at bind instead. On POSIX SO_REUSEADDR never shares a
    listening port - it only skips TIME_WAIT from the last served redirect,
    which a quick retry needs - so it stays on there."""

    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class _LoginFlow:
    """One Google sign-in attempt, so a retry can stop the previous attempt's
    listener instead of racing it for the redirect."""

    def __init__(self):
        self.cancel = threading.Event()  # a newer attempt took over
        self.closed = threading.Event()  # this attempt no longer holds the port
        self.server = None


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
        self.last_error = ""           # why the last Google sign-in failed (for analytics)
        self.entitlement_known = False  # the server has answered: is_pro is real, not a default

        self._login_flow = None   # the Google sign-in still waiting for its redirect
        self._retry_stop = None   # set -> stops the background session-restore retry
        self._retry_thread = None

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
            if not self.refresh_entitlement():
                self._start_session_retry()
        elif self.is_authenticated:
            # Offline at launch or a Supabase hiccup: the token was kept, but
            # until a refresh lands the user is signed in with no user_id or
            # entitlement - keep trying in the background.
            self._start_session_retry()
        self._notify()
        return ok

    def _start_session_retry(self):
        """Retry the session restore on a daemon thread (at most one)."""
        with self._lock:
            if self._retry_stop is not None:
                return
            stop = threading.Event()
            self._retry_stop = stop
            self._retry_thread = threading.Thread(
                target=self._session_retry_loop, args=(stop,),
                name="auth-session-retry", daemon=True)
            self._retry_thread.start()

    def _stop_session_retry(self):
        with self._lock:
            stop, self._retry_stop = self._retry_stop, None
        if stop is not None:
            stop.set()

    def _session_retry_loop(self, stop):
        attempt = 0
        try:
            while True:
                delays = _SESSION_RETRY_DELAYS
                delay = delays[attempt] if attempt < len(delays) else _SESSION_RETRY_EVERY
                attempt += 1
                if stop.wait(delay) or not self.is_authenticated:
                    return
                # get_access_token() also covers another caller having
                # refreshed meanwhile - that path never loaded the entitlement.
                if self.get_access_token():
                    if stop.is_set():
                        return
                    # user_id is known now: the per-user keys can load.
                    self._notify()
                    if self.refresh_entitlement():
                        logger.info("Session restored after %d retries", attempt)
                        return
                    continue    # the entitlement check failed: keep trying
                if not self.is_authenticated:
                    return  # the server rejected the token (already cleared)
        except Exception:
            logger.debug("session restore retry failed", exc_info=True)
        finally:
            with self._lock:
                if self._retry_stop is stop:
                    self._retry_stop = None

    # ── Google login via system browser + loopback (call in a thread) ────────
    def sign_in_with_google(self):
        """Run the full PKCE OAuth flow. Returns True on success.

        Blocking - call from a worker thread. The browser opens; we wait for the
        loopback redirect, exchange the code, persist the refresh token, and
        load entitlement. Starting a new attempt stops one still waiting."""
        self.last_error = ""
        flow = _LoginFlow()
        with self._lock:
            prev, self._login_flow = self._login_flow, flow
        if prev is not None:
            self._stop_login_flow(prev)
        try:
            return self._google_flow(flow)
        finally:
            flow.closed.set()  # also covers the paths that never bound the port
            with self._lock:
                if self._login_flow is flow:
                    self._login_flow = None

    @staticmethod
    def _stop_login_flow(flow):
        """Make an older attempt release the loopback port, or the browser's
        redirect for the new attempt could land on it (wrong PKCE verifier)."""
        flow.cancel.set()
        if not flow.closed.wait(_LOGIN_POLL * 4):
            server = flow.server  # stuck somewhere: free the port ourselves
            if server is not None:
                try:
                    server.server_close()
                except Exception:
                    pass

    def _google_flow(self, flow):
        if flow.cancel.is_set():  # an even newer attempt started meanwhile
            self.last_error = "superseded"
            return False
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
            server = _LoopbackServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
        except OSError as e:
            logger.error("Could not bind loopback port %d: %s", REDIRECT_PORT, e)
            self.last_error = "port_busy"
            return False
        server.timeout = _LOGIN_POLL
        server.auth_code = None
        server.auth_state = None
        server.auth_error = None
        flow.server = server

        try:
            if flow.cancel.is_set():
                return False    # a newer attempt started meanwhile (finally cleans up)
            webbrowser.open(authorize_url)
            # Wait (bounded) for the single callback request.
            deadline = time.monotonic() + _LOGIN_TIMEOUT
            while (server.auth_code is None and server.auth_error is None
                   and not flow.cancel.is_set() and time.monotonic() < deadline):
                try:
                    server.handle_request()
                except (OSError, ValueError):
                    break  # the listener was closed under us by a newer attempt
        finally:
            try:
                server.server_close()
            except Exception:
                pass
            if server.auth_code is None and flow.cancel.is_set():
                # Before signalling the port free: from then on the newer
                # attempt owns last_error.
                self.last_error = "superseded"
            flow.closed.set()

        if server.auth_code is None and flow.cancel.is_set():
            logger.info("Google sign-in superseded by a newer attempt")
            return False
        if server.auth_error:
            logger.warning("OAuth returned error: %s", server.auth_error)
            self.last_error = "oauth_error"
            return False
        if not server.auth_code:
            logger.warning("OAuth timed out with no code")
            self.last_error = "timeout"
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
            self.last_error = "network"
            return False
        if resp.status_code != 200:
            logger.error("PKCE exchange HTTP %s: %s", resp.status_code, resp.text[:200])
            self.last_error = f"exchange_http_{resp.status_code}"
            return False
        self._apply_session(resp.json())
        self.last_error = ""  # clear a superseded attempt's reason
        self.refresh_entitlement()
        self._notify()
        return True

    # ── Email / password ─────────────────────────────────────────────────────
    def sign_in_email(self, email, password):
        """Sign in with email + password.
        Returns (status, message): ("ok", "") or ("error", <message>)."""
        self.last_error = ""
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                headers=self._headers(),
                json={"email": email, "password": password},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            self.last_error = "network"
            return ("error", "Network error - check your connection and try again.")
        if resp.status_code == 200:
            self._apply_session(resp.json())
            self.refresh_entitlement()
            self._notify()
            return ("ok", "")
        self.last_error = self._error_code(resp)
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
        self.last_error = ""
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
            self.last_error = "network"
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
        self.last_error = self._error_code(resp)
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
    def _error_code(resp):
        """GoTrue's machine-readable reason (e.g. "email_address_invalid",
        "user_already_exists") for analytics - never the human message, which
        can quote the email address back."""
        code = ""
        try:
            data = resp.json() or {}
            code = str(data.get("error_code") or data.get("error") or "")
        except Exception:
            pass
        if re.fullmatch(r"[a-z0-9_]{1,40}", code):
            return code
        return f"http_{resp.status_code}"

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
            uid = user.get("id")
            if uid != self.user_id:
                # Another account (or the first real answer after an offline
                # restore): the last entitlement isn't this user's - it is
                # unknown until refresh_entitlement() asks the server.
                self._reset_entitlement()
            self.user_id = uid
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
            if not self._refresh_token_rejected(resp):
                # Supabase down or throttling (5xx/429/...): the token is still
                # good - signing out here deleted it from the keyring.
                logger.warning("Token refresh HTTP %s - keeping session", resp.status_code)
                return False
            with self._lock:
                if self._refresh_token != rt:
                    # A concurrent refresh already rotated it (or the user
                    # signed out): this rejection is about the old token.
                    return self._has_fresh_token()
                logger.warning("Token refresh HTTP %s: refresh token rejected - clearing session",
                               resp.status_code)
                self._clear_local()
            self._notify()
            return False
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or not data.get("access_token"):
            logger.warning("Token refresh returned no session (captive portal?) - keeping session")
            return False
        with self._lock:
            if self._refresh_token != rt:
                # Signed out (or into another account) while this was in
                # flight: don't resurrect the old session.
                return self._has_fresh_token()
            self._apply_session(data)
        return True

    @staticmethod
    def _refresh_token_rejected(resp):
        """True only when GoTrue says the refresh token is invalid (HTTP
        400/401 with one of _REFRESH_TOKEN_DEAD, or "Invalid Refresh Token")."""
        if resp.status_code not in (400, 401):
            return False
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            for key in ("error_code", "error"):
                if str(data.get(key) or "") in _REFRESH_TOKEN_DEAD:
                    return True
        try:
            text = str(resp.text or "")
        except Exception:
            text = ""
        return "invalid refresh token" in text.lower()

    def _has_fresh_token(self):
        with self._lock:
            return bool(self._refresh_token and self._access_token
                        and time.time() < self._access_expires_at)

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
        asked_for = self.user_id
        if not token:
            # Signed out (or the session was revoked): not Pro - but no server
            # answer either, so not "known" (a stale known=True carried into
            # the next sign-in). Still signed in but offline: the answer is
            # unknown, so leave the last one alone rather than reporting a
            # Pro user as free.
            if not self.is_authenticated:
                self._reset_entitlement()
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
        try:
            rows = resp.json() or []
        except ValueError:
            logger.warning("Entitlement returned no JSON")
            return False
        if not isinstance(rows, list):
            return False
        if not self.is_authenticated or self.user_id != asked_for:
            # Signed out, or into another account, while this was in flight.
            return False
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
            self.entitlement_known = True

    def _reset_entitlement(self):
        """Back to "not Pro, unknown": no server answer for this user yet."""
        with self._lock:
            self.is_pro = False
            self.plan = None
            self.period_end = None
            self.cancel_at_period_end = False
            self.trial_available = False
            self.is_admin = False
            self.entitlement_known = False

    # ── sign out ─────────────────────────────────────────────────────────────
    def sign_out(self):
        self._stop_session_retry()  # before the (slow) logout call can race it
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
        self._stop_session_retry()  # signed out / revoked: nothing to restore
        with self._lock:
            self._access_token = None
            self._access_expires_at = 0.0
            self._refresh_token = None
            self.user_email = None
            self.user_name = None
            self.user_id = None
            self._reset_entitlement()
        try:
            storage.write_secret(REFRESH_TOKEN_SECRET, "")  # delete from keyring
        except Exception:
            pass
