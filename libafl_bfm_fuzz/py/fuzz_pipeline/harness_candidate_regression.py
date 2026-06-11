from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from connector_observe import ObservationContext
from connector_observe.trace import read_json_object

from .campaign_orchestrator import CampaignConfig, CampaignOrchestrator
from .harness_optimization import (
    CANDIDATE_EVALUATION_KIND,
    baseline_metric_snapshot,
    list_value,
    metric_direction,
    number_value,
    safe_slug,
    utc_timestamp,
)
from .harness_records import mapping
from .harness_runtime_actions import (
    RUNTIME_METRICS_OUT_ENV,
    read_runtime_metrics,
    runtime_metrics_summary,
)
from .run_adapters import RunBackends
from .run_evaluation import EvaluationBackends
from .run_orchestrator import FuzzRunOrchestrator
from .run_profiles import RunPlanProfile
from .topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


CampaignOrchestratorFactory = Callable[..., CampaignOrchestrator]


ADAPTER_CONFIG_KINDS = {
    "mutation_directive_update": (
        "libafl_bfm_fuzz.harness_candidate_mutation_directive_updates",
        "candidate_mutation_directive_updates",
        "HARNESS_MUTATION_DIRECTIVE_UPDATE_CONFIG",
    ),
    "replay_probe": (
        "libafl_bfm_fuzz.harness_candidate_replay_probe_config",
        "candidate_replay_probe_config",
        "HARNESS_REPLAY_PROBE_CONFIG",
    ),
    "scoreboard_check": (
        "libafl_bfm_fuzz.harness_candidate_scoreboard_check_config",
        "candidate_scoreboard_check_config",
        "HARNESS_SCOREBOARD_CHECK_CONFIG",
    ),
    "coverage_feedback_tuning": (
        "libafl_bfm_fuzz.harness_candidate_coverage_feedback_tuning_config",
        "candidate_coverage_feedback_tuning_config",
        "HARNESS_COVERAGE_FEEDBACK_TUNING_CONFIG",
    ),
    "stimulus_generation_hint": (
        "libafl_bfm_fuzz.harness_candidate_stimulus_hint_config",
        "candidate_stimulus_hint_config",
        "HARNESS_STIMULUS_HINT_CONFIG",
    ),
    "documentation_note": (
        "libafl_bfm_fuzz.harness_candidate_documentation_notes",
        "candidate_documentation_notes",
        "HARNESS_DOCUMENTATION_NOTES",
    ),
}


@dataclass(frozen=True)
class CandidateAcceptanceThresholds:
    max_regressed_metric_count: int = 0
    min_improved_metric_count: int = 1
    accepted_candidate_statuses: tuple[str, ...] = ("passed", "ok")

    def to_json(self) -> dict[str, Any]:
        return {
            "max_regressed_metric_count": self.max_regressed_metric_count,
            "min_improved_metric_count": self.min_improved_metric_count,
            "accepted_candidate_statuses": list(self.accepted_candidate_statuses),
        }


@dataclass(frozen=True)
class CandidateRegressionSettings:
    modes: tuple[str, ...] | None = None
    rounds: int = 1
    iters: int | None = None
    max_seeds: int | None = None
    seed: int | None = None
    run_plan_profile: str | None = None
    round_evaluation: bool = True
    campaign_plan_profile: str = "campaign_with_evaluation"
    thresholds: CandidateAcceptanceThresholds = CandidateAcceptanceThresholds()


@dataclass(frozen=True)
class CandidateActionAdapterContext:
    candidate_id: str
    regression_dir: Path


@dataclass(frozen=True)
class CandidateActionAdapterResult:
    action_type: str
    artifact_role: str
    artifact_path: Path | None
    make_var: str | None
    entries: tuple[dict[str, Any], ...]
    directives: tuple[dict[str, Any], ...] = ()
    variants: tuple[dict[str, Any], ...] = ()
    metric_counts: Mapping[str, int] | None = None

    def artifact_json(self) -> dict[str, str]:
        if self.artifact_path is None:
            return {}
        return {self.artifact_role: str(self.artifact_path)}

    def make_var_assignment(self) -> str | None:
        if self.make_var is None or self.artifact_path is None:
            return None
        return f"{self.make_var}={self.artifact_path}"

    def metric_json(self) -> dict[str, int]:
        return dict(self.metric_counts or {})


