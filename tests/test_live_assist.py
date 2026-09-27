"""Live Assistance: context building, mode plumbing, no-AI fallback, glass helpers.

Pure logic here; the overlay widget is exercised by offscreen smokes and the
in-session probe. Qt-dependent parts are guarded like test_file_transcribe.
"""
import sys
import json
import unittest

import actions
import action_api
import local_llm


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


class TestModePlumbing(unittest.TestCase):
    def test_mode_is_whitelisted(self):
        # Without this the mode silently degrades to "transcribe only" and the
        # overlay would echo the transcript back as a "suggestion".
        self.assertEqual(actions.normalize_action_mode(actions.ACTION_LIVE_ASSIST),
                         actions.ACTION_LIVE_ASSIST)

    def test_cloud_and_local_prompts_exist(self):
        cloud = action_api.build_messages("Conversation (latest part):\nhi?", "live_assist")
        # The brief is the system message (same on every call - cacheable);
        # the user turn is only the live context.
        self.assertEqual(cloud[0]["role"], "system")
        self.assertEqual(cloud[1]["content"], "Conversation (latest part):\nhi?")
        prompt = cloud[0]["content"]
        self.assertIn("companion", prompt)
        self.assertIn("at a glance", prompt)                    # the model is told WHY to be brief
        self.assertIn("no speaker labels", prompt)              # ...and how to read the transcript
        self.assertNotIn("interview", prompt)
        # A companion answers - the old "They're asking / Latest" header read
        # as a summary, and recaps are exactly what users don't want here.
        self.assertNotIn("They're asking", prompt)
        self.assertIn("Never recap", prompt)
        self.assertIn("screenshot", prompt)
        local = local_llm._messages_for("live_assist", "hi?", "auto", "en")
        self.assertIn("companion", local[-1]["content"])
        self.assertLess(len(local[-1]["content"]), len(prompt),
                        "local prompt must stay tighter than the cloud one")

    def test_token_budgets(self):
        # Room for a full code solution in the cloud (it streams in); small
        # local models on a CPU stay short.
        self.assertLessEqual(action_api._max_tokens_for("live_assist"), 800)
        self.assertGreaterEqual(action_api._max_tokens_for("live_assist"), 600)
        self.assertLessEqual(local_llm._MAX_TOKENS_BY_MODE["live_assist"], 400)


class TestBasicFallback(unittest.TestCase):
    def test_points_at_last_question(self):
        text = ("Conversation (latest part):\nWe compared both trackers. "
                "Can you send the calibration code by Friday?")
        out = actions.process(text, actions.ACTION_LIVE_ASSIST,
                              model=actions.RULE_BASED_ID, config={})
        self.assertIn("They're asking: Can you send the calibration code by Friday?", out)
        self.assertIn("basic mode", out)

    def test_question_from_user_is_answered_honestly(self):
        text = "Conversation (latest part):\nNothing much.\n\nUser's question: what deadline?"
        out = actions.process(text, actions.ACTION_LIVE_ASSIST,
                              model=actions.RULE_BASED_ID, config={})
        self.assertIn("You asked: what deadline?", out)
        self.assertIn("can't answer questions", out)

    def test_no_question_gives_latest(self):
        out = actions._live_assist_basic("Conversation (latest part):\nWe agreed on Friday.")
        self.assertTrue(out.startswith("Latest: We agreed on Friday."))


