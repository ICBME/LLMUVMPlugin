"""Ref model adapter for feedback-driven LLM code generation."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import importlib
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]

from LLMPlugin import LLMBackend

from .loop import generate_with_feedback
from .schema import (
    CodegenEvaluation,
    CodegenEvaluationIssue,
    CodegenLoopConfig,
    CodegenResult,
    FeedbackCodegenError,
    GeneratedFileBundle,
    GoldenCase,
    write_json,
)


DANGEROUS_IMPORT_ROOTS = {"subprocess", "socket", "requests", "urllib"}
DANGEROUS_NAMES = {"eval", "exec"}
DANGEROUS_OS_ATTRS = {"system", "popen", "spawnl", "spawnlp", "spawnv", "spawnvp"}
DANGEROUS_WRITE_ATTRS = {
    "mkdir",
    "open",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "unlink",
    "write_bytes",
    "write_text",
}


@dataclass(frozen=True)
class RefModelCodegenTask:
    ref_model_plan: Mapping[str, Any]
    manifest_path: Path
    spec_paths: tuple[Path, ...] = ()
    golden_cases: tuple[GoldenCase, ...] = ()
    name: str = "ref_model_codegen"
    artifact_kind: str = "ref_model"
    target: str = field(init=False)

    def __post_init__(self) -> None:
        manifest_path = Path(self.manifest_path)
        target = str(self.ref_model_plan.get("target") or _manifest_target(manifest_path) or "")
        if not target:
            raise FeedbackCodegenError("ref model codegen target could not be determined")
        object.__setattr__(self, "manifest_path", manifest_path)
        object.__setattr__(self, "spec_paths", tuple(Path(path) for path in self.spec_paths))
        object.__setattr__(
            self,
            "golden_cases",
            tuple(
                GoldenCase.from_value(case, default_target=target)
                for case in self.golden_cases
            ),
        )
        object.__setattr__(self, "target", target)

    def build_prompt(self, feedback: dict[str, Any] | None = None) -> dict[str, Any]:
        prompt: dict[str, Any] = {
            "task": "Generate a Python reference model plugin from RefModelPlan.",
            "workflow": "ref_model_plan_to_python_ref_model_with_feedback",
            "target": self.target,
            "ref_model_plan": dict(self.ref_model_plan),
            "constraints": [
                "Return strict JSON only.",
                "Return a complete file bundle under files[].",
                "Do not modify replay runtime, test infrastructure, or files outside the bundle.",
                "Generated Python must not use subprocess, socket, requests, urllib, eval, exec, os.system, or file writes.",
                "The ref model plugin must define predict(case) and return ExpectedResult or {'expected': value}.",
                "Use case.data for input fields and preserve RefModelPlan rule intent.",
                "Include metadata.ref_model as 'module:Object' pointing at the generated plugin class.",
            ],
            "plugin_contract": {
                "reference_model": (
                    "A ref model object must provide predict(case). predict returns "
                    "fuzz_uvm.contracts.ExpectedResult, a mapping containing expected, "
                    "or a raw expected value."
                ),
                "constructor": "The plugin class should accept target=None and config=None.",
            },
            "response_contract": {
                "files": [
                    {
                        "path": "generated/<target>_ref_model.py",
                        "content": "Python source text",
                    }
                ],
                "metadata": {
                    "ref_model": "generated.<target>_ref_model:GeneratedRefModel"
                },
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
        return normalize_generated_file_bundle_response(response_json)

    def write_candidate(
        self,
        bundle: GeneratedFileBundle,
        candidate_dir: Path,
    ) -> tuple[Path, ...]:
        return bundle.write_to(candidate_dir)

    def evaluate(
        self,
        bundle: GeneratedFileBundle,
        candidate_dir: Path,
    ) -> CodegenEvaluation:
        issues: list[CodegenEvaluationIssue] = []
        issues.extend(static_validation_issues(bundle))
        if any(issue.blocking for issue in issues):
            return CodegenEvaluation(False, tuple(issues), metadata={"stage": "static"})

        plugin_spec = bundle.metadata.get("ref_model")
        if not isinstance(plugin_spec, str) or not plugin_spec.strip():
            issues.append(
                CodegenEvaluationIssue(
                    stage="contract",
                    path="metadata.ref_model",
                    message="bundle metadata must define ref_model plugin spec",
                )
            )
            return CodegenEvaluation(False, tuple(issues), metadata={"stage": "contract"})

        try:
            plugin = build_candidate_ref_model(
                plugin_spec,
                candidate_dir=candidate_dir,
                target=self.target,
            )
        except Exception as exc:  # noqa: BLE001 - feedback should preserve import failure detail
            issues.append(
                CodegenEvaluationIssue(
                    stage="contract",
                    path=plugin_spec,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return CodegenEvaluation(False, tuple(issues), metadata={"stage": "contract"})

        issues.extend(golden_case_issues(plugin, self.golden_cases))
        return CodegenEvaluation(
            passed=not any(issue.blocking for issue in issues),
            issues=tuple(issues),
            metadata={
                "stage": "golden_case" if self.golden_cases else "contract",
                "ref_model": plugin_spec,
                "golden_case_count": len(self.golden_cases),
            },
        )

    def feedback_from_evaluation(
        self,
        evaluation: CodegenEvaluation,
        *,
        attempt_index: int,
        bundle: GeneratedFileBundle | None = None,
    ) -> dict[str, Any]:
        blocking = [issue.to_json() for issue in evaluation.blocking_issues]
        return {
            "schema_version": 1,
            "target": self.target,
            "artifact_kind": self.artifact_kind,
            "attempt_index": attempt_index,
            "passed": evaluation.passed,
            "blocking_issues": blocking[:20],
            "issue_count": len(evaluation.issues),
            "evaluation_metadata": dict(evaluation.metadata),
            "candidate_summary": bundle_summary(bundle),
        }

    def promote(
        self,
        bundle: GeneratedFileBundle,
        candidate_dir: Path,
        final_dir: Path,
    ) -> tuple[Path, ...]:
        if final_dir.exists():
            shutil.rmtree(final_dir)
        final_dir.mkdir(parents=True, exist_ok=True)
        written = bundle.write_to(final_dir)
        write_json(final_dir / "bundle.json", bundle)
        return written


def generate_ref_model_with_feedback(
    ref_model_plan: dict[str, Any],
    *,
    manifest_path: str | Path,
    spec_paths: tuple[str | Path, ...] = (),
    output_dir: str | Path,
    llm_backend: LLMBackend,
    golden_cases: tuple[GoldenCase | Mapping[str, Any], ...] = (),
    max_attempts: int = 3,
    model: str | None = None,
) -> CodegenResult:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "ref_model_plan.json", ref_model_plan)
    task = RefModelCodegenTask(
        ref_model_plan=ref_model_plan,
        manifest_path=Path(manifest_path),
        spec_paths=tuple(Path(path) for path in spec_paths),
        golden_cases=tuple(golden_cases),
    )
    return generate_with_feedback(
        task,
        CodegenLoopConfig(
            output_dir=output,
            max_attempts=max_attempts,
            run_name="ref_model_feedback_codegen",
            tags=("ref-model", task.target),
            metadata={"target": task.target},
        ),
        llm_backend,
        model=model,
    )


def normalize_generated_file_bundle_response(value: dict[str, Any]) -> GeneratedFileBundle:
    try:
        return GeneratedFileBundle.from_dict(value)
    except FeedbackCodegenError:
        pass
    for key in ("generated_file_bundle", "file_bundle", "bundle", "artifact_bundle", "result", "output"):
        nested = value.get(key)
        if isinstance(nested, dict):
            try:
                return normalize_generated_file_bundle_response(nested)
            except FeedbackCodegenError:
                continue
    raise FeedbackCodegenError("LLM response did not contain a generated file bundle")


def static_validation_issues(bundle: GeneratedFileBundle) -> tuple[CodegenEvaluationIssue, ...]:
    issues: list[CodegenEvaluationIssue] = []
    for item in bundle.files:
        if not item.path.endswith(".py"):
            continue
        try:
            tree = ast.parse(item.content, filename=item.path)
        except SyntaxError as exc:
            issues.append(
                CodegenEvaluationIssue(
                    stage="static",
                    path=item.path,
                    message=f"SyntaxError: {exc.msg} line {exc.lineno}",
                )
            )
            continue
        issues.extend(_dangerous_ast_issues(tree, path=item.path))
    return tuple(issues)


def build_candidate_ref_model(
    plugin_spec: str,
    *,
    candidate_dir: Path,
    target: str,
) -> Any:
    _ensure_fuzz_python_path()
    from fuzz_bfm.plugin_loader import build_plugin
    from fuzz_uvm.contracts import validate_ref_model_plugin

    module_name, sep, _object_name = plugin_spec.partition(":")
    if not sep:
        raise FeedbackCodegenError(f"plugin spec must be 'module:Object', got {plugin_spec!r}")
    _clear_candidate_modules(module_name)
    sys.path.insert(0, str(candidate_dir))
    try:
        plugin = build_plugin(plugin_spec, target=target, config=None)
        return validate_ref_model_plugin(plugin, spec=plugin_spec)
    finally:
        try:
            sys.path.remove(str(candidate_dir))
        except ValueError:
            pass


def golden_case_issues(
    plugin: Any,
    golden_cases: tuple[GoldenCase, ...],
) -> tuple[CodegenEvaluationIssue, ...]:
    if not golden_cases:
        return ()
    _ensure_fuzz_python_path()
    from fuzz_bfm.corpus import FuzzCase
    from fuzz_uvm.contracts import normalize_expected

    issues: list[CodegenEvaluationIssue] = []
    for index, golden in enumerate(golden_cases, start=1):
        case_id = golden.case_id or f"golden_{index}"
        data = dict(golden.data)
        data.setdefault("target", golden.target)
        case = FuzzCase(target=golden.target, data=data, line_no=golden.line_no)
        try:
            actual = normalize_expected(
                plugin.predict(case),
                spec=f"{case_id} ref model prediction",
            ).expected
        except Exception as exc:  # noqa: BLE001 - feedback should capture plugin exceptions
            issues.append(
                CodegenEvaluationIssue(
                    stage="golden_case",
                    case_id=case_id,
                    message=f"{type(exc).__name__}: {exc}",
                    expected=golden.expected,
                )
            )
            continue
        if actual != golden.expected:
            issues.append(
                CodegenEvaluationIssue(
                    stage="golden_case",
                    case_id=case_id,
                    message="expected value mismatch",
                    expected=golden.expected,
                    actual=actual,
                )
            )
    return tuple(issues)


def manifest_payload(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
    }
    if tomllib is None:
        return payload
    try:
        data = tomllib.loads(payload["content"])
    except Exception:
        return payload
    fields = data.get("field", ())
    if not isinstance(fields, list):
        fields = ()
    payload["summary"] = {
        "target": data.get("name"),
        "driver": data.get("driver"),
        "fields": [
            {
                "name": field.get("name"),
                "kind": field.get("kind"),
                "choices": field.get("choices", ()),
                "min": field.get("min"),
                "max": field.get("max"),
                "hex_len": field.get("hex_len"),
            }
            for field in fields
            if isinstance(field, dict)
        ],
    }
    return payload


def spec_payloads(paths: tuple[Path, ...]) -> list[dict[str, Any]]:
    payloads = []
    for index, path in enumerate(paths, start=1):
        payloads.append(
            {
                "id": f"src{index}",
                "path": str(path),
                "content": path.read_text(encoding="utf-8", errors="replace"),
            }
        )
    return payloads


def bundle_summary(bundle: GeneratedFileBundle | None) -> dict[str, Any]:
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


def _dangerous_ast_issues(tree: ast.AST, *, path: str) -> tuple[CodegenEvaluationIssue, ...]:
    issues: list[CodegenEvaluationIssue] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.partition(".")[0]
                if root in DANGEROUS_IMPORT_ROOTS:
                    issues.append(_static_issue(path, node, f"dangerous import {alias.name!r}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.partition(".")[0]
            if root in DANGEROUS_IMPORT_ROOTS:
                issues.append(_static_issue(path, node, f"dangerous import {module!r}"))
        elif isinstance(node, ast.Call):
            issues.extend(_dangerous_call_issues(node, path=path))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if text.startswith("/") or "../" in text or "..\\" in text:
                issues.append(_static_issue(path, node, "absolute or parent-directory path literal is not allowed"))
    return tuple(issues)


def _dangerous_call_issues(node: ast.Call, *, path: str) -> list[CodegenEvaluationIssue]:
    issues: list[CodegenEvaluationIssue] = []
    func = node.func
    if isinstance(func, ast.Name) and func.id in DANGEROUS_NAMES | {"open"}:
        issues.append(_static_issue(path, node, f"dangerous call {func.id}()"))
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr in DANGEROUS_OS_ATTRS:
            issues.append(_static_issue(path, node, f"dangerous call os.{func.attr}()"))
        if func.attr in DANGEROUS_WRITE_ATTRS:
            issues.append(_static_issue(path, node, f"file or process side-effect call {func.attr}()"))
    return issues


def _static_issue(path: str, node: ast.AST, message: str) -> CodegenEvaluationIssue:
    return CodegenEvaluationIssue(
        stage="static",
        path=path,
        message=f"{message} at line {getattr(node, 'lineno', '?')}",
    )


def _ensure_fuzz_python_path() -> None:
    root = Path(__file__).resolve().parents[2]
    fuzz_py = root / "libafl_bfm_fuzz" / "py"
    if fuzz_py.exists() and str(fuzz_py) not in sys.path:
        sys.path.insert(0, str(fuzz_py))


def _clear_candidate_modules(module_name: str) -> None:
    root = module_name.partition(".")[0]
    for name in list(sys.modules):
        if name == root or name.startswith(f"{root}."):
            del sys.modules[name]
    importlib.invalidate_caches()


def _manifest_target(path: Path) -> str:
    if tomllib is None or not path.exists():
        return ""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("name") or "")


__all__ = [
    "RefModelCodegenTask",
    "build_candidate_ref_model",
    "generate_ref_model_with_feedback",
    "golden_case_issues",
    "manifest_payload",
    "normalize_generated_file_bundle_response",
    "spec_payloads",
    "static_validation_issues",
]
