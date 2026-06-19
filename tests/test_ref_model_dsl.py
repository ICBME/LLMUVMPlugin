from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from LLMPlugin import CallableLLMBackend
from Spec2Backend.RefModelDSL import (
    RefModelInterpreter,
    generate_ref_model_ir_with_feedback,
    ref_model_plugin_spec,
    verify_ref_model_ir,
)


def test_schema_type_and_z3_equivalence_for_notgate() -> None:
    report = verify_ref_model_ir(_notgate_ir(), ref_model_plan=_notgate_plan())
    wrong = verify_ref_model_ir(_notgate_ir(correct=False), ref_model_plan=_notgate_plan())
    unknown = verify_ref_model_ir(_notgate_ir(field_name="missing"), ref_model_plan=_notgate_plan())

    assert report.passed is True
    assert report.verification_level == "formally_verified"
    assert "ref_rule_1" in report.proved_rules
    assert wrong.passed is False
    assert any("not equivalent" in issue.message for issue in wrong.blocked_issues)
    assert unknown.passed is False
    assert any("unknown field" in issue.message for issue in unknown.blocked_issues)


def test_totality_and_overlap_failures_are_blocking() -> None:
    incomplete = _mode_ir(
        [
            {
                "id": "only_sha256",
                "when": {"eq": [{"field": "mode"}, {"literal": "sha256"}]},
                "assign": {"expected": {"literal": "ok"}},
            }
        ]
    )
    duplicate = _mode_ir(
        [
            {"id": "left", "assign": {"expected": {"literal": "a"}}},
            {"id": "right", "assign": {"expected": {"literal": "b"}}},
        ]
    )

    incomplete_report = verify_ref_model_ir(incomplete)
    duplicate_report = verify_ref_model_ir(duplicate)

    assert incomplete_report.passed is False
    assert any("not assigned for all inputs" in issue.message for issue in incomplete_report.blocked_issues)
    assert duplicate_report.passed is False
    assert any("can both assign" in issue.message for issue in duplicate_report.blocked_issues)


def test_schema_rejects_missing_target_unassigned_unknown_extern_and_bad_c_abi_path() -> None:
    missing_target = _notgate_ir()
    missing_target["target"] = ""
    unassigned = _notgate_ir()
    unassigned["rules"] = [{"id": "bad", "assign": {"other": {"literal": True}}}]
    unknown_extern = _notgate_ir()
    unknown_extern["rules"] = [{"id": "bad", "assign": {"expected": {"extern_call": "missing"}}}]
    absolute_path = _c_abi_ir(
        "formal_model",
        "0" * 64,
        formal_model={"binary": {"op": "add", "left": {"field": "arg0"}, "right": {"literal": 1}}},
    )
    absolute_path["externs"]["plus_one"]["library"] = "/tmp/libplus_one.so"
    missing_hash = _c_abi_ir(
        "formal_model",
        "",
        formal_model={"binary": {"op": "add", "left": {"field": "arg0"}, "right": {"literal": 1}}},
    )

    reports = [
        verify_ref_model_ir(missing_target),
        verify_ref_model_ir(unassigned),
        verify_ref_model_ir(unknown_extern),
        verify_ref_model_ir(absolute_path),
        verify_ref_model_ir(missing_hash),
    ]

    assert all(report.passed is False for report in reports)
    assert any("must define target" in issue.message for issue in reports[0].blocked_issues)
    assert any("never assigned" in issue.message or "unknown output" in issue.message for issue in reports[1].blocked_issues)
    assert any("unknown extern" in issue.message for issue in reports[2].blocked_issues)
    assert any("stay within artifact directory" in issue.message for issue in reports[3].blocked_issues)
    assert any("artifact_sha256" in str(issue.path) for issue in reports[4].blocked_issues)


def test_interpreter_supports_conditionals_hex_and_trusted_standard_extern() -> None:
    ir = _sha_ir()
    report = verify_ref_model_ir(ir, ref_model_plan=_sha_plan())
    interpreter = RefModelInterpreter(ir)

    assert report.passed is True
    assert report.verification_level == "mixed_formal_trusted"
    assert report.trusted_standard_externs == ("std.sha256",)
    assert interpreter.eval({"mode": "sha256", "message": "616263"}) == hashlib.sha256(b"abc").hexdigest()


