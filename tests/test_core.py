"""
Unit tests for Transcribe App core logic.
Run: python -m pytest tests/ -v
"""
import sys, os, types, unittest
from unittest.mock import patch, MagicMock

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
_stub("pynput",        {})
_stub("pynput.keyboard", {"Controller": MagicMock, "Key": MagicMock})
_stub("pynput.mouse",  {"Listener": MagicMock, "Button": MagicMock})
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
_stub("PIL",           {})
_stub("PIL.Image",     {"new": MagicMock, "LANCZOS": MagicMock})
_stub("PIL.ImageDraw", {"Draw": MagicMock})

# Stub tkinter so no display is needed
_tk = _stub("tkinter")
_tk.Tk = MagicMock; _tk.Toplevel = MagicMock; _tk.Frame = MagicMock
_tk.Label = MagicMock; _tk.Canvas = MagicMock; _tk.StringVar = MagicMock
_tk.IntVar = MagicMock; _tk.BooleanVar = MagicMock; _tk.Text = MagicMock
_tk.Entry = MagicMock; _tk.Button = MagicMock; _tk.Scrollbar = MagicMock
_tk.Radiobutton = MagicMock; _tk.END = "end"
_stub("tkinter.ttk",   {"Scrollbar": MagicMock})

_stub("history",       {"add": MagicMock, "all": MagicMock(return_value=[]),
                         "clear": MagicMock, "search": MagicMock(return_value=[])})
_stub("requests",      {"get": MagicMock, "post": MagicMock})
import ctypes as _real_ctypes
_stub("ctypes",        {"windll": MagicMock, "byref": MagicMock,
                         "c_int":    _real_ctypes.c_int,
                         "sizeof":   _real_ctypes.sizeof,
                         "POINTER":  _real_ctypes.POINTER,
                         "pointer":  _real_ctypes.pointer,
                         "Structure": _real_ctypes.Structure})

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
)


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
        self.assertGreater(_parse_version("1.3.0"), _parse_version(APP_VERSION))

    def test_no_update_needed(self):
        self.assertFalse(_parse_version("1.0.0") > _parse_version(APP_VERSION))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._cfg_path = "test_config_tmp.json"

    def tearDown(self):
        if os.path.exists(self._cfg_path):
            os.remove(self._cfg_path)

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
