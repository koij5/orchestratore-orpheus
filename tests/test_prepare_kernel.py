import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_kernel


class PrepareKernelTests(unittest.TestCase):
    def test_generates_standalone_script_with_embedded_classifier(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            kernel_dir = Path(temp) / "kernel"
            kernel_dir.mkdir()
            for name in ("trascrivi.py", "subject_classifier.py"):
                shutil.copy2(repo / "kaggle_kernel" / name, kernel_dir / name)
            (kernel_dir / "kernel-metadata.json").write_text(json.dumps({
                "code_file": "trascrivi.py",
                "dataset_sources": ["replace-kaggle-username/orpheus-private-config"],
                "id": "replace-kaggle-username/orpheus-whisper",
                "is_private": True,
            }), encoding="utf-8")
            env = {
                "KAGGLE_KERNEL_DIR": str(kernel_dir),
                "KAGGLE_KERNEL_SLUG": "owner/orpheus-worker",
                "KAGGLE_DATASET_SLUG": "owner/private-config",
            }
            with patch.dict(os.environ, env, clear=False):
                prepare_kernel.main()

            script = (kernel_dir / "trascrivi.py").read_text(encoding="utf-8")
            compile(script, str(kernel_dir / "trascrivi.py"), "exec")
            self.assertIn(prepare_kernel.BUNDLE_SIGNATURE, script)
            self.assertIn("MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7", script)
            metadata = json.loads((kernel_dir / "kernel-metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["id"], "owner/orpheus-worker")
            self.assertEqual(metadata["dataset_sources"], ["owner/private-config"])
            self.assertTrue(metadata["is_private"])

            runpy.run_path(str(kernel_dir / "trascrivi.py"), run_name="orpheus_bundle_test")
            bundled = sys.modules["subject_classifier"]

            class FakeClassifier:
                def __call__(self, text, **kwargs):
                    return {"labels": ["biologia", "altro"], "scores": [0.9, 0.1]}

            result = bundled.classify_transcript(
                "[00:00:00 - 00:00:03] lezione di biologia",
                classifier=FakeClassifier(),
            )
            self.assertEqual(result["segments"][0]["materia"], "biologia")


if __name__ == "__main__":
    unittest.main()
