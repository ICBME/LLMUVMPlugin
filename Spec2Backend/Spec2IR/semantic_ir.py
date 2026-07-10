"""Traceable semantic IR extraction from natural-language specs.

SemanticSpecIR is the reviewable layer between raw target documentation and
downstream artifacts such as reference models or SVA.  It deliberately keeps
source claims, evidence, confidence, open questions, and review state alongside
formalized behavior so humans can audit and complete the spec semantics before
any backend-specific lowering decision is made.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .claim_extraction import (
    build_open_questions,
    evidence_from_spec_claims,
    extract_spec_claims,
    gap_requires_human_input,
    semantic_elements_from_claims,
    semantic_gaps_for_uncovered_claims,
)
from .manifest import ManifestSummary, input_from_manifest_field, load_manifest_summary
from .representation_ast import REPRESENTATION_AST_VERSION, representation_ast_contract
from .representation_ast import NODE_ALLOWED_KEYS
from .schema import (
    ALLOWED_CLAIM_OBLIGATION_KINDS,
    ALLOWED_CLAIM_KINDS,
    ALLOWED_CLAIM_STRENGTHS,
    ALLOWED_FORMALIZATION_STATUSES,
    ALLOWED_REVIEW_STATUSES,
    ALLOWED_SEMANTIC_ELEMENT_KINDS,
    ALLOWED_SEMANTIC_GAP_KINDS,
    BLOCKING_FORMALIZATION_STATUSES,
    SEMANTIC_SPEC_IR_SCHEMA_VERSION,
    TRACEABLE_SOURCE_KIND,
    SemanticSpecIRIssue,
    SemanticSpecIRValidationError,
    SourceDocument,
    json_round_trip,
    load_source_documents,
    looks_like_semantic_spec_ir,
)
from .semantic_validation import (
    collect_semantic_spec_ir_issues,
    validate_semantic_spec_ir,
)


def generate_semantic_spec_ir(
    *,
    manifest_path: str | Path,
    spec_paths: Iterable[str | Path] = (),
    design_ir_path: str | Path | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Generate and validate a deterministic SemanticSpecIR draft."""

    manifest = load_manifest_summary(manifest_path)
    target_name = target or manifest.target
    if manifest.target != target_name:
        raise SemanticSpecIRValidationError(
            f"manifest target {manifest.target!r} does not match requested target {target_name!r}"
        )
    documents = load_source_documents(spec_paths)
    ir = generate_rule_based_semantic_spec_ir(
        manifest=manifest,
        documents=documents,
        design_ir_path=design_ir_path,
        target=target_name,
    )
    validate_semantic_spec_ir(
        ir,
        manifest_path=manifest.path,
        spec_paths=tuple(document.path for document in documents),
        target=target_name,
    )
    return ir


def generate_rule_based_semantic_spec_ir(
    *,
    manifest: ManifestSummary,
    documents: tuple[SourceDocument, ...],
    design_ir_path: str | Path | None,
    target: str,
) -> dict[str, Any]:
    """Create a conservative review draft without requiring an LLM."""

    inputs = [input_from_manifest_field(field) for field in manifest.fields]
    spec_claims = extract_spec_claims(documents)
    evidence = evidence_from_spec_claims(spec_claims)
    semantic_elements = semantic_elements_from_claims(
        spec_claims,
        evidence,
        manifest_fields={field.name for field in manifest.fields},
    )
    open_questions = build_open_questions(
        manifest,
        semantic_elements,
        evidence,
        spec_claims,
    )
    semantic_gaps = semantic_gaps_for_uncovered_claims(
        spec_claims,
        semantic_elements,
        open_questions,
    )
    if not spec_claims:
        semantic_gaps.append(
            {
                "id": "gap1",
                "kind": "missing_context",
                "reason": "No normative or behavior-relevant source claims were extracted from the spec text.",
                "resolution": "Provide a natural-language hardware behavior spec or add human-authored claims.",
                "claim_ids": [],
            }
        )

    has_blocking_semantic_element = any(
        isinstance(item, dict)
        and item.get("formalization_status") in BLOCKING_FORMALIZATION_STATUSES
        for item in semantic_elements
    )
    review_status = "needs_human_input" if (
        any(bool(question.get("blocking")) for question in open_questions)
        or any(gap_requires_human_input(gap) for gap in semantic_gaps)
        or has_blocking_semantic_element
    ) else "draft"
    semantic_context = semantic_context_from_manifest(manifest)
    augment_semantic_context_from_elements(semantic_context, semantic_elements)
    return {
        "schema_version": SEMANTIC_SPEC_IR_SCHEMA_VERSION,
        "target": target,
        "sources": [document.payload() for document in documents],
        "spec_claims": spec_claims,
        "inputs": inputs,
        "semantic_context": semantic_context,
        "semantic_elements": semantic_elements,
        "evidence": evidence,
        "open_questions": open_questions,
        "assumptions": [],
        "semantic_gaps": semantic_gaps,
        "review": {
            "status": review_status,
            "human_answers": [],
            "reviewed_items": [],
        },
        "metadata": {
            "source": "rule_based_semantic_spec_ir_generator",
            "manifest": str(manifest.path),
            "design_ir": str(design_ir_path) if design_ir_path is not None else None,
        },
    }


