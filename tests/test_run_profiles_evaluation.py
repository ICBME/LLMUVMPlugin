from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import ObservationContext
from fuzz_pipeline import (
    CampaignConfig,
    CampaignOrchestrator,
    EvaluationBackends,
    FuzzRunConfig,
    PROPOSAL_KIND,
    RunPlanProfile,
    RunStage,
    harness_optimization_paths,
)
from fuzz_pipeline.coverage_feedback import CoverageFeedbackResult
from fuzz_pipeline.run_adapters import RunBackends
from fuzz_pipeline.run_orchestrator import FuzzRunOrchestrator


class StubFeedbackRun(FuzzRunOrchestrator):
    def generate_corpus(self):
        self._required_artifact("corpus").write_text(
            '{"target":"demo"}\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(["generate"], 0)

    def validate_corpus(self):
        return ["case"]

    def coverage_run(self):
        self._required_artifact("replay_artifacts").mkdir(
            parents=True,
            exist_ok=True,
        )
        self._required_artifact("rtl_coverage_dat").write_text(
            "coverage",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(["coverage-run"], 0)

    def generate_coverage_report(self):
        self._required_artifact("coverage_annotated").mkdir(
            parents=True,
            exist_ok=True,
        )
        self._required_artifact("coverage_info").write_text(
            "coverage info",
            encoding="utf-8",
        )
        return {
            "annotate": subprocess.CompletedProcess(["annotate"], 0),
            "write_info": subprocess.CompletedProcess(["write-info"], 0),
        }

    def coverage_feedback(self):
        summary_path = self._path_from_cwd(self.config.summary_out)
        directives_path = self._path_from_cwd(self.config.directives_out)
        prompt_path = self._path_from_cwd(self.config.prompt_out)
        assert summary_path is not None
        assert directives_path is not None
        assert prompt_path is not None
        summary_path.write_text('{"uncovered_line_count":1}\n', encoding="utf-8")
        directives_path.write_text('{"directives":[]}\n', encoding="utf-8")
        prompt_path.write_text('{"messages":[]}\n', encoding="utf-8")
        heuristic_path = self._heuristic_directives()
        assert heuristic_path is not None
        heuristic_path.write_text('{"directives":[]}\n', encoding="utf-8")
        return CoverageFeedbackResult(
            summary={"uncovered_line_count": 1},
            final_directives={
                "source": "heuristic",
                "directives": [{"origin": "rtl_gap"}],
            },
            heuristic_directives={"directives": [{"origin": "rtl_gap"}]},
            prompt={},
        )

    def generate_feedback_corpus(self):
        self._feedback_corpus().write_text('{"target":"demo"}\n', encoding="utf-8")
        return subprocess.CompletedProcess(["feedback-generate"], 0)

    def validate_feedback_corpus(self):
        return ["feedback-case"]

    def feedback_replay(self):
        return subprocess.CompletedProcess(["feedback-replay"], 0)


def test_run_profile_can_insert_round_evaluation_stage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = FuzzRunConfig(
            target="demo",
            corpus=root / "corpus.jsonl",
            libafl_manifest=root / "Cargo.toml",
            coverage_dir=root / "coverage",
            coverage_dat=root / "coverage" / "coverage.dat",
            coverage_info=root / "coverage" / "coverage.info",
            coverage_annotated=root / "coverage" / "annotated",
            summary_out=root / "summary.json",
            directives_out=root / "directives.json",
            prompt_out=root / "prompt.json",
            feedback_corpus=root / "feedback_corpus.jsonl",
            round_manifest_out=root / "round_manifest.json",
            evaluation_out=root / "round_evaluation.json",
        )

        result = StubFeedbackRun(config, ObservationContext()).feedback_fuzz()

        assert "round_evaluation" in result
        assert (root / "round_evaluation.json").exists()
        evaluation = result["round_evaluation"]
        assert isinstance(evaluation, dict)
        assert evaluation["kind"] == "libafl_bfm_fuzz.round_evaluation"
        assert "round_manifest" in evaluation["artifacts"]
        assert evaluation["case_counts"]["corpus_validation"] == 1


def test_round_evaluation_appends_to_explicit_base_profile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = FuzzRunConfig(
            target="demo",
            corpus=root / "corpus.jsonl",
            libafl_manifest=root / "Cargo.toml",
            coverage_dir=root / "coverage",
            coverage_dat=root / "coverage" / "coverage.dat",
            coverage_info=root / "coverage" / "coverage.info",
            coverage_annotated=root / "coverage" / "annotated",
            summary_out=root / "summary.json",
            directives_out=root / "directives.json",
            prompt_out=root / "prompt.json",
            feedback_corpus=root / "feedback_corpus.jsonl",
            round_manifest_out=root / "round_manifest.json",
            run_plan_profile="feedback_fuzz",
            evaluation_out=root / "round_evaluation.json",
        )

        result = StubFeedbackRun(config, ObservationContext()).feedback_fuzz()

        assert "round_evaluation" in result
        assert (root / "round_evaluation.json").exists()


class FakeRoundEvaluationBackend:
    def __init__(self) -> None:
        self.stage_names: list[str] = []

    def run_round(self, stage_results: dict[str, object]) -> dict:
        self.stage_names = list(stage_results)
        return {
            "kind": "fake.round_evaluation",
            "stage_names": self.stage_names,
        }


def test_run_orchestrator_accepts_replacement_round_evaluation_backend() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        backend = FakeRoundEvaluationBackend()
        config = FuzzRunConfig(
            target="demo",
            corpus=root / "corpus.jsonl",
            libafl_manifest=root / "Cargo.toml",
            coverage_dir=root / "coverage",
            coverage_dat=root / "coverage" / "coverage.dat",
            coverage_info=root / "coverage" / "coverage.info",
            coverage_annotated=root / "coverage" / "annotated",
            summary_out=root / "summary.json",
            directives_out=root / "directives.json",
            prompt_out=root / "prompt.json",
            feedback_corpus=root / "feedback_corpus.jsonl",
            round_manifest_out=root / "round_manifest.json",
            evaluation_out=root / "round_evaluation.json",
        )

        result = StubFeedbackRun(
            config,
            ObservationContext(),
            evaluation_backends=EvaluationBackends(round_evaluation=backend),
        ).feedback_fuzz()

        assert result["round_evaluation"]["kind"] == "fake.round_evaluation"
        assert backend.stage_names[-1] == "round_manifest"


def test_run_profile_mode_mismatch_is_rejected() -> None:
    config = FuzzRunConfig(
        target="demo",
        corpus=Path("corpus.jsonl"),
        libafl_manifest=Path("Cargo.toml"),
        run_plan_profile="no_feedback",
    )
    run = StubFeedbackRun(config, ObservationContext())

    try:
        run.feedback_fuzz()
    except ValueError as exc:
        assert "requires 'feedback_fuzz'" in str(exc)
    else:
        raise AssertionError("mode/profile mismatch should fail before stages run")


def test_round_evaluation_requires_round_manifest_stage() -> None:
    config = FuzzRunConfig(
        target="demo",
        corpus=Path("corpus.jsonl"),
        libafl_manifest=Path("Cargo.toml"),
        run_plan_profile="generate_and_validate",
        evaluation_out=Path("round_evaluation.json"),
    )
    run = StubFeedbackRun(config, ObservationContext())

    try:
        run.feedback_fuzz()
    except ValueError as exc:
        assert "without a round_manifest stage" in str(exc)
    else:
        raise AssertionError("round evaluation without round manifest should fail")


class FakeReplayBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_coverage_replay(self):
        self.calls.append("coverage")
        return subprocess.CompletedProcess(["fake-coverage"], 0)

    def run_make_clean(self) -> None:
        self.calls.append("clean")

    def remove_sim_build(self) -> None:
        self.calls.append("remove")

    def coverage_command(self) -> list[str]:
        return ["fake-coverage"]

    def feedback_command(self) -> list[str]:
        return ["fake-feedback"]

    def make_command(self) -> list[str]:
        return ["fake-make"]

    def observation_make_vars(self, *, stage_id: str) -> list[str]:
        return [f"STAGE={stage_id}"]


def test_run_orchestrator_accepts_replacement_replay_backend() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        backend = FakeReplayBackend()
        config = FuzzRunConfig(
            target="demo",
            corpus=root / "corpus.jsonl",
            libafl_manifest=root / "Cargo.toml",
            coverage_dir=root / "coverage",
            coverage_dat=root / "coverage" / "coverage.dat",
            coverage_info=root / "coverage" / "coverage.info",
            coverage_annotated=root / "coverage" / "annotated",
        )
        run = FuzzRunOrchestrator(
            config,
            ObservationContext(),
            backends=RunBackends(uvm_replay=backend),
        )

        result = run.coverage_run()

        assert result.args == ["fake-coverage"]
        assert backend.calls == ["coverage"]


class StubCampaign(CampaignOrchestrator):
    def _run_mode(self, mode: str) -> dict:
        return {
            "mode": mode,
            "rounds": [
                {
                    "round_id": f"{mode}_round_00",
                    "round_manifest": str(self._out_dir() / mode / "round.json"),
                    "coverage": {"uncovered_line_count": 1},
                    "feedback": {"directive_count": 2},
                }
            ],
        }


def test_campaign_profile_can_insert_campaign_evaluation_stage() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign = StubCampaign(
            CampaignConfig(
                target="demo",
                out_dir=root / "campaign",
                libafl_manifest=root / "Cargo.toml",
                modes=("heuristic_feedback",),
                campaign_evaluation_out=root / "campaign" / "evaluation.json",
            ),
            ObservationContext(),
        )

        manifest = campaign.run()

        evaluation_path = root / "campaign" / "evaluation.json"
        assert manifest["artifacts"]["evaluation_report"] == str(evaluation_path)
        assert evaluation_path.exists()


class FakeCampaignEvaluationBackend:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.manifest: dict | None = None

    def run(self, campaign_manifest: dict) -> dict:
        self.manifest = campaign_manifest
        payload = {
            "kind": "fake.campaign_evaluation",
            "summary": {
                "mode_count": len(campaign_manifest.get("modes", [])),
                "round_count": sum(
                    len(mode.get("rounds", []))
                    for mode in campaign_manifest.get("modes", [])
                ),
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("fake evaluation\n", encoding="utf-8")
        return payload


def test_campaign_orchestrator_accepts_replacement_campaign_evaluation_backend() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        evaluation_path = root / "campaign" / "evaluation.json"
        backend = FakeCampaignEvaluationBackend(evaluation_path)
        campaign = StubCampaign(
            CampaignConfig(
                target="demo",
                out_dir=root / "campaign",
                libafl_manifest=root / "Cargo.toml",
                modes=("heuristic_feedback",),
                campaign_evaluation_out=evaluation_path,
            ),
            ObservationContext(),
            evaluation_backends=EvaluationBackends(campaign_evaluation=backend),
        )

        manifest = campaign.run()

        assert backend.manifest is not None
        assert backend.manifest["target"] == "demo"
        assert manifest["artifacts"]["evaluation_report"] == str(evaluation_path)
        assert evaluation_path.read_text(encoding="utf-8") == "fake evaluation\n"


class FakeHarnessTraceCampaignEvaluationBackend:
    def __init__(self, path: Path) -> None:
        self.path = path

    def run(self, campaign_manifest: dict) -> dict:
        root = self.path.parent
        harness_evaluation = root / "evaluation_harness_evaluation.json"
        llm_dataset = root / "evaluation_llm_dataset.jsonl"
        campaign_rollup = root / "evaluation_campaign_rollup.json"
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
                    "failure_clusters": [
                        {
                            "connector": "case_to_dut",
                            "examples": [{"span_id": "span-dut"}],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        llm_dataset.write_text('{"sample":1}\n', encoding="utf-8")
        campaign_rollup.write_text(
            json.dumps(
                {
                    "summary": {"round_count": 1, "record_count": 1},
                    "coverage_trends": [],
                    "failure_trends": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        payload = {
            "kind": "fake.campaign_evaluation",
            "target": campaign_manifest.get("target"),
            "run_id": campaign_manifest.get("run_id"),
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return payload


class FakeHarnessOptimizerBackend:
    def __init__(self) -> None:
        self.task: dict | None = None

    def run(self, task: dict) -> dict:
        self.task = task
        return {
            "schema_version": 1,
            "kind": PROPOSAL_KIND,
            "proposal_id": "proposal-1",
            "status": "proposed",
            "source": "fake",
            "actions": [
                {
                    "action_id": "action-1",
                    "action_type": "scoreboard_check",
                    "target": "demo",
                    "rationale": "tighten failing case scoreboard checks",
                    "evidence_refs": [{"span_id": "span-dut"}],
                }
            ],
            "evidence_refs": [{"span_id": "span-dut"}],
        }


def test_campaign_profile_can_insert_harness_optimization_stages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        evaluation_path = root / "campaign" / "evaluation.json"
        optimizer = FakeHarnessOptimizerBackend()
        campaign = StubCampaign(
            CampaignConfig(
                target="demo",
                out_dir=root / "campaign",
                libafl_manifest=root / "Cargo.toml",
                modes=("heuristic_feedback",),
                campaign_plan_profile="campaign_with_evaluation_and_optimization",
                campaign_evaluation_out=evaluation_path,
            ),
            ObservationContext(run_id="run-1"),
            evaluation_backends=EvaluationBackends(
                campaign_evaluation=FakeHarnessTraceCampaignEvaluationBackend(
                    evaluation_path,
                ),
                harness_optimizer=optimizer,
            ),
        )

        manifest = campaign.run()
        paths = harness_optimization_paths(evaluation_path)
        task = json.loads(paths.task.read_text(encoding="utf-8"))
        proposal = json.loads(paths.proposal.read_text(encoding="utf-8"))
        decision = json.loads(paths.decision.read_text(encoding="utf-8"))

    assert optimizer.task is not None
    assert manifest["artifacts"]["harness_optimization_task"] == str(paths.task)
    assert task["summary"]["llm_sample_count"] == 1
    assert proposal["actions"][0]["action_type"] == "scoreboard_check"
    assert decision["decision"] == "accepted"
    assert decision["summary"]["accepted_action_count"] == 1


def test_campaign_orchestrator_can_insert_custom_campaign_stage() -> None:
    calls: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign = StubCampaign(
            CampaignConfig(
                target="demo",
                out_dir=root / "campaign",
                libafl_manifest=root / "Cargo.toml",
                modes=("heuristic_feedback",),
                campaign_plan_profile="manifest_with_custom_stage",
            ),
            ObservationContext(),
        )
        campaign.register_campaign_stage(
            "custom_campaign_stage",
            lambda: RunStage(
                name="custom_campaign_stage",
                handler=lambda results: calls.append(
                    results["campaign_manifest"]["target"]
                )
                or {"ok": True},
            ),
        )
        campaign.register_campaign_plan_profile(
            RunPlanProfile(
                name="manifest_with_custom_stage",
                stage_names=("campaign_manifest", "custom_campaign_stage"),
            )
        )

        manifest = campaign.run()

        assert manifest["target"] == "demo"
        assert calls == ["demo"]


def test_campaign_profile_rejects_missing_campaign_manifest_dependency() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign = StubCampaign(
            CampaignConfig(
                target="demo",
                out_dir=root / "campaign",
                libafl_manifest=root / "Cargo.toml",
                modes=("heuristic_feedback",),
                campaign_plan_profile="evaluation_without_manifest",
                campaign_evaluation_out=root / "campaign" / "evaluation.json",
            ),
            ObservationContext(),
            campaign_plan_profiles={
                "evaluation_without_manifest": RunPlanProfile(
                    name="evaluation_without_manifest",
                    stage_names=("campaign_evaluation",),
                )
            },
        )

        try:
            campaign.run()
        except ValueError as exc:
            assert "missing required result key" in str(exc)
            assert "campaign_manifest" in str(exc)
        else:
            raise AssertionError("campaign profile should require campaign_manifest")


class RecordingCampaignRun:
    instances: list["RecordingCampaignRun"] = []

    def __init__(
        self,
        config: FuzzRunConfig,
        observation_context: ObservationContext,
        **_kwargs,
    ) -> None:
        self.config = config
        self.observation_context = observation_context
        RecordingCampaignRun.instances.append(self)

    def feedback_fuzz(self) -> dict:
        return self._result("feedback_fuzz")

    def no_feedback_round(self) -> dict:
        return self._result("no_feedback")

    def _result(self, stage_name: str) -> dict:
        artifacts = {
            "round_manifest": str(self.config.round_manifest_out),
            "corpus": str(self.config.corpus),
        }
        if self.config.summary_out is not None:
            artifacts["coverage_summary"] = str(self.config.summary_out)
        if self.config.directives_out is not None:
            artifacts["mutation_directives"] = str(self.config.directives_out)
        if self.config.gap_feedback_out is not None:
            artifacts["gap_feedback"] = str(self.config.gap_feedback_out)
        if self.config.mutation_feedback_out is not None:
            artifacts["mutation_feedback"] = str(self.config.mutation_feedback_out)
        return {
            "round_manifest": {
                "kind": "libafl_bfm_fuzz.round_manifest",
                "round_id": self.config.round_id,
                "cwd": str(self.config.cwd) if self.config.cwd is not None else None,
                "artifacts": artifacts,
                "coverage": {"uncovered_line_count": 1},
                "feedback": {"directive_count": 1},
                "stages": {stage_name: {"returncode": 0}},
            }
        }


def test_campaign_round_scheduler_carries_previous_manifest_state() -> None:
    RecordingCampaignRun.instances = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        campaign = CampaignOrchestrator(
            CampaignConfig(
                target="demo",
                out_dir=root / "campaign",
                libafl_manifest=root / "Cargo.toml",
                modes=("heuristic_feedback",),
                rounds=2,
            ),
            ObservationContext(),
            run_orchestrator_factory=RecordingCampaignRun,
        )

        manifest = campaign.run()

        assert len(RecordingCampaignRun.instances) == 2
        first = RecordingCampaignRun.instances[0].config
        second = RecordingCampaignRun.instances[1].config
        assert first.directives is None
        assert second.directives == first.directives_out
        assert second.previous_directives == first.directives_out
        assert second.previous_summary == first.summary_out
        rounds = manifest["modes"][0]["rounds"]
        assert rounds[0]["previous_round_manifest"] is None
        assert rounds[1]["applied_directives"] == str(first.directives_out)
        assert rounds[1]["previous_summary"] == str(first.summary_out)
