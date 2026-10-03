"""Mixed-language (code-switching) transcription: speech_langs.py, and how
main.py's engines use it - the Gemini prompt, the Pro cloud request, and the
local Whisper path (detection among the mixed languages, the primer, the
primer-echo guard)."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import speech_langs


class TestSpeechLangs(unittest.TestCase):
    def test_mix_languages_only_from_two_ticked(self):
        # Unset (every config before 1.9.3) or one ticked: no restriction -
        # "multi" keeps detecting any language, as it always did.
        self.assertEqual(speech_langs.mix_languages({}), [])
        self.assertEqual(speech_langs.mix_languages({"mix_languages": ["fr"]}), [])
        self.assertEqual(speech_langs.saved_mix({"mix_languages": ["fr"]}), ["fr"])
        self.assertEqual(speech_langs.mix_languages({"mix_languages": "hy,en"}), ["hy", "en"])
        self.assertEqual(speech_langs.mix_languages({"mix_languages": ["fr", "xx", "hy"]}),
                         ["hy", "fr"])                    # picker order, unknown dropped

    def test_mixed_without_a_mix_names_no_language(self):
        hint = speech_langs.cloud_hint("multi", [])
        self.assertIn("may switch languages, even in the middle of a sentence", hint)
        self.assertNotIn("Armenian", hint)

    def test_one_language_keeps_foreign_words_and_native_script(self):
        hint = speech_langs.cloud_hint("hy")
        self.assertIn("mainly Armenian", hint)
        self.assertIn("Armenian alphabet", hint)
        self.assertIn("never transliterated", hint)
        self.assertIn("don't translate", hint)
        self.assertNotIn("return only", hint)             # the old wording forced translation

    def test_mixed_names_every_language_and_its_script(self):
        hint = speech_langs.cloud_hint("multi", ["hy", "en", "ru", "fr"])
        self.assertIn("switches between Armenian, English, Russian and French", hint)
        self.assertIn("middle of a sentence", hint)
        self.assertIn("Armenian in the Armenian alphabet", hint)
        self.assertIn("Russian in Cyrillic", hint)
        self.assertIn("English and French in Latin letters", hint)
        self.assertIn("Never translate", hint)

    def test_auto_still_keeps_switches(self):
        self.assertIn("keep every word in the language it was spoken",
                      speech_langs.cloud_hint("auto"))

    def test_primer_and_detection_among_the_mix(self):
        primer = speech_langs.whisper_primer(["hy", "en"])
        self.assertIn("Բարև", primer)
        self.assertIn("let's get started", primer)
        probs = [("ka", 0.5), ("hy", 0.3), ("en", 0.2)]    # Armenian heard as Georgian
        self.assertEqual(speech_langs.dominant_among(probs, ["hy", "en"], "ka"), "hy")
        self.assertEqual(speech_langs.dominant_among([("de", 1.0)], ["hy", "en"], "de"), "de")
        self.assertEqual(speech_langs.dominant_among(None, ["hy", "en"], "en"), "en")

    def test_primer_echo_is_recognised_but_real_speech_is_kept(self):
        mix = ["hy", "en", "ru"]
        self.assertTrue(speech_langs.is_primer_echo(speech_langs.whisper_primer(mix), mix))
        two = speech_langs._PRIMER["en"] + " " + speech_langs._PRIMER["ru"]
        self.assertTrue(speech_langs.is_primer_echo(two, mix))
        # Someone may really say one of the lines: kept, unless the audio is
        # near-silent.
        self.assertFalse(speech_langs.is_primer_echo("Okay, let's get started.", mix))
        self.assertTrue(speech_langs.is_primer_echo("Okay, let's get started.", mix, quiet=True))
        self.assertFalse(speech_langs.is_primer_echo("Okay, let's get started. Send the report.",
                                                     mix, quiet=True))
        self.assertFalse(speech_langs.is_primer_echo("Давайте начнём.", mix, quiet=True))
        self.assertFalse(speech_langs.is_primer_echo("Բարև, send me the report okay?", mix))
        self.assertFalse(speech_langs.is_primer_echo("", mix))


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload
        self.text = ""

    def json(self):
        return self._p


class TestEngines(unittest.TestCase):
    def _rec(self, lang):
        import main
        return SimpleNamespace(_lang_setting=lambda: lang,
                               _float_to_wav=lambda a: b"RIFF",
                               _fallback_to_local_or_error=mock.MagicMock(),
                               get_auth_token=lambda: "tok"), main

    def test_gemini_prompt_carries_the_mixed_instruction(self):
        rec, main = self._rec("multi")
        sent = {}

        def post(url, json=None, timeout=None, **kw):
            sent["json"] = json
            return _Resp({"candidates": [{"content": {"parts": [{"text": "Բարև, okay"}]}}]})

        with mock.patch.dict(main.cfg, {"google_api_key": "k", "mix_languages": ["hy", "en", "fr"],
                                        "google_stt_model": "gemini-2.5-flash"}), \
             mock.patch("requests.post", post):
            text, lang = main.AudioRecorder._run_google(rec, [0.0])
        prompt = sent["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("switches between Armenian, English and French", prompt)
        self.assertEqual((text, lang), ("Բարև, okay", "multi"))

    def test_pro_cloud_request_lists_the_languages(self):
        rec, main = self._rec("multi")
        sent = {}

        def post(url, json=None, headers=None, timeout=None, **kw):
            sent["json"] = json
            return _Resp({"text": "ok", "lang": "multi"})

        with mock.patch.dict(main.cfg, {"mix_languages": ["ru", "hy"], "sample_rate": 16000,
                                        "managed_provider": "gemini"}), \
             mock.patch("requests.post", post):
            main.AudioRecorder._run_managed(rec, [0.0])
        self.assertEqual(sent["json"]["language"], "multi")
        self.assertEqual(sent["json"]["languages"], ["hy", "ru"])
        rec2, _ = self._rec("hy")
        with mock.patch.dict(main.cfg, {"sample_rate": 16000}), mock.patch("requests.post", post):
            main.AudioRecorder._run_managed(rec2, [0.0])
        self.assertEqual(sent["json"]["languages"], ["hy"])
        rec3, _ = self._rec("auto")
        with mock.patch.dict(main.cfg, {"sample_rate": 16000}), mock.patch("requests.post", post):
            main.AudioRecorder._run_managed(rec3, [0.0])
        self.assertEqual(sent["json"]["languages"], [])
        rec4, _ = self._rec("multi")                     # Mixed languages, none ticked
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "mix_languages": []}), \
             mock.patch("requests.post", post):
            main.AudioRecorder._run_managed(rec4, [0.0])
        self.assertEqual((sent["json"]["language"], sent["json"]["languages"]), ("multi", []))


class _Seg:
    def __init__(self, text):
        self.text = text


class _FakeModel:
    """Detection says Georgian (Armenian is easily heard as it); decoding
    returns whatever the test sets."""

    def __init__(self, decoded):
        self.calls = []
        self.decoded = decoded

    def transcribe(self, audio, language=None, initial_prompt=None, **kw):
        self.calls.append({"language": language, "initial_prompt": initial_prompt,
                           "beam_size": kw.get("beam_size")})
        if kw.get("beam_size") == 1:                      # the detection pass
            info = SimpleNamespace(language="ka",
                                   all_language_probs=[("ka", 0.5), ("hy", 0.3), ("en", 0.2)])
            return iter([]), info
        return iter([_Seg(self.decoded)]), SimpleNamespace(language=language)


class TestLocalWhisperMixed(unittest.TestCase):
    def _run(self, decoded, mix=("hy", "en")):
        import main
        model = _FakeModel(decoded)
        rec = SimpleNamespace(load_model=lambda *a: None, _model_lock=threading.Lock(),
                              _model=model, _infer_lock=threading.Lock(), _session_lang=None,
                              _capture_mode=None, _lang_setting=lambda: "multi")
        with mock.patch.dict(main.cfg, {"sample_rate": 16000, "initial_prompt": "",
                                        "mix_languages": list(mix), "vocabulary": []}):
            # A plain list: the path only slices the audio, and other test
            # files stub numpy out.
            text, lang = main.AudioRecorder._run_local_with(rec, model, [0.0] * (16000 * 2))
        return text, lang, model

    def test_detects_among_the_mix_and_primes_the_decoder(self):
        text, lang, model = self._run("Բարև, send me the report okay?")
        self.assertEqual(lang, "hy")                      # not Georgian
        decode = model.calls[-1]
        self.assertEqual(decode["language"], "hy")
        self.assertIn("Բարև", decode["initial_prompt"])
        self.assertIn("let's get started", decode["initial_prompt"])
        self.assertEqual(text, "Բարև, send me the report okay?")

    def test_a_primer_echo_is_dropped(self):
        text, lang, model = self._run(speech_langs.whisper_primer(["hy", "en"]))
        self.assertEqual(text, "")

    def test_without_a_mix_any_language_is_detected_as_before(self):
        text, lang, model = self._run("Привет, как дела?", mix=())
        self.assertEqual(lang, "ka")                      # Whisper's own pick, unrestricted
        self.assertIsNone(model.calls[-1]["initial_prompt"])
        self.assertEqual(text, "Привет, как дела?")


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
        return isinstance(QWidget, type) and QWidget.__module__.startswith("PySide6")
    except Exception:
        return False


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestSettingsMixRow(unittest.TestCase):
    """The real Settings handlers for 'Languages you mix', on a stand-in."""

    def setUp(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QLabel, QWidget
        QApplication.instance() or QApplication([])
        from ui.settings import Settings
        self.S = Settings
        self.page = QWidget()
        combo = QComboBox(self.page)
        combo.addItem("Auto-detect", "auto")
        combo.addItem("Mixed languages", "multi")
        me = SimpleNamespace(app=object(), cfg_working={"mix_languages": ["ru", "hy"]},
                             combo_lang=combo, mix_row=QWidget(self.page),
                             mix_hint=QLabel(self.page), _refresh_dirty=mock.MagicMock())
        me.mix_checks = {c: QCheckBox(n, me.mix_row) for c, n in speech_langs.NAMES.items()}
        me._update_mix_row = lambda *a: Settings._update_mix_row(me, *a)
        for code, cb in me.mix_checks.items():
            cb.toggled.connect(lambda on, c=code: Settings._on_mix_toggled(me, c, on))
        self.me = me

    def tearDown(self):
        self.page.deleteLater()

    def test_shown_only_for_mixed_languages_with_the_saved_picks(self):
        self.S._load_mix_checks(self.me)
        ticked = [c for c, cb in self.me.mix_checks.items() if cb.isChecked()]
        self.assertEqual(ticked, ["hy", "ru"])
        self.assertTrue(self.me.mix_row.isHidden())           # Auto-detect
        self.me.combo_lang.setCurrentIndex(1)
        self.me._update_mix_row()
        self.assertFalse(self.me.mix_row.isHidden())
        self.assertFalse(self.me.mix_hint.isHidden())

    def test_ticking_stages_the_mix(self):
        self.S._load_mix_checks(self.me)
        self.me.mix_checks["en"].setChecked(True)
        self.assertEqual(self.me.cfg_working["mix_languages"], ["hy", "en", "ru"])
        self.me._refresh_dirty.assert_called()
        self.me.mix_checks["en"].setChecked(False)
        self.me.mix_checks["ru"].setChecked(False)             # one left: saved as is...
        self.assertEqual(self.me.cfg_working["mix_languages"], ["hy"])
        self.assertEqual(speech_langs.mix_languages(self.me.cfg_working), [])   # ...no restriction


if __name__ == "__main__":
    unittest.main()
