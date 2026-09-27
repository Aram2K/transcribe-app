"""Custom vocabulary must reach every backend, and must stay local when asked.

Pure stdlib - no Qt/numpy stubs needed.
"""
import unittest

import vocabulary as v


class TestVocabularyHonesty(unittest.TestCase):
    """A term may fix the spelling of something actually said - never put
    itself where it wasn't (the user's "Aibuben, Doctolib" everywhere bug)."""

    TERMS = ["Aibuben", "Doctolib"]

    def test_evidence_check_flags_invented_terms(self):
        f = v.unsupported_terms
        self.assertEqual(f("I went to the Doctolib.", "I went to the doctor.", self.TERMS),
                         ["Doctolib"])
        self.assertEqual(f("Call Aibuben tomorrow.", "Call a boy tomorrow.", self.TERMS),
                         ["Aibuben"])
        self.assertEqual(f("Aibuben, Doctolib.", "Thank you.", self.TERMS),
                         ["Aibuben", "Doctolib"])                       # prompt echo

    def test_evidence_check_keeps_terms_really_said(self):
        f = v.unsupported_terms
        self.assertEqual(f("Book it on Doctolib.", "Book it on doctor lib.", self.TERMS), [])
        self.assertEqual(f("We met at Aibuben.", "We met at Ay Buben.", self.TERMS), [])
        # Said in Armenian: the unprompted decode wrote it in Armenian script.
        self.assertEqual(f("Aibuben is growing.", "Այբուբենը մեծանում է։", self.TERMS), [])
        self.assertEqual(f("Nothing here.", "Nothing here.", self.TERMS), [])

    def test_spelling_fix_never_inserts(self):
        c = lambda t: v.correct_spellings(t, self.TERMS)
        # The term plus an ending is left alone: from the text alone it can't
        # be told from a real word form ("the institute" for "Institut").
        self.assertEqual(c("Book it on doctolibe please"), "Book it on doctolibe please")
        # A title-case term never re-cases a lowercase word: from the text alone
        # "doctolib" can't be told from an ordinary word ("will" for "Will").
        self.assertEqual(c("a doctolib appointment"), "a doctolib appointment")
        self.assertEqual(c("aibuben's office"), "aibuben's office")
        self.assertEqual(c("two doctolibs"), "two doctolibs")
        # Ordinary words and phrases that merely resemble a term are left alone.
        self.assertEqual(c("Doctor lib is open"), "Doctor lib is open")
        self.assertEqual(c("the doctor said so"), "the doctor said so")
        self.assertEqual(c("I told the doctors."), "I told the doctors.")
        self.assertEqual(c(""), "")

    def test_short_terms_only_get_their_casing_fixed(self):
        # "Api" is how plain writing capitalises any word (a sentence start), so
        # it stays - the same rule keeps "It works." from becoming "IT works.".
        self.assertEqual(v.correct_spellings("the Api and the api", ["API"]),
                         "the Api and the api")
        self.assertEqual(v.correct_spellings("call the aPI", ["API"]), "call the API")
        self.assertEqual(v.correct_spellings("the ape", ["API"]), "the ape")

    def test_real_words_are_never_replaced(self):
        # From the review: every one of these used to be rewritten.
        cases = [
            (["Teams"], "our team is great"), (["Rust"], "I trust you, a bit rusty"),
            (["Unity"], "run the unit tests"), (["Stripe"], "strip the whitespace"),
            (["Adam"], "Yes, madam. They built a dam"), (["Aram"], "pass the param to a ram"),
            (["JSON"], "Jason from sales"), (["Mistral"], "a mistrial"),
            (["GPT-4"], "GPT-4o and GPT-4.1"), (["Windows 10"], "upgrade to Windows 11"),
            (["Python 3.12"], "Python 3.11"), (["LinkedIn"], "It's linked in the doc"),
            (["Outlook"], "Check it out. Look at this"), (["AI"], "plan A. I think so"),
            (["C++"], "Plan C, vitamin C."), ([".NET"], "net income; Нет, я не приду"),
            (["IT"], "it's raining, its tail"), (["US"], "tell us more"),
            (["Don"], "I don't know"),
        ]
        for terms, text in cases:
            self.assertEqual(v.correct_spellings(text, terms), text, (terms, text))

    def test_similar_terms_do_not_swap(self):
        self.assertEqual(v.correct_spellings("Eric called Erica", ["Eric", "Erica"]),
                         "Eric called Erica")
        self.assertEqual(v.correct_spellings("Julian and Julia", ["Julia", "Julian"]),
                         "Julian and Julia")

    def test_other_scripts_are_left_alone(self):
        self.assertEqual(v.correct_spellings("Արամը ասաց, Արամի գիրքը", ["Aram"]),
                         "Արամը ասաց, Արամի գիրքը")
        self.assertEqual(v.correct_spellings("Ивана нет дома", ["Ivan"]), "Ивана нет дома")
        self.assertEqual(v.correct_spellings("Այբուբենը մեծանում է", ["Այբուբեն"]),
                         "Այբուբենը մեծանում է")          # native term keeps its ending

    def test_split_numbers_are_rejoined_without_duplicates(self):
        self.assertEqual(v.correct_spellings("pyside 6", ["PySide6"]), "PySide6")
        self.assertEqual(v.correct_spellings("the qwen 3 model", ["Qwen3"]), "the Qwen3 model")
        self.assertEqual(v.correct_spellings("GPT-4 o", ["GPT-4o"]), "GPT-4 o")

    def test_evidence_trigger_ignores_everyday_words(self):
        self.assertFalse(v.evidence_trigger("I think it works", ["IT"]))
        self.assertTrue(v.evidence_trigger("the IT team", ["IT"]))
        # A decode copying the glossary spells the term as the glossary does;
        # a lowercase "doctolib" is not that, and "will" or "notion" matched
        # case-blind would cost a second decode on nearly every sentence.
        self.assertTrue(v.evidence_trigger("book on Doctolib", ["Doctolib"]))
        self.assertFalse(v.evidence_trigger("book on doctolib", ["Doctolib"]))
        self.assertFalse(v.evidence_trigger("I will send it, will you?", ["Will"]))
        self.assertFalse(v.evidence_trigger("a good notion, some slack", ["Notion", "Slack"]))
        self.assertFalse(v.evidence_trigger("Это максимум, читаю роман", ["Максим", "Роман"]))
        self.assertTrue(v.evidence_trigger("Ask Notion AI", ["Notion"]))
        self.assertTrue(v.evidence_trigger("Kubectl apply it", ["kubectl"]))   # sentence start

    def test_uncomparable_script_is_not_evidence_against(self):
        self.assertEqual(v.unsupported_terms("Book it on Doctolib.", "Κλείσε στο Ντοκτολίμπ",
                                             ["Doctolib"]), [])
        self.assertEqual(v.unsupported_terms("Aibuben, Doctolib.", "", self.TERMS), self.TERMS)

    def test_wording_fixes_spelling_only(self):
        cfg = {"vocabulary": self.TERMS}
        hint = v.cloud_transcription_hint(cfg)
        self.assertNotIn("similar-sounding", hint)
        self.assertIn("Never add them", hint)
        self.assertIn("never add one", v.spelling_authority_block(cfg))


