"""Generic pass runner for Spec2Backend IR checkers."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from Spec2Backend.Proof import run_proof_backend

from .expression import ExpressionChecker, ref_name, z3_condition, z3_constraints, z3_env, z3_expr
from .model import CheckIssue, CheckReport, issue, type_compatible


DEFAULT_PASSES = (
    "schema",
    "reference",
    "type",
    "extern",
    "smt",
)

PYTHON_STDLIB_ALLOWLIST = {
    "hashlib.sha224",
    "hashlib.sha256",
    "hashlib.sha384",
    "hashlib.sha512",
}


def run_checks(
    subject: Mapping[str, Any],
    adapter: Any,
    *,
    passes: Iterable[str] | None = None,
    base_dir: str | Path = ".",
    proof_backend: str | None = None,
    proof_options: Mapping[str, Any] | None = None,
) -> CheckReport:
    selected = tuple(passes or DEFAULT_PASSES)
    issues: list[CheckIssue] = []
    proved_rules: list[str] = []
    trusted_externs: list[str] = []
    tested_externs: list[str] = []
    proof_metadata: dict[str, Any] | None = None

    if "schema" in selected:
        issues.extend(adapter.schema_issues(subject, base_dir=Path(base_dir)))

    try:
        context = adapter.context(subject)
    except Exception as exc:  # noqa: BLE001 - adapters must fail as reports, not raw exceptions
        issues.append(issue("schema", "$", f"could not build check context: {exc}"))
        context = None

    if context is not None and not any(item.blocking for item in issues):
        if "reference" in selected or "type" in selected:
            expression_checker = ExpressionChecker(
                context.symbols,
                {extern.extern_id: extern.spec for extern in context.externs},
            )
            for expr_check in context.expressions:
                _, expr_issues = expression_checker.infer(expr_check.expr, expr_check.path)
                issues.extend(_filter_stage(expr_issues, selected))
            for predicate in context.predicates:
                typ, predicate_issues = expression_checker.infer(predicate.expr, predicate.path)
                issues.extend(_filter_stage(predicate_issues, selected))
                if "type" in selected and not (typ.is_bool or typ.is_any):
                    issues.append(
                        issue(
                            "type",
                            predicate.path,
                            "predicate expression must be bool",
                            rule_id=predicate.rule_id,
                            code="condition_type",
                        )
                    )
            for assignment in context.assignments:
                target_type, target_issues = expression_checker.infer(assignment.target, f"{assignment.path}.target")
                value_type, value_issues = expression_checker.infer(assignment.value, f"{assignment.path}.value")
                issues.extend(_filter_stage(target_issues + value_issues, selected))
                if assignment.condition is not None:
                    condition_type, condition_issues = expression_checker.infer(
                        assignment.condition,
                        f"{assignment.path}.condition",
                    )
                    issues.extend(_filter_stage(condition_issues, selected))
                    if "type" in selected and not (condition_type.is_bool or condition_type.is_any):
                        issues.append(
                            issue(
                                "type",
                                f"{assignment.path}.condition",
                                "assignment condition must be bool",
                                rule_id=assignment.rule_id,
                                code="condition_type",
                            )
                        )
                if "type" in selected and not type_compatible(value_type, target_type):
                    target_name = ref_name(assignment.target)
                    issues.append(
                        issue(
                            "type",
                            assignment.path,
                            f"assignment to {target_name!r} has incompatible value type {value_type.to_json()} for target type {target_type.to_json()}",
                            rule_id=assignment.rule_id,
                            code="assignment_type_mismatch",
                        )
                    )

        if "extern" in selected:
            extern_result = _extern_issues(context.externs, base_dir=Path(base_dir))
            issues.extend(extern_result["issues"])
            trusted_externs.extend(extern_result["trusted"])

        if "smt" in selected and not any(item.blocking for item in issues):
            try:
                import z3  # noqa: F401
            except Exception as exc:  # noqa: BLE001
                issues.append(issue("z3", "$", f"z3-solver is required for verification: {exc}"))
            else:
                smt_result = _smt_issues(context)
                issues.extend(smt_result["issues"])
                proved_rules.extend(smt_result["proved_rules"])
                trusted_externs.extend(item for item in smt_result["trusted"] if item not in trusted_externs)

        if "proof" in selected and not any(item.blocking for item in issues):
            proof_result = run_proof_backend(
                context,
                proof_backend=proof_backend,
                proof_options=proof_options,
            )
            issues.extend(proof_result.issues)
            proved_rules.extend(proof_result.proved_rules)
            proof_metadata = dict(proof_result.metadata)

    if context is not None:
        tested_externs.extend(str(item) for item in context.metadata.get("tested_externs", ()))

    blocked = [item for item in issues if item.blocking]
    if blocked:
        status = "failed"
        level = "unverified"
    elif trusted_externs and proved_rules:
        status = "passed"
        level = "mixed_formal_trusted"
    elif trusted_externs:
        status = "passed"
        level = "trusted_standard"
    elif proved_rules:
        status = "passed"
        level = "formally_verified"
    else:
        status = "passed"
        level = "schema_verified"

    metadata = dict(context.metadata) if context is not None else {}
    metadata.setdefault("target", str(subject.get("target") or ""))
    metadata.setdefault("schema_version", subject.get("schema_version"))
    if "proof" in selected and context is not None:
        metadata.setdefault("proof", proof_metadata if proof_metadata is not None else {"backend": proof_backend, "skipped": True})
    return CheckReport(
        status=status,
        verification_level=level,
        proved_rules=tuple(dict.fromkeys(proved_rules)),
        trusted_standard_externs=tuple(dict.fromkeys(trusted_externs)),
        tested_externs=tuple(dict.fromkeys(tested_externs)),
        issues=tuple(issues),
        metadata=metadata,
    )


def _filter_stage(issues: list[CheckIssue], selected: tuple[str, ...]) -> list[CheckIssue]:
    if "reference" in selected and "type" in selected:
        return issues
    return [item for item in issues if item.stage in selected]


def _extern_issues(externs: Iterable[Any], *, base_dir: Path) -> dict[str, Any]:
    issues: list[CheckIssue] = []
    trusted: list[str] = []
    for extern in externs:
        extern_id = str(extern.extern_id)
        spec = dict(extern.spec)
        path = extern.path
        kind = str(spec.get("kind") or "")
        policy = str(spec.get("verification_policy") or "")
        if spec.get("pure") is not True:
            issues.append(issue("extern", f"{path}.pure", "extern must declare pure=true", extern_id=extern_id))
        if spec.get("deterministic") is not True:
            issues.append(issue("extern", f"{path}.deterministic", "extern must declare deterministic=true", extern_id=extern_id))
        if kind == "python_stdlib":
            binding = str(spec.get("binding") or extern_id)
            if binding not in PYTHON_STDLIB_ALLOWLIST:
                issues.append(
                    issue(
                        "extern",
                        f"{path}.binding",
                        f"python stdlib binding {binding!r} is not allowlisted",
                        extern_id=extern_id,
                    )
                )
            if policy != "trusted_standard":
                issues.append(
                    issue(
                        "extern",
                        f"{path}.verification_policy",
                        "python_stdlib externs must use trusted_standard policy",
                        extern_id=extern_id,
                    )
                )
            for key in ("standard_name", "implementation", "version", "artifact_sha256"):
                if not str(spec.get(key) or "").strip():
                    issues.append(issue("extern", f"{path}.{key}", f"trusted_standard extern must define {key}", extern_id=extern_id))
            if not any(item.extern_id == extern_id and item.blocking for item in issues):
                trusted.append(extern_id)
            continue
        if kind == "c_abi":
            _check_c_abi(extern_id, spec, path=path, base_dir=base_dir, issues=issues)
            continue
        if kind == "systemc_worker":
            issues.append(issue("extern", path, "systemc_worker externs are declared but not implemented in v1", extern_id=extern_id))
            continue
        issues.append(issue("extern", path, f"unsupported extern kind {kind!r}", extern_id=extern_id))
    return {"issues": issues, "trusted": trusted}


def _check_c_abi(
    extern_id: str,
    spec: Mapping[str, Any],
    *,
    path: str,
    base_dir: Path,
    issues: list[CheckIssue],
) -> None:
    policy = str(spec.get("verification_policy") or "")
    library = ""
    try:
        library = _normalize_extern_path(spec.get("library"))
    except Exception as exc:  # noqa: BLE001
        issues.append(issue("extern", f"{path}.library", str(exc), extern_id=extern_id))
    if not str(spec.get("function") or "").strip():
        issues.append(issue("extern", f"{path}.function", "c_abi extern must define function", extern_id=extern_id))
    if not str(spec.get("artifact_sha256") or "").strip():
        issues.append(issue("extern", f"{path}.artifact_sha256", "c_abi extern must define artifact_sha256", extern_id=extern_id))
    elif library:
        lib_path = base_dir / library
        if lib_path.exists():
            actual = hashlib.sha256(lib_path.read_bytes()).hexdigest()
            if actual != str(spec.get("artifact_sha256")):
                issues.append(issue("extern", f"{path}.artifact_sha256", "c_abi artifact_sha256 mismatch", extern_id=extern_id))
    timeout_ms = spec.get("timeout_ms")
    if not isinstance(timeout_ms, int) or timeout_ms <= 0:
        issues.append(issue("extern", f"{path}.timeout_ms", "c_abi extern must define a positive timeout_ms", extern_id=extern_id))
    if policy != "formal_model":
        issues.append(issue("extern", f"{path}.verification_policy", "non-standard c_abi externs require formal_model policy", extern_id=extern_id))
    if "formal_model" not in spec:
        issues.append(issue("extern", f"{path}.formal_model", "c_abi formal_model policy requires formal_model expression", extern_id=extern_id))


def _normalize_extern_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("extern path must be a non-empty string")
    if "\\" in path:
        raise ValueError(f"extern path must use POSIX separators: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or any(part in {"", "."} for part in pure.parts):
        raise ValueError(f"extern path must stay within artifact directory: {path!r}")
    return pure.as_posix()


def _smt_issues(context: Any) -> dict[str, Any]:
    import z3

    env = z3_env(context.symbols)
    constraints = z3_constraints(context.symbols, env)
    externs = {extern.extern_id: extern.spec for extern in context.externs}
    issues: list[CheckIssue] = []
    proved_rules: list[str] = []
    trusted: list[str] = []

    for predicate in context.predicates:
        try:
            solver = z3.Solver()
            solver.add(*constraints)
            solver.add(z3_condition(predicate.expr, env, context.symbols, externs))
            if solver.check() == z3.unsat:
                issues.append(issue("z3", predicate.path, "predicate is unsatisfiable", rule_id=predicate.rule_id))
        except Exception as exc:  # noqa: BLE001
            issues.append(issue("z3", predicate.path, f"could not check predicate satisfiability: {exc}", rule_id=predicate.rule_id))

    for totality in context.totality:
        assigning = [rule for rule in totality.rules if totality.output in dict(rule.get("assign", {}))]
        if not assigning:
            issues.append(issue("z3", totality.path, f"output {totality.output!r} is never assigned"))
            continue
        conditions = [z3_condition(rule.get("when"), env, context.symbols, externs) for rule in assigning]
        solver = z3.Solver()
        solver.add(*constraints)
        solver.add(z3.Not(z3.Or(*conditions)))
        if solver.check() == z3.sat:
            issues.append(issue("z3", totality.path, f"output {totality.output!r} is not assigned for all inputs"))
        else:
            proved_rules.extend(str(rule.get("id") or f"rule_{idx}") for idx, rule in enumerate(assigning))
        for left_index, left in enumerate(assigning):
            for right in assigning[left_index + 1 :]:
                solver = z3.Solver()
                solver.add(*constraints)
                solver.add(z3_condition(left.get("when"), env, context.symbols, externs))
                solver.add(z3_condition(right.get("when"), env, context.symbols, externs))
                if solver.check() == z3.sat:
                    issues.append(
                        issue(
                            "z3",
                            totality.path,
                            f"rules {left.get('id')!r} and {right.get('id')!r} can both assign {totality.output!r}",
                        )
                    )

    for equivalence in context.equivalences:
        if equivalence.trusted_extern_id:
            trusted.append(equivalence.trusted_extern_id)
            continue
        try:
            left = z3_expr(equivalence.left, env, context.symbols, externs)
            right = z3_expr(equivalence.right, env, context.symbols, externs)
            solver = z3.Solver()
            solver.add(*constraints)
            if equivalence.condition is not None:
                solver.add(z3_condition(equivalence.condition, env, context.symbols, externs))
            solver.add(left != right)
            if solver.check() == z3.sat:
                issues.append(
                    issue(
                        "z3",
                        equivalence.path,
                        "RefModelIR expression is not equivalent to RefModelPlan rule",
                        rule_id=equivalence.rule_id,
                    )
                )
            else:
                proved_rules.append(equivalence.rule_id or "rule")
        except Exception as exc:  # noqa: BLE001
            issues.append(
                issue(
                    "z3",
                    equivalence.path,
                    f"could not prove rule equivalence: {exc}",
                    rule_id=equivalence.rule_id,
                )
            )
    return {"issues": issues, "proved_rules": proved_rules, "trusted": trusted}