class TestRollingContext(unittest.TestCase):
    def setUp(self):
        from live_context import rolling_context, TAIL_CHARS
        self.rc, self.tail = rolling_context, TAIL_CHARS

    def test_trims_to_tail_at_sentence_boundary(self):
        text = ("Old stuff nobody needs. " * 400) + "Recent point. Final question?"
        ctx = self.rc(text, output_lang="auto")
        self.assertLessEqual(len(ctx), self.tail + 200)
        self.assertTrue(ctx.endswith("Final question?"))
        body = ctx.split("Conversation (latest part):\n", 1)[1]
        self.assertTrue(body[0].isupper(), "must start at a sentence, not mid-word")

    def test_includes_meta_and_question(self):
        ctx = self.rc("Hello.", question="What now?", title="Sync", attendees="Aram")
        self.assertIn("Meeting: Sync", ctx)
        self.assertIn("Attendees: Aram", ctx)
        self.assertTrue(ctx.endswith("User's question: What now?"))

    def test_empty_transcript_is_explicit(self):
        self.assertIn("(nothing transcribed yet)", self.rc(""))

    def test_earlier_answers_ride_along_for_follow_ups(self):
        history = [("First?", "old answer one"), ("Second?", "answer two " * 60),
                   ("Third?", "answer three")]
        ctx = self.rc("And the second part?", question="and the second part?",
                      history=history)
        self.assertNotIn("old answer one", ctx)              # only the last turns
        self.assertIn("Q: Third? A: answer three", ctx)
        self.assertIn("…", ctx)                              # long answers are clipped
        self.assertTrue(ctx.endswith("User's question: and the second part?"))
        self.assertNotIn("earlier answers", self.rc("Hi."))  # nothing when empty

    def test_screen_note(self):
        self.assertIn("screenshot", self.rc("Hi.", screen=True))
        self.assertNotIn("screenshot", self.rc("Hi."))


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestOverlayHelpers(unittest.TestCase):
    def setUp(self):
        import ui.live_assist as la
        self.la = la

    def test_private_state_is_truthful(self):
        ps = self.la.private_state
        self.assertEqual(ps(True, True, False, True)[0], "on")
        # Wanted but the OS did NOT confirm -> must say visible, never hidden.
        self.assertEqual(ps(True, True, False, False)[0], "failed")
        self.assertIn("visible", ps(True, True, False, False)[1].lower())
        self.assertEqual(ps(True, False, False, False)[0], "unavailable")   # old Windows / macOS
        self.assertEqual(ps(True, True, True, False)[0], "unavailable")    # remote session
        self.assertEqual(ps(False, True, False, False)[0], "off")
        for args in ((True, False, False, True), (True, True, True, True)):
            self.assertNotIn("not in share", ps(*args)[1])
        # Header budget: every chip text must stay short enough not to clip.
        for args in ((True, True, False, True), (True, True, False, False),
                     (True, False, False, False), (True, True, True, False),
                     (False, True, False, False)):
            self.assertLessEqual(len(ps(*args)[1]), 24, ps(*args)[1])

    def test_position_clamped_onto_a_live_screen(self):
        rects = [(0, 0, 1920, 1080)]
        # A position saved on a monitor that's gone must come back on-screen.
        x, y = self.la.clamp_to_rects(2500, 300, 420, 560, rects)
        self.assertTrue(0 <= x <= 1920 - 420 and 0 <= y <= 1080 - 560)
        # A valid position is left alone.
        self.assertEqual(self.la.clamp_to_rects(100, 100, 420, 560, rects), (100, 100))
        # Second monitor counts as valid too.
        rects2 = [(0, 0, 1920, 1080), (1920, 0, 3840, 1080)]
        self.assertEqual(self.la.clamp_to_rects(2500, 300, 420, 560, rects2), (2500, 300))

    def test_quick_actions_shape(self):
        labels = [l for l, _ in self.la.QUICK_ACTIONS]
        self.assertEqual(labels, ["Answer", "Follow-ups", "Solve screen"])
        self.assertNotIn("Recap", labels)                     # no summaries here
        self.assertEqual(self.la.QUICK_ACTIONS[0][1], "")     # default = answer the latest
        self.assertTrue(all(q for _, q in self.la.QUICK_ACTIONS[1:]))
        self.assertEqual(self.la.QUICK_ACTIONS[2][1], self.la.SOLVE_SCREEN)


class TestImagePlumbing(unittest.TestCase):
    MSGS = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "example"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "the real question"}]

    def test_jpeg_screenshots_get_the_right_mime_type(self):
        jpeg = "/9j/4AAQSkZJRg"
        url = action_api.openai_messages_with_image(self.MSGS, jpeg)[3]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,/9j/"))
        self.assertEqual(action_api.gemini_parts("p", jpeg)[1]["inline_data"]["mime_type"], "image/jpeg")
        convo = action_api.anthropic_convo_with_image([{"role": "user", "content": "q"}], jpeg)
        self.assertEqual(convo[0]["content"][0]["source"]["media_type"], "image/jpeg")

    def test_openai_attaches_to_last_user_turn_only(self):
        out = action_api.openai_messages_with_image(self.MSGS, "AAAA")
        self.assertEqual(out[1]["content"], "example")          # few-shot untouched
        parts = out[3]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "the real question"})
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,AAAA"))
        self.assertEqual(self.MSGS[3]["content"], "the real question")   # input not mutated

    def test_no_image_is_a_plain_copy(self):
        self.assertEqual(action_api.openai_messages_with_image(self.MSGS, None), self.MSGS)

    def test_gemini_and_anthropic_shapes(self):
        parts = action_api.gemini_parts("p", "AAAA")
        self.assertEqual(parts[0], {"text": "p"})
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/png")
        self.assertEqual(action_api.gemini_parts("p", None), [{"text": "p"}])
        convo = action_api.anthropic_convo_with_image(
            [{"role": "user", "content": "q"}], "AAAA")
        self.assertEqual(convo[0]["content"][0]["type"], "image")   # image first
        self.assertEqual(convo[0]["content"][1]["text"], "q")


class TestDevTier(unittest.TestCase):
    def test_env_override_only_when_running_from_source(self):
        import os
        import entitlements
        from unittest.mock import patch
        with patch.dict(os.environ, {"TRANSCRIBE_DEV_TIER": "pro"}):
            with patch.object(sys, "frozen", False, create=True):
                self.assertEqual(entitlements.tier(None), entitlements.TIER_PRO)
                self.assertTrue(entitlements.has_pro_access(None))
            # A frozen (installed) build must ignore it completely.
            with patch.object(sys, "frozen", True, create=True):
                self.assertEqual(entitlements.tier(None), entitlements.TIER_GUEST)
        with patch.dict(os.environ, {"TRANSCRIBE_DEV_TIER": "nonsense"}):
            self.assertEqual(entitlements.tier(None), entitlements.TIER_GUEST)


class TestGlassHelpers(unittest.TestCase):
    def test_noop_off_windows_or_without_window(self):
        from ui import glass

        class NoWin:
            def winId(self):
                raise RuntimeError("no native window")
        w = NoWin()
        # Every helper must degrade to a harmless False/"" rather than raise.
        self.assertFalse(glass.exclude_from_capture(w, True))
        self.assertFalse(glass.is_excluded_from_capture(w))
        self.assertEqual(glass.apply_backdrop_blur(w), "")
        self.assertFalse(glass.set_click_through(w, True))
        self.assertFalse(glass.round_corners(w))
        glass.remove_backdrop_blur(w)

    def test_support_flag_matches_platform(self):
        from ui import glass
        if sys.platform != "win32":
            self.assertFalse(glass.capture_exclusion_supported())
        else:
            self.assertEqual(glass.capture_exclusion_supported(),
                             glass.windows_build() >= 19041)


