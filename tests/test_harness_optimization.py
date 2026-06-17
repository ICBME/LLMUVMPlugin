from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import ObservationContext  # noqa: E402
from fuzz_bfm.bfm_base import ReplayResult  # noqa: E402
from fuzz_bfm.corpus import FuzzCase  # noqa: E402
from fuzz_bfm.target_config import TargetConfig, load_target_config  # noqa: E402
from fuzz_pipeline import (  # noqa: E402
    ADVICE_REPORT_KIND,
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
    HarnessActionPlugin,
    HarnessCandidateRegressionBackend,
    HarnessGapActionabilityContext,
    LlmHarnessOptimizerBackend,
    METRIC_DELTA_KIND,
    PATCH_KIND,
    PROPOSAL_KIND,
    TASK_KIND,
    HarnessOptimizationAdapter,
    default_harness_plugin_registry,
    load_harness_plugin_registry,
    NoopHarnessOptimizerBackend,
    PromptOnlyHarnessOptimizerBackend,
    build_candidate_action_effect_report,
    build_candidate_gap_actionability_report,
    build_candidate_promotion_package,
    build_gap_actionability_minimal_candidate_proposal,
    build_harness_optimization_decision,
    build_harness_optimization_final_decision,
    build_harness_optimization_metric_delta,
    harness_optimization_paths,
    select_candidate_variants,
    validate_harness_plugin_registry,
)
from harness_optimization.io import read_optional_json  # noqa: E402
from harness_optimization.candidate_execution import (  # noqa: E402
    CANDIDATE_ACTION_OVERLAY_KIND as SHARED_CANDIDATE_ACTION_OVERLAY_KIND,
    CANDIDATE_REGRESSION_CONFIG_KIND as SHARED_CANDIDATE_REGRESSION_CONFIG_KIND,
    CandidateActionAdapterResult as SharedCandidateActionAdapterResult,
    CandidateRegressionSettings as SharedCandidateRegressionSettings,
    build_candidate_action_overlay as build_shared_candidate_action_overlay,
    candidate_regression_config_payload as build_shared_candidate_regression_config,
)
from harness_optimization.runtime import (  # noqa: E402
    FUNCTIONAL_COVERAGE_OUT_ENV as SHARED_LEGACY_FUNCTIONAL_COVERAGE_OUT_ENV,
    REPLAY_CORPUS_ENV as SHARED_LEGACY_REPLAY_CORPUS_ENV,
    REPLAY_TARGET_CONFIG_ENV as SHARED_LEGACY_REPLAY_TARGET_CONFIG_ENV,
    REPLAY_TARGET_ENV as SHARED_LEGACY_REPLAY_TARGET_ENV,
    read_runtime_metrics,
)
from fuzz_pipeline.harness_candidate_regression import (  # noqa: E402
    adapt_candidate_actions,
    build_candidate_action_overlay as build_fuzz_candidate_action_overlay,
    candidate_regression_config_payload as build_fuzz_candidate_regression_config,
    classify_gap_actionability,
)
from fuzz_pipeline.coverage_feedback import (  # noqa: E402
    CoverageFeedbackConfig,
    CoverageFeedbackPipeline,
)
from fuzz_pipeline.harness_runtime_actions import (  # noqa: E402
    COVERAGE_FEEDBACK_TUNING_CONFIG_ENV,
    HarnessRuntimeActionManager,
    MMIO_READBACK_CONFIG_ENV,
    REPLAY_PROBE_CONFIG_ENV,
    RUNTIME_METRICS_OUT_ENV,
    RUNTIME_ACTION_PLUGINS_ENV,
    SCOREBOARD_CHECK_CONFIG_ENV,
    CoverageFeedbackTuningRuntime,
    MmioReadbackRuntime,
    ReplayProbeRuntime,
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


class RepairingLlmTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "response_format": response_format,
            }
        )
        if len(self.calls) == 1:
            content = {
                "schema_version": 1,
                "kind": PROPOSAL_KIND,
                "proposal_id": "proposal-needs-repair",
                "status": "proposed",
                "actions": [
                    {
                        "action_id": "scoreboard-bad",
                        "action_type": "scoreboard_check",
                        "payload": {"mode": "field_equals"},
                        "evidence_refs": [{"span_id": "span-dut"}],
                    }
                ],
                "evidence_refs": [{"span_id": "span-dut"}],
            }
        else:
            content = {
                "schema_version": 1,
                "kind": PROPOSAL_KIND,
                "proposal_id": "proposal-repaired",
                "status": "proposed",
                "source": "fake-llm",
                "actions": [
                    {
                        "action_id": "scoreboard-1",
                        "action_type": "scoreboard_check",
                        "payload": {
                            "mode": "field_equals",
                            "field": "result.actual",
                            "expected": "ok",
                        },
                        "evidence_refs": [{"span_id": "span-dut"}],
                    }
                ],
                "evidence_refs": [{"span_id": "span-dut"}],
                "rationale": "repair scoreboard field check",
            }
        return {
            "id": f"resp-{len(self.calls)}",
            "model": model,
            "choices": [{"message": {"content": json.dumps(content)}}],
        }


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


