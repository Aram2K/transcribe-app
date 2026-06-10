"""
Unit tests for Transcribe App core logic.
Run: python -m pytest tests/ -v
"""
import sys, os, types, unittest, tempfile, shutil
from unittest.mock import patch, MagicMock

_TEST_APP_DATA = tempfile.mkdtemp(prefix="transcribe-test-data-")
os.environ["TRANSCRIBE_APP_DATA_DIR"] = _TEST_APP_DATA
os.environ["TRANSCRIBE_DISABLE_KEYRING"] = "1"
os.environ["TRANSCRIBE_SKIP_MIGRATION"] = "1"

# ── Stub heavy imports so tests run without GPU / audio hardware ──────────────

class StubMeta(type):
    def __getattr__(cls, name):
        return MagicMock()

class StubBase(metaclass=StubMeta):
    def __init__(self, *args, **kwargs):
        pass
    def __getattr__(self, name):
        return MagicMock()

class StubModule(types.ModuleType):
    def __init__(self, name, attrs=None):
        super().__init__(name)
        if attrs:
            for k, v in attrs.items():
                setattr(self, k, v)
    def __getattr__(self, name):
        return StubBase

def _stub(name, attrs=None):
    m = StubModule(name, attrs)
    sys.modules[name] = m
    return m

_stub("pyaudio",       {"PyAudio": MagicMock, "paInt16": 8})
_stub("pystray",       {"Icon": MagicMock, "Menu": MagicMock, "MenuItem": MagicMock})
_pynput_kb    = _stub("pynput.keyboard", {"Controller": MagicMock, "Key": MagicMock, "GlobalHotKeys": MagicMock, "Listener": MagicMock})
_pynput_mouse = _stub("pynput.mouse",   {"Listener": MagicMock, "Button": MagicMock})
_pynput       = _stub("pynput",         {})
_pynput.keyboard = _pynput_kb    # needed for Python ≤ 3.11 (no sys.modules fallback in IMPORT_FROM)
_pynput.mouse    = _pynput_mouse
_stub("keyboard",      {"add_hotkey": MagicMock, "remove_hotkey": MagicMock,
                         "is_pressed": MagicMock(return_value=False)})
_stub("pyperclip",     {"copy": MagicMock, "paste": MagicMock})
_stub("faster_whisper",{"WhisperModel": MagicMock})
_stub("psutil",        {"virtual_memory": MagicMock(return_value=MagicMock(total=16*1024**3)),
                         "cpu_count": MagicMock(return_value=8)})
_stub("ctranslate2",   {"get_cuda_device_count": MagicMock(return_value=0)})
_stub("numpy",         {"frombuffer": MagicMock, "abs": MagicMock,
                         "percentile": MagicMock(return_value=100),
                         "concatenate": MagicMock, "array": MagicMock,
                         "int16": MagicMock, "float32": MagicMock,
                         "mean": MagicMock(return_value=0.0)})
_pil_image = _stub("PIL.Image",     {"new": MagicMock, "LANCZOS": MagicMock})
_pil_draw  = _stub("PIL.ImageDraw", {"Draw": MagicMock})
_pil_tk    = _stub("PIL.ImageTk",   {"PhotoImage": MagicMock})
_pil       = _stub("PIL",           {})
_pil.Image     = _pil_image   # needed for Python ≤ 3.11
_pil.ImageDraw = _pil_draw
_pil.ImageTk   = _pil_tk

# Stub tkinter so no display is needed
_tk = _stub("tkinter")
_tk.Tk = MagicMock; _tk.Toplevel = MagicMock; _tk.Frame = MagicMock
_tk.Label = MagicMock; _tk.Canvas = MagicMock; _tk.StringVar = MagicMock
_tk.IntVar = MagicMock; _tk.BooleanVar = MagicMock; _tk.Text = MagicMock
_tk.Entry = MagicMock; _tk.Button = MagicMock; _tk.Scrollbar = MagicMock
_tk.Radiobutton = MagicMock; _tk.END = "end"
_tk_ttk = _stub("tkinter.ttk", {"Scrollbar": MagicMock})
_tk.ttk = _tk_ttk  # needed for Python ≤ 3.11

_stub("history",       {"add": MagicMock, "all": MagicMock(return_value=[]),
                         "clear": MagicMock, "search": MagicMock(return_value=[]),
                         "delete": MagicMock, "export_csv": MagicMock,
                         "export_txt": MagicMock})
_stub("requests",      {"get": MagicMock, "post": MagicMock})

