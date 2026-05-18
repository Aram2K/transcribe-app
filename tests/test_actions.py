import unittest
from unittest.mock import patch

import actions


class TestActions(unittest.TestCase):
    def test_transcribe_only_returns_text(self):
        self.assertEqual(
            actions.process("hello world", "transcribe_only"),
            "hello world",
        )

    def test_write_email_formats_draft(self):
        out = actions.process("please send the report today", "write_email")
        self.assertIn("Subject:", out)
        self.assertIn("Hi,", out)
        self.assertIn("Please send the report today.", out)

    def test_make_todo_list_formats_checkboxes(self):
        out = actions.process(
            "call Ani and then send the file. also review the notes",
            "make_todo_list",
        )
        self.assertIn("- [ ] Call Ani", out)
        self.assertIn("- [ ] Send the file", out)
        self.assertIn("- [ ] Review the notes", out)

    def test_unknown_action_falls_back_to_transcribe(self):
        self.assertEqual(
            actions.process("hello", "unknown"),
            "hello",
        )

    def test_qwen_missing_falls_back_for_email(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=False), \
             patch.object(actions.local_llm, "run_action") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.local_llm.QWEN_TINY_ID,
            )
        self.assertIn("Subject:", out)
        run_action.assert_not_called()

    def test_qwen_ready_handles_action(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=True), \
             patch.object(actions.local_llm, "run_action", return_value="LLM output") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.local_llm.QWEN_TINY_ID,
            )
        self.assertEqual(out, "LLM output")
        run_action.assert_called_once()

    def test_translate_can_use_qwen_when_argos_is_missing(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=True), \
             patch.object(actions.local_llm, "run_action", return_value="Bonjour") as run_action, \
             patch.object(actions, "_translate_local", side_effect=actions.ActionError("missing pack")):
            out = actions.process(
                "Hello",
                "translate",
                target_lang="fr",
                model="built_in",
            )
        self.assertEqual(out, "Bonjour")
        run_action.assert_called_once()


if __name__ == "__main__":
    unittest.main()
