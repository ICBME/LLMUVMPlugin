from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import ObservationContext  # noqa: E402
from fuzz_bfm.bfm_base import ReplayResult  # noqa: E402
from fuzz_bfm.corpus import FuzzCase  # noqa: E402
from fuzz_bfm.target_config import TargetConfig  # noqa: E402
from fuzz_pipeline import (  # noqa: E402
    CANDIDATE_EVALUATION_KIND,
    CANDIDATE_MANIFEST_KIND,
    CandidateAcceptanceThresholds,
    CandidateActionAdapterContext,
    CandidateActionAdapterResult,
    CandidateRegressionSettings,
    CampaignConfig,
    CampaignOrchestrator,
    DECISION_KIND,
    EvaluationBackends,
    FINAL_DECISION_KIND,
    HarnessCandidateRegressionBackend,
    METRIC_DELTA_KIND,
    PATCH_KIND,
    PROPOSAL_KIND,
    TASK_KIND,
    HarnessOptimizationAdapter,
    NoopHarnessOptimizerBackend,
    build_harness_optimization_decision,
    harness_optimization_paths,
)
from fuzz_pipeline.coverage_feedback import (  # noqa: E402
    CoverageFeedbackConfig,
    CoverageFeedbackPipeline,
)
from fuzz_pipeline.harness_runtime_actions import (  # noqa: E402
    COVERAGE_FEEDBACK_TUNING_CONFIG_ENV,
    REPLAY_PROBE_CONFIG_ENV,
    RUNTIME_METRICS_OUT_ENV,
    SCOREBOARD_CHECK_CONFIG_ENV,
    extra_make_var_value,
    load_runtime_action_config,
    merge_runtime_metrics,
)
from fuzz_uvm.observable import (  # noqa: E402
    ObservableReplayDriverAdapter,
    ObservableScoreboardAdapter,
)
from fuzz_uvm.scoreboards import ResultScoreboard  # noqa: E402
from fuzz_uvm.transactions import ReplayRecord  # noqa: E402


def test_harness_optimization_adapter_writes_noop_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        adapter = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
            optimizer_backend=NoopHarnessOptimizerBackend(),
        )

        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = adapter.run_proposal(task)
        decision = adapter.run_decision(task, proposal)

        written_task = json.loads(paths.task.read_text(encoding="utf-8"))
        written_proposal = json.loads(paths.proposal.read_text(encoding="utf-8"))
        written_decision = json.loads(paths.decision.read_text(encoding="utf-8"))

    assert task["kind"] == TASK_KIND
    assert task["summary"]["llm_sample_count"] == 2
    assert task["evidence_index"]["span_ids"] == ["span-dut"]
    assert proposal["kind"] == PROPOSAL_KIND
    assert proposal["status"] == "no_op"
    assert decision["kind"] == DECISION_KIND
    assert decision["decision"] == "accepted"
    assert decision["application_status"] == "not_applied"
    assert written_task == task
    assert written_proposal == proposal
    assert written_decision == decision


def test_harness_optimization_decision_rejects_invalid_action_type() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        task = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
        ).run_task(campaign_evaluation, campaign_manifest)
        proposal = {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": "proposal-invalid",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "action-0",
                    "action_type": "rewrite_unrelated_code",
                    "evidence_refs": [{"span_id": "span-dut"}],
                    "rationale": "invalid action for test",
                }
            ],
        }

        decision = build_harness_optimization_decision(
            task=task,
            proposal=proposal,
        )

    assert decision["decision"] == "rejected"
    assert decision["validation"]["error_count"] == 1
    assert "unsupported action type" in decision["validation"]["errors"][0]["message"]


class RaisingOptimizerBackend:
    def run(self, task: dict) -> dict:
        raise RuntimeError("llm unavailable")


def test_harness_optimization_backend_error_becomes_rejected_decision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        adapter = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
            optimizer_backend=RaisingOptimizerBackend(),
        )

        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = adapter.run_proposal(task)
        decision = adapter.run_decision(task, proposal)

    assert proposal["status"] == "invalid"
    assert proposal["error"]["type"] == "RuntimeError"
    assert decision["decision"] == "rejected"
    assert decision["validation"]["errors"][0]["path"] == "status"


