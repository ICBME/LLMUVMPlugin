from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping


PLUGIN_CONTRACT_SCHEMA_VERSION = 1
PLUGIN_MANIFEST_FIELDS = (
    "ref_model",
    "comparator",
    "scoreboard",
    "coverage_model",
)
PLUGIN_VALIDATION_ORDER = (
    "ref_model",
    "comparator",
    "scoreboard",
    "coverage_model",
)

GENERATED_CONTRACT_DOC_START = "<!-- GENERATED_PLUGIN_CONTRACTS_START -->"
GENERATED_CONTRACT_DOC_END = "<!-- GENERATED_PLUGIN_CONTRACTS_END -->"


def build_plugin_contract_spec() -> dict[str, Any]:
    """Build the machine-readable contract spec consumed by LLM generation."""

    spec = _contract_spec_payload()
    spec["contract_hash"] = plugin_contract_hash(spec)
    return spec


def current_plugin_contract_hash() -> str:
    return plugin_contract_hash(_contract_spec_payload())


def plugin_contract_hash(spec: Mapping[str, Any] | None = None) -> str:
    payload = _contract_spec_payload() if spec is None else dict(spec)
    payload.pop("contract_hash", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_llm_plugin_contract_prompt(spec: Mapping[str, Any] | None = None) -> str:
    contract = deepcopy(dict(spec or build_plugin_contract_spec()))
    lines = [
        "Generated replay plugin contract",
        f"schema_version: {contract['schema_version']}",
        f"contract_hash: {contract['contract_hash']}",
        "",
        "You must return a GeneratedPluginBundle JSON object with:",
        "- target: target name",
        "- plugins: mapping from role to module:Object plugin spec",
        "- metadata.contract_hash: exactly the contract_hash above",
        "",
        "Allowed plugin roles and required interfaces:",
    ]
    for role in PLUGIN_VALIDATION_ORDER:
        role_spec = contract["roles"][role]
        lines.append(f"- {role}: {role_spec['protocol']}")
        lines.append(f"  constructor: {contract['constructor']}")
        for method in role_spec["methods"]:
            lines.append(f"  method: {method['signature']}")
            if method.get("return"):
                lines.append(f"  return: {method['return']}")
        lines.append(f"  notes: {role_spec['notes']}")
    lines.extend(
        [
            "",
            "Hard constraints:",
            *[f"- {item}" for item in contract["hard_constraints"]],
            "",
            "Forbidden imports/calls:",
            *[f"- {item}" for item in contract["forbidden"]],
            "",
            "Generated artifact flow:",
            " -> ".join(contract["pipeline"]),
            "",
            "Persist these JSON artifacts when crossing process or campaign boundaries:",
            *[f"- {item}" for item in contract["artifacts"]],
        ]
    )
    return "\n".join(lines) + "\n"


def render_plugin_contract_docs_fragment(spec: Mapping[str, Any] | None = None) -> str:
    contract = deepcopy(dict(spec or build_plugin_contract_spec()))
    lines = [
        f"`schema_version`: `{contract['schema_version']}`",
        f"`contract_hash`: `{contract['contract_hash']}`",
        "",
        "LLM 生成环节必须使用这个 contract hash，并在 `GeneratedPluginBundle.metadata.contract_hash` 中原样返回。",
        "",
        "| Role | Protocol | Required Methods | Manifest Field |",
        "| --- | --- | --- | --- |",
    ]
    for role in PLUGIN_VALIDATION_ORDER:
        role_spec = contract["roles"][role]
        methods = "<br>".join(method["signature"] for method in role_spec["methods"])
        lines.append(
            f"| `{role}` | `{role_spec['protocol']}` | {methods} | `{role_spec['manifest_field']}` |"
        )
    lines.extend(
        [
            "",
            "Hard constraints:",
            "",
            *[f"- {item}" for item in contract["hard_constraints"]],
            "",
            "Generated artifact flow:",
            "",
            "```text",
            " -> ".join(contract["pipeline"]),
            "```",
            "",
            "JSON artifact:",
            "",
            *[f"- `{item}`" for item in contract["artifacts"]],
        ]
    )
    return "\n".join(lines) + "\n"


def generated_contract_doc_block(spec: Mapping[str, Any] | None = None) -> str:
    return (
        f"{GENERATED_CONTRACT_DOC_START}\n"
        f"{render_plugin_contract_docs_fragment(spec)}"
        f"{GENERATED_CONTRACT_DOC_END}"
    )


def _contract_spec_payload() -> dict[str, Any]:
    return {
        "schema_version": PLUGIN_CONTRACT_SCHEMA_VERSION,
        "constructor": "def __init__(self, target=None, config=None)",
        "roles": {
            "ref_model": {
                "protocol": "ReferenceModelPlugin",
                "manifest_field": "ref_model",
                "methods": (
                    {
                        "name": "predict",
                        "signature": "def predict(self, case) -> ExpectedResult",
                        "return": "ExpectedResult or mapping/object with expected, detail, metadata",
                    },
                ),
                "notes": "Pure prediction from replay case; no DUT, cocotb, time, file, process, or network access.",
            },
            "comparator": {
                "protocol": "ComparatorPlugin",
                "manifest_field": "comparator",
                "methods": (
                    {
                        "name": "compare",
                        "signature": "def compare(self, actual, expected, record) -> ComparisonResult",
                        "return": "ComparisonResult, bool, or mapping/object with passed, reason, detail, metadata",
                    },
                ),
                "notes": "Pure actual/expected comparison policy consumed by ResultScoreboard.",
            },
            "scoreboard": {
                "protocol": "ScoreboardPlugin",
                "manifest_field": "scoreboard",
                "methods": (
                    {
                        "name": "write",
                        "signature": "def write(self, record) -> None",
                    },
                    {
                        "name": "check",
                        "signature": "def check(self) -> None",
                    },
                    {
                        "name": "summary",
                        "signature": "def summary(self) -> dict[str, Any]",
                        "return": "JSON-serializable dict",
                    },
                ),
                "notes": "Use full scoreboard only for multi-transaction, out-of-order, or stateful checks.",
            },
            "coverage_model": {
                "protocol": "FunctionalCoveragePlugin",
                "manifest_field": "coverage_model",
                "methods": (
                    {
                        "name": "sample",
                        "signature": "def sample(self, case) -> None",
                    },
                    {
                        "name": "sample_record",
                        "signature": "def sample_record(self, record) -> None",
                    },
                    {
                        "name": "to_json",
                        "signature": "def to_json(self) -> dict[str, Any]",
                        "return": "JSON-serializable dict",
                    },
                ),
                "notes": "Functional coverage only; no DUT driving or simulator timing.",
            },
        },
        "bundle_schema": {
            "type": "object",
            "required": ("target", "plugins", "metadata"),
            "metadata_required": ("contract_hash",),
            "allowed_plugin_roles": PLUGIN_MANIFEST_FIELDS,
            "plugin_spec_format": "module:Object",
        },
        "hard_constraints": (
            "Generate pure Python plugins only; do not generate UVM components.",
            "Use constructor def __init__(self, target=None, config=None).",
            "Return only JSON-serializable metadata, summary(), and to_json() payloads.",
            "Prefer ref_model + comparator + ResultScoreboard before generating a full custom scoreboard.",
            "GeneratedPluginBundle.metadata.contract_hash must equal the current contract_hash.",
        ),
        "forbidden": (
            "cocotb",
            "uvm_component",
            "Timer",
            "RisingEdge",
            "time.sleep",
            "network access",
            "subprocess/process access",
            "file system access",
            "business logic at module import time",
        ),
        "pipeline": (
            "generation_context",
            "generated_artifact_bundle",
            "plugin_contract_validator",
            "plugin_registry",
            "target_manifest_overlay",
            "target_manifest",
        ),
        "artifacts": (
            "generated_artifact_bundle.json",
            "plugin_validation_report.json",
            "plugin_registry.json",
            "target_manifest_overlay.json",
        ),
    }


__all__ = [
    "GENERATED_CONTRACT_DOC_END",
    "GENERATED_CONTRACT_DOC_START",
    "PLUGIN_CONTRACT_SCHEMA_VERSION",
    "PLUGIN_MANIFEST_FIELDS",
    "PLUGIN_VALIDATION_ORDER",
    "build_plugin_contract_spec",
    "current_plugin_contract_hash",
    "generated_contract_doc_block",
    "plugin_contract_hash",
    "render_llm_plugin_contract_prompt",
    "render_plugin_contract_docs_fragment",
]
