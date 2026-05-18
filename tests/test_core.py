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

def _stub(name, attrs=None):
    m = types.ModuleType(name)
    if attrs:
        for k, v in attrs.items():
            setattr(m, k, v)
    sys.modules[name] = m
    return m

_stub("pyaudio",       {"PyAudio": MagicMock, "paInt16": 8})
_stub("pystray",       {"Icon": MagicMock, "Menu": MagicMock, "MenuItem": MagicMock})
_pynput_kb    = _stub("pynput.keyboard", {"Controller": MagicMock, "Key": MagicMock})
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
# ctypes is NOT stubbed — it is stdlib and works cross-platform.
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

    def test_privacy_mode_forces_local_no_history_no_analytics(self):
        import main as m
        orig = m.CONFIG_PATH
        m.CONFIG_PATH = self._cfg_path
        try:
            save_config({
                **DEFAULT,
                "privacy_mode": True,
                "backend": "google",
                "save_history": True,
                "analytics_enabled": True,
            })
            c = load_config()
            self.assertEqual(c["backend"], "local")
            self.assertFalse(c["save_history"])
            self.assertFalse(c["analytics_enabled"])
        finally:
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
        # tiny needs 2 GB — should pass on any machine with RAM stubs returning 16 GB
        self.assertTrue(model_ok("tiny"))

    def test_unknown_model_raises(self):
        with self.assertRaises(KeyError):
            model_ok("unknown-model")

    def test_models_dict_complete(self):
        for name in MODELS:
            self.assertIn("min_ram", MODELS[name])
            self.assertIn("speed",   MODELS[name])
            self.assertIn("quality", MODELS[name])
            self.assertIn("size",    MODELS[name])


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


if __name__ == "__main__":
    unittest.main()
