from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
from typing import Any, Mapping

from fuzz_bfm.target_config import TargetConfig
from fuzz_uvm.contracts import (
    PluginContractError,
    ensure_json_serializable,
    validate_comparator_plugin,
    validate_coverage_plugin,
    validate_ref_model_plugin,
    validate_scoreboard_plugin,
)


PLUGIN_MANIFEST_FIELDS = (
    "ref_model",
    "comparator",
    "scoreboard",
    "coverage_model",
)
PLUGIN_VALIDATION_ORDER = (
    "ref_model",
    "comparator",
    "scoreboard",
    "coverage_model",
)


@dataclass(frozen=True)
class GeneratedPluginBundle:
    """Generated plugin references emitted by an LLM/codegen stage."""

    target: str
    plugins: Mapping[str, str]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target))
        object.__setattr__(self, "plugins", _string_mapping(self.plugins, spec="plugins"))
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, spec="generated plugin bundle metadata"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "plugins": dict(self.plugins),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PluginValidationIssue:
    role: str
    message: str
    severity: str = "error"

    def to_json(self) -> dict[str, str]:
        return {
            "role": self.role,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class GeneratedPluginValidationReport:
    target: str
    plugins: Mapping[str, str]
    valid: bool
    validated_roles: tuple[str, ...] = ()
    issues: tuple[PluginValidationIssue, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target))
        object.__setattr__(self, "plugins", _string_mapping(self.plugins, spec="plugins"))
        object.__setattr__(self, "valid", _bool_value(self.valid, spec="valid"))
        object.__setattr__(
            self,
            "validated_roles",
            tuple(str(role) for role in self.validated_roles),
        )
        object.__setattr__(
            self,
            "issues",
            tuple(_normalize_issue(issue) for issue in self.issues),
        )
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, spec="plugin validation report metadata"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "plugins": dict(self.plugins),
            "valid": self.valid,
            "validated_roles": list(self.validated_roles),
            "issues": [issue.to_json() for issue in self.issues],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class GeneratedPluginRegistry:
    """Validated generated plugin references ready to overlay onto a manifest."""

    target: str
    plugins: Mapping[str, str]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target))
        object.__setattr__(self, "plugins", _string_mapping(self.plugins, spec="plugins"))
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, spec="generated plugin registry metadata"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "plugins": dict(self.plugins),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class TargetManifestOverlay:
    """Manifest field updates produced from a validated plugin registry."""

    target: str
    updates: Mapping[str, str]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", str(self.target))
        object.__setattr__(self, "updates", _string_mapping(self.updates, spec="updates"))
        object.__setattr__(
            self,
            "metadata",
            _json_mapping(self.metadata, spec="target manifest overlay metadata"),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "updates": dict(self.updates),
            "metadata": dict(self.metadata),
        }


def normalize_generated_plugin_bundle(value: Any) -> GeneratedPluginBundle:
    if isinstance(value, GeneratedPluginBundle):
        return value
    if not isinstance(value, Mapping):
        raise PluginContractError("generated plugin bundle must be a mapping")
    plugins = value.get("plugins")
    if plugins is None:
        plugins = {
            role: value[role]
            for role in PLUGIN_MANIFEST_FIELDS
            if role in value
        }
    target = value.get("target", "")
    if not str(target).strip():
        raise PluginContractError("generated plugin bundle must define target")
    return GeneratedPluginBundle(
        target=str(target),
        plugins=plugins,
        metadata=value.get("metadata", {}),
    )


def normalize_plugin_validation_report(value: Any) -> GeneratedPluginValidationReport:
    if isinstance(value, GeneratedPluginValidationReport):
        return value
    if not isinstance(value, Mapping):
        raise PluginContractError("plugin validation report must be a mapping")
    return GeneratedPluginValidationReport(
        target=str(value.get("target", "")),
        plugins=value.get("plugins", {}),
        valid=_bool_value(value.get("valid", False), spec="valid"),
        validated_roles=tuple(value.get("validated_roles", ())),
        issues=tuple(value.get("issues", ())),
        metadata=value.get("metadata", {}),
    )


def normalize_plugin_registry(value: Any) -> GeneratedPluginRegistry:
    if isinstance(value, GeneratedPluginRegistry):
        return value
    if not isinstance(value, Mapping):
        raise PluginContractError("generated plugin registry must be a mapping")
    return GeneratedPluginRegistry(
        target=str(value.get("target", "")),
        plugins=value.get("plugins", {}),
        metadata=value.get("metadata", {}),
    )


