from __future__ import annotations

import hashlib
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
    dynamic_import = GeneratedFileBundle.from_dict(
        {
            "files": [{"path": "generated/ref_model.py", "content": "__import__('subprocess')\n"}],
            "metadata": {"ref_model": "generated.ref_model:RefModel"},
        }
    )
    good = _valid_bundle(expected_expr="case.data.get('value')")

    assert static_validation_issues(bad)
    assert static_validation_issues(dynamic_import)
    assert not static_validation_issues(good)


def test_ref_model_contract_and_golden_case_validation_passes(tmp_path: Path) -> None:
    task = _task(tmp_path)
    bundle = _valid_bundle(expected_expr="case.data.get('value')")
    candidate_dir = tmp_path / "candidate"
    task.write_candidate(bundle, candidate_dir)

    evaluation = task.evaluate(bundle, candidate_dir)

    assert evaluation.passed is True
    assert not evaluation.issues
    assert evaluation.metadata["execution"] == "subprocess"
    assert (tmp_path / "evaluation_worker_report.json").exists()


def test_ref_model_evaluation_times_out_in_subprocess(tmp_path: Path) -> None:
    task = RefModelCodegenTask(
        ref_model_plan=_plan(),
        manifest_path=_manifest(tmp_path),
        spec_paths=(_spec(tmp_path),),
        golden_cases=(_golden_case(),),
        evaluation_timeout_s=0.5,
    )
    bundle = GeneratedFileBundle.from_dict(
        {
            "files": [
                {"path": "generated/__init__.py", "content": ""},
                {
                    "path": "generated/demo_ref_model.py",
                    "content": "\n".join(
                        [
                            "import time",
                            "from fuzz_uvm.contracts import ExpectedResult",
                            "",
                            "class GeneratedRefModel:",
                            "    def __init__(self, target=None, config=None):",
                            "        self.target = target",
                            "        self.config = config",
                            "",
                            "    def predict(self, case):",
                            "        time.sleep(10)",
                            "        return ExpectedResult(expected=case.data.get('value'))",
                            "",
                        ]
                    ),
                },
            ],
            "metadata": {"ref_model": "generated.demo_ref_model:GeneratedRefModel"},
        }
    )
    candidate_dir = tmp_path / "candidate"
    task.write_candidate(bundle, candidate_dir)

    evaluation = task.evaluate(bundle, candidate_dir)

    assert evaluation.passed is False
    assert evaluation.metadata["stage"] == "worker"
    assert evaluation.metadata["execution"] == "subprocess"
    assert "timed out" in evaluation.issues[0].message


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


def test_secworks_sha256_feedback_codegen_integrates_with_uvm_plugin_stack(
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
    prompts = []

    def llm(prompt, _model):
        prompts.append(prompt)
        if len(prompts) == 1:
            return _secworks_sha256_bundle(expected_expr="'bad-digest'")
        return _secworks_sha256_bundle(
            expected_expr=(
                "hashlib.sha224(message).hexdigest() "
                "if mode == 'sha224' else hashlib.sha256(message).hexdigest()"
            ),
            extra_lines=("import hashlib",),
            setup_lines=(
                "        mode = str(case.data['mode'])",
                "        message = bytes.fromhex(str(case.data['message']))",
                "        if mode not in {'sha224', 'sha256'}:",
                "            raise ValueError(f'unsupported SHA mode={mode!r}')",
            ),
        )

    spec = tmp_path / "secworks_sha256_ref_model_spec.md"
    spec.write_text(
        "Decode case.data['message'] as hex bytes and return SHA-224/SHA-256 digest hex.\n",
        encoding="utf-8",
    )
    result = generate_ref_model_with_feedback(
        _secworks_sha256_plan(),
        manifest_path=root / "libafl_bfm_fuzz" / "targets" / "secworks_sha256.toml",
        spec_paths=(spec,),
        output_dir=tmp_path / "out",
        llm_backend=CallableLLMBackend(llm),
        golden_cases=tuple(cases),
        max_attempts=2,
    )

    assert result.status == "succeeded"
    assert [attempt.status for attempt in result.attempts] == ["evaluation_failed", "succeeded"]
    assert prompts[1]["feedback"]["blocking_issues"][0]["stage"] == "golden_case"

    final_dir = result.final_dir
    assert final_dir is not None
    monkeypatch.syspath_prepend(str(final_dir))
    ref_spec = json.loads((final_dir / "bundle.json").read_text(encoding="utf-8"))["metadata"]["ref_model"]
    config = load_target_config("secworks_sha256")
    plugin_bundle = GeneratedPluginBundle(
        target="secworks_sha256",
        plugins={"ref_model": ref_spec},
        metadata={"contract_hash": current_plugin_contract_hash()},
    )
    report = validate_generated_plugin_bundle(plugin_bundle, config=config)
    registry = build_plugin_registry(report)
    overlay = build_manifest_overlay(registry)
    active_config = apply_manifest_overlay(config, overlay)
    ref_model = build_ref_model(active_config)
    scoreboard = ResultScoreboard(active_config.name, config=active_config)

    assert report.valid is True
    assert active_config.ref_model == ref_spec
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
    assert (output_dir / "ref_model_plan.json").exists()
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


def _secworks_sha256_bundle(
    *,
    expected_expr: str,
    extra_lines: tuple[str, ...] = (),
    setup_lines: tuple[str, ...] = (),
) -> dict:
    content_lines = [
        *extra_lines,
        "from fuzz_uvm.contracts import ExpectedResult",
        "",
        "class GeneratedRefModel:",
        "    def __init__(self, target=None, config=None):",
        "        self.target = target",
        "        self.config = config",
        "",
        "    def predict(self, case):",
        *setup_lines,
        f"        return ExpectedResult(expected={expected_expr})",
        "",
    ]
    return {
        "files": [
            {"path": "generated/__init__.py", "content": ""},
            {
                "path": "generated/secworks_sha256_ref_model.py",
                "content": "\n".join(content_lines),
            },
        ],
        "metadata": {"ref_model": "generated.secworks_sha256_ref_model:GeneratedRefModel"},
        "assumptions": [],
    }
