"""Live Assist: context building, mode plumbing, no-AI fallback, glass helpers.

Pure logic here; the overlay widget is exercised by offscreen smokes and the
in-session probe. Qt-dependent parts are guarded like test_file_transcribe.
"""
import sys
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
        self.assertIn("copilot", cloud[-1]["content"])
        self.assertIn("They're asking", cloud[-1]["content"])
        local = local_llm._messages_for("live_assist", "hi?", "auto", "en")
        self.assertIn("copilot", local[-1]["content"])
        self.assertLess(len(local[-1]["content"]), len(cloud[-1]["content"]),
                        "local prompt must stay tighter than the cloud one")

    def test_token_budgets_are_short(self):
        # It's read at a glance mid-conversation; a wall of text is a failure.
        self.assertLessEqual(action_api._max_tokens_for("live_assist"), 400)
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


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestRollingContext(unittest.TestCase):
    def setUp(self):
        from ui.live_assist import rolling_context, TAIL_CHARS
        self.rc, self.tail = rolling_context, TAIL_CHARS

    def test_trims_to_tail_at_sentence_boundary(self):
        text = ("Old stuff nobody needs. " * 400) + "Recent point. Final question?"
        ctx = self.rc(text)
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
        self.assertEqual(labels[0], "Say next")
        self.assertEqual(self.la.QUICK_ACTIONS[0][1], "")     # default = plain Suggest
        self.assertTrue(all(q for _, q in self.la.QUICK_ACTIONS[1:]))


class TestImagePlumbing(unittest.TestCase):
    MSGS = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "example"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "the real question"}]

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
