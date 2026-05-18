import os
import sys
import tempfile
import unittest


os.environ["TRANSCRIBE_DISABLE_KEYRING"] = "1"
os.environ["TRANSCRIBE_SKIP_MIGRATION"] = "1"

sys.modules.pop("history", None)
import history as hist


class TestHistoryStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = hist.HISTORY_PATH
        hist.HISTORY_PATH = os.path.join(self._tmp.name, "history.json")

    def tearDown(self):
        hist.HISTORY_PATH = self._orig_path
        self._tmp.cleanup()

    def test_save_search_delete_and_export(self):
        hist.save_entry("hello world", "en", "local")
        hist.save_entry("բարեւ աշխարհ", "hy", "google")

        self.assertEqual(len(hist.load()), 2)
        self.assertEqual(len(hist.search("hello")), 1)
        self.assertTrue(hist.delete(1))
        self.assertEqual(len(hist.load()), 1)

        csv_path = os.path.join(self._tmp.name, "history.csv")
        txt_path = os.path.join(self._tmp.name, "history.txt")
        self.assertEqual(hist.export_csv(csv_path), 1)
        self.assertEqual(hist.export_txt(txt_path), 1)
        self.assertTrue(os.path.exists(csv_path))
        self.assertTrue(os.path.exists(txt_path))

    def test_export_selected_entries(self):
        hist.save_entry("first", "en", "local")
        hist.save_entry("second", "hy", "google")
        selected = hist.load()[:1]

        csv_path = os.path.join(self._tmp.name, "selected.csv")
        txt_path = os.path.join(self._tmp.name, "selected.txt")

        self.assertEqual(hist.export_csv(csv_path, selected), 1)
        self.assertEqual(hist.export_txt(txt_path, selected), 1)
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            self.assertIn("second", f.read())
        with open(txt_path, "r", encoding="utf-8") as f:
            self.assertIn("second", f.read())


if __name__ == "__main__":
    unittest.main()
