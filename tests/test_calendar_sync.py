import http.client
import threading
import unittest
from unittest.mock import MagicMock, patch

import calendar_sync


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


class TestCalendarRefresh(unittest.TestCase):
    CFG = {"google_oauth_client_id": "cid", "google_oauth_client_secret": "sec"}

    def setUp(self):
        with calendar_sync._token_lock:
            calendar_sync._token.update(value="", expires=0.0)

    def test_only_invalid_grant_forgets_the_calendar(self):
        with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
             patch.object(calendar_sync.storage, "write_secret") as write, \
             patch.object(calendar_sync.requests, "post",
                          return_value=_resp(400, {"error": "invalid_grant"})):
            with self.assertRaises(calendar_sync.CalendarAuthError):
                calendar_sync._access_token(self.CFG)
        write.assert_called_once_with(calendar_sync.REFRESH_SECRET, "")

    def test_a_misconfigured_client_keeps_the_users_grant(self):
        for body in ({"error": "invalid_client"}, {"error": "invalid_request"}):
            with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
                 patch.object(calendar_sync.storage, "write_secret") as write, \
                 patch.object(calendar_sync.requests, "post", return_value=_resp(401, body)):
                with self.assertRaises(calendar_sync.CalendarError) as ctx:
                    calendar_sync._access_token(self.CFG)
            self.assertNotIsInstance(ctx.exception, calendar_sync.CalendarAuthError)
            write.assert_not_called()

    def test_no_client_id_never_calls_google(self):
        with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
             patch.object(calendar_sync, "GOOGLE_CLIENT_ID", ""), \
             patch.object(calendar_sync.requests, "post") as post:
            with self.assertRaises(calendar_sync.CalendarError):
                calendar_sync._access_token({})
        post.assert_not_called()


class TestCalendarConnect(unittest.TestCase):
    CFG = {"google_oauth_client_id": "cid"}

    def _run_connect(self, hits, token_response=None, write_ok=True, timeout=5):
        """Run connect() and replay ``hits`` (query strings; "{state}" is
        replaced with the real state) against its loopback listener."""
        result = {}

        def fake_browser(url):
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            port = int(q["redirect_uri"][0].rsplit(":", 1)[1])
            state = q["state"][0]

            def hit_all():
                for h in hits:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/?" + h.replace("{state}", state))
                    result.setdefault("codes", []).append(conn.getresponse().status)
                    conn.close()
            t = threading.Thread(target=hit_all, daemon=True)
            result["thread"] = t
            t.start()
            return True

        tok = token_response or _resp(200, {"access_token": "at", "refresh_token": "rt",
                                            "scope": "openid calendar.events.readonly",
                                            "expires_in": 3600})
        with patch.object(calendar_sync.requests, "post", return_value=tok) as post, \
             patch.object(calendar_sync.storage, "write_secret", return_value=write_ok):
            try:
                calendar_sync.connect(self.CFG, open_browser=fake_browser, timeout=timeout)
                result["ok"] = True
            except calendar_sync.CalendarError as e:
                result["error"] = str(e)
            result["post"] = post
        if "thread" in result:
            result["thread"].join(5)   # the last response may still be in flight
        return result

    def test_a_forged_error_without_our_state_does_not_end_the_sign_in(self):
        res = self._run_connect(["error=access_denied", "code=abc&state={state}"])
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["codes"], [400, 200])

    def test_connect_fails_when_the_token_cannot_be_kept(self):
        res = self._run_connect(["code=abc&state={state}"], write_ok=False)
        self.assertIn("keyring", res.get("error", ""))
        revoked = [c for c in res["post"].call_args_list if c.args[0] == calendar_sync.REVOKE_URL]
        self.assertEqual(len(revoked), 1)

    def test_an_idle_connection_cannot_pin_the_sign_in(self):
        import socket
        import time

        def idle_browser(url):
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            port = int(q["redirect_uri"][0].rsplit(":", 1)[1])
            s = socket.create_connection(("127.0.0.1", port))
            self.addCleanup(s.close)   # connects, never sends
            return True

        with patch.object(calendar_sync._Callback, "timeout", 0.5):
            start = time.monotonic()
            with self.assertRaises(calendar_sync.CalendarError):
                calendar_sync.connect(self.CFG, open_browser=idle_browser, timeout=1)
            self.assertLess(time.monotonic() - start, 4)


if __name__ == "__main__":
    unittest.main()
