from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, get_type_hints

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

import ConnectGraph as ConnectGraphRoot  # noqa: E402
import connector_observe as ConnectorObserveRoot  # noqa: E402
from connector_observe import ObservationContext  # noqa: E402
import harness_optimization.candidate_validation as shared_candidate_validation_module  # noqa: E402
from fuzz_pipeline.harness_rollup import (  # noqa: E402
    CampaignTraceRollupBuilder as FuzzCampaignTraceRollupBuilder,
)
from fuzz_pipeline.harness_evidence.analysis import (  # noqa: E402
    UvmFuzzHarnessAnalyzer,
)
from fuzz_pipeline.harness_evidence.records import (  # noqa: E402
    HarnessRecordProjector as FuzzHarnessRecordProjector,
)
from fuzz_pipeline.orchestrator import (  # noqa: E402
    PipelineOrchestrator as FuzzPipelineOrchestrator,
)
from fuzz_pipeline.topology import FULL_FUZZ_TOPOLOGY  # noqa: E402
from fuzz_pipeline.harness_candidate_regression import (  # noqa: E402
    build_candidate_promotion_package as build_fuzz_candidate_promotion_package,
    build_candidate_variant_ranking as build_fuzz_candidate_variant_ranking,
)
from fuzz_pipeline.replay_orchestrator import (  # noqa: E402
    functional_coverage_output_from_target as replay_orchestrator_functional_coverage_output_from_target,
    replay_corpus_from_env as replay_orchestrator_replay_corpus_from_env,
    replay_target_from_env as replay_orchestrator_replay_target_from_env,
)
from ConnectGraph.orchestrator import (  # noqa: E402
    PipelineOrchestrator as ConnectGraphPipelineOrchestrator,
    StepPolicy as ConnectGraphStepPolicy,
)
from harness_optimization.candidate_execution import (  # noqa: E402
    CandidateAcceptanceThresholds,
    CandidateActionAdapterContext,
    adapt_candidate_actions as adapt_shared_candidate_actions,
    load_candidate_actions as load_shared_candidate_actions,
)
from harness_optimization.candidate_validation import (  # noqa: E402
    build_candidate_action_effect_report as build_shared_candidate_action_effect_report,
    build_candidate_error_report as build_shared_candidate_error_report,
    build_candidate_gap_actionability_report as build_shared_candidate_gap_actionability_report,
    build_candidate_not_run_report as build_shared_candidate_not_run_report,
    build_candidate_promotion_package as build_shared_candidate_promotion_package,
    build_candidate_variant_ranking as build_shared_candidate_variant_ranking,
    build_gap_actionability_minimal_candidate_proposal as build_shared_gap_actionability_minimal_candidate_proposal,
    candidate_metric_snapshot as build_shared_candidate_metric_snapshot,
    combined_variant_evaluation,
)
from harness_optimization.analysis import (  # noqa: E402
    HarnessEvaluationAnalyzer as SharedHarnessEvaluationAnalyzer,
)
from harness_optimization.action_dsl import (  # noqa: E402
    builtin_action_plugin as build_shared_builtin_action_plugin,
    builtin_action_plugin_registry as build_shared_builtin_action_plugin_registry,
    builtin_safe_action_dsl_schema as build_shared_builtin_safe_action_dsl_schema,
)
from harness_optimization.evaluation import (  # noqa: E402
    CampaignEvaluationAdapter as SharedCampaignEvaluationAdapter,
    CampaignRollupAttachment,
    RunEvaluationAdapter as SharedRunEvaluationAdapter,
)
from harness_optimization.campaign_optimization import (  # noqa: E402
    CampaignOptimizationStageChain,
)
from harness_optimization.runtime import (  # noqa: E402
    HarnessRuntimeActionManager as SharedHarnessRuntimeActionManager,
    load_runtime_action_config as load_shared_runtime_action_config,
    load_runtime_action_config_from_env as load_shared_runtime_action_config_from_env,
    runtime_action_plugins_from_specs as build_shared_runtime_action_plugins,
)
from harness_optimization.rollup import (  # noqa: E402
    CampaignTraceRollupBuilder as SharedCampaignTraceRollupBuilder,
)
from harness_optimization.orchestrator import (  # noqa: E402
    PipelineContext as SharedPipelineContext,
    PipelineOrchestrator as SharedPipelineOrchestrator,
    StepPolicy as SharedStepPolicy,
    StepSpec as SharedStepSpec,
)
from harness_optimization.optimization import (  # noqa: E402
    LlmOptimizationProposalBackend as SharedLlmOptimizationProposalBackend,
    NoopOptimizationCandidateEvaluationBackend as SharedNoopOptimizationCandidateEvaluationBackend,
    OptimizationRuntimeAdapter as SharedOptimizationRuntimeAdapter,
    PromptOnlyOptimizationProposalBackend as SharedPromptOnlyOptimizationProposalBackend,
    build_optimization_advice_report as build_shared_optimization_advice_report,
    build_optimization_prompt as build_shared_optimization_prompt,
    build_optimization_task as build_shared_optimization_task,
    harness_optimization_paths as shared_harness_optimization_paths,
)
from harness_optimization.observation import (  # noqa: E402
    NullObserver as SharedNullObserver,
    ObservationContext as SharedObservationContext,
    observation_make_vars as shared_observation_make_vars,
)
from harness_optimization.plugins import (  # noqa: E402
    HarnessActionPlugin as SharedHarnessActionPlugin,
    HarnessGapActionabilityContext as SharedHarnessGapActionabilityContext,
    HarnessPluginRegistry as SharedHarnessPluginRegistry,
)
from harness_optimization.paths import RunPathResolver  # noqa: E402
from harness_optimization.planning import RunPlan, RunPlanExecutor  # noqa: E402
from harness_optimization.rules import (  # noqa: E402
    build_candidate_final_decision,
    build_candidate_metric_delta,
    safe_action_dsl_schema,
    validate_harness_optimization_proposal,
)
from harness_optimization.records import (  # noqa: E402
    HarnessExecutionRecordProjector as SharedHarnessExecutionRecordProjector,
)
from harness_optimization.trace import (  # noqa: E402
    HarnessTraceBuilder as SharedHarnessTraceBuilder,
    HarnessTraceOutputs as SharedHarnessTraceOutputs,
    HarnessTraceResult,
)
from harness_optimization.topology import (  # noqa: E402
    ComponentNode,
    ConnectorEdge,
    PipelineTopology,
    write_topology as write_shared_topology,
)


def test_shared_rules_build_generic_metric_delta_and_final_decision() -> None:
    task = {
        "target": "demo",
        "run_id": "run-1",
        "constraints": {
            "allowed_action_types": ["scoreboard_check"],
            "safe_sandbox_action_types": ["scoreboard_check"],
            "safe_action_dsl": safe_action_dsl_schema(),
        },
        "evidence_index": {"span_ids": ["span-1"]},
    }
    proposal = {
        "schema_version": 1,
        "kind": "demo.proposal",
        "proposal_id": "proposal-1",
        "status": "proposed",
        "actions": [
            {
                "action_id": "scoreboard-1",
                "action_type": "scoreboard_check",
                "payload": {
                    "mode": "field_equals",
                    "field": "result.actual",
                    "expected": "ok",
                },
                "evidence_refs": [{"span_id": "span-1"}],
            }
        ],
        "evidence_refs": [{"span_id": "span-1"}],
    }
    validation = validate_harness_optimization_proposal(
        proposal,
        task=task,
        proposal_kind="demo.proposal",
        default_allowed_action_types=("scoreboard_check",),
    )
    candidate_evaluation = {
        "status": "passed",
        "baseline_metrics": {
            "uncovered_line_count": 10,
            "directive_count": 1,
        },
        "candidate_metrics": {
            "uncovered_line_count": 8,
            "directive_count": 2,
        },
        "acceptance_thresholds": {
            "min_improved_metric_count": 1,
            "max_regressed_metric_count": 0,
            "max_flaky_metric_count": 0,
            "accepted_candidate_statuses": ["passed"],
        },
    }
    patch = {
        "status": "applied",
        "candidate_id": "candidate-1",
        "sandbox_dir": "/tmp/candidate-1",
        "summary": {"applied_action_count": 1},
    }
    schema_decision = {"decision": "accepted"}

    metric_delta = build_candidate_metric_delta(
        task=task,
        candidate_evaluation=candidate_evaluation,
        kind="demo.metric_delta",
    )
    final_decision = build_candidate_final_decision(
        task=task,
        proposal=proposal,
        schema_decision=schema_decision,
        patch=patch,
        candidate_evaluation=candidate_evaluation,
        metric_delta=metric_delta,
        kind="demo.final_decision",
    )

    directive_count = next(
        item for item in metric_delta["comparisons"] if item["metric"] == "directive_count"
    )
    assert validation["valid"] is True
    assert metric_delta["kind"] == "demo.metric_delta"
    assert directive_count["role"] == "informational"
    assert directive_count["gates_acceptance"] is False
    assert final_decision["kind"] == "demo.final_decision"
    assert final_decision["decision"] == "accepted_for_review"


