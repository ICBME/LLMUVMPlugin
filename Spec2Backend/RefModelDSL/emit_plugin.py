"""Deterministic UVM reference-model wrapper emission for RefModelIR."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from .schema import write_json


def ref_model_plugin_spec(target: str) -> str:
    return f"generated.{_safe_name(target)}_dsl_ref_model:DslRefModel"


def emit_ref_model_plugin(
    target: str,
    output_dir: str | Path,
    *,
    verification_report: Mapping[str, Any] | None = None,
) -> tuple[Path, ...]:
    root = Path(output_dir)
    generated = root / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    init_path = generated / "__init__.py"
    module_path = generated / f"{_safe_name(target)}_dsl_ref_model.py"
    init_path.write_text("", encoding="utf-8")
    module_path.write_text(_plugin_source(target, verification_report or {}), encoding="utf-8")
    return init_path, module_path


def write_ref_model_bundle(
    path: str | Path,
    *,
    target: str,
    metadata: Mapping[str, Any] | None = None,
    assumptions: tuple[str, ...] = (),
) -> Path:
    payload = {
        "files": [
            {"path": "ref_model_ir.json", "content": "<see artifact>"},
        ],
        "metadata": {
            **dict(metadata or {}),
            "ref_model": ref_model_plugin_spec(target),
        },
        "assumptions": list(assumptions),
    }
    return write_json(path, payload)


def _plugin_source(target: str, verification_report: Mapping[str, Any]) -> str:
    level = str(verification_report.get("verification_level") or "")
    return "\n".join(
        [
            "from pathlib import Path",
            "",
            "from fuzz_uvm.contracts import ExpectedResult",
            "from Spec2Backend.RefModelDSL.interpreter import RefModelInterpreter",
            "",
            "",
            "class DslRefModel:",
            "    def __init__(self, target=None, config=None):",
            f"        self.target = target or {target!r}",
            "        self.config = config",
            "        root = Path(__file__).resolve().parents[1]",
            "        self._interpreter = RefModelInterpreter.from_path(root / 'ref_model_ir.json')",
            "        self._state = self._interpreter.initial_state()",
            "",
            "    def predict(self, case):",
            "        outputs, self._state = self._interpreter.step(case.data, state=self._state)",
            "        expected = outputs.get('expected') if isinstance(outputs, dict) else outputs",
            "        if expected is None and isinstance(outputs, dict) and len(outputs) == 1:",
            "            expected = next(iter(outputs.values()))",
            "        return ExpectedResult(",
            "            expected=expected,",
            f"            detail='RefModelIR {level}',",
            f"            metadata={{'target': self.target, 'verification_level': {level!r}}},",
            "        )",
            "",
        ]
    )


def _safe_name(value: str) -> str:
    result = re.sub(r"[^0-9A-Za-z_]+", "_", str(value)).strip("_").lower()
    if not result:
        result = "ref_model"
    if result[0].isdigit():
        result = f"t_{result}"
    return result


__all__ = ["emit_ref_model_plugin", "ref_model_plugin_spec", "write_ref_model_bundle"]
