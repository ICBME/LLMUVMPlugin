"""Read-only source context serialization for Spec2IR harness observations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from rtlagent_bfm.loader import load_ir

from .manifest import load_manifest_summary


def manifest_context(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    manifest_path = Path(path)
    payload: dict[str, Any] = {
        "path": str(manifest_path),
        "content": manifest_path.read_text(encoding="utf-8"),
    }
    manifest = load_manifest_summary(manifest_path)
    payload["summary"] = {
        "target": manifest.target,
        "fields": [
            {
                "name": field.name,
                "kind": field.kind,
                "choices": list(field.choices),
                "min": field.minimum,
                "max": field.maximum,
                "hex_len": field.hex_len,
                "hex_len_by": field.hex_len_by,
            }
            for field in manifest.fields
        ],
    }
    return payload


def design_ir_context(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    design_path = Path(path)
    design_ir = load_ir(design_path)
    return {
        "path": str(design_path),
        "content": design_path.read_text(encoding="utf-8"),
        "summary": {
            "top": design_ir.top,
            "interfaces": sorted(design_ir.interfaces),
            "bindings": sorted(design_ir.bindings),
            "registers": sorted(design_ir.registers),
        },
    }


def spec_contexts(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    result = []
    for index, raw_path in enumerate(paths, start=1):
        path = Path(raw_path)
        result.append({
            "id": f"src{index}",
            "path": str(path),
            "content": path.read_text(encoding="utf-8", errors="replace"),
        })
    return result


__all__ = ["design_ir_context", "manifest_context", "spec_contexts"]
