import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_llm


class TestLocalLLM(unittest.TestCase):
    def test_legacy_model_ids_normalize_to_real_models(self):
        self.assertEqual(local_llm.normalize_model_id("aibuben_tiny"), local_llm.QWEN_TINY_ID)
        self.assertEqual(local_llm.normalize_model_id("aibuben_balanced"), local_llm.QWEN_3B_ID)
        self.assertEqual(local_llm.normalize_model_id("aibuben_gpu"), local_llm.QWEN_7B_ID)

    def test_catalog_has_three_real_qwen_models(self):
        self.assertIn(local_llm.QWEN_TINY_ID, local_llm.MODEL_CATALOG)
        self.assertIn(local_llm.QWEN_3B_ID, local_llm.MODEL_CATALOG)
        self.assertIn(local_llm.QWEN_7B_ID, local_llm.MODEL_CATALOG)

    def test_remove_model_deletes_folder_and_partial_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_dir = root / "action_models" / local_llm.QWEN_TINY_ID
            model_dir.mkdir(parents=True)
            (model_dir / local_llm.MODEL_CATALOG[local_llm.QWEN_TINY_ID]["filename"]).write_bytes(b"x")
            (model_dir / f"{local_llm.MODEL_CATALOG[local_llm.QWEN_TINY_ID]['filename']}.part").write_bytes(b"partial")
            with patch.object(local_llm.storage, "path_for", side_effect=lambda name: root / name):
                self.assertTrue(local_llm.remove_model(local_llm.QWEN_TINY_ID))
                self.assertFalse(model_dir.exists())


if __name__ == "__main__":
    unittest.main()
