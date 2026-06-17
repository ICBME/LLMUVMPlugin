from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .io import path_or_none, read_json_object, write_json
from .plugins import HarnessPluginRegistry
from .records import list_value, mapping


CANDIDATE_REGRESSION_CONFIG_KIND = "harness_optimization.candidate_regression_config"
CANDIDATE_ACTION_OVERLAY_KIND = "harness_optimization.candidate_action_overlay"
DEFAULT_JSON_CONFIG_KIND_PREFIX = "harness_optimization.harness_candidate_"
DEFAULT_JSON_CONFIG_ARTIFACT_ROLE_PREFIX = "candidate_"
DEFAULT_JSON_CONFIG_MAKE_VAR_PREFIX = "HARNESS_"


@dataclass(frozen=True)
class CandidateAcceptanceThresholds:
    max_regressed_metric_count: int = 0
    min_improved_metric_count: int = 1
    max_flaky_metric_count: int = 0
    accepted_candidate_statuses: tuple[str, ...] = ("passed", "ok")

    def to_json(self) -> dict[str, Any]:
        return {
            "max_regressed_metric_count": self.max_regressed_metric_count,
            "min_improved_metric_count": self.min_improved_metric_count,
            "max_flaky_metric_count": self.max_flaky_metric_count,
            "accepted_candidate_statuses": list(self.accepted_candidate_statuses),
        }


@dataclass(frozen=True)
class CandidateRegressionSettings:
    modes: tuple[str, ...] | None = None
    rounds: int = 1
    iters: int | None = None
    max_seeds: int | None = None
    seed: int | None = None
    max_variant_regressions: int = 1
    run_plan_profile: str | None = None
    round_evaluation: bool = True
    campaign_plan_profile: str = "campaign_with_evaluation"
    matched_baseline: bool = False
    paired_repeats: int = 1
    repeat_seed_stride: int = 1
    attribution_top_k: int | None = None
    attribution_mode: str = "top_k"
    strict_plugin_validation: bool = False
    thresholds: CandidateAcceptanceThresholds = CandidateAcceptanceThresholds()

    def __post_init__(self) -> None:
        if self.max_variant_regressions < 1:
            raise ValueError("max_variant_regressions must be >= 1")
        if self.paired_repeats < 1:
            raise ValueError("paired_repeats must be >= 1")
        if self.repeat_seed_stride < 1:
            raise ValueError("repeat_seed_stride must be >= 1")
        if self.attribution_top_k is not None and self.attribution_top_k < 1:
            raise ValueError("attribution_top_k must be >= 1")
        if self.attribution_mode not in {"top_k", "all_actions"}:
            raise ValueError("attribution_mode must be one of: top_k, all_actions")

    def to_json(self) -> dict[str, Any]:
        return {
            "modes": list(self.modes) if self.modes is not None else None,
            "rounds": self.rounds,
            "iters": self.iters,
            "max_seeds": self.max_seeds,
            "seed": self.seed,
            "max_variant_regressions": self.max_variant_regressions,
            "run_plan_profile": self.run_plan_profile,
            "round_evaluation": self.round_evaluation,
            "campaign_plan_profile": self.campaign_plan_profile,
            "matched_baseline": self.matched_baseline,
            "paired_repeats": self.paired_repeats,
            "repeat_seed_stride": self.repeat_seed_stride,
            "attribution_top_k": self.attribution_top_k,
            "attribution_mode": self.attribution_mode,
            "strict_plugin_validation": self.strict_plugin_validation,
            "thresholds": self.thresholds.to_json(),
        }


@dataclass(frozen=True)
class CandidateActionAdapterContext:
    candidate_id: str
    regression_dir: Path


@dataclass(frozen=True)
class MatchedBaselineRun:
    metrics: dict[str, float | int]
    artifacts: dict[str, str]
    summary: dict[str, Any]


@dataclass(frozen=True)
class CandidateRepeatRun:
    metrics: dict[str, float | int]
    artifacts: dict[str, str]
    evaluation: dict[str, Any]


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


class CandidateRegressionRunConfig(Protocol):
    target: str
    out_dir: Path
    modes: tuple[str, ...]
    rounds: int
    iters: int
    max_seeds: int
    seed: int
    run_plan_profile: str | None
    campaign_plan_profile: str | None
    round_evaluation: bool
    extra_make_vars: tuple[str, ...]
    campaign_manifest_out: Path | None
    campaign_evaluation_out: Path | None