# Stub PySide6
_core = _stub("PySide6.QtCore")
_gui = _stub("PySide6.QtGui")
_widgets = _stub("PySide6.QtWidgets")
_pyside6 = _stub("PySide6")
_pyside6.QtCore = _core
_pyside6.QtGui = _gui
_pyside6.QtWidgets = _widgets
# ctypes is NOT stubbed - it is stdlib and works cross-platform.
# ctypes.windll only appears inside apply_glass() which is never called by tests.
# Replacing ctypes in sys.modules breaks ctypes._layout imports on Python 3.11.

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Import the pieces we want to test ─────────────────────────────────────────

# We import only the pure-logic functions, not the GUI classes
from main import (
    _parse_version,
    load_config,
    save_config,
    DEFAULT,
    MODELS,
    model_ok,
    LANG_NAMES,
    APP_VERSION,
    _trusted_update_url,
    _sha256_from_checksum_text,
    _update_info_from_manifest,
    _update_info_from_release,
)
import storage


def tearDownModule():
    shutil.rmtree(_TEST_APP_DATA, ignore_errors=True)


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestParseVersion(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(_parse_version("v1.2.3"), (1, 2, 3))

    def test_no_v_prefix(self):
        self.assertEqual(_parse_version("2.0.0"), (2, 0, 0))

    def test_short(self):
        self.assertEqual(_parse_version("1.0"), (1, 0, 0))

    def test_invalid(self):
        self.assertEqual(_parse_version("garbage"), (0, 0, 0))

    def test_update_needed(self):
        # A version one major above the current app version should be considered newer.
        cur = _parse_version(APP_VERSION)
        future = (cur[0] + 1, 0, 0)
        self.assertGreater(future, cur)

    def test_no_update_needed(self):
        self.assertFalse(_parse_version("0.0.1") > _parse_version(APP_VERSION))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg_path = os.path.join(self._tmp.name, "config.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_present(self):
        c = load_config()
        for key in DEFAULT:
            self.assertIn(key, c)

    def test_save_and_reload(self):
        import main as m
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        try:
            save_config({**DEFAULT, "whisper_model": "small"})
            c = load_config()
            self.assertEqual(c["whisper_model"], "small")
        finally:
            m.CONFIG_PATH = orig

    def test_missing_file_returns_defaults(self):
        import main as m
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = "nonexistent_config_xyz.json"
        try:
            c = load_config()
            self.assertEqual(c, DEFAULT)
        finally:
            m.CONFIG_PATH = orig

    def test_save_config_keeps_secrets_out_of_disk_when_keyring_available(self):
        # All BYO keys (incl. Mistral) and the per-user stashed keys must go to
        # the OS keyring - plaintext config.json only ever stores "".
        import json as _json
        import main as m
        written = {}

        def fake_write(name, value):
            written[name] = value
            return True

        with patch.object(m.storage, "write_secret", side_effect=fake_write), \
             patch.object(m.storage, "atomic_write_json") as aw:
            cfg = {**m.DEFAULT,
                   "google_api_key": "G-key", "action_api_key": "A-key",
                   "mistral_api_key": "M-key",
                   "user_secrets": {"user:A": {"google_api_key": "UG-key",
                                               "backend": "local"}}}
            m.save_config(cfg)

        disk = aw.call_args[0][1]
        self.assertEqual(disk["google_api_key"], "")
        self.assertEqual(disk["action_api_key"], "")
        self.assertEqual(disk["mistral_api_key"], "")
        self.assertEqual(disk["user_secrets"]["user:A"]["google_api_key"], "")
        self.assertEqual(disk["user_secrets"]["user:A"]["backend"], "local")
        # Keyring received the real values.
        self.assertEqual(written[m.storage.MISTRAL_API_KEY_SECRET], "M-key")
        blob = _json.loads(written[m.storage.USER_SECRETS_SECRET])
        self.assertEqual(blob["user:A"]["google_api_key"], "UG-key")
        # The in-memory config the app keeps using is untouched.
        self.assertEqual(cfg["user_secrets"]["user:A"]["google_api_key"], "UG-key")

    def test_partial_config_merged_with_defaults(self):
        import main as m
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        try:
            import json
            with open(self._cfg_path, "w") as f:
                json.dump({"whisper_model": "large-v3"}, f)
            c = load_config()
            self.assertEqual(c["whisper_model"], "large-v3")
            self.assertEqual(c["language"], DEFAULT["language"])
        finally:
            m.CONFIG_PATH = orig

    def test_api_key_is_sanitized_when_keyring_accepts_it(self):
        import json
        import main as m
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        try:
            with patch.object(m.storage, "write_secret", return_value=True), \
                 patch.object(m.storage, "read_secret", return_value="secret-key"):
                save_config({**DEFAULT, "google_api_key": "secret-key"})
                with open(self._cfg_path, encoding="utf-8") as f:
                    raw = json.load(f)
                self.assertEqual(raw["google_api_key"], "")
                self.assertEqual(load_config()["google_api_key"], "secret-key")
        finally:
            m.CONFIG_PATH = orig

    def test_action_api_key_is_sanitized_when_keyring_accepts_it(self):
        import json
        import main as m
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        try:
            with patch.object(m.storage, "write_secret", return_value=True), \
                 patch.object(m.storage, "read_secret", return_value="action-secret"):
                save_config({**DEFAULT, "action_api_key": "action-secret"})
                with open(self._cfg_path, encoding="utf-8") as f:
                    raw = json.load(f)
                self.assertEqual(raw["action_api_key"], "")
                self.assertEqual(load_config()["action_api_key"], "action-secret")
        finally:
            m.CONFIG_PATH = orig

    def test_action_model_does_not_auto_enable_smart_output_mode(self):
        # Smart Actions are Pro-only and must be opted into explicitly. Selecting
        # an action model must NOT flip the default output mode away from
        # transcribe_only.
        import main as m
        import local_llm
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        try:
            save_config({
                **DEFAULT,
                "output_action": "transcribe_only",
                "action_model": local_llm.QWEN_7B_ID,
            })
            c = load_config()
            self.assertEqual(c["output_action"], "transcribe_only")
        finally:
            m.CONFIG_PATH = orig

    def test_privacy_mode_forces_local_keeps_history_and_analytics(self):
        # Privacy Mode forces backend=local and resets cloud action models, but it
        # leaves local history AND analytics to the user's own settings (local
        # history never leaves the device, so Privacy Mode does not touch it).
        import main as m
        import actions
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        try:
            save_config({
                **DEFAULT,
                "privacy_mode": True,
                "backend": "google",
                "save_history": True,
                "analytics_enabled": True,
                "action_model": actions.API_OPENAI_ID,
            })
            c = load_config()
            self.assertEqual(c["backend"], "local")
            self.assertTrue(c["save_history"])  # local history is independent of Privacy Mode
            self.assertTrue(c["analytics_enabled"])  # NOT forced off anymore
            self.assertEqual(c["action_model"], actions.RULE_BASED_ID)
        finally:
            m.CONFIG_PATH = orig

    def test_installer_analytics_consent_marker_records_consent_applied(self):
        # The .accepted marker just flags consent as recorded; the actual
        # analytics_enabled state comes from the DEFAULT (True) or any
        # user override. Privacy Mode no longer affects this path.
        import main as m
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        marker = m.ANALYTICS_CONSENT_PATH
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("accepted", encoding="ascii")
        try:
            c = load_config()
            self.assertTrue(c["analytics_enabled"])
            self.assertTrue(c["analytics_consent_applied"])
            save_config({**c, "analytics_enabled": False})
            self.assertFalse(load_config()["analytics_enabled"])
        finally:
            try:
                marker.unlink()
            except OSError:
                pass
            m.CONFIG_PATH = orig

    def test_installer_analytics_declined_marker_disables_default(self):
        # If the installer wrote a .declined marker (user unchecked the
        # consent checkbox), load_config should honour it on first launch.
        import main as m
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        accepted = m.ANALYTICS_CONSENT_PATH
        declined = m.ANALYTICS_DECLINED_PATH
        accepted.parent.mkdir(parents=True, exist_ok=True)
        accepted.write_text("accepted", encoding="ascii")
        declined.write_text("declined", encoding="ascii")
        try:
            c = load_config()
            self.assertFalse(c["analytics_enabled"])
            self.assertTrue(c["analytics_consent_applied"])
        finally:
            for p in (accepted, declined):
                try: p.unlink()
                except OSError: pass
            m.CONFIG_PATH = orig


class TestStorage(unittest.TestCase):
    def test_atomic_json_creates_backup_and_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "data.json")
            storage.atomic_write_json(path, {"value": 1})
            storage.atomic_write_json(path, {"value": 2})
            self.assertTrue(os.path.exists(path + ".bak"))
            self.assertEqual(storage.read_json(path, {}), {"value": 2})
            with open(path, "w", encoding="utf-8") as f:
                f.write("{broken")
            self.assertEqual(storage.read_json(path, {}), {"value": 1})

    def test_migrate_legacy_file_copies_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as d:
            legacy = os.path.join(d, "legacy.json")
            target = os.path.join(d, "nested", "target.json")
            with open(legacy, "w", encoding="utf-8") as f:
                f.write('{"ok": true}')
            with patch.dict(os.environ, {"TRANSCRIBE_SKIP_MIGRATION": ""}, clear=False):
                self.assertTrue(storage.migrate_legacy_file(legacy, target))
            self.assertTrue(os.path.exists(legacy))
            self.assertEqual(storage.read_json(target, {}), {"ok": True})

    def test_append_jsonl_and_read_jsonl_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "chunks.jsonl")
            for i, text in enumerate(["one", "two", "three"]):
                self.assertTrue(storage.append_jsonl(path, {"idx": i, "text": text}))
            records = storage.read_jsonl(path)
            self.assertEqual([r["text"] for r in records], ["one", "two", "three"])
            self.assertEqual([r["idx"] for r in records], [0, 1, 2])

    def test_read_jsonl_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "chunks.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"idx": 0, "text": "good"}\n')
                fh.write('not-json\n')
                fh.write('{"idx": 1, "text": "also good"}\n')
            records = storage.read_jsonl(path)
            self.assertEqual([r["text"] for r in records], ["good", "also good"])


