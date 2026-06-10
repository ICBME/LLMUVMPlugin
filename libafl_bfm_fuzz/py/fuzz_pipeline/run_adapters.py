from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Callable, MutableMapping, Protocol

from connector_observe import ObservationContext


class FuzzRunConfigView(Protocol):
    target: str
    corpus: Path
    libafl_manifest: Path
    target_config: Path | None
    directives: Path | None
    iters: int
    max_seeds: int
    seed: int
    cargo: str
    cwd: Path | None
    topology_out: Path | None
    make: str
    verilog_sources: str | None
    toplevel: str | None
    functional_coverage: Path | None
    feedback_functional_coverage: Path | None
    verilator_coverage: str
    extra_make_vars: tuple[str, ...]
    directives_out: Path | None
    heuristic_directives_out: Path | None
    feedback_corpus: Path | None
    observation_out: Path | None
    monitoring_out: Path | None


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


def split_tool_command(value: str, default: str) -> list[str]:
    return shlex.split(value) or [default]


@dataclass
class RunPathResolver:
    config: FuzzRunConfigView
    artifacts: MutableMapping[str, Path] = field(default_factory=dict)

    def path_from_cwd(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        path = Path(path)
        if path.is_absolute() or self.config.cwd is None:
            return path
        cwd = self.config.cwd
        base = cwd if cwd.is_absolute() else Path.cwd() / cwd
        return base / path

    def run_cwd(self) -> Path:
        if self.config.cwd is None:
            return Path.cwd()
        cwd = Path(self.config.cwd)
        return cwd if cwd.is_absolute() else Path.cwd() / cwd

    def required_artifact(self, role: str) -> Path:
        try:
            path = self.artifacts[role]
        except KeyError as exc:
            raise ValueError(f"missing required artifact role: {role}") from exc
        if path is None:
            raise ValueError(f"missing required artifact role: {role}")
        return path

    def optional_artifact(self, role: str) -> Path | None:
        return self.artifacts.get(role)

    def make_value(self, path: Path | None) -> str:
        resolved = self.path_from_cwd(path)
        return str(resolved) if resolved is not None else ""

    def feedback_corpus(self) -> Path:
        path = self.path_from_cwd(self.config.feedback_corpus)
        if path is None:
            raise ValueError(
                "missing required artifact path for feedback replay: feedback_corpus"
            )
        return path

    def feedback_directives(self) -> Path:
        path = self.path_from_cwd(self.config.directives_out)
        if path is None:
            raise ValueError(
                "missing required artifact path for feedback replay: directives_out"
            )
        return path

    def feedback_functional_coverage(self) -> Path | None:
        if self.config.feedback_functional_coverage is not None:
            return self.path_from_cwd(self.config.feedback_functional_coverage)
        if self.config.functional_coverage is None or self.config.feedback_corpus is None:
            return None
        functional_coverage = self.path_from_cwd(self.config.functional_coverage)
        if functional_coverage is None:
            return None
        return functional_coverage.with_name(
            f"{functional_coverage.stem}_feedback{functional_coverage.suffix}"
        )

    def heuristic_directives(self) -> Path | None:
        if self.config.heuristic_directives_out is not None:
            return self.path_from_cwd(self.config.heuristic_directives_out)
        if self.config.directives_out is None:
            return None
        directives = self.path_from_cwd(self.config.directives_out)
        if directives is None:
            return None
        return directives.with_name(f"{directives.stem}_heuristic{directives.suffix}")

    def manifest_path(self, path: Path | None) -> str | None:
        resolved = self.path_from_cwd(path)
        return str(resolved) if resolved is not None else None

    def artifact_exists(self, path: Path | None) -> bool:
        resolved = self.path_from_cwd(path)
        return resolved.exists() if resolved is not None else False


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
        values: list[str] = []
        if self.config.observation_out is not None:
            values.append(
                "CONNECTOR_OBSERVE_OUT="
                f"{self.paths.path_from_cwd(self.config.observation_out)}"
            )
        if self.config.monitoring_out is not None:
            values.append(
                "CONNECTOR_MONITOR_OUT="
                f"{self.paths.path_from_cwd(self.config.monitoring_out)}"
            )
        if self.config.topology_out is not None:
            values.append(
                "CONNECTOR_TOPOLOGY_OUT="
                f"{self.paths.path_from_cwd(self.config.topology_out)}"
            )
        if self.observation_context.run_id is not None:
            values.append(f"CONNECTOR_OBSERVE_RUN_ID={self.observation_context.run_id}")
        round_id = self.round_id()
        if round_id is not None:
            values.append(f"CONNECTOR_OBSERVE_ROUND_ID={round_id}")
        values.append(
            f"CONNECTOR_OBSERVE_STAGE_ID={self.observation_context.stage_id or stage_id}"
        )
        if self.observation_context.parent_event_id is not None:
            values.append(
                "CONNECTOR_OBSERVE_PARENT_EVENT_ID="
                f"{self.observation_context.parent_event_id}"
            )
        return values


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
