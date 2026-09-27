import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import storage
import telemetry

ROOT = Path(__file__).resolve().parent.parent
EDGE_FUNCTION = ROOT / "supabase" / "functions" / "transcribe-analytics" / "index.ts"
CONFIG = {"analytics_enabled": True, "analytics_endpoint": "https://example.com/events"}


def _ts_set(name):
    """String entries of `const <name> = new Set([...])` in the edge function."""
    src = EDGE_FUNCTION.read_text(encoding="utf-8")
    block = re.search(r"const %s = new Set\(\[(.*?)\]\)" % name, src, re.S)
    return set(re.findall(r"'([^']+)'", block.group(1)))


def _event(name="app_started"):
    return {"schema": 2, "event_id": uuid.uuid4().hex, "event": name,
            "timestamp": 1, "install_id": "i", "session_id": "s", "props": {}}


class TestTelemetry(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(telemetry, "_install_id", return_value="test-install")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_privacy_mode_does_not_disable_analytics(self):
        # Analytics is independent of Privacy Mode - the sanitizer already
        # strips all sensitive props, so privacy-mode users can still
        # share usage events when they want to.
        config = {
            "analytics_enabled": True,
            "privacy_mode": True,
            "analytics_endpoint": "https://example.com/events",
        }
        self.assertTrue(telemetry.enabled(config))

    def test_analytics_disabled_when_checkbox_off(self):
        config = {
            "analytics_enabled": False,
            "privacy_mode": False,
            "analytics_endpoint": "https://example.com/events",
        }
        self.assertFalse(telemetry.enabled(config))

    def test_analytics_disabled_without_endpoint(self):
        config = {
            "analytics_enabled": True,
            "privacy_mode": False,
            "analytics_endpoint": "",
        }
        self.assertFalse(telemetry.enabled(config))

    def test_sanitizer_drops_sensitive_keys(self):
        out = telemetry._sanitize({
            "transcript": "hello private text",
            "clipboard": "secret",
            "google_api_key": "secret-key",
            "action_api_key": "action-secret",
            "backend": "local",
            "count": 2,
        })
        self.assertNotIn("transcript", out)
        self.assertNotIn("clipboard", out)
        self.assertNotIn("google_api_key", out)
        self.assertNotIn("action_api_key", out)
        self.assertEqual(out["backend"], "local")
        self.assertEqual(out["count"], 2)

    def test_track_ignores_unknown_events(self):
        config = {
            "analytics_enabled": True,
            "privacy_mode": False,
            "analytics_endpoint": "https://example.com/events",
        }
        with patch.object(telemetry, "_append") as append, \
             patch.object(telemetry, "flush_async") as flush:
            telemetry.track("raw_transcript_saved", {"text": "private"}, config, "1.0.0")
        append.assert_not_called()
        flush.assert_not_called()

    def test_sanitizer_drops_identity_keys(self):
        out = telemetry._sanitize({"email": "a@b.c", "full_name": "A", "title": "Board", "plan": "annual"})
        self.assertEqual(out, {"plan": "annual"})

    def test_sanitizer_drops_values_that_quote_an_email(self):
        out = telemetry._sanitize({"reason": 'Email address "jane.doe@acme-corp.io" is invalid',
                                   "mode": "signup", "count": 3})
        self.assertEqual(out, {"mode": "signup", "count": 3})

    def test_server_drops_values_that_quote_an_email(self):
        src = EDGE_FUNCTION.read_text(encoding="utf-8")
        self.assertIn("emailPattern.test(value)", src)

    def test_track_never_raises(self):
        with patch.object(telemetry, "_append", side_effect=OSError("disk full")):
            telemetry.track("app_started", {}, CONFIG, "1.0.0")  # must not raise

    def test_context_provider_props_are_added_to_every_event(self):
        captured = []
        with patch.object(telemetry, "_append", side_effect=lambda item: captured.append(item) or 1), \
             patch.object(telemetry, "flush_async"), patch.object(telemetry, "_ensure_sender"):
            telemetry.set_context_provider(lambda: {"tier": "pro"})
            try:
                telemetry.track("app_started", {"backend": "local"}, CONFIG, "1.0.0")
                telemetry.set_context_provider(lambda: 1 / 0)   # a broken provider
                telemetry.track("app_started", {}, CONFIG, "1.0.0")
            finally:
                telemetry.set_context_provider(None)
        self.assertEqual(captured[0]["props"], {"tier": "pro", "backend": "local"})
        self.assertEqual(captured[1]["props"], {})
        self.assertTrue(captured[0]["event_id"])

    def test_first_event_uploads_at_once_then_batches(self):
        with patch.object(telemetry, "_drained_once", False), \
             patch.object(telemetry, "_ensure_sender") as sender, \
             patch.object(telemetry, "_wake") as wake, \
             patch.object(telemetry, "_append", side_effect=[1, 2, telemetry.FLUSH_THRESHOLD]):
            for _ in range(3):
                telemetry.track("app_started", {}, CONFIG, "1.0.0")
        # 1st event of the run drains the backlog; the 2nd waits for the timer;
        # the 3rd reaches the threshold.
        self.assertEqual(wake.set.call_count, 2)
        self.assertEqual(sender.call_count, 3)


class TestTelemetryQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(telemetry, "QUEUE_PATH", Path(self._tmp.name) / "queue.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_events_recorded_during_an_upload_are_kept(self):
        telemetry._append(_event("app_started"))
        late = _event("settings_opened")
        batches = []

        def fake_post(url, json=None, timeout=None):
            batches.append([e["event"] for e in json["events"]])
            if len(batches) == 1:
                telemetry._append(late)  # recorded while the first upload is in flight
            return SimpleNamespace(status_code=200)

        with patch.object(telemetry.requests, "post", side_effect=fake_post):
            self.assertTrue(telemetry._flush(CONFIG))
        self.assertEqual(batches, [["app_started"], ["settings_opened"]])
        self.assertEqual(telemetry._load_queue(), [])

    def test_uploads_in_batches_and_keeps_the_rest_on_failure(self):
        for _ in range(120):
            telemetry._append(_event())
        sizes = []

        def fake_post(url, json=None, timeout=None):
            sizes.append(len(json["events"]))
            if len(sizes) > 1:
                raise ConnectionError("offline")
            return SimpleNamespace(status_code=200)

        with patch.object(telemetry.requests, "post", side_effect=fake_post):
            self.assertFalse(telemetry._flush(CONFIG))
        self.assertEqual(sizes, [telemetry.BATCH_SIZE, telemetry.BATCH_SIZE])
        self.assertEqual(len(telemetry._load_queue()), 120 - telemetry.BATCH_SIZE)

    def test_server_error_keeps_the_batch(self):
        telemetry._append(_event())
        with patch.object(telemetry.requests, "post", return_value=SimpleNamespace(status_code=500)):
            self.assertFalse(telemetry._flush(CONFIG))
        self.assertEqual(len(telemetry._load_queue()), 1)

    def test_nothing_is_sent_when_analytics_is_off(self):
        telemetry._append(_event())
        with patch.object(telemetry.requests, "post") as post:
            self.assertTrue(telemetry._flush(dict(CONFIG, analytics_enabled=False)))
        post.assert_not_called()

    def test_events_queued_by_older_versions_get_stable_ids(self):
        legacy = [{"event": "app_started", "timestamp": 1, "install_id": "i", "session_id": "s"},
                  {"event": "app_started", "timestamp": 2, "install_id": "i", "session_id": "s"}]
        storage.atomic_write_json(telemetry.QUEUE_PATH, legacy)
        first = [e["event_id"] for e in telemetry._load_queue()]
        self.assertEqual(first, [e["event_id"] for e in telemetry._load_queue()])
        self.assertEqual(len(set(first)), 2)
        with patch.object(telemetry.requests, "post", return_value=SimpleNamespace(status_code=200)):
            self.assertTrue(telemetry._flush(CONFIG))
        self.assertEqual(telemetry._load_queue(), [])

    def test_queue_is_capped(self):
        with patch.object(telemetry, "MAX_QUEUE", 5):
            for i in range(8):
                pending = telemetry._append(dict(_event(), timestamp=i))
        self.assertEqual(pending, 5)
        self.assertEqual([e["timestamp"] for e in telemetry._load_queue()], [3, 4, 5, 6, 7])


class TestAnalyticsTier(unittest.TestCase):
    """Every event carries the account tier - never the account itself."""

    def _tier(self, **auth):
        import main
        me = SimpleNamespace(auth=SimpleNamespace(**auth))
        return main.AppController._analytics_context(me)["tier"]

    def test_tiers(self):
        base = dict(is_admin=False, is_pro=False, is_authenticated=True, entitlement_known=True)
        self.assertEqual(self._tier(**dict(base, is_admin=True, is_pro=True)), "admin")
        self.assertEqual(self._tier(**dict(base, is_pro=True)), "pro")
        self.assertEqual(self._tier(**base), "free")
        self.assertEqual(self._tier(**dict(base, is_authenticated=False)), "guest")

    def _activation(self, seen, now_pro, known=True, uid="u1"):
        import main
        tracked, saved = [], []
        me = SimpleNamespace(auth=SimpleNamespace(user_id=uid, plan="annual"),
                             cfg={"last_known_pro": dict(seen)} if seen is not None else {},
                             track=lambda e, p=None: tracked.append(e),
                             save_config=lambda: saved.append(True))
        main.AppController._track_pro_activation(me, now_pro, known)
        return tracked, me.cfg.get("last_known_pro"), bool(saved)

    def test_pro_activated_fires_once_when_a_free_account_turns_pro(self):
        tracked, seen, saved = self._activation({"u1": False}, True)
        self.assertEqual(tracked, ["pro_activated"])
        self.assertEqual(seen, {"u1": True})
        self.assertTrue(saved)
        tracked, _, saved = self._activation({"u1": True}, True)   # next launch
        self.assertEqual(tracked, [])
        self.assertFalse(saved)

    def test_existing_pro_accounts_never_count_as_activated(self):
        tracked, seen, _ = self._activation(None, True)
        self.assertEqual(tracked, [])
        self.assertEqual(seen, {"u1": True})

    def test_an_unknown_entitlement_is_not_recorded(self):
        tracked, seen, saved = self._activation({"u1": True}, False, known=False)
        self.assertEqual((tracked, seen, saved), ([], {"u1": True}, False))

    def test_signed_in_before_the_server_answers_is_not_free(self):
        self.assertEqual(self._tier(is_admin=False, is_pro=False, is_authenticated=True,
                                    entitlement_known=False), "unknown")


class TestTelemetryContract(unittest.TestCase):
    """The app, the edge function and the call sites must agree on event names -
    the server silently drops anything it doesn't allow-list."""

    def test_server_allowlist_matches_the_app(self):
        self.assertEqual(_ts_set("allowedEvents"), telemetry.ALLOWED_EVENTS)

    def test_server_strips_every_sensitive_key(self):
        self.assertEqual(telemetry.SENSITIVE_KEYS - _ts_set("sensitiveKeys"), set())

    def test_every_tracked_event_is_allow_listed(self):
        call = re.compile(r"\btrack\(\s*\"(\w+)\"(?:\s+if\s+[^()]+?\s+else\s+\"(\w+)\")?")
        skip = {"venv", ".venv", "tests", "build", "dist", ".claude", ".git", "__pycache__"}
        used = set()
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for fn in filenames:
                if fn.endswith(".py"):
                    src = Path(dirpath, fn).read_text(encoding="utf-8")
                    for m in call.finditer(src):
                        used.update(n for n in m.groups() if n)
        self.assertGreater(len(used), 30)   # the scan actually found the call sites
        self.assertEqual(used - telemetry.ALLOWED_EVENTS, set())


if __name__ == "__main__":
    unittest.main()


class TestTelemetryBackoff(unittest.TestCase):
    def test_new_events_do_not_cut_a_backoff_short(self):
        # A full queue wakes the sender on every event; while the server is
        # failing that must not turn into one upload attempt per event.
        attempts = []

        def failing_flush(_cfg):
            attempts.append(1)
            return False

        waits = []
        real_wait = telemetry._wake.wait

        def fake_wait(timeout=None):
            waits.append(timeout)
            if len(waits) > 6:
                raise SystemExit  # stop the loop
            telemetry._wake.set()   # an event arrives during every wait
            return real_wait(0)

        with patch.object(telemetry, "_flush", side_effect=failing_flush),              patch.object(telemetry._wake, "wait", side_effect=fake_wait),              patch.object(telemetry, "FLUSH_INTERVAL", 0.05),              patch.object(telemetry, "MAX_BACKOFF", 0.2):
            with self.assertRaises(SystemExit):
                telemetry._sender_loop()
        # 7 wake-ups inside the first 0.1 s backoff: one attempt, not seven
        # (the old loop retried on every wake-up).
        self.assertEqual(len(attempts), 1)

