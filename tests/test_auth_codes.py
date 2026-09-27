import hashlib
import http.server
import socket
import threading
import time
import unittest
import urllib.parse
from unittest.mock import MagicMock, patch

import requests

import auth


def _resp(status, body, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = text
    return r


def _session(uid):
    return {"access_token": "at-" + uid, "refresh_token": "rt-" + uid, "expires_in": 3600,
            "user": {"id": uid, "email": uid.lower() + "@example.com"}}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _redirect(port, query):
    """What the browser does after Google: GET the loopback callback."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"GET {auth.REDIRECT_PATH}?{query} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return data
            data += chunk


class TestAuthErrorCodes(unittest.TestCase):
    """login_failed analytics get a fixed code, never the server's message
    (which can quote the email address back)."""

    def test_gotrue_error_code_is_used(self):
        code = auth.AuthManager._error_code(_resp(400, {
            "error_code": "email_address_invalid",
            "msg": 'Email address "jane@acme-corp.io" is invalid'}))
        self.assertEqual(code, "email_address_invalid")

    def test_free_text_never_becomes_the_code(self):
        code = auth.AuthManager._error_code(_resp(422, {"error": 'bad "jane@acme.io"'}))
        self.assertEqual(code, "http_422")

    def test_unparseable_body(self):
        r = _resp(500, None)
        r.json.side_effect = ValueError
        self.assertEqual(auth.AuthManager._error_code(r), "http_500")


class _AuthCase(unittest.TestCase):
    """Keyring, browser and network all stubbed: nothing here may touch the
    real Credential Manager or Supabase. requests.post pops self.responses in
    order (a response, an exception to raise, or a fn(url, kwargs))."""

    def setUp(self):
        self.keyring = {}
        self.posts = []
        self.responses = []

        def post(url, **kw):
            self.posts.append(url)
            if not self.responses:
                raise requests.ConnectionError("offline (no scripted response)")
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            if not isinstance(r, MagicMock) and callable(r):
                return r(url, kw)
            return r

        for p in (
            patch.object(auth.storage, "read_secret", side_effect=lambda n: self.keyring.get(n, "")),
            patch.object(auth.storage, "write_secret", side_effect=self.keyring.__setitem__),
            patch.object(auth.requests, "post", side_effect=post),
            patch.object(auth.webbrowser, "open", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)

    def manager(self, **kw):
        m = auth.AuthManager(**kw)
        self.addCleanup(self._stop_retry, m)  # runs before the patches are undone
        return m

    @staticmethod
    def _stop_retry(m):
        m._stop_session_retry()
        t = m._retry_thread
        if t is not None:
            t.join(5)

    def signed_in(self, uid="X", **kw):
        """A manager whose access token has expired, so the next call refreshes."""
        m = self.manager(**kw)
        m._refresh_token = "rt-" + uid
        m.user_id = uid
        self.keyring[auth.REFRESH_TOKEN_SECRET] = "rt-" + uid
        return m


class TestRefreshFailures(_AuthCase):
    """Only GoTrue saying the refresh token is dead may sign the user out; a
    Supabase incident (5xx/429) used to delete the keyring token."""

    def test_transient_statuses_keep_the_session(self):
        for status in (429, 500, 502, 503, 504, 522):
            with self.subTest(status=status):
                notified = []
                m = self.signed_in(on_state_changed=lambda: notified.append(1))
                self.responses = [_resp(status, {"message": "upstream unavailable"})]
                self.assertFalse(m._refresh_access_token())
                self.assertTrue(m.is_authenticated)
                self.assertEqual(self.keyring[auth.REFRESH_TOKEN_SECRET], "rt-X")
                self.assertEqual(notified, [])

    def test_400_401_that_are_not_about_the_token_keep_it(self):
        for status, body in ((400, {"error_code": "validation_failed", "msg": "bad json"}),
                             (401, {"message": "Invalid API key"})):
            with self.subTest(status=status):
                m = self.signed_in()
                self.responses = [_resp(status, body)]
                self.assertFalse(m._refresh_access_token())
                self.assertTrue(m.is_authenticated)
                self.assertEqual(self.keyring[auth.REFRESH_TOKEN_SECRET], "rt-X")

    def test_dead_refresh_token_signs_out(self):
        unparseable = _resp(400, None, text="Invalid Refresh Token: Already Used")
        unparseable.json.side_effect = ValueError
        cases = [
            _resp(400, {"code": 400, "error_code": "refresh_token_not_found",
                        "msg": "Invalid Refresh Token: Refresh Token Not Found"}),
            _resp(400, {"error_code": "refresh_token_already_used"}),
            _resp(401, {"error_code": "session_not_found"}),
            _resp(400, {"error": "invalid_grant",
                        "error_description": "Invalid Refresh Token: Revoked"}),
            unparseable,
        ]
        for r in cases:
            with self.subTest(status=r.status_code, body=r.json.return_value):
                notified = []
                m = self.signed_in(on_state_changed=lambda: notified.append(1))
                self.responses = [r]
                self.assertFalse(m._refresh_access_token())
                self.assertFalse(m.is_authenticated)
                self.assertIsNone(m.user_id)
                self.assertEqual(self.keyring[auth.REFRESH_TOKEN_SECRET], "")
                self.assertEqual(notified, [1])

    def test_rejection_of_an_already_rotated_token_is_ignored(self):
        m = self.signed_in()

        def rotated_meanwhile(url, kw):
            # Another thread's refresh won the race and rotated the token.
            m._refresh_token = "rt-new"
            return _resp(400, {"error_code": "refresh_token_already_used"})

        self.responses = [rotated_meanwhile]
        m._refresh_access_token()
        self.assertTrue(m.is_authenticated)
        self.assertEqual(m._refresh_token, "rt-new")
        self.assertEqual(self.keyring[auth.REFRESH_TOKEN_SECRET], "rt-X")

    def test_refresh_racing_a_sign_out_does_not_resurrect_it(self):
        m = self.signed_in()

        def signed_out_meanwhile(url, kw):
            m._clear_local()
            return _resp(200, _session("X"))

        self.responses = [signed_out_meanwhile]
        self.assertFalse(m._refresh_access_token())
        self.assertFalse(m.is_authenticated)
        self.assertEqual(self.keyring[auth.REFRESH_TOKEN_SECRET], "")


class TestEntitlementKnown(_AuthCase):
    """entitlement_known must mean "the server answered for THIS user"."""

    def test_signed_out_refresh_is_not_a_server_answer(self):
        m = self.manager()
        m._set_entitlement(True, "annual", "2027-01-01", False, False, True)
        self.assertFalse(m.refresh_entitlement())
        self.assertFalse(m.is_pro)
        self.assertIsNone(m.plan)
        self.assertFalse(m.is_admin)
        self.assertFalse(m.entitlement_known)

    def test_revoked_then_new_sign_in_with_entitlement_outage(self):
        m = self.signed_in()
        m._set_entitlement(False, None, None, False)  # X: a real "free" answer
        # Upgrade-watch tick: the access token expired and the refresh is revoked.
        self.responses = [_resp(400, {"error": "invalid_grant"})]
        m.refresh_entitlement()
        self.assertFalse(m.is_authenticated)
        self.assertFalse(m.entitlement_known)
        # Signing in again while my_entitlement is down: unknown, not "free".
        self.responses = [_resp(200, _session("X")), _resp(503, None)]
        self.assertEqual(m.sign_in_email("x@example.com", "pw"), ("ok", ""))
        self.assertFalse(m.is_pro)
        self.assertFalse(m.entitlement_known)

    def test_account_switch_drops_the_previous_entitlement(self):
        m = self.manager()
        m._apply_session(_session("B"))
        m._set_entitlement(True, "annual", "2027-01-01", True, False, True)
        m._apply_session(_session("A"))
        self.assertEqual(m.user_id, "A")
        self.assertFalse(m.is_pro)
        self.assertIsNone(m.plan)
        self.assertIsNone(m.period_end)
        self.assertFalse(m.cancel_at_period_end)
        self.assertFalse(m.is_admin)
        self.assertFalse(m.entitlement_known)

    def test_same_account_token_refresh_keeps_it(self):
        m = self.manager()
        m._apply_session(_session("B"))
        m._set_entitlement(True, "annual", None, False)
        m._apply_session(_session("B"))
        self.assertTrue(m.is_pro)
        self.assertEqual(m.plan, "annual")
        self.assertTrue(m.entitlement_known)


class TestSessionRestoreRetry(_AuthCase):
    """Offline at launch left a signed-in user with user_id None for the whole
    run; the restore now retries in the background."""

    def setUp(self):
        super().setUp()
        for name, value in (("_SESSION_RETRY_DELAYS", (0.01, 0.01, 0.01)),
                            ("_SESSION_RETRY_EVERY", 0.01)):
            p = patch.object(auth, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.keyring[auth.REFRESH_TOKEN_SECRET] = "rt-X"

    def test_offline_launch_recovers_in_background(self):
        seen = []
        m = self.manager(on_state_changed=lambda: seen.append((m.user_id, m.is_pro)))
        self.responses = [requests.ConnectionError("offline"),
                          _resp(503, {"message": "upstream unavailable"}),
                          _resp(200, _session("X")),
                          _resp(200, [{"is_pro": True, "plan": "annual"}])]
        self.assertFalse(m.load_session())
        self.assertTrue(m.is_authenticated)
        m._retry_thread.join(5)
        self.assertFalse(m._retry_thread.is_alive())
        self.assertEqual(m.user_id, "X")
        self.assertTrue(m.is_pro)
        self.assertTrue(m.entitlement_known)
        self.assertEqual(seen[-1], ("X", True))
        self.assertIsNone(m._retry_stop)

    def test_one_retry_thread_and_sign_out_stops_it(self):
        with patch.object(auth, "_SESSION_RETRY_DELAYS", (30,)):
            m = self.manager()
            self.responses = [requests.ConnectionError("offline")]
            self.assertFalse(m.load_session())
            first = m._retry_thread
            self.assertTrue(first.is_alive())
            m._start_session_retry()
            m.load_session()  # second restore attempt, still offline
            self.assertIs(m._retry_thread, first)
            live = [t for t in threading.enumerate() if t.name == "auth-session-retry"]
            self.assertEqual(len(live), 1)
            posts = len(self.posts)
            m.sign_out()
            first.join(2)
            self.assertFalse(first.is_alive())
            self.assertEqual(len(self.posts), posts)  # no refresh after sign-out

    def test_retry_stops_when_the_token_is_rejected(self):
        m = self.manager()
        self.responses = [requests.ConnectionError("offline"),
                          _resp(400, {"error_code": "refresh_token_not_found"})]
        m.load_session()
        m._retry_thread.join(5)
        self.assertFalse(m._retry_thread.is_alive())
        self.assertFalse(m.is_authenticated)
        self.assertEqual(self.keyring[auth.REFRESH_TOKEN_SECRET], "")
        self.assertEqual(len(self.posts), 2)

    def test_no_retry_after_a_clean_restore_or_a_rejection(self):
        m = self.manager()
        self.responses = [_resp(200, _session("X")), _resp(200, [])]
        self.assertTrue(m.load_session())
        self.assertIsNone(m._retry_thread)

        m2 = self.manager()
        self.keyring[auth.REFRESH_TOKEN_SECRET] = "rt-X"
        self.responses = [_resp(400, {"error": "invalid_grant"})]
        self.assertFalse(m2.load_session())
        self.assertFalse(m2.is_authenticated)
        self.assertIsNone(m2._retry_thread)


class TestGoogleSignInRetry(_AuthCase):
    """A retry within the login window used to bind the port next to the
    still-waiting first attempt (SO_REUSEADDR on Windows), so the redirect
    could land on the stale listener with the wrong PKCE verifier."""

    def setUp(self):
        super().setUp()
        self.port = _free_port()
        self.challenges = []
        self.opened = threading.Semaphore(0)

        def browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.challenges.append(q["code_challenge"][0])
            self.opened.release()
            return True

        auth.webbrowser.open.side_effect = browser
        for name, value in (("REDIRECT_PORT", self.port), ("_LOGIN_TIMEOUT", 15)):
            p = patch.object(auth, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _exchange_for(self, challenge_index, uid):
        def exchange(url, kw):
            v = kw["json"]["code_verifier"]
            ch = auth._b64url(hashlib.sha256(v.encode("ascii")).digest())
            if ch == self.challenges[challenge_index]:
                return _resp(200, _session(uid))
            return _resp(400, {"error_code": "bad_code_verifier"})
        return exchange

    def _start(self, m, results, name):
        def run():
            t0 = time.monotonic()
            ok = m.sign_in_with_google()
            results[name] = (ok, m.last_error, time.monotonic() - t0)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        return t

    def test_retry_supersedes_the_waiting_attempt(self):
        m = self.manager()
        results = {}
        t1 = self._start(m, results, "first")
        self.assertTrue(self.opened.acquire(timeout=5))
        t2 = self._start(m, results, "retry")
        self.assertTrue(self.opened.acquire(timeout=5))
        t1.join(5)
        ok, reason, elapsed = results["first"]
        self.assertFalse(ok)
        self.assertEqual(reason, "superseded")
        self.assertLess(elapsed, 5)  # stopped, not left waiting out _LOGIN_TIMEOUT

        self.responses = [self._exchange_for(1, "G"), _resp(200, [])]
        page = _redirect(self.port, "code=CODE2&state=x")
        self.assertIn(b"signed in", page)
        t2.join(5)
        self.assertEqual(results["retry"][:2], (True, ""))
        self.assertEqual(m.user_id, "G")
        self.assertTrue(m.is_authenticated)

    def test_port_held_by_another_listener_fails_fast(self):
        # A stock (SO_REUSEADDR) listener, e.g. an older build still waiting.
        other = http.server.HTTPServer(("127.0.0.1", self.port), http.server.BaseHTTPRequestHandler)
        self.addCleanup(other.server_close)
        m = self.manager()
        t0 = time.monotonic()
        self.assertFalse(m.sign_in_with_google())
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(m.last_error, "port_busy")
        auth.webbrowser.open.assert_not_called()

    def test_loopback_listener_cannot_be_bound_twice(self):
        first = auth._LoopbackServer(("127.0.0.1", self.port), auth._CallbackHandler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            auth._LoopbackServer(("127.0.0.1", self.port), auth._CallbackHandler).server_close()

    def test_times_out_without_a_redirect(self):
        with patch.object(auth, "_LOGIN_TIMEOUT", 1):
            m = self.manager()
            t0 = time.monotonic()
            self.assertFalse(m.sign_in_with_google())
            self.assertLess(time.monotonic() - t0, 4)
        self.assertEqual(m.last_error, "timeout")
        # The port is free again for the next attempt.
        auth._LoopbackServer(("127.0.0.1", self.port), auth._CallbackHandler).server_close()


class TestLaunchAction(unittest.TestCase):
    def test_second_launch_actions(self):
        import main
        self.assertEqual(main.launch_action(["main.py"]), "show_settings")
        self.assertEqual(main.launch_action(["main.py", "show_meeting"]), "show_meeting")
        self.assertEqual(main.launch_action(["main.py", "live_prompter"]), "live_prompter")
        # The Windows-startup shortcut while the app already runs: stay quiet.
        self.assertIsNone(main.launch_action(["main.py", "--background"]))


if __name__ == "__main__":
    unittest.main()
