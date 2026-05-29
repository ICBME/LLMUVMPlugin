"""Hypothesis-driven legal fuzz case generation for TinyALU_reg.

This module is intentionally pure Python. It creates semantic ALU operation
cases; the pyUVM testbench is responsible for playing them through the BFM.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Iterable

try:
    from hypothesis import HealthCheck, given, seed, settings
    from hypothesis import strategies as st
except ModuleNotFoundError as exc:  # pragma: no cover - simulator env dependent
    raise RuntimeError(
        "Hypothesis is required for TinyALU_reg_fuzz. Run this test with the "
        "uv project environment that has Hypothesis installed."
    ) from exc


BYTE_EDGES = (0x00, 0x01, 0x02, 0x03, 0x7F, 0x80, 0xFE, 0xFF)


@dataclass(frozen=True)
class TinyAluFuzzCase:
    """A legal semantic TinyALU transaction."""

    a: int
    b: int
    op: int
    origin: str = "hypothesis"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class FuzzConfig:
    """Runtime knobs supplied by environment variables."""

    seed: int = 1
    max_examples: int = 32
    replay_path: Path | None = None
    corpus_out: Path | None = None

    @classmethod
    def from_env(cls) -> "FuzzConfig":
        replay = os.getenv("FUZZ_REPLAY")
        corpus_out = os.getenv("FUZZ_CORPUS_OUT")
        return cls(
            seed=int(os.getenv("FUZZ_SEED", "1"), 0),
            max_examples=int(os.getenv("FUZZ_MAX_EXAMPLES", "32"), 0),
            replay_path=Path(replay) if replay else None,
            corpus_out=Path(corpus_out) if corpus_out else None,
        )


def _op_values(ops: Iterable[object]) -> tuple[int, ...]:
    values = tuple(int(op) for op in ops)
    if not values:
        raise ValueError("at least one legal operation is required")
    return values


def _mandatory_cases(ops: tuple[int, ...]) -> list[TinyAluFuzzCase]:
    cases: list[TinyAluFuzzCase] = []
    for op in ops:
        cases.append(TinyAluFuzzCase(0x00, 0x00, op, "mandatory_op"))
        cases.append(TinyAluFuzzCase(0xFF, 0xFF, op, "mandatory_max"))

    directed_pairs = (
        (0x00, 0xFF),
        (0xFF, 0x00),
        (0x7F, 0x80),
        (0x55, 0xAA),
        (0xAA, 0x55),
    )
    for op in ops:
        for a, b in directed_pairs:
            cases.append(TinyAluFuzzCase(a, b, op, "directed_pair"))
    return cases


@st.composite
def tinyalu_case_strategy(draw, ops: tuple[int, ...]) -> TinyAluFuzzCase:
    """Build one legal TinyALU transaction from LLM/BFM constraints.

    TinyALU's current legal BFM contract is:
    - A and B are 8-bit unsigned operands.
    - op is one of the legal operation enum values.
    - No illegal op is generated, because the existing monitor converts op to
      the Ops enum and the base sequence polls done.
    """

    edge_byte = st.sampled_from(BYTE_EDGES)
    byte = st.one_of(edge_byte, st.integers(min_value=0, max_value=0xFF))
    return TinyAluFuzzCase(
        a=draw(byte),
        b=draw(byte),
        op=draw(st.sampled_from(ops)),
    )


def _dedupe(cases: Iterable[TinyAluFuzzCase]) -> list[TinyAluFuzzCase]:
    seen: set[tuple[int, int, int]] = set()
    unique: list[TinyAluFuzzCase] = []
    for case in cases:
        key = (case.a, case.b, case.op)
        if key in seen:
            continue
        seen.add(key)
        unique.append(case)
    return unique


def load_cases(path: Path) -> list[TinyAluFuzzCase]:
    cases: list[TinyAluFuzzCase] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        cases.append(
            TinyAluFuzzCase(
                a=int(data["a"]),
                b=int(data["b"]),
                op=int(data["op"]),
                origin=str(data.get("origin", "replay")),
            )
        )
    return cases


def save_cases(path: Path, cases: Iterable[TinyAluFuzzCase]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(case.to_json() for case in cases) + "\n")


def generate_tinyalu_cases(
    ops: Iterable[object],
    config: FuzzConfig | None = None,
) -> list[TinyAluFuzzCase]:
    """Generate legal fuzz cases and optionally save/replay a JSONL corpus."""

    config = config or FuzzConfig()
    if config.replay_path is not None:
        cases = load_cases(config.replay_path)
        if config.corpus_out is not None:
            save_cases(config.corpus_out, cases)
        return cases

    op_values = _op_values(ops)
    generated: list[TinyAluFuzzCase] = []

    @seed(config.seed)
    @settings(
        database=None,
        deadline=None,
        max_examples=config.max_examples,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tinyalu_case_strategy(op_values))
    def collect(case: TinyAluFuzzCase) -> None:
        generated.append(case)

    collect()
    cases = _dedupe([*_mandatory_cases(op_values), *generated])
    if config.corpus_out is not None:
        save_cases(config.corpus_out, cases)
    return cases
