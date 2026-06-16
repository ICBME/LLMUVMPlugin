"""Structured validation review for SemanticSpecIR documents."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

from rtlagent_bfm.codegen.oracle_ir import load_manifest_summary
from rtlagent_bfm.loader import load_ir

from .claim_extraction import gap_requires_human_input
from .representation_ast import (
    collect_representation_completeness_issues,
    iter_ast_field_refs,
)
from .semantic_ir import (
    BLOCKING_FORMALIZATION_STATUSES,
    SemanticSpecIRIssue,
    collect_semantic_spec_ir_issues,
    extract_spec_claims,
    load_source_documents,
    load_semantic_spec_ir,
    sha256_text,
)
from .semantic_obligation_coverage import collect_obligation_coverage


REVIEW_SCHEMA_VERSION = 1
REVIEW_STAGES = (
    "schema_review",
    "traceability_review",
    "completeness_review",
    "semantic_consistency_review",
    "human_review_gate",
)
ClaimKey = tuple[str, int, int, str, str]


@dataclass(frozen=True)
class ReviewFinding:
    stage: str
    severity: str
    path: str
    message: str
    blocking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
            "blocking": self.blocking,
        }

    def format(self) -> str:
        marker = " blocking" if self.blocking else ""
        return f"{self.stage}:{self.severity}{marker}: {self.path}: {self.message}"


def review_semantic_spec_ir(
    ir: dict[str, Any],
    *,
    manifest_path: str | Path | None = None,
    spec_paths: Iterable[str | Path] = (),
    design_ir_path: str | Path | None = None,
    target: str | None = None,
    require_reviewed: bool = False,
) -> dict[str, Any]:
    """Build a structured SemanticSpecIR validation review report."""

    spec_path_tuple = tuple(Path(path) for path in spec_paths)
    findings: list[ReviewFinding] = []
    expected_claims: list[dict[str, Any]] | None = None
    expected_claims_error = ""
    if spec_path_tuple:
        try:
            expected_claims = extract_spec_claims(load_source_documents(spec_path_tuple))
        except Exception as exc:  # noqa: BLE001 - keep review report structured
            expected_claims = []
            expected_claims_error = str(exc)
    findings.extend(
        schema_review(
            ir,
            manifest_path=manifest_path,
            target=target,
        )
    )
    findings.extend(traceability_review(ir, spec_paths=spec_path_tuple))
    completeness = completeness_review(
        ir,
        expected_claims=expected_claims,
        expected_claims_error=expected_claims_error,
    )
    findings.extend(completeness["findings"])
    findings.extend(
        semantic_consistency_review(
            ir,
            manifest_path=manifest_path,
            design_ir_path=design_ir_path,
        )
    )
    findings.extend(human_review_gate(ir, require_reviewed=require_reviewed))

    finding_dicts = [finding.to_dict() for finding in findings]
    stage_summaries = [
        stage_summary(stage, findings)
        for stage in REVIEW_STAGES
    ]
    status = review_status(findings)
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "target": str(target or ir.get("target") or ""),
        "status": status,
        "stage_summaries": stage_summaries,
        "findings": finding_dicts,
        "completeness": {
            "normative_claims": completeness["normative_claims"],
            "covered_claims": completeness["covered_claims"],
            "trace_covered_claims": completeness["trace_covered_claims"],
            "partial_claims": completeness["partial_claims"],
            "uncovered_claims": completeness["uncovered_claims"],
            "placeholder_only_claims": completeness["placeholder_only_claims"],
            "claim_obligations": completeness["claim_obligations"],
            "obligation_coverage": completeness["obligation_coverage"],
        },
        "semantic_gaps": {
            "count": len(ir.get("semantic_gaps", [])) if isinstance(ir.get("semantic_gaps"), list) else 0,
        },
    }


def review_semantic_spec_ir_file(
    path: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    return review_semantic_spec_ir(load_semantic_spec_ir(path), **kwargs)


def write_semantic_review(path: str | Path, review: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def schema_review(
    ir: dict[str, Any],
    *,
    manifest_path: str | Path | None,
    target: str | None,
) -> list[ReviewFinding]:
    issues = collect_semantic_spec_ir_issues(
        ir,
        manifest_path=manifest_path,
        spec_paths=(),
        target=target,
        require_reviewed=False,
    )
    return [
        finding_from_issue("schema_review", issue, severity="error")
        for issue in issues
    ]


def traceability_review(
    ir: dict[str, Any],
    *,
    spec_paths: tuple[Path, ...],
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    if not spec_paths:
        return [
            ReviewFinding(
                stage="traceability_review",
                severity="error",
                path="spec_paths",
                message="spec paths are required for trusted traceability and completeness review",
                blocking=True,
            )
        ]

    expected_by_path = {
        str(path): path
        for path in spec_paths
    }
    source_by_id = {
        str(source.get("id")): source
        for source in ir.get("sources", [])
        if isinstance(source, dict)
    }
    for index, source in enumerate(ir.get("sources", [])):
        path = f"sources[{index}]"
        if not isinstance(source, dict):
            continue
        source_path = source.get("path")
        if not isinstance(source_path, str) or source_path not in expected_by_path:
            findings.append(
                ReviewFinding(
                    stage="traceability_review",
                    severity="error",
                    path=f"{path}.path",
                    message="source path is not one of the review spec paths",
                    blocking=True,
                )
            )
            continue
        spec_path = expected_by_path[source_path]
        if not spec_path.exists():
            findings.append(
                ReviewFinding(
                    stage="traceability_review",
                    severity="error",
                    path=f"{path}.path",
                    message=f"source file does not exist: {spec_path}",
                    blocking=True,
                )
            )
            continue
        text = spec_path.read_text(encoding="utf-8", errors="replace")
        expected_hash = sha256_text(text)
        if source.get("content_hash") != expected_hash:
            findings.append(
                ReviewFinding(
                    stage="traceability_review",
                    severity="error",
                    path=f"{path}.content_hash",
                    message=f"does not match source file hash {expected_hash}",
                    blocking=True,
                )
            )

    lines_by_source = {
        source_id: expected_by_path[str(source["path"])].read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        for source_id, source in source_by_id.items()
        if isinstance(source.get("path"), str)
        and source["path"] in expected_by_path
        and expected_by_path[source["path"]].exists()
    }
    evidence_ids = set()
    for index, evidence in enumerate(ir.get("evidence", [])):
        path = f"evidence[{index}]"
        if not isinstance(evidence, dict):
            continue
        evidence_id = evidence.get("id")
        if isinstance(evidence_id, str):
            evidence_ids.add(evidence_id)
        source_id = evidence.get("source_id")
        line_start = evidence.get("line_start")
        line_end = evidence.get("line_end")
        quote = evidence.get("quote")
        if (
            not isinstance(source_id, str)
            or source_id not in lines_by_source
            or not isinstance(line_start, int)
            or not isinstance(line_end, int)
            or not isinstance(quote, str)
            or not quote.strip()
        ):
            continue
        line_text = "\n".join(lines_by_source[source_id][line_start - 1 : line_end])
        if quote.strip() not in line_text:
            findings.append(
                ReviewFinding(
                    stage="traceability_review",
                    severity="error",
                    path=f"{path}.quote",
                    message="quote does not appear in referenced source line range",
                    blocking=True,
                )
            )

    for index, claim in enumerate(ir.get("spec_claims", [])):
        path = f"spec_claims[{index}]"
        if not isinstance(claim, dict):
            continue
        source_id = claim.get("source_id")
        line_start = claim.get("line_start")
        line_end = claim.get("line_end")
        quote = claim.get("quote")
        if (
            not isinstance(source_id, str)
            or source_id not in lines_by_source
            or not isinstance(line_start, int)
            or not isinstance(line_end, int)
            or not isinstance(quote, str)
            or not quote.strip()
        ):
            continue
        line_text = "\n".join(lines_by_source[source_id][line_start - 1 : line_end])
        if quote.strip() not in line_text:
            findings.append(
                ReviewFinding(
                    stage="traceability_review",
                    severity="error",
                    path=f"{path}.quote",
                    message="quote does not appear in referenced source line range",
                    blocking=True,
                )
            )

    for item_index, item in enumerate(ir.get("semantic_elements", [])):
        if not isinstance(item, dict):
            continue
        item_evidence = item.get("evidence", [])
        if not isinstance(item_evidence, list) or not item_evidence:
            findings.append(
                ReviewFinding(
                    stage="traceability_review",
                    severity="error",
                    path=f"semantic_elements[{item_index}].evidence",
                    message="semantic element has no evidence",
                    blocking=True,
                )
            )
            continue
        for evidence_index, evidence_id in enumerate(item_evidence):
            if isinstance(evidence_id, str) and evidence_id not in evidence_ids:
                findings.append(
                    ReviewFinding(
                        stage="traceability_review",
                        severity="error",
                        path=f"semantic_elements[{item_index}].evidence[{evidence_index}]",
                        message=f"unknown evidence id {evidence_id!r}",
                        blocking=True,
                    )
                )
    return findings


def completeness_review(
    ir: dict[str, Any],
    *,
    expected_claims: list[dict[str, Any]] | None,
    expected_claims_error: str = "",
) -> dict[str, Any]:
    findings: list[ReviewFinding] = []
    if expected_claims is None:
        findings.append(
            ReviewFinding(
                stage="completeness_review",
                severity="error",
                path="spec_paths",
                message="cannot verify completeness without source spec paths",
                blocking=True,
            )
        )
        return {
            "normative_claims": [],
            "covered_claims": [],
            "trace_covered_claims": [],
            "partial_claims": [],
            "uncovered_claims": [],
            "placeholder_only_claims": [],
            "claim_obligations": [],
            "obligation_coverage": {"summary": {}, "claims": []},
            "findings": findings,
        }
    if expected_claims_error:
        findings.append(
            ReviewFinding(
                stage="completeness_review",
                severity="error",
                path="spec_paths",
                message=f"could not extract expected spec claims: {expected_claims_error}",
                blocking=True,
            )
        )

    source_claims = [
        claim
        for claim in expected_claims
        if isinstance(claim, dict) and claim.get("normative", True)
    ]
    expected_claim_ids = [
        str(claim.get("id"))
        for claim in source_claims
        if isinstance(claim.get("id"), str)
    ]
    expected_by_key = {
        claim_key(claim): str(claim.get("id"))
        for claim in source_claims
        if isinstance(claim.get("id"), str)
    }
    ir_claim_id_to_key = {
        str(claim.get("id")): claim_key(claim)
        for claim in ir.get("spec_claims", [])
        if isinstance(claim, dict)
        and isinstance(claim.get("id"), str)
        and claim.get("normative", True)
    }
    covered_by_semantic = collect_expected_claim_coverage(
        ir.get("semantic_elements", []),
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
        semantic_complete_only=True,
    )
    covered_by_incomplete_semantic = collect_expected_claim_coverage(
        ir.get("semantic_elements", []),
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
        semantic_incomplete_only=True,
    )
    covered_by_questions = collect_expected_claim_coverage(
        ir.get("open_questions", []),
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    covered_by_gaps = collect_expected_claim_coverage(
        ir.get("semantic_gaps", []),
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    semantic_claim_obligations, semantic_issue_findings = collect_semantic_claim_obligations(
        ir.get("semantic_elements", []),
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    question_claim_obligations, question_issue_findings = collect_question_claim_obligations(
        ir.get("open_questions", []),
        review=ir.get("review", {}),
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    gap_claim_obligations, gap_issue_findings = collect_gap_claim_obligations(
        ir.get("semantic_gaps", []),
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    obligation_coverage = collect_obligation_coverage(
        source_claims,
        semantic_elements=ir.get("semantic_elements", []),
        open_questions=ir.get("open_questions", []),
        semantic_gaps=ir.get("semantic_gaps", []),
        review=ir.get("review", {}),
        ir_claim_id_to_key=ir_claim_id_to_key,
        expected_by_key=expected_by_key,
    )
    obligation_coverage_missing = {
        claim_id: list(missing)
        for claim_id, missing in obligation_coverage.get("missing_by_claim", {}).items()
        if isinstance(claim_id, str) and isinstance(missing, list)
    }
    findings.extend(semantic_issue_findings)
    findings.extend(question_issue_findings)
    findings.extend(gap_issue_findings)
    findings.extend(obligation_coverage_findings(obligation_coverage_missing))
    trace_covered_claims = sorted(
        set(expected_claim_ids).intersection(
            covered_by_semantic | covered_by_questions | covered_by_gaps
        )
    )
    covered_claims: list[str] = []
    uncovered_claims: list[str] = []
    placeholder_only_claims: list[str] = []
    partial_claims: list[str] = []
    expected_claims_by_id = {
        str(claim.get("id")): claim
        for claim in source_claims
        if isinstance(claim.get("id"), str)
    }
    claim_obligations: list[dict[str, Any]] = []

    for claim_id in expected_claim_ids:
        has_semantic = claim_id in covered_by_semantic
        has_placeholder = (
            claim_id in covered_by_incomplete_semantic
            or claim_id in covered_by_questions
            or claim_id in covered_by_gaps
        )
        claim_missing_obligations = (
            semantic_claim_obligations.get(claim_id, [])
            + question_claim_obligations.get(claim_id, [])
            + gap_claim_obligations.get(claim_id, [])
            + obligation_coverage_missing.get(claim_id, [])
        )
        claim = expected_claims_by_id.get(claim_id, {})
        path = source_claim_path(claim, fallback_id=claim_id)
        status = "partial" if claim_missing_obligations else "complete"
        if claim_missing_obligations:
            partial_claims.append(claim_id)
        if has_semantic and not claim_missing_obligations:
            covered_claims.append(claim_id)
        if not has_semantic and not has_placeholder:
            status = "uncovered"
            uncovered_claims.append(claim_id)
            findings.append(
                ReviewFinding(
                    stage="completeness_review",
                    severity="error",
                    path=path,
                    message=f"source-derived normative spec claim {claim_id!r} is not covered by semantic_elements, open_questions, or semantic_gaps",
                    blocking=True,
                )
            )
        elif not has_semantic and has_placeholder:
            status = "partial"
            placeholder_only_claims.append(claim_id)
            if claim_id not in partial_claims:
                partial_claims.append(claim_id)
            findings.append(
                ReviewFinding(
                    stage="completeness_review",
                    severity="warning",
                    path=path,
                    message=f"source-derived normative spec claim {claim_id!r} is only covered by incomplete semantic_elements, open_questions, or semantic_gaps",
                    blocking=True,
                )
            )
        claim_obligations.append(
            {
                "claim_id": claim_id,
                "status": status,
                "missing_obligations": claim_missing_obligations,
            }
        )

    return {
        "normative_claims": expected_claim_ids,
        "covered_claims": sorted(covered_claims),
        "trace_covered_claims": trace_covered_claims,
        "partial_claims": sorted(set(partial_claims)),
        "uncovered_claims": uncovered_claims,
        "placeholder_only_claims": placeholder_only_claims,
        "claim_obligations": claim_obligations,
        "obligation_coverage": {
            "summary": obligation_coverage.get("summary", {}),
            "claims": obligation_coverage.get("claims", []),
        },
        "findings": findings,
    }


def obligation_coverage_findings(
    missing_by_claim: dict[str, list[dict[str, str]]],
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    for claim_id, issues in missing_by_claim.items():
        for issue in issues:
            findings.append(
                ReviewFinding(
                    stage="completeness_review",
                    severity="warning",
                    path=issue.get("path", f"spec_claims[{claim_id}]"),
                    message=issue.get("message", "claim obligation is not covered by typed RepresentationAST"),
                    blocking=True,
                )
            )
    return findings


def collect_semantic_claim_obligations(
    value: Any,
    *,
    ir_claim_id_to_key: dict[str, ClaimKey],
    expected_by_key: dict[ClaimKey, str],
) -> tuple[dict[str, list[dict[str, str]]], list[ReviewFinding]]:
    obligations: dict[str, list[dict[str, str]]] = {}
    findings: list[ReviewFinding] = []
    if not isinstance(value, list):
        return obligations, findings
    for item_index, item in enumerate(value):
        if not isinstance(item, dict):
            continue
        item_issues = semantic_element_formalization_issues(item)
        if not item_issues:
            continue
        expected_claim_ids = expected_claim_ids_for_item(
            item,
            ir_claim_id_to_key=ir_claim_id_to_key,
            expected_by_key=expected_by_key,
        )
        for expected_claim_id in expected_claim_ids:
            claim_obligations = obligations.setdefault(expected_claim_id, [])
            for issue in item_issues:
                claim_obligations.append(
                    {
                        "semantic_element_id": str(item.get("id") or f"semantic_elements[{item_index}]"),
                        **issue,
                    }
                )
        for issue in item_issues:
            issue_path = issue.get("path") or "representation"
            findings.append(
                ReviewFinding(
                    stage="completeness_review",
                    severity="warning",
                    path=f"semantic_elements[{item_index}].{issue_path}",
                    message=issue.get("message", "semantic element is not completely formalized"),
                    blocking=True,
                )
            )
    return obligations, findings


def collect_question_claim_obligations(
    value: Any,
    *,
    review: Any,
    ir_claim_id_to_key: dict[str, ClaimKey],
    expected_by_key: dict[ClaimKey, str],
) -> tuple[dict[str, list[dict[str, str]]], list[ReviewFinding]]:
    obligations: dict[str, list[dict[str, str]]] = {}
    findings: list[ReviewFinding] = []
    if not isinstance(value, list):
        return obligations, findings
    answered_question_ids = collect_answered_question_ids(review)
    for question_index, question in enumerate(value):
        if not isinstance(question, dict):
            continue
        question_id = str(question.get("id") or f"open_questions[{question_index}]")
        status = str(question.get("status", "open"))
        is_answered = question_id in answered_question_ids
        if (
            not bool(question.get("blocking"))
            or status in {"resolved", "closed", "accepted"}
            or is_answered
        ):
            continue
        path = f"open_questions[{question_index}]"
        issue = {
            "path": path,
            "code": "blocking_open_question",
            "message": "blocking open question must be answered or resolved before this claim is complete",
        }
        for expected_claim_id in expected_claim_ids_for_item(
            question,
            ir_claim_id_to_key=ir_claim_id_to_key,
            expected_by_key=expected_by_key,
        ):
            obligations.setdefault(expected_claim_id, []).append(
                {
                    "open_question_id": question_id,
                    **issue,
                }
            )
        findings.append(
            ReviewFinding(
                stage="completeness_review",
                severity="warning",
                path=path,
                message=issue["message"],
                blocking=True,
            )
        )
    return obligations, findings


def collect_gap_claim_obligations(
    value: Any,
    *,
    ir_claim_id_to_key: dict[str, ClaimKey],
    expected_by_key: dict[ClaimKey, str],
) -> tuple[dict[str, list[dict[str, str]]], list[ReviewFinding]]:
    obligations: dict[str, list[dict[str, str]]] = {}
    findings: list[ReviewFinding] = []
    if not isinstance(value, list):
        return obligations, findings
    for gap_index, gap in enumerate(value):
        if not isinstance(gap, dict) or not gap_requires_human_input(gap):
            continue
        path = f"semantic_gaps[{gap_index}]"
        gap_id = str(gap.get("id") or path)
        gap_kind = str(gap.get("kind") or "unknown")
        issue = {
            "path": path,
            "code": "semantic_gap_requires_resolution",
            "message": f"semantic gap {gap_id!r} ({gap_kind}) must be resolved before this claim is complete",
        }
        for expected_claim_id in expected_claim_ids_for_item(
            gap,
            ir_claim_id_to_key=ir_claim_id_to_key,
            expected_by_key=expected_by_key,
        ):
            obligations.setdefault(expected_claim_id, []).append(
                {
                    "semantic_gap_id": gap_id,
                    "gap_kind": gap_kind,
                    **issue,
                }
            )
        findings.append(
            ReviewFinding(
                stage="completeness_review",
                severity="warning",
                path=path,
                message=issue["message"],
                blocking=True,
            )
        )
    return obligations, findings


def collect_answered_question_ids(review: Any) -> set[str]:
    answered_question_ids: set[str] = set()
    if not isinstance(review, dict):
        return answered_question_ids
    human_answers = review.get("human_answers", [])
    if not isinstance(human_answers, list):
        return answered_question_ids
    for answer in human_answers:
        if isinstance(answer, dict) and isinstance(answer.get("question_id"), str):
            answered_question_ids.add(answer["question_id"])
    return answered_question_ids


def expected_claim_ids_for_item(
    item: dict[str, Any],
    *,
    ir_claim_id_to_key: dict[str, ClaimKey],
    expected_by_key: dict[ClaimKey, str],
) -> list[str]:
    expected_ids: list[str] = []
    for claim_id in item.get("claim_ids", []):
        if not isinstance(claim_id, str):
            continue
        key = ir_claim_id_to_key.get(claim_id)
        if key is None:
            continue
        expected_claim_id = expected_by_key.get(key)
        if expected_claim_id is not None and expected_claim_id not in expected_ids:
            expected_ids.append(expected_claim_id)
    return expected_ids


def claim_key(claim: dict[str, Any]) -> ClaimKey:
    return (
        str(claim.get("source_id") or ""),
        safe_int(claim.get("line_start")),
        safe_int(claim.get("line_end")),
        str(claim.get("quote") or "").strip(),
        str(claim.get("summary") or "").strip(),
    )


def safe_int(value: Any) -> int:
    return value if isinstance(value, int) else 0


def source_claim_path(claim: dict[str, Any], *, fallback_id: str) -> str:
    source_id = claim.get("source_id")
    line_start = claim.get("line_start")
    line_end = claim.get("line_end")
    if isinstance(source_id, str) and isinstance(line_start, int) and isinstance(line_end, int):
        return f"source_claims[{source_id}:{line_start}-{line_end}]"
    return f"source_claims[{fallback_id}]"


def collect_expected_claim_coverage(
    value: Any,
    *,
    ir_claim_id_to_key: dict[str, ClaimKey],
    expected_by_key: dict[ClaimKey, str],
    semantic_complete_only: bool = False,
    semantic_incomplete_only: bool = False,
) -> set[str]:
    covered: set[str] = set()
    if not isinstance(value, list):
        return covered
    for item in value:
        if not isinstance(item, dict):
            continue
        if semantic_complete_only and not semantic_element_has_complete_formalization(item):
            continue
        if semantic_incomplete_only and semantic_element_has_complete_formalization(item):
            continue
        for claim_id in item.get("claim_ids", []):
            if not isinstance(claim_id, str):
                continue
            key = ir_claim_id_to_key.get(claim_id)
            if key is None:
                continue
            expected_claim_id = expected_by_key.get(key)
            if expected_claim_id is not None:
                covered.add(expected_claim_id)
    return covered


def semantic_element_has_complete_formalization(item: dict[str, Any]) -> bool:
    return not semantic_element_formalization_issues(item)


def semantic_element_formalization_issues(item: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    status = item.get("formalization_status")
    if status in BLOCKING_FORMALIZATION_STATUSES:
        issues.append(
            {
                "path": "formalization_status",
                "code": "blocking_formalization_status",
                "message": f"formalization_status {status!r} requires human review before this claim is complete",
            }
        )
    issues.extend(
        collect_representation_completeness_issues(
            item.get("representation"),
            path="representation",
        )
    )
    return issues


def semantic_consistency_review(
    ir: dict[str, Any],
    *,
    manifest_path: str | Path | None,
    design_ir_path: str | Path | None,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    manifest_fields: set[str] = set()
    if manifest_path is not None:
        try:
            manifest = load_manifest_summary(manifest_path)
            manifest_fields = {field.name for field in manifest.fields}
        except Exception as exc:  # noqa: BLE001 - keep review report best-effort
            findings.append(
                ReviewFinding(
                    stage="semantic_consistency_review",
                    severity="error",
                    path="manifest",
                    message=str(exc),
                    blocking=True,
                )
            )
    if design_ir_path is not None:
        try:
            load_ir(design_ir_path)
        except Exception as exc:  # noqa: BLE001 - keep review report best-effort
            findings.append(
                ReviewFinding(
                    stage="semantic_consistency_review",
                    severity="error",
                    path="design_ir",
                    message=str(exc),
                    blocking=True,
                )
            )

    if manifest_fields:
        for item_index, item in enumerate(ir.get("semantic_elements", [])):
            if not isinstance(item, dict):
                continue
            for field_path, field in iter_ast_field_refs(item.get("representation")):
                if field not in manifest_fields:
                    findings.append(
                        ReviewFinding(
                            stage="semantic_consistency_review",
                            severity="error",
                            path=f"semantic_elements[{item_index}].representation{field_path}",
                            message=f"representation references unknown manifest field {field!r}",
                            blocking=True,
                        )
                    )

    item_ids = {
        str(item.get("id"))
        for item in ir.get("semantic_elements", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    reviewed_items = ir.get("review", {}).get("reviewed_items", [])
    if isinstance(reviewed_items, list):
        for index, item_id in enumerate(reviewed_items):
            if isinstance(item_id, str) and item_id not in item_ids:
                findings.append(
                    ReviewFinding(
                        stage="semantic_consistency_review",
                        severity="warning",
                        path=f"review.reviewed_items[{index}]",
                        message=f"review references unknown semantic element id {item_id!r}",
                    )
                )
    return findings


def human_review_gate(
    ir: dict[str, Any],
    *,
    require_reviewed: bool,
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    review = ir.get("review", {})
    review_status_value = review.get("status") if isinstance(review, dict) else None
    if require_reviewed and review_status_value != "accepted":
        findings.append(
            ReviewFinding(
                stage="human_review_gate",
                severity="error",
                path="review.status",
                message="review.status must be accepted before this gate",
                blocking=True,
            )
        )

    answered_question_ids = collect_answered_question_ids(review)

    for index, question in enumerate(ir.get("open_questions", [])):
        if not isinstance(question, dict):
            continue
        question_id = question.get("id")
        is_blocking = bool(question.get("blocking"))
        status = str(question.get("status", "open"))
        answered = isinstance(question_id, str) and question_id in answered_question_ids
        if is_blocking and status not in {"resolved", "closed", "accepted"} and not answered:
            findings.append(
                ReviewFinding(
                    stage="human_review_gate",
                    severity="warning",
                    path=f"open_questions[{index}]",
                    message="blocking open question requires a human answer before acceptance",
                    blocking=True,
                )
            )
    return findings


def review_status(findings: list[ReviewFinding]) -> str:
    if any(finding.severity == "error" for finding in findings):
        return "failed"
    if any(finding.blocking for finding in findings):
        return "needs_human_input"
    return "passed"


def stage_summary(stage: str, findings: list[ReviewFinding]) -> dict[str, Any]:
    stage_findings = [finding for finding in findings if finding.stage == stage]
    error_count = sum(finding.severity == "error" for finding in stage_findings)
    warning_count = sum(finding.severity == "warning" for finding in stage_findings)
    info_count = sum(finding.severity == "info" for finding in stage_findings)
    status = "failed" if error_count else "passed"
    if not error_count and any(finding.blocking for finding in stage_findings):
        status = "needs_human_input"
    return {
        "stage": stage,
        "status": status,
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
    }


def finding_from_issue(
    stage: str,
    issue: SemanticSpecIRIssue,
    *,
    severity: str,
) -> ReviewFinding:
    return ReviewFinding(
        stage=stage,
        severity=severity,
        path=issue.path,
        message=issue.message,
        blocking=severity == "error",
    )
