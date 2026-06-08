#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
PY_DIR = REPO_ROOT / "libafl_bfm_fuzz" / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from fuzz_pipeline.harness import close_observation  # noqa: E402
from fuzz_pipeline.run_orchestrator import FuzzRunConfig, run_generate_corpus_pipeline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "generate-corpus":
        return generate_corpus(args)
    raise SystemExit(f"unknown command: {args.command}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run libafl_bfm_fuzz pipeline stages.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-corpus")
    generate.add_argument("--target", required=True)
    generate.add_argument("--target-config", type=Path)
    generate.add_argument("--corpus-out", type=Path, required=True)
    generate.add_argument("--libafl-manifest", type=Path, required=True)
    generate.add_argument("--directives", type=Path)
    generate.add_argument("--iters", type=int, default=256)
    generate.add_argument("--max-seeds", type=int, default=32)
    generate.add_argument("--seed", type=int, default=1)
    generate.add_argument("--cargo", default="cargo")
    generate.add_argument("--topology-out", type=Path)
    generate.add_argument("--cwd", type=Path)
    return parser.parse_args(argv)


def generate_corpus(args: argparse.Namespace) -> int:
    try:
        cases = run_generate_corpus_pipeline(
            FuzzRunConfig(
                target=args.target,
                target_config=args.target_config,
                corpus=args.corpus_out,
                libafl_manifest=args.libafl_manifest,
                directives=args.directives,
                iters=args.iters,
                max_seeds=args.max_seeds,
                seed=args.seed,
                cargo=args.cargo,
                cwd=args.cwd,
                topology_out=args.topology_out,
            )
        )
    finally:
        close_observation()
    print(f"generated and validated {len(cases)} {args.target} cases in {args.corpus_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