class PassingCandidateEvaluationBackend:
    def run(
        self,
        task: dict,
        proposal: dict,
        patch: dict,
        candidate_manifest: dict,
    ) -> dict:
        return {
            "schema_version": 1,
            "kind": CANDIDATE_EVALUATION_KIND,
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "candidate_id": candidate_manifest.get("candidate_id"),
            "status": "passed",
            "baseline_metrics": {
                "failed_record_count": 1,
                "hanging_span_count": 0,
            },
            "candidate_metrics": {
                "failed_record_count": 0,
                "hanging_span_count": 0,
            },
        }


class InvalidCandidateEvaluationBackend:
    def run(
        self,
        task: dict,
        proposal: dict,
        patch: dict,
        candidate_manifest: dict,
    ) -> str:
        return "invalid"


class StubRegressionCampaign(CampaignOrchestrator):
    def _run_mode(self, mode: str) -> dict:
        runtime_metrics = extra_make_var_value(
            self.config.extra_make_vars,
            RUNTIME_METRICS_OUT_ENV,
        )
        runtime_metrics_path = Path(runtime_metrics) if runtime_metrics else None
        if runtime_metrics_path is not None:
            if extra_make_var_value(self.config.extra_make_vars, REPLAY_PROBE_CONFIG_ENV):
                merge_runtime_metrics(
                    runtime_metrics_path,
                    "replay_probe",
                    {
                        "configured_count": 1,
                        "sample_count": 3,
                        "field_sample_count": 3,
                        "signal_request_count": 1,
                        "unavailable_signal_count": 1,
                    },
                )
            if extra_make_var_value(
                self.config.extra_make_vars,
                SCOREBOARD_CHECK_CONFIG_ENV,
            ):
                merge_runtime_metrics(
                    runtime_metrics_path,
                    "scoreboard_check",
                    {
                        "configured_count": 1,
                        "checked_count": 3,
                        "passed_count": 3,
                        "failed_count": 0,
                        "enforced_failure_count": 0,
                    },
                )
            if extra_make_var_value(
                self.config.extra_make_vars,
                COVERAGE_FEEDBACK_TUNING_CONFIG_ENV,
            ):
                merge_runtime_metrics(
                    runtime_metrics_path,
                    "coverage_feedback_tuning",
                    {
                        "configured_count": 1,
                        "applied_count": 1,
                        "trimmed_gap_count": 2,
                        "weighted_directive_count": 1,
                    },
                )
        return {
            "mode": mode,
            "rounds": [
                {
                    "round_id": f"{mode}_round_00",
                    "round_manifest": str(self._out_dir() / mode / "round.json"),
                    "coverage": {"uncovered_line_count": 0},
                    "feedback": {"directive_count": 1},
                    "artifacts": {
                        **(
                            {"harness_runtime_metrics": str(runtime_metrics_path)}
                            if runtime_metrics_path is not None
                            else {}
                        )
                    },
                    "stages": {},
                }
            ],
        }


