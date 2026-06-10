from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from connector_observe import ObservationContext

from .harness import observation_context_from_env
from .orchestrator import PipelineContext, PipelineOrchestrator, StepSpec
from .run_orchestrator import FuzzRunConfig, FuzzRunOrchestrator
from .topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


MODE_ORDER = ("no_feedback", "heuristic_feedback", "llm_feedback")
FEEDBACK_MODES = {"heuristic_feedback", "llm_feedback"}


@dataclass(frozen=True)
class CampaignConfig:
    target: str
    out_dir: Path
    libafl_manifest: Path
    target_config: Path | None = None
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


class CampaignOrchestrator:
    """Multi-round campaign orchestration built from single-round fuzz runs."""

    def __init__(
        self,
        config: CampaignConfig,
        observation_context: ObservationContext | None = None,
        *,
        topology: PipelineTopology = FULL_FUZZ_TOPOLOGY,
    ):
        self.config = config
        self.observation_context = observation_context or observation_context_from_env()
        self.topology = topology
        self.context = PipelineContext(
            run_id=self.observation_context.run_id,
            artifacts={
                "round_manifest": self._out_dir(),
                "campaign_manifest": self._campaign_manifest_out(),
            },
            metadata={"target": config.target, "stage": "feedback_campaign"},
        )
        self.orchestrator = PipelineOrchestrator(
            topology,
            self.observation_context,
            topology_out=config.topology_out,
        )

    def run(self) -> dict[str, Any]:
        self._validate()
        self.orchestrator.write_topology()
        self._out_dir().mkdir(parents=True, exist_ok=True)

        mode_runs = []
        for mode in self.config.modes:
            mode_runs.append(self._run_mode(mode))

        return self._write_campaign_manifest(mode_runs)

    def _run_mode(self, mode: str) -> dict[str, Any]:
        rounds = []
        previous: RoundManifestState | None = None
        for index in range(self.config.rounds):
            artifacts = self._round_artifacts(mode, index)
            artifacts.run_dir.mkdir(parents=True, exist_ok=True)
            use_previous = previous if mode in FEEDBACK_MODES else None
            round_config = self._round_config(mode, artifacts, previous=use_previous)
            result = FuzzRunOrchestrator(
                round_config,
                self._round_context(artifacts.round_id),
                topology=self.topology,
            )
            if mode == "no_feedback":
                round_result = result.no_feedback_round()
            else:
                round_result = result.feedback_fuzz()
            manifest = self._round_manifest(round_result, artifacts.round_manifest)
            if mode == "llm_feedback" and self.config.require_real_llm:
                self._require_real_llm_source(manifest, artifacts.round_id)
            rounds.append(self._round_summary(artifacts, manifest, previous=use_previous))
            previous = (
                self._round_manifest_state(artifacts.round_manifest, manifest)
                if mode in FEEDBACK_MODES
                else None
            )
        return {"mode": mode, "rounds": rounds}

    def _round_config(
        self,
        mode: str,
        artifacts: CampaignRoundArtifacts,
        *,
        previous: RoundManifestState | None,
    ) -> FuzzRunConfig:
        feedback_mode = mode in FEEDBACK_MODES
        return FuzzRunConfig(
            target=self.config.target,
            target_config=self.config.target_config,
            corpus=artifacts.corpus,
            libafl_manifest=self.config.libafl_manifest,
            directives=(
                previous.require_artifact("mutation_directives")
                if previous is not None
                else None
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
            observation_out=self.config.observation_out,
            monitoring_out=self.config.monitoring_out,
        )

    def _round_summary(
        self,
        artifacts: CampaignRoundArtifacts,
        manifest: dict[str, Any],
        *,
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
            "coverage": manifest.get("coverage", {}),
            "feedback": manifest.get("feedback", {}),
            "stages": manifest.get("stages", {}),
        }

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
                "ignore_functional_coverage": self.config.ignore_functional_coverage,
                "llm_model": self.config.llm_model,
                "require_real_llm": self.config.require_real_llm,
            },
            "artifacts": {
                "out_dir": str(self._out_dir()),
                "campaign_manifest": str(self._campaign_manifest_out()),
                **self._optional_artifacts(),
            },
            "modes": mode_runs,
        }

    def _optional_artifacts(self) -> dict[str, str]:
        paths = {
            "target_manifest": self.config.target_config,
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

    def _round_artifacts(self, mode: str, index: int) -> CampaignRoundArtifacts:
        return CampaignRoundArtifacts(
            target=self.config.target,
            mode=mode,
            index=index,
            run_dir=self._out_dir() / mode / f"round_{index:02d}",
        )

    def _round_context(self, round_id: str) -> ObservationContext:
        return ObservationContext(
            run_id=self.observation_context.run_id,
            round_id=round_id,
            stage_id=self.observation_context.stage_id,
            parent_event_id=self.observation_context.parent_event_id,
            observer=self.observation_context.observer,
            strict=self.observation_context.strict,
        )

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
        path = Path(value)
        if path.is_absolute():
            return path
        cwd = manifest.get("cwd")
        if isinstance(cwd, str) and cwd:
            return Path(cwd) / path
        return manifest_path.parent / path

    def _optional_state_path(
        self,
        state: RoundManifestState | None,
        role: str,
    ) -> str | None:
        if state is None:
            return None
        path = state.artifacts.get(role)
        return str(path) if path is not None else None

    def _campaign_manifest_out(self) -> Path:
        path = (
            self.config.campaign_manifest_out
            or self.config.out_dir / "campaign_manifest.json"
        )
        resolved = self._path_from_cwd(path)
        if resolved is None:
            raise ValueError("missing campaign manifest path")
        return resolved

    def _out_dir(self) -> Path:
        resolved = self._path_from_cwd(self.config.out_dir)
        if resolved is None:
            raise ValueError("missing campaign output directory")
        return resolved

    def _path_from_cwd(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        path = Path(path)
        if path.is_absolute() or self.config.cwd is None:
            return path
        cwd = self.config.cwd
        base = cwd if cwd.is_absolute() else Path.cwd() / cwd
        return base / path

    def _run_cwd(self) -> Path:
        if self.config.cwd is None:
            return Path.cwd()
        cwd = Path(self.config.cwd)
        return cwd if cwd.is_absolute() else Path.cwd() / cwd

    def _validate(self) -> None:
        if self.config.rounds < 1:
            raise ValueError("campaign rounds must be >= 1")
        unknown = [mode for mode in self.config.modes if mode not in MODE_ORDER]
        if unknown:
            raise ValueError("unknown campaign mode(s): " + ", ".join(unknown))

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
) -> dict[str, Any]:
    return CampaignOrchestrator(config, observation_context).run()
