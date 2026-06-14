"""Structured validation review for SemanticSpecIR documents."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable

from rtlagent_bfm.codegen.oracle_ir import ALLOWED_CALLS, load_manifest_summary
from rtlagent_bfm.loader import load_ir

from .semantic_ir import (
    SemanticSpecIRIssue,
    collect_semantic_spec_ir_issues,
    load_semantic_spec_ir,
    sha256_text,
)


REVIEW_SCHEMA_VERSION = 1
REVIEW_STAGES = (
    "schema_review",
    "traceability_review",
    "semantic_consistency_review",
    "lowering_readiness_review",
    "human_review_gate",
)


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
    findings.extend(
        schema_review(
            ir,
            manifest_path=manifest_path,
            target=target,
        )
    )
    findings.extend(traceability_review(ir, spec_paths=spec_path_tuple))
    findings.extend(
        semantic_consistency_review(
            ir,
            manifest_path=manifest_path,
            design_ir_path=design_ir_path,
        )
    )
    lowering = lowering_readiness_review(ir)
    findings.extend(lowering["findings"])
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
        "lowering": {
            "ready_items": lowering["ready_items"],
            "blocked_items": lowering["blocked_items"],
            "unsupported_items": lowering["unsupported_items"],
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
                severity="warning",
                path="spec_paths",
                message="spec paths not provided; source hash and quote checks were skipped",
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

    for item_index, item in enumerate(ir.get("semantic_items", [])):
        if not isinstance(item, dict):
            continue
        item_evidence = item.get("evidence", [])
        if not isinstance(item_evidence, list) or not item_evidence:
            findings.append(
                ReviewFinding(
                    stage="traceability_review",
                    severity="error",
                    path=f"semantic_items[{item_index}].evidence",
                    message="semantic item has no evidence",
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
                        path=f"semantic_items[{item_index}].evidence[{evidence_index}]",
                        message=f"unknown evidence id {evidence_id!r}",
                        blocking=True,
                    )
                )
    return findings


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
        for item_index, item in enumerate(ir.get("semantic_items", [])):
            if not isinstance(item, dict):
                continue
            for condition_index, condition in enumerate(item.get("conditions", [])):
                if not isinstance(condition, dict):
                    continue
                field = condition.get("field")
                if isinstance(field, str) and field not in manifest_fields:
                    findings.append(
                        ReviewFinding(
                            stage="semantic_consistency_review",
                            severity="error",
                            path=f"semantic_items[{item_index}].conditions[{condition_index}].field",
                            message=f"condition references unknown manifest field {field!r}",
                            blocking=True,
                        )
                    )
            for effect_index, effect in enumerate(item.get("effects", [])):
                if not isinstance(effect, dict):
                    continue
                for field_path, field in iter_expr_fields(effect.get("expr")):
                    if field not in manifest_fields:
                        findings.append(
                            ReviewFinding(
                                stage="semantic_consistency_review",
                                severity="error",
                                path=f"semantic_items[{item_index}].effects[{effect_index}].expr{field_path}",
                                message=f"expression references unknown manifest field {field!r}",
                                blocking=True,
                            )
                        )

    item_ids = {
        str(item.get("id"))
        for item in ir.get("semantic_items", [])
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
                        message=f"review references unknown semantic item id {item_id!r}",
                    )
                )
    return findings


def lowering_readiness_review(ir: dict[str, Any]) -> dict[str, Any]:
    ready_items: list[str] = []
    blocked_items: list[str] = []
    unsupported_items: list[str] = []
    findings: list[ReviewFinding] = []
    for index, item in enumerate(ir.get("semantic_items", [])):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or f"semantic_items[{index}]")
        item_path = f"semantic_items[{index}]"
        effects = item.get("effects", [])
        if not isinstance(effects, list) or not effects:
            unsupported_items.append(item_id)
            findings.append(
                ReviewFinding(
                    stage="lowering_readiness_review",
                    severity="warning",
                    path=f"{item_path}.effects",
                    message="item has no effects and cannot be lowered to OracleIR",
                    blocking=True,
                )
            )
            continue

        item_blocked = False
        item_unsupported = False
        for effect_index, effect in enumerate(effects):
            if not isinstance(effect, dict):
                item_unsupported = True
                continue
            if effect.get("kind") != "compute_expected":
                item_unsupported = True
                findings.append(
                    ReviewFinding(
                        stage="lowering_readiness_review",
                        severity="warning",
                        path=f"{item_path}.effects[{effect_index}].kind",
                        message="only compute_expected effects are lowerable to current OracleIR",
                        blocking=True,
                    )
                )
            calls = list(iter_expr_calls(effect.get("expr")))
            for call_path, call_name in calls:
                if call_name not in ALLOWED_CALLS:
                    item_blocked = True
                    findings.append(
                        ReviewFinding(
                            stage="lowering_readiness_review",
                            severity="error",
                            path=f"{item_path}.effects[{effect_index}].expr{call_path}",
                            message=f"call {call_name!r} is not in OracleIR allowlist",
                            blocking=True,
                        )
                    )
            if not calls and not effect.get("expr"):
                item_unsupported = True
                findings.append(
                    ReviewFinding(
                        stage="lowering_readiness_review",
                        severity="warning",
                        path=f"{item_path}.effects[{effect_index}].expr",
                        message="effect has no expression to lower",
                        blocking=True,
                    )
                )

        if item_blocked:
            blocked_items.append(item_id)
        elif item_unsupported:
            unsupported_items.append(item_id)
        else:
            ready_items.append(item_id)
    return {
        "ready_items": ready_items,
        "blocked_items": blocked_items,
        "unsupported_items": unsupported_items,
        "findings": findings,
    }


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

    answered_question_ids = set()
    if isinstance(review, dict):
        human_answers = review.get("human_answers", [])
        if isinstance(human_answers, list):
            for answer in human_answers:
                if isinstance(answer, dict) and isinstance(answer.get("question_id"), str):
                    answered_question_ids.add(answer["question_id"])

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
                    message="blocking open question requires a human answer before lowering",
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


def iter_expr_fields(expr: Any, path: str = ""):
    if isinstance(expr, dict):
        if isinstance(expr.get("field"), str):
            yield f"{path}.field", expr["field"]
        for key, value in expr.items():
            if key == "field":
                continue
            yield from iter_expr_fields(value, f"{path}.{key}")
    elif isinstance(expr, list):
        for index, value in enumerate(expr):
            yield from iter_expr_fields(value, f"{path}[{index}]")


def iter_expr_calls(expr: Any, path: str = ""):
    if isinstance(expr, dict):
        if isinstance(expr.get("call"), str):
            yield f"{path}.call", expr["call"]
        for key, value in expr.items():
            if key == "call":
                continue
            yield from iter_expr_calls(value, f"{path}.{key}")
    elif isinstance(expr, list):
        for index, value in enumerate(expr):
            yield from iter_expr_calls(value, f"{path}[{index}]")
