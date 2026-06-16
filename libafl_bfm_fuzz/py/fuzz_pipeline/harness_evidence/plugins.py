from __future__ import annotations

from dataclasses import dataclass

from harness_optimization.plugins import (
    HarnessActionPlugin,
    HarnessGapActionabilityContext,
    HarnessPluginRegistry as _HarnessPluginRegistry,
    build_harness_plugin,
    callable_name,
    harness_plugin_registry_fingerprint,
    harness_plugin_validation_error_message,
    load_harness_plugin_registry,
    normalize_gap_actionability,
    plugin_registry_summary,
    plugin_source_for_spec,
    require_valid_harness_plugin_registry as _require_valid_harness_plugin_registry,
    unknown_gap_actionability,
    validate_harness_plugin_registry as _validate_harness_plugin_registry,
)
from harness_optimization.plugins import (
    harness_plugin_provenance as _harness_plugin_provenance,
)
from harness_optimization.plugins import (
    harness_plugin_registry_snapshot_schema as _harness_plugin_registry_snapshot_schema,
)


REGISTRY_SNAPSHOT_KIND = "libafl_bfm_fuzz.harness_plugin_registry"
REGISTRY_PROVENANCE_KIND = "libafl_bfm_fuzz.harness_plugin_provenance"
REGISTRY_VALIDATION_KIND = "libafl_bfm_fuzz.harness_plugin_contract_validation"


@dataclass(frozen=True)
class HarnessPluginRegistry(_HarnessPluginRegistry):
    snapshot_kind: str = REGISTRY_SNAPSHOT_KIND
    provenance_kind: str = REGISTRY_PROVENANCE_KIND
    validation_kind: str = REGISTRY_VALIDATION_KIND


def harness_plugin_registry_snapshot_schema() -> dict[str, object]:
    return _harness_plugin_registry_snapshot_schema(
        snapshot_kind=REGISTRY_SNAPSHOT_KIND,
    )


def validate_harness_plugin_registry(
    registry: HarnessPluginRegistry,
) -> dict[str, object]:
    return _validate_harness_plugin_registry(
        registry,
        validation_kind=REGISTRY_VALIDATION_KIND,
    )


def require_valid_harness_plugin_registry(
    registry: HarnessPluginRegistry,
    *,
    context: str = "harness plugin registry",
) -> dict[str, object]:
    return _require_valid_harness_plugin_registry(
        registry,
        context=context,
        validation_kind=REGISTRY_VALIDATION_KIND,
    )


def harness_plugin_provenance(registry: HarnessPluginRegistry) -> dict[str, object]:
    return _harness_plugin_provenance(
        registry,
        provenance_kind=REGISTRY_PROVENANCE_KIND,
        validation_kind=REGISTRY_VALIDATION_KIND,
    )


def plugin_registry_to_json(registry: HarnessPluginRegistry) -> dict[str, object]:
    return registry.to_json(
        snapshot_kind=REGISTRY_SNAPSHOT_KIND,
        validation_kind=REGISTRY_VALIDATION_KIND,
    )


def plugin_registry_validation_json(registry: HarnessPluginRegistry) -> dict[str, object]:
    return registry.validation_json(validation_kind=REGISTRY_VALIDATION_KIND)


def plugin_registry_provenance_json(registry: HarnessPluginRegistry) -> dict[str, object]:
    return registry.provenance_json(
        provenance_kind=REGISTRY_PROVENANCE_KIND,
        validation_kind=REGISTRY_VALIDATION_KIND,
    )


__all__ = [
    "HarnessActionPlugin",
    "HarnessGapActionabilityContext",
    "HarnessPluginRegistry",
    "REGISTRY_PROVENANCE_KIND",
    "REGISTRY_SNAPSHOT_KIND",
    "REGISTRY_VALIDATION_KIND",
    "callable_name",
    "harness_plugin_provenance",
    "harness_plugin_registry_fingerprint",
    "harness_plugin_registry_snapshot_schema",
    "harness_plugin_validation_error_message",
    "build_harness_plugin",
    "load_harness_plugin_registry",
    "normalize_gap_actionability",
    "plugin_registry_provenance_json",
    "plugin_registry_summary",
    "plugin_registry_to_json",
    "plugin_registry_validation_json",
    "plugin_source_for_spec",
    "require_valid_harness_plugin_registry",
    "unknown_gap_actionability",
    "validate_harness_plugin_registry",
]