class TestUpdaterHelpers(unittest.TestCase):
    def test_trusted_update_url_only_allows_github_https(self):
        self.assertTrue(_trusted_update_url("https://github.com/Aram2K/transcribe-app/releases/download/v1/app.exe"))
        self.assertFalse(_trusted_update_url("http://github.com/Aram2K/transcribe-app/releases/download/v1/app.exe"))
        self.assertFalse(_trusted_update_url("https://example.com/app.exe"))

    def test_sha256_parser(self):
        digest = "a" * 64
        self.assertEqual(_sha256_from_checksum_text(f"{digest}  TranscribeApp-Windows-Setup.exe"), digest)
        self.assertEqual(_sha256_from_checksum_text("not a checksum"), "")

    def test_manifest_update_info(self):
        digest = "b" * 64
        info = _update_info_from_manifest({
            "version": "9.0.0",
            "tag": "v9.0.0",
            "windows": {
                "installer": {
                    "url": "https://github.com/Aram2K/transcribe-app/releases/download/v9.0.0/TranscribeApp-Windows-Setup.exe",
                    "sha256": digest,
                    "sha256_url": "https://github.com/Aram2K/transcribe-app/releases/download/v9.0.0/TranscribeApp-Windows-Setup.exe.sha256",
                }
            },
        })
        self.assertEqual(info["tag"], "v9.0.0")
        self.assertEqual(info["checksum"], digest)

    def test_release_update_info_prefers_stable_installer_name(self):
        info = _update_info_from_release({
            "tag_name": "v9.0.0",
            "body": "notes",
            "assets": [
                {"name": "Something-Setup.exe", "browser_download_url": "https://example.com/wrong.exe"},
                {"name": "TranscribeApp-Windows-Setup.exe", "browser_download_url": "https://github.com/Aram2K/transcribe-app/releases/download/v9.0.0/TranscribeApp-Windows-Setup.exe"},
                {"name": "TranscribeApp-Windows-Setup.exe.sha256", "browser_download_url": "https://github.com/Aram2K/transcribe-app/releases/download/v9.0.0/TranscribeApp-Windows-Setup.exe.sha256"},
            ],
        })
        self.assertTrue(info["installer_url"].endswith("TranscribeApp-Windows-Setup.exe"))
        self.assertTrue(info["checksum_url"].endswith(".sha256"))