class TestOrdinaryWordsKeepTheirCase(unittest.TestCase):
    """correct_spellings runs on every segment: a term that is also a word
    must never re-case that word (review of 1.9.1)."""

    def test_short_all_caps_terms_leave_normal_capitalisation(self):
        for terms, text in [(["IT"], "It works."), (["AM"], "Am I late?"), (["US"], "Us too."),
                            (["OR"], "Or not."), (["API"], "Api docs"), (["IT"], "it is")]:
            self.assertEqual(v.correct_spellings(text, terms), text, (terms, text))

    def test_short_terms_fix_casing_plain_writing_never_produces(self):
        self.assertEqual(v.correct_spellings("the iT team", ["IT"]), "the IT team")
        self.assertEqual(v.correct_spellings("call the aPI", ["API"]), "call the API")

    def test_title_case_terms_leave_words_alone(self):
        cases = [
            (["Will"], "I will send it, will you?"),
            (["Notion", "Slack", "Zoom", "Word", "Hope", "Mark"],
             "a good notion, some slack, zoom in, the word. I hope so, mark it."),
            (["Максим", "Роман"], "Это максимум, читаю роман."),   # no "up to 2 letters" match
            (["Максим", "Роман"], "Максимум. Романа нет."),
            (["Գոհար"], "գոհար"),
            (["Transcription"], "transcriptions and transcriptional"),
            (["Microsoft"], "microsofts"),
        ]
        for terms, text in cases:
            self.assertEqual(v.correct_spellings(text, terms), text, (terms, text))

    def test_distinctive_casing_is_still_fixed(self):
        cases = [
            (["PySide6"], "pyside6's docs", "PySide6's docs"),
            (["GitHub"], "push to github", "push to GitHub"),
            (["iPhone"], "two iphones", "two iPhones"),
            (["iPhone"], "an Iphone", "an iPhone"),
            (["JSON"], "a json file", "a JSON file"),
            (["NASA"], "nasa said", "NASA said"),
        ]
        for terms, text, want in cases:
            self.assertEqual(v.correct_spellings(text, terms), want, (terms, text))

    def test_sentence_start_capital_is_left_alone(self):
        self.assertEqual(v.correct_spellings("E-mail me", ["e-mail"]), "E-mail me")


