"""IR loading helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rtlagent_bfm.ir import DesignIR


def load_ir(path: str | Path) -> DesignIR:
    """Load a DesignIR from JSON or TOML.

    YAML is intentionally not required so this package can stay dependency-free.
    """

    ir_path = Path(path)
    suffix = ir_path.suffix.lower()
    if suffix == ".json":
        data: dict[str, Any] = json.loads(ir_path.read_text(encoding="utf-8"))
    elif suffix == ".toml":
        try:
            import tomllib
        except ModuleNotFoundError:
            try:
                import tomli as tomllib
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "loading TOML IR files on Python < 3.11 requires tomli"
                ) from exc

        data = tomllib.loads(ir_path.read_text(encoding="utf-8"))
    else:
        raise ValueError(f"unsupported IR format {suffix!r}; use .json or .toml")
    return DesignIR.from_dict(data)