class RegressionCampaignEvaluationBackend:
    def __init__(self, *, failed_record_count: int) -> None:
        self.failed_record_count = failed_record_count
        self.manifest: dict | None = None

    def run(self, campaign_manifest: dict) -> dict:
        self.manifest = campaign_manifest
        path = Path(campaign_manifest["artifacts"]["evaluation_report"])
        payload = {
            "kind": "fake.candidate_campaign_evaluation",
            "target": campaign_manifest.get("target"),
            "run_id": campaign_manifest.get("run_id"),
            "summary": {
                "round_count": 1,
                "failed_record_count": self.failed_record_count,
            },
            "harness_trace": {
                "status": "ok",
                "summary": {
                    "record_count": 1,
                    "failed_record_count": self.failed_record_count,
                    "hanging_span_count": 0,
                },
                "artifacts": {},
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return payload


class CustomReplayProbeAdapter:
    action_type = "replay_probe"

    def adapt(
        self,
        actions: tuple[dict, ...],
        context: CandidateActionAdapterContext,
    ) -> CandidateActionAdapterResult:
        artifact_path = context.regression_dir / "custom_replay_probe.json"
        entries = tuple(
            {
                "action_id": action.get("action_id"),
                "action_type": self.action_type,
                "payload": {"custom_adapter": True},
            }
            for action in actions
        )
        artifact_path.write_text(
            json.dumps(
                {
                    "candidate_id": context.candidate_id,
                    "entries": list(entries),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return CandidateActionAdapterResult(
            action_type=self.action_type,
            artifact_role="candidate_custom_replay_probe_config",
            artifact_path=artifact_path,
            make_var="HARNESS_CUSTOM_REPLAY_PROBE_CONFIG",
            entries=entries,
            variants=(
                {
                    "variant_id": "custom_probe",
                    "variant_type": "single_action",
                    "action_ids": [entry["action_id"] for entry in entries],
                    "action_types": [self.action_type],
                    "artifact_paths": [str(artifact_path)],
                    "validation_status": "materialized_not_run",
                },
            ),
            metric_counts={"custom_replay_probe_count": len(entries)},
        )


class FakeReplayStage:
    def build_ref_model(self) -> Any:
        return object()

    def build_replay_driver(self) -> Any:
        return object()

    async def reset_driver(self, target_driver: Any) -> None:
        return None

    async def execute_case(
        self,
        target_driver: Any,
        case: FuzzCase,
        *,
        index: int,
    ) -> ReplayResult:
        return ReplayResult(actual="ok", detail="fake")

    def predict_ref_model(
        self,
        ref_model: Any,
        case: FuzzCase,
        *,
        index: int,
    ) -> ReplayResult:
        return ReplayResult(actual="", expected="ok", detail="fake")


class FakeScoreboardStage:
    def build_scoreboard(self) -> ResultScoreboard:
        return ResultScoreboard("demo")

    def scoreboard_write(
        self,
        checker: ResultScoreboard,
        record: ReplayRecord,
    ) -> None:
        checker.write(record)

    def scoreboard_check(self, checker: ResultScoreboard) -> None:
        checker.check()

    def scoreboard_summary(self, checker: ResultScoreboard) -> dict[str, Any]:
        return checker.summary()


def test_runtime_action_schema_validation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        valid = root / "replay_probe.json"
        valid.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "probe-1",
                            "action_type": "replay_probe",
                            "payload": {"fields": ["case.mode"]},
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        invalid = root / "invalid.json"
        invalid.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "probe-2",
                            "action_type": "replay_probe",
                            "payload": {},
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        negative_tuning = root / "negative_tuning.json"
        negative_tuning.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "tuning-1",
                            "action_type": "coverage_feedback_tuning",
                            "payload": {"max_gap_count": -1},
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        fractional_tuning = root / "fractional_tuning.json"
        fractional_tuning.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "tuning-2",
                            "action_type": "coverage_feedback_tuning",
                            "payload": {"max_gap_count": 1.5},
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )

        config = load_runtime_action_config(valid, action_type="replay_probe")
        try:
            load_runtime_action_config(invalid, action_type="replay_probe")
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("invalid replay_probe payload should fail schema")
        try:
            load_runtime_action_config(
                negative_tuning,
                action_type="coverage_feedback_tuning",
            )
        except ValueError as exc:
            tuning_message = str(exc)
        else:
            raise AssertionError("invalid coverage tuning payload should fail schema")
        try:
            load_runtime_action_config(
                fractional_tuning,
                action_type="coverage_feedback_tuning",
            )
        except ValueError as exc:
            fractional_message = str(exc)
        else:
            raise AssertionError("fractional max_gap_count should fail schema")

    assert config.entries[0].payload["fields"] == ["case.mode"]
    assert "requires fields, signals, or probe" in message
    assert "max_gap_count must be a non-negative integer" in tuning_message
    assert "max_gap_count must be a non-negative integer" in fractional_message


def test_replay_and_scoreboard_runtime_actions_are_consumed() -> None:
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        replay_config = root / "replay_probe.json"
        scoreboard_config = root / "scoreboard_check.json"
        metrics = root / "runtime_metrics.json"
        replay_config.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "probe-1",
                            "action_type": "replay_probe",
                            "payload": {
                                "fields": ["case.mode", "result.actual"],
                                "signals": ["dut.state"],
                            },
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        scoreboard_config.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "scoreboard-1",
                            "action_type": "scoreboard_check",
                            "payload": {"check": "case result must match reference"},
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        old_env = _set_env(
            {
                REPLAY_PROBE_CONFIG_ENV: str(replay_config),
                SCOREBOARD_CHECK_CONFIG_ENV: str(scoreboard_config),
                RUNTIME_METRICS_OUT_ENV: str(metrics),
            }
        )
        try:
            config = TargetConfig(
                name="demo",
                driver="fake:Driver",
                path=root / "demo.toml",
            )
            case = FuzzCase("demo", {"target": "demo", "mode": "read"}, line_no=1)
            replay = ObservableReplayDriverAdapter(
                config,
                stage_adapter=FakeReplayStage(),
            )
            result = asyncio.run(replay.execute(case, index=0))
            record = ReplayRecord(0, case, result=result)
            scoreboard = ObservableScoreboardAdapter(
                config,
                stage_adapter=FakeScoreboardStage(),
            )
            scoreboard.write(record)
            scoreboard.check()
            summary = scoreboard.summary()
            runtime_metrics = json.loads(metrics.read_text(encoding="utf-8"))
        finally:
            _restore_env(old_env)

    assert runtime_metrics["summary"]["replay_probe_sample_count"] == 1
    assert runtime_metrics["summary"]["replay_probe_field_sample_count"] == 2
    assert runtime_metrics["summary"]["scoreboard_check_checked_count"] == 1
    assert summary["harness_scoreboard_checks"]["passed_count"] == 1


def test_coverage_feedback_tuning_runtime_action_is_consumed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        corpus = root / "corpus.jsonl"
        coverage_info = root / "coverage.info"
        tuning = root / "coverage_tuning.json"
        metrics = root / "runtime_metrics.json"
        corpus.write_text('{"target":"demo","mode":"read"}\n', encoding="utf-8")
        coverage_info.write_text("", encoding="utf-8")
        tuning.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "tuning-1",
                            "action_type": "coverage_feedback_tuning",
                            "payload": {
                                "max_gap_count": 1,
                                "directive_weight_multiplier": 2,
                                "prioritize": "uncovered",
                            },
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        result = CoverageFeedbackPipeline(
            CoverageFeedbackConfig(
                target="demo",
                coverage_info=coverage_info,
                corpus=corpus,
                summary_out=root / "summary.json",
                directives_out=root / "directives.json",
                prompt_out=root / "prompt.json",
                heuristic_directives_out=root / "heuristic.json",
                coverage_feedback_tuning=tuning,
                runtime_metrics_out=metrics,
            ),
            ObservationContext(),
        ).run()
        runtime_metrics = json.loads(metrics.read_text(encoding="utf-8"))

    assert result.summary["harness_runtime_actions"][
        "coverage_feedback_tuning"
    ]["configured_count"] == 1
    assert result.summary["harness_runtime_actions"][
        "coverage_feedback_tuning"
    ]["applied_count"] == 1
    assert result.heuristic_directives["directives"][0]["weight"] == 2
    assert runtime_metrics["summary"][
        "coverage_feedback_tuning_weighted_directive_count"
    ] == 1


def test_harness_optimization_sandbox_apply_validates_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        adapter = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
            candidate_evaluation_backend=PassingCandidateEvaluationBackend(),
        )
        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": "proposal-safe",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "scoreboard-1",
                    "action_type": "scoreboard_check",
                    "payload": {"check": "case result must match reference"},
                    "evidence_refs": [{"span_id": "span-dut"}],
                }
            ],
            "evidence_refs": [{"span_id": "span-dut"}],
        }
        decision = adapter.run_decision(task, proposal)

        apply_result = adapter.run_apply(task, proposal, decision)
        patch = apply_result["harness_optimization_patch"]
        candidate_manifest = apply_result["harness_optimization_candidate_manifest"]
        candidate_evaluation = adapter.run_candidate_evaluation(
            task,
            proposal,
            patch,
            candidate_manifest,
        )
        metric_delta = adapter.run_metric_delta(task, candidate_evaluation)
        final_decision = adapter.run_final_decision(
            task,
            proposal,
            decision,
            patch,
            candidate_evaluation,
            metric_delta,
        )
        artifact_path = Path(patch["applied_actions"][0]["artifact_path"])
        artifact_exists = artifact_path.exists()
        artifact_in_sandbox = paths.sandbox_dir in artifact_path.parents

    assert patch["kind"] == PATCH_KIND
    assert patch["status"] == "applied"
    assert patch["safety"]["mainline_modified"] is False
    assert candidate_manifest["kind"] == CANDIDATE_MANIFEST_KIND
    assert artifact_exists
    assert artifact_in_sandbox
    assert candidate_evaluation["kind"] == CANDIDATE_EVALUATION_KIND
    assert metric_delta["kind"] == METRIC_DELTA_KIND
    assert metric_delta["summary"]["improved_metric_count"] == 1
    assert final_decision["kind"] == FINAL_DECISION_KIND
    assert final_decision["decision"] == "accepted_for_review"
    assert final_decision["application_status"] == "not_applied"