class TestNearMissLeavesOtherForms(unittest.TestCase):
    """A near-miss snap fixes a misspelling - never an inflection of the term
    or a different name that happens to look close."""

    def test_russian_case_endings_stay(self):
        cases = [
            (["Екатерина", "Анастасия"], "Я позвонил Екатерине и Анастасии вчера."),
            (["Маргарита", "Елизавета"], "Я видел Маргариту и Елизавету."),
            (["Швейцария", "Калифорния"], "в Швейцарии и в Калифорнии"),
            (["Лаборатория"], "из Лаборатории"),
            (["Лаборатория"], "лабораторный журнал"),
            (["Александр"], "Я видел Александра и Александру."),
        ]
        for terms, text in cases:
            self.assertEqual(v.correct_spellings(text, terms), text, (terms, text))

    def test_english_endings_and_other_names_stay(self):
        cases = [
            (["Alexandra"], "Alexander called."),
            (["Christina"], "Christine called."),
            (["Stephanie"], "Stephane called."),
            (["Transcribe"], "I transcribed the audio, the transcriber is transcribing."),
        ]
        for terms, text in cases:
            self.assertEqual(v.correct_spellings(text, terms), text, (terms, text))

    def test_real_misspellings_are_still_fixed(self):
        self.assertEqual(v.correct_spellings("Екатирина пришла", ["Екатерина"]),
                         "Екатерина пришла")
        self.assertEqual(v.correct_spellings("Alexamdra called.", ["Alexandra"]),
                         "Alexandra called.")
        self.assertEqual(v.correct_spellings("we run kubernetis here", ["Kubernetes"]),
                         "we run Kubernetes here")
        # Brand terms that start lowercase are never capitalised, even at a
        # sentence start; everyday words keep a sentence-start capital.
        for term, text, want in [("iPhone", "IPhone is here.", "iPhone is here."),
                                 ("macOS", "MacOS update", "macOS update"),
                                 ("eBay", "EBay order", "eBay order"),
                                 ("e-mail", "E-mail me", "E-mail me")]:
            self.assertEqual(v.correct_spellings(text, [term]), want)

    def test_english_word_forms_of_a_term_are_left_alone(self):
        for term, text in [("Director", "Open the directory now."),
                           ("Institut", "the institute is big"),
                           ("Transcriber", "please transcribe this"),
                           ("Solutions", "a solution works"),
                           ("Networks", "the network is down"),
                           ("Analytics", "an analytic approach"),
                           ("Transcription", "the transcriptionist")]:
            self.assertEqual(v.correct_spellings(text, [term]), text, term)