class TestModelOk(unittest.TestCase):
    def test_tiny_always_ok(self):
        # tiny needs 2 GB - should pass on any machine with RAM stubs returning 16 GB
        self.assertTrue(model_ok("tiny"))

    def test_unknown_model_raises(self):
        with self.assertRaises(KeyError):
            model_ok("unknown-model")

    def test_models_dict_complete(self):
        for name in MODELS:
            self.assertIn("min_ram",    MODELS[name])
            self.assertIn("speed_rank", MODELS[name])
            self.assertIn("quality",    MODELS[name])
            self.assertIn("size",       MODELS[name])


class TestLangNames(unittest.TestCase):
    def test_armenian_present(self):
        self.assertIn("hy", LANG_NAMES)

    def test_auto_present(self):
        self.assertIn("auto", LANG_NAMES)

    def test_multi_present(self):
        self.assertIn("multi", LANG_NAMES)

    def test_no_empty_names(self):
        for code, name in LANG_NAMES.items():
            self.assertTrue(name.strip(), f"Empty name for lang code '{code}'")


class TestFmtHotkey(unittest.TestCase):
    """Test Settings._fmt_hotkey without instantiating the full GUI."""

    def _fmt(self, hk):
        from main import Settings
        return Settings._fmt_hotkey(None, hk)

    def test_single_key(self):
        self.assertEqual(self._fmt("r"), "R")

    def test_combo(self):
        result = self._fmt("alt+r")
        self.assertIn("Alt", result)
        self.assertIn("R",   result)

    def test_ctrl_shift(self):
        result = self._fmt("ctrl+shift+t")
        self.assertIn("Ctrl",  result)
        self.assertIn("Shift", result)

    def test_mouse(self):
        result = self._fmt("mouse:middle")
        self.assertIn("Middle", result)

    def test_mouse_x1(self):
        result = self._fmt("mouse:x1")
        self.assertIn("Back", result)


