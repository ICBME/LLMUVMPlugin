from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

import pytest

from LLMPlugin import LLMResponse

from Spec2Backend.Spec2IR import generate_semantic_spec_ir, semantic_ir_patch_from_candidate

from .adapters import augment_semantic_ir_interface_context, materialize_verilogeval_case
from .datasets import RealDataCase, load_verilogeval_cases, verilogeval_root_from_env
from .metrics import aggregate_results
from .runner import RealDataLLMRuntimeError, run_verilogeval_case, run_verilogeval_cases


pytestmark = pytest.mark.real_data


def _available_cases(*, limit: int | None = None, case_ids: set[str] | None = None) -> list[RealDataCase]:
    root = verilogeval_root_from_env()
    if not root.exists():
        pytest.skip(f"VerilogEval dataset root is not available: {root}")
    cases = load_verilogeval_cases(root, limit=limit, case_ids=case_ids)
    if not cases:
        pytest.skip(f"no VerilogEval cases found under {root}")
    return cases


def test_verilogeval_groups_prompt_ref_test_files() -> None:
    cases = _available_cases()

    by_id = {case.case_id: case for case in cases}
    assert "Prob031_dff" in by_id
    assert by_id["Prob031_dff"].prompt_path.name == "Prob031_dff_prompt.txt"
    assert by_id["Prob031_dff"].ref_path is not None
    assert by_id["Prob031_dff"].ref_path.name == "Prob031_dff_ref.sv"
    assert by_id["Prob031_dff"].test_path is not None
    assert by_id["Prob031_dff"].test_path.name == "Prob031_dff_test.sv"


def test_adapter_prefers_reference_rtl_ports_for_dff_prompt_mismatch() -> None:
    case = _available_cases(case_ids={"Prob031_dff"})[0]
    with tempfile.TemporaryDirectory() as tmp:
        materialized = materialize_verilogeval_case(case, tmp)
        manifest_text = materialized.manifest_path.read_text(encoding="utf-8")

    ports_by_name = {port.name: port for port in materialized.interface_ports}
    assert ports_by_name["q"].direction == "output"
    assert 'name = "d"' in manifest_text
    assert 'name = "q"' not in manifest_text


def test_adapter_rewrites_generic_port_language_for_behavior_claims() -> None:
    case = _available_cases(case_ids={"Prob006_vectorr"})[0]
    with tempfile.TemporaryDirectory() as tmp:
        materialized = materialize_verilogeval_case(case, tmp)
        spec_text = materialized.spec_path.read_text(encoding="utf-8")

    assert "input port" not in spec_text.lower()
    assert "output port" not in spec_text.lower()
    assert "reverse the bit ordering" in spec_text


def test_adapter_injects_typed_input_and_output_interface_symbols() -> None:
    case = _available_cases(case_ids={"Prob004_vector2"})[0]
    with tempfile.TemporaryDirectory() as tmp:
        materialized = materialize_verilogeval_case(case, tmp)
        semantic_ir = generate_semantic_spec_ir(
            manifest_path=materialized.manifest_path,
            spec_paths=[materialized.spec_path],
            target=materialized.target,
        )
        augment_semantic_ir_interface_context(semantic_ir, materialized.interface_ports)

    symbols = {
        symbol["name"]: symbol
        for symbol in semantic_ir["semantic_context"]["symbols"]
    }
    assert symbols["in"]["type"] == {"kind": "bitvector", "width": 32}
    assert symbols["out"]["type"] == {"kind": "bitvector", "width": 32}
    assert symbols["out"]["direction"] == "output"


def test_adapter_handles_prompt_only_case_without_reference(tmp_path: Path) -> None:
    prompt = tmp_path / "Demo_prompt.txt"
    prompt.write_text(
        "I would like you to implement a module named TopModule.\n"
        "The module should always outputs a LOW.\n",
        encoding="utf-8",
    )
    case = RealDataCase(
        dataset="verilogeval",
        case_id="Demo",
        prompt_path=prompt,
    )

    materialized = materialize_verilogeval_case(case, tmp_path / "work")

    assert materialized.manifest_path.exists()
    assert materialized.spec_path.exists()
    assert materialized.interface_ports == ()


def test_verilogeval_offline_smoke_runs_without_crashing() -> None:
    cases = _available_cases(
        case_ids={
            "Prob001_zero",
            "Prob005_notgate",
            "Prob006_vectorr",
            "Prob008_m2014_q4h",
            "Prob023_vector100r",
            "Prob031_dff",
        }
    )
    with tempfile.TemporaryDirectory() as tmp:
        results = [
            run_verilogeval_case(case, work_root=tmp)
            for case in sorted(cases, key=lambda item: item.case_id)
        ]

    assert results
    assert all(result.status != "crashed" for result in results)
    assert all(result.schema_valid for result in results)
    assert all(result.review is not None for result in results)
    assert all(result.readiness is not None for result in results)


def test_verilogeval_aggregate_metrics_are_reported(spec2ir_real_data_limit: int) -> None:
    limit = max(1, spec2ir_real_data_limit)
    sampled = _available_cases(limit=limit)
    risk_cases = _available_cases(case_ids={"Prob006_vectorr", "Prob008_m2014_q4h", "Prob023_vector100r"})
    cases_by_id = {case.case_id: case for case in [*sampled, *risk_cases]}
    cases = [cases_by_id[key] for key in sorted(cases_by_id)]
    with tempfile.TemporaryDirectory() as tmp:
        results = run_verilogeval_cases(cases, work_root=tmp)
    metrics = aggregate_results(results)

    assert metrics["processed_count"] == len(cases)
    assert metrics["crash_count"] == 0
    assert metrics["schema_valid_rate"] > 0.0
    assert metrics["claim_extraction_rate"] > 0.0
    assert "review_status_counts" in metrics
    assert "readiness_status_counts" in metrics
    assert "agent_model_call_count" in metrics
    assert "agent_no_progress_submission_count" in metrics
    assert "agent_regressive_submission_count" in metrics
    assert "agent_patch_count" in metrics
    assert "agent_patch_accepted_count" in metrics
    assert "agent_context_char_count" in metrics
    assert "agent_input_token_count" in metrics
    assert metrics["human_escalation_count"] <= metrics["processed_count"]
    assert metrics["stage_human_signal_count"] >= metrics["human_escalation_count"]