class TestReconcilePrompted(unittest.TestCase):
    """The glossary-prompted decode against an unprompted one of the same
    audio: keep every term that was really said - however the plain decode
    spelled it - and undo only the ones the prompt wrote in."""

    # (term, prompted decode, how Whisper hears it unprompted)
    SAID = [
        ("Hakobyan", "Ask Hakobyan to call me.", "Ask Akopian to call me."),
        ("Sargsyan", "Mr. Sargsyan will join us.", "Mr. Sarkisian will join us."),
        ("Ghazaryan", "Ghazaryan sent the report.", "Kazarian sent the report."),
        ("Mkrtchyan", "Thanks to Anna Mkrtchyan for this.", "Thanks to Anna Mekerchian for this."),
        ("Claude", "I asked Claude about it.", "I asked cloud about it."),
        ("kubectl", "Run kubectl apply on the cluster.", "Run cube control apply on the cluster."),
        ("PySide6", "The PySide6 build is green.", "The pie side six build is green."),
        ("Doctolib", "Book it on Doctolib please.", "Book it on doctor leap please."),
    ]

    def test_terms_really_said_are_kept(self):
        for term, prompted, plain in self.SAID:
            self.assertEqual(v.reconcile_prompted(prompted, plain, [term]), prompted, term)
            self.assertEqual(v.unsupported_terms(prompted, plain, [term]), [], term)

    def test_several_terms_in_one_decode(self):
        terms = ["kubectl", "PySide6"]
        prompted = "Run kubectl apply on the PySide6 build."
        self.assertEqual(v.reconcile_prompted(
            prompted, "Run cube control apply on the pie side 6 build.", terms), prompted)

    def test_invented_terms_are_replaced_by_what_was_said(self):
        terms = ["Aibuben", "Doctolib", "Hakobyan", "Claude"]
        cases = [
            ("I went to the Doctolib.", "I went to the doctor.", "I went to the doctor."),
            ("Call Aibuben tomorrow.", "Call a boy tomorrow.", "Call a boy tomorrow."),
            ("Ask Hakobyan to call.", "Ask a colleague to call.", "Ask a colleague to call."),
            ("Claude review is done.", "Code review is done.", "Code review is done."),
            # Nothing similar was said there at all: the term just goes.
            ("See you tomorrow Hakobyan.", "See you tomorrow.", "See you tomorrow."),
            ("Ask Hakobyan, please.", "Ask, please.", "Ask, please."),
        ]
        for prompted, plain, want in cases:
            self.assertEqual(v.reconcile_prompted(prompted, plain, terms), want, prompted)

    def test_only_the_invented_term_is_replaced(self):
        # "Hakobyan" was said (heard as "Akopian") and keeps its spelling;
        # "Doctolib" was not, and becomes the plain decode's "the doctor".
        self.assertEqual(v.reconcile_prompted("Ask Hakobyan to book it on Doctolib.",
                                              "Ask Akopian to book it on the doctor.",
                                              ["Hakobyan", "Doctolib"]),
                         "Ask Hakobyan to book it on the doctor.")
        self.assertEqual(v.reconcile_prompted("Hakobyan is here and Hakobyan left.",
                                              "Akopian is here and the man left.", ["Hakobyan"]),
                         "Hakobyan is here and the man left.")

    def test_prompt_echo_falls_back_to_the_plain_decode(self):
        terms = ["Aibuben", "Doctolib"]
        self.assertEqual(v.reconcile_prompted("Aibuben, Doctolib.", "Thank you.", terms),
                         "Thank you.")
        self.assertEqual(v.reconcile_prompted("Thanks, Doctolib.", "Thanks.", terms), "Thanks.")
        self.assertEqual(v.reconcile_prompted("Doctolib.", "", terms), "")
        # The two decodes share nothing else: no splicing into another text.
        self.assertEqual(v.reconcile_prompted("Call Doctolib tomorrow.", "Позвони завтра.", terms),
                         "Позвони завтра.")

    def test_nothing_to_judge_keeps_the_prompted_decode(self):
        prompted = " Book it on Doctolib."
        self.assertIs(v.reconcile_prompted(prompted, " Book it on doctor lib.", ["Doctolib"]),
                      prompted)
        self.assertEqual(v.reconcile_prompted("Nothing here.", "Nothing here.", ["Doctolib"]),
                         "Nothing here.")
        # A script the check can't compare proves nothing either way.
        self.assertEqual(v.reconcile_prompted("Book it on Doctolib.", "Κλείσε στο Ντοκτολίμπ",
                                              ["Doctolib"]), "Book it on Doctolib.")
        self.assertEqual(v.reconcile_prompted("Aibuben is growing.", "Այբուբենը մեծանում է։",
                                              ["Aibuben"]), "Aibuben is growing.")


class TestSpellingSpeed(unittest.TestCase):
    def test_big_vocabulary_on_a_long_segment(self):
        # 100 terms x 400 words took ~2.5 s per call before the per-token
        # precompute and pre-filter; now well under 100 ms. The bound is loose
        # for slow machines but still catches a return to per-term scanning.
        import time
        firsts = ["Alice", "Bernard", "Camille", "Dominique", "Emmanuel", "Guillaume",
                  "Isabelle", "Laurent", "Mathilde", "Nicolas", "Olivier", "Sebastien"]
        lasts = ["Martin", "Dubois", "Thomas", "Robert", "Richard", "Durand", "Leroy",
                 "Moreau", "Simon", "Lefebvre", "Garcia", "Bertrand"]
        terms = ["Doctolib", "Supabase", "Cerebras", "Mistral", "Ministral", "DeepSeek",
                 "Kubernetes", "PostgreSQL", "PyInstaller", "Anthropic", "PySide6", "Qwen3"]
        terms += ["%s %s" % (f, l) for f in firsts for l in lasts][:100 - len(terms)]
        words = ("so I was talking with the team yesterday about the new release and we "
                 "agreed that the supabase functions need another look before we ship, "
                 "especially the analytics pipeline. Then Camille mentioned that kubernetis "
                 "cluster is still pending.").split()
        text = " ".join((words * 20)[:400])
        start = time.perf_counter()
        out = v.correct_spellings(text, terms)
        self.assertLess(time.perf_counter() - start, 0.5)
        self.assertIn("Kubernetes cluster", out)


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