def test_interpreter_supports_bitvector_slice_concat_and_state_transition() -> None:
    bit_ir = {
        "schema_version": 1,
        "target": "nibble_pack",
        "inputs": {"value": {"type": "bitvector", "width": 8}, "swap": {"type": "bool"}},
        "outputs": {"expected": {"type": "int"}},
        "rules": [
            {
                "id": "upper",
                "when": {"field": "swap"},
                "assign": {
                    "expected": {
                        "concat": [
                            {"slice": {"value": {"field": "value"}, "msb": 3, "lsb": 0}},
                            {"slice": {"value": {"field": "value"}, "msb": 7, "lsb": 4}},
                        ]
                    }
                },
            },
            {
                "id": "identity",
                "when": {"not": {"field": "swap"}},
                "assign": {"expected": {"field": "value"}},
            },
        ],
        "externs": {},
        "verification": {"required": True},
        "metadata": {},
    }
    state_ir = _state_transition_ir()
    state_report = verify_ref_model_ir(state_ir, ref_model_plan=_state_transition_plan())
    interpreter = RefModelInterpreter(state_ir)

    assert verify_ref_model_ir(bit_ir).passed is True
    assert RefModelInterpreter(bit_ir).eval({"value": 0xA5, "swap": True}) == 0x5A
    assert RefModelInterpreter(bit_ir).eval({"value": 0xA5, "swap": False}) == 0xA5
    assert state_report.passed is True
    assert state_report.verification_level == "formally_verified"
    outputs, state = interpreter.step({"go": True})
    assert outputs["expected"] == "done"
    assert state["phase"] == "done"
    outputs, state = interpreter.step({"go": False}, state=state)
    assert outputs["expected"] == "done"
    assert state["phase"] == "done"


