import unittest
from unittest.mock import patch

import telemetry


class TestTelemetry(unittest.TestCase):
    def test_privacy_mode_does_not_disable_analytics(self):
        # Analytics is independent of Privacy Mode — the sanitizer already
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


if __name__ == "__main__":
    unittest.main()
