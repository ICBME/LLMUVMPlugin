"""Validation-feedback repair loop for SemanticSpecIR documents."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from LLMPlugin import CallableLLMBackend, LLMBackend, LLMBackendError, LLMRequest
from rtlagent_bfm.codegen.oracle_ir import load_manifest_summary
from rtlagent_bfm.loader import load_ir

from .semantic_ir import (
    SemanticSpecIRCallable,
    llm_response_to_json,
    load_semantic_spec_ir,
    normalize_semantic_spec_ir_response,
    semantic_spec_ir_contract,
    write_semantic_spec_ir,
)
from .validation_review import review_semantic_spec_ir, write_semantic_review


SemanticRepairCallable = Callable[[dict[str, Any], str | None], dict[str, Any] | None]


def build_semantic_spec_ir_repair_prompt(
    semantic_ir: dict[str, Any],
    *,
    manifest_path: str | Path | None = None,
    spec_paths: Iterable[str | Path] = (),
    design_ir_path: str | Path | None = None,
    target: str | None = None,
    require_reviewed: bool = False,
) -> dict[str, Any]:
    """Build a strict JSON prompt for repairing SemanticSpecIR review findings."""

    spec_path_tuple = tuple(spec_paths)
    review = review_semantic_spec_ir(
        semantic_ir,
        manifest_path=manifest_path,
        spec_paths=spec_path_tuple,
        design_ir_path=design_ir_path,
        target=target,
        require_reviewed=require_reviewed,
    )
    return {
        "task": "Repair the SemanticSpecIR JSON so it passes validation review.",
        "workflow": "semantic_spec_ir_validation_feedback_repair",
        "target": target or semantic_ir.get("target"),
        "current_semantic_spec_ir": semantic_ir,
        "review_report": review,
        "constraints": [
            "Return one complete corrected SemanticSpecIR object under the top-level key semantic_spec_ir.",
            "Preserve or repair spec_claims for every normative or behavior-relevant source statement.",
            "Every normative spec_claim must be covered by semantic_elements[].claim_ids, open_questions[].claim_ids, or semantic_gaps[].claim_ids.",
            "Preserve traceability: every semantic element must cite evidence from the original spec text.",
            "Do not fabricate source quotes; evidence quotes must appear in the referenced source line range.",
            "Do not invent manifest fields, DesignIR bindings, or source files.",
            "Represent spec semantics independent of backend support; do not decide whether refmodel, SVA, or OracleIR can lower it.",
            "Use strict RepresentationAST v2 in semantic_elements[].representation; include ast_version, kind, text, and ast.",
            "Prefer typed clock_reset_context, latency_rule, handshake_rule, and signal_binding nodes over text_expr for temporal and protocol semantics.",
            "Do not use legacy free-form representation.type or representation.fields; AST field references must use field_ref nodes.",
            "If representation.ast is semantic_claim, text_expr-only temporal/protocol/constraint roots, latency endpoints with text_expr, handshake rules without clock context, or contains placeholder targets/conditions, use a blocking formalization_status and human review path.",
            "Do not mark review.status accepted unless the input already contains sufficient human review evidence.",
            "If behavior cannot be safely formalized, keep it in semantic_gaps or open_questions instead of inventing rules.",
            "Do not generate Python code, OracleIR, or plugin artifacts in this stage.",
        ],
        "semantic_spec_ir_contract": semantic_spec_ir_contract(),
        "response_contract": {
            "semantic_spec_ir": "complete corrected SemanticSpecIR JSON object",
            "changes": ["optional concise repair summary"],
            "assumptions": ["optional assumptions introduced during repair"],
        },
        "inputs": {
            "target_manifest": manifest_payload(manifest_path),
            "design_ir": design_ir_payload(design_ir_path),
            "specs": spec_payloads(spec_path_tuple),
        },
    }


def repair_semantic_spec_ir_with_review(
    semantic_ir: dict[str, Any],
    *,
    manifest_path: str | Path | None = None,
    spec_paths: Iterable[str | Path] = (),
    design_ir_path: str | Path | None = None,
    target: str | None = None,
    require_reviewed: bool = False,
    llm_backend: LLMBackend | None = None,
    llm_callable: SemanticSpecIRCallable | None = None,
    model: str | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Review and optionally repair a SemanticSpecIR with an LLM backend."""

    current = json_round_trip(semantic_ir)
    spec_path_tuple = tuple(spec_paths)
    backend = llm_backend
    if backend is None and llm_callable is not None:
        backend = CallableLLMBackend(llm_callable)
    llm_responses: list[dict[str, Any]] = []
    review = review_semantic_spec_ir(
        current,
        manifest_path=manifest_path,
        spec_paths=spec_path_tuple,
        design_ir_path=design_ir_path,
        target=target,
        require_reviewed=require_reviewed,
    )
    prompt = build_semantic_spec_ir_repair_prompt(
        current,
        manifest_path=manifest_path,
        spec_paths=spec_path_tuple,
        design_ir_path=design_ir_path,
        target=target,
        require_reviewed=require_reviewed,
    )
    if review["status"] == "passed":
        return repair_result("valid", current, review, prompt, llm_responses)
    if review["status"] == "needs_human_input":
        return repair_result("needs_human_input", current, review, prompt, llm_responses)
    if backend is None:
        return repair_result("invalid", current, review, prompt, llm_responses)

    attempts = max(0, int(max_attempts))
    for _attempt in range(attempts):
        try:
            response = invoke_semantic_repair_backend(
                backend,
                prompt,
                target=str(target or current.get("target") or "unknown"),
                model=model,
            )
        except LLMBackendError as exc:
            llm_responses.append(llm_error_payload(exc, backend=backend))
            return repair_result("llm_unavailable", current, review, prompt, llm_responses)
        if response is None:
            return repair_result("llm_unavailable", current, review, prompt, llm_responses)
        llm_responses.append(response)
        try:
            current = normalize_semantic_spec_ir_response(response)
        except (TypeError, ValueError) as exc:
            review = review_with_response_error(review, str(exc))
            return repair_result("llm_invalid_response", current, review, prompt, llm_responses)

        review = review_semantic_spec_ir(
            current,
            manifest_path=manifest_path,
            spec_paths=spec_path_tuple,
            design_ir_path=design_ir_path,
            target=target,
            require_reviewed=require_reviewed,
        )
        prompt = build_semantic_spec_ir_repair_prompt(
            current,
            manifest_path=manifest_path,
            spec_paths=spec_path_tuple,
            design_ir_path=design_ir_path,
            target=target,
            require_reviewed=require_reviewed,
        )
        if review["status"] == "passed":
            return repair_result("repaired", current, review, prompt, llm_responses)
        if review["status"] == "needs_human_input":
            return repair_result("needs_human_input", current, review, prompt, llm_responses)

    return repair_result("repair_failed", current, review, prompt, llm_responses)