def test_c_abi_extern_requires_formal_model_and_can_execute(tmp_path: Path) -> None:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        pytest.skip("C compiler not available")
    source = tmp_path / "plus_one.c"
    library = tmp_path / "libplus_one.so"
    source.write_text(
        "#include <stdint.h>\nint64_t plus_one(int64_t value) { return value + 1; }\n",
        encoding="utf-8",
    )
    subprocess.run([cc, "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    blocked = _c_abi_ir("unverified", digest)
    verified = _c_abi_ir(
        "formal_model",
        digest,
        formal_model={"binary": {"op": "add", "left": {"field": "arg0"}, "right": {"literal": 1}}},
    )

    blocked_report = verify_ref_model_ir(blocked, base_dir=tmp_path)
    verified_report = verify_ref_model_ir(verified, ref_model_plan=_plus_one_plan(), base_dir=tmp_path)
    interpreter = RefModelInterpreter(verified, base_dir=tmp_path)

    assert blocked_report.passed is False
    assert any("formal_model" in issue.message for issue in blocked_report.blocked_issues)
    assert verified_report.passed is True
    assert "plus_one" in verified_report.tested_externs
    assert interpreter.eval({"value": 7}) == 8


def test_ir_feedback_loop_emits_uvm_wrapper_and_repairs_with_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "libafl_bfm_fuzz" / "py"))

    from fuzz_bfm.bfm_base import ReplayResult
    from fuzz_bfm.corpus import FuzzCase
    from fuzz_bfm.target_config import TargetConfig
    from fuzz_pipeline.generated_plugins import (
        GeneratedPluginBundle,
        apply_manifest_overlay,
        build_manifest_overlay,
        build_plugin_registry,
        validate_generated_plugin_bundle,
    )
    from fuzz_pipeline.plugin_contract_spec import current_plugin_contract_hash
    from fuzz_uvm.ref_models import build_ref_model
    from fuzz_uvm.scoreboards import ResultScoreboard
    from fuzz_uvm.transactions import ReplayRecord

    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        return {"ref_model_ir": _notgate_ir(correct=len(prompts) > 1)}

    manifest = tmp_path / "notgate.toml"
    manifest.write_text(
        "\n".join(
            [
                'name = "notgate"',
                'driver = "demo_driver:Driver"',
                "",
                "[[field]]",
                'name = "value"',
                'kind = "bool"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = generate_ref_model_ir_with_feedback(
        _notgate_plan(),
        manifest_path=manifest,
        output_dir=tmp_path / "out",
        llm_backend=CallableLLMBackend(llm),
        golden_cases=(
            {"target": "notgate", "data": {"value": False}, "expected": True},
            {"target": "notgate", "data": {"value": True}, "expected": False},
        ),
        max_attempts=2,
    )

    assert result.status == "succeeded"
    assert [attempt.status for attempt in result.attempts] == ["evaluation_failed", "succeeded"]
    assert prompts[1]["feedback"]["blocking_issues"][0]["stage"] in {"verification", "golden_case"}
    assert result.final_dir is not None
    assert (result.final_dir / "ref_model_ir.json").exists()
    assert (result.final_dir / "verification_report.json").exists()
    assert (result.final_dir / "generated" / "notgate_dsl_ref_model.py").exists()

    monkeypatch.syspath_prepend(str(result.final_dir))
    config = TargetConfig(name="notgate", driver="demo_driver:Driver", path=manifest)
    bundle = GeneratedPluginBundle(
        target="notgate",
        plugins={"ref_model": ref_model_plugin_spec("notgate")},
        metadata={"contract_hash": current_plugin_contract_hash()},
    )
    report = validate_generated_plugin_bundle(bundle, config=config)
    active = apply_manifest_overlay(config, build_manifest_overlay(build_plugin_registry(report)))
    ref_model = build_ref_model(active)
    scoreboard = ResultScoreboard(active.name, config=active)

    assert report.valid is True
    for index, (value, expected) in enumerate(((False, True), (True, False))):
        case = FuzzCase(target="notgate", data={"value": value}, line_no=index + 1)
        actual_expected = ref_model.predict(case).expected
        assert actual_expected is expected
        scoreboard.write(
            ReplayRecord(
                index=index,
                case=case,
                result=ReplayResult(actual=expected, expected=actual_expected),
            )
        )
    scoreboard.check()


def test_secworks_sha256_ir_first_trusted_standard_integrates_with_uvm_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "libafl_bfm_fuzz" / "py"))

    from fuzz_bfm.bfm_base import ReplayResult
    from fuzz_bfm.corpus import FuzzCase
    from fuzz_bfm.target_config import load_target_config
    from fuzz_pipeline.generated_plugins import (
        GeneratedPluginBundle,
        apply_manifest_overlay,
        build_manifest_overlay,
        build_plugin_registry,
        validate_generated_plugin_bundle,
    )
    from fuzz_pipeline.plugin_contract_spec import current_plugin_contract_hash
    from fuzz_uvm.ref_models import build_ref_model
    from fuzz_uvm.scoreboards import ResultScoreboard
    from fuzz_uvm.transactions import ReplayRecord

    cases = _secworks_sha256_cases()

    def llm(_prompt, _model):
        return {"ref_model_ir": _secworks_sha256_ir()}

    result = generate_ref_model_ir_with_feedback(
        _secworks_sha256_plan(),
        manifest_path=root / "libafl_bfm_fuzz" / "targets" / "secworks_sha256.toml",
        output_dir=tmp_path / "out",
        llm_backend=CallableLLMBackend(llm),
        golden_cases=tuple(cases),
        max_attempts=1,
    )

    assert result.status == "succeeded"
    assert result.final_dir is not None
    report_json = (result.final_dir / "verification_report.json").read_text(encoding="utf-8")
    assert "trusted_standard" in report_json
    monkeypatch.syspath_prepend(str(result.final_dir))
    config = load_target_config("secworks_sha256")
    bundle = GeneratedPluginBundle(
        target="secworks_sha256",
        plugins={"ref_model": ref_model_plugin_spec("secworks_sha256")},
        metadata={"contract_hash": current_plugin_contract_hash()},
    )
    report = validate_generated_plugin_bundle(bundle, config=config)
    active = apply_manifest_overlay(config, build_manifest_overlay(build_plugin_registry(report)))
    ref_model = build_ref_model(active)
    scoreboard = ResultScoreboard(active.name, config=active)

    assert report.valid is True
    for index, item in enumerate(cases):
        case = FuzzCase(target="secworks_sha256", data=dict(item["data"]), line_no=index + 1)
        expected = ref_model.predict(case).expected
        assert expected == item["expected"]
        scoreboard.write(
            ReplayRecord(
                index=index,
                case=case,
                result=ReplayResult(actual=item["expected"], expected=expected),
            )
        )
    scoreboard.check()
    assert scoreboard.summary()["failures"] == 0


def _notgate_ir(*, correct: bool = True, field_name: str = "value") -> dict:
    expr = {"not": {"field": field_name}} if correct else {"field": field_name}
    return {
        "schema_version": 1,
        "target": "notgate",
        "inputs": {"value": {"type": "bool"}},
        "outputs": {"expected": {"type": "bool"}},
        "rules": [
            {
                "id": "ref_rule_1",
                "source_rule_id": "ref_rule_1",
                "assign": {"expected": expr},
            }
        ],
        "externs": {},
        "verification": {"required": True},
        "metadata": {},
    }


def _notgate_plan() -> dict:
    return {
        "schema_version": 1,
        "target": "notgate",
        "status": "ready",
        "summary": {"rule_count": 1},
        "rules": [
            {
                "id": "ref_rule_1",
                "kind": "assignment",
                "target": {"kind": "field", "name": "expected"},
                "value": {"kind": "unary_op", "op": "not", "operand": {"kind": "field", "name": "value"}},
            }
        ],
    }