if __name__ == "__main__":
    unittest.main()


class TestFastProviderPlumbing(unittest.TestCase):
    def test_reasoning_effort_defaults(self):
        f = action_api.reasoning_effort_for
        # Thinking models must be told not to think unless configured.
        self.assertEqual(f("qwen-3.8-27b"), "none")
        self.assertEqual(f("qwen/qwen3.8-27b"), "none")
        # gpt-oss cannot disable reasoning: floor at low, even if asked for none.
        self.assertEqual(f("gpt-oss-120b"), "low")
        self.assertEqual(f("gpt-oss-120b", "none"), "low")
        # Explicit setting wins; unknown models send nothing.
        self.assertEqual(f("qwen-3.8-27b", "low"), "low")
        self.assertIsNone(f("gpt-4o-mini"))

    def test_recap_model_override(self):
        d = action_api.defaults(action_api.PROVIDER_CEREBRAS)
        self.assertEqual(d["default_model"], "qwen-3.8-27b")
        self.assertEqual(action_api.model_for({}, "live_assist", d), "qwen-3.8-27b")
        self.assertEqual(action_api.model_for({"action_api_model_recap": "gpt-oss-120b"},
                                              "live_recap", d), "gpt-oss-120b")
        self.assertEqual(action_api.model_for({"action_api_model_recap": "gpt-oss-120b"},
                                              "live_assist", d), "qwen-3.8-27b")

    def test_cerebras_is_a_registered_cloud_engine(self):
        info = actions.ACTION_MODELS[actions.API_CEREBRAS_ID]
        self.assertEqual(info["kind"], "cloud")
        self.assertEqual(info["provider"], action_api.PROVIDER_CEREBRAS)
        self.assertEqual(action_api.defaults(action_api.PROVIDER_CEREBRAS)["default_base_url"],
                         "https://api.cerebras.ai/v1")


class TestScreenContext(unittest.TestCase):
    def setUp(self):
        from live_context import should_attach_screen
        self.f = should_attach_screen

    def test_on_means_every_answer_sees_the_screen(self):
        # No keyword guessing: "how do I solve this?" is about the screen too.
        self.assertTrue(self.f(True))
        self.assertFalse(self.f(False))

    def test_solve_screen_forces_it(self):
        self.assertTrue(self.f(False, force=True))


class TestQuestionDetection(unittest.TestCase):
    def setUp(self):
        from live_context import looks_like_question, last_question
        self.q, self.last = looks_like_question, last_question

    def test_punctuated_questions_in_any_language(self):
        self.assertTrue(self.q("So what would you do differently?"))
        self.assertTrue(self.q("Ինչպե՞ս ես լուծելու այս խնդիրը։"))      # Armenian ՞
        self.assertTrue(self.q("Как бы вы это решили?"))

    def test_unpunctuated_speech_recognition(self):
        self.assertTrue(self.q("tell me about a project you're proud of"))
        self.assertTrue(self.q("Okay. Walk me through your solution."))
        self.assertTrue(self.q("could you share the numbers"))

    def test_statements_are_not_questions(self):
        self.assertFalse(self.q("We shipped the release on Friday."))
        self.assertFalse(self.q("I think that's fine."))
        self.assertFalse(self.q(""))
        # Wh-openers that end with a full stop are statements - e.g. the user
        # reading the answer aloud must not trigger a new answer.
        for s in ("When we shipped it, latency dropped by half.",
                  "What I would do first is add a cache.", "Which is why we moved it.",
                  "How we solved it was by caching the results.", "Who knows."):
            self.assertFalse(self.q(s), s)

    def test_unpunctuated_wh_question_still_counts(self):
        self.assertTrue(self.q("what would you do differently"))
        self.assertTrue(self.q("Walk me through your solution."))    # a request, full stop or not

    def test_last_question(self):
        text = "We met on Monday. What is the budget? Thanks. How long will it take?"
        self.assertEqual(self.last(text), "How long will it take?")
        self.assertEqual(self.last("No questions here."), "")


NO_THINK = {"thinking": False, "enable_thinking": False}


