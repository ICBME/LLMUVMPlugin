from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_pipeline import RunPlan, RunPlanExecutor, RunStage


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