def _state_transition_ir() -> dict:
    return {
        "schema_version": 1,
        "target": "phase_fsm",
        "inputs": {"go": {"type": "bool"}},
        "state": {"phase": {"type": "enum", "choices": ["idle", "done"], "initial": "idle"}},
        "outputs": {"expected": {"type": "string"}},
        "rules": [],
        "step_rules": [
            {
                "id": "ref_rule_1",
                "source_rule_id": "ref_rule_1",
                "when": {"and": [{"eq": [{"state": "phase"}, {"literal": "idle"}]}, {"field": "go"}]},
                "assign": {"expected": {"literal": "done"}},
                "state_updates": {"phase": {"literal": "done"}},
            },
            {
                "id": "hold_idle",
                "when": {"and": [{"eq": [{"state": "phase"}, {"literal": "idle"}]}, {"not": {"field": "go"}}]},
                "assign": {"expected": {"literal": "idle"}},
                "state_updates": {"phase": {"literal": "idle"}},
            },
            {
                "id": "hold_done",
                "when": {"eq": [{"state": "phase"}, {"literal": "done"}]},
                "assign": {"expected": {"literal": "done"}},
                "state_updates": {"phase": {"literal": "done"}},
            },
        ],
        "externs": {},
        "verification": {"required": True},
        "metadata": {},
    }


def _state_transition_plan() -> dict:
    return {
        "schema_version": 1,
        "target": "phase_fsm",
        "status": "ready",
        "summary": {"rule_count": 1},
        "rules": [
            {
                "id": "ref_rule_1",
                "kind": "state_transition",
                "from": {"kind": "literal", "value": "idle"},
                "to": {"kind": "literal", "value": "done"},
                "condition": {"kind": "field", "name": "go"},
            }
        ],
    }


def _mode_ir(rules: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "target": "mode_demo",
        "inputs": {"mode": {"type": "enum", "choices": ["sha224", "sha256"]}},
        "outputs": {"expected": {"type": "string"}},
        "rules": rules,
        "externs": {},
        "verification": {"required": True},
        "metadata": {},
    }


def _sha_ir() -> dict:
    return {
        "schema_version": 1,
        "target": "secworks_sha256",
        "inputs": {
            "mode": {"type": "enum", "choices": ["sha256"]},
            "message": {"type": "hex_string"},
        },
        "outputs": {"expected": {"type": "hex_string"}},
        "rules": [
            {
                "id": "sha256_digest",
                "source_rule_id": "sha256_digest",
                "assign": {
                    "expected": {
                        "extern_call": "std.sha256",
                        "args": [{"decode_hex": {"field": "message"}}],
                        "result": "hex",
                    }
                },
            }
        ],
        "externs": {
            "std.sha256": {
                "kind": "python_stdlib",
                "binding": "hashlib.sha256",
                "verification_policy": "trusted_standard",
                "standard_name": "FIPS 180-4",
                "implementation": "python hashlib",
                "version": "stdlib",
                "artifact_sha256": "python-stdlib",
                "pure": True,
                "deterministic": True,
            }
        },
        "verification": {"required": True},
        "metadata": {},
    }


def _sha_plan() -> dict:
    return {
        "schema_version": 1,
        "target": "secworks_sha256",
        "status": "ready",
        "summary": {"rule_count": 1},
        "rules": [
            {
                "id": "sha256_digest",
                "kind": "operation_relation",
                "operation": "sha256",
                "operands": [{"kind": "field", "name": "message", "encoding": "hex"}],
                "result": {"kind": "field", "name": "expected", "encoding": "hex"},
            }
        ],
    }


def _secworks_sha256_cases() -> list[dict]:
    def expected(mode: str, message_hex: str) -> str:
        return getattr(hashlib, mode)(bytes.fromhex(message_hex)).hexdigest()

    return [
        {
            "case_id": "sha256_empty",
            "target": "secworks_sha256",
            "data": {"mode": "sha256", "message": ""},
            "expected": expected("sha256", ""),
        },
        {
            "case_id": "sha256_abc",
            "target": "secworks_sha256",
            "data": {"mode": "sha256", "message": "616263"},
            "expected": expected("sha256", "616263"),
        },
        {
            "case_id": "sha224_empty",
            "target": "secworks_sha256",
            "data": {"mode": "sha224", "message": ""},
            "expected": expected("sha224", ""),
        },
        {
            "case_id": "sha224_abc",
            "target": "secworks_sha256",
            "data": {"mode": "sha224", "message": "616263"},
            "expected": expected("sha224", "616263"),
        },
    ]