class TestSelfHostedModels(unittest.TestCase):
    """A Modal (vLLM/SGLang) DeepSeek or Qwen turns thinking off via the chat
    template (older servers reject reasoning_effort); hosted models (Cerebras,
    OpenRouter) keep reasoning_effort."""

    def test_modal_deepseek_gets_thinking_off_and_its_own_sampling(self):
        extras = action_api.openai_payload_extras(
            "deepseek-ai/DeepSeek-V4.1-Flash", "https://ws--deepseek.modal.run/v1", "live_assist")
        self.assertEqual(extras["chat_template_kwargs"], NO_THINK)
        self.assertNotIn("reasoning_effort", extras)
        self.assertEqual((extras["temperature"], extras["top_p"]), (0.6, 0.95))
        self.assertNotIn("top_k", extras)                       # Qwen-only settings
        self.assertNotIn("presence_penalty", extras)
        notes = action_api.openai_payload_extras(
            "deepseek-ai/DeepSeek-V4.1-Flash", "https://ws--deepseek.modal.run/v1", "meeting_notes")
        self.assertEqual(notes, {"chat_template_kwargs": NO_THINK})
        # A short Modal model name still counts; OpenRouter's ids do not.
        self.assertEqual(action_api.self_hosted_family(
            "deepseek-v4.1-flash", "https://ws--ds.modal.run/v1"), "deepseek")
        self.assertIsNone(action_api.self_hosted_family(
            "deepseek/deepseek-chat", "https://openrouter.ai/api/v1"))

    def test_modal_qwen_gets_template_kwargs(self):
        extras = action_api.openai_payload_extras(
            "Qwen/Qwen3.6-35B-A3B", "https://ws--qwen.modal.run/v1", "live_assist")
        self.assertEqual(extras["chat_template_kwargs"], NO_THINK)
        self.assertNotIn("reasoning_effort", extras)
        self.assertEqual(extras["top_k"], 20)                   # model-card sampling, live only
        plain = action_api.openai_payload_extras(
            "Qwen/Qwen3.6-35B-A3B", "https://ws--qwen.modal.run/v1", "smart_auto")
        self.assertNotIn("temperature", plain)                  # other actions stay deterministic

    def test_hosted_models_unchanged(self):
        self.assertEqual(action_api.openai_payload_extras(
            "qwen-3.8-27b", "https://api.cerebras.ai/v1", "live_assist"),
            {"reasoning_effort": "none"})
        self.assertEqual(action_api.openai_payload_extras(
            "gpt-4o-mini", "https://api.openai.com/v1", "live_assist"), {})

    def test_warm_up_only_pings_self_hosted_endpoints(self):
        from unittest.mock import patch
        modal = {"action_api_provider": action_api.PROVIDER_OPENAI, "action_api_key": "wk-1.ws-2",
                 "action_api_base_url": "https://ws--qwen.modal.run/v1",
                 "action_api_model": "Qwen/Qwen3.6-35B-A3B"}
        with patch.object(action_api.requests, "post") as post:
            action_api.warm_up(modal)
        body = post.call_args[1]["json"]
        self.assertEqual(body["max_tokens"], 1)
        self.assertEqual(body["chat_template_kwargs"], NO_THINK)
        cerebras = {"action_api_provider": action_api.PROVIDER_CEREBRAS, "action_api_key": "k"}
        with patch.object(action_api.requests, "post") as post:
            action_api.warm_up(cerebras)
        post.assert_not_called()


class TestThinkFilter(unittest.TestCase):
    """Reasoning a model writes inline must never reach the answer box."""

    def run_filter(self, chunks):
        f = action_api.ThinkFilter()
        return "".join(f.feed(c) for c in chunks) + f.flush()

    def test_leading_block_is_hidden_across_chunk_boundaries(self):
        self.assertEqual(self.run_filter(["<thi", "nk>\nplan it", " out</thi", "nk>\n\nThe ", "answer"]),
                         "The answer")

    def test_text_after_the_answer_starts_is_untouched(self):
        self.assertEqual(self.run_filter(["Hello ", "<think>kept</think>"]), "Hello <think>kept</think>")
        self.assertEqual(self.run_filter(["<", "3 you"]), "<3 you")
        self.assertEqual(self.run_filter(["a < b"]), "a < b")

    def test_unclosed_block_shows_nothing(self):
        self.assertEqual(self.run_filter(["<think>still going"]), "")

    def test_strip_think_final_text(self):
        self.assertEqual(action_api.strip_think("<think>x</think> y"), "y")
        self.assertEqual(action_api.strip_think(" plain "), "plain")
        # Only a template-opening family gets the close-tag-only rule.
        self.assertEqual(action_api.strip_think("reasoning\n</think>\n\nFinal", True), "Final")
        self.assertEqual(action_api.strip_think("reasoning\n</think>\n\nFinal"),
                         "reasoning\n</think>\n\nFinal")

    def test_managed_stream_hides_inline_reasoning(self):
        from unittest.mock import MagicMock, patch
        resp = MagicMock(status_code=200, headers={"Content-Type": "text/event-stream"})
        lines = ['data: {"choices":[{"delta":{"content":"<think>hmm"}}]}',
                 'data: {"choices":[{"delta":{"content":"</think>Use a cache."}}]}',
                 "data: [DONE]"]
        resp.iter_lines.return_value = iter(lines)
        seen = []
        with patch.object(action_api.requests, "post", return_value=resp):
            out = action_api.run_managed_action_stream("q", "live_assist", "T", seen.append)
        self.assertEqual(out, "Use a cache.")
        self.assertEqual("".join(seen), "Use a cache.")

    def test_reasoning_field_is_never_shown(self):
        from unittest.mock import MagicMock, patch
        resp = MagicMock(status_code=200, headers={"Content-Type": "text/event-stream"})
        resp.iter_lines.return_value = iter([
            'data: {"choices":[{"delta":{"reasoning_content":"secret plan"}}]}',
            'data: {"choices":[{"delta":{"content":"Answer"}}]}', "data: [DONE]"])
        seen = []
        with patch.object(action_api.requests, "post", return_value=resp):
            out = action_api.run_managed_action_stream("q", "live_assist", "T", seen.append)
        self.assertEqual((out, "".join(seen)), ("Answer", "Answer"))


