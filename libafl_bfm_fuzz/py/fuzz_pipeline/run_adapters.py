from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Protocol

from ConnectGraph import ObservationContext, observation_make_vars
from harness_optimization.paths import FuzzRunConfigView, RunPathResolver, split_tool_command


class CorpusGeneratorBackend(Protocol):
    def default_command(self) -> tuple[str, ...]:
        ...

    def feedback_command(self) -> tuple[str, ...]:
        ...

    def command(
        self,
        *,
        corpus: Path,
        directives: Path | None,
    ) -> tuple[str, ...]:
        ...

    def cargo_command(self) -> list[str]:
        ...


class ReplayBackend(Protocol):
    def run_coverage_replay(self) -> subprocess.CompletedProcess:
        ...

    def run_make_clean(self) -> None:
        ...

    def remove_sim_build(self) -> None:
        ...

    def coverage_command(self) -> list[str]:
        ...

    def feedback_command(self) -> list[str]:
        ...

    def make_command(self) -> list[str]:
        ...

    def observation_make_vars(self, *, stage_id: str) -> list[str]:
        ...


class CoverageReportBackend(Protocol):
    def run(self) -> dict[str, subprocess.CompletedProcess]:
        ...


@dataclass(frozen=True)
class RunBackends:
    corpus_generator: CorpusGeneratorBackend | None = None
    uvm_replay: ReplayBackend | None = None
    coverage_report: CoverageReportBackend | None = None
@dataclass(frozen=True)
class CorpusGeneratorAdapter:
    config: FuzzRunConfigView
    paths: RunPathResolver

    def default_command(self) -> tuple[str, ...]:
        return self.command(
            corpus=self.config.corpus,
            directives=self.config.directives,
        )

    def feedback_command(self) -> tuple[str, ...]:
        return self.command(
            corpus=self.paths.feedback_corpus(),
            directives=self.paths.feedback_directives(),
        )

    def command(
        self,
        *,
        corpus: Path,
        directives: Path | None,
    ) -> tuple[str, ...]:
        command: list[str] = [
            *self.cargo_command(),
            "run",
            "--quiet",
            "--manifest-path",
            str(self.paths.path_from_cwd(self.config.libafl_manifest)),
            "--",
            "--target",
            self.config.target,
        ]
        if self.config.target_config is not None:
            command.extend(
                [
                    "--target-config",
                    str(self.paths.path_from_cwd(self.config.target_config)),
                ]
            )
        command.extend(
            [
                "--corpus-out",
                str(self.paths.path_from_cwd(corpus)),
                "--iters",
                str(self.config.iters),
                "--max-seeds",
                str(self.config.max_seeds),
                "--seed",
                str(self.config.seed),
            ]
        )
        if directives is not None:
            command.extend(["--directives", str(self.paths.path_from_cwd(directives))])
        return tuple(command)

    def cargo_command(self) -> list[str]:
        return split_tool_command(self.config.cargo, "cargo")


@dataclass(frozen=True)
class UvmReplayAdapter:
    config: FuzzRunConfigView
    paths: RunPathResolver
    observation_context: ObservationContext
    round_id: Callable[[], str | None]

    def run_coverage_replay(self) -> subprocess.CompletedProcess:
        coverage_dir = self.paths.required_artifact("replay_artifacts")
        coverage_dir.mkdir(parents=True, exist_ok=True)
        self.run_make_clean()
        self.remove_sim_build()
        return subprocess.run(
            self.coverage_command(),
            cwd=str(self.paths.run_cwd()),
            check=True,
        )

    def run_make_clean(self) -> None:
        subprocess.run(
            [*self.make_command(), "-C", str(self.paths.run_cwd()), "clean"],
            check=True,
        )

    def remove_sim_build(self) -> None:
        shutil.rmtree(self.paths.run_cwd() / "sim_build", ignore_errors=True)

    def coverage_command(self) -> list[str]:
        command = [
            *self.make_command(),
            "-C",
            str(self.paths.run_cwd()),
            f"TARGET={self.config.target}",
            f"TARGET_CONFIG={self.paths.make_value(self.config.target_config)}",
            "RTL_COVERAGE=1",
            f"COVERAGE_DIR={self.paths.required_artifact('replay_artifacts')}",
            f"COVERAGE_DAT={self.paths.required_artifact('rtl_coverage_dat')}",
            f"FUZZ_CORPUS={self.paths.required_artifact('corpus')}",
            f"LIBAFL_MANIFEST={self.paths.make_value(self.config.libafl_manifest)}",
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
            command.append(
                f"FUZZ_DIRECTIVES={self.paths.required_artifact('directives')}"
            )
        if self.config.functional_coverage is not None:
            command.append(
                "UVM_FUNCTIONAL_COVERAGE_OUT="
                f"{self.paths.required_artifact('functional_coverage')}"
            )
        command.extend(self.observation_make_vars(stage_id="coverage_run"))
        command.extend(self.config.extra_make_vars)
        command.append("sim")
        return command

    def feedback_command(self) -> list[str]:
        command = [
            *self.make_command(),
            "-C",
            str(self.paths.run_cwd()),
            f"TARGET={self.config.target}",
            f"TARGET_CONFIG={self.paths.make_value(self.config.target_config)}",
            f"COVERAGE_DIR={self.paths.required_artifact('replay_artifacts')}",
            f"FUZZ_DIRECTIVES={self.paths.feedback_directives()}",
            f"FUZZ_CORPUS={self.paths.feedback_corpus()}",
            f"LIBAFL_MANIFEST={self.paths.make_value(self.config.libafl_manifest)}",
            f"LIBAFL_ITERS={self.config.iters}",
            f"LIBAFL_MAX_SEEDS={self.config.max_seeds}",
            f"LIBAFL_SEED={self.config.seed}",
            f"CARGO={self.config.cargo}",
        ]
        if self.config.verilog_sources is not None:
            command.append(f"VERILOG_SOURCES={self.config.verilog_sources}")
        if self.config.toplevel is not None:
            command.append(f"TOPLEVEL={self.config.toplevel}")
        feedback_functional_coverage = self.paths.feedback_functional_coverage()
        if feedback_functional_coverage is not None:
            command.append(
                "UVM_FUNCTIONAL_COVERAGE_OUT="
                f"{self.paths.required_artifact('feedback_functional_coverage')}"
            )
        command.extend(self.observation_make_vars(stage_id="feedback_replay"))
        command.extend(self.config.extra_make_vars)
        command.append("sim")
        return command

    def make_command(self) -> list[str]:
        return split_tool_command(self.config.make, "make")

    def observation_make_vars(self, *, stage_id: str) -> list[str]:
        return observation_make_vars(
            observation_out=self.paths.path_from_cwd(self.config.observation_out),
            monitoring_out=self.paths.path_from_cwd(self.config.monitoring_out),
            topology_out=self.paths.path_from_cwd(self.config.topology_out),
            observation_context=self.observation_context,
            stage_id=self.observation_context.stage_id or stage_id,
            run_id=self.observation_context.run_id,
            round_id=self.round_id(),
            parent_event_id=self.observation_context.parent_event_id,
        )


@dataclass(frozen=True)
class CoverageReportAdapter:
    config: FuzzRunConfigView
    paths: RunPathResolver

    def run(self) -> dict[str, subprocess.CompletedProcess]:
        coverage_annotated = self.paths.required_artifact("coverage_annotated")
        coverage_annotated.mkdir(parents=True, exist_ok=True)
        coverage_dat = self.paths.required_artifact("rtl_coverage_dat")
        coverage_info = self.paths.required_artifact("coverage_info")
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
