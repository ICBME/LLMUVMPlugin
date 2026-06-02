import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "scripts"))

import layer1_coverage_feedback_eval as layer1_eval  # noqa: E402


CONFIG_TEXT = """
name = "demo"
toplevel = "demo_top"
driver = "demo_driver:Driver"

[[field]]
name = "mode"
kind = "enum"
choices = ["read", "write"]
"""


class TestLayer1CoverageFeedbackEval(unittest.TestCase):
    def test_layer1_eval_writes_plan_directives_and_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "demo.toml"
            config_path.write_text(CONFIG_TEXT)
            summary = {
                "target": "demo",
                "uncovered_line_count": 1,
                "uvm_functional_coverage": {},
                "stimulus_summary": {"field_counts": {}},
                "rtl_gap_summary": {
                    "top_gaps": [
                        {
                            "id": "gap-stale",
                            "primary_kind": "branch",
                            "module": "demo",
                            "code": "if (round_state == DONE) begin",
                            "context": [],
                            "objects": ["round_state"],
                            "advisor_hints": [
                                {"type": "source_keyword", "value": "state"},
                                {"type": "source_keyword", "value": "round"},
                            ],
                            "evidence": {"point_ids": ["p1"]},
                        },
                        {
                            "id": "gap-mode",
                            "primary_kind": "branch",
                            "module": "demo",
                            "code": "if (mode == write) begin",
                            "context": [],
                            "objects": ["mode"],
                            "advisor_hints": [{"type": "source_keyword", "value": "mode"}],
                            "evidence": {"point_ids": ["p2"]},
                        },
                    ]
                },
            }
            gap_feedback = {
                "gaps": {
                    "gap-stale": {
                        "status": "stale",
                        "next_action": "escalate_to_llm",
                        "stale_count": 2,
                    }
                }
            }
            summary_path = tmp_path / "summary.json"
            gap_feedback_path = tmp_path / "gap_feedback.json"
            directives_out = tmp_path / "directives.json"
            prompt_out = tmp_path / "prompt.json"
            plan_out = tmp_path / "plan.json"
            markdown_out = tmp_path / "plan.md"
            summary_path.write_text(json.dumps(summary))
            gap_feedback_path.write_text(json.dumps(gap_feedback))

            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            old_argv = sys.argv
            os.environ["FUZZ_TARGET_CONFIG"] = str(config_path)
            try:
                sys.argv = [
                    "layer1_coverage_feedback_eval.py",
                    "--summary",
                    str(summary_path),
                    "--gap-feedback",
                    str(gap_feedback_path),
                    "--directives-out",
                    str(directives_out),
                    "--prompt-out",
                    str(prompt_out),
                    "--plan-out",
                    str(plan_out),
                    "--markdown-out",
                    str(markdown_out),
                ]
                status = layer1_eval.main()
            finally:
                sys.argv = old_argv
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config

            directives = json.loads(directives_out.read_text())
            plan = json.loads(plan_out.read_text())
            prompt = json.loads(prompt_out.read_text())
            markdown = markdown_out.read_text()

        self.assertEqual(status, 0)
        self.assertEqual(directives["directives"][0]["gap_ids"], ["gap-mode"])
        self.assertEqual(plan["complex_gaps"][0]["id"], "gap-stale")
        self.assertEqual(prompt["rtl_gap_mutation_prompt"]["complex_rtl_gaps"][0]["id"], "gap-stale")
        self.assertIn("Layer 1 Coverage Feedback Plan", markdown)


if __name__ == "__main__":
    unittest.main()