class TestMeetingsWindowHelpers(unittest.TestCase):
    """Tests for the pure-data helpers on MeetingsWindow - transcript
    stitching, prompt context building, and share-format conversion.

    These don't exercise the Tk widgets; we bypass __init__ and set the
    handful of instance attributes the helpers actually read."""

    def _make_mw(self, **overrides):
        import main
        mw = main.MeetingsWindow.__new__(main.MeetingsWindow)
        mw._meeting_title = overrides.get("title", "")
        mw._meeting_attendees = overrides.get("attendees", "")
        mw._user_notes = overrides.get("user_notes", "")
        mw._summary_var = overrides.get("summary", "")
        mw._transcript_var = overrides.get("transcript", "")
        return mw

    def test_build_transcript_inserts_speaker_change_markers(self):
        mw = self._make_mw()
        chunks = [
            {"text": "Hello everyone.", "silence_before": 0.0},
            {"text": "Thanks for joining.", "silence_before": 0.3},   # short - no marker
            {"text": "I'll draft the spec.", "silence_before": 1.8},  # long - marker
            {"text": "Sounds good.", "silence_before": 2.0},          # long - marker
        ]
        out = mw._build_transcript_with_markers(chunks)
        self.assertIn("Hello everyone.", out)
        self.assertIn("[speaker change] I'll draft the spec.", out)
        self.assertIn("[speaker change] Sounds good.", out)
        # First chunk should never get a marker even if its silence_before > 0
        self.assertFalse(out.startswith("[speaker change]"))

    def test_build_llm_input_prepends_context_when_provided(self):
        mw = self._make_mw(
            title="Q3 planning",
            attendees="Aram, Sara, John",
            user_notes="• budget approval still pending",
        )
        out = mw._build_llm_input("The team discussed the roadmap.")
        self.assertIn("Q3 planning", out)
        self.assertIn("Aram, Sara, John", out)
        self.assertIn("budget approval still pending", out)
        self.assertIn("Transcript:", out)
        self.assertIn("The team discussed the roadmap.", out)

    def test_build_llm_input_returns_bare_transcript_when_no_context(self):
        mw = self._make_mw()
        out = mw._build_llm_input("raw transcript only")
        self.assertEqual(out, "raw transcript only")

    def test_strip_markdown_removes_sigils(self):
        import main
        md = "## Heading\n\n**bold** text and `code` here\n- [ ] checkbox task\n- bullet"
        out = main.MeetingsWindow._strip_markdown(md)
        self.assertNotIn("##", out)
        self.assertNotIn("**", out)
        self.assertNotIn("- [ ]", out)
        self.assertNotIn("`code`", out)
        self.assertIn("Heading", out)
        self.assertIn("bold", out)
        self.assertIn("code", out)
        # Both line-starting `- [ ] X` and `- Y` should convert to bullet glyph
        bullet_lines = [l for l in out.splitlines() if l.startswith("• ")]
        self.assertGreaterEqual(len(bullet_lines), 2)

    def test_format_share_renders_email_with_greeting(self):
        mw = self._make_mw(
            title="Sync with Sara",
            summary="## Summary\nGreat call.\n\n## Action items\n- [ ] follow up",
        )
        out = mw._format_share("email")
        self.assertIn("Subject: Sync with Sara - Notes", out)
        self.assertIn("Hi,", out)
        self.assertIn("Best,", out)
        # Markdown sigils should be stripped in the email body
        self.assertNotIn("##", out)
        self.assertNotIn("- [ ]", out)

    def test_format_share_renders_slack_with_bold(self):
        mw = self._make_mw(
            title="Standup",
            summary="## Summary\n**Aram** will ship by Friday.",
        )
        out = mw._format_share("slack")
        self.assertIn("*Standup - Notes*", out)
        # ## Summary becomes *Summary*; **Aram** becomes *Aram*
        self.assertIn("*Summary*", out)
        self.assertIn("*Aram*", out)
        self.assertNotIn("**", out)
        self.assertNotIn("##", out)

    def test_format_share_markdown_returns_summary_verbatim(self):
        mw = self._make_mw(summary="## Hello\n**bold**\n- item")
        self.assertEqual(mw._format_share("markdown"), "## Hello\n**bold**\n- item")


