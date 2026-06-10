from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any, Sequence

from connector_observe import ObservationContext
from fuzz_bfm.corpus import load_cases
from fuzz_bfm.target_config import load_target_config

from .coverage_feedback import (
    CoverageFeedbackConfig,
    CoverageFeedbackResult,
    run_coverage_feedback_pipeline,
)
from .harness import observation_context_from_env
from .orchestrator import (
    PipelineContext,
    PipelineOrchestrator,
    StepSpec,
    external_command_step,
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
    ):
        self.config = config
        self.observation_context = observation_context or observation_context_from_env()
        self.context = PipelineContext(
            run_id=self.observation_context.run_id,
            artifacts=self._artifacts(),
            metadata=self._context_metadata(),
        )
        self.orchestrator = PipelineOrchestrator(
            topology,
            self.observation_context,
            topology_out=config.topology_out,
        )

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
        self.orchestrator.write_topology()
        self.generate_corpus()
        return self.validate_corpus()

    def coverage_run_pipeline(self) -> dict[str, object]:
        self.orchestrator.write_topology()
        corpus_generation = self.generate_corpus()
        corpus_validation = self.validate_corpus()
        replay = self.coverage_run()
        return {
            "corpus_generation": corpus_generation,
            "corpus_validation": corpus_validation,
            "coverage_run": replay,
        }

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
        self.orchestrator.write_topology()
        corpus_generation = self.generate_corpus()
        corpus_validation = self.validate_corpus()
        replay = self.coverage_run()
        report = self.generate_coverage_report()
        return {
            "corpus_generation": corpus_generation,
            "corpus_validation": corpus_validation,
            "coverage_run": replay,
            **report,
        }

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
        self.orchestrator.write_topology()
        corpus_generation = self.generate_corpus()
        corpus_validation = self.validate_corpus()
        coverage_replay = self.coverage_run()
        coverage_report = self.generate_coverage_report()
        feedback = self.coverage_feedback()
        feedback_corpus_generation = self.generate_feedback_corpus()
        feedback_corpus_validation = self.validate_feedback_corpus()
        feedback_replay = self.feedback_replay()
        results = {
            "corpus_generation": corpus_generation,
            "corpus_validation": corpus_validation,
            "coverage_run": coverage_replay,
            **coverage_report,
            "coverage_feedback": feedback,
            "feedback_corpus_generation": feedback_corpus_generation,
            "feedback_corpus_validation": feedback_corpus_validation,
            "feedback_replay": feedback_replay,
        }
        round_manifest = self.write_round_manifest(results)
        if round_manifest is not None:
            results["round_manifest"] = round_manifest
        return results

    def no_feedback_round(self) -> dict[str, object]:
        self.orchestrator.write_topology()
        corpus_generation = self.generate_corpus()
        corpus_validation = self.validate_corpus()
        coverage_replay = self.coverage_run()
        coverage_report = self.generate_coverage_report()
        results = {
            "corpus_generation": corpus_generation,
            "corpus_validation": corpus_validation,
            "coverage_run": coverage_replay,
            **coverage_report,
        }
        round_manifest = self.write_round_manifest(results)
        if round_manifest is not None:
            results["round_manifest"] = round_manifest
        return results

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
        return self._generator_command(
            corpus=self.config.corpus,
            directives=self.config.directives,
        )

    def _feedback_generator_command(self) -> tuple[str, ...]:
        return self._generator_command(
            corpus=self._feedback_corpus(),
            directives=self._feedback_directives(),
        )

    def _generator_command(
        self,
        *,
        corpus: Path,
        directives: Path | None,
    ) -> tuple[str, ...]:
        command: list[str] = [
            *self._cargo_command(),
            "run",
            "--quiet",
            "--manifest-path",
            str(self._path_from_cwd(self.config.libafl_manifest)),
            "--",
            "--target",
            self.config.target,
        ]
        if self.config.target_config is not None:
            command.extend(
                ["--target-config", str(self._path_from_cwd(self.config.target_config))]
            )
        command.extend(
            [
                "--corpus-out",
                str(self._path_from_cwd(corpus)),
                "--iters",
                str(self.config.iters),
                "--max-seeds",
                str(self.config.max_seeds),
                "--seed",
                str(self.config.seed),
            ]
        )
        if directives is not None:
            command.extend(["--directives", str(self._path_from_cwd(directives))])
        return tuple(command)

    def _path_from_cwd(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        path = Path(path)
        if path.is_absolute() or self.config.cwd is None:
            return path
        cwd = self.config.cwd
        base = cwd if cwd.is_absolute() else Path.cwd() / cwd
        return base / path

    def _cargo_command(self) -> list[str]:
        return shlex.split(self.config.cargo) or ["cargo"]

    def _make_command(self) -> list[str]:
        return shlex.split(self.config.make) or ["make"]

    def _run_cwd(self) -> Path:
        if self.config.cwd is None:
            return Path.cwd()
        cwd = Path(self.config.cwd)
        return cwd if cwd.is_absolute() else Path.cwd() / cwd

    def _run_coverage_replay(self) -> subprocess.CompletedProcess:
        coverage_dir = self._required_artifact("replay_artifacts")
        coverage_dir.mkdir(parents=True, exist_ok=True)
        self._run_make_clean()
        self._remove_sim_build()
        return subprocess.run(
            self._coverage_replay_command(),
            cwd=str(self._run_cwd()),
            check=True,
        )

    def _run_make_clean(self) -> None:
        subprocess.run(
            [*self._make_command(), "-C", str(self._run_cwd()), "clean"],
            check=True,
        )

    def _remove_sim_build(self) -> None:
        shutil.rmtree(self._run_cwd() / "sim_build", ignore_errors=True)

    def _coverage_replay_command(self) -> list[str]:
        command = [
            *self._make_command(),
            "-C",
            str(self._run_cwd()),
            f"TARGET={self.config.target}",
            f"TARGET_CONFIG={self._make_value(self.config.target_config)}",
            "RTL_COVERAGE=1",
            f"COVERAGE_DIR={self._required_artifact('replay_artifacts')}",
            f"COVERAGE_DAT={self._required_artifact('rtl_coverage_dat')}",
            f"FUZZ_CORPUS={self._required_artifact('corpus')}",
            f"LIBAFL_MANIFEST={self._make_value(self.config.libafl_manifest)}",
            f"LIBAFL_ITERS={self.config.iters}",
            f"LIBAFL_MAX_SEEDS={self.config.max_seeds}",
            f"LIBAFL_SEED={self.config.seed}",
            f"CARGO={self.config.cargo}",
        ]
        if self.config.verilog_sources is not None:
            command.append(f"VERILOG_SOURCES={self.config.verilog_sources}")
        if self.config.toplevel is not None:
            command.append(f"TOPLEVEL={self.config.toplevel}")
        if self.config.directives is not None:
            command.append(f"FUZZ_DIRECTIVES={self._required_artifact('directives')}")
        if self.config.functional_coverage is not None:
            command.append(
                f"UVM_FUNCTIONAL_COVERAGE_OUT={self._required_artifact('functional_coverage')}"
            )
        command.extend(self._observation_make_vars(stage_id="coverage_run"))
        command.extend(self.config.extra_make_vars)
        command.append("sim")
        return command

    def _feedback_replay_command(self) -> list[str]:
        command = [
            *self._make_command(),
            "-C",
            str(self._run_cwd()),
            f"TARGET={self.config.target}",
            f"TARGET_CONFIG={self._make_value(self.config.target_config)}",
            f"COVERAGE_DIR={self._required_artifact('replay_artifacts')}",
            f"FUZZ_DIRECTIVES={self._feedback_directives()}",
            f"FUZZ_CORPUS={self._feedback_corpus()}",
            f"LIBAFL_MANIFEST={self._make_value(self.config.libafl_manifest)}",
            f"LIBAFL_ITERS={self.config.iters}",
            f"LIBAFL_MAX_SEEDS={self.config.max_seeds}",
            f"LIBAFL_SEED={self.config.seed}",
            f"CARGO={self.config.cargo}",
        ]
        if self.config.verilog_sources is not None:
            command.append(f"VERILOG_SOURCES={self.config.verilog_sources}")
        if self.config.toplevel is not None:
            command.append(f"TOPLEVEL={self.config.toplevel}")
        feedback_functional_coverage = self._feedback_functional_coverage()
        if feedback_functional_coverage is not None:
            command.append(
                "UVM_FUNCTIONAL_COVERAGE_OUT="
                f"{self._required_artifact('feedback_functional_coverage')}"
            )
        command.extend(self._observation_make_vars(stage_id="feedback_replay"))
        command.extend(self.config.extra_make_vars)
        command.append("sim")
        return command

    def _run_verilator_coverage_report(self) -> dict[str, subprocess.CompletedProcess]:
        coverage_annotated = self._required_artifact("coverage_annotated")
        coverage_annotated.mkdir(parents=True, exist_ok=True)
        coverage_dat = self._required_artifact("rtl_coverage_dat")
        coverage_info = self._required_artifact("coverage_info")
        annotate = subprocess.run(
            [
                self.config.verilator_coverage,
                "--annotate",
                str(coverage_annotated),
                str(coverage_dat),
            ],
            check=True,
        )
        write_info = subprocess.run(
            [
                self.config.verilator_coverage,
                "--write-info",
                str(coverage_info),
                str(coverage_dat),
            ],
            check=True,
        )
        return {"annotate": annotate, "write_info": write_info}

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
        try:
            path = self.context.artifacts[role]
        except KeyError as exc:
            raise ValueError(f"missing required artifact role: {role}") from exc
        if path is None:
            raise ValueError(f"missing required artifact role: {role}")
        return path

    def _make_value(self, path: Path | None) -> str:
        resolved = self._path_from_cwd(path)
        return str(resolved) if resolved is not None else ""

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
        return self.context.artifacts.get(role)

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
        path = self._path_from_cwd(self.config.feedback_corpus)
        if path is None:
            raise ValueError(
                "missing required artifact path for feedback replay: feedback_corpus"
            )
        return path

    def _feedback_directives(self) -> Path:
        path = self._path_from_cwd(self.config.directives_out)
        if path is None:
            raise ValueError(
                "missing required artifact path for feedback replay: directives_out"
            )
        return path

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
        return value

    def _mode(self) -> str:
        return self.config.mode or "feedback_fuzz"

    def _round_id(self) -> str | None:
        return self.config.round_id or self.observation_context.round_id

    def _observation_make_vars(self, *, stage_id: str) -> list[str]:
        values: list[str] = []
        if self.config.observation_out is not None:
            values.append(
                f"CONNECTOR_OBSERVE_OUT={self._path_from_cwd(self.config.observation_out)}"
            )
        if self.config.monitoring_out is not None:
            values.append(
                f"CONNECTOR_MONITOR_OUT={self._path_from_cwd(self.config.monitoring_out)}"
            )
        if self.config.topology_out is not None:
            values.append(
                f"CONNECTOR_TOPOLOGY_OUT={self._path_from_cwd(self.config.topology_out)}"
            )
        if self.observation_context.run_id is not None:
            values.append(f"CONNECTOR_OBSERVE_RUN_ID={self.observation_context.run_id}")
        round_id = self._round_id()
        if round_id is not None:
            values.append(f"CONNECTOR_OBSERVE_ROUND_ID={round_id}")
        values.append(
            f"CONNECTOR_OBSERVE_STAGE_ID={self.observation_context.stage_id or stage_id}"
        )
        if self.observation_context.parent_event_id is not None:
            values.append(
                f"CONNECTOR_OBSERVE_PARENT_EVENT_ID={self.observation_context.parent_event_id}"
            )
        return values

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
        if self.config.feedback_functional_coverage is not None:
            return self._path_from_cwd(self.config.feedback_functional_coverage)
        if self.config.functional_coverage is None or self.config.feedback_corpus is None:
            return None
        functional_coverage = self._path_from_cwd(self.config.functional_coverage)
        if functional_coverage is None:
            return None
        return functional_coverage.with_name(
            f"{functional_coverage.stem}_feedback{functional_coverage.suffix}"
        )

    def _heuristic_directives(self) -> Path | None:
        if self.config.heuristic_directives_out is not None:
            return self._path_from_cwd(self.config.heuristic_directives_out)
        if self.config.directives_out is None:
            return None
        directives = self._path_from_cwd(self.config.directives_out)
        if directives is None:
            return None
        return directives.with_name(f"{directives.stem}_heuristic{directives.suffix}")

    def _manifest_path(self, path: Path | None) -> str | None:
        resolved = self._path_from_cwd(path)
        return str(resolved) if resolved is not None else None

    def _artifact_exists(self, path: Path | None) -> bool:
        resolved = self._path_from_cwd(path)
        return resolved.exists() if resolved is not None else False

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
