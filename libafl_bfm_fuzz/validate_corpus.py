from __future__ import annotations

import argparse
from pathlib import Path
import sys


THIS_DIR = Path(__file__).resolve().parent
PY_DIR = THIS_DIR / "py"
if str(PY_DIR) not in sys.path:
    sys.path.insert(0, str(PY_DIR))

from fuzz_bfm.corpus import load_cases  # noqa: E402
from fuzz_bfm.target_config import load_target_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate LibAFL BFM JSONL corpus")
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()

    config = load_target_config(args.target)
    cases = load_cases(args.corpus, args.target, config=config)
    print(f"validated {len(cases)} {args.target} cases in {args.corpus}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