class CandidateActionAdapter(Protocol):
    action_type: str

    def adapt(
        self,
        actions: tuple[dict[str, Any], ...],
        context: CandidateActionAdapterContext,
    ) -> CandidateActionAdapterResult:
        ...


@dataclass(frozen=True)
class JsonConfigActionAdapter:
    action_type: str

    def adapt(
        self,
        actions: tuple[dict[str, Any], ...],
        context: CandidateActionAdapterContext,
    ) -> CandidateActionAdapterResult:
        kind, artifact_role, make_var = adapter_config_for(self.action_type)
        entries = tuple(action_entry_for(action) for action in actions)
        artifact = {
            "schema_version": 1,
            "kind": kind,
            "created_at": utc_timestamp(),
            "candidate_id": context.candidate_id,
            "action_type": self.action_type,
            "entries": list(entries),
        }
        artifact_path = context.regression_dir / f"{artifact_role}.json"
        _write_json(artifact_path, artifact)
        directives = tuple(
            directive
            for entry in entries
            for directive in directives_from_action_entry(entry)
        )
        return CandidateActionAdapterResult(
            action_type=self.action_type,
            artifact_role=artifact_role,
            artifact_path=artifact_path,
            make_var=make_var,
            entries=entries,
            directives=directives,
            variants=tuple(
                action_variant(entry, artifact_path=artifact_path)
                for entry in entries
            ),
            metric_counts={f"{self.action_type}_count": len(entries)},
        )


