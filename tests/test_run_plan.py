from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_pipeline import RunPlan, RunPlanExecutor, RunStage, StepPolicy  # noqa: E402
from harness_optimization.planning import (  # noqa: E402
    apply_stage_policies,
    result_stage,
    zero_arg_stage,
)


def test_run_plan_records_ordered_results_and_merged_mappings() -> None:
    calls: list[str] = []

    def topology() -> None:
        calls.append("topology")

    plan = RunPlan(
        name="smoke",
        stages=(
            RunStage(
                name="first",
                handler=lambda _results: calls.append("first") or 1,
            ),
            RunStage(
                name="merged",
                handler=lambda results: {"second": results["first"] + 1},
                merge_mapping=True,
            ),
            RunStage(
                name="none",
                handler=lambda _results: None,
            ),
            RunStage(
                name="kept_none",
                handler=lambda _results: None,
                store_none=True,
            ),
        ),
    )

    results = RunPlanExecutor(write_topology=topology).run(plan)

    assert calls == ["topology", "first"]
    assert results == {"first": 1, "second": 2, "kept_none": None}


def test_shared_planning_stage_helpers_build_run_stages() -> None:
    plan = RunPlan(
        name="helpers",
        stages=(
            zero_arg_stage("first", lambda: 1),
            zero_arg_stage(
                "merged",
                lambda: {"second": 2},
                merge_mapping=True,
                produces_results=("second",),
            ),
            result_stage(
                "sum",
                lambda results: results["first"] + results["second"],
                result_key="total",
            ),
        ),
        write_topology=False,
    )

    results = RunPlanExecutor().run(plan)

    assert results == {"first": 1, "second": 2, "total": 3}


def test_shared_planning_apply_stage_policies_updates_known_stages_only() -> None:
    stages = (
        zero_arg_stage("first", lambda: 1),
        zero_arg_stage("second", lambda: 2),
    )

    updated = apply_stage_policies(
        stages,
        {"second": StepPolicy(fail_main_on_step_error=False, timeout_s=3.0)},
        profile_name="demo",
    )

    assert updated[0].policy == stages[0].policy
    assert updated[1].policy.fail_main_on_step_error is False
    assert updated[1].policy.timeout_s == 3.0


def test_shared_planning_apply_stage_policies_rejects_unknown_stage_names() -> None:
    try:
        apply_stage_policies(
            (zero_arg_stage("first", lambda: 1),),
            {"missing": StepPolicy(fail_main_on_step_error=False)},
            profile_name="demo",
            plan_label="campaign",
        )
    except ValueError as exc:
        assert "campaign plan profile 'demo' declares policy for unknown stage" in str(
            exc
        )
    else:
        raise AssertionError("unknown stage policy should fail")


def test_run_plan_can_skip_topology_write() -> None:
    calls: list[str] = []
    plan = RunPlan(
        name="no_topology",
        stages=(RunStage(name="stage", handler=lambda _results: "ok"),),
        write_topology=False,
    )

    results = RunPlanExecutor(write_topology=lambda: calls.append("topology")).run(plan)

    assert calls == []
    assert results == {"stage": "ok"}


def test_run_plan_accepts_initial_results() -> None:
    plan = RunPlan(
        name="seeded",
        stages=(
            RunStage(
                name="derived",
                handler=lambda results: f"{results['seed']}-derived",
            ),
        ),
        write_topology=False,
    )

    results = RunPlanExecutor().run(plan, initial_results={"seed": "value"})

    assert results == {"seed": "value", "derived": "value-derived"}


def test_run_plan_rejects_missing_result_contract_before_execution() -> None:
    calls: list[str] = []
    plan = RunPlan(
        name="missing_result",
        stages=(
            RunStage(
                name="needs_missing",
                handler=lambda _results: calls.append("ran"),
                requires_results=("missing",),
            ),
        ),
        write_topology=False,
    )

    try:
        RunPlanExecutor().run(plan)
    except ValueError as exc:
        assert "missing required result key" in str(exc)
    else:
        raise AssertionError("missing result contract should fail before execution")

    assert calls == []


def test_run_plan_rejects_missing_artifact_role_before_execution() -> None:
    calls: list[str] = []
    plan = RunPlan(
        name="missing_artifact",
        stages=(
            RunStage(
                name="needs_corpus",
                handler=lambda _results: calls.append("ran"),
                input_roles=("corpus",),
            ),
        ),
        write_topology=False,
    )

    try:
        RunPlanExecutor().run(plan)
    except ValueError as exc:
        assert "missing required artifact role" in str(exc)
    else:
        raise AssertionError("missing artifact role should fail before execution")

    assert calls == []


