from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from harness_optimization.campaign_optimization import CampaignOptimizationStageChain
from harness_optimization.observation import (
    ObservationContext,
    observation_context_from_env,
)
from harness_optimization.orchestrator import (
    PipelineContext,
    PipelineOrchestrator,
    StepSpec,
)
from .harness_evidence.optimization import (
    HarnessOptimizationAdapter,
    NoopHarnessCandidateEvaluationBackend,
    NoopHarnessOptimizerBackend,
    harness_optimization_paths,
)
from harness_optimization.paths import manifest_path_from_value, path_from_cwd, run_cwd
from harness_optimization.planning import (
    RunPlan,
    RunPlanExecutor,
    RunResults,
    RunStage,
    apply_stage_policies,
    RunPlanProfile,
    RunStageFactory,
    RunStageRegistry,
)
from .run_adapters import RunBackends
from .run_evaluation import CampaignEvaluationAdapter, EvaluationBackends
from .run_orchestrator import FuzzRunConfig, FuzzRunOrchestrator
from .run_profiles import (
    DEFAULT_CAMPAIGN_PLAN_PROFILES,
    DEFAULT_RUN_PLAN_PROFILES,
)
from .topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


MODE_ORDER = ("no_feedback", "heuristic_feedback", "llm_feedback")
FEEDBACK_MODES = {"heuristic_feedback", "llm_feedback"}


@dataclass(frozen=True)
class CampaignConfig:
    target: str
    out_dir: Path
    libafl_manifest: Path
    target_config: Path | None = None
    initial_directives: Path | None = None
    modes: tuple[str, ...] = ("heuristic_feedback",)
    rounds: int = 2
    iters: int = 256
    max_seeds: int = 32
    seed: int = 1
    cargo: str = "cargo"
    make: str = "make"
    verilog_sources: str | None = None
    toplevel: str | None = None
    verilator_coverage: str = "verilator_coverage"
    extra_make_vars: tuple[str, ...] = ()
    cwd: Path | None = None
    topology_out: Path | None = None
    observation_out: Path | None = None
    monitoring_out: Path | None = None
    campaign_manifest_out: Path | None = None
    run_plan_profile: str | None = None
    campaign_plan_profile: str | None = None
    round_evaluation: bool = False
    campaign_evaluation_out: Path | None = None
    ignore_functional_coverage: bool = False
    llm_model: str | None = None
    require_real_llm: bool = False


@dataclass(frozen=True)
class CampaignRoundArtifacts:
    target: str
    mode: str
    index: int
    run_dir: Path

    @property
    def round_id(self) -> str:
        return f"{self.mode}_round_{self.index:02d}"

    @property
    def corpus(self) -> Path:
        return self.run_dir / f"{self.target}_{self.mode}_round_{self.index:02d}_corpus.jsonl"

    @property
    def feedback_corpus(self) -> Path:
        return self.run_dir / f"{self.target}_feedback_corpus.jsonl"

    @property
    def coverage_dat(self) -> Path:
        return self.run_dir / f"{self.target}_coverage.dat"

    @property
    def coverage_info(self) -> Path:
        return self.run_dir / f"{self.target}_coverage.info"

    @property
    def coverage_annotated(self) -> Path:
        return self.run_dir / f"{self.target}_annotated"

    @property
    def functional_coverage(self) -> Path:
        return self.run_dir / f"{self.target}_uvm_functional_coverage.json"

    @property
    def feedback_functional_coverage(self) -> Path:
        return self.run_dir / f"{self.target}_feedback_uvm_functional_coverage.json"

    @property
    def summary(self) -> Path:
        return self.run_dir / f"{self.target}_coverage_summary.json"

    @property
    def heuristic_directives(self) -> Path:
        return self.run_dir / f"{self.target}_heuristic_mutation_directives.json"

    @property
    def directives(self) -> Path:
        return self.run_dir / f"{self.target}_mutation_directives.json"

    @property
    def prompt(self) -> Path:
        return self.run_dir / f"{self.target}_llm_prompt.json"

    @property
    def llm_response(self) -> Path:
        return self.run_dir / f"{self.target}_llm_response.json"

    @property
    def gap_feedback(self) -> Path:
        return self.run_dir / f"{self.target}_gap_feedback.json"

    @property
    def mutation_feedback(self) -> Path:
        return self.run_dir / f"{self.target}_mutation_feedback.json"

    @property
    def round_manifest(self) -> Path:
        return self.run_dir / f"{self.target}_round_manifest.json"

    @property
    def evaluation(self) -> Path:
        return self.run_dir / f"{self.target}_round_evaluation.json"