class TestThinkFilterTemplateOpened(unittest.TestCase):
    """Self-hosted DeepSeek (V3.1+/V4/R1) and Qwen3 templates open the block in
    the prompt: without a reasoning parser the output is
    "{reasoning}</think>{answer}" - only the close tag, often with no newline.
    That rule applies ONLY to those families; for everyone else a "</think>"
    in the text is something the answer is talking about."""

    def stream(self, chunks, template_opened=True):
        f = action_api.ThinkFilter(template_opened)
        shown, events = [], []
        for c in chunks + [None]:
            d = f.flush() if c is None else f.feed(c)
            if isinstance(d, action_api.ReplaceText):
                events.append("replace")
                shown[:] = [str(d)]
            elif d:
                shown.append(d)
        return "".join(shown), events, f

    def test_deepseek_format_with_no_newline(self):
        shown, events, f = self.stream(["User wants the budget; they said 40k.</thi", "nk>",
                                        "Say: **40k for Q3.**"])
        self.assertEqual((shown, events), ("Say: **40k for Q3.**", ["replace"]))
        self.assertTrue(f.saw_reasoning)

    def test_reasoning_line_then_answer(self):
        shown, events, _ = self.stream(["The user asks about the budget.", " I should say 40k.\n</th",
                                        "ink>\n\n", "Say: **40k**."])
        self.assertEqual((shown.strip(), events), ("Say: **40k**.", ["replace"]))

    def test_reasoning_that_mentions_fences_or_the_open_tag(self):
        for reasoning in ["I'll put it in a ```python block for them.",
                          "They asked about <think> tags, so explain briefly."]:
            shown, events, _ = self.stream([reasoning, "</think>", "Answer."])
            self.assertEqual((shown, events), ("Answer.", ["replace"]), reasoning)

    def test_tag_inside_code_is_left_alone(self):
        for answer in ["Split on the closing tag:\n```python\nanswer = raw.split('</think>')[-1]\n```",
                       "Use `</think>` as the separator.",
                       "Use this:\n```\n</think>\n```\nthat's the tag."]:
            shown, events, _ = self.stream([answer[:10], answer[10:]])
            self.assertEqual((shown, events), (answer, []), answer)
            self.assertEqual(action_api.strip_think(answer, True), answer.strip())

    def test_other_models_never_get_the_close_tag_rule(self):
        for answer in ["DeepSeek's reasoning block closes with </think>",
                       "It ends with </think>\n- the template opened it for you",
                       "Models emit </think> when they finish."]:
            shown, events, f = self.stream([answer[:12], answer[12:]], template_opened=False)
            self.assertEqual((shown, events), (answer, []), answer)
            self.assertFalse(f.saw_reasoning)
            self.assertEqual(action_api.strip_think(answer), answer.strip())

    def test_reasoning_with_no_answer(self):
        shown, events, f = self.stream(["plan\n</think>"])
        self.assertEqual(shown, "")
        self.assertTrue(f.saw_reasoning)

    def test_strip_think_template_opened(self):
        self.assertEqual(action_api.strip_think("plan\n</think>\n\nFinal", True), "Final")
        self.assertEqual(action_api.strip_think("plan.</think>Final", True), "Final")


class TestAnswerFamily(unittest.TestCase):
    """The server says which family answered; the app picks the rule from it."""

    class Resp:
        status_code = 200

        def __init__(self, lines, family=""):
            self.headers = {"Content-Type": "text/event-stream", "X-Answer-Family": family}
            self.lines = list(lines)

        def iter_lines(self, *a, **k):
            return iter(self.lines)

        def close(self):
            pass

    def run_stream(self, lines, family):
        from unittest.mock import patch
        seen = []
        with patch.object(action_api.requests, "post", return_value=self.Resp(lines, family)):
            out = action_api.run_managed_action_stream("q", "live_assist", "T", seen.append)
        return out

    LINES = [b'data: {"choices":[{"delta":{"content":"They want 40k.</think>"}}]}',
             b'data: {"choices":[{"delta":{"content":"Say: 40k."}}]}', b"data: [DONE]"]

    def test_deepseek_answer_is_cleaned(self):
        self.assertEqual(self.run_stream(self.LINES, "deepseek"), "Say: 40k.")

    def test_gemini_answer_is_left_alone(self):
        self.assertEqual(self.run_stream(self.LINES, ""), "They want 40k.</think>Say: 40k.")

    def test_thinking_only_is_a_clear_error(self):
        from unittest.mock import patch
        lines = [b'data: {"choices":[{"delta":{"content":"<think>long plan"}}]}', b"data: [DONE]"]
        with patch.object(action_api.requests, "post", return_value=self.Resp(lines, "")):
            with self.assertRaises(action_api.ActionAPIError) as ctx:
                action_api.run_managed_action_stream("q", "live_assist", "T", lambda d: None)
        self.assertEqual(str(ctx.exception), action_api.THOUGHT_ONLY)