def load_semantic_spec_ir(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_semantic_spec_ir(path: str | Path, ir: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(ir, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_semantic_spec_ir(ir: dict[str, Any]) -> dict[str, Any]:
    """Normalize one SemanticSpecIR artifact after a patch is applied."""

    if not isinstance(ir, dict) or not looks_like_semantic_spec_ir(ir):
        raise ValueError("value is not a SemanticSpecIR object")
    normalized = json_round_trip(ir)
    normalize_semantic_spec_ir_representations(normalized)
    return normalized


def normalize_semantic_spec_ir_representations(ir: dict[str, Any]) -> None:
    for element in ir.get("semantic_elements", []):
        if not isinstance(element, dict):
            continue
        clean_string_list(element, "subjects")
        representation = element.get("representation")
        if not isinstance(representation, dict):
            continue
        clean_string_list(representation, "subjects")
        ast = representation.get("ast")
        if isinstance(ast, dict):
            normalize_representation_ast_node(ast, fallback_text=representation.get("text"))


def normalize_representation_ast_node(node: dict[str, Any], *, fallback_text: Any = None) -> None:
    node_name = node.get("node")
    text_candidate = first_non_empty_string(
        node.get("text"),
        node.get("statement"),
        fallback_text,
    )
    if isinstance(node_name, str) and node_name in NODE_ALLOWED_KEYS:
        for key in list(node):
            if key not in NODE_ALLOWED_KEYS[node_name]:
                node.pop(key, None)
    if node_name == "semantic_claim" and not str(node.get("text") or "").strip():
        node["text"] = text_candidate or "Unformalized semantic claim."
    if node_name == "compare":
        node["op"] = {
            "==": "eq",
            "!=": "ne",
            "<": "lt",
            "<=": "le",
            ">": "gt",
            ">=": "ge",
        }.get(node.get("op"), node.get("op"))
    clean_string_list(node, "subjects")
    clean_string_list(node, "participants")
    for value in list(node.values()):
        if isinstance(value, dict):
            normalize_representation_ast_node(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    normalize_representation_ast_node(item)


def clean_string_list(container: dict[str, Any], key: str) -> None:
    value = container.get(key)
    if isinstance(value, list):
        container[key] = [item for item in value if isinstance(item, str) and item.strip()]


def first_non_empty_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def semantic_spec_ir_contract() -> dict[str, Any]:
    return {
        "schema_version": SEMANTIC_SPEC_IR_SCHEMA_VERSION,
        "required_top_level_keys": [
            "schema_version",
            "target",
            "sources",
            "spec_claims",
            "inputs",
            "semantic_context",
            "semantic_elements",
            "evidence",
            "open_questions",
            "semantic_gaps",
            "review",
        ],
        "source_schema": {
            "id": "stable source id such as src1",
            "path": "source file path",
            "kind": TRACEABLE_SOURCE_KIND,
            "content_hash": "sha256 hex of source content",
            "line_count": "number of lines",
        },
        "spec_claim_schema": {
            "id": "stable claim id such as claim1",
            "source_id": "source document id",
            "line_start": "1-based start line",
            "line_end": "1-based end line",
            "quote": "short exact quote copied from the source line range",
            "summary": "atomic normalized claim summary",
            "kind": sorted(ALLOWED_CLAIM_KINDS),
            "strength": sorted(ALLOWED_CLAIM_STRENGTHS),
            "normative": "true for claims that must be covered before SemanticSpecIR review can pass",
            "subjects": ["signals, fields, states, protocol entities, or outputs"],
            "fingerprint": "sha256 over normalized source identity, location, summary, and quote",
            "decomposition": {
                "version": 1,
                "atomic_obligations": [
                    {
                        "id": "stable obligation id such as claim1.obl1",
                        "kind": sorted(ALLOWED_CLAIM_OBLIGATION_KINDS),
                        "text": "atomic obligation text",
                        "subjects": ["signals, states, protocol entities, or fields"],
                        "required": "true when this obligation must be represented before the claim is complete",
                        "attributes": "optional structured details such as direction, edge, delay_cycles, or operation",
                    }
                ],
            },
        },
        "semantic_element_schema": {
            "id": "stable semantic element id such as sem1",
            "kind": sorted(ALLOWED_SEMANTIC_ELEMENT_KINDS),
            "summary": "human-readable formalized semantic statement",
            "formalization_status": sorted(ALLOWED_FORMALIZATION_STATUSES),
            "confidence": "0.0 to 1.0",
            "subjects": ["manifest fields, outputs, registers, or protocol entities"],
            "representation": {
                "ast_version": REPRESENTATION_AST_VERSION,
                "kind": "strict RepresentationAST kind",
                "text": "human-readable review aid; not the semantic source of truth",
                "ast": "strict RepresentationAST node object",
                "placeholder_rule": "semantic_claim, text_expr-only temporal/protocol/constraint roots, latency endpoints with text_expr, missing temporal/protocol clock context, or placeholder targets/conditions require a blocking formalization_status",
            },
            "evidence": ["ev1"],
            "claim_ids": ["claim1"],
        },
        "semantic_context_schema": {
            "version": 1,
            "symbols": [
                {
                    "name": "manifest field, signal, state, or protocol entity name",
                    "kind": "field | signal | state | clock | reset | protocol_entity",
                    "type": {
                        "kind": "bool | int | uint | bitvector | enum | string | bytes | any",
                        "width": "required positive integer for bitvector when known",
                        "choices": "required list for enum when known",
                        "format": "optional string format such as hex",
                    },
                    "direction": "input | output | internal | inout | unknown",
                    "roles": ["input, output, clock, reset, state, valid, ready, payload, control, or unknown"],
                    "source": "manifest, design_ir, spec, or human_review",
                }
            ],
            "constraints": [
                {
                    "id": "stable constraint id",
                    "kind": "domain | invariant | assumption",
                    "expr": "RepresentationAST predicate node",
                }
            ],
        },
        "representation_ast_schema": representation_ast_contract(),
        "semantic_gap_schema": {
            "id": "stable gap id such as gap1",
            "kind": sorted(ALLOWED_SEMANTIC_GAP_KINDS),
            "reason": "why the source claim is not fully formalized",
            "resolution": "human or LLM action needed to complete the semantics",
            "claim_ids": ["claim ids covered by this gap"],
        },
        "allowed_review_statuses": sorted(ALLOWED_REVIEW_STATUSES),
    }


def semantic_context_from_manifest(manifest: ManifestSummary) -> dict[str, Any]:
    return {
        "version": 1,
        "symbols": [
            {
                "name": field.name,
                "kind": "field",
                "type": semantic_type_from_manifest_field(field),
                "direction": "input",
                "roles": ["input"],
                "source": "manifest",
            }
            for field in manifest.fields
        ],
        "constraints": [],
    }


def semantic_type_from_manifest_field(field: Any) -> dict[str, Any]:
    kind = str(getattr(field, "kind", "any") or "any")
    if kind in {"bool", "boolean"}:
        return {"kind": "bool"}
    if kind == "enum":
        result: dict[str, Any] = {"kind": "enum"}
        choices = list(getattr(field, "choices", ()) or ())
        if choices:
            result["choices"] = choices
        return result
    if kind in {"int", "integer"}:
        return {"kind": "int"}
    if kind in {"uint", "unsigned"}:
        return {"kind": "uint"}
    if kind in {"bitvector", "bits"}:
        result = {"kind": "bitvector"}
        width = getattr(field, "width", None)
        if isinstance(width, int) and width > 0:
            result["width"] = width
        return result
    if kind == "hex":
        result = {"kind": "string", "format": "hex"}
        hex_len = getattr(field, "hex_len", None)
        if isinstance(hex_len, int) and hex_len > 0:
            result["length"] = hex_len
        return result
    if kind in {"string", "bytes"}:
        return {"kind": kind}
    return {"kind": "any"}


def augment_semantic_context_from_elements(
    semantic_context: dict[str, Any],
    semantic_elements: list[dict[str, Any]],
) -> None:
    symbols = semantic_context.setdefault("symbols", [])
    if not isinstance(symbols, list):
        return
    by_name = {
        str(item.get("name")): item
        for item in symbols
        if isinstance(item, dict) and item.get("name")
    }
    for element in semantic_elements:
        if not isinstance(element, dict):
            continue
        representation = element.get("representation")
        if not isinstance(representation, dict):
            continue
        ast = representation.get("ast")
        if isinstance(ast, dict):
            for name, hint in collect_ast_symbol_hints(ast).items():
                if name in by_name:
                    merge_symbol_hint(by_name[name], hint)
                    continue
                symbol = {
                    "name": name,
                    "kind": hint.get("kind", "signal"),
                    "type": hint.get("type", {"kind": "any"}),
                    "direction": hint.get("direction", "internal"),
                    "roles": sorted(hint.get("roles", {"unknown"})),
                    "source": "spec",
                }
                symbols.append(symbol)
                by_name[name] = symbol


def collect_ast_symbol_hints(ast: Any) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}

    def visit(node: Any, *, role: str | None = None, target_type: dict[str, Any] | None = None) -> None:
        if not isinstance(node, dict):
            return
        node_kind = str(node.get("node") or "")
        if node_kind == "signal_ref":
            name = str(node.get("name") or "")
            if name:
                add_hint(
                    name,
                    {
                        "kind": "signal",
                        "type": target_type or ({"kind": "bool"} if role in {"clock", "reset", "valid", "ready", "event"} else {"kind": "any"}),
                        "roles": {role or "unknown"},
                    },
                )
            return
        if node_kind == "state_ref":
            name = str(node.get("name") or "")
            if name:
                add_hint(name, {"kind": "state", "type": target_type or {"kind": "any"}, "roles": {"state"}})
            return
        if node_kind == "field_ref":
            return
        if node_kind in {"assignment", "constant_relation"}:
            visit(node.get("target"), target_type=literal_type_hint(node.get("value")))
            visit(node.get("value"))
            return
        if node_kind == "conditional_assignment":
            visit(node.get("condition"), role="condition")
            visit(node.get("target"), target_type=literal_type_hint(node.get("value")))
            visit(node.get("value"))
            return
        if node_kind == "clock_event":
            visit(node.get("signal"), role="clock", target_type={"kind": "bool"})
            return
        if node_kind in {"rose", "fell", "stable", "past"}:
            visit(node.get("signal"), role="event", target_type={"kind": "bool"})
            return
        if node_kind == "handshake_rule":
            visit(node.get("valid"), role="valid", target_type={"kind": "bool"})
            visit(node.get("ready"), role="ready", target_type={"kind": "bool"})
            for payload in node.get("payload", []) if isinstance(node.get("payload"), list) else []:
                visit(payload, role="payload")
            return
        if node_kind == "signal_binding":
            binding_role = str(node.get("role") or role or "unknown")
            visit(node.get("signal"), role=binding_role)
            return
        for value in node.values():
            if isinstance(value, dict):
                visit(value, role=role)
            elif isinstance(value, list):
                for item in value:
                    visit(item, role=role)

    def add_hint(name: str, hint: dict[str, Any]) -> None:
        current = hints.setdefault(
            name,
            {
                "kind": hint.get("kind", "signal"),
                "type": hint.get("type", {"kind": "any"}),
                "roles": set(),
                "direction": "internal",
            },
        )
        if current.get("type", {}).get("kind") == "any" and hint.get("type", {}).get("kind") != "any":
            current["type"] = hint["type"]
        current.setdefault("roles", set()).update(hint.get("roles", set()))

    visit(ast)
    return hints


def literal_type_hint(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("node") != "literal":
        return None
    literal = value.get("value")
    if isinstance(literal, bool):
        return {"kind": "bool"}
    if isinstance(literal, int):
        width = value.get("width")
        if isinstance(width, int) and width > 0:
            return {"kind": "bitvector", "width": width}
        return {"kind": "int"}
    if isinstance(literal, str):
        return {"kind": "string"}
    return None


def merge_symbol_hint(symbol: dict[str, Any], hint: dict[str, Any]) -> None:
    roles = set(symbol.get("roles", []) if isinstance(symbol.get("roles"), list) else [])
    roles.update(str(role) for role in hint.get("roles", set()))
    symbol["roles"] = sorted(role for role in roles if role)
    current_type = symbol.get("type") if isinstance(symbol.get("type"), dict) else {"kind": "any"}
    hint_type = hint.get("type") if isinstance(hint.get("type"), dict) else {"kind": "any"}
    if current_type.get("kind") == "any" and hint_type.get("kind") != "any":
        symbol["type"] = hint_type
