from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from fuzz_bfm.target_config import TargetConfig
from harness_optimization.io import path_sha256
from harness_optimization.observation import (
    NullObserver,
    ObservationContext,
    observation_context_from_env,
    topology_out_from_env,
)
from harness_optimization.orchestrator import (
    PipelineContext,
    PipelineOrchestrator,
    StepSpec,
)
from harness_optimization.runtime import (
    FUNCTIONAL_COVERAGE_OUT_ENV as LEGACY_FUNCTIONAL_COVERAGE_OUT_ENV,
    REPLAY_CORPUS_ENV as LEGACY_REPLAY_CORPUS_ENV,
    REPLAY_TARGET_CONFIG_ENV as LEGACY_REPLAY_TARGET_CONFIG_ENV,
    REPLAY_TARGET_ENV as LEGACY_REPLAY_TARGET_ENV,
    functional_coverage_output_from_target as _shared_functional_coverage_output_from_target,
    replay_corpus_from_env as _shared_replay_corpus_from_env,
    replay_target_from_env as _shared_replay_target_from_env,
)

from .harness_evidence.collection import (
    functional_coverage_metrics,
    replay_case_metadata,
    replay_context_metrics,
    replay_result_metrics,
    scoreboard_metrics,
)
from .generated_plugins import (
    GeneratedPluginBundle,
    GeneratedPluginRegistry,
    GeneratedPluginValidationReport,
    TargetManifestOverlay,
    apply_manifest_overlay,
    build_manifest_overlay,
    build_plugin_registry,
    generated_plugin_bundle_metrics,
    manifest_overlay_metrics,
    normalize_generated_plugin_bundle,
    normalize_manifest_overlay,
    normalize_plugin_registry,
    normalize_plugin_validation_report,
    plugin_registry_metrics,
    plugin_validation_report_metrics,
    validate_generated_plugin_bundle,
    write_generated_plugin_json,
)
from .topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


def replay_target_from_env() -> str:
    return _shared_replay_target_from_env(
        target_env=LEGACY_REPLAY_TARGET_ENV,
        target_config_env=LEGACY_REPLAY_TARGET_CONFIG_ENV,
    )


def replay_corpus_from_env(target: str | None = None) -> Path:
    return _shared_replay_corpus_from_env(
        target,
        target_env=LEGACY_REPLAY_TARGET_ENV,
        corpus_env=LEGACY_REPLAY_CORPUS_ENV,
    )


def functional_coverage_output_from_target(target_name: str) -> Path:
    return _shared_functional_coverage_output_from_target(
        target_name,
        env_var=LEGACY_FUNCTIONAL_COVERAGE_OUT_ENV,
    )


