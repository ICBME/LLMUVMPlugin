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

from LLMPlugin import (
    CallableLLMBackend,
    LLMBackend,
    LLMRequest,
    LLMResponse,
    create_backend,
)
from rtlagent_bfm.loader import load_ir

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
    SemanticSpecIRCallable,
    SemanticSpecIRIssue,
    SemanticSpecIRValidationError,
    SourceDocument,
    extract_json,
    json_round_trip,
    load_source_documents,
    looks_like_semantic_spec_ir,
    sha256_text,
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
    llm_backend: LLMBackend | None = None,
    llm_callable: SemanticSpecIRCallable | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Generate a draft SemanticSpecIR from target docs.

    If ``llm_backend`` is provided and returns a value, that response is
    normalized and validated as the primary extraction.  Without an LLM, this
    function still returns a deterministic first-pass IR for supported
    algorithmic behaviors so the pipeline remains testable and reviewable.
    """

    manifest = load_manifest_summary(manifest_path)
    target_name = target or manifest.target
    if manifest.target != target_name:
        raise SemanticSpecIRValidationError(
            f"manifest target {manifest.target!r} does not match requested target {target_name!r}"
        )
    documents = load_source_documents(spec_paths)
    prompt = build_semantic_spec_ir_prompt(
        manifest_path=manifest.path,
        spec_paths=tuple(document.path for document in documents),
        design_ir_path=design_ir_path,
        target=target_name,
    )
    backend = llm_backend
    if backend is None and llm_callable is not None:
        backend = CallableLLMBackend(llm_callable)
    if backend is not None:
        response = invoke_semantic_spec_ir_backend(
            backend,
            prompt,
            target=target_name,
            model=model,
        )
        if response is not None:
            ir = normalize_semantic_spec_ir_response(response)
            validate_semantic_spec_ir(
                ir,
                manifest_path=manifest.path,
                spec_paths=tuple(document.path for document in documents),
                target=target_name,
            )
            return ir

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
    return {
        "schema_version": SEMANTIC_SPEC_IR_SCHEMA_VERSION,
        "target": target,
        "sources": [document.payload() for document in documents],
        "spec_claims": spec_claims,
        "inputs": inputs,
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


def build_semantic_spec_ir_prompt(
    *,
    manifest_path: str | Path,
    spec_paths: Iterable[str | Path] = (),
    design_ir_path: str | Path | None = None,
    target: str | None = None,
) -> dict[str, Any]:
    """Build the strict JSON prompt used for LLM semantic extraction."""

    manifest = load_manifest_summary(manifest_path)
    target_name = target or manifest.target
    if manifest.target != target_name:
        raise SemanticSpecIRValidationError(
            f"manifest target {manifest.target!r} does not match requested target {target_name!r}"
        )
    documents = load_source_documents(spec_paths)
    design_ir_payload = None
    if design_ir_path is not None:
        path = Path(design_ir_path)
        design_ir = load_ir(path)
        design_ir_payload = {
            "path": str(path),
            "content": path.read_text(encoding="utf-8"),
            "summary": {
                "top": design_ir.top,
                "interfaces": sorted(design_ir.interfaces),
                "bindings": sorted(design_ir.bindings),
                "registers": sorted(design_ir.registers),
            },
        }

    return {
        "task": (
            "Extract a traceable SemanticSpecIR from natural-language hardware "
            "specifications. Return strict JSON only."
        ),
        "workflow": "natural_language_spec_to_traceable_semantic_ir",
        "target": target_name,
        "constraints": [
            "Return one complete SemanticSpecIR object under the top-level key semantic_spec_ir.",
            "Extract atomic spec_claims for every normative or behavior-relevant statement in the specs.",
            "Every source fragment with normative or behavior semantics must be represented by at least one spec_claim with correct source line provenance.",
            "For every spec_claim include a stable fingerprint and decomposition.atomic_obligations for condition, trigger, response, timing, clock/reset, protocol, interface, operation, state transition, truth-table row, or fallback behavior semantics.",
            "Every normative spec_claim must be covered by semantic_elements[].claim_ids, open_questions[].claim_ids, or semantic_gaps[].claim_ids.",
            "Every semantic element must cite at least one evidence id from the original spec text.",
            "Evidence must include source_id, line_start, line_end, and a short quote copied from those lines.",
            "Represent spec semantics independent of backend support; do not decide whether refmodel, SVA, or OracleIR can lower it.",
            "Use strict RepresentationAST v2 in semantic_elements[].representation; text is only review aid, ast is the semantic source of truth.",
            "RepresentationAST must include ast_version, kind, text, and ast; do not use legacy free-form type/fields representation.",
            "Prefer typed clock_reset_context, latency_rule, handshake_rule, and signal_binding nodes over text_expr for temporal and protocol semantics.",
            "If representation.ast is semantic_claim, text_expr-only temporal/protocol/constraint roots, latency endpoints with text_expr, handshake rules without clock context, or contains placeholder targets/conditions, formalization_status must be needs_human_review, incomplete, ambiguous, or conflict.",
            "Use open_questions for missing, ambiguous, or conflicting semantics.",
            "Use semantic_gaps for source claims that are not yet formalized, ambiguous, incomplete, or conflicting.",
            "Do not generate Python code, OracleIR, or plugin artifacts in this stage.",
            "Do not invent manifest fields; field references must come from inputs, manifest fields, or be marked as open questions.",
        ],
        "semantic_spec_ir_contract": semantic_spec_ir_contract(),
        "response_contract": {
            "semantic_spec_ir": "complete SemanticSpecIR JSON object",
            "notes": ["optional extraction notes"],
        },
        "inputs": {
            "target_manifest": {
                "path": str(manifest.path),
                "content": manifest.path.read_text(encoding="utf-8"),
                "summary": manifest_summary_payload(manifest),
            },
            "design_ir": design_ir_payload,
            "specs": [
                {
                    "id": document.id,
                    "path": str(document.path),
                    "content_hash": sha256_text(document.text),
                    "content": document.text,
                }
                for document in documents
            ],
        },
    }

def load_semantic_spec_ir(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_semantic_spec_ir(path: str | Path, ir: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(ir, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def invoke_semantic_spec_ir_backend(
    backend: LLMBackend,
    prompt: dict[str, Any],
    *,
    target: str,
    model: str | None = None,
) -> dict[str, Any] | None:
    response = backend.invoke(
        LLMRequest(
            prompt=prompt,
            model=model,
            system_prompt=(
                "You extract traceable hardware specification semantics. "
                "Return strict JSON satisfying the response_contract."
            ),
            run_name="semantic_spec_ir_extraction",
            tags=("semantic-spec-ir", target),
            metadata={"target": target},
            response_format="json_object",
        )
    )
    if response is None:
        return None
    return llm_response_to_json(response)


def maybe_call_semantic_spec_ir_llm(
    prompt: dict[str, Any],
    model: str | None = None,
    *,
    backend_name: str | None = None,
) -> dict[str, Any] | None:
    backend = create_backend(backend_name, model=model)
    if backend is None:
        return None
    return invoke_semantic_spec_ir_backend(
        backend,
        prompt,
        target=str(prompt.get("target") or "unknown"),
        model=model,
    )


def llm_response_to_json(response: LLMResponse) -> dict[str, Any]:
    if response.parsed_json is not None:
        return response.parsed_json
    return json.loads(extract_json(response.content))


def normalize_semantic_spec_ir_response(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"LLM response JSON must be an object, got {type(value).__name__}")
    if looks_like_semantic_spec_ir(value):
        return json_round_trip(value)
    for key in ("semantic_spec_ir", "semantic_ir", "ir", "result", "output", "response"):
        nested = value.get(key)
        if isinstance(nested, dict):
            if looks_like_semantic_spec_ir(nested):
                return json_round_trip(nested)
            try:
                return normalize_semantic_spec_ir_response(nested)
            except ValueError:
                continue
    raise ValueError("LLM response did not contain a SemanticSpecIR object")


def semantic_spec_ir_contract() -> dict[str, Any]:
    return {
        "schema_version": SEMANTIC_SPEC_IR_SCHEMA_VERSION,
        "required_top_level_keys": [
            "schema_version",
            "target",
            "sources",
            "spec_claims",
            "inputs",
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

def manifest_summary_payload(manifest: ManifestSummary) -> dict[str, Any]:
    return {
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
