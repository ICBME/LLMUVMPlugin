"""Subprocess worker for ref model candidate evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .ref_model import build_candidate_ref_model, golden_case_issues
from .schema import CodegenEvaluation, CodegenEvaluationIssue, GoldenCase, write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--plugin-spec", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--golden-cases", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    candidate_dir = Path(args.candidate_dir)
    output_path = Path(args.out)
    try:
        cases = load_golden_cases(Path(args.golden_cases), default_target=args.target)
        plugin = build_candidate_ref_model(
            args.plugin_spec,
            candidate_dir=candidate_dir,
            target=args.target,
        )
        issues = golden_case_issues(plugin, cases)
        evaluation = CodegenEvaluation(
            passed=not any(issue.blocking for issue in issues),
            issues=issues,
            metadata={
                "stage": "golden_case" if cases else "contract",
                "ref_model": args.plugin_spec,
                "golden_case_count": len(cases),
                "execution": "subprocess",
            },
        )
    except Exception as exc:  # noqa: BLE001 - worker must return structured feedback
        evaluation = CodegenEvaluation.failed(
            CodegenEvaluationIssue(
                stage="contract",
                path=args.plugin_spec,
                message=f"{type(exc).__name__}: {exc}",
            ),
            stage="contract",
            ref_model=args.plugin_spec,
            execution="subprocess",
        )
    write_json(output_path, evaluation)
    return 0


def load_golden_cases(path: Path, *, default_target: str) -> tuple[GoldenCase, ...]:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("golden cases file must contain a list")
    return tuple(GoldenCase.from_value(item, default_target=default_target) for item in value)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
