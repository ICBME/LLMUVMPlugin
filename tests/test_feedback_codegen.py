from __future__ import annotations

import json
from pathlib import Path

import pytest

from ConnectGraph.connector import ObservationContext
from ConnectGraph.observers import JsonlObserver
from LLMPlugin import CallableLLMBackend, LLMResponse
from Spec2Backend.FeedbackCodegen import (
    CodegenEvaluationIssue,
    CodegenLoopConfig,
    FeedbackCodegenError,
    GeneratedFileBundle,
    RefModelCodegenTask,
    generate_ref_model_with_feedback,
    generate_with_feedback,
    static_validation_issues,
)
from Spec2Backend.FeedbackCodegen.schema import write_json


def test_generated_file_bundle_rejects_path_escape_and_accepts_valid_bundle() -> None:
    bundle = GeneratedFileBundle.from_dict(
        {
            "files": [{"path": "generated/ref_model.py", "content": "class RefModel: pass\n"}],
            "metadata": {"ref_model": "generated.ref_model:RefModel"},
            "assumptions": ["demo"],
        }
    )

    assert bundle.files[0].path == "generated/ref_model.py"
    with pytest.raises(FeedbackCodegenError, match="relative"):
        GeneratedFileBundle.from_dict({"files": [{"path": "/tmp/ref.py", "content": ""}]})
    with pytest.raises(FeedbackCodegenError, match="escape"):
        GeneratedFileBundle.from_dict({"files": [{"path": "../ref.py", "content": ""}]})
    with pytest.raises(FeedbackCodegenError, match="at least one"):
        GeneratedFileBundle.from_dict({"files": []})
    with pytest.raises(FeedbackCodegenError, match="content"):
        GeneratedFileBundle.from_dict({"files": [{"path": "ref.py", "content": 1}]})
    with pytest.raises(FeedbackCodegenError, match="path"):
        GeneratedFileBundle.from_dict({"files": [{"path": 1, "content": ""}]})


def test_evaluation_issue_normalizes_non_string_json_keys() -> None:
    issue = CodegenEvaluationIssue(
        stage="golden_case",
        message="mismatch",
        expected={1: "one"},
        actual={"2": "two"},
    )

    assert issue.to_json()["expected"] == {"1": "one"}


def test_static_validation_rejects_dangerous_api_and_accepts_minimal_ref_model() -> None:
    bad = GeneratedFileBundle.from_dict(
        {
            "files": [{"path": "generated/ref_model.py", "content": "import subprocess\n"}],
            "metadata": {"ref_model": "generated.ref_model:RefModel"},
        }
    )
    good = _valid_bundle(expected_expr="case.data.get('value')")

    assert static_validation_issues(bad)
    assert not static_validation_issues(good)


def test_ref_model_contract_and_golden_case_validation_passes(tmp_path: Path) -> None:
    task = _task(tmp_path)
    bundle = _valid_bundle(expected_expr="case.data.get('value')")
    candidate_dir = tmp_path / "candidate"
    task.write_candidate(bundle, candidate_dir)

    evaluation = task.evaluate(bundle, candidate_dir)

    assert evaluation.passed is True
    assert not evaluation.issues


def test_feedback_loop_repairs_after_golden_case_mismatch(tmp_path: Path) -> None:
    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        if len(prompts) == 1:
            return _bundle_json(expected_expr="0")
        return _bundle_json(expected_expr="case.data.get('value')")

    result = generate_ref_model_with_feedback(
        _plan(),
        manifest_path=_manifest(tmp_path),
        spec_paths=(_spec(tmp_path),),
        output_dir=tmp_path / "out",
        llm_backend=CallableLLMBackend(llm),
        golden_cases=(_golden_case(),),
        max_attempts=2,
    )

    assert result.status == "succeeded"
    assert [attempt.status for attempt in result.attempts] == ["evaluation_failed", "succeeded"]
    assert prompts[1]["feedback"]["blocking_issues"][0]["stage"] == "golden_case"
    assert "expected value mismatch" in prompts[1]["feedback"]["blocking_issues"][0]["message"]
    assert (tmp_path / "out" / "final" / "generated" / "demo_ref_model.py").exists()


