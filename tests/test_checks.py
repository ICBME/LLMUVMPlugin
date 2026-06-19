from __future__ import annotations

from Spec2Backend.Checks import RefModelIRAdapter, SemanticSpecIRAdapter, run_checks


def test_semantic_adapter_reports_missing_symbol_and_json_issue_path() -> None:
    ir = _semantic_ir(
        symbols=[_symbol("x", {"kind": "int"})],
        ast={
            "node": "constant_relation",
            "target": {"node": "signal_ref", "name": "y"},
            "value": {"node": "literal", "value": 1},
        },
    )

    report = run_checks(ir, SemanticSpecIRAdapter(), passes=("schema", "reference", "type"))

    assert report.passed is False
    assert any(issue.path == "semantic_elements[0].representation.ast.target" for issue in report.blocked_issues)
    assert report.to_json()["blocked_issues"][0]["stage"] in {"reference", "type"}


def test_semantic_adapter_reports_width_and_condition_type_errors() -> None:
    width_ir = _semantic_ir(
        symbols=[
            _symbol("dst", {"kind": "bitvector", "width": 4}),
            _symbol("src", {"kind": "bitvector", "width": 8}),
        ],
        ast={
            "node": "assignment",
            "target": {"node": "signal_ref", "name": "dst"},
            "value": {"node": "signal_ref", "name": "src"},
        },
    )
    condition_ir = _semantic_ir(
        symbols=[_symbol("count", {"kind": "int"})],
        ast={"node": "constraint", "expr": {"node": "field_ref", "name": "count"}},
    )

    width_report = run_checks(width_ir, SemanticSpecIRAdapter(), passes=("schema", "reference", "type"))
    condition_report = run_checks(condition_ir, SemanticSpecIRAdapter(), passes=("schema", "reference", "type"))

    assert any(issue.code == "assignment_type_mismatch" for issue in width_report.blocked_issues)
    assert any("predicate expression must be bool" in issue.message for issue in condition_report.blocked_issues)


def test_ref_model_adapter_reuses_generic_checker_for_formal_totality() -> None:
    ir = {
        "schema_version": 1,
        "target": "notgate",
        "inputs": {"value": {"type": "bool"}},
        "outputs": {"expected": {"type": "bool"}},
        "rules": [{"id": "ref_rule_1", "assign": {"expected": {"not": {"field": "value"}}}}],
        "externs": {},
        "verification": {"required": True},
        "metadata": {},
    }

    report = run_checks(ir, RefModelIRAdapter())

    assert report.passed is True
    assert report.verification_level == "formally_verified"
    assert "ref_rule_1" in report.proved_rules


def _semantic_ir(*, symbols: list[dict], ast: dict) -> dict:
    return {
        "schema_version": 6,
        "target": "demo",
        "semantic_context": {"version": 1, "symbols": symbols, "constraints": []},
        "semantic_elements": [
            {
                "id": "sem1",
                "kind": "combinational_behavior",
                "formalization_status": "candidate",
                "representation": {
                    "ast_version": 2,
                    "kind": "combinational_relation",
                    "text": "test",
                    "ast": ast,
                },
            }
        ],
    }


def _symbol(name: str, typ: dict) -> dict:
    return {
        "name": name,
        "kind": "field",
        "type": typ,
        "direction": "input",
        "roles": ["input"],
        "source": "test",
    }
