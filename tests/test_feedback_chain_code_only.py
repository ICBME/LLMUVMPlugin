import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "libafl_bfm_fuzz" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import feedback_chain_code_only as code_only  # noqa: E402
import feedback_chain_compare as chain  # noqa: E402


def coverage_entry(coverage):
    hit = int(round(coverage * 100))
    return {
        "coverage": coverage,
        "hit": hit,
        "total": 100,
        "uncovered": 100 - hit,
    }


def summary(target, coverage, cases, uncovered_lines):
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
        "rtl_gap_summary": {"top_gaps": []},
        "uvm_functional_coverage": {
            "origin_counts": {},
            "bins": {},
            "crosses": {},
            "uncovered": {"fields": {"mode": ["write"]}},
        },
        "uvm_functional_coverage_source": "ignored_by_request",
    }


def write_round(root, target, mode, data):
    round_dir = root / target / mode / "round_00"
    round_dir.mkdir(parents=True)
    (round_dir / f"{target}_coverage_summary.json").write_text(json.dumps(data))
    (round_dir / f"{target}_uvm_functional_coverage.json").write_text(
        json.dumps({"total_cases": 99, "uncovered": {"fields": {"mode": ["read"]}}})
    )
    (round_dir / f"{target}_mutation_directives.json").write_text(
        json.dumps({"source": "generic heuristic", "directives": []})
    )


class TestFeedbackChainCodeOnly(unittest.TestCase):
    def test_reuse_existing_ignores_functional_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = "secworks_sha256"
            write_round(tmp_path, target, "no_feedback", summary(target, 0.5, 4, 10))

            old_argv = sys.argv
            try:
                sys.argv = [
                    "feedback_chain_code_only.py",
                    "--target",
                    target,
                    "--modes",
                    "no_feedback",
                    "--rounds",
                    "1",
                    "--out-dir",
                    str(tmp_path),
                    "--reuse-existing",
                ]
                status = code_only.main()
            finally:
                sys.argv = old_argv

            report = json.loads(
                (tmp_path / "feedback_chain_code_only_comparison.json").read_text()
            )
            markdown = (tmp_path / "feedback_chain_code_only_coverage.md").read_text()

        self.assertEqual(status, 0)
        self.assertTrue(report["functional_coverage_ignored"])
        self.assertEqual(
            report["targets"][0]["modes"][0]["final"]["functional_uncovered"],
            {},
        )
        self.assertIn("Feedback Chain Code-Only Coverage", markdown)
        self.assertIn("Feedback generation and evaluation both ignore", markdown)

    def test_feedback_command_forces_ignore_functional_coverage(self):
        paths = chain.RoundPaths(
            "demo",
            "heuristic_feedback",
            1,
            Path("/tmp/demo/heuristic_feedback/round_01"),
        )
        previous = chain.RoundPaths(
            "demo",
            "heuristic_feedback",
            0,
            Path("/tmp/demo/heuristic_feedback/round_00"),
        )

        cmd = code_only.feedback_command(
            paths,
            previous=previous,
            use_uv=False,
            llm_feedback=True,
            llm_model="test-model",
        )

        self.assertIn("--ignore-functional-coverage", cmd)
        self.assertNotIn("--functional-coverage", cmd)
        self.assertIn("--previous-summary", cmd)
        self.assertIn("--llm", cmd)
        self.assertEqual(cmd[-2:], ["--model", "test-model"])

    def test_coverage_report_command_redirects_functional_output(self):
        paths = chain.RoundPaths("demo", "no_feedback", 0, Path("/tmp/demo/no_feedback/round_00"))
        spec = chain.TargetSpec("demo", "demo_top", "ignored/*.v")
        old_rtl_sources = code_only.chain.rtl_sources
        try:
            code_only.chain.rtl_sources = lambda _spec: "/tmp/demo.v"
            cmd = code_only.coverage_report_command(
                spec,
                paths,
                previous=None,
                use_uv=False,
                libafl_iters=1,
                libafl_max_seeds=1,
                libafl_seed=7,
            )
        finally:
            code_only.chain.rtl_sources = old_rtl_sources

        self.assertIn(f"UVM_FUNCTIONAL_COVERAGE_OUT={paths.ignored_functional}", cmd)
        self.assertIn("coverage-report", cmd)
        self.assertNotIn("coverage-feedback", cmd)


if __name__ == "__main__":
    unittest.main()
