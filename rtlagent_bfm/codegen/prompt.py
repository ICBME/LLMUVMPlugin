"""Prompt construction for direct LLM generation of target plugins."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from rtlagent_bfm.loader import load_ir


DEFAULT_PLUGIN_CONTRACTS = Path("docs/reference/plugin_contracts.md")


def build_generation_prompt(
    *,
    manifest_path: str | Path,
    ir_path: str | Path,
    spec_paths: Iterable[str | Path] = (),
    plugin_contracts_path: str | Path = DEFAULT_PLUGIN_CONTRACTS,
) -> dict[str, Any]:
    """Build a strict JSON prompt for first-pass plugin generation."""

    manifest = Path(manifest_path)
    ir_file = Path(ir_path)
    contracts = Path(plugin_contracts_path)

    # Validate the IR early; the raw text is still included for the LLM.
    load_ir(ir_file)

    specs = []
    for spec_path in spec_paths:
        path = Path(spec_path)
        specs.append(
            {
                "path": str(path),
                "content": path.read_text(encoding="utf-8"),
            }
        )

    return {
        "task": (
            "Generate an initial target reference model and scoreboard as Python "
            "plugins. Do not modify framework code. Return strict JSON only."
        ),
        "workflow": "ir_then_direct_plugin_candidate",
        "constraints": [
            "No DSL is available in this phase.",
            "Use the generated IR as binding/context only; protocol behavior comes from the spec.",
            "Reference model must not drive DUT signals or depend on simulation time.",
            "Scoreboard must implement write(record), check(), and summary().",
            "Generated files must be candidates until validation promotes them to final artifacts.",
        ],
        "inputs": {
            "target_manifest": {
                "path": str(manifest),
                "content": manifest.read_text(encoding="utf-8"),
            },
            "bfm_ir": {
                "path": str(ir_file),
                "content": ir_file.read_text(encoding="utf-8"),
            },
            "plugin_contracts": {
                "path": str(contracts),
                "content": contracts.read_text(encoding="utf-8"),
            },
            "specs": specs,
        },
        "expected_response_schema": {
            "files": [
                {
                    "path": "relative/path/to/generated_ref_model.py",
                    "content": "python source text",
                },
                {
                    "path": "relative/path/to/generated_scoreboard.py",
                    "content": "python source text",
                },
            ],
            "assumptions": ["explicit assumptions inferred from the spec"],
            "required_tests": ["golden cases or replay checks needed before promotion"],
            "metadata": {"ref_model": "module:Class", "scoreboard": "module:Class"},
        },
    }


def write_generation_prompt(output_path: str | Path, **kwargs: Any) -> None:
    prompt = build_generation_prompt(**kwargs)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prompt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