def test_llm_optimizer_backend_writes_prompt_response_and_repairs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        action_effect = root / "candidate_action_effect_report.json"
        action_effect.write_text(
            json.dumps(
                {
                    "summary": {
                        "variant_count": 1,
                        "runtime_action_count": 1,
                        "consumed_action_count": 1,
                    },
                    "variants": [
                        {
                            "variant_id": "combined",
                            "actions": [
                                {
                                    "action_id": "scoreboard-old",
                                    "action_type": "scoreboard_check",
                                    "consumed": True,
                                }
                            ],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        campaign_manifest["artifacts"][
            "candidate_action_effect_report"
        ] = str(action_effect)
        manifest_path.write_text(json.dumps(campaign_manifest) + "\n", encoding="utf-8")
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        transport = RepairingLlmTransport()
        adapter = HarnessOptimizationAdapter(
            target="demo",
            paths=paths,
            campaign_evaluation_path=evaluation_path,
            campaign_manifest_path=manifest_path,
            cwd=root,
            optimizer_backend=LlmHarnessOptimizerBackend(
                model="fake-model",
                transport=transport,
                source="fake-llm",
                max_repair_attempts=1,
            ),
        )

        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = adapter.run_proposal(task)
        decision = adapter.run_decision(task, proposal)
        prompt = json.loads(paths.optimizer_prompt.read_text(encoding="utf-8"))
        response = json.loads(paths.optimizer_response.read_text(encoding="utf-8"))

    assert len(transport.calls) == 2
    assert task["summary"]["action_effect"]["consumed_action_count"] == 1
    assert prompt["context"]["action_effect_report"]["summary"][
        "consumed_action_count"
    ] == 1
    assert response["attempt_count"] == 2
    assert response["attempts"][0]["valid"] is False
    assert response["attempts"][1]["valid"] is True
    assert proposal["proposal_id"] == "proposal-repaired"
    assert proposal["llm_provenance"]["prompt_artifact"] == str(paths.optimizer_prompt)
    assert proposal["llm_provenance"]["response_artifact"] == str(
        paths.optimizer_response
    )
    assert proposal["llm_provenance"]["repaired"] is True
    assert decision["decision"] == "accepted"


def test_prompt_only_optimizer_writes_prompt_response_and_advice_report() -> None:
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
            optimizer_backend=PromptOnlyHarnessOptimizerBackend(
                model="fake-model",
                sample_limit=1,
            ),
        )

        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = adapter.run_proposal(task)
        decision = adapter.run_decision(task, proposal)
        report = adapter.run_advice_report(task, proposal, decision)
        prompt = json.loads(paths.optimizer_prompt.read_text(encoding="utf-8"))
        response = json.loads(paths.optimizer_response.read_text(encoding="utf-8"))
        written_report = json.loads(paths.advice_report.read_text(encoding="utf-8"))

    assert proposal["status"] == "no_op"
    assert proposal["source"] == "prompt_only"
    assert proposal["llm_provenance"]["transport_called"] is False
    assert prompt["kind"] == "libafl_bfm_fuzz.harness_optimizer_prompt"
    assert len(prompt["context"]["llm_dataset_samples"]) == 1
    assert response["status"] == "not_called"
    assert report["kind"] == ADVICE_REPORT_KIND
    assert report["status"] == "no_action"
    assert report["advice_only_guard"]["sandbox_apply_triggered"] is False
    assert report["advice_only_guard"]["candidate_regression_triggered"] is False
    assert report["validation_scope"]["candidate_metric_validation"] is False
    assert report["reproducibility"]["artifacts"]["optimizer_prompt"] == str(
        paths.optimizer_prompt
    )
    assert written_report == report
    assert not paths.patch.exists()
    assert not paths.candidate_evaluation.exists()


def test_harness_optimization_decision_rejects_invalid_safe_action_dsl() -> None:
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
            "proposal_id": "proposal-invalid-dsl",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "scoreboard-1",
                    "action_type": "scoreboard_check",
                    "payload": {"mode": "field_range", "field": "result.latency"},
                    "evidence_refs": [{"span_id": "span-dut"}],
                },
                {
                    "action_id": "tuning-1",
                    "action_type": "coverage_feedback_tuning",
                    "payload": {"min_weight": 3, "max_weight": 1},
                    "evidence_refs": [{"span_id": "span-dut"}],
                },
                {
                    "action_id": "probe-1",
                    "action_type": "replay_probe",
                    "evidence_refs": [{"span_id": "span-dut"}],
                },
            ],
            "evidence_refs": [{"span_id": "span-dut"}],
        }

        decision = build_harness_optimization_decision(task=task, proposal=proposal)

    assert decision["decision"] == "rejected"
    messages = [item["message"] for item in decision["validation"]["errors"]]
    assert "field_range requires min or max" in messages
    assert "min_weight must be <= max_weight" in messages
    assert "replay_probe payload is required" in messages


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
            replay_config = extra_make_var_value(
                self.config.extra_make_vars,
                REPLAY_PROBE_CONFIG_ENV,
            )
            if replay_config:
                merge_runtime_metrics(
                    runtime_metrics_path,
                    "replay_probe",
                    {
                        "configured_count": 1,
                        "sample_count": 3,
                        "field_sample_count": 3,
                        "signal_request_count": 1,
                        "unavailable_signal_count": 1,
                        "actions": _runtime_metric_actions(
                            replay_config,
                            {
                                "sample_count": 3,
                                "field_sample_count": 3,
                                "signal_request_count": 1,
                                "unavailable_signal_count": 1,
                            },
                        ),
                    },
                )
            scoreboard_config = extra_make_var_value(
                self.config.extra_make_vars,
                SCOREBOARD_CHECK_CONFIG_ENV,
            )
            if scoreboard_config:
                merge_runtime_metrics(
                    runtime_metrics_path,
                    "scoreboard_check",
                    {
                        "configured_count": 1,
                        "checked_count": 3,
                        "passed_count": 3,
                        "failed_count": 0,
                        "enforced_failure_count": 0,
                        "actions": _runtime_metric_actions(
                            scoreboard_config,
                            {
                                "checked_count": 3,
                                "passed_count": 3,
                                "failed_count": 0,
                                "enforced_failure_count": 0,
                            },
                        ),
                    },
                )
            tuning_config = extra_make_var_value(
                self.config.extra_make_vars,
                COVERAGE_FEEDBACK_TUNING_CONFIG_ENV,
            )
            if tuning_config:
                merge_runtime_metrics(
                    runtime_metrics_path,
                    "coverage_feedback_tuning",
                    {
                        "configured_count": 1,
                        "applied_count": 1,
                        "trimmed_gap_count": 2,
                        "weighted_directive_count": 1,
                        "actions": _runtime_metric_actions(
                            tuning_config,
                            {
                                "applied_count": 1,
                                "trimmed_gap_count": 2,
                                "weighted_directive_count": 1,
                            },
                        ),
                    },
                )
            mmio_config = extra_make_var_value(
                self.config.extra_make_vars,
                MMIO_READBACK_CONFIG_ENV,
            )
            if mmio_config:
                merge_runtime_metrics(
                    runtime_metrics_path,
                    "mmio_readback",
                    {
                        "configured_count": 1,
                        "sample_count": 3,
                        "read_count": 6,
                        "skipped_count": 0,
                        "unsupported_count": 0,
                        "unresolved_count": 0,
                        "unique_address_count": 2,
                        "actions": _runtime_metric_actions(
                            mmio_config,
                            {
                                "sample_count": 3,
                                "read_count": 6,
                                "skipped_count": 0,
                                "unsupported_count": 0,
                                "unresolved_count": 0,
                            },
                        ),
                    },
                )
        summary_path = self._out_dir() / mode / "round_00" / "coverage_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(
                {
                    "target": self.config.target,
                    "uncovered_line_count": 0,
                    "rtl_gap_summary": {
                        "top_gaps": [
                            {
                                "id": "gap-readback",
                                "file": "/repo/example/aes/src/rtl/aes.v",
                                "line": 253,
                                "code": "ADDR_NAME0: tmp_read_data = CORE_NAME0;",
                                "primary_kind": "line",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
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
                        ),
                        "coverage_summary": str(summary_path),
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


class SequenceRegressionCampaignEvaluationBackend:
    def __init__(self, failed_record_counts: tuple[int, ...]) -> None:
        self.failed_record_counts = failed_record_counts
        self.calls: list[tuple[dict, int]] = []

    def run(self, campaign_manifest: dict) -> dict:
        index = min(len(self.calls), len(self.failed_record_counts) - 1)
        failed_record_count = self.failed_record_counts[index]
        self.calls.append((campaign_manifest, failed_record_count))
        path = Path(campaign_manifest["artifacts"]["evaluation_report"])
        payload = {
            "kind": "fake.candidate_campaign_evaluation",
            "target": campaign_manifest.get("target"),
            "run_id": campaign_manifest.get("run_id"),
            "summary": {
                "round_count": 1,
                "failed_record_count": failed_record_count,
            },
            "harness_trace": {
                "status": "ok",
                "summary": {
                    "record_count": 1,
                    "failed_record_count": failed_record_count,
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


def test_read_optional_json_raises_on_invalid_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "broken.json"
        path.write_text("{not json}\n", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            read_optional_json(path)


def test_read_runtime_metrics_raises_on_invalid_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runtime_metrics.json"
        path.write_text("{not json}\n", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            read_runtime_metrics(path)


def test_shared_runtime_legacy_env_constants_are_preserved() -> None:
    assert SHARED_LEGACY_REPLAY_TARGET_ENV == "FUZZ_TARGET"
    assert SHARED_LEGACY_REPLAY_TARGET_CONFIG_ENV == "FUZZ_TARGET_CONFIG"
    assert SHARED_LEGACY_REPLAY_CORPUS_ENV == "LIBAFL_CORPUS"
    assert SHARED_LEGACY_FUNCTIONAL_COVERAGE_OUT_ENV == "UVM_FUNCTIONAL_COVERAGE_OUT"


def test_shared_candidate_execution_defaults_use_generic_kinds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        adapter_result = SharedCandidateActionAdapterResult(
            action_type="scoreboard_check",
            artifact_role="candidate_scoreboard_check_config",
            artifact_path=root / "scoreboard.json",
            make_var="HARNESS_SCOREBOARD_CHECK_CONFIG",
            entries=(
                {
                    "action_id": "scoreboard-1",
                    "action_type": "scoreboard_check",
                    "payload": {"mode": "field_equals", "field": "result.actual"},
                },
            ),
            variants=(
                {
                    "variant_id": "action_scoreboard_1",
                    "variant_type": "single_action",
                    "action_ids": ["scoreboard-1"],
                    "action_types": ["scoreboard_check"],
                    "artifact_paths": [str(root / "scoreboard.json")],
                    "validation_status": "materialized_not_run",
                },
            ),
        )
        config = CampaignConfig(
            target="demo",
            out_dir=root / "campaign",
            libafl_manifest=root / "Cargo.toml",
            modes=("heuristic_feedback",),
            campaign_manifest_out=root / "campaign_manifest.json",
            campaign_evaluation_out=root / "campaign_evaluation.json",
        )

        overlay = build_shared_candidate_action_overlay(
            {"candidate_id": "candidate-1"},
            (adapter_result,),
        )
        payload = build_shared_candidate_regression_config(
            config,
            action_overlay=root / "overlay.json",
            adapter_results=(adapter_result,),
            initial_directives=None,
            runtime_metrics=root / "runtime_metrics.json",
            settings=SharedCandidateRegressionSettings(),
        )

    assert overlay["kind"] == SHARED_CANDIDATE_ACTION_OVERLAY_KIND
    assert overlay["selected_variant_id"] == "combined"
    assert payload["kind"] == SHARED_CANDIDATE_REGRESSION_CONFIG_KIND
    assert payload["validation_settings"]["campaign_plan_profile"] == (
        "campaign_with_evaluation"
    )


def test_fuzz_pipeline_candidate_helpers_keep_business_kinds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        adapter_result = SharedCandidateActionAdapterResult(
            action_type="scoreboard_check",
            artifact_role="candidate_scoreboard_check_config",
            artifact_path=root / "scoreboard.json",
            make_var="HARNESS_SCOREBOARD_CHECK_CONFIG",
            entries=(
                {
                    "action_id": "scoreboard-1",
                    "action_type": "scoreboard_check",
                    "payload": {"mode": "field_equals", "field": "result.actual"},
                },
            ),
        )
        config = CampaignConfig(
            target="demo",
            out_dir=root / "campaign",
            libafl_manifest=root / "Cargo.toml",
            modes=("heuristic_feedback",),
            campaign_manifest_out=root / "campaign_manifest.json",
            campaign_evaluation_out=root / "campaign_evaluation.json",
        )

        overlay = build_fuzz_candidate_action_overlay(
            {"candidate_id": "candidate-1"},
            (adapter_result,),
        )
        payload = build_fuzz_candidate_regression_config(
            config,
            action_overlay=root / "overlay.json",
            adapter_results=(adapter_result,),
            initial_directives=None,
            runtime_metrics=root / "runtime_metrics.json",
            settings=CandidateRegressionSettings(),
        )

    assert overlay["kind"] == (
        "libafl_bfm_fuzz.harness_optimization_candidate_action_overlay"
    )
    assert payload["kind"] == "libafl_bfm_fuzz.harness_candidate_regression_config"


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
                            "payload": {
                                "fields": ["case.mode"],
                                "max_samples": 1,
                                "case_filter": {"case.mode": "read"},
                            },
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        valid_scoreboard = root / "scoreboard_field_range.json"
        valid_scoreboard.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "scoreboard-1",
                            "action_type": "scoreboard_check",
                            "payload": {
                                "mode": "field_range",
                                "field": "result.latency",
                                "min": 0,
                                "max": 10,
                            },
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
        invalid_weight_bounds = root / "invalid_weight_bounds.json"
        invalid_weight_bounds.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "tuning-3",
                            "action_type": "coverage_feedback_tuning",
                            "payload": {"min_weight": 3, "max_weight": 1},
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        valid_mmio = root / "mmio_readback.json"
        valid_mmio.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "mmio-1",
                            "action_type": "mmio_readback",
                            "payload": {
                                "registers": ["ADDR_NAME0", "ADDR_STATUS"],
                                "addresses": ["0x30"],
                                "max_reads": 3,
                                "case_filter": {"case.mode": "read"},
                            },
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        invalid_mmio = root / "invalid_mmio_readback.json"
        invalid_mmio.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "mmio-2",
                            "action_type": "mmio_readback",
                            "payload": {"addresses": ["not-an-address"]},
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )

        config = load_runtime_action_config(valid, action_type="replay_probe")
        scoreboard_config = load_runtime_action_config(
            valid_scoreboard,
            action_type="scoreboard_check",
        )
        mmio_config = load_runtime_action_config(
            valid_mmio,
            action_type="mmio_readback",
        )
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
        try:
            load_runtime_action_config(
                invalid_weight_bounds,
                action_type="coverage_feedback_tuning",
            )
        except ValueError as exc:
            weight_bounds_message = str(exc)
        else:
            raise AssertionError("invalid tuning weight bounds should fail schema")
        try:
            load_runtime_action_config(invalid_mmio, action_type="mmio_readback")
        except ValueError as exc:
            mmio_message = str(exc)
        else:
            raise AssertionError("invalid mmio_readback payload should fail schema")

    assert config.entries[0].payload["fields"] == ["case.mode"]
    assert scoreboard_config.entries[0].payload["mode"] == "field_range"
    assert mmio_config.entries[0].payload["registers"] == [
        "ADDR_NAME0",
        "ADDR_STATUS",
    ]
    assert "requires fields, signals, or probe" in message
    assert "max_gap_count must be a non-negative integer" in tuning_message
    assert "max_gap_count must be a non-negative integer" in fractional_message
    assert "min_weight must be <= max_weight" in weight_bounds_message
    assert "addresses must be integers or hex strings" in mmio_message


def test_harness_plugin_registry_extends_action_schema_and_adapter() -> None:
    def custom_payload_errors(
        payload: dict[str, Any],
        path: str,
    ) -> list[dict[str, str]]:
        if isinstance(payload.get("flag"), bool):
            return []
        return [{"path": f"{path}.flag", "message": "expected bool"}]

    plugin = HarnessActionPlugin(
        action_type="custom_probe",
        payload_required=True,
        dsl_schema={"payload_fields": {"flag": "bool"}},
        payload_validator=custom_payload_errors,
        adapter_kind="demo.custom_probe_config",
        artifact_role="candidate_custom_probe_config",
        make_var="HARNESS_CUSTOM_PROBE_CONFIG",
        runtime_action=True,
    )
    registry = default_harness_plugin_registry().with_action_plugin(plugin)

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
            plugin_registry=registry,
        )
        task = adapter.run_task(campaign_evaluation, campaign_manifest)
        proposal = {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": "proposal-custom-plugin",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "custom-1",
                    "action_type": "custom_probe",
                    "payload": {"flag": True},
                    "evidence_refs": [{"span_id": "span-dut"}],
                }
            ],
            "evidence_refs": [{"span_id": "span-dut"}],
        }
        decision = adapter.run_decision(task, proposal)
        apply_result = adapter.run_apply(task, proposal, decision)
        candidate_manifest = apply_result["harness_optimization_candidate_manifest"]
        context = CandidateActionAdapterContext(
            candidate_id="candidate-custom",
            regression_dir=root / "candidate",
        )
        adapter_results = adapt_candidate_actions(
            (
                {
                    "action_id": "custom-1",
                    "action_type": "custom_probe",
                    "artifact_path": None,
                    "evidence_refs": [{"span_id": "span-dut"}],
                    "action": {"payload": {"flag": True}},
                },
            ),
            context,
            adapters=None,
            plugin_registry=registry,
        )
        config_payload = json.loads(
            adapter_results[0].artifact_path.read_text(encoding="utf-8")
        )

    assert "custom_probe" in task["constraints"]["allowed_action_types"]
    assert task["constraints"]["safe_action_dsl"]["custom_probe"] == {
        "payload_fields": {"flag": "bool"}
    }
    assert task["constraints"]["plugin_validation"]["valid"] is True
    assert (
        task["constraints"]["plugin_provenance"]["summary"]["runtime_action_count"]
        >= 1
    )
    assert decision["decision"] == "accepted"
    assert candidate_manifest["candidate_artifacts"][0]["action_type"] == (
        "custom_probe"
    )
    assert candidate_manifest["safety"]["plugin_validation"]["valid"] is True
    assert adapter_results[0].artifact_role == "candidate_custom_probe_config"
    assert adapter_results[0].make_var_assignment().startswith(
        "HARNESS_CUSTOM_PROBE_CONFIG="
    )
    assert config_payload["kind"] == "demo.custom_probe_config"
    assert config_payload["entries"][0]["payload"] == {"flag": True}


def test_harness_plugin_registry_loads_dynamic_plugin_spec() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        module = root / "demo_harness_plugin.py"
        module.write_text(
            "\n".join(
                [
                    "from fuzz_pipeline.harness_plugins import HarnessActionPlugin",
                    "",
                    "def classify_demo_gap(gap, context):",
                    "    if context.target != 'dynamic_core' or gap.get('id') != 'gap-dynamic':",
                    "        return None",
                    "    return {",
                    "        **gap,",
                    "        'actionability': 'reachable_with_dynamic_probe',",
                    "        'actionability_reason': 'Dynamic plugin owns this target surface.',",
                    "        'recommended_action_type': 'dynamic_probe',",
                    "        'suggested_payload': {'enabled': True},",
                    "    }",
                    "",
                    "class DemoHarnessPlugin:",
                    "    action_plugins = (",
                    "        HarnessActionPlugin(",
                    "            action_type='dynamic_probe',",
                    "            payload_required=True,",
                    "            dsl_schema={'payload_fields': {'enabled': 'bool'}},",
                    "            adapter_kind='demo.dynamic_probe_config',",
                    "            artifact_role='candidate_dynamic_probe_config',",
                    "            make_var='HARNESS_DYNAMIC_PROBE_CONFIG',",
                    "            runtime_action=True,",
                    "        ),",
                    "    )",
                    "    gap_actionability_classifiers = (classify_demo_gap,)",
                    "",
                    "def build_plugin(**kwargs):",
                    "    return DemoHarnessPlugin()",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        expected_source_hash = hashlib.sha256(module.read_bytes()).hexdigest()
        sys.path.insert(0, str(root))
        try:
            registry = load_harness_plugin_registry(
                ["demo_harness_plugin:build_plugin"],
                base_registry=default_harness_plugin_registry(),
            )
            context = CandidateActionAdapterContext(
                candidate_id="candidate-dynamic",
                regression_dir=root / "candidate",
            )
            adapter_results = adapt_candidate_actions(
                (
                    {
                        "action_id": "dynamic-1",
                        "action_type": "dynamic_probe",
                        "artifact_path": None,
                        "evidence_refs": [],
                        "action": {"payload": {"enabled": True}},
                    },
                ),
                context,
                adapters=None,
                plugin_registry=registry,
            )
            classified = registry.classify_gap_actionability(
                {"id": "gap-dynamic", "file": "fake_core.sv", "line": 7},
                HarnessGapActionabilityContext(target="dynamic_core"),
            )
        finally:
            sys.path.remove(str(root))

    plugin = registry.action_plugin("dynamic_probe")
    assert plugin is not None
    assert plugin.artifact_role == "candidate_dynamic_probe_config"
    assert registry.validation_json()["valid"] is True
    registry_snapshot = registry.to_json()
    assert registry_snapshot["summary"]["runtime_action_count"] >= 1
    assert len(registry_snapshot["fingerprint"]) == 64
    assert registry_snapshot["plugin_sources"][0]["source_file"] == str(
        module.resolve()
    )
    assert registry_snapshot["plugin_sources"][0]["source_sha256"] == (
        expected_source_hash
    )
    assert classified["actionability"] == "reachable_with_dynamic_probe"
    assert classified["recommended_action_type"] == "dynamic_probe"
    assert adapter_results[0].artifact_role == "candidate_dynamic_probe_config"
    assert adapter_results[0].make_var_assignment().startswith(
        "HARNESS_DYNAMIC_PROBE_CONFIG="
    )
    assert "demo_harness_plugin:build_plugin" in registry.plugin_specs


def test_harness_plugin_registry_contract_validation_flags_invalid_plugin() -> None:
    registry = default_harness_plugin_registry().with_action_plugin(
        HarnessActionPlugin(
            action_type="bad action",
            payload_required=True,
            adapter_kind="demo.partial_config",
        )
    )

    validation = validate_harness_plugin_registry(registry)

    assert validation["valid"] is False
    messages = [item["message"] for item in validation["errors"]]
    assert any("must contain only" in message for message in messages)
    assert any("payload-required actions must define" in message for message in messages)
    assert any("provided together" in message for message in messages)


def test_fuzz_pipeline_plugin_registry_instance_methods_keep_business_kinds() -> None:
    registry = default_harness_plugin_registry()

    snapshot = registry.to_json()
    provenance = registry.provenance_json()
    validation = registry.validation_json()

    assert snapshot["kind"] == "libafl_bfm_fuzz.harness_plugin_registry"
    assert snapshot["snapshot_schema"]["kind"] == (
        "libafl_bfm_fuzz.harness_plugin_registry.schema"
    )
    assert provenance["kind"] == "libafl_bfm_fuzz.harness_plugin_provenance"
    assert validation["kind"] == "libafl_bfm_fuzz.harness_plugin_contract_validation"


def test_candidate_regression_strict_plugin_validation_gates_invalid_registry() -> None:
    registry = default_harness_plugin_registry().with_action_plugin(
        HarnessActionPlugin(
            action_type="bad action",
            payload_required=True,
            adapter_kind="demo.partial_config",
        )
    )
    backend = HarnessCandidateRegressionBackend(
        settings=CandidateRegressionSettings(strict_plugin_validation=True),
        plugin_registry=registry,
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        try:
            backend.run(
                task={"target": "demo"},
                proposal={"proposal_id": "proposal-bad"},
                patch={"summary": {"applied_action_count": 1}},
                candidate_manifest={
                    "candidate_id": "candidate-bad",
                    "sandbox_dir": str(root / "candidate"),
                },
            )
        except ValueError as exc:
            message = str(exc)
        else:
            raise AssertionError("strict plugin validation should gate backend.run")

    assert "candidate regression plugin registry" in message
    assert "bad action" in message
    assert "payload-required actions must define" in message


def test_replay_probe_runtime_consumes_filter_and_sample_limit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        replay_config = root / "replay_probe.json"
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
                                "max_samples": 1,
                                "case_filter": {"case.mode": "read"},
                            },
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runtime = ReplayProbeRuntime(
            load_runtime_action_config(replay_config, action_type="replay_probe"),
            metrics_out=metrics,
        )
        runtime.sample(
            index=0,
            case=FuzzCase("demo", {"mode": "read"}, line_no=1),
            result=ReplayResult(actual="ok"),
        )
        runtime.sample(
            index=1,
            case=FuzzCase("demo", {"mode": "read"}, line_no=2),
            result=ReplayResult(actual="ok"),
        )
        runtime.sample(
            index=2,
            case=FuzzCase("demo", {"mode": "write"}, line_no=3),
            result=ReplayResult(actual="ok"),
        )
        payload = json.loads(metrics.read_text(encoding="utf-8"))

    assert payload["summary"]["replay_probe_sample_count"] == 1
    assert payload["summary"]["replay_probe_skipped_count"] == 2


def test_runtime_action_manager_dispatches_lifecycle_hooks() -> None:
    import asyncio

    events: list[tuple[str, Any]] = []

    class DynamicRuntime:
        def before_case(self, *, case: FuzzCase) -> None:
            events.append(("before_case", case.target))

        async def after_execute(self, *, index: int, result: ReplayResult) -> None:
            events.append(("after_execute", index))
            events.append(("after_execute_result", result.actual))

        def sample_after_execute(self, *, index: int, case: FuzzCase) -> None:
            events.append(("sample_after_execute", (index, case.target)))

        def finalize(self) -> None:
            events.append(("finalize", None))

    manager = HarnessRuntimeActionManager((DynamicRuntime(),))
    case = FuzzCase("demo", {"mode": "read"}, line_no=1)

    asyncio.run(manager.before_case(index=0, case=case, driver=object()))
    asyncio.run(
        manager.sample_after_execute(
            index=0,
            case=case,
            result=ReplayResult(actual="ok"),
            driver=object(),
        )
    )
    asyncio.run(manager.finalize(driver=object()))

    assert manager.hook_capabilities()["DynamicRuntime"] == [
        "before_case",
        "after_execute",
        "sample_after_execute",
        "finalize",
    ]
    assert events == [
        ("before_case", "demo"),
        ("after_execute", 0),
        ("after_execute_result", "ok"),
        ("sample_after_execute", (0, "demo")),
        ("finalize", None),
    ]


def test_mmio_readback_runtime_consumes_registers_and_addresses() -> None:
    import asyncio

    class FakeMmioDriver:
        def __init__(self) -> None:
            self.reads: list[int] = []

        def resolve_mmio_address(self, name: str) -> int | None:
            return {"ADDR_NAME0": 0x00, "ADDR_STATUS": 0x09}.get(name)

        async def read_mmio_word(self, address: int) -> int:
            self.reads.append(address)
            return address + 0x100

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mmio_config = root / "mmio_readback.json"
        metrics = root / "runtime_metrics.json"
        mmio_config.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "mmio-1",
                            "action_type": "mmio_readback",
                            "payload": {
                                "registers": ["ADDR_NAME0", "ADDR_STATUS"],
                                "addresses": ["0x30"],
                                "case_filter": {"case.mode": "read"},
                            },
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        driver = FakeMmioDriver()
        runtime = MmioReadbackRuntime(
            load_runtime_action_config(mmio_config, action_type="mmio_readback"),
            metrics_out=metrics,
        )
        asyncio.run(
            runtime.sample(
                index=0,
                case=FuzzCase("demo", {"mode": "read"}, line_no=1),
                driver=driver,
            )
        )
        asyncio.run(
            runtime.sample(
                index=1,
                case=FuzzCase("demo", {"mode": "write"}, line_no=2),
                driver=driver,
            )
        )
        payload = json.loads(metrics.read_text(encoding="utf-8"))

    assert driver.reads == [0x00, 0x09, 0x30]
    assert payload["summary"]["mmio_readback_sample_count"] == 1
    assert payload["summary"]["mmio_readback_read_count"] == 3
    assert payload["summary"]["mmio_readback_skipped_count"] == 1
    assert payload["summary"]["mmio_readback_unique_address_count"] == 3
    assert payload["sections"]["mmio_readback"]["actions"][0]["read_count"] == 3
    assert payload["sections"]["mmio_readback"]["reads"][1]["label"] == "ADDR_STATUS"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mmio_config = root / "many_mmio_readback.json"
        metrics = root / "many_runtime_metrics.json"
        mmio_config.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "mmio-many",
                            "action_type": "mmio_readback",
                            "payload": {
                                "addresses": [
                                    hex(address) for address in range(40)
                                ],
                            },
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runtime = MmioReadbackRuntime(
            load_runtime_action_config(mmio_config, action_type="mmio_readback"),
            metrics_out=metrics,
        )
        asyncio.run(
            runtime.sample(
                index=0,
                case=FuzzCase("demo", {"mode": "read"}, line_no=1),
                driver=FakeMmioDriver(),
            )
        )
        payload = json.loads(metrics.read_text(encoding="utf-8"))

    assert payload["summary"]["mmio_readback_read_count"] == 40
    assert payload["summary"]["mmio_readback_read_preview_count"] == 32
    assert payload["summary"]["mmio_readback_unique_address_count"] == 40


def test_gap_actionability_report_classifies_aes_mmio_gaps() -> None:
    target_config = load_target_config("secworks_aes")
    registry = load_harness_plugin_registry(
        target_config.harness_optimization_plugins,
        base_registry=default_harness_plugin_registry(),
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        baseline_summary = root / "baseline_summary.json"
        candidate_summary = root / "candidate_summary.json"
        baseline_summary.write_text(
            json.dumps({"target": "secworks_aes", "uncovered_line_count": 18})
            + "\n",
            encoding="utf-8",
        )
        candidate_summary.write_text(
            json.dumps(
                {
                    "target": "secworks_aes",
                    "uncovered_line_count": 12,
                    "rtl_gap_summary": {
                        "top_gaps": [
                            {
                                "id": "gap-name0",
                                "file": "/repo/example/aes/src/rtl/aes.v",
                                "line": 253,
                                "code": "ADDR_NAME0: tmp_read_data = CORE_NAME0;",
                            },
                            {
                                "id": "gap-block-write",
                                "file": "/repo/example/aes/src/rtl/aes.v",
                                "line": 246,
                                "code": "if ((address >= ADDR_BLOCK0) && (address <= ADDR_BLOCK3))",
                            },
                            {
                                "id": "gap-result-upper-bound",
                                "file": "/repo/example/aes/src/rtl/aes.v",
                                "line": 264,
                                "code": "if ((address >= ADDR_RESULT0) && (address <= ADDR_RESULT3))",
                            },
                            {
                                "id": "gap-core-default",
                                "file": "/repo/example/aes/src/rtl/aes_core.v",
                                "line": 331,
                                "code": "default:",
                            },
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        baseline_manifest = {
            "modes": [
                {
                    "rounds": [
                        {"artifacts": {"coverage_summary": str(baseline_summary)}}
                    ]
                }
            ]
        }
        candidate_manifest = {
            "modes": [
                {
                    "rounds": [
                        {"artifacts": {"coverage_summary": str(candidate_summary)}}
                    ]
                }
            ]
        }
        report = build_candidate_gap_actionability_report(
            task={"target": "secworks_aes"},
            proposal={"proposal_id": "proposal-aes"},
            candidate_manifest={"candidate_id": "candidate-aes"},
            baseline_campaign_manifest=baseline_manifest,
            candidate_campaign_manifest=candidate_manifest,
            baseline_metrics={"uncovered_line_count": 18},
            candidate_metrics={"uncovered_line_count": 12},
            action_effect_report={
                "summary": {
                    "improved_action_count": 1,
                    "neutral_action_count": 2,
                    "regressed_action_count": 0,
                }
            },
            plugin_registry=registry,
        )

    assert report["summary"]["uncovered_line_delta"] == -6
    assert report["summary"]["recommended_action_type_counts"] == {
        "mmio_readback": 2,
        "mmio_write": 1,
    }
    assert report["summary"]["blocked_by_action_surface"] is True
    assert report["summary"]["has_mmio_readback_targets"] is True
    gaps_by_id = {gap["id"]: gap for gap in report["remaining_gaps"]}
    assert gaps_by_id["gap-name0"]["actionability"] == (
        "reachable_with_mmio_readback"
    )
    assert gaps_by_id["gap-name0"]["suggested_payload"]["registers"] == [
        "ADDR_NAME0",
        "ADDR_NAME1",
        "ADDR_VERSION",
    ]
    assert gaps_by_id["gap-result-upper-bound"]["actionability"] == (
        "reachable_with_mmio_readback"
    )
    assert gaps_by_id["gap-result-upper-bound"]["suggested_payload"] == {
        "addresses": ["0x34"]
    }
    assert gaps_by_id["gap-block-write"]["actionability"] == (
        "requires_mmio_write_surface"
    )
    assert gaps_by_id["gap-block-write"]["suggested_payload"] == {
        "addresses": ["0x24"]
    }
    assert gaps_by_id["gap-core-default"]["actionability"] == (
        "requires_internal_state_surface"
    )
    assert (
        "fuzz_examples.secworks_aes_harness_plugin:build_plugin"
        in report["plugin_provenance"]["plugin_specs"]
    )
    assert report["plugin_validation"]["valid"] is True


def test_core_gap_actionability_is_dut_agnostic_without_target_plugin() -> None:
    classified = classify_gap_actionability(
        {
            "id": "gap-name0",
            "file": "/repo/example/aes/src/rtl/aes.v",
            "line": 253,
            "code": "ADDR_NAME0: tmp_read_data = CORE_NAME0;",
        },
        target="secworks_aes",
    )

    assert classified["actionability"] == "unknown"
    assert classified["recommended_action_type"] is None


def test_gap_actionability_report_uses_registered_classifier() -> None:
    def demo_classifier(
        gap: dict[str, Any],
        context: HarnessGapActionabilityContext,
    ) -> dict[str, Any] | None:
        if context.target != "demo_core":
            return None
        if gap.get("id") != "gap-demo":
            return None
        return {
            **gap,
            "actionability": "reachable_with_custom_probe",
            "actionability_reason": "Demo plugin can sample the missing state.",
            "recommended_action_type": "custom_probe",
            "suggested_payload": {"flag": True},
        }

    registry = default_harness_plugin_registry().with_action_plugin(
        HarnessActionPlugin(
            action_type="custom_probe",
            adapter_kind="demo.custom_probe_config",
            artifact_role="candidate_custom_probe_config",
            make_var="HARNESS_CUSTOM_PROBE_CONFIG",
            runtime_action=True,
        )
    ).with_gap_actionability_classifier(demo_classifier)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        baseline_summary = root / "baseline_summary.json"
        candidate_summary = root / "candidate_summary.json"
        baseline_summary.write_text(
            json.dumps({"target": "demo_core", "uncovered_line_count": 4}) + "\n",
            encoding="utf-8",
        )
        candidate_summary.write_text(
            json.dumps(
                {
                    "target": "demo_core",
                    "uncovered_line_count": 3,
                    "rtl_gap_summary": {
                        "top_gaps": [
                            {
                                "id": "gap-demo",
                                "file": "/repo/demo_core.sv",
                                "line": 12,
                                "code": "if (rare_state)",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        report = build_candidate_gap_actionability_report(
            task={"target": "demo_core"},
            proposal={"proposal_id": "proposal-demo"},
            candidate_manifest={"candidate_id": "candidate-demo"},
            baseline_campaign_manifest={
                "modes": [
                    {
                        "rounds": [
                            {"artifacts": {"coverage_summary": str(baseline_summary)}}
                        ]
                    }
                ]
            },
            candidate_campaign_manifest={
                "modes": [
                    {
                        "rounds": [
                            {"artifacts": {"coverage_summary": str(candidate_summary)}}
                        ]
                    }
                ]
            },
            baseline_metrics={"uncovered_line_count": 4},
            candidate_metrics={"uncovered_line_count": 3},
            action_effect_report={"summary": {}},
            plugin_registry=registry,
        )

    gap = report["remaining_gaps"][0]
    assert gap["actionability"] == "reachable_with_custom_probe"
    assert gap["recommended_action_type"] == "custom_probe"
    assert report["summary"]["recommended_action_type_counts"] == {
        "custom_probe": 1
    }
    assert report["plugin_registry"]["gap_actionability_classifier_count"] >= 1


def test_fake_dut_manifest_plugin_generates_gap_minimal_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        plugin_module = root / "fake_dut_harness_plugin.py"
        plugin_module.write_text(
            "\n".join(
                [
                    "from fuzz_pipeline.harness_plugins import HarnessActionPlugin",
                    "",
                    "def fake_payload_errors(payload, path):",
                    "    if isinstance(payload.get('enabled'), bool):",
                    "        return []",
                    "    return [{'path': f'{path}.enabled', 'message': 'expected bool'}]",
                    "",
                    "def fake_classifier(gap, context):",
                    "    if context.target != 'fake_dut' or gap.get('id') != 'gap-fake':",
                    "        return None",
                    "    return {",
                    "        **gap,",
                    "        'actionability': 'reachable_with_fake_probe',",
                    "        'actionability_reason': 'Fake DUT plugin can expose this gap.',",
                    "        'recommended_action_type': 'fake_probe',",
                    "        'suggested_payload': {'enabled': True},",
                    "    }",
                    "",
                    "class FakeDutHarnessPlugin:",
                    "    action_plugins = (HarnessActionPlugin(",
                    "        action_type='fake_probe',",
                    "        payload_required=True,",
                    "        dsl_schema={'payload_fields': {'enabled': 'bool'}},",
                    "        payload_validator=fake_payload_errors,",
                    "        adapter_kind='fake.probe_config',",
                    "        artifact_role='candidate_fake_probe_config',",
                    "        make_var='HARNESS_FAKE_PROBE_CONFIG',",
                    "        runtime_action=True,",
                    "    ),)",
                    "    gap_actionability_classifiers = (fake_classifier,)",
                    "",
                    "def build_plugin(**kwargs):",
                    "    return FakeDutHarnessPlugin()",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        target_config = root / "fake_dut.toml"
        target_config.write_text(
            "\n".join(
                [
                    'name = "fake_dut"',
                    'driver = "fake_driver:Driver"',
                    "",
                    "[harness_optimization]",
                    'plugins = ["fake_dut_harness_plugin:build_plugin"]',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        baseline_summary = root / "baseline_summary.json"
        candidate_summary = root / "candidate_summary.json"
        baseline_summary.write_text(
            json.dumps({"target": "fake_dut", "uncovered_line_count": 2}) + "\n",
            encoding="utf-8",
        )
        candidate_summary.write_text(
            json.dumps(
                {
                    "target": "fake_dut",
                    "uncovered_line_count": 1,
                    "rtl_gap_summary": {
                        "top_gaps": [
                            {
                                "id": "gap-fake",
                                "file": "/repo/fake_dut.sv",
                                "line": 9,
                                "code": "if (rare_state)",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        sys.path.insert(0, str(root))
        try:
            config = load_target_config("fake_dut", target_config=target_config)
            registry = load_harness_plugin_registry(
                config.harness_optimization_plugins,
                base_registry=default_harness_plugin_registry(),
            )
            report = build_candidate_gap_actionability_report(
                task={
                    "target": "fake_dut",
                    "evidence_index": {"span_ids": ["span-fake"]},
                },
                proposal={
                    "proposal_id": "proposal-fake",
                    "evidence_refs": [{"span_id": "span-fake"}],
                },
                candidate_manifest={"candidate_id": "candidate-fake"},
                baseline_campaign_manifest={
                    "modes": [
                        {
                            "rounds": [
                                {
                                    "artifacts": {
                                        "coverage_summary": str(baseline_summary)
                                    }
                                }
                            ]
                        }
                    ]
                },
                candidate_campaign_manifest={
                    "modes": [
                        {
                            "rounds": [
                                {
                                    "artifacts": {
                                        "coverage_summary": str(candidate_summary)
                                    }
                                }
                            ]
                        }
                    ]
                },
                baseline_metrics={"uncovered_line_count": 2},
                candidate_metrics={"uncovered_line_count": 1},
                action_effect_report={"summary": {}},
                plugin_registry=registry,
            )
            minimal = build_gap_actionability_minimal_candidate_proposal(
                task={
                    "target": "fake_dut",
                    "constraints": {
                        "allowed_action_types": list(registry.allowed_action_types())
                    },
                    "evidence_index": {"span_ids": ["span-fake"]},
                },
                proposal={
                    "proposal_id": "proposal-fake",
                    "evidence_refs": [{"span_id": "span-fake"}],
                },
                candidate_manifest={"candidate_id": "candidate-fake"},
                gap_actionability_report=report,
                plugin_registry=registry,
            )
            context = CandidateActionAdapterContext(
                candidate_id="candidate-fake",
                regression_dir=root / "candidate",
            )
            adapter_results = adapt_candidate_actions(
                tuple(
                    {
                        "action_id": action["action_id"],
                        "action_type": action["action_type"],
                        "artifact_path": None,
                        "evidence_refs": action["evidence_refs"],
                        "action": {"payload": action["payload"]},
                    }
                    for action in minimal["actions"]
                ),
                context,
                adapters=None,
                plugin_registry=registry,
            )
        finally:
            sys.path.remove(str(root))

    assert report["remaining_gaps"][0]["recommended_action_type"] == "fake_probe"
    assert minimal["status"] == "proposed"
    assert minimal["validation"]["valid"] is True
    assert minimal["summary"]["recommended_action_count"] == 1
    assert minimal["actions"][0]["action_type"] == "fake_probe"
    assert minimal["actions"][0]["payload"] == {"enabled": True}
    assert minimal["plugin_provenance"]["plugin_sources"][0]["source_file"] == str(
        plugin_module.resolve()
    )
    assert adapter_results[0].artifact_role == "candidate_fake_probe_config"
    assert adapter_results[0].make_var_assignment().startswith(
        "HARNESS_FAKE_PROBE_CONFIG="
    )


def test_coverage_feedback_tuning_runtime_consumes_filters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tuning = root / "coverage_tuning.json"
        metrics = root / "runtime_metrics.json"
        tuning.write_text(
            json.dumps(
                {
                    "entries": [
                        {
                            "action_id": "tuning-1",
                            "action_type": "coverage_feedback_tuning",
                            "payload": {
                                "gap_type": "branch",
                                "directive_source": "selected",
                                "max_gap_count": 1,
                                "directive_weight_multiplier": 2,
                            },
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runtime = CoverageFeedbackTuningRuntime.from_path(
            tuning,
            metrics_out=metrics,
        )
        summary = runtime.apply_summary(
            {
                "rtl_gap_summary": {
                    "top_gaps": [
                        {"gap_id": "gap-1", "gap_type": "branch"},
                        {"gap_id": "gap-2", "gap_type": "line"},
                    ]
                }
            }
        )
        directives = runtime.apply_directives(
            {
                "directives": [
                    {"source": "selected", "gap_type": "branch", "weight": 1},
                    {"source": "other", "gap_type": "branch", "weight": 1},
                    {"source": "selected", "gap_type": "line", "weight": 1},
                ]
            }
        )
        payload = json.loads(metrics.read_text(encoding="utf-8"))

    assert summary["rtl_gap_summary"]["top_gaps"] == [
        {"gap_id": "gap-1", "gap_type": "branch"}
    ]
    assert [item["weight"] for item in directives["directives"]] == [2, 1, 1]
    assert payload["summary"]["coverage_feedback_tuning_filtered_gap_count"] == 1
    assert payload["summary"]["coverage_feedback_tuning_weighted_directive_count"] == 1


def test_replay_and_scoreboard_runtime_actions_are_consumed() -> None:
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        replay_config = root / "replay_probe.json"
        scoreboard_config = root / "scoreboard_check.json"
        metrics = root / "runtime_metrics.json"
        hook_events = root / "hook_events.jsonl"
        hook_module = root / "demo_runtime_hooks.py"
        hook_module.write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "",
                    "class HookRuntime:",
                    "    def __init__(self):",
                    "        self.path = os.environ['HOOK_EVENTS_OUT']",
                    "",
                    "    def _event(self, name):",
                    "        with open(self.path, 'a', encoding='utf-8') as handle:",
                    "            handle.write(json.dumps({'event': name}) + '\\n')",
                    "",
                    "    def before_reset(self, **kwargs):",
                    "        self._event('before_reset')",
                    "",
                    "    def after_reset(self, **kwargs):",
                    "        self._event('after_reset')",
                    "",
                    "    def before_case(self, **kwargs):",
                    "        self._event('before_case')",
                    "",
                    "    def after_ref_model(self, **kwargs):",
                    "        self._event('after_ref_model')",
                    "",
                    "    def after_execute(self, **kwargs):",
                    "        self._event('after_execute')",
                    "",
                    "    def after_scoreboard_record(self, **kwargs):",
                    "        self._event('after_scoreboard_record')",
                    "",
                    "    def finalize(self, **kwargs):",
                    "        self._event('finalize')",
                    "",
                    "def build_plugin(**kwargs):",
                    "    return HookRuntime()",
                    "",
                ]
            ),
            encoding="utf-8",
        )
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
                                "max_samples": 1,
                                "case_filter": {"case.mode": "read"},
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
                            "payload": {
                                "mode": "field_equals",
                                "field": "result.actual",
                                "expected": "ok",
                            },
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
                RUNTIME_ACTION_PLUGINS_ENV: "demo_runtime_hooks:build_plugin",
                "HOOK_EVENTS_OUT": str(hook_events),
            }
        )
        sys.path.insert(0, str(root))
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
            asyncio.run(replay.reset())
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
            events = [
                json.loads(line)["event"]
                for line in hook_events.read_text(encoding="utf-8").splitlines()
            ]
        finally:
            sys.path.remove(str(root))
            _restore_env(old_env)

    assert runtime_metrics["summary"]["replay_probe_sample_count"] == 1
    assert runtime_metrics["summary"]["replay_probe_field_sample_count"] == 2
    assert runtime_metrics["summary"]["scoreboard_check_checked_count"] == 1
    assert summary["harness_scoreboard_checks"]["passed_count"] == 1
    assert "before_reset" in events
    assert "after_reset" in events
    assert "before_case" in events
    assert "after_ref_model" in events
    assert "after_execute" in events
    assert "after_scoreboard_record" in events
    assert "finalize" in events


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
                                "max_weight": 1.5,
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
    assert result.heuristic_directives["directives"][0]["weight"] == 1.5
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


def test_harness_metric_delta_treats_directive_count_as_informational() -> None:
    task = {"target": "demo", "run_id": "run-1"}
    proposal = {
        "schema_version": 1,
        "kind": PROPOSAL_KIND,
        "proposal_id": "proposal-directive-trim",
        "status": "proposed",
    }
    schema_decision = {"decision": "accepted"}
    patch = {
        "status": "applied",
        "candidate_id": "run-1_proposal-directive-trim",
        "sandbox_dir": "/tmp/candidate",
        "summary": {"applied_action_count": 1},
    }
    candidate_evaluation = {
        "status": "passed",
        "baseline_metrics": {
            "failed_record_count": 0,
            "uncovered_line_count": 34,
            "directive_count": 4,
        },
        "candidate_metrics": {
            "failed_record_count": 0,
            "uncovered_line_count": 34,
            "directive_count": 1,
        },
        "acceptance_thresholds": {
            "max_regressed_metric_count": 0,
            "min_improved_metric_count": 0,
            "accepted_candidate_statuses": ["passed"],
        },
    }

    metric_delta = build_harness_optimization_metric_delta(
        task=task,
        candidate_evaluation=candidate_evaluation,
    )
    final_decision = build_harness_optimization_final_decision(
        task=task,
        proposal=proposal,
        schema_decision=schema_decision,
        patch=patch,
        candidate_evaluation=candidate_evaluation,
        metric_delta=metric_delta,
    )
    directive_comparison = next(
        item
        for item in metric_delta["comparisons"]
        if item["metric"] == "directive_count"
    )

    assert directive_comparison["direction"] == "changed"
    assert directive_comparison["role"] == "informational"
    assert directive_comparison["gates_acceptance"] is False
    assert metric_delta["summary"]["regressed_metric_count"] == 0
    assert metric_delta["summary"]["gateable_regressed_metric_count"] == 0
    assert metric_delta["summary"]["informational_changed_metric_count"] == 1
    assert final_decision["decision"] == "accepted_for_review"
    assert final_decision["summary"]["gateable_regressed_metric_count"] == 0


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
                    "payload": {"mode": "record_seen"},
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
                {
                    "action_id": "mmio-1",
                    "action_type": "mmio_readback",
                    "payload": {"registers": ["ADDR_NAME0", "ADDR_STATUS"]},
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
        mmio = json.loads(
            Path(artifacts["candidate_mmio_readback_config"]).read_text(
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
        variant_evaluations = json.loads(
            Path(artifacts["candidate_variant_evaluations"]).read_text(
                encoding="utf-8"
            )
        )
        action_effect = json.loads(
            Path(artifacts["candidate_action_effect_report"]).read_text(
                encoding="utf-8"
            )
        )
        actionability = json.loads(
            Path(artifacts["candidate_gap_actionability_report"]).read_text(
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
        item.startswith("HARNESS_MMIO_READBACK_CONFIG=")
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
    assert candidate_evaluation["candidate_metrics"]["mmio_readback_read_count"] == 6
    assert candidate_evaluation["candidate_metrics"]["candidate_action_count"] == 5
    assert candidate_evaluation["candidate_metrics"]["candidate_overlay_count"] == 5
    assert candidate_evaluation["candidate_metrics"]["candidate_variant_count"] == 6
    assert candidate_evaluation["candidate_metrics"]["candidate_directive_count"] == 1
    assert candidate_evaluation["candidate_metrics"][
        "mutation_directive_update_count"
    ] == 1
    assert candidate_evaluation["candidate_metrics"]["replay_probe_count"] == 1
    assert candidate_evaluation["candidate_metrics"]["scoreboard_check_count"] == 1
    assert candidate_evaluation["candidate_metrics"][
        "coverage_feedback_tuning_count"
    ] == 1
    assert candidate_evaluation["candidate_metrics"]["mmio_readback_count"] == 1
    assert overlay["selected_variant_id"] == "combined"
    assert len(overlay["actions"]) == 5
    assert len(overlay["adapter_results"]) == 5
    assert run_config["safety"]["mainline_modified"] is False
    assert run_config["initial_directives"] == str(directives_path)
    assert run_config["runtime_metrics"] == artifacts["candidate_runtime_metrics"]
    assert run_config["validation_settings"]["attribution_mode"] == "top_k"
    assert run_config["validation_settings"]["thresholds"][
        "min_improved_metric_count"
    ] == 1
    assert run_config["adapter_metrics"]["candidate_action_count"] == 5
    assert "candidate_replay_probe_config" in run_config["adapter_artifacts"]
    assert "candidate_scoreboard_check_config" in run_config["adapter_artifacts"]
    assert (
        "candidate_coverage_feedback_tuning_config"
        in run_config["adapter_artifacts"]
    )
    assert "candidate_mmio_readback_config" in run_config["adapter_artifacts"]
    assert len(run_config["extra_make_vars"]) >= 5
    assert directives["directives"][0]["origin"] == "harness_optimizer"
    assert replay_probe["entries"][0]["payload"]["signals"] == ["dut.state"]
    assert scoreboard["entries"][0]["payload"]["check"].startswith("case result")
    assert tuning["entries"][0]["payload"]["max_gap_count"] == 8
    assert mmio["entries"][0]["payload"]["registers"] == [
        "ADDR_NAME0",
        "ADDR_STATUS",
    ]
    assert runtime_metrics["summary"]["replay_probe_sample_count"] == 3
    assert runtime_metrics["summary"]["mmio_readback_read_count"] == 6
    assert ranking["top_variant"]["variant_id"] == "combined"
    assert len(ranking["variants"]) == 6
    assert promotion["promotion_status"] == "ready_for_review"
    assert promotion["gap_actionability_summary"]["remaining_gap_count"] == 1
    assert promotion["safety"]["mainline_modified"] is False
    assert actionability["summary"]["remaining_gap_count"] == 1
    assert len(variant_evaluations["evaluations"]) == 1
    assert variant_evaluations["evaluations"][0]["variant_id"] == "combined"
    assert action_effect["summary"]["variant_count"] == 1
    assert action_effect["summary"]["consumed_action_count"] == 4
    assert any(
        action["action_id"] == "probe-1"
        and action["consumed"] is True
        and action["runtime_metrics"]["sample_count"] == 3
        for action in action_effect["variants"][0]["actions"]
    )
    assert any(
        action["action_id"] == "mmio-1"
        and action["consumed"] is True
        and action["runtime_metrics"]["read_count"] == 6
        for action in action_effect["variants"][0]["actions"]
    )
    assert candidate_evaluation["summary"]["candidate_variant_count"] == 6
    assert candidate_evaluation["summary"]["evaluated_variant_count"] == 1
    assert candidate_evaluation["summary"]["selected_variant_id"] == "combined"
    assert candidate_evaluation["summary"]["promotion_status"] == "ready_for_review"
    assert metric_delta["summary"]["improved_metric_count"] == 1
    assert final_decision["decision"] == "accepted_for_review"
    assert final_decision["summary"]["acceptance_thresholds"][
        "min_improved_metric_count"
    ] == 1


def test_harness_candidate_regression_uses_matched_noop_baseline() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seen_configs: list[CampaignConfig] = []

        def factory(config, observation_context, **kwargs):
            seen_configs.append(config)
            return StubRegressionCampaign(config, observation_context, **kwargs)

        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        sequence_backend = SequenceRegressionCampaignEvaluationBackend((2, 0))
        backend = HarnessCandidateRegressionBackend(
            settings=CandidateRegressionSettings(
                modes=("heuristic_feedback",),
                rounds=1,
                matched_baseline=True,
                thresholds=CandidateAcceptanceThresholds(
                    min_improved_metric_count=1,
                ),
            ),
            evaluation_backends=EvaluationBackends(
                campaign_evaluation=sequence_backend,
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
            "proposal_id": "proposal-matched-baseline",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "probe-1",
                    "action_type": "replay_probe",
                    "payload": {"signals": ["dut.state"], "sample_on": "posedge"},
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
        matched_config = json.loads(
            Path(artifacts["matched_baseline_regression_config"]).read_text(
                encoding="utf-8"
            )
        )
        matched_overlay = json.loads(
            Path(artifacts["matched_baseline_action_overlay"]).read_text(
                encoding="utf-8"
            )
        )
        failed_comparison = next(
            item
            for item in metric_delta["comparisons"]
            if item["metric"] == "failed_record_count"
        )

    assert len(seen_configs) == 2
    assert len(sequence_backend.calls) == 2
    assert "matched_noop_baseline" in str(seen_configs[0].out_dir)
    assert seen_configs[0].initial_directives is None
    assert not any(
        item.startswith("HARNESS_REPLAY_PROBE_CONFIG=")
        for item in seen_configs[0].extra_make_vars
    )
    assert any(
        item.startswith("HARNESS_REPLAY_PROBE_CONFIG=")
        for item in seen_configs[1].extra_make_vars
    )
    assert candidate_evaluation["baseline_source"] == "matched_noop_rerun"
    assert candidate_evaluation["source_baseline_metrics"][
        "failed_record_count"
    ] == 1
    assert candidate_evaluation["baseline_metrics"]["failed_record_count"] == 2
    assert candidate_evaluation["matched_baseline_metrics"][
        "failed_record_count"
    ] == 2
    assert candidate_evaluation["candidate_metrics"]["failed_record_count"] == 0
    assert candidate_evaluation["summary"]["matched_baseline"]["status"] == "passed"
    assert matched_config["run_role"] == "matched_noop_baseline"
    assert matched_config["initial_directives"] is None
    assert matched_config["adapter_metrics"]["candidate_action_count"] == 0
    assert matched_overlay["baseline_role"] == "matched_noop_baseline"
    assert matched_overlay["actions"] == []
    assert failed_comparison["baseline"] == 2
    assert failed_comparison["candidate"] == 0
    assert failed_comparison["direction"] == "improved"
    assert metric_delta["summary"]["gateable_improved_metric_count"] == 1
    assert final_decision["decision"] == "accepted_for_review"


def test_harness_candidate_regression_runs_paired_repeated_validation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        seen_configs: list[CampaignConfig] = []

        def factory(config, observation_context, **kwargs):
            seen_configs.append(config)
            return StubRegressionCampaign(config, observation_context, **kwargs)

        campaign_evaluation, campaign_manifest, manifest_path = _source_payloads(root)
        evaluation_path = root / "campaign_evaluation.json"
        paths = harness_optimization_paths(evaluation_path)
        sequence_backend = SequenceRegressionCampaignEvaluationBackend((2, 0, 2, 2))
        backend = HarnessCandidateRegressionBackend(
            settings=CandidateRegressionSettings(
                modes=("heuristic_feedback",),
                rounds=1,
                matched_baseline=True,
                paired_repeats=2,
                repeat_seed_stride=1,
                thresholds=CandidateAcceptanceThresholds(
                    min_improved_metric_count=1,
                    max_flaky_metric_count=0,
                ),
            ),
            evaluation_backends=EvaluationBackends(
                campaign_evaluation=sequence_backend,
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
            "proposal_id": "proposal-paired-validation",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "probe-1",
                    "action_type": "replay_probe",
                    "payload": {"signals": ["dut.state"], "sample_on": "posedge"},
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
        paired_validation = json.loads(
            Path(artifacts["candidate_paired_validation"]).read_text(
                encoding="utf-8"
            )
        )
        action_effect = json.loads(
            Path(artifacts["candidate_action_effect_report"]).read_text(
                encoding="utf-8"
            )
        )
        promotion = json.loads(
            Path(artifacts["candidate_promotion_package"]).read_text(
                encoding="utf-8"
            )
        )
        failed_stability = next(
            item
            for item in paired_validation["metric_stability"]
            if item["metric"] == "failed_record_count"
        )

    assert len(seen_configs) == 4
    assert len(sequence_backend.calls) == 4
    assert "matched_noop_baseline" in str(seen_configs[0].out_dir)
    assert "paired_validation/repeat_001/matched_noop_baseline" in str(
        seen_configs[2].out_dir
    )
    assert "paired_validation/repeat_001/candidate" in str(seen_configs[3].out_dir)
    assert seen_configs[0].seed == 7
    assert seen_configs[1].seed == 7
    assert seen_configs[2].seed == 8
    assert seen_configs[3].seed == 8
    assert candidate_evaluation["baseline_metrics"]["failed_record_count"] == 2
    assert candidate_evaluation["candidate_metrics"]["failed_record_count"] == 1
    assert candidate_evaluation["stability_summary"]["complete_pair_count"] == 2
    assert candidate_evaluation["stability_summary"]["flaky_metric_count"] == 1
    assert paired_validation["summary"]["aggregate"] == "mean"
    assert paired_validation["summary"]["flaky_metric_count"] == 1
    assert len(paired_validation["runs"]) == 2
    assert paired_validation["runs"][1]["seed"] == 8
    assert failed_stability["flaky"] is True
    assert failed_stability["paired_directions"] == ["improved", "unchanged"]
    assert action_effect["summary"]["improved_action_count"] == 1
    assert action_effect["actions"][0]["effect_status"] == "improved"
    assert promotion["promotion_status"] == "hold"
    assert promotion["threshold_summary"]["flaky_metric_count"] == 1
    assert metric_delta["summary"]["gateable_improved_metric_count"] == 1
    assert final_decision["decision"] == "rejected"
    assert final_decision["reason"] == "candidate_stability_below_threshold"


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


def test_harness_candidate_regression_runs_top_k_variants() -> None:
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
                max_variant_regressions=3,
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
            "proposal_id": "proposal-top-k",
            "status": "proposed",
            "actions": [
                {
                    "action_id": "probe-1",
                    "action_type": "replay_probe",
                    "payload": {"probe": "result.actual"},
                    "evidence_refs": [{"span_id": "span-dut"}],
                },
                {
                    "action_id": "scoreboard-1",
                    "action_type": "scoreboard_check",
                    "payload": {"mode": "record_seen"},
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
        variant_evaluations = json.loads(
            Path(artifacts["candidate_variant_evaluations"]).read_text(
                encoding="utf-8"
            )
        )
        ranking = json.loads(
            Path(artifacts["candidate_variant_ranking"]).read_text(
                encoding="utf-8"
            )
        )
        action_effect = json.loads(
            Path(artifacts["candidate_action_effect_report"]).read_text(
                encoding="utf-8"
            )
        )

    assert len(seen_configs) == 3
    assert any("variants/action_probe-1" in str(config.out_dir) for config in seen_configs)
    assert any(
        "variants/action_scoreboard-1" in str(config.out_dir)
        for config in seen_configs
    )
    assert candidate_evaluation["status"] == "passed"
    assert candidate_evaluation["summary"]["evaluated_variant_count"] == 3
    assert candidate_evaluation["summary"]["selected_variant_id"] == "combined"
    assert {item["variant_id"] for item in variant_evaluations["evaluations"]} == {
        "combined",
        "action_probe-1",
        "action_scoreboard-1",
    }
    assert action_effect["summary"]["runtime_action_count"] == 2
    assert action_effect["summary"]["consumed_action_count"] == 2
    assert all(
        item["validation_status"] == "passed"
        for item in ranking["variants"]
        if item["variant_id"] in {"combined", "action_probe-1", "action_scoreboard-1"}
    )
    probe_variant = next(
        item for item in action_effect["variants"] if item["variant_id"] == "action_probe-1"
    )
    assert probe_variant["actions"][0]["consumed"] is True
    assert probe_variant["actions"][0]["runtime_metrics"]["sample_count"] == 3
    assert metric_delta["summary"]["improved_metric_count"] == 1
    assert final_decision["decision"] == "accepted_for_review"


def test_candidate_regression_settings_rejects_invalid_variant_count() -> None:
    try:
        CandidateRegressionSettings(max_variant_regressions=0)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("max_variant_regressions=0 should fail schema")

    assert "max_variant_regressions must be >= 1" in message

    try:
        CandidateRegressionSettings(paired_repeats=0)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("paired_repeats=0 should fail schema")

    assert "paired_repeats must be >= 1" in message

    try:
        CandidateRegressionSettings(attribution_top_k=0)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("attribution_top_k=0 should fail schema")

    assert "attribution_top_k must be >= 1" in message

    try:
        CandidateRegressionSettings(attribution_mode="everything")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("unknown attribution_mode should fail schema")

    assert "attribution_mode must be one of" in message


def test_candidate_attribution_all_actions_keeps_each_variant() -> None:
    variants = [
        {"variant_id": "combined", "action_ids": ["a", "b", "c"]},
        {"variant_id": "action_a", "action_ids": ["a"]},
        {"variant_id": "action_b", "action_ids": ["b"]},
        {"variant_id": "action_c", "action_ids": ["c"]},
    ]

    top_k = select_candidate_variants(
        variants,
        max_count=2,
        attribution_mode="top_k",
    )
    all_actions = select_candidate_variants(
        variants,
        max_count=2,
        attribution_mode="all_actions",
    )

    assert [item["variant_id"] for item in top_k] == ["combined", "action_a"]
    assert [item["variant_id"] for item in all_actions] == [
        "combined",
        "action_a",
        "action_b",
        "action_c",
    ]


def test_action_effect_report_prefers_standalone_action_effects() -> None:
    task = {"target": "demo", "run_id": "run-1"}
    proposal = {
        "proposal_id": "proposal-1",
        "actions": [],
        "evidence_refs": [{"span_id": "span-root"}],
    }
    candidate_manifest = {"candidate_id": "candidate-1"}
    boundary_action = {
        "action_id": "boundary",
        "action_type": "mutation_directive_update",
        "payload": {"directives": [{"name": "boundary"}]},
        "evidence_refs": [{"span_id": "span-boundary"}],
    }
    hint_action = {
        "action_id": "hint",
        "action_type": "mutation_directive_update",
        "payload": {"directives": [{"name": "hint"}]},
        "evidence_refs": [{"span_id": "span-hint"}],
    }
    proposal["actions"] = [boundary_action, hint_action]
    baseline_metrics = {"uncovered_line_count": 34}
    report = build_candidate_action_effect_report(
        task=task,
        proposal=proposal,
        candidate_manifest=candidate_manifest,
        baseline_metrics=baseline_metrics,
        variant_evaluations=[
            {
                "variant_id": "combined",
                "variant_type": "combined_actions",
                "status": "passed",
                "action_ids": ["boundary", "hint"],
                "actions": [boundary_action, hint_action],
                "metrics": {"uncovered_line_count": 14},
            },
            {
                "variant_id": "action_boundary",
                "variant_type": "single_action",
                "status": "passed",
                "action_ids": ["boundary"],
                "actions": [boundary_action],
                "metrics": {"uncovered_line_count": 14},
            },
            {
                "variant_id": "action_hint",
                "variant_type": "single_action",
                "status": "passed",
                "action_ids": ["hint"],
                "actions": [hint_action],
                "metrics": {"uncovered_line_count": 34},
            },
        ],
    )
    promotion = build_candidate_promotion_package(
        task=task,
        proposal=proposal,
        patch={"sandbox_dir": "/tmp/candidate"},
        candidate_manifest=candidate_manifest,
        candidate_metrics={"uncovered_line_count": 14},
        baseline_metrics=baseline_metrics,
        ranking={"top_variant": {"variant_id": "combined"}},
        thresholds=CandidateAcceptanceThresholds(min_improved_metric_count=1),
        action_effect_report=report,
    )
    actions = {item["action_id"]: item for item in report["actions"]}

    assert report["summary"]["standalone_evaluated_action_count"] == 2
    assert report["summary"]["improved_action_count"] == 1
    assert report["summary"]["neutral_action_count"] == 1
    assert report["summary"]["standalone_improved_action_count"] == 1
    assert report["summary"]["standalone_neutral_action_count"] == 1
    assert actions["boundary"]["effect_status"] == "improved"
    assert actions["boundary"]["standalone_effect_status"] == "improved"
    assert actions["boundary"]["combined_effect_status"] == "improved"
    assert actions["hint"]["effect_status"] == "neutral"
    assert actions["hint"]["standalone_effect_status"] == "neutral"
    assert actions["hint"]["combined_effect_status"] == "improved"
    assert actions["hint"]["aggregate_effect_status"] == "improved"
    assert actions["hint"]["supporting_variants"][0]["variant_type"] == "combined_actions"
    assert [item["action_id"] for item in promotion["effective_actions"]] == [
        "boundary"
    ]
    assert [item["action_id"] for item in promotion["neutral_actions"]] == ["hint"]
    assert promotion["harmful_actions"] == []
    assert [
        item["action_id"] for item in promotion["recommended_promotion_actions"]
    ] == ["boundary"]
    assert promotion["minimal_promotion_candidate"]["action_ids"] == ["boundary"]
    assert (
        promotion["minimal_promotion_candidate"]["validated_by_variant_id"]
        == "action_boundary"
    )
    assert promotion["minimal_promotion_candidate"]["status"] == "ready_for_review"
    assert promotion["minimal_promotion_candidate"]["validation_status"] == "validated"
    assert promotion["minimal_promotion_candidate"]["requires_validation"] is False
    assert promotion["action_pruning_summary"]["minimal_action_count"] == 1
    assert promotion["plugin_validation"]["valid"] is True
    assert (
        promotion["plugin_provenance"]["kind"]
        == "libafl_bfm_fuzz.harness_plugin_provenance"
    )
    assert (
        promotion["action_pruning_summary"]["source_promotion_status"]
        == "ready_for_review"
    )


def test_promotion_package_classifies_regressed_actions_as_harmful() -> None:
    task = {"target": "demo"}
    proposal = {
        "proposal_id": "proposal-1",
        "actions": [
            {"action_id": "good", "action_type": "mutation_directive_update"},
            {"action_id": "bad", "action_type": "mutation_directive_update"},
        ],
    }
    candidate_manifest = {"candidate_id": "candidate-1"}
    baseline_metrics = {"uncovered_line_count": 20}
    report = build_candidate_action_effect_report(
        task=task,
        proposal=proposal,
        candidate_manifest=candidate_manifest,
        baseline_metrics=baseline_metrics,
        variant_evaluations=[
            {
                "variant_id": "combined",
                "variant_type": "combined_actions",
                "status": "passed",
                "action_ids": ["good", "bad"],
                "actions": proposal["actions"],
                "metrics": {"uncovered_line_count": 10},
            },
            {
                "variant_id": "action_good",
                "variant_type": "single_action",
                "status": "passed",
                "action_ids": ["good"],
                "actions": [proposal["actions"][0]],
                "metrics": {"uncovered_line_count": 10},
            },
            {
                "variant_id": "action_bad",
                "variant_type": "single_action",
                "status": "passed",
                "action_ids": ["bad"],
                "actions": [proposal["actions"][1]],
                "metrics": {"uncovered_line_count": 30},
            },
        ],
    )
    promotion = build_candidate_promotion_package(
        task=task,
        proposal=proposal,
        patch={},
        candidate_manifest=candidate_manifest,
        candidate_metrics={"uncovered_line_count": 10},
        baseline_metrics=baseline_metrics,
        ranking={"top_variant": {"variant_id": "combined"}},
        thresholds=CandidateAcceptanceThresholds(min_improved_metric_count=1),
        action_effect_report=report,
    )

    assert [item["action_id"] for item in promotion["effective_actions"]] == ["good"]
    assert [item["action_id"] for item in promotion["harmful_actions"]] == ["bad"]
    assert promotion["neutral_actions"] == []
    assert promotion["minimal_promotion_candidate"]["action_ids"] == ["good"]
    assert promotion["minimal_promotion_candidate"]["status"] == "ready_for_review"
    assert (
        promotion["minimal_promotion_candidate"]["validated_by_variant_id"]
        == "action_good"
    )
    assert promotion["action_pruning_summary"]["harmful_action_count"] == 1


def test_promotion_package_returns_empty_minimal_candidate_for_neutral_actions() -> None:
    task = {"target": "demo"}
    proposal = {
        "proposal_id": "proposal-1",
        "actions": [
            {"action_id": "boundary", "action_type": "mutation_directive_update"},
            {"action_id": "probe", "action_type": "replay_probe"},
        ],
    }
    candidate_manifest = {"candidate_id": "candidate-1"}
    baseline_metrics = {"uncovered_line_count": 18}
    report = build_candidate_action_effect_report(
        task=task,
        proposal=proposal,
        candidate_manifest=candidate_manifest,
        baseline_metrics=baseline_metrics,
        variant_evaluations=[
            {
                "variant_id": "combined",
                "variant_type": "combined_actions",
                "status": "passed",
                "action_ids": ["boundary", "probe"],
                "actions": proposal["actions"],
                "metrics": {"uncovered_line_count": 18},
            },
            {
                "variant_id": "action_boundary",
                "variant_type": "single_action",
                "status": "passed",
                "action_ids": ["boundary"],
                "actions": [proposal["actions"][0]],
                "metrics": {"uncovered_line_count": 18},
            },
            {
                "variant_id": "action_probe",
                "variant_type": "single_action",
                "status": "passed",
                "action_ids": ["probe"],
                "actions": [proposal["actions"][1]],
                "metrics": {"uncovered_line_count": 18},
            },
        ],
    )
    promotion = build_candidate_promotion_package(
        task=task,
        proposal=proposal,
        patch={},
        candidate_manifest=candidate_manifest,
        candidate_metrics={"uncovered_line_count": 18},
        baseline_metrics=baseline_metrics,
        ranking={"top_variant": {"variant_id": "combined"}},
        thresholds=CandidateAcceptanceThresholds(min_improved_metric_count=1),
        action_effect_report=report,
    )

    assert promotion["effective_actions"] == []
    assert [item["action_id"] for item in promotion["neutral_actions"]] == [
        "boundary",
        "probe",
    ]
    assert promotion["harmful_actions"] == []
    assert promotion["recommended_promotion_actions"] == []
    assert promotion["minimal_promotion_candidate"]["action_ids"] == []
    assert promotion["minimal_promotion_candidate"]["actions"] == []
    assert promotion["minimal_promotion_candidate"]["dropped_action_ids"] == [
        "boundary",
        "probe",
    ]
    assert promotion["minimal_promotion_candidate"]["status"] == "hold"
    assert (
        promotion["minimal_promotion_candidate"]["validation_status"]
        == "not_recommended"
    )
    assert promotion["action_pruning_summary"]["effective_action_count"] == 0
    assert promotion["action_pruning_summary"]["neutral_action_count"] == 2
    assert promotion["action_pruning_summary"]["minimal_action_count"] == 0
    assert promotion["action_pruning_summary"]["source_promotion_status"] == "hold"


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


def _runtime_metric_actions(
    config_path: str,
    metrics: dict[str, int],
) -> list[dict[str, Any]]:
    payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return [
        {
            "action_id": entry.get("action_id"),
            "action_type": entry.get("action_type"),
            **metrics,
        }
        for entry in payload.get("entries", [])
        if isinstance(entry, dict)
    ]


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