class ReplayPipelineOrchestrator:
    """Replay-specific orchestration facade for pyUVM component boundaries."""

    def __init__(
        self,
        *,
        observation_context: ObservationContext | None = None,
        pipeline_context: PipelineContext | None = None,
        topology: PipelineTopology = FULL_FUZZ_TOPOLOGY,
        topology_out: Path | None = None,
    ):
        self.observation_context = observation_context or observation_context_from_env()
        self.context = pipeline_context or PipelineContext(
            run_id=self.observation_context.run_id,
        )
        self.orchestrator = PipelineOrchestrator(
            topology,
            self.observation_context,
            topology_out=topology_out if topology_out is not None else topology_out_from_env(),
        )
        self.orchestrator.write_topology()
        if "corpus" in self.context.artifacts:
            self.set_corpus(self.context.artifacts["corpus"])

    @classmethod
    def from_env(
        cls,
        *,
        config: TargetConfig | None = None,
        corpus: Path | None = None,
    ) -> "ReplayPipelineOrchestrator":
        pipeline = cls()
        if config is not None:
            pipeline.set_target_config(config)
        if corpus is not None:
            pipeline.set_corpus(corpus)
        return pipeline

    def set_target_config(self, config: TargetConfig) -> None:
        self.context.metadata["target"] = config.name
        self.context.artifacts["target_manifest"] = config.path
        self.context.values["target_manifest"] = config

    def set_generated_artifact_bundle(
        self,
        bundle: GeneratedPluginBundle | dict[str, Any],
        *,
        path: Path | str | None = None,
    ) -> GeneratedPluginBundle:
        normalized = normalize_generated_plugin_bundle(bundle)
        self.context.values["generated_artifact_bundle"] = normalized
        self.context.metadata["target"] = normalized.target
        if path is not None:
            self.context.artifacts["generated_artifact_bundle"] = Path(path)
        return normalized

    def publish_generated_plugin_bundle(
        self,
        bundle: GeneratedPluginBundle | dict[str, Any],
        handler: Callable[[], Any] | None = None,
        *,
        bundle_path: Path | str | None = None,
    ) -> GeneratedPluginBundle:
        initial = self.set_generated_artifact_bundle(bundle, path=bundle_path)
        result = self.orchestrator.run_step(
            StepSpec(
                name="generated_artifact_bundle",
                connector="generation_context_to_artifact_bundle",
                handler=lambda _context: handler() if handler is not None else initial,
                metrics=generated_plugin_bundle_metrics,
                metadata={"target": initial.target},
            ),
            self.context,
        )
        normalized = self.set_generated_artifact_bundle(result, path=bundle_path)
        if bundle_path is not None:
            write_generated_plugin_json(bundle_path, normalized)
        return normalized

    def validate_generated_plugins(
        self,
        bundle: GeneratedPluginBundle | dict[str, Any] | None = None,
        *,
        config: TargetConfig | None = None,
        import_plugins: bool = True,
        require_contract_hash: bool = True,
        expected_contract_hash: str | None = None,
        handler: Callable[[], Any] | None = None,
        report_path: Path | str | None = None,
    ) -> GeneratedPluginValidationReport:
        if bundle is None:
            bundle = self.context.values["generated_artifact_bundle"]
        normalized_bundle = normalize_generated_plugin_bundle(bundle)
        if config is not None:
            self.set_target_config(config)
        result = self.orchestrator.run_step(
            StepSpec(
                name="plugin_validation_report",
                connector="artifact_bundle_to_contract_validation",
                handler=lambda _context: (
                    handler()
                    if handler is not None
                    else validate_generated_plugin_bundle(
                        normalized_bundle,
                        config=config,
                        import_plugins=import_plugins,
                        require_contract_hash=require_contract_hash,
                        expected_contract_hash=expected_contract_hash,
                    )
                ),
                metrics=plugin_validation_report_metrics,
                metadata={
                    "target": normalized_bundle.target,
                    "import_plugins": import_plugins,
                    "require_contract_hash": require_contract_hash,
                },
            ),
            self.context,
        )
        report = normalize_plugin_validation_report(result)
        self.context.values["plugin_validation_report"] = report
        if report_path is not None:
            self.context.artifacts["plugin_validation_report"] = Path(report_path)
            write_generated_plugin_json(report_path, report)
        return report

    def register_generated_plugins(
        self,
        report: GeneratedPluginValidationReport | dict[str, Any] | None = None,
        handler: Callable[[], Any] | None = None,
        *,
        registry_path: Path | str | None = None,
    ) -> GeneratedPluginRegistry:
        if report is None:
            report = self.context.values["plugin_validation_report"]
        validation_report = normalize_plugin_validation_report(report)
        result = self.orchestrator.run_step(
            StepSpec(
                name="plugin_registry",
                connector="contract_validation_to_plugin_registry",
                handler=lambda _context: (
                    handler()
                    if handler is not None
                    else build_plugin_registry(validation_report)
                ),
                metrics=plugin_registry_metrics,
                metadata={"target": validation_report.target},
            ),
            self.context,
        )
        registry = normalize_plugin_registry(result)
        self.context.values["plugin_registry"] = registry
        if registry_path is not None:
            self.context.artifacts["plugin_registry"] = Path(registry_path)
            write_generated_plugin_json(registry_path, registry)
        return registry

    def build_generated_manifest_overlay(
        self,
        registry: GeneratedPluginRegistry | dict[str, Any] | None = None,
        handler: Callable[[], Any] | None = None,
        *,
        overlay_path: Path | str | None = None,
    ) -> TargetManifestOverlay:
        if registry is None:
            registry = self.context.values["plugin_registry"]
        plugin_registry = normalize_plugin_registry(registry)
        result = self.orchestrator.run_step(
            StepSpec(
                name="target_manifest_overlay",
                connector="plugin_registry_to_manifest_overlay",
                handler=lambda _context: (
                    handler()
                    if handler is not None
                    else build_manifest_overlay(plugin_registry)
                ),
                metrics=manifest_overlay_metrics,
                metadata={"target": plugin_registry.target},
            ),
            self.context,
        )
        overlay = normalize_manifest_overlay(result)
        self.context.values["target_manifest_overlay"] = overlay
        if overlay_path is not None:
            self.context.artifacts["target_manifest_overlay"] = Path(overlay_path)
            write_generated_plugin_json(overlay_path, overlay)
        return overlay

    def activate_generated_manifest(
        self,
        config: TargetConfig,
        overlay: TargetManifestOverlay | dict[str, Any] | None = None,
        handler: Callable[[], Any] | None = None,
        *,
        active_manifest_path: Path | str | None = None,
    ) -> TargetConfig:
        self.set_target_config(config)
        if overlay is None:
            overlay = self.context.values["target_manifest_overlay"]
        manifest_overlay = normalize_manifest_overlay(overlay)
        result = self.orchestrator.run_step(
            StepSpec(
                name="target_manifest",
                connector="manifest_overlay_to_target_manifest",
                handler=lambda _context: (
                    handler()
                    if handler is not None
                    else apply_manifest_overlay(config, manifest_overlay)
                ),
                metrics=lambda value: {
                    "target": value.name,
                    "ref_model": value.ref_model or "",
                    "comparator": value.comparator or "",
                    "scoreboard": value.scoreboard or "",
                    "coverage_model": value.coverage_model or "",
                },
                metadata={"target": config.name},
            ),
            self.context,
        )
        active_config = result
        if active_manifest_path is not None:
            active_config = replace(active_config, path=Path(active_manifest_path))
        self.set_target_config(active_config)
        return active_config

    def set_corpus(self, corpus: Path | str) -> None:
        corpus_path = Path(corpus)
        self.context.artifacts["corpus"] = corpus_path
        self.context.metadata["corpus_path"] = str(corpus_path)
        if not self._observation_enabled():
            self.context.metadata.pop("corpus_sha256", None)
            return
        digest = path_sha256(corpus_path)
        if digest is not None:
            self.context.metadata["corpus_sha256"] = digest

    def _observation_enabled(self) -> bool:
        observer = self.observation_context.observer
        return observer is not None and not isinstance(observer, NullObserver)

    def load_replay_context(self, handler: Callable[[], Any], *, corpus: Path) -> Any:
        self.set_corpus(corpus)
        result = self.orchestrator.run_step(
            StepSpec(
                name="replay_context",
                connector="corpus_to_replay_context",
                handler=lambda _context: handler(),
                input_roles=("corpus",),
                output_roles=("corpus",),
                metrics=replay_context_metrics,
            ),
            self.context,
        )
        config = getattr(result, "config", None)
        if config is not None:
            self.set_target_config(config)
        loaded_corpus = getattr(result, "corpus", None)
        if loaded_corpus is not None:
            self.set_corpus(Path(loaded_corpus))
        return result

    def build_ref_model(self, config: TargetConfig, handler: Callable[[], Any]) -> Any:
        self.set_target_config(config)
        return self.orchestrator.run_step(
            StepSpec(
                name="ref_model",
                connector="manifest_to_ref_model",
                handler=lambda _context: handler(),
                input_roles=("target_manifest",),
                metrics=lambda value: {"available": value is not None},
                metadata={"ref_model": config.ref_model or ""},
            ),
            self.context,
        )

    def build_comparator(self, config: TargetConfig, handler: Callable[[], Any]) -> Any:
        self.set_target_config(config)
        return self.orchestrator.run_step(
            StepSpec(
                name="comparator",
                connector="manifest_to_comparator",
                handler=lambda _context: handler(),
                input_roles=("target_manifest",),
                metrics=lambda value: {
                    "available": value is not None,
                    "comparator": config.comparator or "default",
                },
                metadata={"comparator": config.comparator or "default"},
            ),
            self.context,
        )

    def build_replay_driver(self, config: TargetConfig, handler: Callable[[], Any]) -> Any:
        self.set_target_config(config)
        return self.orchestrator.run_step(
            StepSpec(
                name="replay_driver",
                connector="manifest_to_replay_driver",
                handler=lambda _context: handler(),
                input_roles=("target_manifest",),
                metrics=lambda _value: {"driver": config.driver},
            ),
            self.context,
        )

    def build_scoreboard(self, config: TargetConfig, handler: Callable[[], Any]) -> Any:
        self.set_target_config(config)
        return self.orchestrator.run_step(
            StepSpec(
                name="scoreboard",
                connector="manifest_to_scoreboard",
                handler=lambda _context: handler(),
                input_roles=("target_manifest",),
                metrics=lambda _value: {
                    "scoreboard": config.scoreboard or "default",
                    "comparator": config.comparator or "default",
                },
                metadata={
                    "scoreboard": config.scoreboard or "default",
                    "comparator": config.comparator or "default",
                },
            ),
            self.context,
        )

    def attach_comparator_to_scoreboard(
        self,
        handler: Callable[[], Any],
        *,
        comparator: str | None = None,
        scoreboard: str | None = None,
    ) -> Any:
        return self.orchestrator.run_step(
            StepSpec(
                name="scoreboard_comparator",
                connector="comparator_to_scoreboard",
                handler=lambda _context: handler(),
                metrics=lambda _value: {"attached": True},
                metadata={
                    "comparator": comparator or "default",
                    "scoreboard": scoreboard or "default",
                },
            ),
            self.context,
        )

    def build_functional_coverage(
        self,
        config: TargetConfig,
        handler: Callable[[], Any],
    ) -> Any:
        self.set_target_config(config)
        return self.orchestrator.run_step(
            StepSpec(
                name="functional_coverage",
                connector="manifest_to_functional_coverage",
                handler=lambda _context: handler(),
                input_roles=("target_manifest",),
                metrics=lambda _value: {
                    "coverage_model": config.coverage_model or "schema_default"
                },
            ),
            self.context,
        )

    def functional_coverage_output(self, config: TargetConfig) -> Path:
        output_path = functional_coverage_output_from_target(config.name)
        self.context.artifacts["functional_coverage"] = output_path
        return output_path

    async def start_sequence(
        self,
        handler: Callable[[], Any],
        *,
        corpus: Path,
        target: str,
        case_count: int,
    ) -> Any:
        self.context.metadata["target"] = target
        self.set_corpus(corpus)
        return await self.orchestrator.run_step_async(
            StepSpec(
                name="replay_sequence",
                connector="replay_context_to_sequence",
                handler=lambda _context: handler(),
                input_roles=("corpus",),
                metrics=lambda _value: {"case_count": case_count},
                metadata={"target": target},
                async_step=True,
            ),
            self.context,
        )

    async def send_case(self, handler: Callable[[], Any], case: Any, *, index: int) -> Any:
        return await self.orchestrator.run_step_async(
            StepSpec(
                name="send_case",
                connector="case_to_replay_driver",
                handler=lambda _context: handler(),
                metadata=replay_case_metadata(case, index=index),
                metrics=lambda _value: {"sent": True},
                async_step=True,
            ),
            self.context,
        )

    async def reset_driver(self, handler: Callable[[], Any]) -> Any:
        return await self.orchestrator.run_step_async(
            StepSpec(
                name="driver_reset",
                connector="driver_reset_to_dut",
                handler=lambda _context: handler(),
                metrics=lambda _value: {"operation": "reset"},
                async_step=True,
            ),
            self.context,
        )

    async def execute_case(self, handler: Callable[[], Any], case: Any, *, index: int) -> Any:
        return await self.orchestrator.run_step_async(
            StepSpec(
                name="dut_execute",
                connector="case_to_dut",
                handler=lambda _context: handler(),
                metadata=replay_case_metadata(case, index=index),
                metrics=replay_result_metrics,
                async_step=True,
            ),
            self.context,
        )

    def predict_ref_model(self, handler: Callable[[], Any], case: Any, *, index: int) -> Any:
        return self.orchestrator.run_step(
            StepSpec(
                name="ref_model_predict",
                connector="case_to_ref_model",
                handler=lambda _context: handler(),
                metadata=replay_case_metadata(case, index=index),
                metrics=lambda value: {
                    "has_expected": getattr(value, "expected", None) is not None
                },
            ),
            self.context,
        )

    def scoreboard_write(
        self,
        handler: Callable[[], Any],
        record: Any,
        *,
        summary: Callable[[], dict[str, Any]],
    ) -> Any:
        return self.orchestrator.run_step(
            StepSpec(
                name="scoreboard_write",
                connector="driver_to_scoreboard",
                handler=lambda _context: handler(),
                metadata=replay_case_metadata(record.case, index=record.index),
                metrics=lambda _value: scoreboard_metrics(summary()),
            ),
            self.context,
        )

    def scoreboard_check(self, handler: Callable[[], Any]) -> Any:
        return self.orchestrator.run_step(
            StepSpec(
                name="scoreboard_check",
                connector="scoreboard_to_report",
                handler=lambda _context: handler(),
            ),
            self.context,
        )

    def scoreboard_summary(self, handler: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        return self.orchestrator.run_step(
            StepSpec(
                name="scoreboard_summary",
                connector="scoreboard_to_report",
                handler=lambda _context: handler(),
                metrics=scoreboard_metrics,
            ),
            self.context,
        )

    def coverage_sample(
        self,
        handler: Callable[[], Any],
        record: Any,
        *,
        summary: Callable[[], dict[str, Any]],
    ) -> Any:
        return self.orchestrator.run_step(
            StepSpec(
                name="functional_coverage_sample",
                connector="driver_to_functional_coverage",
                handler=lambda _context: handler(),
                metadata=replay_case_metadata(record.case, index=record.index),
                metrics=lambda _value: functional_coverage_metrics(summary()),
            ),
            self.context,
        )

    def coverage_export(
        self,
        handler: Callable[[], dict[str, Any]],
        *,
        output_path: Path,
    ) -> dict[str, Any]:
        self.context.artifacts["functional_coverage"] = output_path
        return self.orchestrator.run_step(
            StepSpec(
                name="functional_coverage_summary",
                connector="functional_coverage_to_summary",
                handler=lambda _context: handler(),
                output_roles=("functional_coverage",),
                metrics=functional_coverage_metrics,
            ),
            self.context,
        )
