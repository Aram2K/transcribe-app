"""Regression tests for managed (Pro) routing of non-smart action modes.

Bug: actions.process() had no `managed` branch outside of smart_auto, so a Pro
user's meeting notes silently went to the local extractive summarizer, and a
cloud engine without a key raised "Add your action API key" - which on the
meeting path then wiped the recording. These lock the corrected routing in.
"""
import unittest
from unittest.mock import patch

import actions


class TestManagedMeetingNotes(unittest.TestCase):
    def test_pro_meeting_notes_routes_to_managed_cloud(self):
        cfg = {"_managed_token": "tok123"}
        with patch("action_api.run_managed_action",
                   return_value="# Notes\n- shipped build") as m:
            out = actions.process(
                "we shipped the build and agreed to meet monday",
                actions.ACTION_MEETING_NOTES,
                source_lang="en", target_lang="en",
                model=actions.API_MANAGED_ID, config=cfg,
            )
        self.assertEqual(out, "# Notes\n- shipped build")
        m.assert_called_once()
        args, _ = m.call_args
        self.assertEqual(args[1], actions.ACTION_MEETING_NOTES)  # mode forwarded
        self.assertEqual(args[2], "tok123")                       # token forwarded

    def test_managed_without_token_falls_back_to_extractive(self):
        # Not Pro / signed out: no _managed_token. Must NOT raise and must NOT
        # demand an API key - produce a local extractive summary instead.
        out = actions.process(
            "first we discussed the roadmap. then we assigned tasks. bob owns X.",
            actions.ACTION_MEETING_NOTES,
            source_lang="en", target_lang="en",
            model=actions.API_MANAGED_ID, config={},
        )
        self.assertIsInstance(out, str)
        self.assertTrue(out.strip())

    def test_privacy_mode_keeps_managed_local(self):
        # Privacy mode must keep everything local even with a token present.
        cfg = {"_managed_token": "tok123", "privacy_mode": True}
        with patch("action_api.run_managed_action") as m:
            out = actions.process(
                "we discussed the launch plan and next steps.",
                actions.ACTION_MEETING_NOTES,
                source_lang="en", target_lang="en",
                model=actions.API_MANAGED_ID, config=cfg,
            )
        m.assert_not_called()
        self.assertIsInstance(out, str)
        self.assertTrue(out.strip())


if __name__ == "__main__":
    unittest.main()