def test_shared_rules_reject_non_list_action_evidence_refs() -> None:
    task = {
        "target": "demo",
        "run_id": "run-1",
        "constraints": {
            "allowed_action_types": ["scoreboard_check"],
            "safe_sandbox_action_types": ["scoreboard_check"],
            "safe_action_dsl": safe_action_dsl_schema(),
        },
        "evidence_index": {"span_ids": ["span-1"]},
    }
    proposal = {
        "schema_version": 1,
        "kind": "demo.proposal",
        "proposal_id": "proposal-1",
        "status": "proposed",
        "actions": [
            {
                "action_id": "scoreboard-1",
                "action_type": "scoreboard_check",
                "payload": {
                    "mode": "field_equals",
                    "field": "result.actual",
                    "expected": "ok",
                },
                "evidence_refs": {"span_id": "span-bad"},
            }
        ],
        "evidence_refs": [{"span_id": "span-1"}],
    }

    validation = validate_harness_optimization_proposal(
        proposal,
        task=task,
        proposal_kind="demo.proposal",
        default_allowed_action_types=("scoreboard_check",),
    )

    assert validation["valid"] is False
    assert {
        "path": "actions[0].evidence_refs",
        "message": "expected list",
    } in validation["errors"]


def test_shared_candidate_validation_uses_generic_kinds() -> None:
    overlay = {
        "variants": [
            {
                "variant_id": "combined",
                "variant_type": "combined_actions",
                "action_ids": ["action-1"],
                "action_types": ["scoreboard_check"],
                "validation_status": "selected_for_regression",
            }
        ]
    }
    ranking = build_shared_candidate_variant_ranking(
        action_overlay=overlay,
        baseline_metrics={"uncovered_line_count": 9},
        candidate_metrics={"uncovered_line_count": 7},
        selected_variant_id="combined",
    )
    promotion = build_shared_candidate_promotion_package(
        task={"target": "demo", "run_id": "run-1"},
        proposal={"proposal_id": "proposal-1", "actions": [], "evidence_refs": []},
        patch={"sandbox_dir": "/tmp/candidate-1"},
        candidate_manifest={"candidate_id": "candidate-1"},
        candidate_metrics={"uncovered_line_count": 7},
        baseline_metrics={"uncovered_line_count": 9},
        ranking=ranking,
        thresholds=CandidateAcceptanceThresholds(min_improved_metric_count=0),
    )

    assert ranking["kind"] == "harness_optimization.candidate_variant_ranking"
    assert promotion["kind"] == "harness_optimization.candidate_promotion_package"
    assert (
        promotion["minimal_promotion_candidate"]["kind"]
        == "harness_optimization.minimal_promotion_candidate"
    )