def test_harness_optimization_sandbox_skips_unsafe_action_type() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        adapter = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
        )
        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": "proposal-unsafe",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "patch-ref-model",
                    "action_type": "ref_model_patch",
                    "payload": {"patch": "diff --git ..."},
                    "evidence_refs": [{"span_id": "span-dut"}],
                }
            ],
            "evidence_refs": [{"span_id": "span-dut"}],
        }
        decision = adapter.run_decision(task, proposal)

        apply_result = adapter.run_apply(task, proposal, decision)
        patch = apply_result["harness_optimization_patch"]
        candidate_manifest = apply_result["harness_optimization_candidate_manifest"]

    assert decision["decision"] == "accepted"
    assert patch["status"] == "skipped"
    assert patch["summary"]["applied_action_count"] == 0
    assert patch["summary"]["unsafe_skipped_count"] == 1
    assert candidate_manifest["candidate_artifacts"] == []


def test_harness_optimization_invalid_candidate_evaluation_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        adapter = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
            candidate_evaluation_backend=InvalidCandidateEvaluationBackend(),
        )
        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": "proposal-invalid-candidate",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "scoreboard-1",
                    "action_type": "scoreboard_check",
                    "evidence_refs": [{"span_id": "span-dut"}],
                }
            ],
            "evidence_refs": [{"span_id": "span-dut"}],
        }
        decision = adapter.run_decision(task, proposal)
        apply_result = adapter.run_apply(task, proposal, decision)
        patch = apply_result["harness_optimization_patch"]
        candidate_manifest = apply_result["harness_optimization_candidate_manifest"]
        candidate_evaluation = adapter.run_candidate_evaluation(
            task,
            proposal,
            patch,
            candidate_manifest,
        )
        metric_delta = adapter.run_metric_delta(task, candidate_evaluation)
        final_decision = adapter.run_final_decision(
            task,
            proposal,
            decision,
            patch,
            candidate_evaluation,
            metric_delta,
        )

    assert candidate_evaluation["status"] == "error"
    assert candidate_evaluation["error"]["type"] == "TypeError"
    assert final_decision["decision"] == "rejected"
    assert final_decision["reason"] == "candidate_validation_failed"


