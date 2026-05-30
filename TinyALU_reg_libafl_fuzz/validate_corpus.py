"""Validate a TinyALU LibAFL JSONL corpus."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys


LEGAL_OPS = {1: "ADD", 2: "AND", 3: "XOR", 4: "MUL"}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_corpus.py CORPUS.jsonl", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    cases = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        data = json.loads(line)
        a = int(data["a"])
        b = int(data["b"])
        op = int(data["op"])
        if not 0 <= a <= 0xFF:
            raise ValueError(f"{path}:{line_no}: invalid A={a}")
        if not 0 <= b <= 0xFF:
            raise ValueError(f"{path}:{line_no}: invalid B={b}")
        if op not in LEGAL_OPS:
            raise ValueError(f"{path}:{line_no}: invalid op={op}")
        cases.append((a, b, op))

    if not cases:
        raise ValueError(f"{path}: no cases found")

    op_counts = Counter(op for _, _, op in cases)
    missing = set(LEGAL_OPS) - set(op_counts)
    if missing:
        raise ValueError(f"{path}: missing op coverage: {sorted(missing)}")

    required = {(0x00, 0x00, op) for op in LEGAL_OPS}
    required |= {(0xFF, 0xFF, op) for op in LEGAL_OPS}
    missing_required = required - set(cases)
    if missing_required:
        raise ValueError(f"{path}: missing mandatory cases: {sorted(missing_required)}")

    summary = ", ".join(f"{LEGAL_OPS[op]}={op_counts[op]}" for op in sorted(op_counts))
    print(f"{path}: {len(cases)} valid cases ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