def repair_semantic_spec_ir_file(
    path: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    return repair_semantic_spec_ir_with_review(load_semantic_spec_ir(path), **kwargs)


def write_semantic_repair_artifacts(
    *,
    semantic_ir_path: str | Path | None = None,
    review_path: str | Path | None = None,
    prompt_path: str | Path | None = None,
    result: dict[str, Any],
) -> None:
    if semantic_ir_path is not None:
        write_semantic_spec_ir(semantic_ir_path, result["semantic_ir"])
    if review_path is not None:
        write_semantic_review(review_path, result["review"])
    if prompt_path is not None:
        output = Path(prompt_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result["prompt"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def invoke_semantic_repair_backend(
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
                "You repair traceable hardware SemanticSpecIR documents. "
                "Return strict JSON satisfying the response_contract."
            ),
            run_name="semantic_spec_ir_validation_feedback_repair",
            tags=("semantic-spec-ir-repair", target),
            metadata={"target": target},
            response_format="json_object",
        )
    )
    if response is None:
        return None
    return llm_response_to_json(response)


def repair_result(
    status: str,
    semantic_ir: dict[str, Any],
    review: dict[str, Any],
    prompt: dict[str, Any],
    llm_responses: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": status,
        "semantic_ir": semantic_ir,
        "review": review,
        "prompt": prompt,
        "llm_responses": llm_responses,
        "attempt_count": len(llm_responses),
    }


def review_with_response_error(review: dict[str, Any], message: str) -> dict[str, Any]:
    updated = json_round_trip(review)
    updated["status"] = "failed"
    updated.setdefault("findings", []).append(
        {
            "stage": "repair_response_review",
            "severity": "error",
            "path": "llm_response",
            "message": message,
            "blocking": True,
        }
    )
    return updated


def llm_error_payload(exc: Exception, *, backend: LLMBackend) -> dict[str, Any]:
    return {
        "error": type(exc).__name__,
        "message": str(exc),
        "backend": getattr(backend, "name", type(backend).__name__),
    }


def manifest_payload(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    manifest_path = Path(path)
    payload: dict[str, Any] = {
        "path": str(manifest_path),
        "content": manifest_path.read_text(encoding="utf-8"),
    }
    try:
        manifest = load_manifest_summary(manifest_path)
    except Exception:
        return payload
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


def design_ir_payload(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    design_path = Path(path)
    payload: dict[str, Any] = {
        "path": str(design_path),
        "content": design_path.read_text(encoding="utf-8"),
    }
    try:
        design_ir = load_ir(design_path)
    except Exception:
        return payload
    payload["summary"] = {
        "top": design_ir.top,
        "interfaces": sorted(design_ir.interfaces),
        "bindings": sorted(design_ir.bindings),
        "registers": sorted(design_ir.registers),
    }
    return payload


def spec_payloads(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    payloads = []
    for index, raw_path in enumerate(paths, start=1):
        path = Path(raw_path)
        payloads.append(
            {
                "id": f"src{index}",
                "path": str(path),
                "content": path.read_text(encoding="utf-8", errors="replace"),
            }
        )
    return payloads


def json_round_trip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(deepcopy(value), sort_keys=True))