def test_harness_candidate_regression_backend_runs_sandbox_campaign() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seen_configs: list[CampaignConfig] = []

        def factory(config, observation_context, **kwargs):
            seen_configs.append(config)
            return StubRegressionCampaign(config, observation_context, **kwargs)

        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        backend = HarnessCandidateRegressionBackend(
            settings=CandidateRegressionSettings(
                modes=("heuristic_feedback",),
                rounds=1,
                thresholds=CandidateAcceptanceThresholds(
                    min_improved_metric_count=1,
                ),
            ),
            evaluation_backends=EvaluationBackends(
                campaign_evaluation=RegressionCampaignEvaluationBackend(
                    failed_record_count=0,
                )
            ),
            campaign_orchestrator_factory=factory,
        )
        adapter = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
            candidate_evaluation_backend=backend,
        )
        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": "proposal-regression",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "directive-1",
                    "action_type": "mutation_directive_update",
                    "payload": {
                        "directives": [
                            {"origin": "harness_optimizer", "weight": 2}
                        ]
                    },
                    "evidence_refs": [{"span_id": "span-dut"}],
                },
                {
                    "action_id": "probe-1",
                    "action_type": "replay_probe",
                    "payload": {"signals": ["dut.state"], "sample_on": "posedge"},
                    "evidence_refs": [{"span_id": "span-dut"}],
                },
                {
                    "action_id": "scoreboard-1",
                    "action_type": "scoreboard_check",
                    "payload": {"check": "case result must match reference"},
                    "evidence_refs": [{"span_id": "span-dut"}],
                },
                {
                    "action_id": "tuning-1",
                    "action_type": "coverage_feedback_tuning",
                    "payload": {"max_gap_count": 8, "prioritize": "uncovered"},
                    "evidence_refs": [{"span_id": "span-dut"}],
                },
            ],
            "evidence_refs": [{"span_id": "span-dut"}],
        }
        decision = adapter.run_decision(task, proposal)
        apply_result = adapter.run_apply(task, proposal, decision)
        patch = apply_result["harness_optimization_patch"]
        candidate_manifest = apply_result["harness_optimization_candidate_manifest"]

        candidate_evaluation = adapter.run_candidate_evaluation(
            task,
            proposal,
            patch,
            candidate_manifest,
        )
        metric_delta = adapter.run_metric_delta(task, candidate_evaluation)
        final_decision = adapter.run_final_decision(
            task,
            proposal,
            decision,
            patch,
            candidate_evaluation,
            metric_delta,
        )
        artifacts = candidate_evaluation["artifacts"]
        overlay = json.loads(
            Path(artifacts["candidate_action_overlay"]).read_text(
                encoding="utf-8"
            )
        )
        run_config = json.loads(
            Path(artifacts["candidate_regression_config"]).read_text(
                encoding="utf-8"
            )
        )
        directives_path = Path(artifacts["candidate_mutation_directives"])
        directives = json.loads(directives_path.read_text(encoding="utf-8"))
        replay_probe = json.loads(
            Path(artifacts["candidate_replay_probe_config"]).read_text(
                encoding="utf-8"
            )
        )
        scoreboard = json.loads(
            Path(artifacts["candidate_scoreboard_check_config"]).read_text(
                encoding="utf-8"
            )
        )
        tuning = json.loads(
            Path(artifacts["candidate_coverage_feedback_tuning_config"]).read_text(
                encoding="utf-8"
            )
        )
        ranking = json.loads(
            Path(artifacts["candidate_variant_ranking"]).read_text(
                encoding="utf-8"
            )
        )
        promotion = json.loads(
            Path(artifacts["candidate_promotion_package"]).read_text(
                encoding="utf-8"
            )
        )
        runtime_metrics = json.loads(
            Path(artifacts["candidate_runtime_metrics"]).read_text(
                encoding="utf-8"
            )
        )

    assert seen_configs
    assert seen_configs[0].campaign_plan_profile == "campaign_with_evaluation"
    assert seen_configs[0].initial_directives == directives_path
    assert any(
        item.startswith("HARNESS_REPLAY_PROBE_CONFIG=")
        for item in seen_configs[0].extra_make_vars
    )
    assert any(
        item.startswith("HARNESS_SCOREBOARD_CHECK_CONFIG=")
        for item in seen_configs[0].extra_make_vars
    )
    assert any(
        item.startswith("HARNESS_COVERAGE_FEEDBACK_TUNING_CONFIG=")
        for item in seen_configs[0].extra_make_vars
    )
    assert any(
        item.startswith("HARNESS_RUNTIME_METRICS_OUT=")
        for item in seen_configs[0].extra_make_vars
    )
    assert candidate_evaluation["status"] == "passed"
    assert candidate_evaluation["candidate_metrics"]["failed_record_count"] == 0
    assert candidate_evaluation["candidate_metrics"]["replay_probe_sample_count"] == 3
    assert candidate_evaluation["candidate_metrics"][
        "scoreboard_check_checked_count"
    ] == 3
    assert candidate_evaluation["candidate_metrics"][
        "coverage_feedback_tuning_weighted_directive_count"
    ] == 1
    assert candidate_evaluation["candidate_metrics"]["candidate_action_count"] == 4
    assert candidate_evaluation["candidate_metrics"]["candidate_overlay_count"] == 4
    assert candidate_evaluation["candidate_metrics"]["candidate_variant_count"] == 5
    assert candidate_evaluation["candidate_metrics"]["candidate_directive_count"] == 1
    assert candidate_evaluation["candidate_metrics"][
        "mutation_directive_update_count"
    ] == 1
    assert candidate_evaluation["candidate_metrics"]["replay_probe_count"] == 1
    assert candidate_evaluation["candidate_metrics"]["scoreboard_check_count"] == 1
    assert candidate_evaluation["candidate_metrics"][
        "coverage_feedback_tuning_count"
    ] == 1
    assert overlay["selected_variant_id"] == "combined"
    assert len(overlay["actions"]) == 4
    assert len(overlay["adapter_results"]) == 4
    assert run_config["safety"]["mainline_modified"] is False
    assert run_config["initial_directives"] == str(directives_path)
    assert run_config["runtime_metrics"] == artifacts["candidate_runtime_metrics"]
    assert run_config["adapter_metrics"]["candidate_action_count"] == 4
    assert "candidate_replay_probe_config" in run_config["adapter_artifacts"]
    assert "candidate_scoreboard_check_config" in run_config["adapter_artifacts"]
    assert (
        "candidate_coverage_feedback_tuning_config"
        in run_config["adapter_artifacts"]
    )
    assert len(run_config["extra_make_vars"]) >= 4
    assert directives["directives"][0]["origin"] == "harness_optimizer"
    assert replay_probe["entries"][0]["payload"]["signals"] == ["dut.state"]
    assert scoreboard["entries"][0]["payload"]["check"].startswith("case result")
    assert tuning["entries"][0]["payload"]["max_gap_count"] == 8
    assert runtime_metrics["summary"]["replay_probe_sample_count"] == 3
    assert ranking["top_variant"]["variant_id"] == "combined"
    assert len(ranking["variants"]) == 5
    assert promotion["promotion_status"] == "ready_for_review"
    assert promotion["safety"]["mainline_modified"] is False
    assert candidate_evaluation["summary"]["candidate_variant_count"] == 5
    assert candidate_evaluation["summary"]["promotion_status"] == "ready_for_review"
    assert metric_delta["summary"]["improved_metric_count"] == 1
    assert final_decision["decision"] == "accepted_for_review"
    assert final_decision["summary"]["acceptance_thresholds"][
        "min_improved_metric_count"
    ] == 1


