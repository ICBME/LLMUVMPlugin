import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_bfm.corpus import load_cases  # noqa: E402
from fuzz_bfm.target_config import load_target_config  # noqa: E402
from fuzz_feedback.advisors import propose_directives  # noqa: E402
from fuzz_feedback.coverage import build_summary  # noqa: E402
from fuzz_uvm.functional_coverage import build_functional_coverage  # noqa: E402
from fuzz_uvm.functional_coverage import GenericCoverageModel  # noqa: E402


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

[[coverpoint]]
name = "payload_pattern"
field = "payload"
patterns = ["zero", "ff", "increment"]

[[cross]]
name = "mode_x_payload_pattern"
coverpoints = ["mode", "payload_pattern"]
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
            self.assertEqual(summary["uncovered"]["coverpoints"]["payload_pattern"], ["ff", "increment"])
            self.assertIn("read|ff", summary["uncovered"]["crosses"]["mode_x_payload_pattern"])
            self.assertEqual(summary["coverage"]["total_bins"], 8)
            self.assertEqual(summary["coverage"]["total_crosses"], 6)

    def test_target_config_loads_functional_coverpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)

            config = load_target_config("demo", targets_dir=tmp_path)

            self.assertEqual(config.coverpoints[0].name, "payload_pattern")
            self.assertEqual(config.coverpoints[0].patterns, ("zero", "ff", "increment"))
            self.assertEqual(config.crosses[0].coverpoints, ("mode", "payload_pattern"))

    def test_coverage_model_can_sample_replay_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo.toml").write_text(CONFIG_TEXT)
            config = load_target_config("demo", targets_dir=tmp_path)
            model = GenericCoverageModel("demo", config=config)
            case = SimpleNamespace(
                target="demo",
                data={"target": "demo", "mode": "read", "size": 1, "payload": "00"},
                line_no=1,
            )

            model.sample_record(SimpleNamespace(case=case, error=None))
            model.sample_record(SimpleNamespace(case=case, error="driver failed"))

            summary = model.to_json()
            self.assertEqual(summary["total_cases"], 1)
            self.assertEqual(summary["bins"]["payload_pattern"], {"zero": 1})

    def test_feedback_summary_prefers_uvm_functional_coverage_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            functional = tmp_path / "uvm_functional.json"
            functional.write_text(json.dumps({"domain": "uvm_functional", "target": "demo", "marker": "replay"}))

            summary = build_summary(
                "demo",
                tmp_path / "missing.info",
                tmp_path / "missing.jsonl",
                functional_coverage=functional,
            )

            self.assertEqual(summary["uvm_functional_coverage"]["marker"], "replay")
            self.assertEqual(summary["uvm_functional_coverage_source"], str(functional))

    def test_advisor_uses_functional_uncovered_fields_and_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "demo.toml"
            config_path.write_text(CONFIG_TEXT)
            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config_path)
            try:
                directives = propose_directives(
                    {
                        "target": "demo",
                        "uncovered_line_count": 0,
                        "uvm_functional_coverage": {
                            "uncovered": {
                                "fields": {"mode": ["write"]},
                                "coverpoints": {"payload_pattern": ["ff"]},
                            }
                        },
                        "stimulus_summary": {"field_counts": {}},
                    }
                )
            finally:
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config

            directive = directives["directives"][0]
            self.assertEqual(directive["name"], "functional_schema_refresh")
            self.assertEqual(directive["mode_values"], ["write"])
            self.assertEqual(directive["payload_patterns"], ["ff"])


if __name__ == "__main__":
    unittest.main()