def test_llm_unavailable_invalid_json_invalid_bundle_and_max_attempts_statuses(tmp_path: Path) -> None:
    unavailable = generate_ref_model_with_feedback(
        _plan(),
        manifest_path=_manifest(tmp_path),
        output_dir=tmp_path / "unavailable",
        llm_backend=CallableLLMBackend(lambda _prompt, _model: None),
        golden_cases=(_golden_case(),),
        max_attempts=1,
    )
    invalid_json = generate_ref_model_with_feedback(
        _plan(),
        manifest_path=_manifest(tmp_path),
        output_dir=tmp_path / "invalid_json",
        llm_backend=CallableLLMBackend(
            lambda _prompt, _model: LLMResponse(content="not json")
        ),
        golden_cases=(_golden_case(),),
        max_attempts=1,
    )
    invalid_bundle = generate_ref_model_with_feedback(
        _plan(),
        manifest_path=_manifest(tmp_path),
        output_dir=tmp_path / "invalid_bundle",
        llm_backend=CallableLLMBackend(lambda _prompt, _model: {"files": []}),
        golden_cases=(_golden_case(),),
        max_attempts=1,
    )
    exhausted = generate_ref_model_with_feedback(
        _plan(),
        manifest_path=_manifest(tmp_path),
        output_dir=tmp_path / "exhausted",
        llm_backend=CallableLLMBackend(lambda _prompt, _model: _bundle_json(expected_expr="0")),
        golden_cases=(_golden_case(),),
        max_attempts=1,
    )

    assert unavailable.status == "llm_unavailable"
    assert invalid_json.status == "llm_invalid_response"
    assert invalid_bundle.status == "invalid_bundle"
    assert exhausted.status == "max_attempts_exhausted"


def test_connectgraph_observer_records_successful_codegen_chain(tmp_path: Path) -> None:
    events_out = tmp_path / "events.jsonl"
    observer = JsonlObserver(events_out)
    task = _task(tmp_path)
    output_dir = tmp_path / "observed"
    output_dir.mkdir()
    write_json(output_dir / "ref_model_plan.json", _plan())

    result = generate_with_feedback(
        task,
        CodegenLoopConfig(
            output_dir=output_dir,
            max_attempts=1,
            observation_context=ObservationContext(run_id="run-1", observer=observer),
        ),
        CallableLLMBackend(lambda _prompt, _model: _bundle_json(expected_expr="case.data.get('value')")),
    )
    observer.close()
    finished = {
        json.loads(line)["connector"]
        for line in events_out.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["event_type"] == "connector.finished"
    }

    assert result.status == "succeeded"
    assert {
        "plan_to_prompt",
        "prompt_to_llm_response",
        "response_to_candidate",
        "candidate_to_evaluation",
        "evaluation_to_feedback",
        "candidate_to_final_artifact",
    } <= finished


def _task(tmp_path: Path) -> RefModelCodegenTask:
    return RefModelCodegenTask(
        ref_model_plan=_plan(),
        manifest_path=_manifest(tmp_path),
        spec_paths=(_spec(tmp_path),),
        golden_cases=(_golden_case(),),
    )


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "demo.toml"
    path.write_text(
        "\n".join(
            [
                'name = "demo"',
                'driver = "demo_driver:Driver"',
                "",
                "[[field]]",
                'name = "value"',
                'kind = "int"',
                "min = 0",
                "max = 255",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _spec(tmp_path: Path) -> Path:
    path = tmp_path / "spec.md"
    path.write_text("Expected output equals the input value.\n", encoding="utf-8")
    return path


def _plan() -> dict:
    return {
        "schema_version": 1,
        "target": "demo",
        "status": "ready",
        "summary": {"rule_count": 1, "blocked_item_count": 0},
        "rules": [
            {
                "id": "ref_rule_1",
                "kind": "assignment",
                "semantic_element_id": "sem1",
                "claim_ids": ["claim1"],
                "target": {"kind": "field", "name": "expected"},
                "value": {"kind": "field", "name": "value"},
            }
        ],
        "blocked_items": [],
        "not_applicable": [],
    }


def _golden_case() -> dict:
    return {
        "case_id": "case_1",
        "target": "demo",
        "data": {"target": "demo", "value": 7},
        "expected": 7,
    }


def _valid_bundle(*, expected_expr: str) -> GeneratedFileBundle:
    return GeneratedFileBundle.from_dict(_bundle_json(expected_expr=expected_expr))


def _bundle_json(*, expected_expr: str) -> dict:
    return {
        "files": [
            {"path": "generated/__init__.py", "content": ""},
            {
                "path": "generated/demo_ref_model.py",
                "content": "\n".join(
                    [
                        "from fuzz_uvm.contracts import ExpectedResult",
                        "",
                        "class GeneratedRefModel:",
                        "    def __init__(self, target=None, config=None):",
                        "        self.target = target",
                        "        self.config = config",
                        "",
                        "    def predict(self, case):",
                        f"        return ExpectedResult(expected={expected_expr})",
                        "",
                    ]
                ),
            },
        ],
        "metadata": {"ref_model": "generated.demo_ref_model:GeneratedRefModel"},
        "assumptions": [],
    }
