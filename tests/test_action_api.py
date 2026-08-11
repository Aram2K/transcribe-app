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


class TestAnthropicPayload(unittest.TestCase):
    """Regression: the Anthropic path used to send messages[1] as the user turn.
    For smart_auto that index is the FIRST FEW-SHOT EXAMPLE, so the user's real
    dictation never reached the model."""

    def _post(self, mode, text):
        response = MagicMock(status_code=200)
        response.json.return_value = {"content": [{"text": "OK"}]}
        cfg = {"action_api_key": "k", "action_api_provider": action_api.PROVIDER_ANTHROPIC}
        with patch.object(action_api.requests, "post", return_value=response) as post:
            action_api.run_action(text, mode, cfg)
        return post.call_args[1]["json"]

    def test_user_dictation_reaches_the_model(self):
        body = self._post("smart_auto", "translate to russian I will meet you at 5pm")
        last = body["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("5pm", last["content"])

    def test_few_shot_turns_are_preserved(self):
        body = self._post("smart_auto", "hello world")
        roles = [m["role"] for m in body["messages"]]
        self.assertGreater(len(roles), 1, "few-shot turns should be forwarded")
        self.assertTrue(all(r in ("user", "assistant") for r in roles))
        self.assertNotIn("system", roles)   # system goes in its own field

    def test_simple_mode_still_works(self):
        body = self._post("summarize", "some long text to summarize")
        self.assertEqual(len(body["messages"]), 1)
        self.assertIn("some long text", body["messages"][0]["content"])
        self.assertTrue(body["system"])


class TestVocabularyInPrompts(unittest.TestCase):
    def test_vocab_block_reaches_system_message(self):
        msgs = action_api.build_messages("hi", "summarize", vocab_block="SPELLING: Aibuben")
        self.assertIn("Aibuben", msgs[0]["content"])

    def test_absent_by_default(self):
        msgs = action_api.build_messages("hi", "summarize")
        self.assertNotIn("SPELLING", msgs[0]["content"])

    def test_smart_auto_receives_it_too(self):
        msgs = action_api.build_messages("hi", "smart_auto", vocab_block="SPELLING: Aibuben")
        self.assertIn("Aibuben", msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
