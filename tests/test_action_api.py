import unittest
from unittest.mock import MagicMock, patch

import action_api


class TestActionAPI(unittest.TestCase):
    def test_privacy_mode_blocks_cloud_actions(self):
        with self.assertRaises(action_api.ActionAPIError):
            action_api.run_action(
                "hello",
                "write_email",
                {"action_api_key": "secret", "privacy_mode": True},
            )

    def test_missing_key_blocks_cloud_actions(self):
        with self.assertRaises(action_api.ActionAPIError):
            action_api.run_action(
                "hello",
                "write_email",
                {"privacy_mode": False},
            )

    def test_openai_request_uses_auth_header_not_payload_key(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        with patch.object(action_api.requests, "post", return_value=response) as post:
            out = action_api.run_action(
                "hello",
                "write_email",
                {
                    "privacy_mode": False,
                    "action_api_key": "secret",
                    "action_api_provider": action_api.PROVIDER_OPENAI,
                    "action_api_base_url": "https://api.example.com/v1",
                    "action_api_model": "demo",
                },
            )
        self.assertEqual(out, "ok")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertNotIn("secret", str(kwargs["json"]))

    def test_managed_action_requires_token(self):
        with self.assertRaises(action_api.ActionAPIError):
            action_api.run_managed_action("hello", "smart_auto", "")

    def test_managed_action_posts_messages_with_bearer_token(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"text": "MANAGED OK"}
        with patch.object(action_api.requests, "post", return_value=response) as post:
            out = action_api.run_managed_action(
                "summarize this", "smart_auto", "tok123",
            )
        self.assertEqual(out, "MANAGED OK")
        args, kwargs = post.call_args
        # Goes to the smart-action edge function with the user's JWT, and the
        # built messages (never a raw API key) in the body.
        self.assertEqual(args[0], action_api.SMART_ACTION_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok123")
        self.assertIn("messages", kwargs["json"])
        self.assertTrue(kwargs["json"]["messages"])

    def test_managed_action_maps_403_to_pro_required(self):
        response = MagicMock(status_code=403)
        response.json.return_value = {"error": "pro_required"}
        with patch.object(action_api.requests, "post", return_value=response):
            with self.assertRaises(action_api.ActionAPIError) as ctx:
                action_api.run_managed_action("hi", "smart_auto", "tok")
        self.assertIn("Pro", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