class TestMistral(unittest.TestCase):
    @patch("requests.post")
    def test_run_mistral_success(self, mock_post):
        import main
        from main import AudioRecorder
        
        # Configure mock response
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"text": "hello from mistral", "language": "en"}
        mock_post.return_value = mock_resp
        
        # Create recorder and stub dependencies
        recorder = AudioRecorder()
        recorder._float_to_wav = MagicMock(return_value=b"fake-wav")
        
        # Setup config mock
        with patch.dict(main.cfg, {
            "backend": "mistral",
            "mistral_api_key": "fake-key",
            "mistral_stt_model": "voxtral-mini-latest",
            "language": "en",
            "sample_rate": 16000
        }):
            import numpy as np
            audio = np.array([0.0]*1000, dtype=np.float32)
            text, lang = recorder._run_mistral(audio)
            
            self.assertEqual(text, "hello from mistral")
            self.assertEqual(lang, "en")
            
            mock_post.assert_called_once()
            args, kwargs = mock_post.call_args
            self.assertEqual(args[0], "https://api.mistral.ai/v1/audio/transcriptions")
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fake-key")
            self.assertEqual(kwargs["data"]["model"], "voxtral-mini-latest")
            self.assertEqual(kwargs["data"]["language"], "en")


class TestSettingsValidation(unittest.TestCase):
    def test_save_clicked_missing_mistral_key(self):
        from ui.settings import Settings
        from PySide6.QtWidgets import QMessageBox

        dialog = MagicMock()
        dialog.cfg_working = {"backend": "mistral", "mistral_api_key": ""}
        dialog.tabs = MagicMock()
        dialog.mistral_key_input = MagicMock()
        dialog.mistral_key_input.text.return_value = ""
        dialog.accept = MagicMock()
        
        with patch.object(QMessageBox, "warning") as mock_warning:
            Settings._on_save_clicked(dialog)
            
            dialog._sync_action_settings_from_widgets.assert_called_once()
            dialog.tabs.setCurrentIndex.assert_called_once_with(1)
            dialog.mistral_key_input.setFocus.assert_called_once()
            dialog.mistral_key_input.setStyleSheet.assert_called_once_with(
                "border: 2px solid #ef4444; background-color: #fef2f2;"
            )
            mock_warning.assert_called_once()
            dialog.accept.assert_not_called()

    def test_save_clicked_missing_google_key(self):
        from ui.settings import Settings
        from PySide6.QtWidgets import QMessageBox

        dialog = MagicMock()
        dialog.cfg_working = {"backend": "google", "google_api_key": ""}
        dialog.tabs = MagicMock()
        dialog.google_key_input = MagicMock()
        dialog.google_key_input.text.return_value = ""
        dialog.accept = MagicMock()
        
        with patch.object(QMessageBox, "warning") as mock_warning:
            Settings._on_save_clicked(dialog)
            
            dialog._sync_action_settings_from_widgets.assert_called_once()
            dialog.tabs.setCurrentIndex.assert_called_once_with(1)
            dialog.google_key_input.setFocus.assert_called_once()
            dialog.google_key_input.setStyleSheet.assert_called_once_with(
                "border: 2px solid #ef4444; background-color: #fef2f2;"
            )
            mock_warning.assert_called_once()
            dialog.accept.assert_not_called()

    def test_save_clicked_valid(self):
        from ui.settings import Settings

        dialog = MagicMock()
        dialog.cfg_working = {"backend": "mistral", "mistral_api_key": "some-key"}
        dialog._mistral_key_verified = True
        dialog.tabs = MagicMock()
        dialog.mistral_key_input = MagicMock()
        dialog.mistral_key_input.text.return_value = "some-key"
        dialog.accept = MagicMock()
        dialog.app = MagicMock()
        dialog.app.cfg = {}

        Settings._on_save_clicked(dialog)

        # Save persists config and confirms with a toast - but does NOT close the
        # window (Save no longer dismisses Settings).
        dialog._sync_action_settings_from_widgets.assert_called_once()
        dialog.app.save_config.assert_called_once()
        dialog._show_saved_toast.assert_called_once()
        dialog.accept.assert_not_called()
        dialog.tabs.setCurrentIndex.assert_not_called()

    def test_save_clicked_invalid_mistral_key(self):
        from ui.settings import Settings
        from PySide6.QtWidgets import QMessageBox

        dialog = MagicMock()
        dialog.cfg_working = {"backend": "mistral", "mistral_api_key": "bad-key"}
        dialog._mistral_key_verified = False
        dialog.tabs = MagicMock()
        dialog.mistral_key_input = MagicMock()
        dialog.mistral_key_input.text.return_value = "bad-key"
        dialog.accept = MagicMock()
        
        with patch.object(QMessageBox, "warning") as mock_warning:
            Settings._on_save_clicked(dialog)
            
            dialog.tabs.setCurrentIndex.assert_called_once_with(1)
            dialog.mistral_key_input.setFocus.assert_called_once()
            mock_warning.assert_called_once()
            dialog.accept.assert_not_called()

    def test_text_changed_invalidates_state(self):
        from ui.settings import Settings

        dialog = MagicMock()
        dialog.app = MagicMock()
        dialog.cfg_working = {"backend": "mistral", "mistral_api_key": "old-key"}
        dialog._mistral_key_verified = True
        dialog.chk_privacy.isChecked.return_value = False

        dialog.mistral_key_input = MagicMock()
        dialog.mistral_key_input.text.return_value = ""  # Changed to empty!
        
        dialog.lbl_status_mistral = MagicMock()
        dialog.mistral_cards = {}
        
        Settings._save_general_configs(dialog)
        
        self.assertFalse(dialog._mistral_key_verified)
        dialog.lbl_status_mistral.setText.assert_called_with("Not Configured")

    def test_text_changed_sets_untested_but_valid(self):
        from ui.settings import Settings

        dialog = MagicMock()
        dialog.app = MagicMock()
        dialog.cfg_working = {"backend": "mistral", "mistral_api_key": "old-key"}
        dialog._mistral_key_verified = False
        dialog.chk_privacy.isChecked.return_value = False

        dialog.mistral_key_input = MagicMock()
        dialog.mistral_key_input.text.return_value = "new-key"  # Changed to non-empty!
        
        dialog.lbl_status_mistral = MagicMock()
        dialog.mistral_cards = {}
        
        Settings._save_general_configs(dialog)
        
        self.assertTrue(dialog._mistral_key_verified)
        dialog.lbl_status_mistral.setText.assert_called_with("Not Tested")

    def test_mistral_test_key_success(self):
        from ui.settings import Settings

        dialog = MagicMock()
        dialog.mistral_key_input = MagicMock()
        dialog.mistral_key_input.text.return_value = "valid-key"
        dialog.cfg_working = {"mistral_stt_model": "voxtral-mini-latest"}
        dialog.lbl_status_mistral = MagicMock()
        dialog.btn_test_mistral = MagicMock()
        dialog.mistral_test_finished = MagicMock()

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            with patch("threading.Thread") as mock_thread:
                Settings._test_mistral_key(dialog)

                worker_func = mock_thread.call_args[1]["target"]
                worker_func()

                # The test now does a real transcription call (validates the key,
                # model access AND credits), not just a key-only models check.
                self.assertEqual(mock_post.call_count, 1)
                args, kwargs = mock_post.call_args
                self.assertEqual(args[0], "https://api.mistral.ai/v1/audio/transcriptions")
                self.assertEqual(kwargs["headers"], {"Authorization": "Bearer valid-key"})
                self.assertEqual(kwargs["data"], {"model": "voxtral-mini-latest"})
                dialog.mistral_test_finished.emit.assert_called_once_with(True, "Working!")