@dataclass(frozen=True)
class RoundManifestState:
    path: Path
    manifest: dict[str, Any]
    artifacts: dict[str, Path]

    @property
    def coverage_summary(self) -> Path | None:
        return self.artifacts.get("coverage_summary")

    @property
    def mutation_directives(self) -> Path | None:
        return self.artifacts.get("mutation_directives")

    @property
    def gap_feedback(self) -> Path | None:
        return self.artifacts.get("gap_feedback")

    @property
    def mutation_feedback(self) -> Path | None:
        return self.artifacts.get("mutation_feedback")

    def require_artifact(self, role: str) -> Path:
        try:
            return self.artifacts[role]
        except KeyError as exc:
            raise ValueError(
                f"{self.path}: missing required round manifest artifact {role!r}"
            ) from exc

    @property
    def round_id(self) -> str | None:
        value = self.manifest.get("round_id")
        return str(value) if value is not None else None


class CampaignRoundScheduler:
    """Expands campaign modes into round-level fuzz runs.

    The scheduler owns round state transitions. CampaignOrchestrator keeps the
    campaign-level plan, manifest, and connector wrapping.
    """

    def __init__(
        self,
        config: CampaignConfig,
        observation_context: ObservationContext,
        *,
        topology: PipelineTopology,
        out_dir: Callable[[], Path],
        run_backends: RunBackends | None = None,
        run_plan_profiles: Mapping[str, RunPlanProfile] | None = None,
        evaluation_backends: EvaluationBackends | None = None,
        run_orchestrator_factory: Callable[..., FuzzRunOrchestrator] = (
            FuzzRunOrchestrator
        ),
    ):
        self.config = config
        self.observation_context = observation_context
        self.topology = topology
        self.out_dir = out_dir
        self.run_backends = run_backends
        self.run_plan_profiles = run_plan_profiles
        self.evaluation_backends = evaluation_backends
        self.run_orchestrator_factory = run_orchestrator_factory

    def run_modes(self) -> list[dict[str, Any]]:
        return [self.run_mode(mode) for mode in self.config.modes]

    def run_mode(self, mode: str) -> dict[str, Any]:
        rounds = []
        previous: RoundManifestState | None = None
        for index in range(self.config.rounds):
            artifacts = self._round_artifacts(mode, index)
            artifacts.run_dir.mkdir(parents=True, exist_ok=True)
            use_previous = previous if mode in FEEDBACK_MODES else None
            round_config = self._round_config(mode, artifacts, previous=use_previous)
            round_result = self._run_round(mode, artifacts, round_config)
            manifest = self._round_manifest(round_result, artifacts.round_manifest)
            if mode == "llm_feedback" and self.config.require_real_llm:
                self._require_real_llm_source(manifest, artifacts.round_id)
            rounds.append(
                self._round_summary(
                    artifacts,
                    manifest,
                    round_result=round_result,
                    previous=use_previous,
                )
            )
            previous = (
                self._round_manifest_state(artifacts.round_manifest, manifest)
                if mode in FEEDBACK_MODES
                else None
            )
        return {"mode": mode, "rounds": rounds}

    def _run_round(
        self,
        mode: str,
        artifacts: CampaignRoundArtifacts,
        round_config: FuzzRunConfig,
    ) -> dict[str, object]:
        run = self.run_orchestrator_factory(
            round_config,
            self._round_context(artifacts.round_id),
            topology=self.topology,
            plan_profiles=self.run_plan_profiles,
            backends=self.run_backends,
            evaluation_backends=self.evaluation_backends,
        )
        if mode == "no_feedback":
            return run.no_feedback_round()
        return run.feedback_fuzz()

    def _round_config(
        self,
        mode: str,
        artifacts: CampaignRoundArtifacts,
        *,
        previous: RoundManifestState | None,
    ) -> FuzzRunConfig:
        feedback_mode = mode in FEEDBACK_MODES
        round_plan_profile = self._round_run_plan_profile(mode)
        round_evaluation = self._round_evaluation_enabled(round_plan_profile)
        return FuzzRunConfig(
            target=self.config.target,
            target_config=self.config.target_config,
            corpus=artifacts.corpus,
            libafl_manifest=self.config.libafl_manifest,
            directives=(
                previous.require_artifact("mutation_directives")
                if previous is not None
                else self.config.initial_directives
            ),
            iters=self.config.iters,
            max_seeds=self.config.max_seeds,
            seed=self.config.seed + artifacts.index,
            cargo=self.config.cargo,
            cwd=self.config.cwd,
            topology_out=self.config.topology_out,
            make=self.config.make,
            verilog_sources=self.config.verilog_sources,
            toplevel=self.config.toplevel,
            coverage_dir=artifacts.run_dir,
            coverage_dat=artifacts.coverage_dat,
            coverage_info=artifacts.coverage_info,
            coverage_annotated=artifacts.coverage_annotated,
            functional_coverage=artifacts.functional_coverage,
            feedback_functional_coverage=(
                artifacts.feedback_functional_coverage if feedback_mode else None
            ),
            verilator_coverage=self.config.verilator_coverage,
            extra_make_vars=self.config.extra_make_vars,
            summary_out=artifacts.summary if feedback_mode else None,
            directives_out=artifacts.directives if feedback_mode else None,
            heuristic_directives_out=(
                artifacts.heuristic_directives if feedback_mode else None
            ),
            prompt_out=artifacts.prompt if feedback_mode else None,
            previous_summary=(
                previous.require_artifact("coverage_summary")
                if feedback_mode and previous is not None
                else None
            ),
            previous_directives=(
                previous.require_artifact("mutation_directives")
                if feedback_mode and previous is not None
                else None
            ),
            previous_gap_feedback=(
                previous.gap_feedback
                if feedback_mode and previous is not None
                else None
            ),
            previous_mutation_feedback=(
                previous.mutation_feedback
                if feedback_mode and previous is not None
                else None
            ),
            gap_feedback_out=artifacts.gap_feedback if feedback_mode else None,
            mutation_feedback_out=(
                artifacts.mutation_feedback if feedback_mode else None
            ),
            llm_response_out=artifacts.llm_response if feedback_mode else None,
            feedback_corpus=artifacts.feedback_corpus if feedback_mode else None,
            ignore_functional_coverage=self.config.ignore_functional_coverage,
            llm=mode == "llm_feedback",
            llm_model=self.config.llm_model,
            mode=mode,
            round_id=artifacts.round_id,
            round_manifest_out=artifacts.round_manifest,
            run_plan_profile=round_plan_profile,
            evaluation_out=artifacts.evaluation if round_evaluation else None,
            observation_out=self.config.observation_out,
            monitoring_out=self.config.monitoring_out,
        )

    def _round_summary(
        self,
        artifacts: CampaignRoundArtifacts,
        manifest: dict[str, Any],
        *,
        round_result: dict[str, object],
        previous: RoundManifestState | None,
    ) -> dict[str, Any]:
        return {
            "index": artifacts.index,
            "round_id": artifacts.round_id,
            "run_dir": str(artifacts.run_dir),
            "round_manifest": str(artifacts.round_manifest),
            "previous_round_manifest": str(previous.path)
            if previous is not None
            else None,
            "previous_round_id": previous.round_id if previous is not None else None,
            "applied_directives": self._optional_state_path(
                previous, "mutation_directives"
            ),
            "previous_summary": self._optional_state_path(
                previous, "coverage_summary"
            ),
            "previous_gap_feedback": str(previous.gap_feedback)
            if previous is not None and previous.gap_feedback is not None
            else None,
            "previous_mutation_feedback": str(previous.mutation_feedback)
            if previous is not None and previous.mutation_feedback is not None
            else None,
            "artifacts": manifest.get("artifacts", {}),
            "evaluation": round_result.get("round_evaluation"),
            "coverage": manifest.get("coverage", {}),
            "feedback": manifest.get("feedback", {}),
            "stages": manifest.get("stages", {}),
        }

    def _round_run_plan_profile(self, mode: str) -> str | None:
        if self.config.run_plan_profile is not None:
            return self.config.run_plan_profile
        if not self.config.round_evaluation:
            return None
        return "no_feedback_with_evaluation" if mode == "no_feedback" else (
            "feedback_fuzz_with_evaluation"
        )

    def _round_evaluation_enabled(self, profile_name: str | None) -> bool:
        if self.config.round_evaluation:
            return True
        if profile_name is None:
            return False
        if self.run_plan_profiles is not None and profile_name in self.run_plan_profiles:
            stages = self.run_plan_profiles[profile_name].stage_names
        else:
            stages = DEFAULT_RUN_PLAN_PROFILES.get(
                profile_name,
                RunPlanProfile(profile_name, ()),
            ).stage_names
        return "round_evaluation" in stages

    def _round_artifacts(self, mode: str, index: int) -> CampaignRoundArtifacts:
        return CampaignRoundArtifacts(
            target=self.config.target,
            mode=mode,
            index=index,
            run_dir=self.out_dir() / mode / f"round_{index:02d}",
        )

    def _round_context(self, round_id: str) -> ObservationContext:
        return self.observation_context.with_overrides(round_id=round_id)

    def _round_manifest(
        self,
        result: dict[str, object],
        path: Path,
    ) -> dict[str, Any]:
        value = result.get("round_manifest")
        if isinstance(value, dict):
            return value
        return self._read_json(path)

    def _round_manifest_state(
        self,
        path: Path,
        manifest: dict[str, Any],
    ) -> RoundManifestState:
        return RoundManifestState(
            path=path,
            manifest=manifest,
            artifacts=self._round_manifest_artifacts(path, manifest),
        )

    def _round_manifest_artifacts(
        self,
        path: Path,
        manifest: dict[str, Any],
    ) -> dict[str, Path]:
        artifacts = manifest.get("artifacts", {})
        if not isinstance(artifacts, dict):
            raise ValueError(f"{path}: artifacts must be a JSON object")
        return {
            str(role): self._manifest_path(path, manifest, value)
            for role, value in artifacts.items()
            if isinstance(value, str) and value
        }

    def _manifest_path(
        self,
        manifest_path: Path,
        manifest: dict[str, Any],
        value: str,
    ) -> Path:
        resolved = manifest_path_from_value(
            value,
            manifest_path=manifest_path,
            cwd=manifest.get("cwd"),
        )
        if resolved is None:
            raise ValueError(f"{manifest_path}: missing manifest path value")
        return resolved

    def _optional_state_path(
        self,
        state: RoundManifestState | None,
        role: str,
    ) -> str | None:
        if state is None:
            return None
        path = state.artifacts.get(role)
        return str(path) if path is not None else None

    def _require_real_llm_source(
        self,
        manifest: dict[str, Any],
        round_id: str,
    ) -> None:
        source = str(manifest.get("feedback", {}).get("directive_source", ""))
        lowered = source.lower()
        if "llm" not in lowered or "heuristic" in lowered or "failed" in lowered:
            raise RuntimeError(f"{round_id}: expected real LLM directives, got {source!r}")

    def _read_json(self, path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return value


class CampaignOrchestrator:
    """Multi-round campaign orchestration built from single-round fuzz runs."""

    def __init__(
        self,
        config: CampaignConfig,
        observation_context: ObservationContext | None = None,
        *,
        topology: PipelineTopology = FULL_FUZZ_TOPOLOGY,
        run_backends: RunBackends | None = None,
        run_plan_profiles: Mapping[str, RunPlanProfile] | None = None,
        campaign_plan_profiles: Mapping[str, RunPlanProfile] | None = None,
        evaluation_backends: EvaluationBackends | None = None,
        run_orchestrator_factory: Callable[..., FuzzRunOrchestrator] = (
            FuzzRunOrchestrator
        ),
    ):
        self.config = config
        self.observation_context = observation_context or observation_context_from_env()
        self.topology = topology
        self.run_backends = run_backends
        self.run_plan_profiles = run_plan_profiles
        self.evaluation_backends = evaluation_backends or EvaluationBackends()
        self.campaign_plan_profiles = {
            name: profile
            for name, profile in DEFAULT_CAMPAIGN_PLAN_PROFILES.items()
        }
        if campaign_plan_profiles is not None:
            self.campaign_plan_profiles.update(campaign_plan_profiles)
        self.context = PipelineContext(
            run_id=self.observation_context.run_id,
            artifacts={
                "round_manifest": self._out_dir(),
                "campaign_manifest": self._campaign_manifest_out(),
                **self._campaign_evaluation_context_artifact(),
            },
            metadata={"target": config.target, "stage": "feedback_campaign"},
        )
        self.orchestrator = PipelineOrchestrator(
            topology,
            self.observation_context,
            topology_out=config.topology_out,
        )
        self.optimization_stage_chain = CampaignOptimizationStageChain(
            target=self.config.target,
            modes=self.config.modes,
            rounds=self.config.rounds,
            context_artifacts=self.context.artifacts,
            step_runner=lambda step: self.orchestrator.run_step(step, self.context),
            paths_factory=lambda: harness_optimization_paths(
                self._campaign_evaluation_out()
            ),
            adapter_factory=lambda paths: HarnessOptimizationAdapter(
                target=self.config.target,
                paths=paths,
                campaign_evaluation_path=self._campaign_evaluation_out(),
                campaign_manifest_path=self._campaign_manifest_out(),
                cwd=self._run_cwd(),
                optimizer_backend=(
                    self.evaluation_backends.harness_optimizer
                    or NoopHarnessOptimizerBackend()
                ),
                candidate_evaluation_backend=(
                    self.evaluation_backends.harness_candidate_evaluation
                    or NoopHarnessCandidateEvaluationBackend()
                ),
                plugin_registry=self.evaluation_backends.harness_plugin_registry,
            ),
        )
        self.campaign_stage_registry = self._default_campaign_stage_registry()
        self.round_scheduler = CampaignRoundScheduler(
            config,
            self.observation_context,
            topology=topology,
            out_dir=self._out_dir,
            run_backends=run_backends,
            run_plan_profiles=run_plan_profiles,
            evaluation_backends=self.evaluation_backends,
            run_orchestrator_factory=run_orchestrator_factory,
        )

    def run(self) -> dict[str, Any]:
        self._validate()
        self.orchestrator.write_topology()
        self._out_dir().mkdir(parents=True, exist_ok=True)

        mode_runs = []
        for mode in self.config.modes:
            mode_runs.append(self._run_mode(mode))

        results = self._run_campaign_plan(mode_runs)
        value = results.get("campaign_manifest")
        if not isinstance(value, dict):
            raise ValueError("campaign_manifest stage did not return a manifest")
        return value

    def _run_mode(self, mode: str) -> dict[str, Any]:
        return self.round_scheduler.run_mode(mode)

    def _run_campaign_plan(self, mode_runs: list[dict[str, Any]]) -> RunResults:
        plan = self._campaign_plan()
        return RunPlanExecutor().run(plan, initial_results={"mode_runs": mode_runs})

    def _campaign_plan(self) -> RunPlan:
        profile_name = self._selected_campaign_plan_profile()
        profile = self._campaign_profile(profile_name)
        stages = apply_stage_policies(
            self.campaign_stage_registry.build_many(profile.stage_names),
            profile.stage_policies,
            profile_name=profile.name,
            plan_label="campaign",
        )
        plan = RunPlan(
            name=profile_name,
            stages=stages,
            write_topology=False,
            initial_result_keys=("mode_runs",),
            initial_artifact_roles=tuple(self.context.artifacts),
        )
        plan.validate()
        return plan

    def _selected_campaign_plan_profile(self) -> str:
        if self.config.campaign_plan_profile is not None:
            return self.config.campaign_plan_profile
        if self.config.campaign_evaluation_out is not None:
            return "campaign_with_evaluation"
        return "campaign_manifest"

    def _campaign_profile(self, name: str) -> RunPlanProfile:
        try:
            return self.campaign_plan_profiles[name]
        except KeyError as exc:
            raise ValueError(f"unknown campaign plan profile: {name}") from exc

    def register_campaign_stage(
        self,
        name: str,
        factory: RunStageFactory,
        *,
        overwrite: bool = False,
    ) -> None:
        self.campaign_stage_registry.register(name, factory, overwrite=overwrite)

    def register_campaign_plan_profile(self, profile: RunPlanProfile) -> None:
        self.campaign_plan_profiles[profile.name] = profile

    def _default_campaign_stage_registry(self) -> RunStageRegistry:
        return RunStageRegistry(
            {
                "campaign_manifest": lambda: RunStage(
                    name="campaign_manifest",
                    handler=lambda results: self._write_campaign_manifest(
                        self._mode_runs_from_results(results)
                    ),
                    requires_results=("mode_runs",),
                    produces_results=("campaign_manifest",),
                    input_roles=("round_manifest",),
                    output_roles=("campaign_manifest",),
                ),
                "campaign_evaluation": lambda: RunStage(
                    name="campaign_evaluation",
                    handler=lambda results: self._write_campaign_evaluation(
                        results,
                    ),
                    requires_results=("campaign_manifest",),
                    produces_results=("campaign_evaluation",),
                    input_roles=("campaign_manifest",),
                    output_roles=("evaluation_report",),
                ),
                **self.optimization_stage_chain.stage_factories(),
            }
        )

    def _mode_runs_from_results(self, results: RunResults) -> list[dict[str, Any]]:
        value = results.get("mode_runs")
        if not isinstance(value, list):
            raise ValueError("campaign plan missing mode_runs")
        return [item for item in value if isinstance(item, dict)]

    def _write_campaign_manifest(
        self,
        mode_runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        step = StepSpec(
            name="campaign_manifest",
            connector="round_manifest_to_campaign_manifest",
            handler=lambda _context: self._write_campaign_manifest_file(mode_runs),
            input_roles=("round_manifest",),
            output_roles=("campaign_manifest",),
            metrics=lambda value: {
                "mode_count": len(value.get("modes", [])),
                "round_count": sum(
                    len(mode.get("rounds", [])) for mode in value.get("modes", [])
                ),
            },
            metadata={
                "target": self.config.target,
                "modes": ",".join(self.config.modes),
                "rounds": self.config.rounds,
            },
        )
        return self.orchestrator.run_step(step, self.context)

    def _write_campaign_manifest_file(
        self,
        mode_runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        manifest = self._campaign_manifest_payload(mode_runs)
        path = self._campaign_manifest_out()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest

    def _write_campaign_evaluation(
        self,
        stage_results: RunResults,
    ) -> dict[str, Any]:
        path = self._campaign_evaluation_out()
        self.context.artifacts["evaluation_report"] = path
        manifest = stage_results.get("campaign_manifest")
        if not isinstance(manifest, dict):
            raise ValueError("campaign_evaluation requires campaign_manifest result")
        step = StepSpec(
            name="campaign_evaluation",
            connector="campaign_to_evaluation_report",
            handler=lambda _context: self._campaign_evaluation_backend(path).run(
                manifest
            ),
            input_roles=("campaign_manifest",),
            output_roles=("evaluation_report",),
            metrics=lambda value: {
                "mode_count": value.get("summary", {}).get("mode_count", 0),
                "round_count": value.get("summary", {}).get("round_count", 0),
            },
            metadata={
                "target": self.config.target,
                "modes": ",".join(self.config.modes),
                "rounds": self.config.rounds,
            },
        )
        return self.orchestrator.run_step(step, self.context)

    def _campaign_evaluation_backend(self, path: Path):
        if self.evaluation_backends.campaign_evaluation is not None:
            return self.evaluation_backends.campaign_evaluation
        return CampaignEvaluationAdapter(
            target=self.config.target,
            path=path,
            observation_context=self.observation_context,
            cwd=self._run_cwd(),
        )

    def _campaign_manifest_payload(
        self,
        mode_runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "libafl_bfm_fuzz.campaign_manifest",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "target": self.config.target,
            "run_id": self.observation_context.run_id,
            "cwd": str(self._run_cwd()),
            "rounds": self.config.rounds,
            "mode_names": list(self.config.modes),
            "config": {
                "iters": self.config.iters,
                "max_seeds": self.config.max_seeds,
                "seed": self.config.seed,
                "cargo": self.config.cargo,
                "make": self.config.make,
                "verilator_coverage": self.config.verilator_coverage,
                "toplevel": self.config.toplevel,
                "verilog_sources": self.config.verilog_sources,
                "extra_make_vars": list(self.config.extra_make_vars),
                "initial_directives": (
                    str(self._path_from_cwd(self.config.initial_directives))
                    if self.config.initial_directives is not None
                    else None
                ),
                "ignore_functional_coverage": self.config.ignore_functional_coverage,
                "llm_model": self.config.llm_model,
                "require_real_llm": self.config.require_real_llm,
                "run_plan_profile": self.config.run_plan_profile,
                "campaign_plan_profile": self.config.campaign_plan_profile,
                "round_evaluation": self.config.round_evaluation,
            },
            "artifacts": {
                "out_dir": str(self._out_dir()),
                "campaign_manifest": str(self._campaign_manifest_out()),
                **self._campaign_evaluation_manifest_artifact(),
                **self._campaign_optimization_manifest_artifacts(),
                **self._optional_artifacts(),
            },
            "modes": mode_runs,
        }

    def _campaign_evaluation_context_artifact(self) -> dict[str, Path]:
        path = self._path_from_cwd(self.config.campaign_evaluation_out)
        return {"evaluation_report": path} if path is not None else {}

    def _campaign_evaluation_manifest_artifact(self) -> dict[str, str]:
        path = self._path_from_cwd(self.config.campaign_evaluation_out)
        return {"evaluation_report": str(path)} if path is not None else {}

    def _campaign_optimization_manifest_artifacts(self) -> dict[str, str]:
        try:
            profile = self._campaign_profile(self._selected_campaign_plan_profile())
        except ValueError:
            return {}
        return self.optimization_stage_chain.manifest_artifacts(profile.stage_names)

    def _optional_artifacts(self) -> dict[str, str]:
        paths = {
            "target_manifest": self.config.target_config,
            "initial_directives": self.config.initial_directives,
            "libafl_manifest": self.config.libafl_manifest,
            "observation_events": self.config.observation_out,
            "monitoring": self.config.monitoring_out,
            "topology": self.config.topology_out,
        }
        return {
            name: str(self._path_from_cwd(path))
            for name, path in paths.items()
            if path is not None
        }

    def _campaign_manifest_out(self) -> Path:
        path = (
            self.config.campaign_manifest_out
            or self.config.out_dir / "campaign_manifest.json"
        )
        resolved = self._path_from_cwd(path)
        if resolved is None:
            raise ValueError("missing campaign manifest path")
        return resolved

    def _campaign_evaluation_out(self) -> Path:
        resolved = self._path_from_cwd(self.config.campaign_evaluation_out)
        if resolved is None:
            raise ValueError("missing campaign evaluation output path")
        return resolved

    def _out_dir(self) -> Path:
        resolved = self._path_from_cwd(self.config.out_dir)
        if resolved is None:
            raise ValueError("missing campaign output directory")
        return resolved

    def _path_from_cwd(self, path: Path | None) -> Path | None:
        return path_from_cwd(path, self.config.cwd)

    def _run_cwd(self) -> Path:
        return run_cwd(self.config.cwd)

    def _validate(self) -> None:
        if self.config.rounds < 1:
            raise ValueError("campaign rounds must be >= 1")
        unknown = [mode for mode in self.config.modes if mode not in MODE_ORDER]
        if unknown:
            raise ValueError("unknown campaign mode(s): " + ", ".join(unknown))


def normalize_campaign_modes(raw_modes: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(raw_modes, str):
        requested = [mode.strip() for mode in raw_modes.split(",") if mode.strip()]
    else:
        requested = [str(mode).strip() for mode in raw_modes if str(mode).strip()]
    if not requested:
        raise ValueError("provide at least one campaign mode")
    if "all" in requested:
        requested = list(MODE_ORDER)
    unknown = [mode for mode in requested if mode not in MODE_ORDER]
    if unknown:
        raise ValueError("unknown campaign mode(s): " + ", ".join(unknown))
    return tuple(mode for mode in MODE_ORDER if mode in set(requested))


def run_feedback_campaign_pipeline(
    config: CampaignConfig,
    observation_context: ObservationContext | None = None,
    *,
    evaluation_backends: EvaluationBackends | None = None,
) -> dict[str, Any]:
    return CampaignOrchestrator(
        config,
        observation_context,
        evaluation_backends=evaluation_backends,
    ).run()
