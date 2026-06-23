from __future__ import annotations

from Spec2Backend.Checks import RefModelIRAdapter, run_checks
from Spec2Backend.Proof import discover_lean, run_lean_source
from Spec2Backend.RefModelDSL import verify_ref_model_ir


def test_lean_discovery_falls_back_to_elan_bin() -> None:
    assert discover_lean() is not None


def test_lean_backend_proves_bool_equivalence_and_records_metadata() -> None:
    ir, plan = _notgate_ir_and_plan()

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
    )

    assert report.passed is True
    assert "ref_rule_1" in report.proved_rules
    proof = dict(report.metadata["proof"])
    assert proof["backend"] == "lean4"
    assert proof["proved_obligations"] == ["ref_rule_1"]
    assert proof["theorems"][0]["sha256"]
    assert proof["theorems"][0]["obligation_id"] == "ref_rule_1"


def test_lean_backend_proves_bitvector_equivalence() -> None:
    ir = {
        "schema_version": 1,
        "target": "and4",
        "inputs": {
            "a": {"type": "bitvector", "width": 4},
            "b": {"type": "bitvector", "width": 4},
        },
        "outputs": {"expected": {"type": "bitvector", "width": 4}},
        "rules": [
            {
                "id": "ref_rule_1",
                "source_rule_id": "ref_rule_1",
                "assign": {
                    "expected": {
                        "kind": "binary_op",
                        "op": "&",
                        "left": {"field": "b"},
                        "right": {"field": "a"},
                    }
                },
            }
        ],
        "externs": {},
        "verification": {"required": True},
        "metadata": {},
    }
    plan = {
        "rules": [
            {
                "id": "ref_rule_1",
                "kind": "assignment",
                "target": {"kind": "field", "name": "expected"},
                "value": {
                    "kind": "binary_op",
                    "op": "&",
                    "left": {"kind": "field", "name": "a"},
                    "right": {"kind": "field", "name": "b"},
                },
            }
        ]
    }

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
    )

    assert report.passed is True
    assert "ref_rule_1" in report.proved_rules
    assert any("bv_decide" in axiom for axiom in report.metadata["proof"]["theorems"][0]["axioms"])


def test_lean_backend_reports_unsupported_type_without_raw_exception() -> None:
    ir = {
        "schema_version": 1,
        "target": "string_ref",
        "inputs": {"value": {"type": "string"}},
        "outputs": {"expected": {"type": "string"}},
        "rules": [
            {
                "id": "ref_rule_1",
                "source_rule_id": "ref_rule_1",
                "assign": {"expected": {"field": "value"}},
            }
        ],
        "externs": {},
        "verification": {"required": True},
        "metadata": {},
    }
    plan = {
        "rules": [
            {
                "id": "ref_rule_1",
                "kind": "assignment",
                "target": {"kind": "field", "name": "expected"},
                "value": {"kind": "field", "name": "value"},
            }
        ]
    }

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
    )

    assert report.passed is False
    assert any(issue.stage == "proof" and issue.code == "unsupported_obligation" for issue in report.blocked_issues)


def test_coq_backend_is_registered_but_unimplemented() -> None:
    ir, plan = _notgate_ir_and_plan()

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="coq",
    )

    assert report.passed is False
    assert any(issue.code == "coq_unimplemented" for issue in report.blocked_issues)


def test_forbidden_lean_source_is_rejected_before_tool_execution() -> None:
    result = run_lean_source(
        "axiom bad : True\n",
        command=discover_lean() or "/missing/lean",
        timeout_s=1.0,
        theorem_name="bad",
        allowed_axioms=(),
    )

    assert result["ok"] is False
    assert result["code"] == "forbidden_proof_token"


def test_missing_lean_returns_blocking_issue(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LEAN_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    ir, plan = _notgate_ir_and_plan()

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
    )

    assert report.passed is False
    assert any(issue.code == "lean_missing" for issue in report.blocked_issues)


def test_verify_ref_model_ir_accepts_lean4_backend_arguments() -> None:
    ir, plan = _notgate_ir_and_plan()

    report = verify_ref_model_ir(
        ir,
        ref_model_plan=plan,
        proof_backend="lean4",
    )

    assert set(report.to_json()) == {
        "status",
        "verification_level",
        "proved_rules",
        "trusted_standard_externs",
        "tested_externs",
        "blocked_issues",
        "issues",
        "metadata",
    }


def _notgate_ir_and_plan() -> tuple[dict, dict]:
    ir = {
        "schema_version": 1,
        "target": "notgate",
        "inputs": {"value": {"type": "bool"}},
        "outputs": {"expected": {"type": "bool"}},
        "rules": [
            {
                "id": "ref_rule_1",
                "source_rule_id": "ref_rule_1",
                "assign": {"expected": {"not": {"field": "value"}}},
            }
        ],
        "externs": {},
        "verification": {"required": True},
        "metadata": {},
    }
    plan = {
        "rules": [
            {
                "id": "ref_rule_1",
                "kind": "assignment",
                "target": {"kind": "field", "name": "expected"},
                "value": {
                    "kind": "unary_op",
                    "op": "not",
                    "operand": {"kind": "field", "name": "value"},
                },
            }
        ]
    }
    return ir, plan