def normalize_manifest_overlay(value: Any) -> TargetManifestOverlay:
    if isinstance(value, TargetManifestOverlay):
        return value
    if not isinstance(value, Mapping):
        raise PluginContractError("target manifest overlay must be a mapping")
    return TargetManifestOverlay(
        target=str(value.get("target", "")),
        updates=value.get("updates", {}),
        metadata=value.get("metadata", {}),
    )


def validate_generated_plugin_bundle(
    bundle: GeneratedPluginBundle | Mapping[str, Any],
    *,
    config: TargetConfig | None = None,
    import_plugins: bool = True,
) -> GeneratedPluginValidationReport:
    normalized = normalize_generated_plugin_bundle(bundle)
    issues: list[PluginValidationIssue] = []
    validated_roles: list[str] = []
    comparator_plugin: Any = None

    if not normalized.plugins:
        issues.append(
            PluginValidationIssue(
                role="plugins",
                message="generated plugin bundle must contain at least one plugin",
            )
        )

    for role in sorted(normalized.plugins):
        if role not in PLUGIN_MANIFEST_FIELDS:
            issues.append(
                PluginValidationIssue(
                    role=role,
                    message=f"unsupported generated plugin role {role!r}",
                )
            )

    for role in PLUGIN_VALIDATION_ORDER:
        if role not in normalized.plugins:
            continue
        spec = normalized.plugins[role]
        spec_error = _plugin_spec_error(spec)
        if spec_error is not None:
            issues.append(PluginValidationIssue(role=role, message=spec_error))
            continue
        if not import_plugins:
            validated_roles.append(role)
            continue
        try:
            plugin = _build_generated_plugin(
                role,
                spec,
                target=normalized.target,
                config=config,
                comparator=comparator_plugin,
            )
            if role == "ref_model":
                validate_ref_model_plugin(plugin, spec=spec)
            elif role == "comparator":
                comparator_plugin = validate_comparator_plugin(plugin, spec=spec)
            elif role == "scoreboard":
                validate_scoreboard_plugin(plugin, spec=spec)
            elif role == "coverage_model":
                validate_coverage_plugin(plugin, spec=spec)
            validated_roles.append(role)
        except Exception as exc:  # noqa: BLE001 - report should retain plugin failure detail
            issues.append(
                PluginValidationIssue(
                    role=role,
                    message=f"{type(exc).__name__}: {exc}",
                )
            )

    valid = not any(issue.severity == "error" for issue in issues)
    return GeneratedPluginValidationReport(
        target=normalized.target,
        plugins=normalized.plugins,
        valid=valid,
        validated_roles=tuple(validated_roles),
        issues=tuple(issues),
        metadata={
            "import_plugins": import_plugins,
            "source_metadata": dict(normalized.metadata),
        },
    )


def build_plugin_registry(
    report: GeneratedPluginValidationReport | Mapping[str, Any],
) -> GeneratedPluginRegistry:
    normalized = normalize_plugin_validation_report(report)
    if not normalized.valid:
        raise PluginContractError("cannot build plugin registry from invalid validation report")
    return GeneratedPluginRegistry(
        target=normalized.target,
        plugins={
            role: spec
            for role, spec in normalized.plugins.items()
            if role in normalized.validated_roles
        },
        metadata={
            "validated_roles": list(normalized.validated_roles),
            "validation_metadata": dict(normalized.metadata),
        },
    )


def build_manifest_overlay(
    registry: GeneratedPluginRegistry | Mapping[str, Any],
) -> TargetManifestOverlay:
    normalized = normalize_plugin_registry(registry)
    return TargetManifestOverlay(
        target=normalized.target,
        updates=dict(normalized.plugins),
        metadata={"registry_metadata": dict(normalized.metadata)},
    )


def apply_manifest_overlay(
    config: TargetConfig,
    overlay: TargetManifestOverlay | Mapping[str, Any],
) -> TargetConfig:
    normalized = normalize_manifest_overlay(overlay)
    if normalized.target and normalized.target != config.name:
        raise PluginContractError(
            f"manifest overlay target {normalized.target!r} does not match config {config.name!r}"
        )
    unknown = sorted(set(normalized.updates) - set(PLUGIN_MANIFEST_FIELDS))
    if unknown:
        raise PluginContractError(
            f"manifest overlay contains unsupported field(s): {', '.join(unknown)}"
        )
    return replace(config, **dict(normalized.updates))