def test_run_plan_rejects_duplicate_result_keys() -> None:
    plan = RunPlan(
        name="duplicate_results",
        stages=(
            RunStage(
                name="first",
                handler=lambda _results: "first",
                result_key="shared",
            ),
            RunStage(
                name="second",
                handler=lambda _results: "second",
                result_key="shared",
            ),
        ),
        write_topology=False,
    )

    try:
        RunPlanExecutor().run(plan)
    except ValueError as exc:
        assert "would overwrite result key" in str(exc)
    else:
        raise AssertionError("duplicate result keys should fail before execution")


def test_run_plan_rejects_duplicate_result_keys_within_stage_contract() -> None:
    plan = RunPlan(
        name="duplicate_stage_contract",
        stages=(
            RunStage(
                name="bad_stage",
                handler=lambda _results: {"shared": 1},
                merge_mapping=True,
                produces_results=("shared", "shared"),
            ),
        ),
        write_topology=False,
    )

    try:
        RunPlanExecutor().run(plan)
    except ValueError as exc:
        assert "duplicate produced result key" in str(exc)
    else:
        raise AssertionError("duplicate produced result keys should fail")


def test_run_plan_rechecks_runtime_required_results() -> None:
    calls: list[str] = []
    plan = RunPlan(
        name="runtime_missing",
        stages=(
            RunStage(
                name="maybe",
                handler=lambda _results: calls.append("maybe")
                or (_ for _ in ()).throw(RuntimeError("boom")),
                produces_results=("maybe",),
                policy=StepPolicy(fail_main_on_step_error=False),
            ),
            RunStage(
                name="needs_maybe",
                handler=lambda _results: calls.append("needs"),
                requires_results=("maybe",),
            ),
        ),
        write_topology=False,
    )

    try:
        RunPlanExecutor().run(plan)
    except ValueError as exc:
        assert "missing runtime result key" in str(exc)
    else:
        raise AssertionError("missing runtime result should fail before dependent stage")

    assert calls == ["maybe"]


def test_run_plan_policy_can_continue_after_missing_runtime_dependency() -> None:
    plan = RunPlan(
        name="fail_open_runtime_missing",
        stages=(
            RunStage(
                name="maybe",
                handler=lambda _results: None,
                produces_results=("missing",),
                policy=StepPolicy(fail_main_on_step_error=False),
            ),
            RunStage(
                name="optional_downstream",
                handler=lambda _results: "unused",
                requires_results=("missing",),
                policy=StepPolicy(fail_main_on_step_error=False),
            ),
            RunStage(name="after", handler=lambda _results: "ok"),
        ),
        write_topology=False,
    )

    results = RunPlanExecutor().run(plan)

    assert results["after"] == "ok"
    assert results["stage_errors"][0]["stage"] == "maybe"
    assert "did not produce declared result key" in results["stage_errors"][0]["message"]
    assert results["stage_errors"][1]["stage"] == "optional_downstream"
    assert "missing runtime result key" in results["stage_errors"][1]["message"]


def test_run_plan_rejects_missing_declared_output_by_default() -> None:
    plan = RunPlan(
        name="missing_output",
        stages=(
            RunStage(
                name="maybe",
                handler=lambda _results: None,
                produces_results=("maybe",),
            ),
        ),
        write_topology=False,
    )

    try:
        RunPlanExecutor().run(plan)
    except ValueError as exc:
        assert "did not produce declared result key" in str(exc)
    else:
        raise AssertionError("missing declared output should fail by default")


def test_run_plan_policy_can_continue_after_missing_declared_output() -> None:
    plan = RunPlan(
        name="fail_open_missing_output",
        stages=(
            RunStage(
                name="maybe",
                handler=lambda _results: None,
                produces_results=("maybe",),
                policy=StepPolicy(fail_main_on_step_error=False),
            ),
            RunStage(name="after", handler=lambda _results: "ok"),
        ),
        write_topology=False,
    )

    results = RunPlanExecutor().run(plan)

    assert results["after"] == "ok"
    assert results["stage_errors"][0]["stage"] == "maybe"
    assert "did not produce declared result key" in results["stage_errors"][0]["message"]


def test_run_plan_policy_can_continue_after_stage_error() -> None:
    plan = RunPlan(
        name="fail_open",
        stages=(
            RunStage(
                name="optional_probe",
                handler=lambda _results: (_ for _ in ()).throw(RuntimeError("boom")),
                policy=StepPolicy(fail_main_on_step_error=False),
            ),
            RunStage(name="after", handler=lambda _results: "ok"),
        ),
        write_topology=False,
    )

    results = RunPlanExecutor().run(plan)

    assert results["after"] == "ok"
    assert results["stage_errors"] == [
        {
            "stage": "optional_probe",
            "type": "RuntimeError",
            "message": "boom",
        }
    ]
