"""Custom vocabulary must reach every backend, and must stay local when asked.

Pure stdlib - no Qt/numpy stubs needed.
"""
import unittest

import vocabulary as v


class TestNormalize(unittest.TestCase):
    def test_list_input(self):
        self.assertEqual(v.normalize_terms(["Aram", "Aibuben"]), ["Aram", "Aibuben"])

    def test_free_text_input(self):
        self.assertEqual(v.normalize_terms("Aram, Aibuben\nPySide6"),
                         ["Aram", "Aibuben", "PySide6"])

    def test_dedupes_case_insensitively_keeping_first(self):
        self.assertEqual(v.normalize_terms(["Aram", "ARAM"]), ["Aram"])

    def test_drops_overlong_terms(self):
        self.assertEqual(v.normalize_terms(["x" * 51]), [])
        self.assertEqual(v.normalize_terms(["one two three four five six seven"]), [])

    def test_keeps_short_phrases(self):
        self.assertEqual(v.normalize_terms(["New York City"]), ["New York City"])

    def test_collapses_internal_whitespace(self):
        self.assertEqual(v.normalize_terms(["  Py   Side  "]), ["Py Side"])

    def test_caps_term_count(self):
        self.assertEqual(len(v.normalize_terms([f"t{i}" for i in range(500)])),
                         v.MAX_TERMS)

    def test_empty_and_junk(self):
        self.assertEqual(v.normalize_terms(None), [])
        self.assertEqual(v.normalize_terms(""), [])
        self.assertEqual(v.normalize_terms(["", "  "]), [])


class TestLoadTerms(unittest.TestCase):
    def test_structured_key_wins(self):
        cfg = {"vocabulary": ["New"], "initial_prompt": "Old"}
        self.assertEqual(v.load_terms(cfg), ["New"])

    def test_falls_back_to_legacy_prompt(self):
        self.assertEqual(v.load_terms({"initial_prompt": "Aram, Aibuben"}),
                         ["Aram", "Aibuben"])

    def test_empty_config(self):
        self.assertEqual(v.load_terms({}), [])
        self.assertEqual(v.load_terms(None), [])


class TestWhisperPrompt(unittest.TestCase):
    def test_renders_glossary(self):
        self.assertEqual(v.whisper_prompt({"vocabulary": ["Aram", "PySide6"]}),
                         "Glossary: Aram, PySide6.")

    def test_none_when_empty(self):
        self.assertIsNone(v.whisper_prompt({}))

    def test_truncates_on_a_term_boundary(self):
        out = v.whisper_prompt({"vocabulary": [f"term{i:03d}" for i in range(100)]})
        self.assertLessEqual(len(out), v.MAX_PROMPT_CHARS)
        self.assertTrue(out.endswith("."))
        # never cut mid-term
        body = out[len("Glossary: "):-1]
        for term in body.split(", "):
            self.assertRegex(term, r"^term\d{3}$")

    def test_not_gated_by_privacy_mode(self):
        # Local Whisper never leaves the machine, so privacy mode must not
        # degrade local accuracy.
        cfg = {"vocabulary": ["Aram"], "privacy_mode": True}
        self.assertEqual(v.whisper_prompt(cfg), "Glossary: Aram.")


class TestCloudGating(unittest.TestCase):
    def setUp(self):
        self.cfg = {"vocabulary": ["Aram", "Aibuben"]}

    def test_hint_present_by_default(self):
        self.assertIn("Aram", v.cloud_transcription_hint(self.cfg))

    def test_hint_blocked_by_privacy_mode(self):
        cfg = {**self.cfg, "privacy_mode": True}
        self.assertEqual(v.cloud_transcription_hint(cfg), "")
        self.assertEqual(v.cloud_terms(cfg), [])
        self.assertEqual(v.spelling_authority_block(cfg), "")

    def test_hint_blocked_by_opt_out(self):
        cfg = {**self.cfg, "vocabulary_share_with_cloud": False}
        self.assertEqual(v.cloud_transcription_hint(cfg), "")
        self.assertEqual(v.cloud_terms(cfg), [])
        self.assertEqual(v.spelling_authority_block(cfg), "")

    def test_empty_when_no_terms(self):
        self.assertEqual(v.cloud_transcription_hint({}), "")
        self.assertEqual(v.spelling_authority_block({}), "")

    def test_spelling_block_lists_terms(self):
        block = v.spelling_authority_block(self.cfg)
        self.assertIn("Aram, Aibuben", block)


class TestLooksLikeTermList(unittest.TestCase):
    def test_comma_or_newline_separated_is_a_list(self):
        self.assertTrue(v.looks_like_term_list("Aram, Aibuben"))
        self.assertTrue(v.looks_like_term_list("Aram\nAibuben"))
        self.assertTrue(v.looks_like_term_list("Aram; Aibuben"))

    def test_short_single_term_is_a_list(self):
        self.assertTrue(v.looks_like_term_list("PySide6"))
        self.assertTrue(v.looks_like_term_list("New York City"))

    def test_prose_is_not_a_list(self):
        self.assertFalse(v.looks_like_term_list("my hand written prompt"))
        self.assertFalse(
            v.looks_like_term_list("The speaker is discussing quarterly results"))

    def test_empty(self):
        self.assertFalse(v.looks_like_term_list(""))
        self.assertFalse(v.looks_like_term_list(None))


class TestSyncConfig(unittest.TestCase):
    def test_mirrors_initial_prompt(self):
        cfg = {"vocabulary": ["Aram"], "initial_prompt": ""}
        v.sync_config(cfg)
        self.assertEqual(cfg["initial_prompt"], "Glossary: Aram.")

    def test_preserves_legacy_prose_when_list_empty(self):
        cfg = {"vocabulary": [], "initial_prompt": "My hand written prompt"}
        v.sync_config(cfg)
        self.assertEqual(cfg["initial_prompt"], "My hand written prompt")

    def test_idempotent(self):
        cfg = {"vocabulary": ["Aram"]}
        v.sync_config(cfg)
        once = cfg["initial_prompt"]
        v.sync_config(cfg)
        self.assertEqual(cfg["initial_prompt"], once)

    def test_tolerates_non_dict(self):
        v.sync_config(None)   # must not raise
        v.sync_config("nope")


if __name__ == "__main__":
    unittest.main()
