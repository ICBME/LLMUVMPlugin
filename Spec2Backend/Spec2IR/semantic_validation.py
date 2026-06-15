"""Schema validation for SemanticSpecIR documents."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable

from rtlagent_bfm.codegen.oracle_ir import ManifestSummary, load_manifest_summary

from .representation_ast import representation_requires_human_review
from .representation_ast_validation import validate_representation
from .schema import (
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
    load_source_lines_by_id,
    sha256_text,
)


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
    claim_ids = validate_spec_claims(ir.get("spec_claims"), source_ids, spec_paths, issues)
    evidence_ids = validate_evidence(ir.get("evidence"), source_ids, spec_paths, issues)
    validate_inputs(ir.get("inputs"), manifest_fields, issues)
    element_ids = validate_semantic_elements(
        ir.get("semantic_elements"),
        evidence_ids,
        claim_ids,
        manifest_fields,
        issues,
    )
    validate_open_questions(ir.get("open_questions", []), element_ids, claim_ids, issues)
    validate_string_list(ir.get("assumptions", []), "assumptions", issues)
    validate_review(ir.get("review"), require_reviewed, issues)
    validate_semantic_gaps(ir.get("semantic_gaps", []), claim_ids, issues)
    semantic_gaps = ir.get("semantic_gaps", [])
    if not ir.get("semantic_elements") and not semantic_gaps and not ir.get("open_questions"):
        issues.append(
            SemanticSpecIRIssue(
                "semantic_gaps",
                "must explain why no semantic elements were generated",
            )
        )
    return issues


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


def validate_spec_claims(
    value: Any,
    source_ids: set[str],
    spec_paths: Iterable[str | Path],
    issues: list[SemanticSpecIRIssue],
) -> set[str]:
    claim_ids: set[str] = set()
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue("spec_claims", "must be a list"))
        return claim_ids
    source_lines = load_source_lines_by_id(spec_paths)
    for index, item in enumerate(value):
        path = f"spec_claims[{index}]"
        if not isinstance(item, dict):
            issues.append(SemanticSpecIRIssue(path, "must be an object"))
            continue
        claim_id = item.get("id")
        if not isinstance(claim_id, str) or not claim_id:
            issues.append(SemanticSpecIRIssue(f"{path}.id", "must be a non-empty string"))
            continue
        if claim_id in claim_ids:
            issues.append(SemanticSpecIRIssue(f"{path}.id", f"duplicate claim id {claim_id!r}"))
        claim_ids.add(claim_id)
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
        if not isinstance(item.get("summary"), str) or not item.get("summary"):
            issues.append(SemanticSpecIRIssue(f"{path}.summary", "must be a non-empty string"))
        if item.get("kind") not in ALLOWED_CLAIM_KINDS:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.kind",
                    f"must be one of {sorted(ALLOWED_CLAIM_KINDS)}, got {item.get('kind')!r}",
                )
            )
        if item.get("strength") not in ALLOWED_CLAIM_STRENGTHS:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.strength",
                    f"must be one of {sorted(ALLOWED_CLAIM_STRENGTHS)}, got {item.get('strength')!r}",
                )
            )
        if not isinstance(item.get("normative"), bool):
            issues.append(SemanticSpecIRIssue(f"{path}.normative", "must be a boolean"))
        validate_string_list(item.get("subjects", []), f"{path}.subjects", issues)
    return claim_ids


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


def validate_semantic_elements(
    value: Any,
    evidence_ids: set[str],
    claim_ids: set[str],
    manifest_fields: set[str],
    issues: list[SemanticSpecIRIssue],
) -> set[str]:
    element_ids: set[str] = set()
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue("semantic_elements", "must be a list"))
        return element_ids
    for index, item in enumerate(value):
        path = f"semantic_elements[{index}]"
        if not isinstance(item, dict):
            issues.append(SemanticSpecIRIssue(path, "must be an object"))
            continue
        element_id = item.get("id")
        if not isinstance(element_id, str) or not element_id:
            issues.append(SemanticSpecIRIssue(f"{path}.id", "must be a non-empty string"))
            continue
        if element_id in element_ids:
            issues.append(SemanticSpecIRIssue(f"{path}.id", f"duplicate semantic element id {element_id!r}"))
        element_ids.add(element_id)
        if item.get("kind") not in ALLOWED_SEMANTIC_ELEMENT_KINDS:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.kind",
                    f"must be one of {sorted(ALLOWED_SEMANTIC_ELEMENT_KINDS)}, got {item.get('kind')!r}",
                )
            )
        if not isinstance(item.get("summary"), str) or not item.get("summary"):
            issues.append(SemanticSpecIRIssue(f"{path}.summary", "must be a non-empty string"))
        status = item.get("formalization_status")
        if status not in ALLOWED_FORMALIZATION_STATUSES:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.formalization_status",
                    f"must be one of {sorted(ALLOWED_FORMALIZATION_STATUSES)}, got {status!r}",
                )
            )
        confidence = item.get("confidence")
        if not isinstance(confidence, int | float) or not 0 <= float(confidence) <= 1:
            issues.append(SemanticSpecIRIssue(f"{path}.confidence", "must be a number from 0 to 1"))
        validate_string_list(item.get("subjects", []), f"{path}.subjects", issues)
        validate_item_evidence(item.get("evidence"), f"{path}.evidence", evidence_ids, issues)
        validate_claim_ids(item.get("claim_ids"), f"{path}.claim_ids", claim_ids, issues)
        representation = item.get("representation")
        if not isinstance(representation, dict):
            issues.append(SemanticSpecIRIssue(f"{path}.representation", "must be an object"))
        else:
            validate_representation(representation, f"{path}.representation", manifest_fields, issues)
            if (
                status not in BLOCKING_FORMALIZATION_STATUSES
                and representation_requires_human_review(representation)
            ):
                issues.append(
                    SemanticSpecIRIssue(
                        f"{path}.formalization_status",
                        "must be a blocking status when representation.ast is a placeholder",
                    )
                )
    return element_ids


def validate_open_questions(
    value: Any,
    item_ids: set[str],
    claim_ids: set[str],
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
                            f"unknown semantic element id {related_id!r}",
                        )
                    )
        if "claim_ids" in item:
            validate_claim_ids(item.get("claim_ids"), f"{path}.claim_ids", claim_ids, issues)


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


def validate_claim_ids(
    value: Any,
    path: str,
    claim_ids: set[str],
    issues: list[SemanticSpecIRIssue],
    *,
    required: bool = True,
) -> None:
    if value is None and not required:
        return
    if not claim_ids and value is None:
        return
    if not isinstance(value, list) or (required and not value):
        issues.append(SemanticSpecIRIssue(path, "must contain at least one spec claim id"))
        return
    for index, claim_id in enumerate(value):
        if not isinstance(claim_id, str):
            issues.append(SemanticSpecIRIssue(f"{path}[{index}]", "must be a string"))
        elif claim_id not in claim_ids:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}[{index}]",
                    f"unknown spec claim id {claim_id!r}",
                )
            )


def validate_semantic_gaps(
    value: Any,
    claim_ids: set[str],
    issues: list[SemanticSpecIRIssue],
) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        issues.append(SemanticSpecIRIssue("semantic_gaps", "must be a list"))
        return
    for index, item in enumerate(value):
        path = f"semantic_gaps[{index}]"
        if not isinstance(item, dict):
            issues.append(SemanticSpecIRIssue(path, "must be an object"))
            continue
        if "id" in item and (not isinstance(item["id"], str) or not item["id"]):
            issues.append(SemanticSpecIRIssue(f"{path}.id", "must be a non-empty string"))
        if item.get("kind") not in ALLOWED_SEMANTIC_GAP_KINDS:
            issues.append(
                SemanticSpecIRIssue(
                    f"{path}.kind",
                    f"must be one of {sorted(ALLOWED_SEMANTIC_GAP_KINDS)}, got {item.get('kind')!r}",
                )
            )
        if "reason" in item and not isinstance(item["reason"], str):
            issues.append(SemanticSpecIRIssue(f"{path}.reason", "must be a string"))
        if "resolution" in item and not isinstance(item["resolution"], str):
            issues.append(SemanticSpecIRIssue(f"{path}.resolution", "must be a string"))
        if "claim_ids" in item:
            validate_claim_ids(
                item.get("claim_ids"),
                f"{path}.claim_ids",
                claim_ids,
                issues,
                required=False,
            )


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
