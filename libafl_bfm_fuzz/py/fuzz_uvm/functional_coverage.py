from __future__ import annotations

from collections import Counter
from itertools import product
import json
from pathlib import Path
from typing import Any, Iterable, Protocol

from fuzz_bfm.corpus import FuzzCase
from fuzz_bfm.plugin_loader import build_plugin
from fuzz_bfm.target_config import CoverpointSpec, FieldSpec, TargetConfig, load_target_config


MAX_EXPECTED_CROSS_BINS = 1024


class FunctionalCoverageProtocol(Protocol):
    target: str

    def sample(self, case: FuzzCase) -> None:
        ...

    def sample_record(self, record: Any) -> None:
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

    def sample_record(self, record: Any) -> None:
        if getattr(record, "error", None) is None:
            self.sample(record.case)

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
            "coverage": self.coverage_summary(),
            "uncovered": self.uncovered(),
        }

    def uncovered(self) -> dict[str, Any]:
        return {}

    def expected_bins(self) -> dict[str, tuple[str, ...]]:
        return {}

    def expected_crosses(self) -> dict[str, tuple[str, ...]]:
        return {}

    def coverage_summary(self) -> dict[str, int | float | None]:
        expected_bins = self.expected_bins()
        expected_crosses = self.expected_crosses()
        total_bins = sum(len(values) for values in expected_bins.values())
        total_crosses = sum(len(values) for values in expected_crosses.values())
        covered_bins = sum(
            1
            for name, values in expected_bins.items()
            for value in values
            if self.bins.get(name, Counter()).get(value, 0) > 0
        )
        covered_crosses = sum(
            1
            for name, values in expected_crosses.items()
            for value in values
            if self.crosses.get(name, Counter()).get(value, 0) > 0
        )
        total = total_bins + total_crosses
        covered = covered_bins + covered_crosses
        percent = round((covered / total) * 100.0, 2) if total else None
        return {
            "covered_bins": covered_bins,
            "total_bins": total_bins,
            "covered_crosses": covered_crosses,
            "total_crosses": total_crosses,
            "covered": covered,
            "total": total,
            "percent": percent,
        }

    def _hit(self, bin_name: str, value: str) -> None:
        self.bins.setdefault(bin_name, Counter())[value] += 1

    def _cross(self, cross_name: str, *values: str) -> None:
        self.crosses.setdefault(cross_name, Counter())["|".join(values)] += 1


class GenericCoverageModel(CounterCoverageModel):
    """Schema-driven functional coverage for arbitrary target manifests."""

    def sample_case(self, data: dict[str, Any]) -> None:
        self._hit("target", self.target)
        fields = self.config.fields if self.config is not None else ()
        field_by_name = {field.name: field for field in fields}
        sampled_values: dict[str, str] = {}
        previous: tuple[str, str] | None = None
        for field in fields:
            if field.name not in data:
                continue
            value = coverage_value(field, data[field.name])
            sampled_values[field.name] = value
            self._hit(field.name, value)
            if previous is not None:
                prev_name, prev_value = previous
                self._cross(f"{prev_name}_x_{field.name}", prev_value, value)
            previous = (field.name, value)

        coverpoints = self.config.coverpoints if self.config is not None else ()
        for coverpoint in coverpoints:
            field = field_by_name.get(coverpoint.field)
            if field is None or coverpoint.field not in data:
                continue
            value = coverpoint_value(coverpoint, field, data[coverpoint.field])
            sampled_values[coverpoint.name] = value
            self._hit(coverpoint.name, value)

        crosses = self.config.crosses if self.config is not None else ()
        for cross in crosses:
            values = [sampled_values.get(name) for name in cross.coverpoints]
            if all(value is not None for value in values):
                self._cross(cross.name, *(str(value) for value in values))

    def uncovered(self) -> dict[str, Any]:
        uncovered: dict[str, Any] = {}
        if self.config is None:
            return uncovered

        field_uncovered: dict[str, list[str]] = {}
        for name, expected in self.expected_field_bins().items():
            missing_values = missing(self.bins.get(name, Counter()), expected)
            if missing_values:
                field_uncovered[name] = missing_values
                uncovered[name] = missing_values

        coverpoint_uncovered: dict[str, list[str]] = {}
        for name, expected in self.expected_coverpoint_bins().items():
            missing_values = missing(self.bins.get(name, Counter()), expected)
            if missing_values:
                coverpoint_uncovered[name] = missing_values

        cross_uncovered: dict[str, list[str]] = {}
        for name, expected in self.expected_crosses().items():
            missing_values = missing(self.crosses.get(name, Counter()), expected)
            if missing_values:
                cross_uncovered[name] = missing_values

        if field_uncovered:
            uncovered["fields"] = field_uncovered
        if coverpoint_uncovered:
            uncovered["coverpoints"] = coverpoint_uncovered
        if cross_uncovered:
            uncovered["crosses"] = cross_uncovered
        return uncovered

    def expected_bins(self) -> dict[str, tuple[str, ...]]:
        expected = dict(self.expected_field_bins())
        expected.update(self.expected_coverpoint_bins())
        return expected

    def expected_field_bins(self) -> dict[str, tuple[str, ...]]:
        if self.config is None:
            return {}
        return {
            field.name: tuple(str(choice) for choice in field.choices)
            for field in self.config.fields
            if field.choices
        }

    def expected_coverpoint_bins(self) -> dict[str, tuple[str, ...]]:
        if self.config is None:
            return {}
        expected: dict[str, tuple[str, ...]] = {}
        for coverpoint in self.config.coverpoints:
            values = tuple(str(value) for value in coverpoint.bins or coverpoint.patterns)
            if values:
                expected[coverpoint.name] = values
        return expected

    def expected_crosses(self) -> dict[str, tuple[str, ...]]:
        if self.config is None:
            return {}
        expected_bins = self.expected_bins()
        crosses: dict[str, tuple[str, ...]] = {}
        for cross in self.config.crosses:
            coverpoint_bins = [expected_bins.get(name, ()) for name in cross.coverpoints]
            if not coverpoint_bins or any(not values for values in coverpoint_bins):
                continue
            expected_count = 1
            for values in coverpoint_bins:
                expected_count *= len(values)
            if expected_count > MAX_EXPECTED_CROSS_BINS:
                continue
            crosses[cross.name] = tuple(
                "|".join(values)
                for values in product(*coverpoint_bins)
            )
        return crosses


def coverage_value(field: FieldSpec, value: Any) -> str:
    if field.kind == "hex":
        try:
            raw = bytes.fromhex(str(value))
        except ValueError:
            return "invalid_hex"
        return f"{byte_pattern(raw)}:{length_bucket(len(raw))}"
    return str(value)


def coverpoint_value(coverpoint: CoverpointSpec, field: FieldSpec, value: Any) -> str:
    if field.kind == "hex" and coverpoint.patterns:
        try:
            return byte_pattern(bytes.fromhex(str(value)))
        except ValueError:
            return "invalid_hex"
    return coverage_value(field, value)


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
