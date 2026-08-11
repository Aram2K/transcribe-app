"""The output-quality pass must remove Whisper's junk and nothing else.

The destructive failure mode is over-deletion, so most of these tests assert
what SURVIVES. Pure stdlib - no Qt/numpy stubs needed.
"""
import unittest

import text_cleanup as tc


def clean(text, **kw):
    return tc.clean(text, options=tc.CleanupOptions(**kw))


class TestArtifacts(unittest.TestCase):
    def test_strips_known_artifacts(self):
        self.assertEqual(clean("[BLANK_AUDIO] hello there"), "hello there")
        self.assertEqual(clean("(music) hello"), "hello")
        self.assertEqual(clean("hello [Applause]"), "hello")
        self.assertEqual(clean("♪♪ hello"), "hello")

    def test_legitimate_parentheses_survive(self):
        # The anti-regression test: a generic bracket regex would destroy these.
        self.assertEqual(clean("the result (about 40%) was fine"),
                         "the result (about 40%) was fine")
        self.assertEqual(clean("call arr[0] first"), "call arr[0] first")
        self.assertEqual(clean("use foo() here"), "use foo() here")
        self.assertEqual(clean("a dict {key: value} literal"),
                         "a dict {key: value} literal")

    def test_artifact_word_in_normal_prose_survives(self):
        # "music" only matches when bracketed.
        self.assertEqual(clean("I love music a lot"), "I love music a lot")


class TestRepeatCollapse(unittest.TestCase):
    def test_collapses_hallucination_loop(self):
        # The actual reported production failure.
        self.assertEqual(clean("Thank you. Thank you. Thank you. Thank you."),
                         "Thank you.")

    def test_two_repeats_are_left_alone(self):
        # Deliberate emphasis, not a decoder loop.
        self.assertEqual(clean("No. No."), "No. No.")

    def test_collapses_long_repeated_sentence(self):
        text = "The meeting is over. " * 4
        self.assertEqual(clean(text), "The meeting is over.")

    def test_distinct_sentences_untouched(self):
        text = "First point. Second point. Third point."
        self.assertEqual(clean(text), text)


class TestHallucinationPhrases(unittest.TestCase):
    def test_drops_boilerplate_among_real_speech(self):
        out = clean("Let's ship the release on Friday. Thanks for watching!")
        self.assertEqual(out, "Let's ship the release on Friday.")

    def test_never_matches_mid_sentence(self):
        text = "Thank you for the update on the migration."
        self.assertEqual(clean(text), text)

    def test_lone_utterance_is_never_removed(self):
        # A single "Thank you." is almost certainly real dictation.
        self.assertEqual(clean("Thank you."), "Thank you.")
        self.assertEqual(clean("Okay"), "Okay")

    def test_contractions_fold_correctly(self):
        # Regression: apostrophes used to fold to a space ("I'll" -> "i ll"),
        # so no contraction phrase in the list ever matched.
        self.assertEqual(tc.fold("I'll see you next time."), "ill see you next time")
        self.assertEqual(tc.fold("I’ll see you next time."), "ill see you next time")

    def test_drops_contraction_boilerplate(self):
        out = clean("The migration is done. I'll see you next time.")
        self.assertEqual(out, "The migration is done.")

    def test_all_boilerplate_becomes_empty(self):
        self.assertEqual(
            clean("Thank you. Thanks for watching. I'll see you next time."), "")

    def test_custom_phrase(self):
        out = clean("Real content here. Boilerplate line.",
                    extra_hallucination_phrases=("boilerplate line",))
        self.assertEqual(out, "Real content here.")


class TestReplacements(unittest.TestCase):
    def test_basic_replacement(self):
        self.assertEqual(clean("i use pyside 6 daily",
                               replacements=(("pyside 6", "PySide6"),)),
                         "i use PySide6 daily")

    def test_longest_match_wins(self):
        pairs = (("new york", "NYC"), ("new york city", "NYC Metro"))
        self.assertEqual(clean("visiting new york city soon", replacements=pairs),
                         "visiting NYC Metro soon")

    def test_no_cascade(self):
        # a->b then b->c must yield b, not c.
        self.assertEqual(clean("a", replacements=(("a", "b"), ("b", "c"))), "b")

    def test_case_shape_preserved_for_lowercase_target(self):
        pairs = (("teh", "the"),)
        self.assertEqual(clean("Teh cat", replacements=pairs), "The cat")
        self.assertEqual(clean("TEH CAT", replacements=pairs), "THE CAT")
        self.assertEqual(clean("teh cat", replacements=pairs), "the cat")

    def test_canonical_target_kept_verbatim(self):
        # A replacement carrying its own capitals is a canonical spelling.
        pairs = (("pyside", "PySide6"),)
        self.assertEqual(clean("PYSIDE rocks", replacements=pairs), "PySide6 rocks")

    def test_word_boundary_not_substring(self):
        pairs = (("cat", "dog"),)
        self.assertEqual(clean("concatenate the cat", replacements=pairs),
                         "concatenate the dog")

    def test_unicode_boundary(self):
        # Cyrillic term must not match inside a longer Cyrillic word.
        pairs = (("да", "yes"),)
        self.assertEqual(clean("да ладно", replacements=pairs), "yes ладно")

    def test_flexible_internal_whitespace(self):
        self.assertEqual(clean("pyside   6 rules",
                               replacements=(("pyside 6", "PySide6"),)),
                         "PySide6 rules")