class TestCaptureMode(unittest.TestCase):
    """Plain dictation must never trigger loopback/system-audio capture; only an
    explicit meeting capture_mode="smart_meeting" does."""

    @patch("threading.Thread")
    def test_dictation_default_is_mic_only(self, _mock_thread):
        import main
        from main import AudioRecorder
        rec = AudioRecorder()
        rec.audio = MagicMock()
        rec._find_default_loopback_device = MagicMock(return_value=7)
        try:
            with patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": 1024,
                                       "input_device_index": None}):
                rec.start_recording()  # no capture_mode → plain dictation
            rec._find_default_loopback_device.assert_not_called()
            self.assertIsNone(rec.mic_stream)
            self.assertEqual(rec.audio.open.call_count, 1)  # primary mic only
        finally:
            rec.recording = False

    @patch("threading.Thread")
    def test_dictation_ignores_stale_smart_meeting_in_config(self, _mock_thread):
        # A "smart_meeting" sentinel left in input_device_index by an older build
        # must not make ordinary dictation capture system audio.
        import main
        from main import AudioRecorder
        rec = AudioRecorder()
        rec.audio = MagicMock()
        rec._find_default_loopback_device = MagicMock(return_value=7)
        try:
            with patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": 1024,
                                       "input_device_index": "smart_meeting"}):
                rec.start_recording()
            rec._find_default_loopback_device.assert_not_called()
            self.assertIsNone(rec.mic_stream)
            self.assertEqual(rec.audio.open.call_count, 1)
        finally:
            rec.recording = False

    @patch("threading.Thread")
    def test_smart_meeting_uses_loopback_and_mic(self, _mock_thread):
        import main
        from main import AudioRecorder
        rec = AudioRecorder()
        rec.audio = MagicMock()
        rec.audio.get_device_info_by_index.return_value = {
            "defaultSampleRate": 48000, "maxInputChannels": 2}
        rec._find_default_loopback_device = MagicMock(return_value=7)
        try:
            with patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": 1024}):
                rec.start_recording(capture_mode="smart_meeting")
            rec._find_default_loopback_device.assert_called_once()
            self.assertIsNotNone(rec.mic_stream)              # secondary mic opened
            self.assertEqual(rec.audio.open.call_count, 2)    # loopback + mic
        finally:
            rec.recording = False

    @patch("threading.Thread")
    def test_system_only_uses_loopback_without_mic(self, _mock_thread):
        # "System sound only" records the computer's audio with NO mic stream.
        import main
        from main import AudioRecorder
        rec = AudioRecorder()
        rec.audio = MagicMock()
        rec.audio.get_device_info_by_index.return_value = {
            "defaultSampleRate": 48000, "maxInputChannels": 2}
        rec._find_default_loopback_device = MagicMock(return_value=7)
        try:
            with patch.dict(main.cfg, {"sample_rate": 16000, "chunk_size": 1024}):
                rec.start_recording(capture_mode="system_only")
            rec._find_default_loopback_device.assert_called_once()
            self.assertIsNone(rec.mic_stream)                 # no secondary mic
            self.assertEqual(rec.audio.open.call_count, 1)    # loopback only
        finally:
            rec.recording = False


