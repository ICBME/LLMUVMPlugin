"""Traceable semantic IR extraction from natural-language specs.

SemanticSpecIR is the reviewable layer between raw target documentation and
lower-level verifiable artifacts such as OracleIR.  It deliberately keeps
evidence, confidence, open questions, and review state alongside extracted
behavior so humans can audit and complete the spec semantics before codegen.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from LLMPlugin import (
    CallableLLMBackend,
    LLMBackend,
    LLMRequest,
    LLMResponse,
    create_backend,
)
from rtlagent_bfm.codegen.oracle_ir import (
    ALLOWED_CALLS,
    ManifestField,
    ManifestSummary,
    input_from_manifest_field,
    load_manifest_summary,
    normalize_algorithm_text,
    select_algorithm_mode_field,
    select_hex_message_field,
)
from rtlagent_bfm.loader import load_ir


SEMANTIC_SPEC_IR_SCHEMA_VERSION = 1

SHA_ALGORITHMS = ("sha1", "sha224", "sha256", "sha384", "sha512")
CRC_ALGORITHMS = ("crc32",)
SUPPORTED_ALGORITHMS = SHA_ALGORITHMS + CRC_ALGORITHMS
TRACEABLE_SOURCE_KIND = "natural_language_spec"
ALLOWED_ITEM_STATUSES = {
    "draft",
    "needs_review",
    "human_confirmed",
    "accepted",
    "rejected",
}
ALLOWED_REVIEW_STATUSES = {
    "draft",
    "needs_human_input",
    "accepted",
    "rejected",
}


SemanticSpecIRCallable = Callable[[dict[str, Any], str | None], dict[str, Any] | None]


class SemanticSpecIRValidationError(ValueError):
    """Raised when a SemanticSpecIR document fails validation."""


@dataclass(frozen=True)
class SourceDocument:
    id: str
    path: Path
    text: str

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "kind": TRACEABLE_SOURCE_KIND,
            "content_hash": sha256_text(self.text),
            "line_count": len(self.lines),
        }


@dataclass(frozen=True)
class SemanticSpecIRIssue:
    path: str
    message: str

    def format(self) -> str:
        return f"{self.path}: {self.message}"


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
    evidence = collect_algorithm_evidence(documents)
    semantic_items = infer_algorithm_semantic_items(
        manifest=manifest,
        documents=documents,
        evidence=evidence,
    )
    open_questions = build_open_questions(manifest, semantic_items, evidence)
    unsupported = []
    if not semantic_items:
        unsupported.append(
            {
                "reason": "No supported reference-model behavior was extracted from the spec text.",
                "requires": "LLM extraction or human SemanticSpecIR authoring",
            }
        )

    review_status = "needs_human_input" if any(
        bool(question.get("blocking")) for question in open_questions
    ) else "draft"
    return {
        "schema_version": SEMANTIC_SPEC_IR_SCHEMA_VERSION,
        "target": target,
        "sources": [document.payload() for document in documents],
        "inputs": inputs,
        "semantic_items": semantic_items,
        "evidence": evidence,
        "open_questions": open_questions,
        "assumptions": default_assumptions(semantic_items),
        "unsupported": unsupported,
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
            "Every semantic item must cite at least one evidence id from the original spec text.",
            "Evidence must include source_id, line_start, line_end, and a short quote copied from those lines.",
            "Use open_questions for missing, ambiguous, or conflicting semantics.",
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


def validate_semantic_spec_ir(
    ir: dict[str, Any],
    *,
    manifest_path: str | Path | None = None,
    spec_paths: Iterable[str | Path] = (),
    target: str | None = None,
    require_reviewed: bool = False,
) -> None:
    issues = collect_semantic_spec_ir_issues(
        ir,
        manifest_path=manifest_path,
        spec_paths=spec_paths,
        target=target,
        require_reviewed=require_reviewed,
    )
    if issues:
        formatted = "\n".join(issue.format() for issue in issues)
        raise SemanticSpecIRValidationError(f"SemanticSpecIR validation failed:\n{formatted}")


def collect_semantic_spec_ir_issues(
    ir: Any,
    *,
    manifest_path: str | Path | None = None,
    spec_paths: Iterable[str | Path] = (),
    target: str | None = None,
    require_reviewed: bool = False,
) -> list[SemanticSpecIRIssue]:
    issues: list[SemanticSpecIRIssue] = []
    if not isinstance(ir, dict):
        return [SemanticSpecIRIssue("$", "SemanticSpecIR must be a JSON object")]

    manifest: ManifestSummary | None = None
    if manifest_path is not None:
        try:
            manifest = load_manifest_summary(manifest_path)
        except Exception as exc:  # noqa: BLE001 - surface manifest context
            issues.append(SemanticSpecIRIssue("manifest", str(exc)))
    manifest_fields = {field.name for field in manifest.fields} if manifest else set()

    schema_version = ir.get("schema_version")
    if schema_version != SEMANTIC_SPEC_IR_SCHEMA_VERSION:
        issues.append(
            SemanticSpecIRIssue(
                "schema_version",
                f"must be {SEMANTIC_SPEC_IR_SCHEMA_VERSION}, got {schema_version!r}",
            )
        )
    ir_target = ir.get("target")
    if not isinstance(ir_target, str) or not ir_target:
        issues.append(SemanticSpecIRIssue("target", "must be a non-empty string"))
    if target is not None and ir_target != target:
        issues.append(SemanticSpecIRIssue("target", f"expected {target!r}, got {ir_target!r}"))
    if manifest is not None and ir_target != manifest.target:
        issues.append(
            SemanticSpecIRIssue(
                "target",
                f"does not match manifest target {manifest.target!r}",
            )
        )

    source_ids = validate_sources(ir.get("sources"), spec_paths, issues)
    evidence_ids = validate_evidence(ir.get("evidence"), source_ids, spec_paths, issues)
    validate_inputs(ir.get("inputs"), manifest_fields, issues)
    item_ids = validate_semantic_items(
        ir.get("semantic_items"),
        evidence_ids,
        manifest_fields,
        issues,
    )
    validate_open_questions(ir.get("open_questions", []), item_ids, issues)
    validate_string_list(ir.get("assumptions", []), "assumptions", issues)
    validate_review(ir.get("review"), require_reviewed, issues)
    unsupported = ir.get("unsupported", [])
    if unsupported is not None and not isinstance(unsupported, list):
        issues.append(SemanticSpecIRIssue("unsupported", "must be a list"))
    if not ir.get("semantic_items") and not unsupported:
        issues.append(
            SemanticSpecIRIssue(
                "unsupported",
                "must explain why no semantic items were generated",
            )
        )
    return issues


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
                "You extract traceable hardware reference-model semantics. "
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
            "inputs",
            "semantic_items",
            "evidence",
            "open_questions",
            "review",
        ],
        "source_schema": {
            "id": "stable source id such as src1",
            "path": "source file path",
            "kind": TRACEABLE_SOURCE_KIND,
            "content_hash": "sha256 hex of source content",
            "line_count": "number of lines",
        },
        "semantic_item_schema": {
            "id": "stable semantic item id such as sem1",
            "kind": "functional_behavior | reference_model_rule | state_behavior | compare_policy",
            "summary": "human-readable semantic claim",
            "status": sorted(ALLOWED_ITEM_STATUSES),
            "confidence": "0.0 to 1.0",
            "subjects": ["manifest fields, outputs, registers, or protocol entities"],
            "conditions": [{"field": "mode", "op": "eq", "value": "sha256"}],
            "effects": [
                {
                    "kind": "compute_expected",
                    "output": "expected",
                    "expr": {
                        "call": "hashlib.sha256",
                        "args": [{"bytes_from_hex": {"field": "message"}}],
                        "format": "hexdigest",
                    },
                }
            ],
            "evidence": ["ev1"],
        },
        "allowed_review_statuses": sorted(ALLOWED_REVIEW_STATUSES),
        "allowed_lowerable_calls": sorted(ALLOWED_CALLS),
    }


def load_source_documents(paths: Iterable[str | Path]) -> tuple[SourceDocument, ...]:
    documents = []
    for index, raw_path in enumerate(paths, start=1):
        path = Path(raw_path)
        documents.append(
            SourceDocument(
                id=f"src{index}",
                path=path,
                text=path.read_text(encoding="utf-8", errors="replace"),
            )
        )
    return tuple(documents)


def collect_algorithm_evidence(documents: tuple[SourceDocument, ...]) -> list[dict[str, Any]]:
    keywords = SUPPORTED_ALGORITHMS + ("checksum", "digest", "hash")
    evidence: list[dict[str, Any]] = []
    for document in documents:
        for line_no, line in enumerate(document.lines, start=1):
            normalized = normalize_algorithm_text(line)
            if not any(keyword in normalized for keyword in keywords):
                continue
            evidence.append(
                {
                    "id": f"ev{len(evidence) + 1}",
                    "source_id": document.id,
                    "line_start": line_no,
                    "line_end": line_no,
                    "quote": line.strip()[:240],
                }
            )
    return evidence


def infer_algorithm_semantic_items(
    *,
    manifest: ManifestSummary,
    documents: tuple[SourceDocument, ...],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    message_field = select_hex_message_field(manifest.fields)
    if message_field is None:
        return []

    text = normalize_algorithm_text(
        "\n".join(
            [
                manifest.target,
                *field_context_parts(manifest.fields),
                *(document.text for document in documents),
            ]
        )
    )
    mode_field = select_algorithm_mode_field(manifest.fields)
    semantic_items: list[dict[str, Any]] = []
    for algorithm in SUPPORTED_ALGORITHMS:
        if not algorithm_is_present(algorithm, text, mode_field):
            continue
        evidence_ids = [
            str(item["id"])
            for item in evidence
            if algorithm in normalize_algorithm_text(str(item.get("quote", "")))
            or (
                algorithm == "crc32"
                and "checksum" in normalize_algorithm_text(str(item.get("quote", "")))
            )
        ]
        if not evidence_ids:
            continue
        semantic_items.append(
            semantic_item_for_algorithm(
                algorithm=algorithm,
                message_field=message_field,
                mode_field=mode_field,
                evidence_ids=evidence_ids,
                index=len(semantic_items) + 1,
            )
        )
    return semantic_items


def semantic_item_for_algorithm(
    *,
    algorithm: str,
    message_field: ManifestField,
    mode_field: ManifestField | None,
    evidence_ids: list[str],
    index: int,
) -> dict[str, Any]:
    condition = None
    subjects = [message_field.name, "expected"]
    if mode_field is not None:
        condition = {"field": mode_field.name, "op": "eq", "value": algorithm}
        subjects.insert(0, mode_field.name)
    call_name = f"hashlib.{algorithm}" if algorithm.startswith("sha") else "zlib.crc32"
    item: dict[str, Any] = {
        "id": f"sem{index}",
        "kind": "reference_model_rule",
        "summary": (
            f"Expected result is {algorithm.upper()} of input field "
            f"{message_field.name!r}."
        ),
        "status": "needs_review",
        "confidence": 0.72 if evidence_ids else 0.55,
        "subjects": subjects,
        "conditions": [condition] if condition else [],
        "effects": [
            {
                "kind": "compute_expected",
                "output": "expected",
                "expr": {
                    "call": call_name,
                    "args": [{"bytes_from_hex": {"field": message_field.name}}],
                    "format": "hexdigest" if algorithm.startswith("sha") else "hex",
                },
            }
        ],
        "evidence": evidence_ids,
        "provenance": {
            "source": "rule_based",
            "extractor": "algorithm_keyword_semantic_extractor",
        },
    }
    return item


def build_open_questions(
    manifest: ManifestSummary,
    semantic_items: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    hex_fields = [field for field in manifest.fields if field.kind == "hex"]
    if len(hex_fields) > 1 and semantic_items:
        questions.append(
            {
                "id": f"q{len(questions) + 1}",
                "blocking": True,
                "status": "open",
                "question": "Which hex manifest field is the reference-model message/input payload?",
                "related_items": [item["id"] for item in semantic_items],
                "suggested_answers": [field.name for field in hex_fields],
            }
        )
    if semantic_items:
        questions.append(
            {
                "id": f"q{len(questions) + 1}",
                "blocking": False,
                "status": "open",
                "question": "Confirm the expected-result representation used by the scoreboard.",
                "related_items": [item["id"] for item in semantic_items],
                "suggested_answers": ["lowercase_hex_string", "raw_bytes", "integer"],
            }
        )
    if not semantic_items and evidence:
        questions.append(
            {
                "id": f"q{len(questions) + 1}",
                "blocking": True,
                "status": "open",
                "question": "The spec mentions hash/checksum terms, but no lowerable behavior was extracted.",
                "related_items": [],
                "suggested_answers": ["author_semantic_item", "mark_unsupported"],
            }
        )
    return questions


def default_assumptions(semantic_items: list[dict[str, Any]]) -> list[str]:
    if not semantic_items:
        return []
    return [
        "Algorithmic expected values are represented using the effect expression format until reviewed.",
    ]


def validate_sources(
    value: Any,
    spec_paths: Iterable[str | Path],
    issues: list[SemanticSpecIRIssue],
) -> set[str]:
    source_ids: set[str] = set()
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue("sources", "must be a list"))
        return source_ids
    expected_paths = {str(Path(path)) for path in spec_paths}
    expected_hashes = {
        str(Path(path)): sha256_text(Path(path).read_text(encoding="utf-8", errors="replace"))
        for path in spec_paths
        if Path(path).exists()
    }
    for index, item in enumerate(value):
        path = f"sources[{index}]"
        if not isinstance(item, dict):
            issues.append(SemanticSpecIRIssue(path, "must be an object"))
            continue
        source_id = item.get("id")
        if not isinstance(source_id, str) or not source_id:
            issues.append(SemanticSpecIRIssue(f"{path}.id", "must be a non-empty string"))
            continue
        if source_id in source_ids:
            issues.append(SemanticSpecIRIssue(f"{path}.id", f"duplicate source id {source_id!r}"))
        source_ids.add(source_id)
        source_path = item.get("path")
        if not isinstance(source_path, str) or not source_path:
            issues.append(SemanticSpecIRIssue(f"{path}.path", "must be a non-empty string"))
        elif expected_paths and source_path not in expected_paths:
            issues.append(SemanticSpecIRIssue(f"{path}.path", "not present in requested spec paths"))
        if item.get("kind") != TRACEABLE_SOURCE_KIND:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.kind",
                    f"must be {TRACEABLE_SOURCE_KIND!r}",
                )
            )
        content_hash = item.get("content_hash")
        if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            issues.append(SemanticSpecIRIssue(f"{path}.content_hash", "must be a sha256 hex string"))
        elif isinstance(source_path, str) and source_path in expected_hashes:
            expected_hash = expected_hashes[source_path]
            if content_hash != expected_hash:
                issues.append(
                    SemanticSpecIRIssue(
                        f"{path}.content_hash",
                        f"does not match source file hash {expected_hash}",
                    )
                )
    return source_ids


def validate_evidence(
    value: Any,
    source_ids: set[str],
    spec_paths: Iterable[str | Path],
    issues: list[SemanticSpecIRIssue],
) -> set[str]:
    evidence_ids: set[str] = set()
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue("evidence", "must be a list"))
        return evidence_ids
    source_lines = load_source_lines_by_id(spec_paths)
    for index, item in enumerate(value):
        path = f"evidence[{index}]"
        if not isinstance(item, dict):
            issues.append(SemanticSpecIRIssue(path, "must be an object"))
            continue
        evidence_id = item.get("id")
        if not isinstance(evidence_id, str) or not evidence_id:
            issues.append(SemanticSpecIRIssue(f"{path}.id", "must be a non-empty string"))
            continue
        if evidence_id in evidence_ids:
            issues.append(SemanticSpecIRIssue(f"{path}.id", f"duplicate evidence id {evidence_id!r}"))
        evidence_ids.add(evidence_id)
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or source_id not in source_ids:
            issues.append(SemanticSpecIRIssue(f"{path}.source_id", f"unknown source id {source_id!r}"))
        line_start = item.get("line_start")
        line_end = item.get("line_end")
        if not isinstance(line_start, int) or line_start < 1:
            issues.append(SemanticSpecIRIssue(f"{path}.line_start", "must be a positive integer"))
        if not isinstance(line_end, int) or line_end < 1:
            issues.append(SemanticSpecIRIssue(f"{path}.line_end", "must be a positive integer"))
        if isinstance(line_start, int) and isinstance(line_end, int) and line_end < line_start:
            issues.append(SemanticSpecIRIssue(f"{path}.line_end", "must be >= line_start"))
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            issues.append(SemanticSpecIRIssue(f"{path}.quote", "must be a non-empty string"))
        elif (
            isinstance(source_id, str)
            and isinstance(line_start, int)
            and isinstance(line_end, int)
            and source_id in source_lines
        ):
            quoted_lines = "\n".join(source_lines[source_id][line_start - 1 : line_end])
            if quote.strip() not in quoted_lines:
                issues.append(
                    SemanticSpecIRIssue(
                        f"{path}.quote",
                        "must appear in the referenced source line range",
                    )
                )
    return evidence_ids


def validate_inputs(
    value: Any,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> set[str]:
    input_names: set[str] = set()
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue("inputs", "must be a list"))
        return input_names
    for index, item in enumerate(value):
        path = f"inputs[{index}]"
        if not isinstance(item, dict):
            issues.append(SemanticSpecIRIssue(path, "must be an object"))
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            issues.append(SemanticSpecIRIssue(f"{path}.name", "must be a non-empty string"))
            continue
        if name in input_names:
            issues.append(SemanticSpecIRIssue(f"{path}.name", f"duplicate input {name!r}"))
        input_names.add(name)
        if manifest_fields and name not in manifest_fields:
            issues.append(SemanticSpecIRIssue(f"{path}.name", "not present in manifest fields"))
    return input_names


def validate_semantic_items(
    value: Any,
    evidence_ids: set[str],
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> set[str]:
    item_ids: set[str] = set()
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue("semantic_items", "must be a list"))
        return item_ids
    for index, item in enumerate(value):
        path = f"semantic_items[{index}]"
        if not isinstance(item, dict):
            issues.append(SemanticSpecIRIssue(path, "must be an object"))
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            issues.append(SemanticSpecIRIssue(f"{path}.id", "must be a non-empty string"))
            continue
        if item_id in item_ids:
            issues.append(SemanticSpecIRIssue(f"{path}.id", f"duplicate semantic item id {item_id!r}"))
        item_ids.add(item_id)
        if not isinstance(item.get("kind"), str) or not item.get("kind"):
            issues.append(SemanticSpecIRIssue(f"{path}.kind", "must be a non-empty string"))
        if not isinstance(item.get("summary"), str) or not item.get("summary"):
            issues.append(SemanticSpecIRIssue(f"{path}.summary", "must be a non-empty string"))
        status = item.get("status")
        if status not in ALLOWED_ITEM_STATUSES:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.status",
                    f"must be one of {sorted(ALLOWED_ITEM_STATUSES)}, got {status!r}",
                )
            )
        confidence = item.get("confidence")
        if not isinstance(confidence, int | float) or not 0 <= float(confidence) <= 1:
            issues.append(SemanticSpecIRIssue(f"{path}.confidence", "must be a number from 0 to 1"))
        validate_string_list(item.get("subjects", []), f"{path}.subjects", issues)
        validate_item_evidence(item.get("evidence"), f"{path}.evidence", evidence_ids, issues)
        validate_conditions(item.get("conditions", []), f"{path}.conditions", manifest_fields, issues)
        validate_effects(item.get("effects", []), f"{path}.effects", manifest_fields, issues)
    return item_ids


def validate_open_questions(
    value: Any,
    item_ids: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue("open_questions", "must be a list"))
        return
    question_ids: set[str] = set()
    for index, item in enumerate(value):
        path = f"open_questions[{index}]"
        if not isinstance(item, dict):
            issues.append(SemanticSpecIRIssue(path, "must be an object"))
            continue
        question_id = item.get("id")
        if not isinstance(question_id, str) or not question_id:
            issues.append(SemanticSpecIRIssue(f"{path}.id", "must be a non-empty string"))
        elif question_id in question_ids:
            issues.append(SemanticSpecIRIssue(f"{path}.id", f"duplicate question id {question_id!r}"))
        else:
            question_ids.add(question_id)
        if not isinstance(item.get("question"), str) or not item.get("question"):
            issues.append(SemanticSpecIRIssue(f"{path}.question", "must be a non-empty string"))
        if "blocking" in item and not isinstance(item["blocking"], bool):
            issues.append(SemanticSpecIRIssue(f"{path}.blocking", "must be a boolean"))
        validate_string_list(item.get("suggested_answers", []), f"{path}.suggested_answers", issues)
        related_items = item.get("related_items", [])
        validate_string_list(related_items, f"{path}.related_items", issues)
        if isinstance(related_items, list):
            for related_index, related_id in enumerate(related_items):
                if isinstance(related_id, str) and related_id not in item_ids:
                    issues.append(
                        SemanticSpecIRIssue(
                            f"{path}.related_items[{related_index}]",
                            f"unknown semantic item id {related_id!r}",
                        )
                    )


def validate_review(
    value: Any,
    require_reviewed: bool,
    issues: list[SemanticSpecIRIssue],
) -> None:
    if not isinstance(value, dict):
        issues.append(SemanticSpecIRIssue("review", "must be an object"))
        return
    status = value.get("status")
    if status not in ALLOWED_REVIEW_STATUSES:
        issues.append(
            SemanticSpecIRIssue(
                "review.status",
                f"must be one of {sorted(ALLOWED_REVIEW_STATUSES)}, got {status!r}",
            )
        )
    if require_reviewed and status != "accepted":
        issues.append(SemanticSpecIRIssue("review.status", "must be accepted"))
    human_answers = value.get("human_answers", [])
    if human_answers is not None and not isinstance(human_answers, list):
        issues.append(SemanticSpecIRIssue("review.human_answers", "must be a list"))
    reviewed_items = value.get("reviewed_items", [])
    validate_string_list(reviewed_items, "review.reviewed_items", issues)


def validate_item_evidence(
    value: Any,
    path: str,
    evidence_ids: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if not isinstance(value, list) or not value:
        issues.append(SemanticSpecIRIssue(path, "must contain at least one evidence id"))
        return
    for index, evidence_id in enumerate(value):
        if not isinstance(evidence_id, str):
            issues.append(SemanticSpecIRIssue(f"{path}[{index}]", "must be a string"))
        elif evidence_id not in evidence_ids:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}[{index}]",
                    f"unknown evidence id {evidence_id!r}",
                )
            )


def validate_conditions(
    value: Any,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue(path, "must be a list"))
        return
    for index, condition in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(condition, dict):
            issues.append(SemanticSpecIRIssue(item_path, "must be an object"))
            continue
        field = condition.get("field")
        if field is not None and (not isinstance(field, str) or not field):
            issues.append(SemanticSpecIRIssue(f"{item_path}.field", "must be a non-empty string"))
        elif isinstance(field, str) and manifest_fields and field not in manifest_fields:
            issues.append(SemanticSpecIRIssue(f"{item_path}.field", f"unknown manifest field {field!r}"))
        if "op" in condition and not isinstance(condition["op"], str):
            issues.append(SemanticSpecIRIssue(f"{item_path}.op", "must be a string"))


def validate_effects(
    value: Any,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue(path, "must be a list"))
        return
    for index, effect in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(effect, dict):
            issues.append(SemanticSpecIRIssue(item_path, "must be an object"))
            continue
        if not isinstance(effect.get("kind"), str) or not effect.get("kind"):
            issues.append(SemanticSpecIRIssue(f"{item_path}.kind", "must be a non-empty string"))
        if "expr" in effect:
            validate_expr_references(effect["expr"], f"{item_path}.expr", manifest_fields, issues)


def validate_expr_references(
    expr: Any,
    path: str,
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if isinstance(expr, str | int | float | bool) or expr is None:
        return
    if isinstance(expr, list):
        for index, item in enumerate(expr):
            validate_expr_references(item, f"{path}[{index}]", manifest_fields, issues)
        return
    if not isinstance(expr, dict):
        issues.append(SemanticSpecIRIssue(path, "expression must be a scalar, list, or object"))
        return
    if "field" in expr:
        field_name = expr["field"]
        if not isinstance(field_name, str) or not field_name:
            issues.append(SemanticSpecIRIssue(f"{path}.field", "must be a non-empty string"))
        elif manifest_fields and field_name not in manifest_fields:
            issues.append(SemanticSpecIRIssue(f"{path}.field", f"unknown manifest field {field_name!r}"))
    if "call" in expr and expr["call"] not in ALLOWED_CALLS:
        issues.append(
            SemanticSpecIRIssue(
                f"{path}.call",
                f"not lowerable by current OracleIR call allowlist: {expr['call']!r}",
            )
        )
    for key, value in expr.items():
        if key in {"field", "call", "format"}:
            continue
        validate_expr_references(value, f"{path}.{key}", manifest_fields, issues)


def validate_string_list(
    value: Any,
    path: str,
    issues: list[SemanticSpecIRIssue],
) -> None:
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue(path, "must be a list"))
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(SemanticSpecIRIssue(f"{path}[{index}]", "must be a string"))


def load_source_lines_by_id(spec_paths: Iterable[str | Path]) -> dict[str, list[str]]:
    result = {}
    for index, raw_path in enumerate(spec_paths, start=1):
        path = Path(raw_path)
        if path.exists():
            result[f"src{index}"] = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return result


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


def field_context_parts(fields: tuple[ManifestField, ...]) -> list[str]:
    parts = []
    for field in fields:
        parts.append(field.name)
        parts.extend(str(choice) for choice in field.choices)
    return parts


def algorithm_is_present(
    algorithm: str,
    normalized_text: str,
    mode_field: ManifestField | None,
) -> bool:
    if mode_field is not None and str(algorithm).lower() in {
        str(choice).lower() for choice in mode_field.choices
    }:
        return True
    return algorithm in normalized_text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def looks_like_semantic_spec_ir(value: dict[str, Any]) -> bool:
    return {
        "schema_version",
        "target",
        "sources",
        "semantic_items",
        "evidence",
        "review",
    }.issubset(value)


def json_round_trip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(deepcopy(value), sort_keys=True))


def extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("{"):
        return text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain JSON")
    return match.group(0)