class TestStreamRobustness(unittest.TestCase):
    """Brings its own fake `requests` (other test modules stub the real one)."""

    class FakeRequests:
        class RequestException(Exception):
            pass

        def __init__(self, resp):
            self.resp = resp

        def post(self, *a, **k):
            return self.resp

    class Resp:
        status_code = 200
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self, raw_lines, fail_after=None, exc=None):
            self.raw_lines, self.fail_after, self.exc = raw_lines, fail_after, exc
            self.closed = False

        def iter_lines(self, *a, **k):
            for i, line in enumerate(self.raw_lines):
                if self.fail_after is not None and i == self.fail_after:
                    raise self.exc("Response ended prematurely")
                yield line

        def close(self):
            self.closed = True

    def run_stream(self, resp):
        from unittest.mock import patch
        fake = self.FakeRequests(resp)
        with patch.object(action_api, "requests", fake):
            return action_api.run_managed_action_stream("q", "live_assist", "T", lambda d: None)

    def test_a_dropped_connection_keeps_the_partial_answer(self):
        resp = self.Resp([b'data: {"choices":[{"delta":{"content":"The answer is "}}]}',
                          b'data: {"choices":[{"delta":{"content":"forty"}}]}',
                          b'data: {"choices":[{"delta":{"content":"-two"}}]}'],
                         fail_after=2, exc=self.FakeRequests.RequestException)
        self.assertEqual(self.run_stream(resp), "The answer is forty")
        self.assertTrue(resp.closed)

    def test_a_dropped_connection_before_any_text_is_a_plain_error(self):
        resp = self.Resp([b"data: x"], fail_after=0, exc=self.FakeRequests.RequestException)
        with self.assertRaises(action_api.ActionAPIError) as ctx:
            self.run_stream(resp)
        self.assertIn("interrupted", str(ctx.exception))

    def test_unicode_line_separators_inside_json_survive(self):
        # JSON leaves U+2028/U+2029/U+0085 unescaped; splitting decoded text
        # on them cut the event in two and dropped its words. requests'
        # iter_lines() splits raw bytes with bytes.splitlines(), as here.
        import json as _json
        texts = ["Line one", " ", "Next sentence with para break\u0085", " end."]
        raw = b"".join(b"data: " + _json.dumps({"choices": [{"delta": {"content": t}}]},
                                               ensure_ascii=False).encode("utf-8") + b"\n\n"
                       for t in texts) + b"data: [DONE]\n\n"
        self.assertEqual(self.run_stream(self.Resp(raw.splitlines())), "".join(texts).strip())


class TestTextOnlyModels(unittest.TestCase):
    """A model that refuses the screenshot still answers - from the
    conversation, without a note claiming a screenshot is attached."""

    class FakeRequests:
        class RequestException(Exception):
            pass

        def __init__(self, responses):
            self.responses, self.bodies = list(responses), []

        def post(self, url, **kw):
            self.bodies.append(kw.get("json"))
            return self.responses.pop(0)

    class Resp:
        def __init__(self, status, payload=None, lines=()):
            self.status_code, self.payload, self.lines = status, payload, list(lines)
            self.headers = {"Content-Type": "text/event-stream" if lines else "application/json"}

        def json(self):
            return self.payload

        def iter_lines(self, *a, **k):
            return iter(self.lines)

        def close(self):
            pass

    CFG = {"action_api_provider": action_api.PROVIDER_OPENAI, "action_api_key": "wk-1.ws-2",
           "action_api_base_url": "https://ws--deepseek.modal.run/v1",
           "action_api_model": "deepseek-ai/DeepSeek-V4-Flash-0731", "_image_png_b64": "/9j/AAAA"}

    def setUp(self):
        action_api._NO_VISION.clear()
        self.addCleanup(action_api._NO_VISION.clear)
    TEXT = ("Conversation (latest part):\nWhat does this error mean?\n\n"
            "(A screenshot of the user's screen is attached. Use it as context; solve what it "
            "shows only when that is what is being asked or nothing was asked.)")

    def test_byo_stream_retries_without_the_screenshot(self):
        from unittest.mock import patch
        fake = self.FakeRequests([
            self.Resp(400, {"object": "error", "message": "This model does not support image input"}),
            self.Resp(200, lines=[b'data: {"choices":[{"delta":{"content":"It means X."}}]}',
                                  b"data: [DONE]"])])
        status = {}
        with patch.object(action_api, "requests", fake):
            out = action_api.run_action_stream(self.TEXT, "live_assist",
                                               dict(self.CFG, _image_status=status), lambda d: None)
        self.assertEqual(out, "It means X.")
        first, second = (json.dumps(b) for b in fake.bodies)
        self.assertIn("image_url", first)
        self.assertNotIn("image_url", second)
        self.assertNotIn("screenshot of the user's screen is attached", second)
        self.assertIn("cannot see the user's screen", second)       # no made-up screen
        self.assertIn("What does this error mean?", second)
        self.assertEqual(status, {"dropped": True})                  # the card won't say "screen seen"

    def test_a_refusal_is_remembered(self):
        from unittest.mock import patch
        ok = lambda: self.Resp(200, lines=[b'data: {"choices":[{"delta":{"content":"A"}}]}',
                                           b"data: [DONE]"])
        fake = self.FakeRequests([self.Resp(400, {"message": "no images"}), ok(), ok()])
        with patch.object(action_api, "requests", fake):
            action_api.run_action_stream(self.TEXT, "live_assist", dict(self.CFG), lambda d: None)
            status = {}
            action_api.run_action_stream(self.TEXT, "live_assist",
                                         dict(self.CFG, _image_status=status), lambda d: None)
        self.assertEqual(len(fake.bodies), 3)
        self.assertNotIn("image_url", json.dumps(fake.bodies[2]))     # no second failed upload
        self.assertEqual(status, {"dropped": True})

    def test_byo_non_stream_retries_without_the_screenshot(self):
        from unittest.mock import patch
        fake = self.FakeRequests([
            self.Resp(422, {"detail": "image_url is not supported"}),
            self.Resp(200, {"choices": [{"message": {"content": "Answer"}}]})])
        with patch.object(action_api, "requests", fake):
            out = action_api.run_action(self.TEXT, "live_assist", dict(self.CFG))
        self.assertEqual(out, "Answer")
        self.assertNotIn("image_url", json.dumps(fake.bodies[1]))

    def test_old_vllm_error_shape_is_readable(self):
        resp = self.Resp(400, {"object": "error", "message": "model not found"})
        with self.assertRaises(action_api.ActionAPIError) as ctx:
            action_api._json_or_error(resp)
        self.assertEqual(str(ctx.exception), "model not found")

    def test_without_screenshot_keeps_everything_else(self):
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": self.TEXT}]
        out = action_api.without_screenshot(msgs)
        self.assertEqual(out[0], msgs[0])
        self.assertEqual(out[1]["content"], "Conversation (latest part):\nWhat does this error mean?"
                         "\n\n" + action_api._NO_SCREEN_NOTE)
        self.assertIn("attached", msgs[1]["content"])             # the input is untouched


