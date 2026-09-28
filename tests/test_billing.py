"""Manage subscription -> Stripe Customer Portal (main.billing_portal_session
and AppController.open_billing): the URL check, failures, the fallbacks."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _resp(status, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = text
    return r


class TestBillingPortalSession(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main

    def test_no_token_means_signed_out(self):
        post = MagicMock()
        self.assertEqual(self.main.billing_portal_session(None, post=post), (None, "signed_out"))
        post.assert_not_called()

    def test_returns_the_portal_url_for_the_signed_in_user(self):
        url = "https://billing.stripe.com/p/session/test_123"
        post = MagicMock(return_value=_resp(200, {"url": url}))
        self.assertEqual(self.main.billing_portal_session("tok", post=post), (url, ""))
        args, kwargs = post.call_args
        self.assertEqual(args[0], self.main.BILLING_PORTAL_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")

    def test_never_opens_anything_but_stripe(self):
        post = MagicMock(return_value=_resp(200, {"url": "https://evil.example/billing"}))
        self.assertEqual(self.main.billing_portal_session("tok", post=post), (None, "unavailable"))
        post = MagicMock(return_value=_resp(200, None))
        self.assertEqual(self.main.billing_portal_session("tok", post=post), (None, "unavailable"))

    def test_status_codes_map_to_problems(self):
        for status, problem in ((401, "signed_out"), (404, "no_customer"),
                                (502, "unavailable"), (503, "unavailable")):
            post = MagicMock(return_value=_resp(status, {"error": "x"}))
            self.assertEqual(self.main.billing_portal_session("tok", post=post),
                             (None, problem), status)

    def test_network_error_is_unavailable(self):
        post = MagicMock(side_effect=OSError("offline"))
        self.assertEqual(self.main.billing_portal_session("tok", post=post), (None, "unavailable"))


class TestOpenBilling(unittest.TestCase):
    def _app(self, authed=True):
        return SimpleNamespace(
            auth=SimpleNamespace(is_authenticated=authed, get_access_token=lambda: "tok",
                                 user_email="a@b.c"),
            show_auth_gate=MagicMock(), _billing_problem=MagicMock(),
            overlay=SimpleNamespace(call_soon=MagicMock()), _billing_busy=False)

    def test_signed_out_goes_to_sign_in(self):
        import main
        me = self._app(authed=False)
        with patch.object(main.threading, "Thread") as th:
            main.AppController.open_billing(me)
        me.show_auth_gate.assert_called_once()
        th.assert_not_called()

    def test_a_second_click_while_opening_is_ignored(self):
        import main
        me = self._app()
        me._billing_busy = True
        with patch.object(main.threading, "Thread") as th:
            main.AppController.open_billing(me)
        th.assert_not_called()

    def test_opens_the_portal(self):
        import main
        me = self._app()
        url = "https://billing.stripe.com/p/session/x"
        with patch.object(main, "billing_portal_session", return_value=(url, "")), \
             patch.object(main.webbrowser, "open") as op:
            main.AppController._open_billing_worker(me)
        op.assert_called_once_with(url)
        me.overlay.call_soon.assert_not_called()
        self.assertFalse(me._billing_busy)

    def test_unmatched_account_falls_back_to_the_login_link(self):
        import main
        me = self._app()
        link = "https://billing.stripe.com/p/login/abc"
        with patch.object(main, "billing_portal_session", return_value=(None, "no_customer")), \
             patch.object(main, "STRIPE_PORTAL_URL", link), \
             patch.object(main.webbrowser, "open") as op:
            main.AppController._open_billing_worker(me)
        op.assert_called_once_with(link)

    def test_explains_when_nothing_can_open(self):
        import main
        me = self._app()
        with patch.object(main, "billing_portal_session", return_value=(None, "no_customer")), \
             patch.object(main, "STRIPE_PORTAL_URL", ""), \
             patch.object(main.webbrowser, "open") as op:
            main.AppController._open_billing_worker(me)
        op.assert_not_called()
        me.overlay.call_soon.assert_called_once_with(me._billing_problem, "no_customer")
        self.assertFalse(me._billing_busy)

    def test_an_expired_session_is_not_sent_to_the_login_link(self):
        import main
        me = self._app()
        with patch.object(main, "billing_portal_session", return_value=(None, "signed_out")), \
             patch.object(main, "STRIPE_PORTAL_URL", "https://billing.stripe.com/p/login/abc"), \
             patch.object(main.webbrowser, "open") as op:
            main.AppController._open_billing_worker(me)
        op.assert_not_called()
        me.overlay.call_soon.assert_called_once_with(me._billing_problem, "signed_out")


if __name__ == "__main__":
    unittest.main()