def test_harness_candidate_regression_threshold_rejects_neutral_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seen_configs: list[CampaignConfig] = []

        def factory(config, observation_context, **kwargs):
            seen_configs.append(config)
            return StubRegressionCampaign(config, observation_context, **kwargs)

        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        backend = HarnessCandidateRegressionBackend(
            settings=CandidateRegressionSettings(
                modes=("heuristic_feedback",),
                rounds=1,
                thresholds=CandidateAcceptanceThresholds(
                    min_improved_metric_count=1,
                ),
            ),
            evaluation_backends=EvaluationBackends(
                campaign_evaluation=RegressionCampaignEvaluationBackend(
                    failed_record_count=1,
                )
            ),
            action_adapters={"replay_probe": CustomReplayProbeAdapter()},
            campaign_orchestrator_factory=factory,
        )
        adapter = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
            candidate_evaluation_backend=backend,
        )
        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": "proposal-neutral",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "probe-1",
                    "action_type": "replay_probe",
                    "payload": {"probe": "state"},
                    "evidence_refs": [{"span_id": "span-dut"}],
                }
            ],
            "evidence_refs": [{"span_id": "span-dut"}],
        }
        decision = adapter.run_decision(task, proposal)
        apply_result = adapter.run_apply(task, proposal, decision)
        patch = apply_result["harness_optimization_patch"]
        candidate_manifest = apply_result["harness_optimization_candidate_manifest"]
        candidate_evaluation = adapter.run_candidate_evaluation(
            task,
            proposal,
            patch,
            candidate_manifest,
        )
        metric_delta = adapter.run_metric_delta(task, candidate_evaluation)
        final_decision = adapter.run_final_decision(
            task,
            proposal,
            decision,
            patch,
            candidate_evaluation,
            metric_delta,
        )
        artifacts = candidate_evaluation["artifacts"]
        custom_probe = json.loads(
            Path(artifacts["candidate_custom_replay_probe_config"]).read_text(
                encoding="utf-8"
            )
        )
        run_config = json.loads(
            Path(artifacts["candidate_regression_config"]).read_text(
                encoding="utf-8"
            )
        )

    assert candidate_evaluation["status"] == "passed"
    assert seen_configs
    assert any(
        item.startswith("HARNESS_CUSTOM_REPLAY_PROBE_CONFIG=")
        for item in seen_configs[0].extra_make_vars
    )
    assert custom_probe["entries"][0]["payload"]["custom_adapter"] is True
    assert candidate_evaluation["candidate_metrics"]["custom_replay_probe_count"] == 1
    assert (
        "candidate_custom_replay_probe_config"
        in run_config["adapter_artifacts"]
    )
    assert metric_delta["summary"]["improved_metric_count"] == 0
    assert final_decision["decision"] == "rejected"
    assert final_decision["reason"] == "candidate_metric_improvement_below_threshold"