class TestGoogleKeyTest(unittest.TestCase):
    # The key check now hits the Gemini (AI Studio) models endpoint with a GET,
    # because Google Cloud Speech rejects API keys. 200 = valid; 400 = invalid key.
    def _run_worker(self, status_code, json_data):
        from ui.settings import Settings
        dialog = MagicMock()
        dialog.google_key_input.text.return_value = "some-key"
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = status_code
            mock_resp.json.return_value = json_data
            mock_get.return_value = mock_resp
            with patch("threading.Thread") as mock_thread:
                Settings._test_google_key(dialog)
                worker = mock_thread.call_args[1]["target"]
                worker()
        return dialog.google_test_finished.emit

    def test_valid_key_reports_working(self):
        # 200 from the models endpoint means the AI Studio key is valid.
        emit = self._run_worker(200, {"models": []})
        emit.assert_called_once_with(True, "Working!")

    def test_invalid_key_reports_invalid(self):
        emit = self._run_worker(400, {"error": {
            "status": "INVALID_ARGUMENT",
            "message": "API key not valid. Please pass a valid API key."}})
        emit.assert_called_once_with(False, "Invalid API key")

    def test_permission_denied_not_reported_as_working(self):
        emit = self._run_worker(403, {"error": {
            "status": "PERMISSION_DENIED",
            "message": "Permission denied on this API"}})
        self.assertFalse(emit.call_args[0][0])

    def test_server_error_not_reported_as_working(self):
        # A 5xx must not be misread as a passing key.
        emit = self._run_worker(500, {"error": {
            "status": "INTERNAL", "message": "backend error"}})
        self.assertFalse(emit.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