class TestLiveEngineChoice(unittest.TestCase):
    """Live answers need speed and sight: a Pro user must not be stuck on a
    tiny offline model just because it is set for dictation Smart Actions."""

    def setUp(self):
        from unittest.mock import patch
        p = patch.object(actions, "_first_downloaded_local_model", return_value="qwen_tiny")
        p.start()
        self.addCleanup(p.stop)
        self.f = actions.live_engine

    def test_pro_user_with_local_model_gets_the_pro_cloud(self):
        self.assertEqual(self.f("qwen_tiny", {"_managed_token": "T"}), actions.API_MANAGED_ID)

    def test_privacy_mode_keeps_it_local(self):
        self.assertEqual(self.f("qwen_tiny", {"_managed_token": "T", "privacy_mode": True}),
                         "qwen_tiny")
        # Even with the managed engine selected: a local model, never the server.
        self.assertEqual(self.f(actions.API_MANAGED_ID,
                                {"_managed_token": "T", "privacy_mode": True}), "qwen_tiny")
        self.assertEqual(self.f(actions.API_GEMINI_ID,
                                {"google_api_key": "G", "privacy_mode": True}), "qwen_tiny")

    def test_warm_up_sends_nothing_in_privacy_mode(self):
        from unittest.mock import patch
        with patch.object(action_api, "warm_up_managed") as wm, \
             patch.object(action_api, "warm_up") as wu:
            actions.warm_up(actions.API_MANAGED_ID, {"_managed_token": "T", "privacy_mode": True})
        wm.assert_not_called()
        wu.assert_not_called()

    def test_without_pro_the_local_model_stays(self):
        self.assertEqual(self.f("qwen_tiny", {}), "qwen_tiny")

    def test_pro_always_gets_the_pro_cloud(self):
        cfg = {"_managed_token": "T", "google_api_key": "G"}
        self.assertEqual(self.f(actions.API_GEMINI_ID, cfg), actions.API_MANAGED_ID)

    def test_without_pro_a_cloud_engine_with_its_own_key_is_kept(self):
        self.assertEqual(self.f(actions.API_GEMINI_ID, {"google_api_key": "G"}),
                         actions.API_GEMINI_ID)

    def test_unusable_engines_fall_back_to_a_local_model(self):
        self.assertEqual(self.f(actions.API_CEREBRAS_ID, {}), "qwen_tiny")       # no key
        self.assertEqual(self.f(actions.API_MANAGED_ID, {}), "qwen_tiny")        # no Pro token
        self.assertEqual(self.f(actions.RULE_BASED_ID, {}), "qwen_tiny")


class TestManagedStreaming(unittest.TestCase):
    """Pro answers stream through the server; an older server that still
    answers in one JSON piece keeps working."""

    class Resp:
        def __init__(self, status=200, ctype="text/event-stream", lines=(), payload=None):
            self.status_code = status
            self.headers = {"Content-Type": ctype}
            self._lines, self._payload = lines, payload
            self.encoding = None

        def iter_lines(self, decode_unicode=False):
            yield from self._lines

        def json(self):
            return self._payload

    def _run(self, resp):
        from unittest.mock import patch
        tokens = []
        with patch.object(action_api.requests, "post", return_value=resp) as post:
            out = action_api.run_managed_action_stream(
                "Conversation (latest part):\nWhat is 2+2?", "live_assist", "TOKEN",
                tokens.append)
        return out, tokens, post.call_args[1]["json"]

    def test_event_stream_is_relayed_token_by_token(self):
        lines = ['data: {"choices":[{"delta":{"content":"Four"}}]}',
                 'data: {"choices":[{"delta":{"content":"."}}]}', "data: [DONE]"]
        out, tokens, body = self._run(self.Resp(lines=lines))
        self.assertEqual((out, tokens), ("Four.", ["Four", "."]))
        self.assertTrue(body["stream"])
        self.assertEqual(body["mode"], "live_assist")           # server routes live answers

    def test_old_server_json_answer(self):
        out, tokens, _ = self._run(self.Resp(ctype="application/json", payload={"text": "Four."}))
        self.assertEqual((out, tokens), ("Four.", ["Four."]))

    def test_limits_surface_clearly(self):
        with self.assertRaises(action_api.ActionAPIError):
            self._run(self.Resp(status=429, ctype="application/json", payload={"error": "quota"}))

    def test_process_stream_uses_the_streaming_path(self):
        from unittest.mock import patch
        with patch.object(action_api, "run_managed_action_stream", return_value="ok") as s:
            out = actions.process_stream("Conversation (latest part):\nhi?",
                                         actions.ACTION_LIVE_ASSIST, lambda d: None,
                                         model=actions.API_MANAGED_ID,
                                         config={"_managed_token": "T"})
        self.assertEqual(out, "ok")
        s.assert_called_once()