def _set_env(values: dict[str, str]) -> dict[str, str | None]:
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    return old


def _restore_env(values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _source_payloads(root: Path) -> tuple[dict, dict, Path]:
    harness_evaluation = root / "campaign_evaluation_harness_evaluation.json"
    llm_dataset = root / "campaign_evaluation_llm_dataset.jsonl"
    campaign_rollup = root / "campaign_evaluation_campaign_rollup.json"
    manifest_path = root / "campaign_manifest.json"
    libafl_manifest = root / "Cargo.toml"
    libafl_manifest.write_text("[package]\nname='demo'\n", encoding="utf-8")
    harness_evaluation.write_text(
        json.dumps(
            {
                "summary": {"record_count": 1, "failed_record_count": 1},
                "optimization_hints": {
                    "failing_connectors": ["case_to_dut"],
                },
                "trace_quality": {"hanging_span_count": 0},
                "failed_records": [
                    {
                        "span_id": "span-dut",
                        "connector": "case_to_dut",
                        "case_id": "case-0",
                        "directive_id": "dir-a",
                    }
                ],
                "slowest_records": [],
                "failure_clusters": [
                    {
                        "connector": "case_to_dut",
                        "examples": [
                            {
                                "span_id": "span-dut",
                                "connector": "case_to_dut",
                                "case_id": "case-0",
                                "directive_id": "dir-a",
                            }
                        ],
                    }
                ],
                "case_summary": [{"case_id": "case-0"}],
                "directive_summary": [{"directive_id": "dir-a"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    llm_dataset.write_text('{"sample":1}\n{"sample":2}\n', encoding="utf-8")
    campaign_rollup.write_text(
        json.dumps(
            {
                "summary": {"round_count": 1, "record_count": 1},
                "coverage_trends": [
                    {
                        "metric": "uncovered_line_count",
                        "values": [{"round_id": "round-0", "value": 3}],
                    }
                ],
                "failure_trends": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "target": "demo",
        "run_id": "run-1",
        "cwd": str(root),
        "config": {
            "iters": 8,
            "max_seeds": 4,
            "seed": 7,
            "cargo": "cargo",
            "make": "make",
            "extra_make_vars": [],
        },
        "artifacts": {
            "campaign_manifest": str(manifest_path),
            "libafl_manifest": str(libafl_manifest),
        },
        "modes": [],
    }
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    campaign_evaluation = {
        "target": "demo",
        "run_id": "run-1",
        "summary": {"round_count": 1},
        "harness_trace": {
            "status": "ok",
            "artifacts": {
                "harness_evaluation": str(harness_evaluation),
                "llm_optimization_dataset": str(llm_dataset),
                "campaign_trace_rollup": str(campaign_rollup),
            },
        },
    }
    return campaign_evaluation, manifest, manifest_path