def generated_plugin_bundle_metrics(value: Any) -> dict[str, Any]:
    bundle = normalize_generated_plugin_bundle(value)
    return {
        "target": bundle.target,
        "plugin_count": len(bundle.plugins),
        "roles": sorted(bundle.plugins),
    }


def plugin_validation_report_metrics(value: Any) -> dict[str, Any]:
    report = normalize_plugin_validation_report(value)
    return {
        "target": report.target,
        "valid": report.valid,
        "validated_role_count": len(report.validated_roles),
        "issue_count": len(report.issues),
        "roles": list(report.validated_roles),
    }


def plugin_registry_metrics(value: Any) -> dict[str, Any]:
    registry = normalize_plugin_registry(value)
    return {
        "target": registry.target,
        "plugin_count": len(registry.plugins),
        "roles": sorted(registry.plugins),
    }


def manifest_overlay_metrics(value: Any) -> dict[str, Any]:
    overlay = normalize_manifest_overlay(value)
    return {
        "target": overlay.target,
        "update_count": len(overlay.updates),
        "fields": sorted(overlay.updates),
    }


def write_generated_plugin_json(path: Path | str, value: Any) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.to_json() if hasattr(value, "to_json") else value
    ensure_json_serializable(payload, spec=str(output_path))
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def read_generated_plugin_json(path: Path | str) -> dict[str, Any]:
    input_path = Path(path)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PluginContractError(f"{input_path}: generated plugin artifact must be a JSON object")
    return payload


def load_generated_plugin_bundle(path: Path | str) -> GeneratedPluginBundle:
    return normalize_generated_plugin_bundle(read_generated_plugin_json(path))


def load_plugin_validation_report(path: Path | str) -> GeneratedPluginValidationReport:
    return normalize_plugin_validation_report(read_generated_plugin_json(path))


def load_plugin_registry(path: Path | str) -> GeneratedPluginRegistry:
    return normalize_plugin_registry(read_generated_plugin_json(path))


def load_manifest_overlay(path: Path | str) -> TargetManifestOverlay:
    return normalize_manifest_overlay(read_generated_plugin_json(path))


def _build_generated_plugin(
    role: str,
    spec: str,
    *,
    target: str,
    config: TargetConfig | None,
    comparator: Any,
) -> Any:
    from fuzz_bfm.plugin_loader import build_plugin

    kwargs = {"target": target, "config": config}
    if role == "scoreboard" and comparator is not None:
        kwargs["comparator"] = comparator
    return build_plugin(spec, **kwargs)


def _plugin_spec_error(spec: Any) -> str | None:
    if not isinstance(spec, str) or not spec.strip():
        return "plugin spec must be a non-empty string"
    module_name, sep, object_name = spec.partition(":")
    if not sep or not module_name or not object_name:
        return f"plugin spec must be 'module:Object', got {spec!r}"
    return None


def _string_mapping(value: Any, *, spec: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise PluginContractError(f"{spec} must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        result[str(key)] = str(item)
    return result


def _json_mapping(value: Any, *, spec: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PluginContractError(f"{spec} must be a mapping")
    result = dict(value)
    ensure_json_serializable(result, spec=spec)
    return result


def _bool_value(value: Any, *, spec: str) -> bool:
    if not isinstance(value, bool):
        raise PluginContractError(f"{spec} must be a bool")
    return value


def _normalize_issue(value: Any) -> PluginValidationIssue:
    if isinstance(value, PluginValidationIssue):
        return value
    if not isinstance(value, Mapping):
        raise PluginContractError("validation issue must be a mapping")
    return PluginValidationIssue(
        role=str(value.get("role", "")),
        message=str(value.get("message", "")),
        severity=str(value.get("severity", "error")),
    )


__all__ = [
    "PLUGIN_MANIFEST_FIELDS",
    "GeneratedPluginBundle",
    "GeneratedPluginRegistry",
    "GeneratedPluginValidationReport",
    "PluginValidationIssue",
    "TargetManifestOverlay",
    "apply_manifest_overlay",
    "build_manifest_overlay",
    "build_plugin_registry",
    "generated_plugin_bundle_metrics",
    "load_generated_plugin_bundle",
    "load_manifest_overlay",
    "load_plugin_registry",
    "load_plugin_validation_report",
    "manifest_overlay_metrics",
    "normalize_generated_plugin_bundle",
    "normalize_manifest_overlay",
    "normalize_plugin_registry",
    "normalize_plugin_validation_report",
    "plugin_registry_metrics",
    "plugin_validation_report_metrics",
    "read_generated_plugin_json",
    "validate_generated_plugin_bundle",
    "write_generated_plugin_json",
]
