from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
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

    def _artifacts(self) -> dict[str, Path]:
        artifacts = {"corpus": self._path_from_cwd(self.config.corpus)}
        if self.config.target_config is not None:
            artifacts["target_manifest"] = self._path_from_cwd(self.config.target_config)
        if self.config.directives is not None:
            artifacts["directives"] = self._path_from_cwd(self.config.directives)
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


def run_generate_corpus_pipeline(
    config: FuzzRunConfig,
    observation_context: ObservationContext | None = None,
):
    return FuzzRunOrchestrator(config, observation_context).generate_and_validate()


def command_tuple(command: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(item) for item in command)