class TestStreamingRobustness(unittest.TestCase):
    def test_sse_lines_decode_utf8_without_charset(self):
        # requests defaults text/* without a charset to ISO-8859-1, which
        # garbles every non-ASCII token; event streams are UTF-8 by spec.
        body = ('data: {"choices":[{"delta":{"content":"Բարեւ ձեզ — привет"}}]}\n\n'
                'data: [DONE]\n').encode("utf-8")

        class FakeResp:
            # Mimics requests: decode_unicode uses .encoding, which requests
            # sets to ISO-8859-1 for text/* responses lacking a charset.
            encoding = "ISO-8859-1"

            def iter_lines(self, decode_unicode=False):
                for line in body.split(b"\n"):
                    yield line.decode(self.encoding) if decode_unicode else line

        resp = FakeResp()
        lines = list(action_api._sse_data_lines(resp))
        self.assertEqual(resp.encoding, "utf-8")
        self.assertIn("Բարեւ ձեզ — привет", lines[0])
        self.assertEqual(lines[1], "[DONE]")

    def test_process_cloud_branch_uses_google_key_for_gemini(self):
        # The live recap routes through process(); a Gemini engine running on
        # the speech Google key must not be rejected for a missing action key.
        from unittest.mock import patch
        seen = {}
        def fake_run(text, mode, api_config, source_lang="auto", target_lang="en"):
            seen.update(api_config)
            return "- recap"
        with patch.object(action_api, "run_action", fake_run):
            out = actions.process("Conversation (latest part):\nhi", actions.ACTION_LIVE_RECAP,
                                  model=actions.API_GEMINI_ID,
                                  config={"google_api_key": "G-KEY", "action_api_key": ""})
        self.assertEqual(out, "- recap")
        self.assertEqual(seen.get("action_api_key"), "G-KEY")


class TestMistralEngine(unittest.TestCase):
    def test_mistral_is_registered_with_speech_key_fallback(self):
        info = actions.ACTION_MODELS[actions.API_MISTRAL_ID]
        self.assertEqual(info["provider"], action_api.PROVIDER_MISTRAL)
        d = action_api.defaults(action_api.PROVIDER_MISTRAL)
        self.assertEqual(d["default_base_url"], "https://api.mistral.ai/v1")
        cfg = {"mistral_api_key": "M-KEY", "action_api_key": ""}
        api_cfg = actions.cloud_api_config(actions.API_MISTRAL_ID, cfg)
        self.assertEqual(api_cfg["action_api_key"], "M-KEY")
        self.assertEqual(api_cfg["action_api_provider"], action_api.PROVIDER_MISTRAL)
        self.assertTrue(actions.engine_has_key(actions.API_MISTRAL_ID, cfg))
        self.assertFalse(actions.engine_has_key(actions.API_MISTRAL_ID, {}))
        # A dedicated action key wins over the speech key.
        self.assertEqual(actions.cloud_api_config(actions.API_MISTRAL_ID,
                                                  {"mistral_api_key": "M", "action_api_key": "A"})["action_api_key"], "A")
        # Mistral rejects unknown fields: no reasoning_effort for its models.
        self.assertIsNone(action_api.reasoning_effort_for("ministral-14b-2512"))

    def test_engine_has_key_for_other_kinds(self):
        self.assertTrue(actions.engine_has_key("qwen_tiny", {}))          # local: keys irrelevant
        self.assertTrue(actions.engine_has_key(actions.API_GEMINI_ID, {"google_api_key": "G"}))
        self.assertFalse(actions.engine_has_key(actions.API_CEREBRAS_ID, {"mistral_api_key": "M"}))

    def test_mistral_ocr_request_shape_and_failure(self):
        from unittest.mock import MagicMock, patch
        ok = MagicMock(status_code=200)
        ok.json.return_value = {"pages": [{"markdown": "Quarterly numbers"}, {"markdown": "42% growth"}]}
        with patch.object(action_api.requests, "post", return_value=ok) as post:
            text = action_api.mistral_ocr("AAAA", "M-KEY")
        self.assertEqual(text, "Quarterly numbers\n\n42% growth")
        body = post.call_args[1]["json"]
        self.assertEqual(body["model"], "mistral-ocr-latest")
        self.assertEqual(body["document"]["type"], "image_url")
        self.assertTrue(body["document"]["image_url"].startswith("data:image/png;base64,AAAA"))
        bad = MagicMock(status_code=401)
        with patch.object(action_api.requests, "post", return_value=bad):
            self.assertEqual(action_api.mistral_ocr("AAAA", "M-KEY"), "")
        self.assertEqual(action_api.mistral_ocr("AAAA", ""), "")
