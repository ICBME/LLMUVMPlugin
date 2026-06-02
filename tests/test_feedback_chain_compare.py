import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "scripts"))

import feedback_chain_compare as chain  # noqa: E402


def summary(target, coverage, cases, uncovered_lines, open_gaps=0):
    entry = coverage_entry(coverage)
    return {
        "target": target,
        "uncovered_line_count": uncovered_lines,
        "stimulus_summary": {"total_cases": cases, "origin_counts": {}},
        "rtl_structure_coverage": {
            "totals": entry,
            "by_kind": {
                "line": entry,
                "toggle": entry,
                "branch": entry,
                "expression": coverage_entry(1.0),
            },
        },
        "rtl_gap_summary": {
            "top_gaps": [{"id": f"gap-{idx}"} for idx in range(open_gaps)]
        },
        "uvm_functional_coverage": {"origin_counts": {}, "bins": {}, "crosses": {}},
    }


def coverage_entry(coverage):
    hit = int(round(coverage * 100))
    return {
        "coverage": coverage,
        "hit": hit,
        "total": 100,
        "uncovered": 100 - hit,
    }


def write_round(root, target, mode, index, data, source, gap_feedback=None, mutation_feedback=None):
    round_dir = root / target / mode / f"round_{index:02d}"
    round_dir.mkdir(parents=True)
    (round_dir / f"{target}_coverage_summary.json").write_text(json.dumps(data))
    (round_dir / f"{target}_uvm_functional_coverage.json").write_text(
        json.dumps({"total_cases": data["stimulus_summary"]["total_cases"], "uncovered": {}})
    )
    (round_dir / f"{target}_mutation_directives.json").write_text(
        json.dumps({"source": source, "directives": []})
    )
    if gap_feedback is not None:
        (round_dir / f"{target}_gap_feedback.json").write_text(json.dumps(gap_feedback))
    if mutation_feedback is not None:
        (round_dir / f"{target}_mutation_feedback.json").write_text(json.dumps(mutation_feedback))


class TestFeedbackChainCompare(unittest.TestCase):
    def test_normalize_modes_keeps_defined_order(self):
        self.assertEqual(
            chain.normalize_modes("llm_feedback,no_feedback"),
            ("no_feedback", "llm_feedback"),
        )
        with self.assertRaises(SystemExit):
            chain.normalize_modes("unknown")

    def test_reuse_existing_builds_chain_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = "secworks_sha256"
            write_round(
                tmp_path,
                target,
                "no_feedback",
                0,
                summary(target, 0.5, 4, 10),
                "generic heuristic",
            )
            write_round(
                tmp_path,
                target,
                "no_feedback",
                1,
                summary(target, 0.55, 5, 9),
                "generic heuristic",
            )
            write_round(
                tmp_path,
                target,
                "heuristic_feedback",
                0,
                summary(target, 0.5, 4, 10),
                "generic heuristic",
            )
            write_round(
                tmp_path,
                target,
                "heuristic_feedback",
                1,
                summary(target, 0.7, 8, 6, open_gaps=1),
                "generic heuristic",
                gap_feedback={
                    "status_counts": {"resolved": 1},
                    "next_action_counts": {"done": 1},
                },
                mutation_feedback={
                    "directions": {"directed": {"decision": "increase_weight", "score": 1.0}},
                    "aggregate_delta": {"structural_coverage_delta": 0.2},
                },
            )
            write_round(
                tmp_path,
                target,
                "llm_feedback",
                0,
                summary(target, 0.5, 4, 10),
                "llm:langchain:test",
            )
            write_round(
                tmp_path,
                target,
                "llm_feedback",
                1,
                summary(target, 0.8, 9, 3),
                "llm:langchain:test",
                mutation_feedback={
                    "directions": {"llm_directed": {"decision": "increase_weight", "score": 2.0}},
                    "aggregate_delta": {"structural_coverage_delta": 0.3},
                },
            )

            old_argv = sys.argv
            try:
                sys.argv = [
                    "feedback_chain_compare.py",
                    "--target",
                    target,
                    "--modes",
                    "no_feedback,heuristic_feedback,llm_feedback",
                    "--rounds",
                    "2",
                    "--out-dir",
                    str(tmp_path),
                    "--reuse-existing",
                ]
                status = chain.main()
            finally:
                sys.argv = old_argv

            report = json.loads((tmp_path / "feedback_chain_comparison.json").read_text())
            markdown = (tmp_path / "feedback_chain_comparison.md").read_text()
            code_report = json.loads(
                (tmp_path / "feedback_chain_code_coverage.json").read_text()
            )
            code_markdown = (tmp_path / "feedback_chain_code_coverage.md").read_text()

        self.assertEqual(status, 0)
        target_report = report["targets"][0]
        self.assertEqual(target_report["deltas"]["heuristic_feedback"]["overall_vs_no_feedback"], 0.15)
        self.assertEqual(target_report["deltas"]["llm_feedback"]["overall_vs_heuristic_feedback"], 0.1)
        self.assertEqual(
            target_report["modes"][1]["aggregate_feedback"]["layer2_status_counts"],
            {"resolved": 1},
        )
        self.assertIn("Feedback Chain Comparison", markdown)
        self.assertIn("llm_feedback", markdown)
        self.assertEqual(code_report["scope"], "rtl_code_coverage_only")
        target_code_report = code_report["targets"][0]
        self.assertEqual(
            target_code_report["deltas"]["heuristic_feedback"]["vs_no_feedback"]["overall"],
            0.15,
        )
        self.assertEqual(
            target_code_report["deltas"]["llm_feedback"]["vs_heuristic_feedback"]["hit_delta"],
            10,
        )
        self.assertEqual(
            target_code_report["modes"][2]["code_coverage"]["gain_from_first"]["overall"],
            0.3,
        )
        self.assertIn("Feedback Chain Code Coverage", code_markdown)
        self.assertIn("Functional coverage and functional gaps are intentionally omitted", code_markdown)


if __name__ == "__main__":
    unittest.main()
