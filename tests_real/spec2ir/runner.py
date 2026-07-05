"""Stage runner for Spec2IR real-data evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
import traceback
from pathlib import Path
from typing import Any

from LLMPlugin import LLMBackend, create_backend
from Spec2Backend.BackendReadiness import analyze_backend_readiness
from Spec2Backend.Spec2IR import (
    generate_semantic_spec_ir,
    repair_semantic_spec_ir_with_review,
    review_semantic_spec_ir,
    validate_semantic_spec_ir,
)

from .adapters import MaterializedSpec2IRInput, materialize_verilogeval_case
from .datasets import RealDataCase


@dataclass(frozen=True)
class StageRecord:
    name: str
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class RealDataCaseResult:
    case: RealDataCase
    materialized: MaterializedSpec2IRInput | None = None
    status: str = "not_run"
    schema_valid: bool = False
    llm_enabled: bool = False
    stages: list[StageRecord] = field(default_factory=list)
    semantic_ir: dict[str, Any] | None = None
    repair_result: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    readiness: dict[str, Any] | None = None
    error: str | None = None
    traceback_text: str | None = None

    def add_stage(self, name: str, status: str, error: str | None = None) -> None:
        self.stages.append(StageRecord(name=name, status=status, error=error))

    def to_dict(self, *, include_ir: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case": self.case.to_dict(),
            "materialized": self.materialized.to_dict() if self.materialized is not None else None,
            "status": self.status,
            "schema_valid": self.schema_valid,
            "llm_enabled": self.llm_enabled,
            "stages": [stage.to_dict() for stage in self.stages],
            "repair_status": self.repair_result.get("status") if self.repair_result else None,
            "review_status": self.review.get("status") if self.review else None,
            "readiness_status": self.readiness.get("status") if self.readiness else None,
            "error": self.error,
        }
        if self.review is not None:
            payload["review"] = self.review
        if self.readiness is not None:
            payload["readiness"] = self.readiness
        if self.repair_result is not None:
            payload["automation_decisions"] = self.repair_result.get("automation_decisions", [])
            payload["deterministic_repairs"] = self.repair_result.get("deterministic_repairs", [])
            payload["attempt_count"] = self.repair_result.get("attempt_count", 0)
        if include_ir and self.semantic_ir is not None:
            payload["semantic_ir"] = self.semantic_ir
        if self.traceback_text:
            payload["traceback"] = self.traceback_text
        return payload


def run_verilogeval_case(
    case: RealDataCase,
    *,
    work_root: str | Path,
    with_llm: bool = False,
    model: str | None = None,
    backend_name: str | None = None,
    llm_backend: LLMBackend | None = None,
) -> RealDataCaseResult:
    result = RealDataCaseResult(case=case, llm_enabled=with_llm)
    try:
        materialized = materialize_verilogeval_case(case, work_root)
        result.materialized = materialized
        result.add_stage("dataset", "passed")
        result.add_stage("adapter", "passed")

        backend = llm_backend
        if with_llm and backend is None:
            try:
                backend = create_backend(backend_name, model=model)
            except Exception as exc:  # noqa: BLE001 - report backend setup as availability
                result.status = "llm_unavailable"
                result.error = f"{type(exc).__name__}: {exc}"
                result.add_stage("llm_backend", "failed", result.error)
                return result
        if with_llm and backend is None:
            result.status = "llm_unavailable"
            result.error = "LLM mode was requested, but no LLM backend is configured"
            result.add_stage("llm_backend", "failed", result.error)
            return result

        semantic_ir = generate_semantic_spec_ir(
            manifest_path=materialized.manifest_path,
            spec_paths=[materialized.spec_path],
            target=materialized.target,
            llm_backend=backend,
            model=model,
        )
        result.semantic_ir = semantic_ir
        result.add_stage("generation", "passed")

        validate_semantic_spec_ir(
            semantic_ir,
            manifest_path=materialized.manifest_path,
            spec_paths=[materialized.spec_path],
            target=materialized.target,
        )
        result.schema_valid = True
        result.add_stage("schema_validation", "passed")

        repair = repair_semantic_spec_ir_with_review(
            semantic_ir,
            manifest_path=materialized.manifest_path,
            spec_paths=[materialized.spec_path],
            target=materialized.target,
            llm_backend=backend if with_llm else None,
            model=model,
            max_attempts=2,
        )
        result.repair_result = repair
        result.semantic_ir = repair["semantic_ir"]
        result.add_stage("automation_repair", repair["status"])

        review = review_semantic_spec_ir(
            result.semantic_ir,
            manifest_path=materialized.manifest_path,
            spec_paths=[materialized.spec_path],
            target=materialized.target,
        )
        result.review = review
        result.add_stage("review", review["status"])

        readiness = analyze_backend_readiness(
            result.semantic_ir,
            review=review,
            require_review_passed=False,
        )
        result.readiness = readiness
        result.add_stage("readiness", readiness["status"])
        result.status = "passed"
        return result
    except Exception as exc:  # noqa: BLE001 - real-data reports should keep going
        result.status = "crashed"
        result.error = f"{type(exc).__name__}: {exc}"
        result.traceback_text = traceback.format_exc()
        result.add_stage("error", "failed", result.error)
        return result


def run_verilogeval_cases(
    cases: list[RealDataCase],
    *,
    work_root: str | Path,
    with_llm: bool = False,
    model: str | None = None,
    backend_name: str | None = None,
) -> list[RealDataCaseResult]:
    return [
        run_verilogeval_case(
            case,
            work_root=work_root,
            with_llm=with_llm,
            model=model,
            backend_name=backend_name,
        )
        for case in cases
    ]
