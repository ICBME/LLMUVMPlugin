from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from ConnectGraph import ObservationContext, observation_context_from_env
from fuzz_bfm.corpus import load_cases
from fuzz_bfm.target_config import load_target_config

from .coverage_feedback import (
    CoverageFeedbackConfig,
    CoverageFeedbackResult,
    run_coverage_feedback_pipeline,
)
from harness_optimization.runtime import (
    COVERAGE_FEEDBACK_TUNING_CONFIG_ENV,
    RUNTIME_METRICS_OUT_ENV,
    extra_make_var_value,
)
from .orchestrator import (
    PipelineContext,
    PipelineOrchestrator,
    StepSpec,
    external_command_step,
)
from harness_optimization.paths import RunPathResolver
from harness_optimization.planning import (
    RunPlan,
    RunPlanExecutor,
    RunResults,
    RunStage,
    RunPlanProfile,
    RunStageFactory,
    RunStageRegistry,
)
from .run_adapters import (
    CorpusGeneratorAdapter,
    CoverageReportAdapter,
    RunBackends,
    UvmReplayAdapter,
)
from .run_evaluation import EvaluationBackends, RunEvaluationAdapter
from .run_profiles import (
    DEFAULT_RUN_PLAN_PROFILES,
    DEFAULT_RUN_PLAN_STAGE_NAMES,
)
from .topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


@dataclass(frozen=True)
class FuzzRunConfig:
    target: str
    corpus: Path
    libafl_manifest: Path
    target_config: Path | None = None
    directives: Path | None = None
    iters: int = 256
    max_seeds: int = 32
    seed: int = 1
    cargo: str = "cargo"
    cwd: Path | None = None
    topology_out: Path | None = None
    generator_command: tuple[str, ...] | None = None
    make: str = "make"
    verilog_sources: str | None = None
    toplevel: str | None = None
    coverage_dir: Path | None = None
    coverage_dat: Path | None = None
    coverage_info: Path | None = None
    coverage_annotated: Path | None = None
    functional_coverage: Path | None = None
    feedback_functional_coverage: Path | None = None
    verilator_coverage: str = "verilator_coverage"
    extra_make_vars: tuple[str, ...] = ()
    summary_out: Path | None = None
    directives_out: Path | None = None
    heuristic_directives_out: Path | None = None
    prompt_out: Path | None = None
    previous_summary: Path | None = None
    previous_directives: Path | None = None
    previous_gap_feedback: Path | None = None
    previous_mutation_feedback: Path | None = None
    gap_feedback_out: Path | None = None
    mutation_feedback_out: Path | None = None
    llm_response_out: Path | None = None
    feedback_corpus: Path | None = None
    ignore_functional_coverage: bool = False
    llm: bool = False
    llm_model: str | None = None
    mode: str | None = None
    round_id: str | None = None
    round_manifest_out: Path | None = None
    run_plan_profile: str | None = None
    evaluation_out: Path | None = None
    observation_out: Path | None = None
    monitoring_out: Path | None = None