def test_verilogeval_llm_mode_reports_missing_backend() -> None:
    case = _available_cases(limit=1)[0]
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(RealDataLLMRuntimeError, match="backend setup failed"):
            run_verilogeval_case(
                case,
                work_root=tmp,
                with_llm=True,
                backend_name="missing_spec2ir_real_data_backend",
            )


def test_verilogeval_llm_mode_rejects_backend_without_response() -> None:
    class EmptyBackend:
        name = "empty"

        def invoke(self, request):  # noqa: ANN001 - protocol-shaped test double
            return None

    case = _available_cases(limit=1)[0]
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(RealDataLLMRuntimeError, match="returned no response"):
            run_verilogeval_case(
                case,
                work_root=tmp,
                with_llm=True,
                llm_backend=EmptyBackend(),
            )


def test_verilogeval_llm_agent_uses_previous_attempt_context_for_repair() -> None:
    class ContextAwareBackend:
        name = "context-aware"

        def __init__(self, semantic_patch: dict):
            self.semantic_patch = semantic_patch
            self.prompts: list[dict] = []

        def invoke(self, request):  # noqa: ANN001 - protocol-shaped test double
            self.prompts.append(request.prompt)
            if len(self.prompts) == 1:
                return LLMResponse(
                    content='{"action": "submit_semantic_spec_ir", "semantic_spec_ir": {"not": "semantic ir"}}'
                )
            history = request.prompt["attempt_history"]
            assert history
            assert history[0]["status"] == "llm_invalid_response"
            assert history[0]["harness_result"]["error"]["type"] == "ValueError"
            current_patch = json.loads(json.dumps(self.semantic_patch))
            current_patch["base_revision"] = request.prompt["observation"]["artifact"]["revision"]
            current_patch["base_sha256"] = request.prompt["observation"]["artifact"]["sha256"]
            return LLMResponse(
                content=json.dumps(
                    {"action": "apply_semantic_ir_patch", "patch": current_patch},
                    sort_keys=True,
                )
            )

    case = _available_cases(limit=1)[0]
    with tempfile.TemporaryDirectory() as tmp:
        materialized = materialize_verilogeval_case(case, Path(tmp) / "materialized")
        semantic_ir = generate_semantic_spec_ir(
            manifest_path=materialized.manifest_path,
            spec_paths=[materialized.spec_path],
            target=materialized.target,
        )
        fixed_ir = json.loads(json.dumps(semantic_ir))
        fixed_ir["semantic_elements"][0].update(
            {
                "formalization_status": "formalized",
                "kind": "combinational_behavior",
                "subjects": ["zero"],
                "summary": "The zero output is continuously LOW.",
                "representation": {
                    "ast": {
                        "node": "assignment",
                        "target": {"node": "signal_ref", "name": "zero"},
                        "value": {"node": "literal", "value": False, "width": 1},
                    },
                    "ast_version": 2,
                    "kind": "combinational_relation",
                    "subjects": ["zero"],
                    "text": "The zero output is always assigned logic LOW.",
                },
            }
        )
        fixed_ir["review"]["status"] = "draft"
        semantic_patch = semantic_ir_patch_from_candidate(semantic_ir, fixed_ir, revision=0)
        backend = ContextAwareBackend(semantic_patch)
        result = run_verilogeval_case(
            case,
            work_root=Path(tmp) / "run",
            with_llm=True,
            llm_backend=backend,
            agent_max_attempts=2,
        )

    assert result.status == "passed"
    assert result.repair_result is not None
    assert result.repair_result["attempt_count"] == 2
    assert [attempt["status"] for attempt in result.repair_result["attempts"]] == [
        "llm_invalid_response",
        "repaired",
    ]
    assert result.repair_result["status"] == "repaired"
    assert len(backend.prompts) == 2
    assert backend.prompts[1]["attempt_history"][0]["status"] == "llm_invalid_response"
    trace_payload = result.to_dict(include_agent_trace=True)
    assert trace_payload["agent_trace"]["attempts"]
    assert trace_payload["agent_trace"]["events"]
    assert trace_payload["agent_trace"]["review_history"]
    assert any(stage.name == "llm_agent" and stage.status == "passed" for stage in result.stages)


@pytest.mark.llm
@pytest.mark.slow
def test_verilogeval_optional_llm_smoke() -> None:
    if os.environ.get("SPEC2IR_REALDATA_ENABLE_LLM", "0") != "1":
        pytest.skip("set SPEC2IR_REALDATA_ENABLE_LLM=1 to enable real LLM smoke testing")
    model = os.environ.get("SPEC2IR_REALDATA_LLM_MODEL")
    cases = _available_cases(limit=1)
    with tempfile.TemporaryDirectory() as tmp:
        result = run_verilogeval_case(cases[0], work_root=tmp, with_llm=True, model=model)

    assert result.status == "passed"
    assert result.schema_valid
    assert any(
        stage.name == "llm_agent" and stage.status == "passed"
        for stage in result.stages
    )
