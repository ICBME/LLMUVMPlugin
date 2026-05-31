from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any, Iterable, Protocol

from fuzz_bfm.corpus import FuzzCase
from fuzz_bfm.plugin_loader import build_plugin
from fuzz_bfm.target_config import FieldSpec, TargetConfig, load_target_config


class FunctionalCoverageProtocol(Protocol):
    target: str

    def sample(self, case: FuzzCase) -> None:
        ...

    def to_json(self) -> dict[str, Any]:
        ...


def build_coverage_model(
    target: str,
    config: TargetConfig | None = None,
) -> FunctionalCoverageProtocol:
    config = config or _try_load_target_config(target)
    if config is not None and config.coverage_model:
        return build_plugin(config.coverage_model, target=target, config=config)
    return GenericCoverageModel(target=target, config=config)


def FunctionalCoverageModel(
    target: str,
    config: TargetConfig | None = None,
) -> FunctionalCoverageProtocol:
    """Backward-compatible factory for the target coverage plugin."""

    return build_coverage_model(target, config=config)


def build_functional_coverage(
    target: str,
    cases: Iterable[FuzzCase],
    config: TargetConfig | None = None,
) -> dict[str, Any]:
    model = build_coverage_model(target, config=config)
    for case in cases:
        model.sample(case)
    return model.to_json()


def build_functional_coverage_from_jsonl(path: Path, target: str) -> dict[str, Any]:
    config = _try_load_target_config(target)
    cases: list[FuzzCase] = []
    if path.exists():
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            data = json.loads(line)
            case_target = str(data.get("target", target))
            if case_target == target:
                cases.append(FuzzCase(case_target, data, line_no))
    return build_functional_coverage(target, cases, config=config)


class CounterCoverageModel:
    def __init__(self, target: str, config: TargetConfig | None = None):
        self.target = target
        self.config = config
        self.total_cases = 0
        self.bins: dict[str, Counter[str]] = {}
        self.crosses: dict[str, Counter[str]] = {}
        self.origins: Counter[str] = Counter()

    def sample(self, case: FuzzCase) -> None:
        if case.target != self.target:
            return
        self.total_cases += 1
        self.origins[str(case.data.get("origin", "unknown"))] += 1
        self.sample_case(case.data)

    def sample_case(self, data: dict[str, Any]) -> None:
        self._hit("target", self.target)

    def to_json(self) -> dict[str, Any]:
        return {
            "domain": "uvm_functional",
            "target": self.target,
            "total_cases": self.total_cases,
            "origin_counts": dict(sorted(self.origins.items())),
            "bins": {key: dict(sorted(value.items())) for key, value in sorted(self.bins.items())},
            "crosses": {key: dict(sorted(value.items())) for key, value in sorted(self.crosses.items())},
            "uncovered": self.uncovered(),
        }

    def uncovered(self) -> dict[str, list[str]]:
        return {}

    def _hit(self, bin_name: str, value: str) -> None:
        self.bins.setdefault(bin_name, Counter())[value] += 1

    def _cross(self, cross_name: str, *values: str) -> None:
        self.crosses.setdefault(cross_name, Counter())["|".join(values)] += 1


class GenericCoverageModel(CounterCoverageModel):
    """Schema-driven functional coverage for arbitrary target manifests."""

    def sample_case(self, data: dict[str, Any]) -> None:
        self._hit("target", self.target)
        fields = self.config.fields if self.config is not None else ()
        previous: tuple[str, str] | None = None
        for field in fields:
            if field.name not in data:
                continue
            value = coverage_value(field, data[field.name])
            self._hit(field.name, value)
            if previous is not None:
                prev_name, prev_value = previous
                self._cross(f"{prev_name}_x_{field.name}", prev_value, value)
            previous = (field.name, value)

    def uncovered(self) -> dict[str, list[str]]:
        uncovered: dict[str, list[str]] = {}
        if self.config is None:
            return uncovered
        for field in self.config.fields:
            expected = tuple(str(choice) for choice in field.choices)
            if expected:
                missing_values = missing(self.bins.get(field.name, Counter()), expected)
                if missing_values:
                    uncovered[field.name] = missing_values
        return uncovered


def coverage_value(field: FieldSpec, value: Any) -> str:
    if field.kind == "hex":
        try:
            raw = bytes.fromhex(str(value))
        except ValueError:
            return "invalid_hex"
        return f"{byte_pattern(raw)}:{length_bucket(len(raw))}"
    return str(value)


def missing(counter: Counter[str], expected: tuple[str, ...]) -> list[str]:
    return [value for value in expected if counter.get(value, 0) == 0]


def byte_pattern(data: bytes) -> str:
    if not data:
        return "empty"
    if all(byte == 0 for byte in data):
        return "zero"
    if all(byte == 0xFF for byte in data):
        return "ff"
    if data == bytes(idx & 0xFF for idx in range(len(data))):
        return "increment"
    if data == bytes(0xFF - (idx & 0xFF) for idx in range(len(data))):
        return "decrement"
    if data in {
        bytes(0xAA if idx % 2 == 0 else 0x55 for idx in range(len(data))),
        bytes(0x55 if idx % 2 == 0 else 0xAA for idx in range(len(data))),
    }:
        return "alternating"
    if data == bytes(1 << (idx % 8) for idx in range(len(data))):
        return "walking_one"
    return "mixed"


def length_bucket(length: int) -> str:
    if length == 0:
        return "0"
    if length <= 15:
        return "1..15"
    if length <= 63:
        return "16..63"
    if length <= 255:
        return "64..255"
    return "256+"


def _try_load_target_config(target: str) -> TargetConfig | None:
    try:
        return load_target_config(target)
    except (FileNotFoundError, RuntimeError, ValueError):
        return None
