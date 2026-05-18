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


if __name__ == "__main__":
    unittest.main()
