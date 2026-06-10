from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from connector_observe import ObservationContext
from fuzz_pipeline import CampaignConfig, CampaignOrchestrator, FuzzRunConfig
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
