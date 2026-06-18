"""Target manifest helpers used by Spec2Backend stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ManifestFieldSummary:
    name: str
    kind: str = "any"
    choices: tuple[Any, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    hex_len: int | None = None
    hex_len_by: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class ManifestSummary:
    path: Path
    target: str
    fields: tuple[ManifestFieldSummary, ...] = ()


def load_manifest_summary(path: str | Path) -> ManifestSummary:
    manifest_path = Path(path)
    if tomllib is None:
        raise RuntimeError("tomllib/tomli is required to load target manifests")
    with manifest_path.open("rb") as file:
        data = tomllib.load(file)
    target = str(data.get("name") or manifest_path.stem)
    fields = data.get("field", ())
    if fields is None:
        fields = ()
    if not isinstance(fields, list | tuple):
        raise ValueError(f"{manifest_path}: field entries must be a list")
    return ManifestSummary(
        path=manifest_path,
        target=target,
        fields=tuple(_field_from_mapping(item) for item in fields if isinstance(item, dict)),
    )


def input_from_manifest_field(field: ManifestFieldSummary) -> dict[str, Any]:
    value: dict[str, Any] = {
        "name": field.name,
        "kind": field.kind,
    }
    if field.choices:
        value["choices"] = list(field.choices)
    if field.minimum is not None:
        value["min"] = field.minimum
    if field.maximum is not None:
        value["max"] = field.maximum
    if field.hex_len is not None:
        value["hex_len"] = field.hex_len
    if field.hex_len_by:
        value["hex_len_by"] = field.hex_len_by
    return value


def _field_from_mapping(data: dict[str, Any]) -> ManifestFieldSummary:
    choices = data.get("choices", ())
    if choices is None:
        choices_tuple: tuple[Any, ...] = ()
    elif isinstance(choices, list | tuple):
        choices_tuple = tuple(choices)
    else:
        choices_tuple = (choices,)
    hex_len_by = data.get("hex_len_by", {})
    if not isinstance(hex_len_by, dict):
        hex_len_by = {}
    return ManifestFieldSummary(
        name=str(data["name"]),
        kind=str(data.get("kind", "any")),
        choices=choices_tuple,
        minimum=_optional_int(data.get("min")),
        maximum=_optional_int(data.get("max")),
        hex_len=_optional_int(data.get("hex_len")),
        hex_len_by={
            str(selector): {
                str(choice): int(length)
                for choice, length in selector_map.items()
            }
            for selector, selector_map in hex_len_by.items()
            if isinstance(selector_map, dict)
        },
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


__all__ = [
    "ManifestFieldSummary",
    "ManifestSummary",
    "input_from_manifest_field",
    "load_manifest_summary",
]