def candidate_regression_config_payload(
    config: CandidateRegressionRunConfig,
    *,
    action_overlay: Path,
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    initial_directives: Path | None,
    runtime_metrics: Path,
    run_role: str = "candidate",
    settings: CandidateRegressionSettings | Mapping[str, Any] | None = None,
    kind: str = CANDIDATE_REGRESSION_CONFIG_KIND,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": kind,
        "created_at": _utc_timestamp(),
        "run_role": run_role,
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
        "initial_directives": (
            str(initial_directives) if initial_directives is not None else None
        ),
        "action_overlay": str(action_overlay),
        "runtime_metrics": str(runtime_metrics),
        "adapter_artifacts": adapter_artifacts(adapter_results),
        "adapter_metrics": adapter_metric_snapshot(adapter_results),
        "validation_settings": _settings_json(settings),
        "campaign_manifest_out": (
            str(config.campaign_manifest_out)
            if config.campaign_manifest_out is not None
            else None
        ),
        "campaign_evaluation_out": (
            str(config.campaign_evaluation_out)
            if config.campaign_evaluation_out is not None
            else None
        ),
        "safety": {
            "mainline_modified": False,
            "application": "sandbox_candidate_regression",
        },
    }


def build_candidate_action_overlay(
    candidate_manifest: Mapping[str, Any],
    adapter_results: tuple[CandidateActionAdapterResult, ...],
    *,
    kind: str = CANDIDATE_ACTION_OVERLAY_KIND,
) -> dict[str, Any]:
    variants = build_candidate_variants(adapter_results)
    return {
        "schema_version": 1,
        "kind": kind,
        "created_at": _utc_timestamp(),
        "candidate_id": candidate_manifest.get("candidate_id"),
        "actions": [entry for result in adapter_results for entry in result.entries],
        "adapter_results": [
            {
                "action_type": result.action_type,
                "artifact_role": result.artifact_role,
                "artifact_path": (
                    str(result.artifact_path)
                    if result.artifact_path is not None
                    else None
                ),
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
    *,
    source: str = "harness_optimization_candidate",
) -> dict[str, Any]:
    directives = [
        directive for result in adapter_results for directive in result.directives
    ]
    return {
        "schema_version": 1,
        "source": source,
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
        "variant_id": f"action_{_safe_slug(action_id)}",
        "variant_type": "single_action",
        "action_ids": [entry.get("action_id")],
        "action_types": [entry.get("action_type")],
        "artifact_paths": [str(artifact_path)],
        "validation_status": "materialized_not_run",
    }


@dataclass(frozen=True)
class JsonConfigActionAdapter:
    action_type: str
    artifact_kind: str
    artifact_role: str
    make_var: str | None = None

    def adapt(
        self,
        actions: tuple[dict[str, Any], ...],
        context: CandidateActionAdapterContext,
    ) -> CandidateActionAdapterResult:
        entries = tuple(action_entry_for(action) for action in actions)
        artifact = {
            "schema_version": 1,
            "kind": self.artifact_kind,
            "created_at": _utc_timestamp(),
            "candidate_id": context.candidate_id,
            "action_type": self.action_type,
            "entries": list(entries),
        }
        artifact_path = context.regression_dir / f"{self.artifact_role}.json"
        write_json(artifact_path, artifact)
        directives = tuple(
            directive
            for entry in entries
            for directive in directives_from_action_entry(entry)
        )
        return CandidateActionAdapterResult(
            action_type=self.action_type,
            artifact_role=self.artifact_role,
            artifact_path=artifact_path,
            make_var=self.make_var,
            entries=entries,
            directives=directives,
            variants=tuple(
                action_variant(entry, artifact_path=artifact_path)
                for entry in entries
            ),
            metric_counts={f"{self.action_type}_count": len(entries)},
        )


def build_candidate_variants(
    adapter_results: tuple[CandidateActionAdapterResult, ...],
) -> list[dict[str, Any]]:
    single_action_variants = [
        variant for result in adapter_results for variant in result.variants
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


def select_candidate_variants(
    variants: Any,
    *,
    max_count: int,
    attribution_mode: str = "top_k",
) -> tuple[dict[str, Any], ...]:
    if max_count < 1:
        raise ValueError("max_count must be >= 1")
    if attribution_mode not in {"top_k", "all_actions"}:
        raise ValueError("attribution_mode must be one of: top_k, all_actions")
    selected = [variant for variant in list_value(variants) if isinstance(variant, dict)]
    if attribution_mode == "all_actions":
        return tuple(selected)
    return tuple(selected[:max_count])


def filter_candidate_actions_for_variant(
    actions: tuple[dict[str, Any], ...],
    variant: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    action_ids = {
        str(action_id)
        for action_id in list_value(variant.get("action_ids"))
        if action_id is not None
    }
    if not action_ids:
        return ()
    return tuple(action for action in actions if str(action.get("action_id")) in action_ids)


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


def json_config_adapter_config_for(
    action_type: str,
    *,
    plugin_registry: HarnessPluginRegistry | None = None,
    kind_prefix: str = DEFAULT_JSON_CONFIG_KIND_PREFIX,
    artifact_role_prefix: str = DEFAULT_JSON_CONFIG_ARTIFACT_ROLE_PREFIX,
    make_var_prefix: str = DEFAULT_JSON_CONFIG_MAKE_VAR_PREFIX,
) -> tuple[str, str, str]:
    plugin_config = (
        plugin_registry.adapter_config_for(action_type)
        if plugin_registry is not None
        else None
    )
    if plugin_config is not None:
        return plugin_config
    slug = _safe_slug(action_type)
    make_slug = slug.upper().replace("-", "_").replace(".", "_")
    return (
        f"{kind_prefix}{slug}_config",
        f"{artifact_role_prefix}{slug}_config",
        f"{make_var_prefix}{make_slug}_CONFIG",
    )


def json_config_action_adapter(
    action_type: str,
    *,
    plugin_registry: HarnessPluginRegistry | None = None,
    kind_prefix: str = DEFAULT_JSON_CONFIG_KIND_PREFIX,
    artifact_role_prefix: str = DEFAULT_JSON_CONFIG_ARTIFACT_ROLE_PREFIX,
    make_var_prefix: str = DEFAULT_JSON_CONFIG_MAKE_VAR_PREFIX,
) -> JsonConfigActionAdapter:
    kind, artifact_role, make_var = json_config_adapter_config_for(
        action_type,
        plugin_registry=plugin_registry,
        kind_prefix=kind_prefix,
        artifact_role_prefix=artifact_role_prefix,
        make_var_prefix=make_var_prefix,
    )
    return JsonConfigActionAdapter(
        action_type=action_type,
        artifact_kind=kind,
        artifact_role=artifact_role,
        make_var=make_var,
    )


def default_candidate_action_adapters(
    plugin_registry: HarnessPluginRegistry | None = None,
    *,
    kind_prefix: str = DEFAULT_JSON_CONFIG_KIND_PREFIX,
    artifact_role_prefix: str = DEFAULT_JSON_CONFIG_ARTIFACT_ROLE_PREFIX,
    make_var_prefix: str = DEFAULT_JSON_CONFIG_MAKE_VAR_PREFIX,
) -> dict[str, CandidateActionAdapter]:
    if plugin_registry is None:
        return {}
    return {
        action_type: json_config_action_adapter(
            action_type,
            plugin_registry=plugin_registry,
            kind_prefix=kind_prefix,
            artifact_role_prefix=artifact_role_prefix,
            make_var_prefix=make_var_prefix,
        )
        for action_type in plugin_registry.allowed_action_types()
        if plugin_registry.adapter_config_for(action_type) is not None
    }


def load_candidate_actions(
    candidate_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    actions = []
    for item in list_value(candidate_manifest.get("candidate_artifacts")):
        if not isinstance(item, dict):
            continue
        artifact_path = path_or_none(item.get("artifact_path"))
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
    plugin_registry: HarnessPluginRegistry | None = None,
    kind_prefix: str = DEFAULT_JSON_CONFIG_KIND_PREFIX,
    artifact_role_prefix: str = DEFAULT_JSON_CONFIG_ARTIFACT_ROLE_PREFIX,
    make_var_prefix: str = DEFAULT_JSON_CONFIG_MAKE_VAR_PREFIX,
) -> tuple[CandidateActionAdapterResult, ...]:
    adapter_map = default_candidate_action_adapters(
        plugin_registry,
        kind_prefix=kind_prefix,
        artifact_role_prefix=artifact_role_prefix,
        make_var_prefix=make_var_prefix,
    )
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
            adapter = json_config_action_adapter(
                action_type,
                plugin_registry=plugin_registry,
                kind_prefix=kind_prefix,
                artifact_role_prefix=artifact_role_prefix,
                make_var_prefix=make_var_prefix,
            )
        results.append(adapter.adapt(tuple(grouped_actions), context))
    return tuple(results)


def _settings_json(
    settings: CandidateRegressionSettings | Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if settings is None:
        return None
    if isinstance(settings, CandidateRegressionSettings):
        return settings.to_json()
    return dict(settings)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_slug(value: str) -> str:
    chars = []
    for char in value:
        if char.isascii() and (char.isalnum() or char in {"-", "_", "."}):
            chars.append(char)
        else:
            chars.append("_")
    slug = "".join(chars).strip("._")
    return slug or "item"


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None