class FuzzRunOrchestrator:
    """Top-level reusable orchestration for fuzz harness stages."""

    def __init__(
        self,
        config: FuzzRunConfig,
        observation_context: ObservationContext | None = None,
        *,
        topology: PipelineTopology = FULL_FUZZ_TOPOLOGY,
        plan_stage_names: Mapping[str, Sequence[str]] | None = None,
        plan_profiles: Mapping[str, RunPlanProfile] | None = None,
        backends: RunBackends | None = None,
        evaluation_backends: EvaluationBackends | None = None,
    ):
        self.config = config
        self.observation_context = observation_context or observation_context_from_env()
        self.paths = RunPathResolver(config)
        self.context = PipelineContext(
            run_id=self.observation_context.run_id,
            artifacts=self._artifacts(),
            metadata=self._context_metadata(),
        )
        self.paths.artifacts = self.context.artifacts
        backends = backends or RunBackends()
        self.corpus_generator = backends.corpus_generator or CorpusGeneratorAdapter(
            config,
            self.paths,
        )
        self.uvm_replay = backends.uvm_replay or UvmReplayAdapter(
            config,
            self.paths,
            self.observation_context,
            round_id=self._round_id,
        )
        self.coverage_report = backends.coverage_report or CoverageReportAdapter(
            config,
            self.paths,
        )
        evaluation_backends = evaluation_backends or EvaluationBackends()
        self.evaluation = evaluation_backends.round_evaluation or (
            RunEvaluationAdapter(
                config,
                self.paths,
                self.observation_context,
                round_id=self._round_id,
            )
        )
        self.orchestrator = PipelineOrchestrator(
            topology,
            self.observation_context,
            topology_out=config.topology_out,
        )
        self.run_stage_registry = self._default_run_stage_registry()
        self.plan_profiles = {
            name: profile for name, profile in DEFAULT_RUN_PLAN_PROFILES.items()
        }
        if plan_profiles is not None:
            self.plan_profiles.update(plan_profiles)
        self.plan_stage_names: dict[str, tuple[str, ...]] = {
            name: tuple(stages)
            for name, stages in DEFAULT_RUN_PLAN_STAGE_NAMES.items()
        }
        self.plan_stage_names.update(
            {
                name: profile.stage_names
                for name, profile in self.plan_profiles.items()
            }
        )
        if plan_stage_names is not None:
            for name, stages in plan_stage_names.items():
                self.set_run_plan_stage_names(name, stages)

    def generate_corpus(self) -> subprocess.CompletedProcess:
        command = list(self.config.generator_command or self._default_generator_command())
        step = external_command_step(
            name="corpus_generator_to_corpus",
            connector="corpus_generator_to_corpus",
            command=command,
            input_roles=self._generator_input_roles(),
            output_roles=("corpus",),
            metadata={
                "target": self.config.target,
                "iters": self.config.iters,
                "max_seeds": self.config.max_seeds,
            },
            cwd=self.config.cwd,
        )
        return self.orchestrator.run_step(step, self.context)

    def validate_corpus(self):
        return self._validate_corpus(
            self._path_from_cwd(self.config.corpus),
            name="corpus_to_validation",
        )

    def validate_feedback_corpus(self):
        self._require_feedback_replay_paths()
        self.context.artifacts["corpus"] = self._feedback_corpus()
        return self._validate_corpus(
            self._feedback_corpus(),
            name="feedback_corpus_to_validation",
        )

    def _validate_corpus(self, corpus: Path | None, *, name: str):
        if corpus is None:
            raise ValueError("missing corpus path for validation")
        step = StepSpec(
            name=name,
            connector="corpus_to_validation",
            handler=lambda _context: load_cases(
                corpus,
                self.config.target,
                config=load_target_config(
                    self.config.target,
                    target_config=self._path_from_cwd(self.config.target_config),
                ),
            ),
            input_roles=("corpus",),
            output_roles=("corpus",),
            metrics=lambda value: {"case_count": len(value)},
        )
        return self.orchestrator.run_step(step, self.context)

    def generate_and_validate(self):
        results = self._run_plan(self._generate_and_validate_plan())
        return results["corpus_validation"]

    def coverage_run_pipeline(self) -> dict[str, object]:
        return self._run_plan(self._coverage_run_plan())

    def coverage_run(self) -> subprocess.CompletedProcess:
        self._require_coverage_paths()
        step = StepSpec(
            name="coverage_run",
            connector="corpus_to_uvm_replay_process",
            handler=lambda _context: self._run_coverage_replay(),
            input_roles=("corpus",),
            output_roles=("rtl_coverage_dat", "replay_artifacts"),
            metrics=lambda result: {"returncode": int(result.returncode)},
            metadata=self._coverage_metadata(),
        )
        return self.orchestrator.run_step(step, self.context)

    def generate_coverage_report(self) -> dict[str, subprocess.CompletedProcess]:
        self._require_coverage_paths()
        step = StepSpec(
            name="coverage_report",
            connector="rtl_coverage_to_coverage_report",
            handler=lambda _context: self._run_verilator_coverage_report(),
            input_roles=("rtl_coverage_dat",),
            output_roles=("coverage_info", "coverage_annotated"),
            metrics=lambda result: {
                "annotate_returncode": int(result["annotate"].returncode),
                "write_info_returncode": int(result["write_info"].returncode),
            },
            metadata=self._coverage_metadata(),
        )
        return self.orchestrator.run_step(step, self.context)

    def coverage_run_and_report(self) -> dict[str, object]:
        return self._run_plan(self._coverage_report_plan())

    def coverage_feedback(self) -> CoverageFeedbackResult:
        return run_coverage_feedback_pipeline(
            self._coverage_feedback_config(),
            self.observation_context,
        )

    def feedback_replay(self) -> subprocess.CompletedProcess:
        self._require_feedback_replay_paths()
        self.context.artifacts["directives"] = self._feedback_directives()
        self.context.artifacts["corpus"] = self._feedback_corpus()
        step = external_command_step(
            name="feedback_replay",
            connector="directives_to_feedback_replay",
            command=self._feedback_replay_command(),
            input_roles=("directives", "corpus"),
            output_roles=("replay_artifacts",),
            metadata=self._feedback_replay_metadata(),
            cwd=self._run_cwd(),
        )
        return self.orchestrator.run_step(step, self.context)

    def generate_feedback_corpus(self) -> subprocess.CompletedProcess:
        self._require_feedback_replay_paths()
        self.context.artifacts["directives"] = self._feedback_directives()
        self.context.artifacts["corpus"] = self._feedback_corpus()
        step = external_command_step(
            name="feedback_corpus_generator_to_corpus",
            connector="corpus_generator_to_corpus",
            command=list(self._feedback_generator_command()),
            input_roles=self._feedback_generator_input_roles(),
            output_roles=("corpus",),
            metadata=self._feedback_corpus_metadata(),
            cwd=self.config.cwd,
        )
        return self.orchestrator.run_step(step, self.context)

    def feedback_fuzz(self) -> dict[str, object]:
        return self._run_plan(self._feedback_fuzz_plan())

    def no_feedback_round(self) -> dict[str, object]:
        return self._run_plan(self._no_feedback_plan())

    def _run_plan(self, plan: RunPlan) -> RunResults:
        return RunPlanExecutor(write_topology=self.orchestrator.write_topology).run(plan)

    def _generate_and_validate_plan(self) -> RunPlan:
        return self.build_run_plan("generate_and_validate")

    def _coverage_run_plan(self) -> RunPlan:
        return self.build_run_plan("coverage_run")

    def _coverage_report_plan(self) -> RunPlan:
        return self.build_run_plan("coverage_report")

    def _feedback_fuzz_plan(self) -> RunPlan:
        return self.build_run_plan(
            self._selected_plan_name("feedback_fuzz"),
            expected_mode="feedback_fuzz",
        )

    def _no_feedback_plan(self) -> RunPlan:
        return self.build_run_plan(
            self._selected_plan_name("no_feedback"),
            expected_mode="no_feedback",
        )

    def build_run_plan(
        self,
        name: str,
        *,
        stage_names: Sequence[str] | None = None,
        expected_mode: str | None = None,
    ) -> RunPlan:
        names = (
            tuple(stage_names)
            if stage_names is not None
            else self._stage_names(name, expected_mode=expected_mode)
        )
        stages = self._apply_profile_policies(
            name,
            self.run_stage_registry.build_many(names),
        )
        plan = RunPlan(
            name=name,
            stages=stages,
            initial_artifact_roles=tuple(self.context.artifacts),
        )
        plan.validate()
        return plan

    def _apply_profile_policies(
        self,
        name: str,
        stages: tuple[RunStage, ...],
    ) -> tuple[RunStage, ...]:
        profile = self.plan_profiles.get(name)
        if profile is None or not profile.stage_policies:
            return stages
        names = {stage.name for stage in stages}
        unknown = sorted(set(profile.stage_policies) - names)
        if unknown:
            raise ValueError(
                f"run plan profile {name!r} declares policy for unknown "
                f"stage(s): {unknown}"
            )
        return tuple(
            replace(
                stage,
                policy=profile.stage_policies.get(stage.name, stage.policy),
            )
            for stage in stages
        )

    def register_run_stage(
        self,
        name: str,
        factory: RunStageFactory,
        *,
        overwrite: bool = False,
    ) -> None:
        self.run_stage_registry.register(name, factory, overwrite=overwrite)

    def set_run_plan_stage_names(
        self,
        name: str,
        stage_names: Sequence[str],
    ) -> None:
        self.plan_stage_names[name] = tuple(stage_names)

    def register_run_plan_profile(self, profile: RunPlanProfile) -> None:
        self.plan_profiles[profile.name] = profile
        self.set_run_plan_stage_names(profile.name, profile.stage_names)

    def _stage_names(
        self,
        name: str,
        *,
        expected_mode: str | None = None,
    ) -> tuple[str, ...]:
        try:
            names = self.plan_stage_names[name]
        except KeyError as exc:
            raise ValueError(f"unknown run plan: {name}") from exc
        self._validate_profile_mode(name, expected_mode)
        return self._with_requested_round_evaluation(name, names)

    def _validate_profile_mode(
        self,
        name: str,
        expected_mode: str | None,
    ) -> None:
        if expected_mode is None:
            return
        profile = self.plan_profiles.get(name)
        if profile is None or profile.mode is None:
            return
        if profile.mode != expected_mode:
            raise ValueError(
                f"run plan profile {name!r} is for mode {profile.mode!r}, "
                f"but this run requires {expected_mode!r}"
            )

    def _with_requested_round_evaluation(
        self,
        name: str,
        stage_names: tuple[str, ...],
    ) -> tuple[str, ...]:
        if self.config.evaluation_out is None or "round_evaluation" in stage_names:
            return stage_names
        if "round_manifest" not in stage_names:
            raise ValueError(
                f"run plan profile {name!r} cannot enable round_evaluation "
                "without a round_manifest stage"
            )
        return (*stage_names, "round_evaluation")

    def _selected_plan_name(self, default: str) -> str:
        if self.config.run_plan_profile is not None:
            return self.config.run_plan_profile
        if self.config.evaluation_out is not None:
            evaluation_profile = f"{default}_with_evaluation"
            if evaluation_profile in self.plan_stage_names:
                return evaluation_profile
        return default

    def _default_run_stage_registry(self) -> RunStageRegistry:
        return RunStageRegistry(
            {
                "corpus_generation": lambda: self._stage(
                    "corpus_generation",
                    self.generate_corpus,
                    input_roles=self._generator_input_roles(),
                    output_roles=("corpus",),
                ),
                "corpus_validation": lambda: self._stage(
                    "corpus_validation",
                    self.validate_corpus,
                    input_roles=("corpus",),
                    output_roles=("corpus",),
                ),
                "coverage_run": lambda: self._stage(
                    "coverage_run",
                    self.coverage_run,
                    requires_results=("corpus_validation",),
                    input_roles=("corpus",),
                    output_roles=("rtl_coverage_dat", "replay_artifacts"),
                ),
                "coverage_report": lambda: self._merge_stage(
                    "coverage_report",
                    self.generate_coverage_report,
                    requires_results=("coverage_run",),
                    produces_results=("annotate", "write_info"),
                    input_roles=("rtl_coverage_dat",),
                    output_roles=("coverage_info", "coverage_annotated"),
                ),
                "coverage_feedback": lambda: self._stage(
                    "coverage_feedback",
                    self.coverage_feedback,
                    requires_results=("annotate", "write_info"),
                    input_roles=("coverage_info", "corpus"),
                    output_roles=self._coverage_feedback_output_roles(),
                ),
                "feedback_corpus_generation": lambda: self._stage(
                    "feedback_corpus_generation",
                    self.generate_feedback_corpus,
                    requires_results=("coverage_feedback",),
                    input_roles=self._feedback_generator_input_roles(),
                    output_roles=("corpus",),
                ),
                "feedback_corpus_validation": lambda: self._stage(
                    "feedback_corpus_validation",
                    self.validate_feedback_corpus,
                    requires_results=("feedback_corpus_generation",),
                    input_roles=("corpus",),
                    output_roles=("corpus",),
                ),
                "feedback_replay": lambda: self._stage(
                    "feedback_replay",
                    self.feedback_replay,
                    requires_results=("feedback_corpus_validation",),
                    input_roles=("directives", "corpus"),
                    output_roles=("replay_artifacts",),
                ),
                "round_manifest": lambda: self._result_stage(
                    "round_manifest",
                    self.write_round_manifest,
                    requires_results=("annotate", "write_info"),
                    input_roles=("corpus",),
                    output_roles=("round_manifest",),
                ),
                "round_evaluation": lambda: self._result_stage(
                    "round_evaluation",
                    self.write_round_evaluation,
                    requires_results=("round_manifest",),
                    input_roles=("round_manifest",),
                    output_roles=("evaluation_report",),
                ),
            }
        )

    def _stage(
        self,
        name: str,
        handler,
        *,
        requires_results: tuple[str, ...] = (),
        produces_results: tuple[str, ...] | None = None,
        input_roles: tuple[str, ...] = (),
        output_roles: tuple[str, ...] = (),
    ) -> RunStage:
        return RunStage(
            name=name,
            handler=lambda _results: handler(),
            requires_results=requires_results,
            produces_results=produces_results or (name,),
            input_roles=input_roles,
            output_roles=output_roles,
        )

    def _merge_stage(
        self,
        name: str,
        handler,
        *,
        requires_results: tuple[str, ...] = (),
        produces_results: tuple[str, ...] = (),
        input_roles: tuple[str, ...] = (),
        output_roles: tuple[str, ...] = (),
    ) -> RunStage:
        return RunStage(
            name=name,
            handler=lambda _results: handler(),
            merge_mapping=True,
            requires_results=requires_results,
            produces_results=produces_results,
            input_roles=input_roles,
            output_roles=output_roles,
        )

    def _result_stage(
        self,
        name: str,
        handler,
        *,
        requires_results: tuple[str, ...] = (),
        produces_results: tuple[str, ...] | None = None,
        input_roles: tuple[str, ...] = (),
        output_roles: tuple[str, ...] = (),
    ) -> RunStage:
        return RunStage(
            name=name,
            handler=handler,
            requires_results=requires_results,
            produces_results=produces_results or (name,),
            input_roles=input_roles,
            output_roles=output_roles,
        )

    def _coverage_feedback_output_roles(self) -> tuple[str, ...]:
        roles = ["summary", "directives", "heuristic_directives", "prompt"]
        for role in ("gap_feedback", "mutation_feedback", "llm_response"):
            if role in self.context.artifacts:
                roles.append(role)
        return tuple(roles)

    def write_round_manifest(
        self,
        stage_results: dict[str, object],
    ) -> dict[str, Any] | None:
        if self.config.round_manifest_out is None:
            return None
        self.context.artifacts["round_manifest"] = self._path_from_cwd(
            self.config.round_manifest_out
        )
        step = StepSpec(
            name="round_manifest",
            connector="round_artifacts_to_round_manifest",
            handler=lambda _context: self._write_round_manifest(stage_results),
            input_roles=self._round_manifest_input_roles(stage_results),
            output_roles=("round_manifest",),
            metrics=lambda value: {
                "artifact_count": len(value.get("artifacts", {})),
                "directive_count": value.get("feedback", {}).get("directive_count", 0),
            },
            metadata=self._round_manifest_metadata(),
        )
        return self.orchestrator.run_step(step, self.context)

    def write_round_evaluation(
        self,
        stage_results: dict[str, object],
    ) -> dict[str, Any]:
        if self.config.evaluation_out is None:
            raise ValueError("missing evaluation_out for round_evaluation stage")
        self.context.artifacts["evaluation_report"] = self._path_from_cwd(
            self.config.evaluation_out
        )
        step = StepSpec(
            name="round_evaluation",
            connector="round_artifacts_to_evaluation",
            handler=lambda _context: self.evaluation.run_round(stage_results),
            input_roles=self._round_evaluation_input_roles(stage_results),
            output_roles=("evaluation_report",),
            metrics=lambda value: {
                "stage_count": len(value.get("stage_names", [])),
                "case_count": sum(value.get("case_counts", {}).values()),
            },
            metadata=self._round_manifest_metadata(),
        )
        return self.orchestrator.run_step(step, self.context)

    def _artifacts(self) -> dict[str, Path]:
        artifacts = {"corpus": self._path_from_cwd(self.config.corpus)}
        if self.config.target_config is not None:
            artifacts["target_manifest"] = self._path_from_cwd(self.config.target_config)
        if self.config.directives is not None:
            artifacts["directives"] = self._path_from_cwd(self.config.directives)
        if self.config.coverage_dir is not None:
            artifacts["replay_artifacts"] = self._path_from_cwd(self.config.coverage_dir)
        if self.config.coverage_dat is not None:
            artifacts["rtl_coverage_dat"] = self._path_from_cwd(self.config.coverage_dat)
        if self.config.coverage_info is not None:
            artifacts["coverage_info"] = self._path_from_cwd(self.config.coverage_info)
        if self.config.coverage_annotated is not None:
            artifacts["coverage_annotated"] = self._path_from_cwd(
                self.config.coverage_annotated
            )
        if self.config.functional_coverage is not None:
            artifacts["functional_coverage"] = self._path_from_cwd(
                self.config.functional_coverage
            )
        feedback_functional_coverage = self._feedback_functional_coverage()
        if feedback_functional_coverage is not None:
            artifacts["feedback_functional_coverage"] = feedback_functional_coverage
        if self.config.summary_out is not None:
            artifacts["summary"] = self._path_from_cwd(self.config.summary_out)
        heuristic_directives = self._heuristic_directives()
        if heuristic_directives is not None:
            artifacts["heuristic_directives"] = heuristic_directives
        if self.config.prompt_out is not None:
            artifacts["prompt"] = self._path_from_cwd(self.config.prompt_out)
        if self.config.gap_feedback_out is not None:
            artifacts["gap_feedback"] = self._path_from_cwd(
                self.config.gap_feedback_out
            )
        if self.config.mutation_feedback_out is not None:
            artifacts["mutation_feedback"] = self._path_from_cwd(
                self.config.mutation_feedback_out
            )
        if self.config.llm_response_out is not None:
            artifacts["llm_response"] = self._path_from_cwd(
                self.config.llm_response_out
            )
        if self.config.feedback_corpus is not None:
            artifacts["feedback_corpus"] = self._path_from_cwd(
                self.config.feedback_corpus
            )
        if self.config.round_manifest_out is not None:
            artifacts["round_manifest"] = self._path_from_cwd(
                self.config.round_manifest_out
            )
        if self.config.evaluation_out is not None:
            artifacts["evaluation_report"] = self._path_from_cwd(
                self.config.evaluation_out
            )
        runtime_metrics = self._harness_runtime_metrics()
        if runtime_metrics is not None:
            artifacts["harness_runtime_metrics"] = runtime_metrics
        return artifacts

    def _generator_input_roles(self) -> tuple[str, ...]:
        roles = []
        if self.config.target_config is not None:
            roles.append("target_manifest")
        if self.config.directives is not None:
            roles.append("directives")
        return tuple(roles)

    def _feedback_generator_input_roles(self) -> tuple[str, ...]:
        roles = []
        if self.config.target_config is not None:
            roles.append("target_manifest")
        roles.append("directives")
        return tuple(roles)

    def _default_generator_command(self) -> tuple[str, ...]:
        return self.corpus_generator.default_command()

    def _feedback_generator_command(self) -> tuple[str, ...]:
        return self.corpus_generator.feedback_command()

    def _generator_command(
        self,
        *,
        corpus: Path,
        directives: Path | None,
    ) -> tuple[str, ...]:
        return self.corpus_generator.command(corpus=corpus, directives=directives)

    def _path_from_cwd(self, path: Path | None) -> Path | None:
        return self.paths.path_from_cwd(path)

    def _cargo_command(self) -> list[str]:
        return self.corpus_generator.cargo_command()

    def _make_command(self) -> list[str]:
        return self.uvm_replay.make_command()

    def _run_cwd(self) -> Path:
        return self.paths.run_cwd()

    def _run_coverage_replay(self) -> subprocess.CompletedProcess:
        return self.uvm_replay.run_coverage_replay()

    def _run_make_clean(self) -> None:
        self.uvm_replay.run_make_clean()

    def _remove_sim_build(self) -> None:
        self.uvm_replay.remove_sim_build()

    def _coverage_replay_command(self) -> list[str]:
        return self.uvm_replay.coverage_command()

    def _feedback_replay_command(self) -> list[str]:
        return self.uvm_replay.feedback_command()

    def _run_verilator_coverage_report(self) -> dict[str, subprocess.CompletedProcess]:
        return self.coverage_report.run()

    def _coverage_metadata(self) -> dict[str, str]:
        value = {
            "target": self.config.target,
            "coverage_dat": str(self._required_artifact("rtl_coverage_dat")),
        }
        if self.config.toplevel is not None:
            value["toplevel"] = self.config.toplevel
        return value

    def _require_coverage_paths(self) -> None:
        for role in (
            "corpus",
            "replay_artifacts",
            "rtl_coverage_dat",
            "coverage_info",
            "coverage_annotated",
        ):
            self._required_artifact(role)

    def _required_artifact(self, role: str) -> Path:
        return self.paths.required_artifact(role)

    def _make_value(self, path: Path | None) -> str:
        return self.paths.make_value(path)

    def _coverage_feedback_config(self) -> CoverageFeedbackConfig:
        self._require_feedback_paths()
        return CoverageFeedbackConfig(
            target=self.config.target,
            coverage_info=self._required_artifact("coverage_info"),
            coverage_dat=self._optional_artifact("rtl_coverage_dat"),
            functional_coverage=self._optional_artifact("functional_coverage"),
            ignore_functional_coverage=self.config.ignore_functional_coverage,
            corpus=self._required_artifact("corpus"),
            summary_out=self._path_from_cwd(self.config.summary_out),
            directives_out=self._path_from_cwd(self.config.directives_out),
            heuristic_directives_out=self._heuristic_directives(),
            prompt_out=self._path_from_cwd(self.config.prompt_out),
            previous_summary=self._path_from_cwd(self.config.previous_summary),
            previous_directives=self._path_from_cwd(self.config.previous_directives),
            previous_gap_feedback=self._path_from_cwd(self.config.previous_gap_feedback),
            previous_mutation_feedback=self._path_from_cwd(
                self.config.previous_mutation_feedback
            ),
            gap_feedback_out=self._path_from_cwd(self.config.gap_feedback_out),
            mutation_feedback_out=self._path_from_cwd(
                self.config.mutation_feedback_out
            ),
            llm=self.config.llm,
            llm_response_out=self._path_from_cwd(self.config.llm_response_out),
            model=self.config.llm_model,
            topology_out=self.config.topology_out,
            mode=self._mode(),
            coverage_feedback_tuning=self._harness_runtime_config(
                COVERAGE_FEEDBACK_TUNING_CONFIG_ENV
            ),
            runtime_metrics_out=self._harness_runtime_metrics(),
        )

    def _require_feedback_paths(self) -> None:
        required = {
            "coverage_info": self.config.coverage_info,
            "summary_out": self.config.summary_out,
            "directives_out": self.config.directives_out,
            "prompt_out": self.config.prompt_out,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "missing required artifact path(s) for coverage feedback: "
                + ", ".join(sorted(missing))
            )

    def _optional_artifact(self, role: str) -> Path | None:
        return self.paths.optional_artifact(role)

    def _require_feedback_replay_paths(self) -> None:
        required = {
            "directives_out": self.config.directives_out,
            "feedback_corpus": self.config.feedback_corpus,
            "coverage_dir": self.config.coverage_dir,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError(
                "missing required artifact path(s) for feedback replay: "
                + ", ".join(sorted(missing))
            )
        self._required_artifact("replay_artifacts")

    def _feedback_corpus(self) -> Path:
        return self.paths.feedback_corpus()

    def _feedback_directives(self) -> Path:
        return self.paths.feedback_directives()

    def _feedback_replay_metadata(self) -> dict[str, str]:
        value = {
            "target": self.config.target,
            "feedback_corpus": str(self._feedback_corpus()),
            "directives": str(self._feedback_directives()),
            "mode": self._mode(),
        }
        feedback_functional_coverage = self._feedback_functional_coverage()
        if feedback_functional_coverage is not None:
            value["functional_coverage"] = str(feedback_functional_coverage)
        round_id = self._round_id()
        if round_id is not None:
            value["round_id"] = round_id
        if self.config.toplevel is not None:
            value["toplevel"] = self.config.toplevel
        return value

    def _feedback_corpus_metadata(self) -> dict[str, str]:
        value = {
            "target": self.config.target,
            "feedback_corpus": str(self._feedback_corpus()),
            "directives": str(self._feedback_directives()),
            "mode": self._mode(),
        }
        round_id = self._round_id()
        if round_id is not None:
            value["round_id"] = round_id
        return value

    def _context_metadata(self) -> dict[str, str]:
        value = {"target": self.config.target, "mode": self._mode()}
        round_id = self._round_id()
        if round_id is not None:
            value["round_id"] = round_id
        if self.config.run_plan_profile is not None:
            value["run_plan_profile"] = self.config.run_plan_profile
        return value

    def _mode(self) -> str:
        return self.config.mode or "feedback_fuzz"

    def _round_id(self) -> str | None:
        return self.config.round_id or self.observation_context.round_id

    def _observation_make_vars(self, *, stage_id: str) -> list[str]:
        return self.uvm_replay.observation_make_vars(stage_id=stage_id)

    def _round_manifest_metadata(self) -> dict[str, str]:
        value = {"target": self.config.target, "mode": self._mode()}
        round_id = self._round_id()
        if round_id is not None:
            value["round_id"] = round_id
        return value

    def _round_manifest_input_roles(
        self,
        stage_results: dict[str, object],
    ) -> tuple[str, ...]:
        roles = ["corpus"]
        if "coverage_feedback" in stage_results:
            roles.extend(["summary", "directives"])
        return tuple(role for role in roles if role in self.context.artifacts)

    def _round_evaluation_input_roles(
        self,
        stage_results: dict[str, object],
    ) -> tuple[str, ...]:
        roles = []
        if "round_manifest" in stage_results:
            roles.append("round_manifest")
        return tuple(role for role in roles if role in self.context.artifacts)

    def _write_round_manifest(self, stage_results: dict[str, object]) -> dict[str, Any]:
        manifest = self._round_manifest_payload(stage_results)
        path = self._required_artifact("round_manifest")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return manifest

    def _round_manifest_payload(self, stage_results: dict[str, object]) -> dict[str, Any]:
        feedback = stage_results.get("coverage_feedback")
        summary: dict[str, Any] = {}
        final_directives: dict[str, Any] = {}
        if isinstance(feedback, CoverageFeedbackResult):
            summary = feedback.summary
            final_directives = feedback.final_directives
        return {
            "schema_version": 1,
            "kind": "libafl_bfm_fuzz.round_manifest",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "target": self.config.target,
            "mode": self._mode(),
            "round_id": self._round_id(),
            "run_id": self.observation_context.run_id,
            "cwd": str(self._run_cwd()),
            "config": self._round_manifest_config(),
            "artifacts": self._round_manifest_artifacts(stage_results, feedback),
            "stages": {
                "corpus_generation": self._completed_process_json(
                    stage_results.get("corpus_generation")
                ),
                "corpus_validation": self._validation_json(
                    stage_results.get("corpus_validation")
                ),
                "coverage_run": self._completed_process_json(
                    stage_results.get("coverage_run")
                ),
                "coverage_report": {
                    "annotate": self._completed_process_json(stage_results.get("annotate")),
                    "write_info": self._completed_process_json(
                        stage_results.get("write_info")
                    ),
                },
                "coverage_feedback": self._coverage_feedback_json(feedback),
                "feedback_corpus_generation": self._completed_process_json(
                    stage_results.get("feedback_corpus_generation")
                ),
                "feedback_corpus_validation": self._validation_json(
                    stage_results.get("feedback_corpus_validation")
                ),
                "feedback_replay": self._completed_process_json(
                    stage_results.get("feedback_replay")
                ),
            },
            "coverage": self._coverage_snapshot(summary),
            "feedback": self._feedback_snapshot(feedback, final_directives),
        }

    def _round_manifest_config(self) -> dict[str, Any]:
        return {
            "iters": self.config.iters,
            "max_seeds": self.config.max_seeds,
            "seed": self.config.seed,
            "cargo": self.config.cargo,
            "make": self.config.make,
            "verilator_coverage": self.config.verilator_coverage,
            "toplevel": self.config.toplevel,
            "verilog_sources": self.config.verilog_sources,
            "extra_make_vars": list(self.config.extra_make_vars),
            "llm": self.config.llm,
            "llm_model": self.config.llm_model,
            "ignore_functional_coverage": self.config.ignore_functional_coverage,
            "run_plan_profile": self.config.run_plan_profile,
            "evaluation_out": self._manifest_path(self.config.evaluation_out),
        }

    def _round_manifest_artifacts(
        self,
        stage_results: dict[str, object],
        feedback: object,
    ) -> dict[str, str]:
        paths = {
            "target_manifest": self.config.target_config,
            "libafl_manifest": self.config.libafl_manifest,
            "input_corpus": self.config.corpus,
            "input_directives": self.config.directives,
            "coverage_dir": self.config.coverage_dir,
            "coverage_dat": self.config.coverage_dat,
            "coverage_info": self.config.coverage_info,
            "coverage_annotated": self.config.coverage_annotated,
            "round_manifest": self.config.round_manifest_out,
            "observation_events": self.config.observation_out,
            "monitoring": self.config.monitoring_out,
            "topology": self.config.topology_out,
        }
        artifacts = {
            name: self._manifest_path(path)
            for name, path in paths.items()
            if path is not None
        }
        materialized_paths = {
            "functional_coverage": self.config.functional_coverage,
            "harness_runtime_metrics": self._harness_runtime_metrics(),
        }
        if "feedback_corpus_generation" in stage_results:
            materialized_paths["feedback_corpus"] = self.config.feedback_corpus
        if "feedback_replay" in stage_results:
            materialized_paths[
                "feedback_functional_coverage"
            ] = self._feedback_functional_coverage()
        if isinstance(feedback, CoverageFeedbackResult):
            materialized_paths.update(
                {
                    "coverage_summary": self.config.summary_out,
                    "heuristic_mutation_directives": self._heuristic_directives(),
                    "mutation_directives": self.config.directives_out,
                    "llm_prompt": self.config.prompt_out,
                    "llm_response": self.config.llm_response_out,
                }
            )
            if feedback.gap_feedback is not None:
                materialized_paths["gap_feedback"] = self.config.gap_feedback_out
            if feedback.mutation_feedback is not None:
                materialized_paths[
                    "mutation_feedback"
                ] = self.config.mutation_feedback_out
        for name, path in materialized_paths.items():
            if path is not None and self._artifact_exists(path):
                artifacts[name] = self._manifest_path(path)
        return artifacts

    def _feedback_functional_coverage(self) -> Path | None:
        return self.paths.feedback_functional_coverage()

    def _harness_runtime_config(self, env_name: str) -> Path | None:
        value = extra_make_var_value(self.config.extra_make_vars, env_name)
        return self._path_from_cwd(Path(value)) if value is not None else None

    def _harness_runtime_metrics(self) -> Path | None:
        value = extra_make_var_value(self.config.extra_make_vars, RUNTIME_METRICS_OUT_ENV)
        return self._path_from_cwd(Path(value)) if value is not None else None

    def _heuristic_directives(self) -> Path | None:
        return self.paths.heuristic_directives()

    def _manifest_path(self, path: Path | None) -> str | None:
        return self.paths.manifest_path(path)

    def _artifact_exists(self, path: Path | None) -> bool:
        return self.paths.artifact_exists(path)

    def _completed_process_json(self, result: object) -> dict[str, Any] | None:
        if not isinstance(result, subprocess.CompletedProcess):
            return None
        return {
            "returncode": int(result.returncode),
            "command": self._command_json(result.args),
        }

    def _validation_json(self, result: object) -> dict[str, Any] | None:
        if result is None:
            return None
        try:
            return {"case_count": len(result)}
        except TypeError:
            return None

    def _command_json(self, args: object) -> list[str] | str:
        if isinstance(args, (list, tuple)):
            return [str(item) for item in args]
        return str(args)

    def _coverage_feedback_json(self, result: object) -> dict[str, Any] | None:
        if not isinstance(result, CoverageFeedbackResult):
            return None
        final_directives = result.final_directives.get("directives", [])
        heuristic_directives = result.heuristic_directives.get("directives", [])
        return {
            "uncovered_line_count": result.summary.get("uncovered_line_count"),
            "functional_coverage_source": result.summary.get(
                "uvm_functional_coverage_source"
            ),
            "directive_count": len(final_directives),
            "heuristic_directive_count": len(heuristic_directives),
            "directive_source": result.final_directives.get("source"),
            "has_gap_feedback": result.gap_feedback is not None,
            "has_mutation_feedback": result.mutation_feedback is not None,
        }

    def _coverage_snapshot(self, summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "coverage_info": summary.get("coverage_info"),
            "coverage_dat": summary.get("coverage_dat"),
            "functional_coverage": summary.get("functional_coverage"),
            "functional_coverage_source": summary.get(
                "uvm_functional_coverage_source"
            ),
            "uncovered_line_count": summary.get("uncovered_line_count"),
            "rtl_gap_summary": summary.get("rtl_gap_summary", {}),
            "uvm_functional_coverage": summary.get("uvm_functional_coverage", {}),
        }

    def _feedback_snapshot(
        self,
        result: object,
        final_directives: dict[str, Any],
    ) -> dict[str, Any]:
        directives = final_directives.get("directives", [])
        value = {
            "directive_count": len(directives),
            "directive_source": final_directives.get("source"),
            "directive_origins": sorted(
                {
                    str(item.get("origin"))
                    for item in directives
                    if isinstance(item, dict) and item.get("origin") is not None
                }
            ),
        }
        if isinstance(result, CoverageFeedbackResult):
            value["has_gap_feedback"] = result.gap_feedback is not None
            value["has_mutation_feedback"] = result.mutation_feedback is not None
        return value


def run_generate_corpus_pipeline(
    config: FuzzRunConfig,
    observation_context: ObservationContext | None = None,
):
    return FuzzRunOrchestrator(config, observation_context).generate_and_validate()


def run_coverage_report_pipeline(
    config: FuzzRunConfig,
    observation_context: ObservationContext | None = None,
):
    return FuzzRunOrchestrator(config, observation_context).coverage_run_and_report()


def run_coverage_feedback_stage_pipeline(
    config: FuzzRunConfig,
    observation_context: ObservationContext | None = None,
):
    return FuzzRunOrchestrator(config, observation_context).coverage_feedback()


def run_feedback_fuzz_pipeline(
    config: FuzzRunConfig,
    observation_context: ObservationContext | None = None,
):
    orchestrator = FuzzRunOrchestrator(config, observation_context)
    if config.mode == "no_feedback":
        return orchestrator.no_feedback_round()
    return orchestrator.feedback_fuzz()


def command_tuple(command: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(item) for item in command)