@dataclass(frozen=True)
class HarnessCandidateRegressionBackend:
    settings: CandidateRegressionSettings = CandidateRegressionSettings()
    topology: PipelineTopology = FULL_FUZZ_TOPOLOGY
    run_backends: RunBackends | None = None
    run_plan_profiles: Mapping[str, RunPlanProfile] | None = None
    campaign_plan_profiles: Mapping[str, RunPlanProfile] | None = None
    evaluation_backends: EvaluationBackends | None = None
    action_adapters: Mapping[str, CandidateActionAdapter] | None = None
    run_orchestrator_factory: Callable[..., FuzzRunOrchestrator] = FuzzRunOrchestrator
    campaign_orchestrator_factory: CampaignOrchestratorFactory = CampaignOrchestrator
    observation_context: ObservationContext | None = None

    def run(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        baseline_metrics = baseline_metric_snapshot(task)
        sandbox_dir = _required_path(candidate_manifest.get("sandbox_dir"))
        regression_dir = sandbox_dir / "candidate_regression"
        regression_dir.mkdir(parents=True, exist_ok=True)
        baseline_manifest_path = _required_path(
            mapping(task.get("artifacts")).get("campaign_manifest")
            or mapping(task.get("sources")).get("campaign_manifest")
        )
        baseline_manifest = read_json_object(baseline_manifest_path)
        raw_actions = load_candidate_actions(candidate_manifest)
        adapter_context = CandidateActionAdapterContext(
            candidate_id=str(candidate_manifest.get("candidate_id") or "candidate"),
            regression_dir=regression_dir,
        )
        adapter_results = adapt_candidate_actions(
            raw_actions,
            adapter_context,
            adapters=self.action_adapters,
        )
        overlay = build_candidate_action_overlay(candidate_manifest, adapter_results)
        overlay_path = regression_dir / "candidate_action_overlay.json"
        _write_json(overlay_path, overlay)
        candidate_directives = build_candidate_directives(adapter_results)
        candidate_directives_path = (
            regression_dir / "candidate_mutation_directives.json"
            if candidate_directives["directives"]
            else None
        )
        if candidate_directives_path is not None:
            _write_json(candidate_directives_path, candidate_directives)
        runtime_metrics_path = regression_dir / "candidate_runtime_metrics.json"

        campaign_config = self._candidate_campaign_config(
            task=task,
            baseline_manifest=baseline_manifest,
            regression_dir=regression_dir,
            action_overlay=overlay_path,
            adapter_results=adapter_results,
            initial_directives=candidate_directives_path,
            runtime_metrics=runtime_metrics_path,
        )
        run_config_path = regression_dir / "candidate_regression_config.json"
        _write_json(
            run_config_path,
            candidate_regression_config_payload(
                campaign_config,
                action_overlay=overlay_path,
                adapter_results=adapter_results,
                initial_directives=candidate_directives_path,
                runtime_metrics=runtime_metrics_path,
            ),
        )
        if int(mapping(patch.get("summary")).get("applied_action_count", 0)) <= 0:
            return self._not_run_report(
                task=task,
                proposal=proposal,
                patch=patch,
                candidate_manifest=candidate_manifest,
                baseline_metrics=baseline_metrics,
                run_config_path=run_config_path,
                overlay_path=overlay_path,
                adapter_results=adapter_results,
                directives_path=candidate_directives_path,
                runtime_metrics_path=runtime_metrics_path,
                reason="no safe applied actions to validate",
            )

        try:
            candidate_campaign_manifest = self._run_candidate_campaign(
                campaign_config,
                task=task,
                proposal=proposal,
                patch=patch,
            )
        except Exception as exc:  # noqa: BLE001 - final decision rejects failed validation
            return self._error_report(
                task=task,
                proposal=proposal,
                baseline_metrics=baseline_metrics,
                run_config_path=run_config_path,
                overlay_path=overlay_path,
                adapter_results=adapter_results,
                directives_path=candidate_directives_path,
                runtime_metrics_path=runtime_metrics_path,
                error=exc,
            )

        candidate_evaluation = read_json_object(campaign_config.campaign_evaluation_out)
        candidate_metrics = candidate_metric_snapshot(
            candidate_campaign_manifest,
            candidate_evaluation,
            cwd=_campaign_cwd(candidate_campaign_manifest, campaign_config.cwd),
        )
        candidate_metrics.update(adapter_metric_snapshot(adapter_results))
        candidate_metrics.update(runtime_metric_snapshot(runtime_metrics_path))
        ranking = build_candidate_variant_ranking(
            action_overlay=overlay,
            baseline_metrics=baseline_metrics,
            candidate_metrics=candidate_metrics,
            selected_variant_id="combined",
        )
        ranking_path = regression_dir / "candidate_variant_ranking.json"
        _write_json(ranking_path, ranking)
        promotion = build_candidate_promotion_package(
            task=task,
            proposal=proposal,
            patch=patch,
            candidate_manifest=candidate_manifest,
            candidate_metrics=candidate_metrics,
            baseline_metrics=baseline_metrics,
            ranking=ranking,
            thresholds=self.settings.thresholds,
        )
        promotion_path = regression_dir / "candidate_promotion_package.json"
        _write_json(promotion_path, promotion)
        return {
            "schema_version": 1,
            "kind": CANDIDATE_EVALUATION_KIND,
            "created_at": utc_timestamp(),
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "candidate_id": candidate_manifest.get("candidate_id"),
            "source": type(self).__name__,
            "status": "passed",
            "application_status": patch.get("status"),
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "acceptance_thresholds": self.settings.thresholds.to_json(),
            "artifacts": {
                "candidate_regression_config": str(run_config_path),
                "candidate_action_overlay": str(overlay_path),
                **adapter_artifacts(adapter_results),
                **_optional_artifact(
                    "candidate_mutation_directives",
                    candidate_directives_path,
                ),
                **_optional_existing_artifact(
                    "candidate_runtime_metrics",
                    runtime_metrics_path,
                ),
                "candidate_variant_ranking": str(ranking_path),
                "candidate_promotion_package": str(promotion_path),
                "candidate_campaign_manifest": str(
                    campaign_config.campaign_manifest_out
                ),
                "candidate_campaign_evaluation": str(
                    campaign_config.campaign_evaluation_out
                ),
            },
            "summary": {
                "candidate_artifact_count": len(
                    list_value(candidate_manifest.get("candidate_artifacts"))
                ),
                "candidate_mode_count": len(
                    candidate_campaign_manifest.get("modes", [])
                ),
                "candidate_round_count": sum(
                    len(mode.get("rounds", []))
                    for mode in list_value(candidate_campaign_manifest.get("modes"))
                    if isinstance(mode, dict)
                ),
                "candidate_variant_count": len(ranking.get("variants", [])),
                "promotion_status": promotion.get("promotion_status"),
            },
        }

    def _candidate_campaign_config(
        self,
        *,
        task: dict[str, Any],
        baseline_manifest: dict[str, Any],
        regression_dir: Path,
        action_overlay: Path,
        adapter_results: tuple[CandidateActionAdapterResult, ...],
        initial_directives: Path | None,
        runtime_metrics: Path,
    ) -> CampaignConfig:
        baseline_config = mapping(baseline_manifest.get("config"))
        baseline_artifacts = mapping(baseline_manifest.get("artifacts"))
        cwd = _path_or_none(baseline_manifest.get("cwd"))
        modes = self.settings.modes or tuple(
            str(mode)
            for mode in list_value(baseline_manifest.get("mode_names"))
            if mode is not None
        )
        if not modes:
            modes = ("heuristic_feedback",)
        extra_make_vars = tuple(
            str(item)
            for item in list_value(baseline_config.get("extra_make_vars"))
            if item is not None
        )
        extra_make_vars = (
            *extra_make_vars,
            f"HARNESS_OPTIMIZATION_ACTION_OVERLAY={action_overlay}",
            f"{RUNTIME_METRICS_OUT_ENV}={runtime_metrics}",
            *adapter_make_vars(adapter_results),
        )
        return CampaignConfig(
            target=str(task.get("target") or baseline_manifest.get("target") or ""),
            target_config=_artifact_path(
                baseline_artifacts,
                "target_manifest",
                cwd,
            ),
            initial_directives=initial_directives,
            out_dir=regression_dir / "campaign",
            libafl_manifest=(
                _artifact_path(baseline_artifacts, "libafl_manifest", cwd)
                or _artifact_path(
                    mapping(task.get("artifacts")),
                    "libafl_manifest",
                    cwd,
                )
                or Path("Cargo.toml")
            ),
            modes=modes,
            rounds=self.settings.rounds,
            iters=_setting_int(self.settings.iters, baseline_config, "iters", 256),
            max_seeds=_setting_int(
                self.settings.max_seeds,
                baseline_config,
                "max_seeds",
                32,
            ),
            seed=_setting_int(self.settings.seed, baseline_config, "seed", 1),
            cargo=str(baseline_config.get("cargo") or "cargo"),
            make=str(baseline_config.get("make") or "make"),
            verilog_sources=_optional_str(baseline_config.get("verilog_sources")),
            toplevel=_optional_str(baseline_config.get("toplevel")),
            verilator_coverage=str(
                baseline_config.get("verilator_coverage") or "verilator_coverage"
            ),
            extra_make_vars=extra_make_vars,
            cwd=cwd,
            campaign_manifest_out=regression_dir / "candidate_campaign_manifest.json",
            run_plan_profile=self.settings.run_plan_profile
            or _optional_str(baseline_config.get("run_plan_profile")),
            campaign_plan_profile=self.settings.campaign_plan_profile,
            round_evaluation=self.settings.round_evaluation,
            campaign_evaluation_out=regression_dir
            / "candidate_campaign_evaluation.json",
            ignore_functional_coverage=bool(
                baseline_config.get("ignore_functional_coverage", False)
            ),
            llm_model=_optional_str(baseline_config.get("llm_model")),
            require_real_llm=False,
        )

    def _run_candidate_campaign(
        self,
        config: CampaignConfig,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        context = self.observation_context or ObservationContext(
            run_id=":".join(
                [
                    str(task.get("run_id") or "run"),
                    "candidate",
                    safe_slug(str(proposal.get("proposal_id") or "proposal")),
                ]
            ),
            parent_event_id=_optional_str(patch.get("candidate_id")),
        )
        campaign = self.campaign_orchestrator_factory(
            config,
            context,
            topology=self.topology,
            run_backends=self.run_backends,
            run_plan_profiles=self.run_plan_profiles,
            campaign_plan_profiles=self.campaign_plan_profiles,
            evaluation_backends=self.evaluation_backends or EvaluationBackends(),
            run_orchestrator_factory=self.run_orchestrator_factory,
        )
        return campaign.run()

    def _not_run_report(
        self,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
        baseline_metrics: dict[str, float | int],
        run_config_path: Path,
        overlay_path: Path,
        adapter_results: tuple[CandidateActionAdapterResult, ...],
        directives_path: Path | None,
        runtime_metrics_path: Path,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": CANDIDATE_EVALUATION_KIND,
            "created_at": utc_timestamp(),
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "candidate_id": candidate_manifest.get("candidate_id"),
            "source": type(self).__name__,
            "status": "not_run",
            "application_status": patch.get("status"),
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": dict(baseline_metrics),
            "acceptance_thresholds": self.settings.thresholds.to_json(),
            "reason": reason,
            "artifacts": {
                "candidate_regression_config": str(run_config_path),
                "candidate_action_overlay": str(overlay_path),
                **adapter_artifacts(adapter_results),
                **_optional_artifact(
                    "candidate_mutation_directives",
                    directives_path,
                ),
                **_optional_existing_artifact(
                    "candidate_runtime_metrics",
                    runtime_metrics_path,
                ),
            },
            "summary": {
                **adapter_metric_snapshot(adapter_results),
                "candidate_variant_count": len(
                    build_candidate_variants(adapter_results)
                ),
            },
        }

    def _error_report(
        self,
        *,
        task: dict[str, Any],
        proposal: dict[str, Any],
        baseline_metrics: dict[str, float | int],
        run_config_path: Path,
        overlay_path: Path,
        adapter_results: tuple[CandidateActionAdapterResult, ...],
        directives_path: Path | None,
        runtime_metrics_path: Path,
        error: BaseException,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": CANDIDATE_EVALUATION_KIND,
            "created_at": utc_timestamp(),
            "target": task.get("target"),
            "run_id": task.get("run_id"),
            "proposal_id": proposal.get("proposal_id"),
            "source": type(self).__name__,
            "status": "error",
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": {},
            "acceptance_thresholds": self.settings.thresholds.to_json(),
            "error": {"type": type(error).__name__, "message": str(error)},
            "artifacts": {
                "candidate_regression_config": str(run_config_path),
                "candidate_action_overlay": str(overlay_path),
                **adapter_artifacts(adapter_results),
                **_optional_artifact(
                    "candidate_mutation_directives",
                    directives_path,
                ),
                **_optional_existing_artifact(
                    "candidate_runtime_metrics",
                    runtime_metrics_path,
                ),
            },
            "summary": {
                **adapter_metric_snapshot(adapter_results),
                "candidate_variant_count": len(
                    build_candidate_variants(adapter_results)
                ),
            },
        }


def default_candidate_action_adapters() -> dict[str, CandidateActionAdapter]:
    return {
        action_type: JsonConfigActionAdapter(action_type)
        for action_type in ADAPTER_CONFIG_KINDS
    }


def adapter_config_for(action_type: str) -> tuple[str, str, str]:
    config = ADAPTER_CONFIG_KINDS.get(action_type)
    if config is not None:
        return config
    slug = safe_slug(action_type)
    make_slug = slug.upper().replace("-", "_").replace(".", "_")
    return (
        f"libafl_bfm_fuzz.harness_candidate_{slug}_config",
        f"candidate_{slug}_config",
        f"HARNESS_{make_slug}_CONFIG",
    )


def load_candidate_actions(
    candidate_manifest: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    actions = []
    for item in list_value(candidate_manifest.get("candidate_artifacts")):
        if not isinstance(item, dict):
            continue
        artifact_path = _path_or_none(item.get("artifact_path"))
        action_payload = read_json_object(artifact_path) if artifact_path else {}
        actions.append(
            {
                "action_id": item.get("action_id"),
                "action_type": item.get("action_type"),
                "artifact_path": str(artifact_path) if artifact_path else None,
                "evidence_refs": list_value(item.get("evidence_refs")),
                "action": mapping(action_payload.get("action")),
            }
        )
    return tuple(actions)


def adapt_candidate_actions(
    actions: tuple[dict[str, Any], ...],
    context: CandidateActionAdapterContext,
    *,
    adapters: Mapping[str, CandidateActionAdapter] | None,
) -> tuple[CandidateActionAdapterResult, ...]:
    adapter_map = default_candidate_action_adapters()
    if adapters is not None:
        adapter_map.update(adapters)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        action_type = _optional_str(action.get("action_type"))
        if action_type is None:
            continue
        by_type.setdefault(action_type, []).append(action)
    results = []
    for action_type, grouped_actions in sorted(by_type.items()):
        adapter = adapter_map.get(action_type)
        if adapter is None:
            adapter = JsonConfigActionAdapter(action_type)
        results.append(adapter.adapt(tuple(grouped_actions), context))
    return tuple(results)


def build_candidate_action_overlay(
    candidate_manifest: dict[str, Any],
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> dict[str, Any]:
    variants = build_candidate_variants(adapter_results)
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_optimization_candidate_action_overlay",
        "created_at": utc_timestamp(),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "actions": [
            entry
            for result in adapter_results
            for entry in result.entries
        ],
        "adapter_results": [
            {
                "action_type": result.action_type,
                "artifact_role": result.artifact_role,
                "artifact_path": str(result.artifact_path)
                if result.artifact_path is not None
                else None,
                "make_var": result.make_var,
                "entry_count": len(result.entries),
                "variant_count": len(result.variants),
            }
            for result in adapter_results
        ],
        "variants": variants,
        "selected_variant_id": "combined" if variants else None,
    }


def build_candidate_directives(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> dict[str, Any]:
    directives = [
        directive
        for result in adapter_results
        for directive in result.directives
    ]
    return {
        "schema_version": 1,
        "source": "harness_optimization_candidate",
        "directives": directives,
    }


def action_entry_for(action: dict[str, Any]) -> dict[str, Any]:
    payload = mapping(mapping(action.get("action")).get("payload"))
    return {
        "action_id": action.get("action_id"),
        "action_type": action.get("action_type"),
        "artifact_path": action.get("artifact_path"),
        "evidence_refs": list_value(action.get("evidence_refs")),
        "payload": payload,
        "rationale": mapping(action.get("action")).get("rationale"),
    }


def directives_from_action_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if entry.get("action_type") != "mutation_directive_update":
        return ()
    payload = mapping(entry.get("payload"))
    if isinstance(payload.get("directives"), list):
        return tuple(
            directive
            for directive in payload["directives"]
            if isinstance(directive, dict)
        )
    if isinstance(payload.get("directive"), dict):
        return (payload["directive"],)
    if payload:
        return (
            {
                "source": "harness_optimization_candidate",
                "action_id": entry.get("action_id"),
                "payload": payload,
            },
        )
    return ()


def action_variant(
    entry: dict[str, Any],
    *,
    artifact_path: Path,
) -> dict[str, Any]:
    action_id = str(entry.get("action_id") or entry.get("action_type") or "action")
    return {
        "variant_id": f"action_{safe_slug(action_id)}",
        "variant_type": "single_action",
        "action_ids": [entry.get("action_id")],
        "action_types": [entry.get("action_type")],
        "artifact_paths": [str(artifact_path)],
        "validation_status": "materialized_not_run",
    }


def build_candidate_variants(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> list[dict[str, Any]]:
    single_action_variants = [
        variant
        for result in adapter_results
        for variant in result.variants
    ]
    if not single_action_variants:
        return []
    combined = {
        "variant_id": "combined",
        "variant_type": "combined_actions",
        "action_ids": [
            action_id
            for variant in single_action_variants
            for action_id in list_value(variant.get("action_ids"))
        ],
        "action_types": sorted(
            {
                str(action_type)
                for variant in single_action_variants
                for action_type in list_value(variant.get("action_types"))
                if action_type is not None
            }
        ),
        "artifact_paths": sorted(
            {
                str(path)
                for variant in single_action_variants
                for path in list_value(variant.get("artifact_paths"))
                if path is not None
            }
        ),
        "validation_status": "selected_for_regression",
    }
    return [combined, *single_action_variants]


def adapter_make_vars(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> tuple[str, ...]:
    values = [
        value
        for result in adapter_results
        if (value := result.make_var_assignment()) is not None
    ]
    return tuple(values)


def adapter_artifacts(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for result in adapter_results:
        artifacts.update(result.artifact_json())
    return artifacts


def adapter_metric_snapshot(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> dict[str, int]:
    metrics = {
        "candidate_action_count": sum(len(result.entries) for result in adapter_results),
        "candidate_overlay_count": len(adapter_results),
        "candidate_variant_count": len(build_candidate_variants(adapter_results)),
    }
    for result in adapter_results:
        metrics.update(result.metric_json())
    metrics["candidate_directive_count"] = sum(
        len(result.directives) for result in adapter_results
    )
    return metrics


def candidate_regression_config_payload(
    config: CampaignConfig,
    *,
    action_overlay: Path,
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    initial_directives: Path | None,
    runtime_metrics: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_candidate_regression_config",
        "created_at": utc_timestamp(),
        "target": config.target,
        "out_dir": str(config.out_dir),
        "modes": list(config.modes),
        "rounds": config.rounds,
        "iters": config.iters,
        "max_seeds": config.max_seeds,
        "seed": config.seed,
        "run_plan_profile": config.run_plan_profile,
        "campaign_plan_profile": config.campaign_plan_profile,
        "round_evaluation": config.round_evaluation,
        "extra_make_vars": list(config.extra_make_vars),
        "initial_directives": str(initial_directives)
        if initial_directives is not None
        else None,
        "action_overlay": str(action_overlay),
        "runtime_metrics": str(runtime_metrics),
        "adapter_artifacts": adapter_artifacts(adapter_results),
        "adapter_metrics": adapter_metric_snapshot(adapter_results),
        "campaign_manifest_out": str(config.campaign_manifest_out),
        "campaign_evaluation_out": str(config.campaign_evaluation_out),
        "safety": {
            "mainline_modified": False,
            "application": "sandbox_candidate_regression",
        },
    }


def build_candidate_variant_ranking(
    *,
    action_overlay: dict[str, Any],
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
    selected_variant_id: str,
) -> dict[str, Any]:
    variants = []
    for variant in list_value(action_overlay.get("variants")):
        if not isinstance(variant, dict):
            continue
        variant_id = str(variant.get("variant_id") or "variant")
        score = variant_materialization_score(variant)
        validation_status = variant.get("validation_status")
        if variant_id == selected_variant_id:
            score += metric_improvement_score(baseline_metrics, candidate_metrics)
            validation_status = "executed"
        variants.append(
            {
                **variant,
                "validation_status": validation_status,
                "score": score,
            }
        )
    ranked = sorted(
        variants,
        key=lambda item: (
            -float(number_value(item.get("score")) or 0),
            str(item.get("variant_id")),
        ),
    )
    for index, variant in enumerate(ranked, start=1):
        variant["rank"] = index
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_candidate_variant_ranking",
        "created_at": utc_timestamp(),
        "selected_variant_id": selected_variant_id,
        "variants": ranked,
        "top_variant": ranked[0] if ranked else None,
    }


def build_candidate_promotion_package(
    *,
    task: dict[str, Any],
    proposal: dict[str, Any],
    patch: dict[str, Any],
    candidate_manifest: dict[str, Any],
    candidate_metrics: dict[str, float | int],
    baseline_metrics: dict[str, float | int],
    ranking: dict[str, Any],
    thresholds: CandidateAcceptanceThresholds,
) -> dict[str, Any]:
    summary = metric_threshold_summary(
        baseline_metrics,
        candidate_metrics,
        thresholds,
    )
    promotion_status = "ready_for_review" if summary["passes_thresholds"] else "hold"
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_candidate_promotion_package",
        "created_at": utc_timestamp(),
        "target": task.get("target"),
        "run_id": task.get("run_id"),
        "proposal_id": proposal.get("proposal_id"),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "promotion_status": promotion_status,
        "top_variant": ranking.get("top_variant"),
        "threshold_summary": summary,
        "evidence_refs": list_value(proposal.get("evidence_refs")),
        "safety": {
            "mainline_modified": False,
            "promotion_mode": "review_artifact_only",
            "sandbox_dir": patch.get("sandbox_dir"),
        },
        "candidate_metrics": candidate_metrics,
        "baseline_metrics": baseline_metrics,
    }


def metric_threshold_summary(
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
    thresholds: CandidateAcceptanceThresholds,
) -> dict[str, Any]:
    improved = 0
    regressed = 0
    changed = 0
    for metric, baseline in baseline_metrics.items():
        if metric not in candidate_metrics:
            continue
        delta = candidate_metrics[metric] - baseline
        direction = metric_direction(metric, delta)
        if direction == "improved":
            improved += 1
        elif direction == "regressed":
            regressed += 1
        elif direction == "changed":
            changed += 1
    return {
        "improved_metric_count": improved,
        "regressed_metric_count": regressed,
        "changed_metric_count": changed,
        "max_regressed_metric_count": thresholds.max_regressed_metric_count,
        "min_improved_metric_count": thresholds.min_improved_metric_count,
        "passes_thresholds": (
            regressed <= thresholds.max_regressed_metric_count
            and improved >= thresholds.min_improved_metric_count
        ),
    }


def metric_improvement_score(
    baseline_metrics: dict[str, float | int],
    candidate_metrics: dict[str, float | int],
) -> float:
    score = 0.0
    for metric, baseline in baseline_metrics.items():
        if metric not in candidate_metrics:
            continue
        delta = candidate_metrics[metric] - baseline
        direction = metric_direction(metric, delta)
        if direction == "improved":
            score += 10.0
        elif direction == "regressed":
            score -= 20.0
        elif direction == "changed":
            score += 1.0
    return score


def variant_materialization_score(variant: dict[str, Any]) -> float:
    return float(len(list_value(variant.get("action_ids"))))


def candidate_metric_snapshot(
    campaign_manifest: dict[str, Any],
    campaign_evaluation: dict[str, Any],
    *,
    cwd: Path | None,
) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for source in (
        mapping(campaign_evaluation.get("summary")),
        mapping(mapping(campaign_evaluation.get("harness_trace")).get("summary")),
    ):
        _merge_numeric_metrics(metrics, source)
    harness_evaluation = _harness_evaluation(campaign_evaluation, cwd)
    _merge_numeric_metrics(metrics, mapping(harness_evaluation.get("summary")))
    _merge_numeric_metrics(metrics, mapping(harness_evaluation.get("trace_quality")))
    _merge_campaign_round_metrics(metrics, campaign_manifest)
    _merge_campaign_runtime_metrics(metrics, campaign_manifest, cwd=cwd)
    return metrics


def runtime_metric_snapshot(path: Path | None) -> dict[str, float | int]:
    if path is None or not path.exists():
        return {}
    return runtime_metrics_summary(read_runtime_metrics(path))


def _merge_campaign_round_metrics(
    metrics: dict[str, float | int],
    campaign_manifest: dict[str, Any],
) -> None:
    last_coverage: dict[str, Any] = {}
    directive_count = None
    case_count = 0
    for mode in list_value(campaign_manifest.get("modes")):
        if not isinstance(mode, dict):
            continue
        for round_item in list_value(mode.get("rounds")):
            if not isinstance(round_item, dict):
                continue
            coverage = mapping(round_item.get("coverage"))
            if coverage:
                last_coverage = coverage
            feedback = mapping(round_item.get("feedback"))
            if feedback.get("directive_count") is not None:
                directive_count = feedback.get("directive_count")
            stages = mapping(round_item.get("stages"))
            corpus_validation = mapping(
                mapping(stages.get("corpus_validation")).get("validation")
            )
            case_count += int(number_value(corpus_validation.get("case_count")) or 0)
    _merge_numeric_metrics(metrics, last_coverage)
    if directive_count is not None:
        value = number_value(directive_count)
        if value is not None:
            metrics["directive_count"] = value
    if case_count:
        metrics["case_count"] = case_count


def _merge_campaign_runtime_metrics(
    metrics: dict[str, float | int],
    campaign_manifest: dict[str, Any],
    *,
    cwd: Path | None,
) -> None:
    for mode in list_value(campaign_manifest.get("modes")):
        if not isinstance(mode, dict):
            continue
        for round_item in list_value(mode.get("rounds")):
            if not isinstance(round_item, dict):
                continue
            path = _artifact_path(
                mapping(round_item.get("artifacts")),
                "harness_runtime_metrics",
                cwd,
            )
            _merge_numeric_metrics(metrics, runtime_metric_snapshot(path))


def _harness_evaluation(
    campaign_evaluation: dict[str, Any],
    cwd: Path | None,
) -> dict[str, Any]:
    artifacts = mapping(
        mapping(campaign_evaluation.get("harness_trace")).get("artifacts")
    )
    path = _path_or_none(artifacts.get("harness_evaluation"))
    if path is None:
        return {}
    if not path.is_absolute() and cwd is not None:
        path = cwd / path
    return read_json_object(path)


def _merge_numeric_metrics(
    target: dict[str, float | int],
    values: dict[str, Any],
) -> None:
    for key, value in values.items():
        number = number_value(value)
        if number is not None:
            target[str(key)] = number


def _setting_int(
    value: int | None,
    baseline_config: dict[str, Any],
    key: str,
    default: int,
) -> int:
    if value is not None:
        return value
    baseline_value = number_value(baseline_config.get(key))
    return int(baseline_value if baseline_value is not None else default)


def _artifact_path(
    artifacts: dict[str, Any],
    role: str,
    cwd: Path | None,
) -> Path | None:
    path = _path_or_none(artifacts.get(role))
    if path is None:
        return None
    if path.is_absolute() or cwd is None:
        return path
    return cwd / path


def _campaign_cwd(
    campaign_manifest: dict[str, Any],
    fallback: Path | None,
) -> Path | None:
    return _path_or_none(campaign_manifest.get("cwd")) or fallback


def _required_path(value: Any) -> Path:
    path = _path_or_none(value)
    if path is None:
        raise ValueError(f"expected non-empty path, got {value!r}")
    return path


def _path_or_none(value: Any) -> Path | None:
    if not isinstance(value, str | Path) or not str(value):
        return None
    return Path(value)


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _optional_artifact(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None else {}


def _optional_existing_artifact(name: str, path: Path | None) -> dict[str, str]:
    return {name: str(path)} if path is not None and path.exists() else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
