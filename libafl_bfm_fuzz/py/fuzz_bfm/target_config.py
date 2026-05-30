from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str = "any"
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[Any, ...] = ()
    hex_len: int | None = None
    hex_len_by: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetConfig:
    name: str
    driver: str
    path: Path
    toplevel: str | None = None
    bfm_ir: Path | None = None
    coverage_hints: str | None = None
    oracle: str | None = None
    monitor: str | None = None
    coverage_model: str | None = None
    sequence_schema: str | None = None
    fields: tuple[FieldSpec, ...] = ()


def default_targets_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "targets"


def load_target_config(target: str, targets_dir: Path | None = None) -> TargetConfig:
    path = _resolve_target_path(target, targets_dir)
    data = _load_toml(path)
    name = str(data.get("name", target))
    driver = data.get("driver")
    if not isinstance(driver, str) or not driver:
        raise ValueError(f"{path}: target config must define driver = 'module:Class'")
    bfm_ir = data.get("bfm_ir")
    return TargetConfig(
        name=name,
        driver=driver,
        path=path,
        toplevel=str(data["toplevel"]) if "toplevel" in data else None,
        bfm_ir=(path.parent / str(bfm_ir)).resolve() if bfm_ir else None,
        coverage_hints=str(data["coverage_hints"]) if "coverage_hints" in data else None,
        oracle=str(data["oracle"]) if "oracle" in data else None,
        monitor=str(data["monitor"]) if "monitor" in data else None,
        coverage_model=str(data["coverage_model"]) if "coverage_model" in data else None,
        sequence_schema=str(data["sequence_schema"]) if "sequence_schema" in data else None,
        fields=tuple(_field_from_dict(item) for item in data.get("field", ())),
    )


def _resolve_target_path(target: str, targets_dir: Path | None) -> Path:
    explicit = os.getenv("FUZZ_TARGET_CONFIG")
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise FileNotFoundError(f"FUZZ_TARGET_CONFIG does not exist: {path}")
        return path
    root = Path(os.getenv("FUZZ_TARGETS_DIR", targets_dir or default_targets_dir()))
    path = root / f"{target}.toml"
    if not path.exists():
        raise FileNotFoundError(f"target config not found for {target!r}: {path}")
    return path


def _load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        raise RuntimeError("tomllib/tomli is required to load target configs")
    with path.open("rb") as file:
        return tomllib.load(file)


def _field_from_dict(data: dict[str, Any]) -> FieldSpec:
    raw_choices = data.get("choices")
    if raw_choices is None:
        choices = ()
    elif isinstance(raw_choices, list):
        choices = tuple(raw_choices)
    else:
        choices = (raw_choices,)
    hex_len_by = data.get("hex_len_by", {})
    return FieldSpec(
        name=str(data["name"]),
        kind=str(data.get("kind", "any")),
        minimum=_optional_int(data.get("min")),
        maximum=_optional_int(data.get("max")),
        choices=choices,
        hex_len=_optional_int(data.get("hex_len")),
        hex_len_by={
            str(selector): {str(key): int(value) for key, value in choices.items()}
            for selector, choices in hex_len_by.items()
        },
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
