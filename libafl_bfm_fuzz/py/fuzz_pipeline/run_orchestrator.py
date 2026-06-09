from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Sequence

from connector_observe import ObservationContext
from fuzz_bfm.corpus import load_cases
from fuzz_bfm.target_config import load_target_config

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
    verilator_coverage: str = "verilator_coverage"
    extra_make_vars: tuple[str, ...] = ()


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
            metadata={"target": config.target},
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
        step = StepSpec(
            name="corpus_to_validation",
            connector="corpus_to_validation",
            handler=lambda _context: load_cases(
                self._path_from_cwd(self.config.corpus),
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

    def coverage_run_and_report(self) -> dict[str, subprocess.CompletedProcess]:
        self.orchestrator.write_topology()
        replay = self.coverage_run()
        report = self.generate_coverage_report()
        return {"coverage_run": replay, **report}

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
        return artifacts

    def _generator_input_roles(self) -> tuple[str, ...]:
        roles = []
        if self.config.target_config is not None:
            roles.append("target_manifest")
        if self.config.directives is not None:
            roles.append("directives")
        return tuple(roles)

    def _default_generator_command(self) -> tuple[str, ...]:
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
                str(self._path_from_cwd(self.config.corpus)),
                "--iters",
                str(self.config.iters),
                "--max-seeds",
                str(self.config.max_seeds),
                "--seed",
                str(self.config.seed),
            ]
        )
        if self.config.directives is not None:
            command.extend(
                ["--directives", str(self._path_from_cwd(self.config.directives))]
            )
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
            raise ValueError(f"missing required artifact role for coverage run: {role}") from exc
        if path is None:
            raise ValueError(f"missing required artifact role for coverage run: {role}")
        return path

    def _make_value(self, path: Path | None) -> str:
        resolved = self._path_from_cwd(path)
        return str(resolved) if resolved is not None else ""


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


def command_tuple(command: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(item) for item in command)
