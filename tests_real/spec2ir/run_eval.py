"""CLI for running Spec2IR real-data evaluation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .datasets import load_verilogeval_cases, verilogeval_root_from_env
from .metrics import aggregate_results
from .runner import run_verilogeval_cases


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=["verilogeval"], default="verilogeval")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, default=None)
    parser.add_argument("--with-llm", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--backend", default=None)
    parser.add_argument("--include-ir", action="store_true")
    args = parser.parse_args(argv)

    root = args.root or verilogeval_root_from_env()
    cases = load_verilogeval_cases(root, limit=args.limit)
    if not cases:
        raise SystemExit(f"no VerilogEval cases found under {root}")

    work_root = args.work_root or args.out.with_suffix("").with_name(f"{args.out.stem}_work")
    results = run_verilogeval_cases(
        cases,
        work_root=work_root,
        with_llm=args.with_llm,
        model=args.model,
        backend_name=args.backend,
    )

    payload = {
        "dataset": args.dataset,
        "root": str(root),
        "limit": args.limit,
        "with_llm": args.with_llm,
        "metrics": aggregate_results(results),
        "cases": [result.to_dict(include_ir=args.include_ir) for result in results],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    unavailable = sum(1 for result in results if result.status == "llm_unavailable")
    return 0 if payload["metrics"]["crash_count"] == 0 and unavailable == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
