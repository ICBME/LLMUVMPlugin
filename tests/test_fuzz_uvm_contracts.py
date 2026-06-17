import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_bfm.bfm_base import ReplayResult  # noqa: E402
from fuzz_bfm.target_config import TargetConfig  # noqa: E402
from fuzz_uvm.contracts import (  # noqa: E402
    ExpectedResult,
    PluginContractError,
    normalize_comparison,
    normalize_expected,
    validate_coverage_plugin,
)
from fuzz_uvm.scoreboards import ResultScoreboard, build_scoreboard  # noqa: E402
from fuzz_uvm.transactions import ReplayRecord  # noqa: E402


class AttributeExpected:
    expected = "attr-value"
    detail = "attr-detail"
    metadata = {"source": "attribute"}


class PassingComparator:
    def compare(self, actual, expected, record):
        return {"passed": True, "detail": f"accepted {record.index}"}


class FailingComparator:
    def compare(self, actual, expected, record):
        return {"passed": False, "reason": "forced mismatch"}


class BadCoverageSummary:
    def sample(self, case):
        return None

    def sample_record(self, record):
        return None

    def to_json(self):
        return {"bad": object()}


class TestFuzzUvmContracts(unittest.TestCase):
    def test_normalize_expected_accepts_contract_shapes(self):
        self.assertEqual(normalize_expected(ExpectedResult("ok")).expected, "ok")

        mapped = normalize_expected(
            {
                "expected": {"status": "ok"},
                "detail": "mapped",
                "metadata": {"rule": "demo"},
            }
        )
        self.assertEqual(mapped.expected, {"status": "ok"})
        self.assertEqual(mapped.detail, "mapped")
        self.assertEqual(mapped.metadata["rule"], "demo")

        attr = normalize_expected(AttributeExpected())
        self.assertEqual(attr.expected, "attr-value")
        self.assertEqual(attr.detail, "attr-detail")

        raw = normalize_expected("raw-value")
        self.assertEqual(raw.expected, "raw-value")

    def test_normalize_expected_rejects_ambiguous_mapping(self):
        with self.assertRaisesRegex(PluginContractError, "expected"):
            normalize_expected({"value": "missing-key"})

    def test_normalize_comparison_requires_bool_passed(self):
        with self.assertRaisesRegex(PluginContractError, "passed"):
            normalize_comparison({"passed": "false"})

    def test_result_scoreboard_uses_custom_comparator(self):
        scoreboard = ResultScoreboard("demo", comparator=PassingComparator())
        record = _record(actual="rtl", expected="model")

        scoreboard.write(record)
        scoreboard.check()

        self.assertEqual(scoreboard.summary()["failures"], 0)

    def test_result_scoreboard_reports_comparator_failure(self):
        scoreboard = ResultScoreboard("demo", comparator=FailingComparator())
        scoreboard.write(_record(actual="rtl", expected="model"))

        with self.assertRaisesRegex(AssertionError, "forced mismatch"):
            scoreboard.check()

    def test_build_scoreboard_loads_configured_comparator(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "demo_compare.py").write_text(
                "\n".join(
                    [
                        "class AlwaysPassComparator:",
                        "    def __init__(self, target=None, config=None):",
                        "        self.target = target",
                        "        self.config = config",
                        "",
                        "    def compare(self, actual, expected, record):",
                        "        return {'passed': True}",
                        "",
                    ]
                )
            )
            sys.path.insert(0, str(tmp_path))
            try:
                config = TargetConfig(
                    name="demo",
                    driver="demo_driver:Driver",
                    path=tmp_path / "demo.toml",
                    comparator="demo_compare:AlwaysPassComparator",
                )

                scoreboard = build_scoreboard(config)
                scoreboard.write(_record(actual="rtl", expected="model"))
                scoreboard.check()

                self.assertEqual(scoreboard.summary()["failures"], 0)
            finally:
                sys.path.remove(str(tmp_path))

    def test_coverage_contract_requires_json_summary(self):
        with self.assertRaisesRegex(PluginContractError, "JSON-serializable"):
            validate_coverage_plugin(BadCoverageSummary(), spec="bad coverage")


def _record(*, actual, expected):
    return ReplayRecord(
        index=3,
        case=SimpleNamespace(line_no=12, data={"op": "demo"}),
        result=ReplayResult(actual=actual, expected=expected, detail="demo replay"),
    )


if __name__ == "__main__":
    unittest.main()