def test_shared_candidate_execution_loads_and_adapts_actions_generically() -> None:
    registry = SharedHarnessPluginRegistry().with_action_plugin(
        SharedHarnessActionPlugin(
            action_type="shared_probe",
            payload_required=True,
            dsl_schema={"payload_fields": {"enabled": "bool"}},
            adapter_kind="demo.shared_probe_config",
            artifact_role="candidate_shared_probe_config",
            make_var="HARNESS_SHARED_PROBE_CONFIG",
            runtime_action=True,
        )
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        artifact_path = root / "action.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "action": {
                        "payload": {"enabled": True},
                        "rationale": "demo",
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        actions = load_shared_candidate_actions(
            {
                "candidate_artifacts": [
                    {
                        "action_id": "probe-1",
                        "action_type": "shared_probe",
                        "artifact_path": str(artifact_path),
                        "evidence_refs": [{"span_id": "span-1"}],
                    }
                ]
            }
        )
        results = adapt_shared_candidate_actions(
            actions,
            CandidateActionAdapterContext(
                candidate_id="candidate-1",
                regression_dir=root / "candidate",
            ),
            adapters=None,
            plugin_registry=registry,
        )
        payload = json.loads(
            results[0].artifact_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        )

    assert actions[0]["action"]["payload"] == {"enabled": True}
    assert results[0].artifact_role == "candidate_shared_probe_config"
    assert results[0].make_var_assignment().startswith("HARNESS_SHARED_PROBE_CONFIG=")
    assert payload["kind"] == "demo.shared_probe_config"
    assert payload["entries"][0]["action_id"] == "probe-1"
    assert results[0].metric_json()["shared_probe_count"] == 1


def test_shared_runtime_action_loader_plugins_and_manager_are_generic() -> None:
    validator_calls: list[tuple[str, str, int]] = []

    def validator(entry: object, *, path: Path, index: int) -> None:
        payload = getattr(entry, "payload")
        if not isinstance(payload.get("enabled"), bool):
            raise ValueError(f"{path}: entries[{index}].payload.enabled must be bool")
        validator_calls.append((getattr(entry, "action_id"), str(path), index))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config_path = root / "runtime_action.json"
        config_path.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "shared-runtime-1",
                            "action_type": "shared_runtime",
                            "payload": {"enabled": True},
                            "evidence_refs": [{"span_id": "span-1"}],
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        prior = os.environ.get("SHARED_RUNTIME_CONFIG")
        os.environ["SHARED_RUNTIME_CONFIG"] = str(config_path)
        try:
            config = load_shared_runtime_action_config(
                config_path,
                action_type="shared_runtime",
                validator=validator,
            )
            env_config = load_shared_runtime_action_config_from_env(
                "SHARED_RUNTIME_CONFIG",
                action_type="shared_runtime",
                validator=validator,
            )
        finally:
            if prior is None:
                os.environ.pop("SHARED_RUNTIME_CONFIG", None)
            else:
                os.environ["SHARED_RUNTIME_CONFIG"] = prior
        plugins = build_shared_runtime_action_plugins(
            ("demo.runtime:build",),
            plugin_builder=lambda spec, **kwargs: {
                "spec": spec,
                "metrics_out": kwargs.get("metrics_out"),
            },
            metrics_out=root / "runtime_metrics.json",
        )

    assert config.action_type == "shared_runtime"
    assert config.entries[0].artifact_path is None
    assert config.entries[0].evidence_refs == ({"span_id": "span-1"},)
    assert env_config == config
    assert validator_calls == [
        ("shared-runtime-1", str(config_path), 0),
        ("shared-runtime-1", str(config_path), 0),
    ]
    assert plugins == [
        {
            "spec": "demo.runtime:build",
            "metrics_out": config_path.parent / "runtime_metrics.json",
        }
    ]


def test_replay_orchestrator_env_helpers_keep_legacy_libafl_contract() -> None:
    prior_target = os.environ.get("FUZZ_TARGET")
    prior_corpus = os.environ.get("LIBAFL_CORPUS")
    prior_coverage = os.environ.get("UVM_FUNCTIONAL_COVERAGE_OUT")
    os.environ["FUZZ_TARGET"] = "shared-demo"
    os.environ["LIBAFL_CORPUS"] = "artifacts/shared-demo.jsonl"
    os.environ["UVM_FUNCTIONAL_COVERAGE_OUT"] = "artifacts/shared-demo_cov.json"
    try:
        assert replay_orchestrator_replay_target_from_env() == "shared-demo"
        assert replay_orchestrator_replay_corpus_from_env() == Path(
            "artifacts/shared-demo.jsonl"
        )
        assert replay_orchestrator_functional_coverage_output_from_target(
            "ignored-name"
        ) == Path("artifacts/shared-demo_cov.json")
    finally:
        if prior_target is None:
            os.environ.pop("FUZZ_TARGET", None)
        else:
            os.environ["FUZZ_TARGET"] = prior_target
        if prior_corpus is None:
            os.environ.pop("LIBAFL_CORPUS", None)
        else:
            os.environ["LIBAFL_CORPUS"] = prior_corpus
        if prior_coverage is None:
            os.environ.pop("UVM_FUNCTIONAL_COVERAGE_OUT", None)
        else:
            os.environ["UVM_FUNCTIONAL_COVERAGE_OUT"] = prior_coverage


def test_shared_runtime_action_manager_dispatches_sync_and_async_hooks() -> None:
    events: list[tuple[str, Any]] = []

    class SyncRuntime:
        def before_reset(self, *, driver: Any | None = None) -> None:
            events.append(("before_reset", driver))

        def after_execute(self, *, index: int, case: Any, result: Any) -> None:
            events.append(("after_execute", index))

    class AsyncRuntime:
        async def after_scoreboard_record(self, *, record: Any) -> None:
            events.append(("after_scoreboard_record", record))

        async def finalize(self, *, driver: Any | None = None) -> None:
            events.append(("finalize", driver))

    manager = SharedHarnessRuntimeActionManager.from_runtime_list(
        [SyncRuntime(), AsyncRuntime()]
    )

    asyncio.run(manager.before_reset(driver="drv"))
    asyncio.run(
        manager.sample_after_execute(
            index=7,
            case={"mode": "read"},
            result={"actual": "ok"},
            driver="ignored",
        )
    )
    manager.after_scoreboard_record_sync(
        index=0,
        case=None,
        result=None,
        record="record-1",
    )
    manager.finalize_sync(driver="drv")

    assert manager.hook_capabilities() == {
        "SyncRuntime": ["before_reset", "after_execute"],
        "AsyncRuntime": ["after_scoreboard_record", "finalize"],
    }
    assert events == [
        ("before_reset", "drv"),
        ("after_execute", 7),
        ("after_scoreboard_record", "record-1"),
        ("finalize", "drv"),
    ]


def test_shared_candidate_validation_private_type_hints_resolve() -> None:
    get_type_hints(shared_candidate_validation_module._skipped_gap_recommendation)
    get_type_hints(shared_candidate_validation_module._action_payload_key)


def test_shared_combined_variant_evaluation_omits_missing_optional_artifacts() -> None:
    evaluation = combined_variant_evaluation(
        overlay={
            "variants": [
                {
                    "variant_id": "combined",
                    "variant_type": "combined_actions",
                }
            ]
        },
        metrics={"uncovered_line_count": 7},
        baseline_metrics={"uncovered_line_count": 9},
        campaign_config=SimpleNamespace(
            campaign_manifest_out=None,
            campaign_evaluation_out=None,
        ),
        run_config_path=Path("/tmp/candidate_regression_config.json"),
        runtime_metrics_path=Path("/tmp/candidate_runtime_metrics.json"),
        adapter_results=(),
    )

    assert evaluation["artifacts"]["candidate_regression_config"].endswith(
        "candidate_regression_config.json"
    )
    assert evaluation["artifacts"]["candidate_runtime_metrics"].endswith(
        "candidate_runtime_metrics.json"
    )
    assert "candidate_campaign_manifest" not in evaluation["artifacts"]
    assert "candidate_campaign_evaluation" not in evaluation["artifacts"]


def test_shared_candidate_validation_builds_generic_reports_and_metric_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime_metrics = root / "runtime_metrics.json"
        runtime_metrics.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "demo.runtime_metrics",
                    "sections": {
                        "replay_probe": {"sample_count": 3},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        harness_evaluation = root / "harness_evaluation.json"
        harness_evaluation.write_text(
            json.dumps(
                {
                    "summary": {"record_count": 4},
                    "trace_quality": {"hanging_span_count": 1},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        plugin_provenance_path = root / "plugin_provenance.json"
        plugin_provenance_path.write_text(
            json.dumps({"validation": {"valid": True}}) + "\n",
            encoding="utf-8",
        )
        metrics = build_shared_candidate_metric_snapshot(
            {
                "modes": [
                    {
                        "mode": "feedback",
                        "rounds": [
                            {
                                "coverage": {"covered_line_count": 12},
                                "feedback": {"directive_count": 5},
                                "stages": {
                                    "corpus_validation": {
                                        "validation": {"case_count": 8}
                                    }
                                },
                                "artifacts": {
                                    "harness_runtime_metrics": str(runtime_metrics)
                                },
                            }
                        ],
                    }
                ]
            },
            {
                "summary": {"round_count": 1},
                "harness_trace": {
                    "summary": {"failed_record_count": 2},
                    "artifacts": {"harness_evaluation": str(harness_evaluation)},
                },
            },
            cwd=root,
        )
        not_run = build_shared_candidate_not_run_report(
            task={"target": "demo", "run_id": "run-1"},
            proposal={"proposal_id": "proposal-1"},
            patch={"status": "applied"},
            candidate_manifest={"candidate_id": "candidate-1"},
            baseline_metrics={"failed_record_count": 2},
            acceptance_thresholds=CandidateAcceptanceThresholds(
                min_improved_metric_count=1
            ),
            plugin_provenance={"validation": {"valid": True}},
            plugin_provenance_path=plugin_provenance_path,
            run_config_path=root / "candidate_regression_config.json",
            overlay_path=root / "candidate_action_overlay.json",
            adapter_results=(),
            directives_path=None,
            runtime_metrics_path=runtime_metrics,
            reason="no safe applied actions",
            source="DemoBackend",
            matched_baseline_enabled=True,
        )
        error = build_shared_candidate_error_report(
            task={"target": "demo", "run_id": "run-1"},
            proposal={"proposal_id": "proposal-1"},
            baseline_metrics={"failed_record_count": 2},
            acceptance_thresholds=CandidateAcceptanceThresholds(
                min_improved_metric_count=1
            ),
            plugin_provenance={"validation": {"valid": True}},
            plugin_provenance_path=plugin_provenance_path,
            run_config_path=root / "candidate_regression_config.json",
            overlay_path=root / "candidate_action_overlay.json",
            adapter_results=(),
            directives_path=None,
            runtime_metrics_path=runtime_metrics,
            error=RuntimeError("boom"),
            source="DemoBackend",
            baseline_source="matched_noop_rerun",
            source_baseline_metrics={"failed_record_count": 1},
            matched_baseline_artifacts={
                "matched_baseline_action_overlay": str(root / "matched_overlay.json")
            },
            matched_baseline_summary={
                "enabled": True,
                "status": "passed",
                "baseline_source": "matched_noop_rerun",
            },
        )

    assert metrics["round_count"] == 1
    assert metrics["failed_record_count"] == 2
    assert metrics["record_count"] == 4
    assert metrics["hanging_span_count"] == 1
    assert metrics["covered_line_count"] == 12
    assert metrics["directive_count"] == 5
    assert metrics["case_count"] == 8
    assert metrics["replay_probe_sample_count"] == 3
    assert not_run["status"] == "not_run"
    assert not_run["summary"]["matched_baseline"]["enabled"] is True
    assert not_run["artifacts"]["candidate_plugin_provenance"] == str(
        plugin_provenance_path
    )
    assert error["status"] == "error"
    assert error["matched_baseline_metrics"]["failed_record_count"] == 2
    assert error["error"]["message"] == "boom"
    assert error["artifacts"]["matched_baseline_action_overlay"].endswith(
        "matched_overlay.json"
    )


def test_shared_candidate_action_effect_report_is_runtime_agnostic() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime_metrics_path = root / "candidate_runtime_metrics.json"
        runtime_metrics_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "demo.runtime_metrics",
                    "sections": {
                        "replay_probe": {
                            "sample_count": 3,
                            "actions": [
                                {
                                    "action_id": "probe-1",
                                    "action_type": "replay_probe",
                                    "sample_count": 3,
                                }
                            ],
                        }
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        report = build_shared_candidate_action_effect_report(
            task={"target": "demo", "run_id": "run-1"},
            proposal={"proposal_id": "proposal-1"},
            candidate_manifest={"candidate_id": "candidate-1"},
            baseline_metrics={"uncovered_line_count": 10},
            variant_evaluations=[
                {
                    "variant_id": "combined",
                    "variant_type": "combined_actions",
                    "status": "passed",
                    "action_ids": ["probe-1", "directive-1"],
                    "actions": [
                        {
                            "action_id": "probe-1",
                            "action_type": "replay_probe",
                            "evidence_refs": [{"span_id": "probe"}],
                        },
                        {
                            "action_id": "directive-1",
                            "action_type": "mutation_directive_update",
                            "evidence_refs": [{"span_id": "directive"}],
                        },
                    ],
                    "metrics": {"uncovered_line_count": 8},
                    "artifacts": {
                        "candidate_runtime_metrics": str(runtime_metrics_path)
                    },
                },
                {
                    "variant_id": "action_probe-1",
                    "variant_type": "single_action",
                    "status": "passed",
                    "action_ids": ["probe-1"],
                    "actions": [
                        {
                            "action_id": "probe-1",
                            "action_type": "replay_probe",
                            "evidence_refs": [{"span_id": "probe"}],
                        }
                    ],
                    "metrics": {"uncovered_line_count": 8},
                    "artifacts": {
                        "candidate_runtime_metrics": str(runtime_metrics_path)
                    },
                },
                {
                    "variant_id": "action_directive-1",
                    "variant_type": "single_action",
                    "status": "passed",
                    "action_ids": ["directive-1"],
                    "actions": [
                        {
                            "action_id": "directive-1",
                            "action_type": "mutation_directive_update",
                            "evidence_refs": [{"span_id": "directive"}],
                        }
                    ],
                    "metrics": {"uncovered_line_count": 10},
                    "artifacts": {},
                },
            ],
            runtime_action_types=("replay_probe",),
            plugin_registry={"fingerprint": "fp-1"},
            plugin_validation={"valid": True},
            plugin_provenance={"kind": "demo.plugin_provenance"},
        )

    actions = {item["action_id"]: item for item in report["actions"]}
    assert report["kind"] == "harness_optimization.candidate_action_effect_report"
    assert report["summary"]["runtime_action_count"] == 1
    assert report["summary"]["consumed_action_count"] == 1
    assert report["summary"]["standalone_improved_action_count"] == 1
    assert report["summary"]["standalone_neutral_action_count"] == 1
    assert actions["probe-1"]["is_runtime_action"] is True
    assert actions["probe-1"]["consumed"] is True
    assert actions["probe-1"]["standalone_effect_status"] == "improved"
    assert actions["directive-1"]["is_runtime_action"] is False
    assert actions["directive-1"]["effect_status"] == "neutral"
    assert report["plugin_validation"]["valid"] is True


def test_shared_gap_actionability_report_and_minimal_proposal_are_generic() -> None:
    def payload_errors(payload: dict[str, object], path: str) -> list[dict[str, str]]:
        if isinstance(payload.get("enabled"), bool):
            return []
        return [{"path": f"{path}.enabled", "message": "expected bool"}]

    def classifier(
        gap: dict[str, object],
        context: SharedHarnessGapActionabilityContext,
    ) -> dict[str, object] | None:
        if context.target != "demo":
            return None
        if gap.get("id") == "gap-probe":
            return {
                **gap,
                "actionability": "reachable_with_shared_probe",
                "actionability_reason": "shared probe can observe this state",
                "recommended_action_type": "shared_probe",
                "suggested_payload": {"enabled": True},
                "evidence_refs": [{"span_id": "span-demo"}],
            }
        if gap.get("id") == "gap-invalid":
            return {
                **gap,
                "actionability": "reachable_with_shared_probe",
                "actionability_reason": "payload intentionally invalid",
                "recommended_action_type": "shared_probe",
                "suggested_payload": {"enabled": "yes"},
                "evidence_refs": [{"span_id": "span-demo"}],
            }
        if gap.get("id") == "gap-blocked":
            return {
                **gap,
                "actionability": "requires_internal_state_surface",
                "actionability_reason": "needs hidden state access",
                "recommended_action_type": None,
                "suggested_payload": {},
            }
        return None

    registry = SharedHarnessPluginRegistry().with_action_plugin(
        SharedHarnessActionPlugin(
            action_type="shared_probe",
            payload_required=True,
            dsl_schema={"payload_fields": {"enabled": "bool"}},
            payload_validator=payload_errors,
            adapter_kind="demo.shared_probe_config",
            artifact_role="candidate_shared_probe_config",
            make_var="HARNESS_SHARED_PROBE_CONFIG",
            runtime_action=True,
        )
    ).with_gap_actionability_classifier(classifier)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        baseline_summary = root / "baseline_summary.json"
        candidate_summary = root / "candidate_summary.json"
        baseline_summary.write_text(
            json.dumps({"uncovered_line_count": 5}) + "\n",
            encoding="utf-8",
        )
        candidate_summary.write_text(
            json.dumps(
                {
                    "uncovered_line_count": 2,
                    "rtl_gap_summary": {
                        "top_gaps": [
                            {"id": "gap-probe", "file": "/repo/demo.sv", "line": 7},
                            {"id": "gap-invalid", "file": "/repo/demo.sv", "line": 8},
                            {"id": "gap-blocked", "file": "/repo/demo.sv", "line": 9},
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        report = build_shared_candidate_gap_actionability_report(
            task={"target": "demo", "run_id": "run-1"},
            proposal={"proposal_id": "proposal-1", "evidence_refs": [{"span_id": "span-demo"}]},
            candidate_manifest={"candidate_id": "candidate-1"},
            baseline_campaign_manifest={
                "modes": [{"rounds": [{"artifacts": {"coverage_summary": str(baseline_summary)}}]}]
            },
            candidate_campaign_manifest={
                "modes": [{"rounds": [{"artifacts": {"coverage_summary": str(candidate_summary)}}]}]
            },
            baseline_metrics={"uncovered_line_count": 5},
            candidate_metrics={"uncovered_line_count": 2},
            action_effect_report={"summary": {"improved_action_count": 1}},
            plugin_registry=registry,
        )
        minimal = build_shared_gap_actionability_minimal_candidate_proposal(
            task={
                "target": "demo",
                "run_id": "run-1",
                "evidence_index": {"span_ids": ["span-demo"]},
            },
            proposal={"proposal_id": "proposal-1", "evidence_refs": [{"span_id": "span-demo"}]},
            candidate_manifest={"candidate_id": "candidate-1"},
            gap_actionability_report=report,
            plugin_registry=registry,
            proposal_kind="demo.proposal",
        )

    assert report["kind"] == "harness_optimization.candidate_gap_actionability_report"
    assert report["summary"]["remaining_gap_count"] == 3
    assert report["summary"]["recommended_action_type_counts"] == {"shared_probe": 2}
    assert report["summary"]["blocked_by_action_surface"] is True
    assert report["plugin_validation"]["valid"] is True
    assert minimal["kind"] == "demo.proposal"
    assert minimal["status"] == "proposed"
    assert [item["action_type"] for item in minimal["actions"]] == ["shared_probe"]
    assert minimal["actions"][0]["payload"] == {"enabled": True}
    assert minimal["summary"]["skipped_recommendation_count"] == 2
    assert minimal["validation"]["valid"] is True


def test_shared_trace_and_rollup_use_generic_kinds() -> None:
    class DemoProjector:
        def project(
            self,
            events: list[tuple[int, dict[str, object]]],
            *,
            round_manifest: dict[str, object],
            campaign_manifest: dict[str, object],
        ) -> list[dict[str, object]]:
            return [
                {
                    "kind": "demo.record",
                    "run_id": "run-1",
                    "target": "demo",
                    "round_id": "round-1",
                    "case_id": "case-1",
                    "directive_id": "dir-1",
                    "connector": str(event.get("connector")),
                    "status": "ok",
                }
                for _line_no, event in events
            ]

    class DemoAnalyzer:
        def analyze(
            self,
            records: list[dict[str, object]],
            **kwargs: object,
        ) -> dict[str, object]:
            return {
                "kind": "demo.evaluation",
                "run_id": "run-1",
                "target": "demo",
                "summary": {
                    "record_count": len(records),
                    "hanging_span_count": kwargs["trace_quality"]["hanging_span_count"],
                },
            }

    class DemoDatasetBuilder:
        def build(
            self,
            records: list[dict[str, object]],
            evaluation: dict[str, object],
        ) -> list[dict[str, object]]:
            return [
                {
                    "kind": "demo.llm_dataset",
                    "record_count": len(records),
                    "evaluation_kind": evaluation["kind"],
                }
            ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "events.jsonl"
        events.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_type": "connector.finished",
                    "connector": "case_to_dut",
                    "from_layer": "driver",
                    "to_layer": "dut",
                    "run_id": "run-1",
                    "span_id": "span-1",
                    "timestamp_ns": 1,
                    "status": "ok",
                    "metadata": {"round_id": "round-1"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        outputs = SharedHarnessTraceOutputs.from_evaluation_path(root / "evaluation.json")
        result = SharedHarnessTraceBuilder(
            observation_events=events,
            record_projector=DemoProjector(),
            analyzer=DemoAnalyzer(),
            llm_dataset_builder=DemoDatasetBuilder(),
        ).write(outputs)
        rollup = SharedCampaignTraceRollupBuilder(
            records=[
                {
                    "run_id": "run-1",
                    "target": "demo",
                    "round_id": "round-1",
                    "case_id": "case-1",
                    "directive_id": "dir-1",
                    "connector": "case_to_dut",
                    "status": "ok",
                }
            ],
            evaluation={"run_id": "run-1", "target": "demo"},
            campaign_manifest={
                "run_id": "run-1",
                "target": "demo",
                "modes": [
                    {
                        "mode": "feedback",
                        "rounds": [
                            {
                                "index": 0,
                                "round_id": "round-1",
                                "coverage": {"covered_line_count": 1},
                                "feedback": {"directive_count": 1},
                            }
                        ],
                    }
                ],
            },
        ).build()

    assert result.records[0]["kind"] == "demo.record"
    assert result.evaluation["kind"] == "demo.evaluation"
    assert result.llm_dataset[0]["kind"] == "demo.llm_dataset"
    assert outputs.to_json()["harness_execution_records"].endswith(
        "evaluation_harness_records.jsonl"
    )
    assert rollup["kind"] == "harness_optimization.campaign_trace_rollup"


def test_shared_record_and_analysis_use_generic_kinds() -> None:
    class DemoMetadataExtractor:
        def extract(self, metadata: dict[str, object]) -> dict[str, object]:
            return {
                "case_index": metadata.get("case_index"),
                "case_id": metadata.get("case_id"),
                "directive_id": metadata.get("directive_id"),
                "corpus_sha256": metadata.get("corpus_sha256"),
            }

    event = {
        "event_type": "connector.finished",
        "connector": "case_to_dut",
        "from_layer": "driver",
        "to_layer": "dut",
        "run_id": "run-1",
        "span_id": "span-1",
        "timestamp_ns": 1,
        "duration_ms": 2.5,
        "metrics": {"passed": True},
        "metadata": {
            "target": "demo",
            "mode": "feedback",
            "round_id": "round-1",
            "stage_id": "stage-1",
            "step": "dut_execute",
            "case_index": 0,
            "case_id": "case-0",
            "directive_id": "dir-0",
            "corpus_sha256": "hash-0",
        },
        "status": "ok",
    }
    records = SharedHarnessExecutionRecordProjector(
        observation_events=Path("/tmp/shared_events.jsonl"),
        metadata_extractor=DemoMetadataExtractor(),
    ).project(
        [(7, event)],
        round_manifest={"target": "demo", "mode": "feedback", "round_id": "round-1"},
        campaign_manifest={"run_id": "run-1"},
    )
    evaluation = SharedHarnessEvaluationAnalyzer().analyze(
        records,
        observation_events=Path("/tmp/shared_events.jsonl"),
        monitoring_path=None,
        round_manifest_path=None,
        campaign_manifest_path=None,
        monitoring={"event_count": 1},
        round_manifest={
            "target": "demo",
            "mode": "feedback",
            "round_id": "round-1",
            "artifacts": {"round_manifest": "/tmp/round.json"},
        },
        campaign_manifest={"run_id": "run-1"},
        trace_quality={},
    )

    assert records[0]["kind"] == "harness_optimization.harness_execution_record"
    assert records[0]["directive_id"] == "dir-0"
    assert evaluation["kind"] == "harness_optimization.harness_evaluation"
    assert evaluation["summary"]["record_count"] == 1
    assert evaluation["connectors"][0]["connector"] == "case_to_dut"


def test_shared_optimization_runtime_supports_generic_kinds() -> None:
    class FakeTransport:
        def complete(
            self,
            *,
            messages: list[dict[str, str]],
            model: str,
            temperature: float,
            response_format: dict[str, object] | None = None,
        ) -> dict[str, object]:
            assert messages[0]["role"] == "system"
            assert response_format == {"type": "json_object"}
            return {
                "id": "resp-1",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "kind": "demo.proposal",
                                    "proposal_id": "demo-proposal-1",
                                    "status": "proposed",
                                    "actions": [],
                                    "evidence_refs": [],
                                }
                            )
                        }
                    }
                ],
            }

    def prompt_builder(task: dict[str, object], limit: int) -> dict[str, object]:
        assert limit == 2
        return {
            "schema_version": 1,
            "kind": "demo.prompt",
            "schema_hint": {"proposal_kind": "demo.proposal"},
            "messages": [
                {"role": "system", "content": "return json"},
                {"role": "user", "content": json.dumps(task, sort_keys=True)},
            ],
        }

    def validator(
        proposal: dict[str, object],
        task: dict[str, object],
    ) -> dict[str, object]:
        assert task["run_id"] == "run-1"
        return {"valid": proposal.get("kind") == "demo.proposal", "errors": []}

    def normalize_candidate_evaluation(
        value: object,
        task: dict[str, object],
        proposal: dict[str, object],
        source: str,
    ) -> dict[str, object]:
        assert task["run_id"] == "run-1"
        assert proposal["proposal_id"] == "demo-proposal-1"
        assert source == "NoopOptimizationCandidateEvaluationBackend"
        if isinstance(value, dict):
            return value
        raise AssertionError("expected mapping candidate evaluation")

    def candidate_evaluation_error(
        task: dict[str, object],
        proposal: dict[str, object],
        source: str,
        error_type: str,
        message: str,
    ) -> dict[str, object]:
        return {
            "kind": "demo.candidate_evaluation_error",
            "target": task.get("target"),
            "proposal_id": proposal.get("proposal_id"),
            "source": source,
            "error": {"type": error_type, "message": message},
        }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = shared_harness_optimization_paths(root / "campaign_evaluation.json")
        runtime = SharedOptimizationRuntimeAdapter(
            paths=paths,
            cwd=root,
            optimizer_backend=SharedLlmOptimizationProposalBackend(
                model="fake-model",
                prompt_builder=prompt_builder,
                validator=validator,
                proposal_kind="demo.proposal",
                response_kind="demo.response",
                transport=FakeTransport(),
                source="fake-llm",
                sample_limit=2,
            ),
            candidate_evaluation_backend=SharedNoopOptimizationCandidateEvaluationBackend(
                candidate_evaluation_kind="demo.candidate_evaluation",
                baseline_metric_snapshot=lambda _task: {"failed_record_count": 1},
                rationale="candidate validation not configured",
            ),
            proposal_kind="demo.proposal",
            normalize_candidate_evaluation=normalize_candidate_evaluation,
            candidate_evaluation_error=candidate_evaluation_error,
        )
        task = {"target": "demo", "run_id": "run-1"}
        proposal = runtime.run_proposal(task)
        evaluation = runtime.run_candidate_evaluation(
            task,
            proposal,
            {"status": "applied"},
            {"candidate_id": "candidate-1", "candidate_artifacts": []},
        )
        prompt = json.loads(paths.optimizer_prompt.read_text(encoding="utf-8"))
        response = json.loads(paths.optimizer_response.read_text(encoding="utf-8"))
        written_proposal = json.loads(paths.proposal.read_text(encoding="utf-8"))
        written_evaluation = json.loads(
            paths.candidate_evaluation.read_text(encoding="utf-8")
        )

    assert prompt["kind"] == "demo.prompt"
    assert response["kind"] == "demo.response"
    assert response["source"] == "fake-llm"
    assert proposal["kind"] == "demo.proposal"
    assert proposal["llm_provenance"]["model"] == "fake-model"
    assert evaluation["kind"] == "demo.candidate_evaluation"
    assert evaluation["candidate_id"] == "candidate-1"
    assert written_proposal == proposal
    assert written_evaluation == evaluation


def test_shared_prompt_only_backend_accepts_positional_prompt_builder() -> None:
    def prompt_builder(task: dict[str, object], limit: int) -> dict[str, object]:
        assert task["run_id"] == "run-1"
        assert limit == 3
        return {
            "schema_version": 1,
            "kind": "demo.prompt",
            "messages": [
                {"role": "system", "content": "return json"},
                {"role": "user", "content": json.dumps(task, sort_keys=True)},
            ],
        }

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = shared_harness_optimization_paths(root / "campaign_evaluation.json")
        backend = SharedPromptOnlyOptimizationProposalBackend(
            model="prompt-only",
            prompt_builder=prompt_builder,
            proposal_kind="demo.proposal",
            response_kind="demo.response",
            sample_limit=3,
        )
        proposal = backend.run_with_context(
            {"target": "demo", "run_id": "run-1"},
            SimpleNamespace(paths=paths, cwd=root),
        )
        prompt = json.loads(paths.optimizer_prompt.read_text(encoding="utf-8"))
        response = json.loads(paths.optimizer_response.read_text(encoding="utf-8"))

    assert prompt["kind"] == "demo.prompt"
    assert response["kind"] == "demo.response"
    assert proposal["kind"] == "demo.proposal"
    assert proposal["status"] == "no_op"


def test_shared_optimization_task_prompt_and_advice_builders_are_generic() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        harness_evaluation = root / "harness_evaluation.json"
        harness_evaluation.write_text(
            json.dumps(
                {
                    "summary": {"record_count": 2},
                    "optimization_hints": {"failing_connectors": ["case_to_dut"]},
                    "trace_quality": {"hanging_span_count": 1},
                    "failure_clusters": [{"connector": "case_to_dut"}],
                    "slowest_records": [{"connector": "case_to_dut", "duration_ms": 3.0}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        dataset = root / "llm_dataset.jsonl"
        dataset.write_text(
            json.dumps({"kind": "demo.sample", "connector": "case_to_dut"}) + "\n",
            encoding="utf-8",
        )
        rollup = root / "campaign_rollup.json"
        rollup.write_text(
            json.dumps(
                {
                    "summary": {"round_count": 1},
                    "coverage_trends": [{"metric": "covered_line_count"}],
                    "failure_trends": [{"round_id": "round-1"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        action_effect = root / "candidate_action_effect_report.json"
        action_effect.write_text(
            json.dumps({"summary": {"consumed_action_count": 1}}) + "\n",
            encoding="utf-8",
        )
        campaign_evaluation_path = root / "campaign_evaluation.json"
        campaign_manifest_path = root / "campaign_manifest.json"
        paths = shared_harness_optimization_paths(campaign_evaluation_path)
        task = build_shared_optimization_task(
            task_kind="demo.task",
            campaign_evaluation={
                "target": "demo",
                "run_id": "run-1",
                "summary": {"round_count": 1},
                "harness_trace": {
                    "summary": {"record_count": 2},
                    "artifacts": {
                        "harness_evaluation": str(harness_evaluation),
                        "llm_optimization_dataset": str(dataset),
                        "campaign_trace_rollup": str(rollup),
                        "candidate_action_effect_report": str(action_effect),
                    },
                },
            },
            campaign_manifest={
                "target": "demo",
                "run_id": "run-1",
                "artifacts": {
                    "candidate_action_effect_report": str(action_effect),
                },
            },
            campaign_evaluation_path=campaign_evaluation_path,
            campaign_manifest_path=campaign_manifest_path,
            target="demo",
            cwd=root,
            objective="produce optimization guidance",
            constraints={
                "allowed_action_types": ["scoreboard_check"],
                "safe_sandbox_action_types": ["scoreboard_check"],
                "safe_action_dsl": {"scoreboard_check": {"type": "object"}},
                "plugin_registry": {"fingerprint": "fp-1"},
                "plugin_validation": {"valid": True},
                "plugin_provenance": {"source": "test"},
            },
            evidence_index_builder=lambda payload: {
                "span_ids": [item["connector"] for item in payload.get("failure_clusters", [])]
            },
        )
        prompt = build_shared_optimization_prompt(
            task=task,
            sample_limit=1,
            prompt_kind="demo.prompt",
            schema_hint={"proposal_kind": "demo.proposal"},
            system_message="return json only",
        )
        proposal = {
            "proposal_id": "demo-proposal-1",
            "status": "proposed",
            "source": "demo",
            "actions": [
                {
                    "action_id": "scoreboard-1",
                    "action_type": "scoreboard_check",
                    "payload": {"mode": "field_equals"},
                    "evidence_refs": [{"span_id": "case_to_dut"}],
                }
            ],
            "artifacts": {"optimizer_prompt": str(paths.optimizer_prompt)},
        }
        decision = {
            "decision": "accepted",
            "validation": {"errors": []},
        }
        advice = build_shared_optimization_advice_report(
            kind="demo.advice",
            task=task,
            proposal=proposal,
            decision=decision,
            paths=paths,
        )

    assert task["kind"] == "demo.task"
    assert task["summary"]["llm_sample_count"] == 1
    assert task["evidence_index"]["span_ids"] == ["case_to_dut"]
    assert prompt["kind"] == "demo.prompt"
    assert prompt["context"]["llm_dataset_samples"][0]["kind"] == "demo.sample"
    assert advice["kind"] == "demo.advice"
    assert advice["status"] == "ready_for_candidate_validation"
    assert advice["recommended_actions"][0]["sandbox_safe"] is True
    assert advice["reproducibility"]["artifacts"]["optimizer_prompt"] == str(
        paths.optimizer_prompt
    )


def test_shared_orchestrator_runs_without_fuzz_pipeline_wrapper() -> None:
    topology = PipelineTopology(
        name="shared-topology",
        components=(
            ComponentNode(name="source", kind="source"),
            ComponentNode(name="sink", kind="sink"),
        ),
        connectors=(
            ConnectorEdge(
                name="source_to_sink",
                from_layer="source",
                to_layer="sink",
            ),
        ),
    )
    context = SharedPipelineContext(run_id="run-1")
    result = SharedPipelineOrchestrator(topology).run(
        [
            SharedStepSpec(
                name="emit",
                connector="source_to_sink",
                handler=lambda _context: {"status": "ok"},
                result_key="shared_result",
            )
        ],
        context,
    )

    assert result is context
    assert result.values["shared_result"] == {"status": "ok"}


def test_compatibility_facades_point_to_shared_orchestration_kernel() -> None:
    assert ConnectGraphPipelineOrchestrator is SharedPipelineOrchestrator
    assert ConnectGraphStepPolicy is SharedStepPolicy
    assert issubclass(FuzzPipelineOrchestrator, SharedPipelineOrchestrator)
    assert FuzzPipelineOrchestrator().topology == FULL_FUZZ_TOPOLOGY


def test_connectgraph_root_surfaces_observation_not_orchestration() -> None:
    assert ConnectGraphRoot.ObservationContext is ObservationContext
    assert ConnectorObserveRoot.ObservationContext is ObservationContext
    for module in (ConnectGraphRoot, ConnectorObserveRoot):
        assert hasattr(module, "Connector")
        assert not hasattr(module, "PipelineContext")
        assert not hasattr(module, "PipelineOrchestrator")
        assert not hasattr(module, "StepPolicy")
        assert not hasattr(module, "StepSpec")
        assert not hasattr(module, "external_command_step")


def test_fuzz_pipeline_business_modules_do_not_import_connectgraph_directly() -> None:
    source_root = ROOT / "libafl_bfm_fuzz" / "py" / "fuzz_pipeline"
    violations: dict[str, list[str]] = {}
    for path in sorted(source_root.rglob("*.py")):
        lines = [
            f"{line_no}:{line.strip()}"
            for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            )
            if "ConnectGraph" in line
        ]
        if lines:
            violations[str(path.relative_to(ROOT))] = lines

    assert violations == {}


def test_fuzz_pipeline_internal_modules_do_not_depend_on_compatibility_wrappers() -> None:
    source_root = ROOT / "libafl_bfm_fuzz" / "py" / "fuzz_pipeline"
    allowed = {
        "__init__.py",
        "harness.py",
        "harness_analysis.py",
        "harness_candidate_regression.py",
        "harness_llm_tasks.py",
        "harness_metadata.py",
        "harness_optimization.py",
        "harness_plugins.py",
        "harness_records.py",
        "harness_rollup.py",
        "harness_runtime_actions.py",
        "harness_trace.py",
        "orchestrator.py",
        "run_plan.py",
        "run_stage_registry.py",
    }
    wrapper_import = re.compile(
        r"from\s+(?:\.+|fuzz_pipeline\.)"
        r"(?:harness(?:\b|_(?!evidence\b))|orchestrator\b|run_plan\b|run_stage_registry\b)"
    )
    violations: dict[str, list[str]] = {}
    for path in sorted(source_root.rglob("*.py")):
        relative = str(path.relative_to(source_root))
        if relative in allowed:
            continue
        matches = [
            f"{line_no}:{line.strip()}"
            for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            )
            if wrapper_import.search(line)
        ]
        if matches:
            violations[relative] = matches

    assert violations == {}


def test_business_record_and_analysis_wrappers_preserve_libafl_kinds() -> None:
    event = {
        "event_type": "connector.finished",
        "connector": "case_to_dut",
        "from_layer": "driver",
        "to_layer": "dut",
        "run_id": "run-1",
        "span_id": "span-1",
        "timestamp_ns": 1,
        "duration_ms": 2.5,
        "metadata": {
            "target": "demo",
            "mode": "feedback",
            "round_id": "round-1",
            "step": "dut_execute",
            "index": 3,
            "case_id": "case-3",
            "origin": "seed-origin",
        },
        "status": "ok",
    }
    records = FuzzHarnessRecordProjector(
        observation_events=Path("/tmp/fuzz_events.jsonl"),
    ).project(
        [(9, event)],
        round_manifest={"target": "demo", "mode": "feedback", "round_id": "round-1"},
        campaign_manifest={"run_id": "run-1"},
    )
    evaluation = UvmFuzzHarnessAnalyzer().analyze(
        records,
        observation_events=Path("/tmp/fuzz_events.jsonl"),
        monitoring_path=None,
        round_manifest_path=None,
        campaign_manifest_path=None,
        monitoring={"event_count": 1},
        round_manifest={"target": "demo", "mode": "feedback", "round_id": "round-1"},
        campaign_manifest={"run_id": "run-1"},
        trace_quality={},
    )

    assert records[0]["kind"] == "libafl_bfm_fuzz.harness_execution_record"
    assert records[0]["case_index"] == 3
    assert records[0]["directive_id"] == "seed-origin"
    assert evaluation["kind"] == "libafl_bfm_fuzz.harness_evaluation"
    assert evaluation["summary"]["record_count"] == 1


def test_business_wrappers_preserve_libafl_specific_kinds() -> None:
    overlay = {
        "variants": [
            {
                "variant_id": "combined",
                "variant_type": "combined_actions",
                "action_ids": ["action-1"],
                "action_types": ["scoreboard_check"],
                "validation_status": "selected_for_regression",
            }
        ]
    }
    ranking = build_fuzz_candidate_variant_ranking(
        action_overlay=overlay,
        baseline_metrics={"uncovered_line_count": 9},
        candidate_metrics={"uncovered_line_count": 7},
        selected_variant_id="combined",
    )
    promotion = build_fuzz_candidate_promotion_package(
        task={"target": "demo", "run_id": "run-1"},
        proposal={"proposal_id": "proposal-1", "actions": [], "evidence_refs": []},
        patch={"sandbox_dir": "/tmp/candidate-1"},
        candidate_manifest={"candidate_id": "candidate-1"},
        candidate_metrics={"uncovered_line_count": 7},
        baseline_metrics={"uncovered_line_count": 9},
        ranking=ranking,
        thresholds=CandidateAcceptanceThresholds(min_improved_metric_count=0),
    )

    assert ranking["kind"] == "libafl_bfm_fuzz.harness_candidate_variant_ranking"
    assert promotion["kind"] == "libafl_bfm_fuzz.harness_candidate_promotion_package"
    assert (
        promotion["minimal_promotion_candidate"]["kind"]
        == "libafl_bfm_fuzz.harness_minimal_promotion_candidate"
    )


def test_business_rollup_wrapper_preserves_libafl_kind() -> None:
    rollup = FuzzCampaignTraceRollupBuilder(
        records=[
            {
                "run_id": "run-1",
                "target": "demo",
                "round_id": "round-1",
                "case_id": "case-1",
                "directive_id": "dir-1",
                "connector": "case_to_dut",
                "status": "ok",
            }
        ],
        evaluation={"run_id": "run-1", "target": "demo"},
        campaign_manifest={
            "run_id": "run-1",
            "target": "demo",
            "modes": [
                {
                    "mode": "feedback",
                    "rounds": [
                        {
                            "index": 0,
                            "round_id": "round-1",
                            "coverage": {"covered_line_count": 1},
                            "feedback": {"directive_count": 1},
                        }
                    ],
                }
            ],
        },
    ).build()

    assert rollup["kind"] == "libafl_bfm_fuzz.campaign_trace_rollup"


class _FakeCampaignOptimizationAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def run_task(
        self,
        campaign_evaluation: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("task", (campaign_evaluation, campaign_manifest)))
        return {
            "task_id": "task-1",
            "summary": {"llm_sample_count": 1},
            "constraints": {"allowed_action_types": ["scoreboard_check"]},
        }

    def run_proposal(self, task: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("proposal", (task,)))
        return {
            "proposal_id": "proposal-1",
            "status": "proposed",
            "actions": [{"action_id": "action-1"}],
        }

    def run_decision(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("decision", (task, proposal)))
        return {
            "decision": "accepted",
            "validation": {"error_count": 0},
        }

    def run_advice_report(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("advice", (task, proposal, decision)))
        return {
            "status": "ready_for_candidate_validation",
            "summary": {"recommended_action_count": 1},
            "advice_only_guard": {
                "candidate_regression_triggered": False,
            },
        }

    def run_apply(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        self.calls.append(("apply", (task, proposal, decision)))
        return {
            "harness_optimization_patch": {
                "status": "applied",
                "summary": {
                    "applied_action_count": 1,
                    "skipped_action_count": 0,
                },
            },
            "harness_optimization_candidate_manifest": {
                "candidate_id": "candidate-1",
            },
        }

    def run_candidate_evaluation(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            ("candidate_evaluation", (task, proposal, patch, candidate_manifest))
        )
        return {
            "status": "passed",
            "candidate_metrics": {"covered_line_count": 3},
        }

    def run_metric_delta(
        self,
        task: dict[str, Any],
        candidate_evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("metric_delta", (task, candidate_evaluation)))
        return {
            "summary": {
                "improved_metric_count": 1,
                "regressed_metric_count": 0,
            }
        }

    def run_final_decision(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        schema_decision: dict[str, Any],
        patch: dict[str, Any],
        candidate_evaluation: dict[str, Any],
        metric_delta: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "final_decision",
                (
                    task,
                    proposal,
                    schema_decision,
                    patch,
                    candidate_evaluation,
                    metric_delta,
                ),
            )
        )
        return {
            "decision": "accepted_for_review",
            "summary": {"regressed_metric_count": 0},
        }


def test_shared_campaign_optimization_stage_chain_reports_manifest_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        evaluation_path = root / "campaign_evaluation.json"
        paths = shared_harness_optimization_paths(evaluation_path)
        paths.optimizer_prompt.write_text("{}", encoding="utf-8")
        paths.optimizer_response.write_text("{}", encoding="utf-8")
        chain = CampaignOptimizationStageChain(
            target="demo",
            modes=("heuristic_feedback",),
            rounds=2,
            context_artifacts={},
            step_runner=lambda step: step.handler(SimpleNamespace()),
            paths_factory=lambda: paths,
            adapter_factory=lambda _paths: _FakeCampaignOptimizationAdapter(),
        )

        base_artifacts = chain.manifest_artifacts(("harness_optimization_task",))
        advice_artifacts = chain.manifest_artifacts(
            (
                "harness_optimization_task",
                "harness_optimization_advice_report",
            )
        )
        validation_artifacts = chain.manifest_artifacts(
            (
                "harness_optimization_task",
                "harness_optimization_apply",
            )
        )

    assert chain.manifest_artifacts(("campaign_manifest",)) == {}
    assert base_artifacts["harness_optimization_task"] == str(paths.task)
    assert base_artifacts["harness_optimization_proposal"] == str(paths.proposal)
    assert base_artifacts["harness_optimization_decision"] == str(paths.decision)
    assert base_artifacts["harness_optimization_optimizer_prompt"] == str(
        paths.optimizer_prompt
    )
    assert base_artifacts["harness_optimization_optimizer_response"] == str(
        paths.optimizer_response
    )
    assert "harness_optimization_advice_report" not in base_artifacts
    assert "harness_optimization_patch" not in base_artifacts
    assert advice_artifacts["harness_optimization_advice_report"] == str(
        paths.advice_report
    )
    assert validation_artifacts["harness_optimization_patch"] == str(paths.patch)
    assert validation_artifacts["harness_optimization_final_decision"] == str(
        paths.final_decision
    )
    assert validation_artifacts["harness_optimization_sandbox"] == str(
        paths.sandbox_dir
    )


def test_shared_campaign_optimization_stage_chain_runs_validation_sequence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        evaluation_path = root / "campaign_evaluation.json"
        paths = shared_harness_optimization_paths(evaluation_path)
        adapter = _FakeCampaignOptimizationAdapter()
        context_artifacts: dict[str, Path] = {"evaluation_report": evaluation_path}
        captured_steps = []
        chain = CampaignOptimizationStageChain(
            target="demo",
            modes=("heuristic_feedback", "llm_feedback"),
            rounds=2,
            context_artifacts=context_artifacts,
            step_runner=lambda step: (
                captured_steps.append(step),
                step.handler(SimpleNamespace()),
            )[1],
            paths_factory=lambda: paths,
            adapter_factory=lambda _paths: adapter,
        )
        plan = RunPlan(
            name="shared_campaign_optimization_validation",
            stages=tuple(chain.stage_factories()[name]() for name in chain.stage_factories()),
            write_topology=False,
            initial_result_keys=("campaign_manifest", "campaign_evaluation"),
            initial_artifact_roles=("evaluation_report",),
        )

        results = RunPlanExecutor().run(
            plan,
            initial_results={
                "campaign_manifest": {"target": "demo"},
                "campaign_evaluation": {"summary": {"mode_count": 2}},
            },
        )

    assert [step.name for step in captured_steps] == [
        "harness_optimization_task",
        "harness_optimization_proposal",
        "harness_optimization_decision",
        "harness_optimization_advice_report",
        "harness_optimization_apply",
        "harness_optimization_candidate_evaluation",
        "harness_optimization_metric_delta",
        "harness_optimization_final_decision",
    ]
    assert captured_steps[0].metadata == {
        "target": "demo",
        "modes": "heuristic_feedback,llm_feedback",
        "rounds": 2,
    }
    assert [name for name, _args in adapter.calls] == [
        "task",
        "proposal",
        "decision",
        "advice",
        "apply",
        "candidate_evaluation",
        "metric_delta",
        "final_decision",
    ]
    task_args = adapter.calls[0][1]
    candidate_args = adapter.calls[5][1]
    final_args = adapter.calls[7][1]
    assert task_args == ({"summary": {"mode_count": 2}}, {"target": "demo"})
    assert candidate_args[2]["status"] == "applied"
    assert candidate_args[3]["candidate_id"] == "candidate-1"
    assert final_args[2]["decision"] == "accepted"
    assert final_args[4]["status"] == "passed"
    assert final_args[5]["summary"]["improved_metric_count"] == 1
    assert results["harness_optimization_patch"]["status"] == "applied"
    assert (
        results["harness_optimization_candidate_manifest"]["candidate_id"]
        == "candidate-1"
    )
    assert (
        results["harness_optimization_final_decision"]["decision"]
        == "accepted_for_review"
    )
    assert context_artifacts == {
        "evaluation_report": evaluation_path,
        "harness_optimization_task": paths.task,
        "harness_optimization_proposal": paths.proposal,
        "harness_optimization_decision": paths.decision,
        "harness_optimization_advice_report": paths.advice_report,
        "harness_optimization_patch": paths.patch,
        "harness_optimization_candidate_manifest": paths.candidate_manifest,
        "harness_optimization_candidate_evaluation": paths.candidate_evaluation,
        "harness_optimization_metric_delta": paths.metric_delta,
        "harness_optimization_final_decision": paths.final_decision,
    }


@dataclass(frozen=True)
class _SharedEvalConfig:
    target: str
    mode: str | None
    round_id: str | None
    evaluation_out: Path | None
    observation_out: Path | None
    monitoring_out: Path | None
    cwd: Path | None = None


class _FakeRoundTraceWriter:
    def __init__(self, calls: list[dict[str, Any]], **kwargs: Any) -> None:
        self.calls = calls
        self.kwargs = kwargs

    def write(self, outputs: SharedHarnessTraceOutputs) -> HarnessTraceResult:
        self.calls.append({"outputs": outputs, **self.kwargs})
        outputs.records.write_text('{"kind":"demo.record"}\n', encoding="utf-8")
        outputs.evaluation.write_text(
            json.dumps({"summary": {"record_count": 1}}) + "\n",
            encoding="utf-8",
        )
        outputs.llm_dataset.write_text('{"kind":"demo.dataset"}\n', encoding="utf-8")
        return HarnessTraceResult(
            records=[{"kind": "demo.record"}],
            evaluation={
                "summary": {"record_count": 1},
                "optimization_hints": {"failing_connectors": ["case_to_dut"]},
            },
            llm_dataset=[{"kind": "demo.dataset"}],
        )


class _FakeCampaignTraceWriter(_FakeRoundTraceWriter):
    pass


def test_shared_run_evaluation_adapter_supports_injected_trace_builder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "events.jsonl"
        monitor = root / "monitor.json"
        manifest_path = root / "round_manifest.json"
        evaluation_out = root / "round_evaluation.json"
        events.write_text("{}\n", encoding="utf-8")
        monitor.write_text("{}\n", encoding="utf-8")
        manifest = {
            "coverage": {"covered_line_count": 3},
            "feedback": {"directive_count": 1},
            "artifacts": {
                "observation_events": str(events),
                "monitoring": str(monitor),
                "round_manifest": str(manifest_path),
            },
        }
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        config = _SharedEvalConfig(
            target="demo",
            mode="feedback_fuzz",
            round_id="round-1",
            evaluation_out=evaluation_out,
            observation_out=events,
            monitoring_out=monitor,
        )
        paths = RunPathResolver(config, artifacts={"round_manifest": manifest_path})
        calls: list[dict[str, Any]] = []
        adapter = SharedRunEvaluationAdapter(
            config=config,
            paths=paths,
            observation_context=ObservationContext(run_id="run-1"),
            round_id=lambda: "round-1",
            trace_builder_factory=lambda **kwargs: _FakeRoundTraceWriter(calls, **kwargs),
            feedback_snapshot_builder=lambda result: {
                "feedback_type": type(result).__name__,
            },
            kind="demo.round_evaluation",
        )

        payload = adapter.run_round(
            {
                "round_manifest": manifest,
                "corpus_validation": ["case-1"],
                "coverage_feedback": "feedback-result",
            }
        )

    assert payload["kind"] == "demo.round_evaluation"
    assert payload["feedback"] == {"directive_count": 1}
    assert payload["case_counts"]["corpus_validation"] == 1
    assert payload["harness_trace"]["status"] == "ok"
    assert payload["harness_trace"]["summary"]["record_count"] == 1
    assert calls[0]["observation_events"] == events
    assert calls[0]["monitoring"] == monitor
    assert calls[0]["round_manifest"] == manifest_path
    assert calls[0]["ignored_hanging_connectors"] == (
        "round_artifacts_to_evaluation",
    )


def test_shared_campaign_evaluation_adapter_supports_injected_rollup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events = root / "campaign_events.jsonl"
        monitor = root / "monitor.json"
        manifest_path = root / "campaign_manifest.json"
        evaluation_out = root / "campaign_evaluation.json"
        events.write_text("{}\n", encoding="utf-8")
        monitor.write_text("{}\n", encoding="utf-8")
        manifest = {
            "target": "demo",
            "run_id": "run-1",
            "artifacts": {
                "observation_events": str(events),
                "monitoring": str(monitor),
                "campaign_manifest": str(manifest_path),
            },
            "modes": [{"mode": "heuristic_feedback", "rounds": [{"round_id": "r1"}]}],
        }
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        calls: list[dict[str, Any]] = []
        adapter = SharedCampaignEvaluationAdapter(
            target="demo",
            path=evaluation_out,
            observation_context=ObservationContext(run_id="run-1"),
            cwd=root,
            trace_builder_factory=lambda **kwargs: _FakeCampaignTraceWriter(
                calls,
                **kwargs,
            ),
            rollup_writer=lambda result, campaign_manifest, path: CampaignRollupAttachment(
                path=path.with_name(f"{path.stem}_custom_rollup.json"),
                payload={
                    "summary": {"round_count": len(campaign_manifest.get("modes", []))}
                },
            ),
            kind="demo.campaign_evaluation",
        )

        payload = adapter.run(manifest)

    assert payload["kind"] == "demo.campaign_evaluation"
    assert payload["summary"]["mode_count"] == 1
    assert payload["harness_trace"]["status"] == "ok"
    assert payload["harness_trace"]["artifacts"]["campaign_trace_rollup"] == str(
        evaluation_out.with_name("campaign_evaluation_custom_rollup.json")
    )
    assert payload["harness_trace"]["campaign_rollup"]["summary"]["round_count"] == 1
    assert calls[0]["observation_events"] == events
    assert calls[0]["monitoring"] == monitor
    assert calls[0]["campaign_manifest"] == manifest_path
    assert calls[0]["ignored_hanging_connectors"] == (
        "campaign_to_evaluation_report",
    )


def test_shared_observation_facade_builds_connector_make_vars() -> None:
    values = shared_observation_make_vars(
        observation_out=Path("events.jsonl"),
        monitoring_out=Path("monitor.json"),
        topology_out=Path("topology.json"),
        observation_context=SharedObservationContext(
            run_id="run-1",
            round_id="round-1",
            observer=SharedNullObserver(),
        ),
        stage_id="coverage_run",
    )

    assert values == [
        "CONNECTOR_OBSERVE_OUT=events.jsonl",
        "CONNECTOR_MONITOR_OUT=monitor.json",
        "CONNECTOR_TOPOLOGY_OUT=topology.json",
        "CONNECTOR_OBSERVE_RUN_ID=run-1",
        "CONNECTOR_OBSERVE_ROUND_ID=round-1",
        "CONNECTOR_OBSERVE_STAGE_ID=coverage_run",
    ]


def test_shared_topology_facade_writes_pipeline_topology() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "topology.json"
        topology = PipelineTopology(
            name="demo",
            components=(
                ComponentNode(name="source", kind="source"),
                ComponentNode(name="sink", kind="sink"),
            ),
            connectors=(
                ConnectorEdge(name="source_to_sink", from_layer="source", to_layer="sink"),
            ),
        )

        write_shared_topology(topology, path)
        payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["name"] == "demo"
    assert payload["components"][0]["name"] == "source"
    assert payload["connectors"][0]["name"] == "source_to_sink"


def test_shared_action_dsl_registry_is_single_source_for_builtin_schema() -> None:
    registry = build_shared_builtin_action_plugin_registry()
    schema = build_shared_builtin_safe_action_dsl_schema()
    plugin = build_shared_builtin_action_plugin(
        "scoreboard_check",
        adapter_kind="demo.scoreboard",
        artifact_role="candidate_scoreboard",
        make_var="HARNESS_SCOREBOARD",
        runtime_action=True,
    )

    assert registry.safe_action_dsl_schema() == schema
    assert registry.dsl_payload_action_types() == (
        "replay_probe",
        "scoreboard_check",
        "coverage_feedback_tuning",
        "mmio_readback",
    )
    assert plugin.dsl_schema == schema["scoreboard_check"]
    assert plugin.runtime_action is True
    assert plugin.payload_validator is not None
    assert plugin.payload_validator({}, "payload") == [
        {"path": "payload", "message": "requires check or mode"}
    ]
