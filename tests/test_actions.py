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

    def test_rule_based_formatter_does_not_call_llm(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=True), \
             patch.object(actions.local_llm, "run_action") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.RULE_BASED_ID,
            )
        self.assertIn("Subject:", out)
        run_action.assert_not_called()

    def test_smart_auto_email_uses_rule_formatter_when_rule_based(self):
        out = actions.process(
            "write an email to john that the report is ready tonight",
            actions.ACTION_SMART_AUTO,
            model=actions.RULE_BASED_ID,
        )
        self.assertIn("Subject:", out)
        self.assertIn("Hi,", out)
        self.assertNotIn("write an email", out.lower())

    def test_smart_auto_email_uses_llm_when_downloaded(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=True), \
             patch.object(actions.local_llm, "run_action", return_value="Subject: Hi\n\nBody\n\nBest,") as run_action:
            out = actions.process(
                "write an email to john that the report is ready",
                actions.ACTION_SMART_AUTO,
                model=actions.local_llm.QWEN_TINY_ID,
            )
        self.assertIn("Subject:", out)
        run_action.assert_called_once()

    def test_smart_auto_missing_local_model_raises_clear_error(self):
        # When the chosen local engine isn't downloaded, Smart mode must
        # surface a clear, actionable error instead of silently degrading to
        # rule-based output (which looked like "it just transcribed").
        with patch.object(actions.local_llm, "model_downloaded", return_value=False), \
             patch.object(actions.local_llm, "run_action") as run_action:
            with self.assertRaises(actions.ActionError) as ctx:
                actions.process(
                    "write an email to john that the report is ready",
                    actions.ACTION_SMART_AUTO,
                    model=actions.local_llm.QWEN_7B_ID,
                )
        self.assertIn("download", str(ctx.exception).lower())
        run_action.assert_not_called()

    def test_cloud_action_uses_configured_api(self):
        with patch.object(actions.action_api, "run_action", return_value="API output") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.API_OPENAI_ID,
                config={"action_api_key": "secret", "privacy_mode": False},
            )
        self.assertEqual(out, "API output")
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


class TestMeetingNotes(unittest.TestCase):
    SAMPLE = (
        "Hi everyone, thanks for joining the kickoff. We need to lock the "
        "API contract by Friday. I'll draft the spec and send it tonight. "
        "Aram should review it Monday morning. We also discussed the budget "
        "but did not decide anything yet. Open question: are we shipping the "
        "v2 endpoint with or without auth?"
    )

    def test_summarize_extractive_returns_a_short_summary(self):
        out = actions._summarize_extractive(self.SAMPLE, max_sentences=3)
        self.assertTrue(out)
        # Output is shorter than the original transcript and keeps the
        # first sentence + last sentence as anchors for context.
        self.assertLess(len(out), len(self.SAMPLE))
        self.assertTrue(out.startswith("Hi everyone"))
        # Sentence count in output should be at most max_sentences.
        sentence_count = sum(1 for ch in out if ch in ".!?")
        self.assertLessEqual(sentence_count, 3)
        self.assertGreaterEqual(sentence_count, 2)

    def test_action_items_heuristic_finds_obvious_todos(self):
        items = actions._extract_action_items_heuristic(self.SAMPLE)
        joined = " ".join(items).lower()
        self.assertIn("draft", joined)         # 'I'll draft the spec...'
        self.assertIn("review", joined)        # 'Aram should review...'

    def test_meeting_notes_rule_based_returns_structured_markdown(self):
        out = actions.process(self.SAMPLE, actions.ACTION_MEETING_NOTES,
                              model=actions.RULE_BASED_ID)
        self.assertIn("## Summary", out)
        self.assertIn("## Action items", out)
        self.assertIn("- [ ]", out)            # at least one checkbox item

    def test_meeting_notes_cloud_uses_api(self):
        with patch.object(actions.action_api, "run_action",
                          return_value="## Summary\nGreat meeting.") as call:
            out = actions.process(
                self.SAMPLE,
                actions.ACTION_MEETING_NOTES,
                model=actions.API_OPENAI_ID,
                config={"action_api_key": "secret", "privacy_mode": False},
            )
        self.assertIn("Great meeting", out)
        call.assert_called_once()

    def test_summarize_cloud_uses_api(self):
        with patch.object(actions.action_api, "run_action",
                          return_value="Two sentences.") as call:
            out = actions.process(
                self.SAMPLE,
                actions.ACTION_SUMMARIZE,
                model=actions.API_OPENAI_ID,
                config={"action_api_key": "secret", "privacy_mode": False},
            )
        self.assertEqual(out, "Two sentences.")
        call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
