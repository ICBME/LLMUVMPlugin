import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "scripts"))

import coverage_feedback_compare as compare  # noqa: E402


class TestCoverageFeedbackCompare(unittest.TestCase):
    def test_normalize_modes_always_includes_baseline(self):
        self.assertEqual(
            compare.normalize_modes("heuristic,llm"),
            {"baseline", "heuristic", "llm"},
        )

        with self.assertRaises(SystemExit):
            compare.normalize_modes("baseline,unknown")

    def test_build_comparison_adds_delta_vs_baseline(self):
        rows = [
            {
                "target": "dut",
                "mode": "baseline",
                "cases": 2,
                "applied_directive_source": "none",
                "functional_uncovered": {},
                "uncovered_lines": 4,
                "overall": {"coverage": 0.5},
                "line": {"coverage": 0.25},
                "toggle": {"coverage": 0.5},
                "branch": {"coverage": 0.75},
                "expression": {"coverage": 1.0},
            },
            {
                "target": "dut",
                "mode": "heuristic",
                "cases": 5,
                "applied_directive_source": "generic heuristic",
                "functional_uncovered": {},
                "uncovered_lines": 1,
                "overall": {"coverage": 0.8},
                "line": {"coverage": 0.5},
                "toggle": {"coverage": 0.75},
                "branch": {"coverage": 1.0},
                "expression": {"coverage": 1.0},
            },
        ]

        comparison = compare.build_comparison(rows)
        heuristic = comparison["summary"][1]

        self.assertEqual(heuristic["delta_vs_baseline"]["cases"], 3)
        self.assertEqual(heuristic["delta_vs_baseline"]["uncovered_lines"], -3)
        self.assertEqual(heuristic["delta_vs_baseline"]["overall"], 0.3)

    def test_summarize_run_reads_coverage_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            target = "dut"
            (run_dir / f"{target}_coverage_summary.json").write_text(
                json.dumps(
                    {
                        "stimulus_summary": {"total_cases": 2},
                        "uncovered_line_count": 4,
                        "rtl_structure_coverage": {
                            "totals": {"coverage": 0.5},
                            "by_kind": {
                                "line": {"coverage": 0.25},
                                "toggle": {"coverage": 0.5},
                                "branch": {"coverage": 0.75},
                                "expression": {"coverage": 1.0},
                            },
                        },
                    }
                )
            )
            (run_dir / f"{target}_uvm_functional_coverage.json").write_text(
                json.dumps({"total_cases": 3, "uncovered": {"fields": {"mode": ["x"]}}})
            )

            row = compare.summarize_run(
                compare.RunPaths(target, "baseline", run_dir),
                applied_source="none",
            )

        self.assertEqual(row["cases"], 3)
        self.assertEqual(row["functional_uncovered"], {"fields": {"mode": ["x"]}})
        self.assertEqual(row["branch"], {"coverage": 0.75})

    def test_render_markdown_includes_llm_fallback_note(self):
        rows = [
            {
                "target": "dut",
                "mode": "baseline",
                "run_dir": "baseline",
                "cases": 1,
                "applied_directive_source": "none",
                "functional_uncovered": {},
                "uncovered_lines": 2,
                "overall": {"coverage": 0.5},
                "line": {"coverage": 0.5},
                "toggle": {"coverage": 0.5},
                "branch": {"coverage": 0.5},
                "expression": {"coverage": 0.5},
            },
            {
                "target": "dut",
                "mode": "llm",
                "run_dir": "llm",
                "cases": 1,
                "applied_directive_source": "heuristic; OPENAI_API_KEY not set",
                "functional_uncovered": {},
                "uncovered_lines": 2,
                "overall": {"coverage": 0.5},
                "line": {"coverage": 0.5},
                "toggle": {"coverage": 0.5},
                "branch": {"coverage": 0.5},
                "expression": {"coverage": 0.5},
            },
        ]

        markdown = compare.render_markdown(compare.build_comparison(rows))

        self.assertIn("Coverage Feedback Comparison", markdown)
        self.assertIn("OPENAI_API_KEY", markdown)


if __name__ == "__main__":
    unittest.main()
