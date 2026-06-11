from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from connector_observe import ObservationContext
from connector_observe.trace import read_json_object

from .campaign_orchestrator import CampaignConfig, CampaignOrchestrator
from .harness_optimization import (
    CANDIDATE_EVALUATION_KIND,
    baseline_metric_snapshot,
    list_value,
    number_value,
    safe_slug,
    utc_timestamp,
)
from .harness_records import mapping
from .run_adapters import RunBackends
from .run_evaluation import EvaluationBackends
from .run_orchestrator import FuzzRunOrchestrator
from .run_profiles import RunPlanProfile
from .topology import FULL_FUZZ_TOPOLOGY, PipelineTopology


CampaignOrchestratorFactory = Callable[..., CampaignOrchestrator]


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
class HarnessCandidateRegressionBackend:
    settings: CandidateRegressionSettings = CandidateRegressionSettings()
    topology: PipelineTopology = FULL_FUZZ_TOPOLOGY
    run_backends: RunBackends | None = None
    run_plan_profiles: Mapping[str, RunPlanProfile] | None = None
    campaign_plan_profiles: Mapping[str, RunPlanProfile] | None = None
    evaluation_backends: EvaluationBackends | None = None
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
        overlay = build_candidate_action_overlay(candidate_manifest)
        overlay_path = regression_dir / "candidate_action_overlay.json"
        _write_json(overlay_path, overlay)
        candidate_directives = build_candidate_directives(overlay)
        candidate_directives_path = (
            regression_dir / "candidate_mutation_directives.json"
            if candidate_directives["directives"]
            else None
        )
        if candidate_directives_path is not None:
            _write_json(candidate_directives_path, candidate_directives)

        campaign_config = self._candidate_campaign_config(
            task=task,
            baseline_manifest=baseline_manifest,
            regression_dir=regression_dir,
            action_overlay=overlay_path,
            initial_directives=candidate_directives_path,
        )
        run_config_path = regression_dir / "candidate_regression_config.json"
        _write_json(
            run_config_path,
            candidate_regression_config_payload(
                campaign_config,
                action_overlay=overlay_path,
                initial_directives=candidate_directives_path,
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
                directives_path=candidate_directives_path,
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
                directives_path=candidate_directives_path,
                error=exc,
            )

        candidate_evaluation = read_json_object(campaign_config.campaign_evaluation_out)
        candidate_metrics = candidate_metric_snapshot(
            candidate_campaign_manifest,
            candidate_evaluation,
            cwd=_campaign_cwd(candidate_campaign_manifest, campaign_config.cwd),
        )
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
                **_optional_artifact(
                    "candidate_mutation_directives",
                    candidate_directives_path,
                ),
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
            },
        }

    def _candidate_campaign_config(
        self,
        *,
        task: dict[str, Any],
        baseline_manifest: dict[str, Any],
        regression_dir: Path,
        action_overlay: Path,
        initial_directives: Path | None,
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
        directives_path: Path | None,
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
                **_optional_artifact(
                    "candidate_mutation_directives",
                    directives_path,
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
        directives_path: Path | None,
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
                **_optional_artifact(
                    "candidate_mutation_directives",
                    directives_path,
                ),
            },
        }


def build_candidate_action_overlay(
    candidate_manifest: dict[str, Any],
) -> dict[str, Any]:
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
    return {
        "schema_version": 1,
        "kind": "libafl_bfm_fuzz.harness_optimization_candidate_action_overlay",
        "created_at": utc_timestamp(),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "actions": actions,
    }


def build_candidate_directives(action_overlay: dict[str, Any]) -> dict[str, Any]:
    directives = []
    for item in list_value(action_overlay.get("actions")):
        if not isinstance(item, dict) or item.get("action_type") != (
            "mutation_directive_update"
        ):
            continue
        action = mapping(item.get("action"))
        payload = mapping(action.get("payload"))
        if isinstance(payload.get("directives"), list):
            directives.extend(payload["directives"])
        elif isinstance(payload.get("directive"), dict):
            directives.append(payload["directive"])
        elif payload:
            directives.append(
                {
                    "source": "harness_optimization_candidate",
                    "action_id": item.get("action_id"),
                    "payload": payload,
                }
            )
    return {
        "schema_version": 1,
        "source": "harness_optimization_candidate",
        "candidate_id": action_overlay.get("candidate_id"),
        "directives": directives,
    }


def candidate_regression_config_payload(
    config: CampaignConfig,
    *,
    action_overlay: Path,
    initial_directives: Path | None,
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
        "initial_directives": str(initial_directives)
        if initial_directives is not None
        else None,
        "action_overlay": str(action_overlay),
        "campaign_manifest_out": str(config.campaign_manifest_out),
        "campaign_evaluation_out": str(config.campaign_evaluation_out),
        "safety": {
            "mainline_modified": False,
            "application": "sandbox_candidate_regression",
        },
    }


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
    return metrics


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


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
