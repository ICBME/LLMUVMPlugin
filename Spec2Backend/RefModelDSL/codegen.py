"""Feedback-driven RefModelIR generation task."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
from typing import Any, Mapping

from LLMPlugin import LLMBackend
from Spec2Backend.FeedbackCodegen.loop import generate_with_feedback
from Spec2Backend.FeedbackCodegen.ref_model import (
    evaluate_candidate_ref_model_subprocess,
    manifest_payload,
    spec_payloads,
)
from Spec2Backend.FeedbackCodegen.schema import (
    CodegenEvaluation,
    CodegenEvaluationIssue,
    CodegenLoopConfig,
    CodegenResult,
    FeedbackCodegenError,
    GeneratedFileBundle,
    GoldenCase,
    write_json as write_codegen_json,
)

from .emit_plugin import emit_ref_model_plugin, ref_model_plugin_spec
from .schema import VerificationReport, normalize_ref_model_ir, read_json, write_json
from .verifier import verify_ref_model_ir


@dataclass(frozen=True)
class RefModelIRCodegenTask:
    ref_model_plan: Mapping[str, Any]
    manifest_path: Path
    spec_paths: tuple[Path, ...] = ()
    golden_cases: tuple[GoldenCase, ...] = ()
    evaluation_timeout_s: float = 10.0
    name: str = "ref_model_ir_codegen"
    artifact_kind: str = "ref_model_ir"
    target: str = field(init=False)

    def __post_init__(self) -> None:
        target = str(self.ref_model_plan.get("target") or "")
        if not target:
            raise FeedbackCodegenError("RefModelIR codegen target could not be determined")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "manifest_path", Path(self.manifest_path))
        object.__setattr__(self, "spec_paths", tuple(Path(path) for path in self.spec_paths))
        object.__setattr__(
            self,
            "golden_cases",
            tuple(GoldenCase.from_value(case, default_target=target) for case in self.golden_cases),
        )

    def prepare_input_artifacts(self, output_dir: Path) -> None:
        write_codegen_json(output_dir / "ref_model_plan.json", dict(self.ref_model_plan))

    def input_artifacts(self, output_dir: Path) -> dict[str, Path]:
        return {"plan": output_dir / "ref_model_plan.json"}

    def build_prompt(self, feedback: dict[str, Any] | None = None) -> dict[str, Any]:
        prompt: dict[str, Any] = {
            "task": "Generate a verifiable RefModelIR DSL artifact from RefModelPlan.",
            "workflow": "ref_model_plan_to_ref_model_ir_with_feedback",
            "target": self.target,
            "ref_model_plan": dict(self.ref_model_plan),
            "constraints": [
                "Return strict JSON only.",
                "Return ref_model_ir, metadata, and assumptions.",
                "Do not generate Python plugin code; the framework emits the UVM wrapper deterministically.",
                "Use extern_call only for declared pure deterministic externs.",
                "Use trusted_standard only for standard reference implementations with provenance metadata.",
                "Non-standard c_abi externs require verification_policy='formal_model'.",
            ],
            "response_contract": {
                "ref_model_ir": {
                    "schema_version": 1,
                    "target": self.target,
                    "inputs": {"field_name": {"type": "int|bool|enum|hex_string|bytes"}},
                    "outputs": {"expected": {"type": "int|bool|hex_string|string"}},
                    "rules": [
                        {
                            "id": "rule_id",
                            "source_rule_id": "RefModelPlan rule id",
                            "when": {"eq": [{"field": "mode"}, {"literal": "sha256"}]},
                            "assign": {"expected": {"literal": "value or expression"}},
                        }
                    ],
                    "externs": {},
                    "verification": {"required": True},
                    "metadata": {},
                },
                "metadata": {"notes": "optional JSON metadata"},
                "assumptions": ["optional concise assumptions"],
            },
            "inputs": {
                "target_manifest": manifest_payload(self.manifest_path),
                "specs": spec_payloads(self.spec_paths),
                "golden_case_count": len(self.golden_cases),
            },
        }
        if feedback is not None:
            prompt["feedback"] = feedback
        return prompt

    def normalize_response(self, response_json: dict[str, Any]) -> GeneratedFileBundle:
        ref_model_ir = normalize_ref_model_ir(_extract_ref_model_ir(response_json))
        metadata = dict(response_json.get("metadata", {})) if isinstance(response_json.get("metadata", {}), Mapping) else {}
        metadata["ref_model"] = ref_model_plugin_spec(str(ref_model_ir.get("target") or self.target))
        metadata.setdefault("artifact_kind", "ref_model_ir")
        assumptions = response_json.get("assumptions", ())
        if assumptions is None:
            assumptions = ()
        return GeneratedFileBundle.from_dict(
            {
                "files": [
                    {
                        "path": "ref_model_ir.json",
                        "content": json.dumps(ref_model_ir, indent=2, sort_keys=True) + "\n",
                    }
                ],
                "metadata": metadata,
                "assumptions": list(assumptions) if isinstance(assumptions, list | tuple) else [str(assumptions)],
            }
        )

    def write_candidate(self, bundle: GeneratedFileBundle, candidate_dir: Path) -> tuple[Path, ...]:
        return bundle.write_to(candidate_dir)

    def evaluate(self, bundle: GeneratedFileBundle, candidate_dir: Path) -> CodegenEvaluation:
        try:
            ir = read_json(candidate_dir / "ref_model_ir.json")
        except Exception as exc:  # noqa: BLE001
            return CodegenEvaluation.failed(
                CodegenEvaluationIssue(stage="schema", path="ref_model_ir.json", message=f"{type(exc).__name__}: {exc}"),
                stage="schema",
            )
        report = verify_ref_model_ir(ir, ref_model_plan=self.ref_model_plan, base_dir=candidate_dir)
        write_json(candidate_dir / "verification_report.json", report)
        if not report.passed:
            return CodegenEvaluation(
                passed=False,
                issues=tuple(_issue_to_codegen(issue) for issue in report.blocked_issues),
                metadata={
                    "stage": "verification",
                    "verification_report": str(candidate_dir / "verification_report.json"),
                    "verification_level": report.verification_level,
                },
            )
        emit_ref_model_plugin(self.target, candidate_dir, verification_report=report.to_json())
        plugin_spec = str(bundle.metadata.get("ref_model") or ref_model_plugin_spec(self.target))
        golden_eval = evaluate_candidate_ref_model_subprocess(
            bundle,
            candidate_dir=candidate_dir,
            target=self.target,
            plugin_spec=plugin_spec,
            golden_cases=self.golden_cases,
            timeout_s=self.evaluation_timeout_s,
        )
        metadata = {
            **dict(golden_eval.metadata),
            "verification_level": report.verification_level,
            "verification_report": str(candidate_dir / "verification_report.json"),
        }
        return CodegenEvaluation(
            passed=golden_eval.passed,
            issues=golden_eval.issues,
            metadata=metadata,
        )

    def feedback_from_evaluation(
        self,
        evaluation: CodegenEvaluation,
        *,
        attempt_index: int,
        bundle: GeneratedFileBundle | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "target": self.target,
            "artifact_kind": self.artifact_kind,
            "attempt_index": attempt_index,
            "passed": evaluation.passed,
            "blocking_issues": [issue.to_json() for issue in evaluation.blocking_issues][:20],
            "issue_count": len(evaluation.issues),
            "evaluation_metadata": dict(evaluation.metadata),
            "candidate_summary": _bundle_summary(bundle),
        }

    def promote(
        self,
        bundle: GeneratedFileBundle,
        candidate_dir: Path,
        final_dir: Path,
    ) -> tuple[Path, ...]:
        if final_dir.exists():
            shutil.rmtree(final_dir)
        shutil.copytree(candidate_dir, final_dir)
        write_codegen_json(final_dir / "bundle.json", bundle)
        return tuple(path for path in final_dir.rglob("*") if path.is_file())


def generate_ref_model_ir_with_feedback(
    ref_model_plan: dict[str, Any],
    *,
    manifest_path: str | Path,
    spec_paths: tuple[str | Path, ...] = (),
    output_dir: str | Path,
    llm_backend: LLMBackend,
    golden_cases: tuple[GoldenCase | Mapping[str, Any], ...] = (),
    max_attempts: int = 3,
    model: str | None = None,
    evaluation_timeout_s: float = 10.0,
) -> CodegenResult:
    task = RefModelIRCodegenTask(
        ref_model_plan=ref_model_plan,
        manifest_path=Path(manifest_path),
        spec_paths=tuple(Path(path) for path in spec_paths),
        golden_cases=tuple(golden_cases),
        evaluation_timeout_s=evaluation_timeout_s,
    )
    return generate_with_feedback(
        task,
        CodegenLoopConfig(
            output_dir=Path(output_dir),
            max_attempts=max_attempts,
            run_name="ref_model_ir_feedback_codegen",
            tags=("ref-model-ir", task.target),
            metadata={"target": task.target},
        ),
        llm_backend,
        model=model,
    )


def _extract_ref_model_ir(value: Mapping[str, Any]) -> dict[str, Any]:
    candidate = value.get("ref_model_ir", value.get("ir", value.get("result")))
    if isinstance(candidate, Mapping):
        return dict(candidate)
    raise FeedbackCodegenError("LLM response did not contain ref_model_ir object")


def _issue_to_codegen(issue: Any) -> CodegenEvaluationIssue:
    return CodegenEvaluationIssue(
        stage=_feedback_stage(str(issue.stage)),
        severity=str(issue.severity),
        path=issue.path,
        message=str(issue.message),
        blocking=bool(issue.blocking),
        rule_id=issue.rule_id,
    )


def _feedback_stage(stage: str) -> str:
    if stage in {"z3", "extern", "schema"}:
        return "verification"
    return stage


def _bundle_summary(bundle: GeneratedFileBundle | None) -> dict[str, Any]:
    if bundle is None:
        return {}
    return {
        "metadata": dict(bundle.metadata),
        "files": [
            {
                "path": item.path,
                "line_count": item.content.count("\n") + 1,
                "char_count": len(item.content),
            }
            for item in bundle.files
        ],
    }


__all__ = ["RefModelIRCodegenTask", "generate_ref_model_ir_with_feedback"]