def _secworks_sha256_plan() -> dict:
    return {
        "schema_version": 1,
        "target": "secworks_sha256",
        "status": "ready",
        "summary": {"rule_count": 2, "blocked_item_count": 0},
        "rules": [
            {
                "id": "sha224_digest",
                "kind": "operation_relation",
                "semantic_element_id": "sem_sha224_digest",
                "claim_ids": ["claim_sha224_digest"],
                "operation": "sha224",
                "operands": [{"kind": "field", "name": "message", "encoding": "hex"}],
                "result": {"kind": "field", "name": "expected", "encoding": "hex"},
            },
            {
                "id": "sha256_digest",
                "kind": "operation_relation",
                "semantic_element_id": "sem_sha256_digest",
                "claim_ids": ["claim_sha256_digest"],
                "operation": "sha256",
                "operands": [{"kind": "field", "name": "message", "encoding": "hex"}],
                "result": {"kind": "field", "name": "expected", "encoding": "hex"},
            },
        ],
        "blocked_items": [],
        "not_applicable": [],
    }


def _secworks_sha256_ir() -> dict:
    return {
        "schema_version": 1,
        "target": "secworks_sha256",
        "inputs": {
            "mode": {"type": "enum", "choices": ["sha224", "sha256"]},
            "message": {"type": "hex_string"},
        },
        "outputs": {"expected": {"type": "hex_string"}},
        "rules": [
            {
                "id": "sha224_digest",
                "source_rule_id": "sha224_digest",
                "when": {"eq": [{"field": "mode"}, {"literal": "sha224"}]},
                "assign": {
                    "expected": {
                        "extern_call": "std.sha224",
                        "args": [{"decode_hex": {"field": "message"}}],
                        "result": "hex",
                    }
                },
            },
            {
                "id": "sha256_digest",
                "source_rule_id": "sha256_digest",
                "when": {"eq": [{"field": "mode"}, {"literal": "sha256"}]},
                "assign": {
                    "expected": {
                        "extern_call": "std.sha256",
                        "args": [{"decode_hex": {"field": "message"}}],
                        "result": "hex",
                    }
                },
            },
        ],
        "externs": {
            "std.sha224": {
                "kind": "python_stdlib",
                "binding": "hashlib.sha224",
                "verification_policy": "trusted_standard",
                "standard_name": "FIPS 180-4",
                "implementation": "python hashlib",
                "version": "stdlib",
                "artifact_sha256": "python-stdlib",
                "pure": True,
                "deterministic": True,
            },
            "std.sha256": {
                "kind": "python_stdlib",
                "binding": "hashlib.sha256",
                "verification_policy": "trusted_standard",
                "standard_name": "FIPS 180-4",
                "implementation": "python hashlib",
                "version": "stdlib",
                "artifact_sha256": "python-stdlib",
                "pure": True,
                "deterministic": True,
            },
        },
        "verification": {"required": True},
        "metadata": {},
    }


def _c_abi_ir(policy: str, artifact_sha256: str, *, formal_model: dict | None = None) -> dict:
    extern = {
        "kind": "c_abi",
        "library": "libplus_one.so",
        "function": "plus_one",
        "arg_codecs": ["int64"],
        "return_codec": "int64",
        "artifact_sha256": artifact_sha256,
        "verification_policy": policy,
        "timeout_ms": 1000,
        "conformance_cases": [{"args": [1], "expected": 2}],
        "pure": True,
        "deterministic": True,
    }
    if formal_model is not None:
        extern["formal_model"] = formal_model
    return {
        "schema_version": 1,
        "target": "plus_one",
        "inputs": {"value": {"type": "int", "min": 0, "max": 16}},
        "outputs": {"expected": {"type": "int"}},
        "rules": [
            {
                "id": "ref_rule_1",
                "source_rule_id": "ref_rule_1",
                "assign": {"expected": {"extern_call": "plus_one", "args": [{"field": "value"}]}},
            }
        ],
        "externs": {"plus_one": extern},
        "verification": {"required": True},
        "metadata": {},
    }


def _plus_one_plan() -> dict:
    return {
        "schema_version": 1,
        "target": "plus_one",
        "status": "ready",
        "summary": {"rule_count": 1},
        "rules": [
            {
                "id": "ref_rule_1",
                "kind": "assignment",
                "target": {"kind": "field", "name": "expected"},
                "value": {
                    "kind": "binary_op",
                    "op": "add",
                    "left": {"kind": "field", "name": "value"},
                    "right": {"kind": "literal", "value": 1},
                },
            }
        ],
    }