class TestFillers(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(clean("um so uh yes"), "um so uh yes")

    def test_removed_when_enabled(self):
        self.assertEqual(clean("um so uh yes", remove_fillers=True), "so yes")

    def test_like_is_never_removed(self):
        # "like" is a real verb; removing it would corrupt meaning.
        self.assertEqual(clean("I like it", remove_fillers=True), "I like it")


class TestPromptEcho(unittest.TestCase):
    """Whisper treats initial_prompt as preceding context and continues it on
    quiet audio, spraying the user's vocabulary into the transcript. These are
    the verbatim outputs the real model produced on low-signal input."""

    TERMS = ("Aram", "Aibuben", "PySide6", "Adamyan")

    def clean(self, text):
        return tc.clean(text, options=tc.CleanupOptions(vocabulary_terms=self.TERMS))

    def test_real_echoes_are_removed(self):
        for observed in ("Aibuben, PySide6, Adamyan.",
                         "Aibuben, PySide6",
                         "Glossary, Adamyan.",
                         "Aram, Aibuben, PySide6, Adamyan."):
            self.assertEqual(self.clean(observed), "", f"not stripped: {observed!r}")

    def test_single_term_is_real_dictation(self):
        # Saying one vocabulary word is normal speech - never drop it.
        self.assertEqual(self.clean("PySide6"), "PySide6")
        self.assertEqual(self.clean("Aibuben."), "Aibuben.")

    def test_terms_inside_a_sentence_survive(self):
        for text in ("I use PySide6 and Aibuben every day.",
                     "Aram and Adamyan reviewed the PySide6 migration."):
            self.assertEqual(self.clean(text), text)

    def test_echo_removed_but_real_speech_kept(self):
        out = self.clean("Aibuben, PySide6, Adamyan. Let's ship on Friday.")
        self.assertEqual(out, "Let's ship on Friday.")

    def test_no_terms_configured_is_a_noop(self):
        text = "Aibuben, PySide6, Adamyan."
        self.assertEqual(tc.clean(text, options=tc.CleanupOptions()), text)

    def test_unrelated_lists_are_untouched(self):
        text = "Apples, oranges, bananas."
        self.assertEqual(self.clean(text), text)


class TestLayoutAndSafety(unittest.TestCase):
    def test_preserve_layout_keeps_newlines(self):
        out = clean("Speaker 1: hi\nSpeaker 2: hello", preserve_layout=True)
        self.assertEqual(out, "Speaker 1: hi\nSpeaker 2: hello")

    def test_newlines_folded_by_default(self):
        self.assertEqual(clean("hello\nworld"), "hello world")

    def test_idempotent(self):
        samples = [
            "Thank you. Thank you. Thank you.",
            "the result (about 40%) was fine",
            "[BLANK_AUDIO] hello there",
            "First point. Second point.",
        ]
        for s in samples:
            once = clean(s)
            self.assertEqual(clean(once), once, f"not idempotent for {s!r}")

    def test_empty_and_none_never_raise(self):
        self.assertEqual(clean(""), "")
        self.assertEqual(clean("   "), "")
        self.assertEqual(tc.clean(None, options=tc.CleanupOptions()), "")

    def test_pure_artifact_input_becomes_empty(self):
        self.assertEqual(clean("[BLANK_AUDIO]"), "")

    def test_never_capitalizes_or_adds_punctuation(self):
        # transcribe_only must paste exactly what was said.
        self.assertEqual(clean("hello world"), "hello world")

    def test_does_not_lose_content_it_cannot_explain(self):
        text = "some perfectly ordinary dictation"
        self.assertEqual(clean(text), text)


class TestOptionsFromConfig(unittest.TestCase):
    def test_defaults(self):
        o = tc.options_from_config({})
        self.assertTrue(o.strip_hallucinations)
        self.assertTrue(o.strip_artifacts)
        self.assertFalse(o.remove_fillers)
        self.assertEqual(o.replacements, ())

    def test_reads_replacement_list(self):
        o = tc.options_from_config(
            {"cleanup_replacements": [{"from": "teh", "to": "the"}]})
        self.assertEqual(o.replacements, (("teh", "the"),))

    def test_tolerates_legacy_dict_and_junk(self):
        o = tc.options_from_config({"cleanup_replacements": {"teh": "the"}})
        self.assertEqual(o.replacements, (("teh", "the"),))
        o2 = tc.options_from_config({"cleanup_replacements": ["nonsense", 5, None]})
        self.assertEqual(o2.replacements, ())

    def test_dedupes_sources(self):
        o = tc.options_from_config({"cleanup_replacements": [
            {"from": "teh", "to": "the"}, {"from": "TEH", "to": "other"}]})
        self.assertEqual(len(o.replacements), 1)

    def test_none_config(self):
        self.assertIsInstance(tc.options_from_config(None), tc.CleanupOptions)


class TestReport(unittest.TestCase):
    def test_report_has_counts_only(self):
        _, report = tc.clean_with_report(
            "[BLANK_AUDIO] hi. Thanks for watching!",
            options=tc.CleanupOptions())
        self.assertTrue(all(isinstance(v, int) for v in report.values()))
        self.assertEqual(report["artifacts"], 1)


if __name__ == "__main__":
    unittest.main()
