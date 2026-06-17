"""Generate plugin candidate bundles from validated OracleIR documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifacts import ArtifactBundle, GeneratedFile
from .oracle_ir import load_oracle_ir, validate_oracle_ir


DEFAULT_REF_MODEL_CLASS = "GeneratedOracleRefModel"


def build_oracle_plugin_bundle(
    oracle_ir: dict[str, Any],
    *,
    target: str | None = None,
    package: str = "generated",
    module_name: str | None = None,
    class_name: str = DEFAULT_REF_MODEL_CLASS,
) -> ArtifactBundle:
    """Build an artifact bundle containing a generated ref-model plugin."""

    target_name = target or str(oracle_ir.get("target") or "dut")
    validate_oracle_ir(oracle_ir, target=target_name, require_rules=True)
    package = safe_module_part(package)
    module_name = safe_module_part(module_name or f"{safe_name(target_name)}_ref_model")
    class_name = safe_class_name(class_name)
    module_spec = f"{package}.{module_name}:{class_name}"
    return ArtifactBundle(
        files=(
            GeneratedFile(path=f"{package}/__init__.py", content=""),
            GeneratedFile(
                path=f"{package}/{module_name}.py",
                content=render_ref_model_source(
                    oracle_ir,
                    class_name=class_name,
                    target=target_name,
                ),
            ),
        ),
        assumptions=tuple(str(item) for item in oracle_ir.get("assumptions", ())),
        required_tests=("Run golden cases before promotion.",),
        metadata={
            "target": target_name,
            "ref_model": module_spec,
            "scoreboard": "fuzz_uvm.scoreboards:ResultScoreboard",
            "oracle_ir_schema_version": oracle_ir.get("schema_version"),
        },
    )


def build_oracle_plugin_bundle_from_file(
    oracle_ir_path: str | Path,
    **kwargs: Any,
) -> ArtifactBundle:
    return build_oracle_plugin_bundle(load_oracle_ir(oracle_ir_path), **kwargs)


def write_oracle_plugin_bundle(path: str | Path, bundle: ArtifactBundle) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(bundle_to_dict(bundle), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def bundle_to_dict(bundle: ArtifactBundle) -> dict[str, Any]:
    return {
        "files": [
            {"path": generated_file.path, "content": generated_file.content}
            for generated_file in bundle.files
        ],
        "assumptions": list(bundle.assumptions),
        "required_tests": list(bundle.required_tests),
        "metadata": dict(bundle.metadata),
    }


def render_ref_model_source(
    oracle_ir: dict[str, Any],
    *,
    class_name: str,
    target: str,
) -> str:
    oracle_ir_json = json.dumps(oracle_ir, indent=2, sort_keys=True)
    return (
        "from __future__ import annotations\n"
        "\n"
        "from typing import Any\n"
        "\n"
        "from fuzz_uvm.contracts import ExpectedResult\n"
        "from rtlagent_bfm.codegen.oracle_eval import evaluate_oracle_ir\n"
        "\n"
        "\n"
        f"ORACLE_IR = {oracle_ir_json}\n"
        "\n"
        "\n"
        f"class {class_name}:\n"
        "    def __init__(self, target=None, config=None):\n"
        f"        self.target = target or {target!r}\n"
        "        self.config = config\n"
        "\n"
        "    def predict(self, case: Any) -> ExpectedResult:\n"
        "        expected = evaluate_oracle_ir(ORACLE_IR, case.data)\n"
        "        return ExpectedResult(expected=expected, detail=\"oracle_ir_ref_model\")\n"
    )


def safe_name(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "_" for char in str(value)]
    name = "".join(chars).strip("_")
    return name or "generated"


def safe_module_part(value: str) -> str:
    name = safe_name(value)
    if name[0].isdigit():
        name = f"_{name}"
    return name


def safe_class_name(value: str) -> str:
    text = str(value)
    if text.isidentifier() and text[:1].isalpha():
        return text
    raw = "".join(char if char.isalnum() else "_" for char in text)
    parts = [part for part in raw.split("_") if part]
    name = "".join(part[:1].upper() + part[1:] for part in parts) or DEFAULT_REF_MODEL_CLASS
    if name[0].isdigit():
        name = f"Generated{name}"
    return name
