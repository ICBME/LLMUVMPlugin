from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


class PluginContractError(TypeError):
    """Raised when a generated plugin does not satisfy a replay contract."""


@dataclass(frozen=True)
class ExpectedResult:
    """Reference-model output consumed by the replay driver adapter."""

    expected: Any
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComparisonResult:
    """Scoreboard/comparator decision for one replay record."""

    passed: bool
    reason: str = ""
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ReferenceModelPlugin(Protocol):
    def predict(self, case: Any) -> ExpectedResult:
        ...


@runtime_checkable
class ScoreboardPlugin(Protocol):
    def write(self, record: Any) -> None:
        ...

    def check(self) -> None:
        ...

    def summary(self) -> dict[str, Any]:
        ...


@runtime_checkable
class ComparatorPlugin(Protocol):
    def compare(self, actual: Any, expected: Any, record: Any) -> ComparisonResult:
        ...


class DefaultComparator:
    """Default actual/expected equality comparator."""

    def compare(self, actual: Any, expected: Any, record: Any) -> ComparisonResult:
        if actual == expected:
            return ComparisonResult(passed=True)
        return ComparisonResult(
            passed=False,
            reason=f"actual={actual!r} expected={expected!r}",
        )


def normalize_expected(value: Any, *, spec: str = "reference model") -> ExpectedResult:
    """Normalize plugin output into the public ExpectedResult contract."""

    if isinstance(value, ExpectedResult):
        return value
    if isinstance(value, Mapping):
        if "expected" not in value:
            raise PluginContractError(f"{spec}: predict() mapping must contain 'expected'")
        return ExpectedResult(
            expected=value["expected"],
            detail=_optional_string(value.get("detail", "")),
            metadata=_metadata_mapping(value.get("metadata", {}), spec=spec),
        )
    if hasattr(value, "expected"):
        return ExpectedResult(
            expected=getattr(value, "expected"),
            detail=_optional_string(getattr(value, "detail", "")),
            metadata=_metadata_mapping(getattr(value, "metadata", {}), spec=spec),
        )
    return ExpectedResult(expected=value)


def normalize_comparison(
    value: Any,
    *,
    spec: str = "comparator",
) -> ComparisonResult:
    """Normalize comparator output into ComparisonResult."""

    if isinstance(value, ComparisonResult):
        return value
    if isinstance(value, bool):
        return ComparisonResult(passed=value)
    if isinstance(value, Mapping):
        if "passed" not in value:
            raise PluginContractError(f"{spec}: compare() mapping must contain 'passed'")
        passed = value["passed"]
        if not isinstance(passed, bool):
            raise PluginContractError(f"{spec}: compare() 'passed' must be bool")
        return ComparisonResult(
            passed=passed,
            reason=_optional_string(value.get("reason", "")),
            detail=_optional_string(value.get("detail", "")),
            metadata=_metadata_mapping(value.get("metadata", {}), spec=spec),
        )
    if hasattr(value, "passed"):
        passed = getattr(value, "passed")
        if not isinstance(passed, bool):
            raise PluginContractError(f"{spec}: compare() 'passed' must be bool")
        return ComparisonResult(
            passed=passed,
            reason=_optional_string(getattr(value, "reason", "")),
            detail=_optional_string(getattr(value, "detail", "")),
            metadata=_metadata_mapping(getattr(value, "metadata", {}), spec=spec),
        )
    raise PluginContractError(
        f"{spec}: compare() must return ComparisonResult, bool, or mapping"
    )


def validate_ref_model_plugin(
    plugin: Any,
    *,
    spec: str = "reference model",
) -> ReferenceModelPlugin:
    _require_callables(plugin, spec, ("predict",))
    return plugin


def validate_scoreboard_plugin(
    plugin: Any,
    *,
    spec: str = "scoreboard",
) -> ScoreboardPlugin:
    _require_callables(plugin, spec, ("write", "check", "summary"))
    summary = plugin.summary()
    if not isinstance(summary, dict):
        raise PluginContractError(f"{spec}: summary() must return a dict")
    return plugin


def validate_comparator_plugin(
    plugin: Any,
    *,
    spec: str = "comparator",
) -> ComparatorPlugin:
    _require_callables(plugin, spec, ("compare",))
    return plugin


def _require_callables(obj: Any, spec: str, names: tuple[str, ...]) -> None:
    for name in names:
        if not callable(getattr(obj, name, None)):
            raise PluginContractError(f"{spec}: missing callable {name}()")


def _optional_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _metadata_mapping(value: Any, *, spec: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PluginContractError(f"{spec}: metadata must be a mapping")
    return dict(value)


__all__ = [
    "ComparatorPlugin",
    "ComparisonResult",
    "DefaultComparator",
    "ExpectedResult",
    "PluginContractError",
    "ReferenceModelPlugin",
    "ScoreboardPlugin",
    "normalize_comparison",
    "normalize_expected",
    "validate_comparator_plugin",
    "validate_ref_model_plugin",
    "validate_scoreboard_plugin",
]
