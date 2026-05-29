"""Hypothesis-driven legal fuzz case generation for TinyALU_reg.

This module is intentionally pure Python. It creates semantic ALU operation
cases; the pyUVM testbench is responsible for playing them through the BFM.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import product
import json
import os
from pathlib import Path
from typing import Any, Iterable

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
    directives_path: Path | None = None

    @classmethod
    def from_env(cls) -> "FuzzConfig":
        replay = os.getenv("FUZZ_REPLAY")
        corpus_out = os.getenv("FUZZ_CORPUS_OUT")
        directives = os.getenv("FUZZ_DIRECTIVES")
        return cls(
            seed=int(os.getenv("FUZZ_SEED", "1"), 0),
            max_examples=int(os.getenv("FUZZ_MAX_EXAMPLES", "32"), 0),
            replay_path=Path(replay) if replay else None,
            corpus_out=Path(corpus_out) if corpus_out else None,
            directives_path=Path(directives) if directives else None,
        )


def _op_values(ops: Iterable[object]) -> tuple[int, ...]:
    values = tuple(int(op) for op in ops)
    if not values:
        raise ValueError("at least one legal operation is required")
    return values


def _op_name_map(ops: Iterable[object]) -> dict[str, int]:
    result: dict[str, int] = {}
    for op in ops:
        name = getattr(op, "name", None)
        if name is not None:
            result[str(name).upper()] = int(op)
    return result


def _as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise TypeError(f"cannot convert {value!r} to int")


def _resolve_ops(
    directive: dict[str, Any],
    legal_ops: tuple[int, ...],
    op_names: dict[str, int],
) -> tuple[int, ...]:
    raw_ops = directive.get("ops", directive.get("op_bias", directive.get("op")))
    if raw_ops is None:
        return legal_ops
    if not isinstance(raw_ops, list):
        raw_ops = [raw_ops]

    resolved: list[int] = []
    for raw_op in raw_ops:
        if isinstance(raw_op, str) and not raw_op.startswith(("0x", "0X")):
            op_value = op_names.get(raw_op.upper())
            if op_value is None:
                continue
        else:
            op_value = _as_int(raw_op)
        if op_value in legal_ops:
            resolved.append(op_value)
    return tuple(dict.fromkeys(resolved)) or legal_ops


def load_directives(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    directives = data.get("directives", data if isinstance(data, list) else [])
    if not isinstance(directives, list):
        raise ValueError("mutation directives must be a list or contain directives[]")
    return [directive for directive in directives if isinstance(directive, dict)]


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


def _constraint_pairs(constraints: Iterable[str]) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    normalized = {constraint.upper() for constraint in constraints}
    if normalized & {"ADD_OVERFLOW", "A_PLUS_B_GE_256"}:
        pairs.extend(((0xFF, 0xFF), (0xFF, 0x01), (0x80, 0x80), (0xFE, 0x02)))
    if normalized & {"MUL_NONZERO", "MUL_PIPELINE", "THREE_CYCLE"}:
        pairs.extend(((0x01, 0xFF), (0x7F, 0x80), (0x80, 0x80), (0xFF, 0xFE)))
    if normalized & {"BIT_PATTERN", "TOGGLE_DENSE"}:
        pairs.extend(((0x55, 0xAA), (0xAA, 0x55), (0x0F, 0xF0), (0xF0, 0x0F)))
    return pairs


def _directive_cases(
    directives: list[dict[str, Any]],
    legal_ops: tuple[int, ...],
    op_names: dict[str, int],
) -> list[TinyAluFuzzCase]:
    cases: list[TinyAluFuzzCase] = []
    for idx, directive in enumerate(directives):
        ops = _resolve_ops(directive, legal_ops, op_names)
        origin = str(directive.get("name", f"directive_{idx}"))
        pairs: list[tuple[int, int]] = []

        for pair in directive.get("operand_pairs", ()):
            if not isinstance(pair, dict):
                continue
            pairs.append((_as_int(pair["a"]), _as_int(pair["b"])))

        operands = directive.get("operands", {})
        if isinstance(operands, dict) and "A" in operands and "B" in operands:
            a_values = [_as_int(value) for value in operands["A"]]
            b_values = [_as_int(value) for value in operands["B"]]
            pairs.extend(product(a_values, b_values))

        pairs.extend(_constraint_pairs(directive.get("constraints", ())))
        for op in ops:
            for a, b in pairs:
                cases.append(TinyAluFuzzCase(a & 0xFF, b & 0xFF, op, origin))
    return cases


def _directive_strategy(
    directives: list[dict[str, Any]],
    legal_ops: tuple[int, ...],
    op_names: dict[str, int],
):
    strategies = []
    for idx, directive in enumerate(directives):
        ops = _resolve_ops(directive, legal_ops, op_names)
        origin = str(directive.get("name", f"directive_{idx}"))
        constraints = {item.upper() for item in directive.get("constraints", ())}

        if constraints & {"ADD_OVERFLOW", "A_PLUS_B_GE_256"}:
            add_op = op_names.get("ADD")
            if add_op in legal_ops:
                @st.composite
                def add_overflow(draw, op=add_op, origin=origin):
                    a = draw(st.integers(min_value=1, max_value=0xFF))
                    b = draw(st.integers(min_value=max(0, 0x100 - a), max_value=0xFF))
                    return TinyAluFuzzCase(a, b, op, origin)

                strategies.append(add_overflow())
                continue

        pairs = _directive_cases([directive], legal_ops, op_names)
        if pairs:
            strategies.append(st.sampled_from(pairs))
            continue

        edge_byte = st.sampled_from(BYTE_EDGES)
        byte = st.one_of(edge_byte, st.integers(min_value=0, max_value=0xFF))

        @st.composite
        def generic_directive(draw, ops=ops, origin=origin):
            return TinyAluFuzzCase(
                a=draw(byte),
                b=draw(byte),
                op=draw(st.sampled_from(ops)),
                origin=origin,
            )

        weight = max(1, int(directive.get("weight", 1)))
        strategies.extend(generic_directive() for _ in range(weight))

    if not strategies:
        return None
    return st.one_of(strategies)


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

    ops = tuple(ops)
    op_values = _op_values(ops)
    directives = (
        load_directives(config.directives_path)
        if config.directives_path is not None
        else []
    )
    op_names = _op_name_map(ops)
    directive_strategy = _directive_strategy(directives, op_values, op_names)
    strategy = tinyalu_case_strategy(op_values)
    if directive_strategy is not None:
        strategy = st.one_of(strategy, directive_strategy)
    generated: list[TinyAluFuzzCase] = []

    @seed(config.seed)
    @settings(
        database=None,
        deadline=None,
        max_examples=config.max_examples,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(strategy)
    def collect(case: TinyAluFuzzCase) -> None:
        generated.append(case)

    collect()
    cases = _dedupe(
        [
            *_mandatory_cases(op_values),
            *_directive_cases(directives, op_values, op_names),
            *generated,
        ]
    )
    if config.corpus_out is not None:
        save_cases(config.corpus_out, cases)
    return cases
