from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_pipeline import (  # noqa: E402
    CANDIDATE_EVALUATION_KIND,
    CANDIDATE_MANIFEST_KIND,
    CandidateAcceptanceThresholds,
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
        return {
            "mode": mode,
            "rounds": [
                {
                    "round_id": f"{mode}_round_00",
                    "round_manifest": str(self._out_dir() / mode / "round.json"),
                    "coverage": {"uncovered_line_count": 0},
                    "feedback": {"directive_count": 1},
                    "artifacts": {},
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
        run_config = json.loads(
            Path(artifacts["candidate_regression_config"]).read_text(
                encoding="utf-8"
            )
        )
        directives_path = Path(artifacts["candidate_mutation_directives"])
        directives = json.loads(directives_path.read_text(encoding="utf-8"))

    assert seen_configs
    assert seen_configs[0].campaign_plan_profile == "campaign_with_evaluation"
    assert seen_configs[0].initial_directives == directives_path
    assert candidate_evaluation["status"] == "passed"
    assert candidate_evaluation["candidate_metrics"]["failed_record_count"] == 0
    assert run_config["safety"]["mainline_modified"] is False
    assert run_config["initial_directives"] == str(directives_path)
    assert directives["directives"][0]["origin"] == "harness_optimizer"
    assert metric_delta["summary"]["improved_metric_count"] == 1
    assert final_decision["decision"] == "accepted_for_review"
    assert final_decision["summary"]["acceptance_thresholds"][
        "min_improved_metric_count"
    ] == 1


def test_harness_candidate_regression_threshold_rejects_neutral_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def factory(config, observation_context, **kwargs):
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

    assert candidate_evaluation["status"] == "passed"
    assert metric_delta["summary"]["improved_metric_count"] == 0
    assert final_decision["decision"] == "rejected"
    assert final_decision["reason"] == "candidate_metric_improvement_below_threshold"


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
