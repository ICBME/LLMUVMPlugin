import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_bfm.corpus import load_cases  # noqa: E402
from fuzz_bfm.target_config import load_target_config  # noqa: E402
from fuzz_uvm.functional_coverage import build_functional_coverage  # noqa: E402


CONFIG_TEXT = """
name = "demo"
toplevel = "demo_top"
driver = "demo_driver:Driver"

[[field]]
name = "mode"
kind = "enum"
choices = ["read", "write"]

[[field]]
name = "size"
kind = "int"
min = 1
max = 4
choices = [1, 2, 4]

[[field]]
name = "payload"
kind = "hex"
hex_len_by = { size = { "1" = 1, "2" = 2, "4" = 4 } }
"""


class TestLibAflBfmGeneric(unittest.TestCase):
    def test_corpus_validation_uses_manifest_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)
            corpus = tmp_path / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"target": "demo", "mode": "read", "size": 2, "payload": "aabb"}) + "\n"
            )
            config = load_target_config("demo", targets_dir=tmp_path)

            cases = load_cases(corpus, "demo", config=config)

            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].data["payload"], "aabb")

    def test_corpus_validation_rejects_selector_length_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)
            corpus = tmp_path / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"target": "demo", "mode": "write", "size": 4, "payload": "aabb"}) + "\n"
            )
            config = load_target_config("demo", targets_dir=tmp_path)

            with self.assertRaisesRegex(ValueError, "payload must be 4 bytes"):
                load_cases(corpus, "demo", config=config)

    def test_functional_coverage_is_schema_driven(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)
            corpus = tmp_path / "corpus.jsonl"
            corpus.write_text(
                json.dumps({"target": "demo", "mode": "read", "size": 1, "payload": "00"}) + "\n"
            )
            config = load_target_config("demo", targets_dir=tmp_path)
            cases = load_cases(corpus, "demo", config=config)

            summary = build_functional_coverage("demo", cases, config=config)

            self.assertEqual(summary["bins"]["mode"], {"read": 1})
            self.assertEqual(summary["uncovered"]["mode"], ["write"])


if __name__ == "__main__":
    unittest.main()
