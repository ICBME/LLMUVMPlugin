import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "scripts"))

import layer2_gap_feedback_eval as layer2_eval  # noqa: E402
from fuzz_feedback.feedback_loop import build_gap_feedback  # noqa: E402


def summary_with_gaps(uncovered_points, gaps):
    return {
        "target": "demo",
        "rtl_structure_coverage": {
            "totals": {"coverage": 0.5},
            "coverage_export": {
                "uncovered_points": [{"id": point_id} for point_id in uncovered_points]
            },
        },
        "rtl_gap_summary": {"top_gaps": gaps},
        "stimulus_summary": {"origin_counts": {}},
        "uvm_functional_coverage": {"origin_counts": {}, "bins": {}, "crosses": {}},
    }


def gap(gap_id, point_ids, kind="branch", priority=100):
    return {
        "id": gap_id,
        "primary_kind": kind,
        "priority": priority,
        "file": "demo.v",
        "line": 10,
        "module": "demo",
        "code": "if (mode) begin",
        "point_count": len(point_ids),
        "evidence": {"point_ids": point_ids},
    }


class TestLayer2GapFeedbackEval(unittest.TestCase):
    def test_gap_feedback_classifies_core_gap_states(self):
        previous = summary_with_gaps(
            ["p1", "p2", "p3", "p4"],
            [
                gap("gap-resolved", ["p1"]),
                gap("gap-improved", ["p2", "p3"]),
                gap("gap-stale", ["p4"]),
            ],
        )
        current = summary_with_gaps(
            ["p2", "p4", "p5"],
            [
                gap("gap-improved", ["p2", "p3"]),
                gap("gap-stale", ["p4"]),
                gap("gap-new", ["p5"]),
            ],
        )
        directives = {
            "directives": [
                {"name": "try_improved", "gap_ids": ["gap-improved"]},
                {"name": "try_stale", "gap_ids": ["gap-stale"]},
            ]
        }
        previous_gap_feedback = {
            "gaps": {
                "gap-stale": {
                    "status": "stale",
                    "stale_count": 1,
                    "attempt_count": 1,
                    "attempted_directives": ["try_stale_old"],
                }
            }
        }

        feedback = build_gap_feedback(
            previous,
            current,
            directives,
            previous_gap_feedback=previous_gap_feedback,
            mutation_feedback={
                "directions": {
                    "try_stale": {"decision": "decrease_weight"},
                    "try_improved": {"decision": "increase_weight"},
                }
            },
        )

        gaps = feedback["gaps"]
        self.assertEqual(gaps["gap-resolved"]["status"], "resolved")
        self.assertEqual(gaps["gap-resolved"]["next_action"], "done")
        self.assertEqual(gaps["gap-improved"]["status"], "improved")
        self.assertEqual(gaps["gap-improved"]["next_action"], "continue")
        self.assertEqual(gaps["gap-stale"]["status"], "stale")
        self.assertEqual(gaps["gap-stale"]["stale_count"], 2)
        self.assertEqual(gaps["gap-stale"]["next_action"], "escalate_to_llm")
        self.assertEqual(gaps["gap-new"]["status"], "new")
        self.assertEqual(gaps["gap-new"]["next_action"], "plan")
        self.assertEqual(feedback["status_counts"]["resolved"], 1)
        self.assertEqual(feedback["next_action_counts"]["escalate_to_llm"], 1)

    def test_gap_feedback_eval_script_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            previous = summary_with_gaps(["p1"], [gap("gap-resolved", ["p1"])])
            current = summary_with_gaps([], [])
            directives = {"directives": [{"name": "directed", "gap_ids": ["gap-resolved"]}]}
            previous_path = tmp_path / "previous.json"
            current_path = tmp_path / "current.json"
            directives_path = tmp_path / "directives.json"
            previous_path.write_text(json.dumps(previous))
            current_path.write_text(json.dumps(current))
            directives_path.write_text(json.dumps(directives))
            feedback_out = tmp_path / "gap_feedback.json"
            markdown_out = tmp_path / "gap_feedback.md"

            old_argv = sys.argv
            try:
                sys.argv = [
                    "layer2_gap_feedback_eval.py",
                    "--previous-summary",
                    str(previous_path),
                    "--current-summary",
                    str(current_path),
                    "--directives",
                    str(directives_path),
                    "--feedback-out",
                    str(feedback_out),
                    "--markdown-out",
                    str(markdown_out),
                ]
                status = layer2_eval.main()
            finally:
                sys.argv = old_argv

            feedback = json.loads(feedback_out.read_text())
            markdown = markdown_out.read_text()

        self.assertEqual(status, 0)
        self.assertEqual(feedback["gaps"]["gap-resolved"]["status"], "resolved")
        self.assertIn("Layer 2 Gap Feedback", markdown)
        self.assertIn("gap-resolved", markdown)


if __name__ == "__main__":
    unittest.main()
