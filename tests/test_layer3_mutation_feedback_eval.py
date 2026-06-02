import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "scripts"))

import layer3_mutation_feedback_eval as layer3_eval  # noqa: E402


class TestLayer3MutationFeedbackEval(unittest.TestCase):
    def test_eval_script_writes_feedback_and_updated_directives(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            previous = {
                "target": "demo",
                "uncovered_line_count": 2,
                "stimulus_summary": {"origin_counts": {}},
                "rtl_structure_coverage": {
                    "totals": {"coverage": 0.5},
                    "coverage_export": {
                        "uncovered_points": [{"id": "p1"}, {"id": "p2"}],
                    },
                },
                "rtl_gap_summary": {
                    "top_gaps": [
                        {"id": "gap-1", "evidence": {"point_ids": ["p1"]}},
                        {"id": "gap-2", "evidence": {"point_ids": ["p2"]}},
                    ]
                },
                "uvm_functional_coverage": {
                    "origin_counts": {},
                    "bins": {"mode": {"read": 1}},
                    "crosses": {},
                },
            }
            current = {
                "target": "demo",
                "uncovered_line_count": 1,
                "stimulus_summary": {"origin_counts": {"directed": 4}},
                "rtl_structure_coverage": {
                    "totals": {"coverage": 0.75},
                    "coverage_export": {
                        "uncovered_points": [{"id": "p2"}],
                    },
                },
                "rtl_gap_summary": {
                    "top_gaps": [
                        {"id": "gap-2", "evidence": {"point_ids": ["p2"]}},
                    ]
                },
                "uvm_functional_coverage": {
                    "origin_counts": {"directed": 4},
                    "bins": {"mode": {"read": 1, "write": 1}},
                    "crosses": {},
                },
            }
            directives = {
                "source": "heuristic",
                "directives": [
                    {"target": "demo", "name": "directed", "weight": 1, "gap_ids": ["gap-1"]}
                ],
            }
            previous_path = tmp_path / "previous.json"
            current_path = tmp_path / "current.json"
            directives_path = tmp_path / "directives.json"
            previous_path.write_text(json.dumps(previous))
            current_path.write_text(json.dumps(current))
            directives_path.write_text(json.dumps(directives))

            feedback_out = tmp_path / "feedback.json"
            updated_out = tmp_path / "updated.json"
            markdown_out = tmp_path / "feedback.md"
            args = [
                "--previous-summary",
                str(previous_path),
                "--current-summary",
                str(current_path),
                "--directives",
                str(directives_path),
                "--feedback-out",
                str(feedback_out),
                "--updated-directives-out",
                str(updated_out),
                "--markdown-out",
                str(markdown_out),
            ]

            old_argv = sys.argv
            try:
                sys.argv = ["layer3_mutation_feedback_eval.py", *args]
                status = layer3_eval.main()
            finally:
                sys.argv = old_argv

            feedback = json.loads(feedback_out.read_text())
            updated = json.loads(updated_out.read_text())
            markdown = markdown_out.read_text()

        self.assertEqual(status, 0)
        self.assertEqual(feedback["directions"]["directed"]["decision"], "increase_weight")
        self.assertGreater(updated["directives"][0]["weight"], 1)
        self.assertIn("Layer 3 Mutation Feedback", markdown)
        self.assertIn("directed", markdown)


if __name__ == "__main__":
    unittest.main()
