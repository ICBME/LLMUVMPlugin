from __future__ import annotations

import copy

from LLMPlugin import CallableLLMBackend
from Spec2Backend.Checks import RefModelIRAdapter, SemanticSpecIRAdapter, run_checks
from Spec2Backend.Checks.model import CheckContext, EquivalenceCheck, PredicateCheck, Symbol, TypeSpec
from Spec2Backend.Proof import discover_lean, run_lean_source
from Spec2Backend.Proof.lean4 import Lean4ProofBackend
from Spec2Backend.RefModelDSL import verify_ref_model_ir
from Spec2Backend.RefModelDSL.emit_plugin import _plugin_source
from Spec2Backend.RefModelPlan import build_ref_model_plan


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
    assert proof["proved_obligations"] == ["expr_equiv:ref_rule_1:1"]
    assert proof["theorems"][0]["sha256"]
    assert proof["theorems"][0]["obligation_id"] == "expr_equiv:ref_rule_1:1"
    assert proof["theorems"][0]["rule_id"] == "ref_rule_1"
    assert proof["theorems"][0]["kind"] == "expr_equiv"


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


def test_lean_backend_proves_bitvector_concat_slice_reduce() -> None:
    for expr, output_type in (
        (
            {"kind": "concat", "parts": [{"field": "a"}, {"field": "b"}]},
            {"type": "bitvector", "width": 4},
        ),
        (
            {"kind": "slice", "value": {"field": "wide"}, "msb": 2, "lsb": 1},
            {"type": "bitvector", "width": 2},
        ),
        (
            {"kind": "reduce", "op": "xor", "operand": {"field": "a"}},
            {"type": "bool"},
        ),
    ):
        ir = {
            "schema_version": 1,
            "target": "bit_structural",
            "inputs": {
                "a": {"type": "bitvector", "width": 2},
                "b": {"type": "bitvector", "width": 2},
                "wide": {"type": "bitvector", "width": 4},
            },
            "outputs": {"expected": output_type},
            "rules": [{"id": "ref_rule_1", "source_rule_id": "ref_rule_1", "assign": {"expected": expr}}],
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
                    "value": expr,
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


def test_lean_backend_splits_mux_obligations() -> None:
    ir = {
        "schema_version": 1,
        "target": "mux",
        "inputs": {"sel": {"type": "bool"}, "a": {"type": "bool"}, "b": {"type": "bool"}},
        "outputs": {"expected": {"type": "bool"}},
        "rules": [
            {
                "id": "ref_rule_1",
                "source_rule_id": "ref_rule_1",
                "assign": {
                    "expected": {
                        "kind": "mux",
                        "condition": {"field": "sel"},
                        "when_true": {"field": "a"},
                        "when_false": {"field": "b"},
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
                    "kind": "if",
                    "condition": {"kind": "field", "name": "sel"},
                    "when_true": {"kind": "field", "name": "a"},
                    "when_false": {"kind": "field", "name": "b"},
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
    assert report.metadata["proof"]["subgoal_count"] == 4
    assert all(".then" in item or ".else" in item for item in report.metadata["proof"]["proved_obligations"])


def test_lean_backend_proves_enum_equivalence_and_rejects_duplicate_choices() -> None:
    ir = {
        "schema_version": 1,
        "target": "fsm",
        "inputs": {"mode": {"type": "enum", "choices": ["idle", "run"]}},
        "outputs": {"expected": {"type": "enum", "choices": ["idle", "run"]}},
        "rules": [
            {
                "id": "ref_rule_1",
                "source_rule_id": "ref_rule_1",
                "assign": {"expected": {"kind": "field", "name": "mode"}},
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
                "value": {"kind": "field", "name": "mode"},
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

    bad_ir = {
        **ir,
        "inputs": {"mode": {"type": "enum", "choices": ["idle", "idle"]}},
        "outputs": {"expected": {"type": "enum", "choices": ["idle", "idle"]}},
    }
    bad_report = run_checks(
        bad_ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
    )

    assert bad_report.passed is False
    assert any("enum choices must be unique" in issue.message for issue in bad_report.blocked_issues)


def test_lean_backend_reports_subgoal_limit() -> None:
    ir, plan = _notgate_ir_and_plan()

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
        proof_options={"max_subgoals": 0},
    )

    assert report.passed is False
    assert any(issue.code == "proof_subgoal_limit" for issue in report.blocked_issues)


def test_lean_backend_reports_invalid_proof_option() -> None:
    ir, plan = _notgate_ir_and_plan()

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
        proof_options={"max_subgoals": "many"},
    )

    assert report.passed is False
    assert any(issue.code == "invalid_proof_option" for issue in report.blocked_issues)


def test_lean_backend_reports_unimplemented_invariant_scope() -> None:
    ir, plan = _notgate_ir_and_plan()

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
        proof_options={"proof_scope": ("invariant",)},
    )

    assert report.passed is False
    assert any(issue.code == "invariant_unimplemented" for issue in report.blocked_issues)


def test_lean_backend_proves_constraint_scope_predicate() -> None:
    context = CheckContext(
        target="constraints",
        symbols={},
        predicates=(PredicateCheck(path="semantic_context.constraints[0]", expr=True, rule_id="constraint_1"),),
    )

    report = Lean4ProofBackend(proof_scope=("constraint",)).prove_context(context)

    assert report.passed is True
    assert report.metadata["obligation_kinds"] == ["constraint_sat"]
    assert report.metadata["proved_obligations"] == ["constraint_sat:constraint_1:1"]


def test_lean_backend_rejects_unknown_bitvector_width() -> None:
    context = CheckContext(
        target="unknown_width",
        symbols={"value": Symbol(name="value", kind="field", type=TypeSpec("bitvector"))},
        equivalences=(
            EquivalenceCheck(
                path="rules.ref_rule_1",
                left={"field": "value"},
                right={"field": "value"},
                rule_id="ref_rule_1",
            ),
        ),
    )

    report = Lean4ProofBackend().prove_context(context)

    assert report.passed is False
    assert any("bitvector type must define width" in issue.message for issue in report.issues)


def test_lean_backend_rule_and_step_scope_records_kinds() -> None:
    ir = {
        "schema_version": 1,
        "target": "counter",
        "inputs": {"inc": {"type": "bool"}},
        "state": {"count": {"type": "bitvector", "width": 4}},
        "outputs": {"expected": {"type": "bitvector", "width": 4}},
        "rules": [],
        "step_rules": [
            {
                "id": "step_rule_1",
                "source_rule_id": "step_rule_1",
                "when": {"field": "inc"},
                "assign": {
                    "expected": {
                        "kind": "binary_op",
                        "op": "+",
                        "left": {"state": "count"},
                        "right": {"literal": 1, "width": 4},
                    }
                },
                "state_updates": {
                    "count": {
                        "kind": "binary_op",
                        "op": "+",
                        "left": {"state": "count"},
                        "right": {"literal": 1, "width": 4},
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
                "id": "step_rule_1",
                "kind": "state_transition",
                "state": {"kind": "state", "name": "count"},
                "from": {"state": "count"},
                "to": {
                    "kind": "binary_op",
                    "op": "+",
                    "left": {"state": "count"},
                    "right": {"literal": 1, "width": 4},
                },
                "condition": {"field": "inc"},
            }
        ]
    }

    report = run_checks(
        ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "extern", "proof"),
        proof_backend="lean4",
        proof_options={"proof_scope": ("expr", "step")},
    )

    assert report.passed is True
    assert set(report.metadata["proof"]["obligation_kinds"]) == {"expr_equiv", "step_equiv"}


def test_ref_model_plan_records_lowering_provenance() -> None:
    semantic_ir, readiness = _semantic_notgate_ir_and_readiness()

    plan = build_ref_model_plan(semantic_ir, readiness=readiness)

    rule = plan["rules"][0]
    assert rule["semantic_element_id"] == "sem1"
    assert rule["source_ast_node"] == "assignment"
    assert rule["source_ast_hash"]
    assert rule["lowered_rule_hash"]
    assert rule["lowering_kind"] == "combinational_relation:assignment"


def test_lean_backend_proves_semantic_plan_equivalence() -> None:
    semantic_ir, readiness = _semantic_notgate_ir_and_readiness()
    plan = build_ref_model_plan(semantic_ir, readiness=readiness)

    report = run_checks(
        semantic_ir,
        SemanticSpecIRAdapter(),
        passes=("schema", "reference", "type", "proof"),
        proof_backend="lean4",
        proof_options={"proof_scope": ("semantic",), "ref_model_plan": plan},
    )

    assert report.passed is True
    proof = report.metadata["proof"]
    assert proof["obligation_kinds"] == ["semantic_plan_equiv"]
    assert proof["proved_obligations"] == ["semantic_plan_equiv:ref_rule_1:value"]
    assert proof["theorems"][0]["kind"] == "semantic_plan_equiv"
    assert proof["theorems"][0]["normalized_sha256"]
    assert proof["theorems"][0]["statement_mode"] == "canonical"
    assert proof["theorems"][0]["canonical_theorem_sha256"]
    assert proof["theorems"][0]["proof_body_sha256"]
    assert proof["theorems"][0]["semantic_ir_sha256"]
    assert proof["theorems"][0]["source_ast_hash"] == plan["rules"][0]["source_ast_hash"]
    assert proof["theorems"][0]["lowered_rule_hash"] == plan["rules"][0]["lowered_rule_hash"]


def test_canonical_theorem_hash_changes_with_semantic_ast() -> None:
    semantic_ir, readiness = _semantic_notgate_ir_and_readiness()
    plan = build_ref_model_plan(semantic_ir, readiness=readiness)
    report = run_checks(
        semantic_ir,
        SemanticSpecIRAdapter(),
        passes=("schema", "reference", "type", "proof"),
        proof_backend="lean4",
        proof_options={"proof_scope": ("semantic",), "ref_model_plan": plan},
    )

    changed_ir = copy.deepcopy(semantic_ir)
    changed_ir["semantic_elements"][0]["representation"]["ast"]["value"] = {"node": "field_ref", "name": "value"}
    changed_plan = build_ref_model_plan(changed_ir, readiness=readiness)
    changed_report = run_checks(
        changed_ir,
        SemanticSpecIRAdapter(),
        passes=("schema", "reference", "type", "proof"),
        proof_backend="lean4",
        proof_options={"proof_scope": ("semantic",), "ref_model_plan": changed_plan},
    )

    assert report.passed is True
    assert changed_report.passed is True
    assert plan["rules"][0]["source_ast_hash"] != changed_plan["rules"][0]["source_ast_hash"]
    assert (
        report.metadata["proof"]["theorems"][0]["canonical_theorem_sha256"]
        != changed_report.metadata["proof"]["theorems"][0]["canonical_theorem_sha256"]
    )


def test_llm_proof_body_can_prove_canonical_semantic_theorem() -> None:
    semantic_ir, readiness = _semantic_notgate_ir_and_readiness()
    plan = build_ref_model_plan(semantic_ir, readiness=readiness)
    prompts = []

    def llm(prompt, model):
        prompts.append(prompt)
        return {"proof_body": "simp"}

    report = run_checks(
        semantic_ir,
        SemanticSpecIRAdapter(),
        passes=("schema", "reference", "type", "proof"),
        proof_backend="lean4",
        proof_options={
            "proof_scope": ("semantic",),
            "ref_model_plan": plan,
            "llm_backend": CallableLLMBackend(llm),
            "llm_model": "fake-lean",
        },
    )

    assert report.passed is True
    theorem = report.metadata["proof"]["theorems"][0]
    assert theorem["statement_mode"] == "canonical"
    assert theorem["llm_attempts"][0]["status"] == "passed"
    assert prompts[0]["workflow"] == "lean4_canonical_proof_body"
    assert "canonical_theorem_statement" in prompts[0]


def test_llm_proof_body_retries_with_lean_diagnostics() -> None:
    semantic_ir, readiness = _semantic_and4_ir_and_readiness()
    plan = build_ref_model_plan(semantic_ir, readiness=readiness)
    ref_ir = _and4_ref_model_ir_commuted()
    prompts = []

    def llm(prompt, model):
        prompts.append(prompt)
        if len(prompts) <= 2:
            return {"proof_body": "rfl"}
        return {"proof_body": "bv_decide"}

    report = run_checks(
        ref_ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "proof"),
        proof_backend="lean4",
        proof_options={
            "proof_scope": ("semantic",),
            "semantic_ir": semantic_ir,
            "llm_backend": CallableLLMBackend(llm),
            "llm_model": "fake-lean",
            "llm_max_attempts": 2,
        },
    )

    assert report.passed is True
    assert len(prompts) == 3
    assert prompts[2]["previous_diagnostics"]
    theorem = next(item for item in report.metadata["proof"]["theorems"] if item["kind"] == "plan_ir_expr_equiv")
    assert [attempt["status"] for attempt in theorem["llm_attempts"]] == ["failed", "passed"]


def test_llm_proof_body_forbidden_tokens_are_blocking() -> None:
    semantic_ir, readiness = _semantic_notgate_ir_and_readiness()
    plan = build_ref_model_plan(semantic_ir, readiness=readiness)

    def llm(prompt, model):
        return {"proof_body": "theorem bad : True := by\n  trivial"}

    report = run_checks(
        semantic_ir,
        SemanticSpecIRAdapter(),
        passes=("schema", "reference", "type", "proof"),
        proof_backend="lean4",
        proof_options={
            "proof_scope": ("semantic",),
            "ref_model_plan": plan,
            "llm_backend": CallableLLMBackend(llm),
        },
    )

    assert report.passed is False
    assert any(issue.code == "llm_proof_body_forbidden" for issue in report.blocked_issues)


def test_lean_backend_proves_semantic_plan_and_plan_ir_equivalence() -> None:
    semantic_ir, readiness = _semantic_notgate_ir_and_readiness()
    plan = build_ref_model_plan(semantic_ir, readiness=readiness)
    ref_ir = _notgate_ref_model_ir()

    report = run_checks(
        ref_ir,
        RefModelIRAdapter(ref_model_plan=plan),
        passes=("schema", "reference", "type", "proof"),
        proof_backend="lean4",
        proof_options={"proof_scope": ("semantic",), "semantic_ir": semantic_ir},
    )

    assert report.passed is True
    proof = report.metadata["proof"]
    assert set(proof["obligation_kinds"]) == {"semantic_plan_equiv", "plan_ir_expr_equiv"}
    assert "semantic_plan_equiv:ref_rule_1:value" in proof["proved_obligations"]
    assert "plan_ir_expr_equiv:ref_rule_1:1" in proof["proved_obligations"]


def test_lean_backend_reports_unsupported_semantic_ast() -> None:
    semantic_ir, _ = _semantic_notgate_ir_and_readiness()
    semantic_ir["semantic_elements"][0]["representation"] = {
        "ast_version": 2,
        "kind": "temporal_rule",
        "text": "done follows start",
        "ast": {"node": "temporal_rule", "property": {"node": "latency_rule"}},
    }
    plan = {
        "rules": [
            {
                "id": "ref_rule_1",
                "kind": "assignment",
                "semantic_element_id": "sem1",
                "target": {"kind": "field", "name": "expected"},
                "value": {"kind": "field", "name": "value"},
            }
        ]
    }

    report = run_checks(
        semantic_ir,
        SemanticSpecIRAdapter(),
        passes=("schema", "proof"),
        proof_backend="lean4",
        proof_options={"proof_scope": ("semantic",), "ref_model_plan": plan},
    )

    assert report.passed is False
    assert any(issue.code == "unsupported_semantic_obligation" for issue in report.blocked_issues)


def test_wrapper_template_check_accepts_deterministic_emitter() -> None:
    ir, _ = _notgate_ir_and_plan()
    source = _plugin_source("notgate", {"verification_level": "schema_verified"})

    report = run_checks(
        ir,
        RefModelIRAdapter(),
        passes=("schema", "proof"),
        proof_backend="lean4",
        proof_options={"proof_scope": ("implementation",), "wrapper_source": source},
    )

    assert report.passed is True
    proof = report.metadata["proof"]
    assert proof["obligation_kinds"] == ["wrapper_template_check"]
    assert proof["proved_obligations"] == ["wrapper_template_check:generated_ref_model"]
    assert proof["static_checks"][0]["implementation_boundary"] == "wrapper_template_only"
    assert proof["static_checks"][0]["trusted_runtime"] == "RefModelInterpreter"
    assert "lean_bin" not in proof


def test_wrapper_template_check_rejects_mutated_logic() -> None:
    ir, _ = _notgate_ir_and_plan()
    source = _plugin_source("notgate", {"verification_level": "schema_verified"})
    bad_sources = [
        source.replace(
            "expected = outputs.get('expected') if isinstance(outputs, dict) else outputs",
            "expected = 0",
        ),
        source.replace("expected=expected,", "expected=0,"),
        source.replace(
            "        outputs, self._state = self._interpreter.step(case.data, state=self._state)\n"
            "        expected = outputs.get('expected') if isinstance(outputs, dict) else outputs",
            "        expected = outputs.get('expected') if isinstance(outputs, dict) else outputs\n"
            "        outputs, self._state = self._interpreter.step(case.data, state=self._state)",
        ),
        source.replace("root = Path(__file__).resolve().parents[1]", "root = Path('/tmp')"),
    ]

    for bad_source in bad_sources:
        report = run_checks(
            ir,
            RefModelIRAdapter(),
            passes=("schema", "proof"),
            proof_backend="lean4",
            proof_options={"proof_scope": ("implementation",), "wrapper_source": bad_source},
        )

        assert report.passed is False
        assert any(issue.code == "wrapper_template_mismatch" for issue in report.blocked_issues)


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


def _notgate_ref_model_ir() -> dict:
    return {
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


def _semantic_notgate_ir_and_readiness() -> tuple[dict, dict]:
    semantic_ir = {
        "schema_version": 6,
        "target": "notgate",
        "inputs": [{"name": "value"}],
        "semantic_context": {
            "version": 1,
            "symbols": [
                {
                    "name": "value",
                    "kind": "field",
                    "type": {"type": "bool"},
                    "direction": "input",
                    "roles": ["input"],
                    "source": "manifest",
                },
                {
                    "name": "expected",
                    "kind": "field",
                    "type": {"type": "bool"},
                    "direction": "output",
                    "roles": ["output"],
                    "source": "manifest",
                },
            ],
            "constraints": [],
        },
        "semantic_elements": [
            {
                "id": "sem1",
                "kind": "combinational_behavior",
                "summary": "expected is the inverse of value",
                "formalization_status": "candidate",
                "confidence": 1.0,
                "subjects": ["expected"],
                "evidence": [],
                "claim_ids": [],
                "representation": {
                    "ast_version": 2,
                    "kind": "combinational_relation",
                    "text": "expected = !value",
                    "ast": {
                        "node": "assignment",
                        "target": {"node": "field_ref", "name": "expected"},
                        "value": {
                            "node": "unary_op",
                            "op": "not",
                            "operand": {"node": "field_ref", "name": "value"},
                        },
                    },
                },
            }
        ],
    }
    readiness = {
        "schema_version": 1,
        "status": "ready",
        "review_gate": {},
        "elements": [
            {
                "semantic_element_id": "sem1",
                "support_status": "ready",
                "recommended_backends": ["ref_model"],
                "lowering_targets": [{"backend": "ref_model", "status": "ready"}],
            }
        ],
    }
    return semantic_ir, readiness


def _semantic_and4_ir_and_readiness() -> tuple[dict, dict]:
    semantic_ir = {
        "schema_version": 6,
        "target": "and4",
        "inputs": [{"name": "a"}, {"name": "b"}],
        "semantic_context": {
            "version": 1,
            "symbols": [
                {
                    "name": "a",
                    "kind": "field",
                    "type": {"type": "bitvector", "width": 4},
                    "direction": "input",
                    "roles": ["input"],
                    "source": "manifest",
                },
                {
                    "name": "b",
                    "kind": "field",
                    "type": {"type": "bitvector", "width": 4},
                    "direction": "input",
                    "roles": ["input"],
                    "source": "manifest",
                },
                {
                    "name": "expected",
                    "kind": "field",
                    "type": {"type": "bitvector", "width": 4},
                    "direction": "output",
                    "roles": ["output"],
                    "source": "manifest",
                },
            ],
            "constraints": [],
        },
        "semantic_elements": [
            {
                "id": "sem1",
                "kind": "combinational_behavior",
                "summary": "expected is a bitwise and b",
                "formalization_status": "candidate",
                "confidence": 1.0,
                "subjects": ["expected"],
                "evidence": [],
                "claim_ids": [],
                "representation": {
                    "ast_version": 2,
                    "kind": "combinational_relation",
                    "text": "expected = a & b",
                    "ast": {
                        "node": "assignment",
                        "target": {"node": "field_ref", "name": "expected"},
                        "value": {
                            "node": "binary_op",
                            "op": "&",
                            "left": {"node": "field_ref", "name": "a"},
                            "right": {"node": "field_ref", "name": "b"},
                        },
                    },
                },
            }
        ],
    }
    readiness = {
        "schema_version": 1,
        "status": "ready",
        "review_gate": {},
        "elements": [
            {
                "semantic_element_id": "sem1",
                "support_status": "ready",
                "recommended_backends": ["ref_model"],
                "lowering_targets": [{"backend": "ref_model", "status": "ready"}],
            }
        ],
    }
    return semantic_ir, readiness


def _and4_ref_model_ir_commuted() -> dict:
    return {
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
