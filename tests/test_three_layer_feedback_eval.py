import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "scripts"))

import three_layer_feedback_eval as eval3  # noqa: E402


CONFIG_TEXT = """
name = "demo"
toplevel = "demo_top"
driver = "demo_driver:Driver"

[[field]]
name = "mode"
kind = "enum"
choices = ["read", "write"]
"""


def gap(gap_id, point_ids, code="if (mode == write) begin", kind="branch"):
    return {
        "id": gap_id,
        "primary_kind": kind,
        "priority": 100,
        "file": "demo.v",
        "line": 10,
        "module": "demo",
        "code": code,
        "context": [],
        "objects": ["mode"],
        "advisor_hints": [{"type": "source_keyword", "value": "mode"}],
        "evidence": {"point_ids": point_ids},
    }


def summary(coverage, uncovered_points, gaps, origin_counts=None, bins=None):
    return {
        "target": "demo",
        "uncovered_line_count": len(uncovered_points),
        "stimulus_summary": {
            "total_cases": sum((origin_counts or {}).values()),
            "origin_counts": origin_counts or {},
        },
        "rtl_structure_coverage": {
            "totals": {"coverage": coverage},
            "by_kind": {
                "line": {"coverage": coverage},
                "toggle": {"coverage": coverage},
                "branch": {"coverage": coverage},
                "expression": {"coverage": 1.0},
            },
            "coverage_export": {
                "uncovered_points": [{"id": point_id} for point_id in uncovered_points],
            },
        },
        "rtl_gap_summary": {"top_gaps": gaps},
        "uvm_functional_coverage": {
            "origin_counts": origin_counts or {},
            "bins": bins or {},
            "crosses": {},
        },
    }


def write_run(root, target, name, data, directives):
    run_dir = root / name
    run_dir.mkdir()
    (run_dir / f"{target}_coverage_summary.json").write_text(json.dumps(data))
    (run_dir / f"{target}_mutation_directives.json").write_text(json.dumps(directives))
    functional = dict(data["uvm_functional_coverage"])
    functional["total_cases"] = data["stimulus_summary"]["total_cases"]
    functional["uncovered"] = {}
    (run_dir / f"{target}_uvm_functional_coverage.json").write_text(json.dumps(functional))


class TestThreeLayerFeedbackEval(unittest.TestCase):
    def test_three_layer_eval_writes_aggregate_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "demo.toml"
            config_path.write_text(CONFIG_TEXT)
            baseline = summary(
                0.5,
                ["p1", "p2"],
                [gap("gap-1", ["p1"]), gap("gap-2", ["p2"])],
                origin_counts={},
                bins={"mode": {"read": 1}},
            )
            heuristic = summary(
                0.75,
                ["p2"],
                [gap("gap-2", ["p2"])],
                origin_counts={"directed": 4},
                bins={"mode": {"read": 1, "write": 1}},
            )
            llm = summary(
                0.75,
                ["p2"],
                [gap("gap-2", ["p2"])],
                origin_counts={"directed": 4, "stale": 4},
                bins={"mode": {"read": 1, "write": 1}},
            )
            write_run(
                tmp_path,
                "demo",
                "baseline",
                baseline,
                {"source": "baseline", "directives": [{"name": "directed", "gap_ids": ["gap-1"], "weight": 1}]},
            )
            write_run(
                tmp_path,
                "demo",
                "heuristic",
                heuristic,
                {"source": "heuristic", "directives": [{"name": "stale", "gap_ids": ["gap-2"], "weight": 1}]},
            )
            write_run(
                tmp_path,
                "demo",
                "llm",
                llm,
                {"source": "llm", "directives": []},
            )
            out_dir = tmp_path / "eval"

            old_argv = sys.argv
            old_config = os.environ.get("FUZZ_TARGET_CONFIG")
            os.environ["FUZZ_TARGET_CONFIG"] = str(config_path)
            try:
                sys.argv = [
                    "three_layer_feedback_eval.py",
                    "--target",
                    "demo",
                    "--run-dir",
                    str(tmp_path),
                    "--round",
                    "baseline",
                    "--round",
                    "heuristic",
                    "--round",
                    "llm",
                    "--out-dir",
                    str(out_dir),
                ]
                status = eval3.main()
            finally:
                sys.argv = old_argv
                if old_config is None:
                    os.environ.pop("FUZZ_TARGET_CONFIG", None)
                else:
                    os.environ["FUZZ_TARGET_CONFIG"] = old_config

            report = json.loads((out_dir / "demo_three_layer_feedback_eval.json").read_text())
            markdown = (out_dir / "demo_three_layer_feedback_eval.md").read_text()

        self.assertEqual(status, 0)
        self.assertEqual(report["summary"]["transition_count"], 2)
        self.assertEqual(report["transitions"][0]["layer2"]["resolved_targeted_gap_count"], 1)
        self.assertEqual(report["transitions"][0]["layer3"]["best_direction"], "directed")
        self.assertEqual(report["transitions"][1]["layer2"]["stale_targeted_gap_count"], 1)
        self.assertIn("Three-Layer Feedback Evaluation", markdown)
        self.assertIn("baseline->heuristic", markdown)


if __name__ == "__main__":
    unittest.main()
