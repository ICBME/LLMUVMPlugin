from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import import_module
import inspect
from typing import Any, Callable, Protocol


REGISTRY_SNAPSHOT_KIND = "libafl_bfm_fuzz.harness_plugin_registry"
REGISTRY_PROVENANCE_KIND = "libafl_bfm_fuzz.harness_plugin_provenance"
REGISTRY_VALIDATION_KIND = "libafl_bfm_fuzz.harness_plugin_contract_validation"

PayloadValidator = Callable[[dict[str, Any], str], list[dict[str, str]]]
GapActionabilityClassifier = Callable[
    [dict[str, Any], "HarnessGapActionabilityContext"],
    dict[str, Any] | None,
]


@dataclass(frozen=True)
class HarnessActionPlugin:
    action_type: str
    safe_for_sandbox: bool = True
    payload_required: bool = False
    dsl_schema: Mapping[str, Any] = field(default_factory=dict)
    payload_validator: PayloadValidator | None = None
    adapter_kind: str | None = None
    artifact_role: str | None = None
    make_var: str | None = None
    runtime_action: bool = False

    def adapter_config(self) -> tuple[str, str, str] | None:
        if (
            self.adapter_kind is None
            or self.artifact_role is None
            or self.make_var is None
        ):
            return None
        return (self.adapter_kind, self.artifact_role, self.make_var)


@dataclass(frozen=True)
class HarnessGapActionabilityContext:
    target: str
    task: Mapping[str, Any] = field(default_factory=dict)
    proposal: Mapping[str, Any] = field(default_factory=dict)
    candidate_manifest: Mapping[str, Any] = field(default_factory=dict)
    baseline_metrics: Mapping[str, Any] = field(default_factory=dict)
    candidate_metrics: Mapping[str, Any] = field(default_factory=dict)


class HarnessPluginBundle(Protocol):
    def register_harness_plugins(
        self,
        registry: "HarnessPluginRegistry",
    ) -> "HarnessPluginRegistry | HarnessActionPlugin | Iterable[Any] | None":
        ...


@dataclass(frozen=True)
class HarnessPluginRegistry:
    action_plugins: Mapping[str, HarnessActionPlugin] = field(default_factory=dict)
    gap_actionability_classifiers: tuple[GapActionabilityClassifier, ...] = ()
    plugin_specs: tuple[str, ...] = ()

    def with_action_plugin(
        self,
        plugin: HarnessActionPlugin,
    ) -> "HarnessPluginRegistry":
        plugins = dict(self.action_plugins)
        plugins[plugin.action_type] = plugin
        return HarnessPluginRegistry(
            action_plugins=plugins,
            gap_actionability_classifiers=self.gap_actionability_classifiers,
            plugin_specs=self.plugin_specs,
        )

    def with_gap_actionability_classifier(
        self,
        classifier: GapActionabilityClassifier,
    ) -> "HarnessPluginRegistry":
        return HarnessPluginRegistry(
            action_plugins=dict(self.action_plugins),
            gap_actionability_classifiers=(
                *self.gap_actionability_classifiers,
                classifier,
            ),
            plugin_specs=self.plugin_specs,
        )

    def with_plugin_spec(self, spec: str) -> "HarnessPluginRegistry":
        return HarnessPluginRegistry(
            action_plugins=dict(self.action_plugins),
            gap_actionability_classifiers=self.gap_actionability_classifiers,
            plugin_specs=(*self.plugin_specs, spec),
        )

    def merge(self, other: "HarnessPluginRegistry") -> "HarnessPluginRegistry":
        registry = self
        for plugin in other.action_plugins.values():
            registry = registry.with_action_plugin(plugin)
        for classifier in other.gap_actionability_classifiers:
            registry = registry.with_gap_actionability_classifier(classifier)
        for spec in other.plugin_specs:
            registry = registry.with_plugin_spec(spec)
        return registry

    def with_plugin_bundle(self, bundle: Any) -> "HarnessPluginRegistry":
        if bundle is None:
            return self
        if isinstance(bundle, HarnessPluginRegistry):
            return self.merge(bundle)
        if isinstance(bundle, HarnessActionPlugin):
            return self.with_action_plugin(bundle)
        registrar = getattr(bundle, "register_harness_plugins", None)
        if callable(registrar):
            registered = registrar(self)
            return self if registered is None else self.with_plugin_bundle(registered)
        registry_factory = getattr(bundle, "harness_plugin_registry", None)
        if callable(registry_factory):
            return self.with_plugin_bundle(registry_factory())
        registry_attr = getattr(bundle, "plugin_registry", None)
        if registry_attr is not None:
            return self.with_plugin_bundle(registry_attr)
        action_plugins = getattr(bundle, "action_plugins", None)
        classifiers = getattr(bundle, "gap_actionability_classifiers", None)
        if action_plugins is not None or classifiers is not None:
            registry = self
            for plugin in _iter_plugin_values(action_plugins):
                if not isinstance(plugin, HarnessActionPlugin):
                    raise TypeError(
                        "action_plugins entries must be HarnessActionPlugin objects"
                    )
                registry = registry.with_action_plugin(plugin)
            for classifier in _iter_plugin_values(classifiers):
                if not callable(classifier):
                    raise TypeError(
                        "gap_actionability_classifiers entries must be callable"
                    )
                registry = registry.with_gap_actionability_classifier(classifier)
            return registry
        classifier = getattr(bundle, "classify_gap_actionability", None)
        if callable(classifier):
            return self.with_gap_actionability_classifier(classifier)
        if isinstance(bundle, Iterable) and not isinstance(bundle, (str, bytes, dict)):
            registry = self
            for item in bundle:
                registry = registry.with_plugin_bundle(item)
            return registry
        raise TypeError(
            "harness plugin bundle must be a registry, action plugin, iterable, "
            "or object with register_harness_plugins()"
        )

    def action_plugin(self, action_type: str) -> HarnessActionPlugin | None:
        return self.action_plugins.get(action_type)

    def allowed_action_types(self) -> tuple[str, ...]:
        return tuple(self.action_plugins)

    def safe_sandbox_action_types(self) -> tuple[str, ...]:
        return tuple(
            action_type
            for action_type, plugin in self.action_plugins.items()
            if plugin.safe_for_sandbox
        )

    def unsafe_action_types(self) -> tuple[str, ...]:
        return tuple(
            action_type
            for action_type, plugin in self.action_plugins.items()
            if not plugin.safe_for_sandbox
        )

    def dsl_payload_action_types(self) -> tuple[str, ...]:
        return tuple(
            action_type
            for action_type, plugin in self.action_plugins.items()
            if plugin.payload_required
        )

    def runtime_action_types(self) -> tuple[str, ...]:
        return tuple(
            action_type
            for action_type, plugin in self.action_plugins.items()
            if plugin.runtime_action
        )

    def safe_action_dsl_schema(self) -> dict[str, Any]:
        return {
            action_type: dict(plugin.dsl_schema)
            for action_type, plugin in self.action_plugins.items()
            if isinstance(plugin.dsl_schema, Mapping) and plugin.dsl_schema
        }

    def action_payload_errors(
        self,
        action_type: str,
        payload: dict[str, Any],
        *,
        path: str,
    ) -> list[dict[str, str]]:
        plugin = self.action_plugin(action_type)
        if plugin is None or plugin.payload_validator is None:
            return []
        return plugin.payload_validator(payload, path)

    def adapter_config_for(self, action_type: str) -> tuple[str, str, str] | None:
        plugin = self.action_plugin(action_type)
        return plugin.adapter_config() if plugin is not None else None

    def classify_gap_actionability(
        self,
        gap: dict[str, Any],
        context: HarnessGapActionabilityContext,
    ) -> dict[str, Any]:
        for classifier in self.gap_actionability_classifiers:
            result = classifier(dict(gap), context)
            if result:
                return normalize_gap_actionability(gap, result)
        return unknown_gap_actionability(gap)

    def to_json(self) -> dict[str, Any]:
        summary = plugin_registry_summary(self)
        return {
            "schema_version": 1,
            "kind": REGISTRY_SNAPSHOT_KIND,
            "snapshot_schema": harness_plugin_registry_snapshot_schema(),
            "summary": summary,
            "plugin_specs": list(self.plugin_specs),
            "actions": [
                {
                    "action_type": plugin.action_type,
                    "safe_for_sandbox": plugin.safe_for_sandbox,
                    "payload_required": plugin.payload_required,
                    "runtime_action": plugin.runtime_action,
                    "adapter_kind": plugin.adapter_kind,
                    "artifact_role": plugin.artifact_role,
                    "make_var": plugin.make_var,
                    "has_payload_validator": plugin.payload_validator is not None,
                    "has_dsl_schema": bool(plugin.dsl_schema),
                }
                for plugin in self.action_plugins.values()
            ],
            "gap_actionability_classifiers": [
                {"name": callable_name(classifier)}
                for classifier in self.gap_actionability_classifiers
            ],
            "gap_actionability_classifier_count": len(
                self.gap_actionability_classifiers
            ),
            "validation": validate_harness_plugin_registry(self),
        }

    def provenance_json(self) -> dict[str, Any]:
        return harness_plugin_provenance(self)

    def validation_json(self) -> dict[str, Any]:
        return validate_harness_plugin_registry(self)


def plugin_registry_summary(registry: HarnessPluginRegistry) -> dict[str, int]:
    runtime_action_count = len(registry.runtime_action_types())
    safe_action_count = len(registry.safe_sandbox_action_types())
    action_count = len(registry.action_plugins)
    return {
        "plugin_spec_count": len(registry.plugin_specs),
        "action_count": action_count,
        "safe_action_count": safe_action_count,
        "unsafe_action_count": action_count - safe_action_count,
        "runtime_action_count": runtime_action_count,
        "dsl_payload_action_count": len(registry.dsl_payload_action_types()),
        "gap_actionability_classifier_count": len(
            registry.gap_actionability_classifiers
        ),
    }


def harness_plugin_registry_snapshot_schema() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": f"{REGISTRY_SNAPSHOT_KIND}.schema",
        "required_fields": [
            "schema_version",
            "kind",
            "summary",
            "plugin_specs",
            "actions",
            "gap_actionability_classifiers",
            "validation",
        ],
        "action_fields": [
            "action_type",
            "safe_for_sandbox",
            "payload_required",
            "runtime_action",
            "adapter_kind",
            "artifact_role",
            "make_var",
            "has_payload_validator",
            "has_dsl_schema",
        ],
    }


def harness_plugin_provenance(registry: HarnessPluginRegistry) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": REGISTRY_PROVENANCE_KIND,
        "plugin_specs": list(registry.plugin_specs),
        "summary": plugin_registry_summary(registry),
        "loaded_action_types": list(registry.allowed_action_types()),
        "runtime_action_types": list(registry.runtime_action_types()),
        "gap_actionability_classifiers": [
            {"name": callable_name(classifier)}
            for classifier in registry.gap_actionability_classifiers
        ],
        "validation": validate_harness_plugin_registry(registry),
    }


def validate_harness_plugin_registry(
    registry: HarnessPluginRegistry,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for action_type, plugin in registry.action_plugins.items():
        path = f"actions.{action_type}"
        if not isinstance(action_type, str) or not action_type:
            errors.append(
                {"path": path, "message": "action type key must be non-empty"}
            )
        if plugin.action_type != action_type:
            errors.append(
                {
                    "path": f"{path}.action_type",
                    "message": "action type key must match plugin.action_type",
                }
            )
        if not isinstance(plugin.action_type, str) or not plugin.action_type:
            errors.append(
                {"path": f"{path}.action_type", "message": "must be non-empty"}
            )
        elif not _valid_action_type(plugin.action_type):
            errors.append(
                {
                    "path": f"{path}.action_type",
                    "message": (
                        "must contain only letters, digits, underscore, dash, dot, "
                        "or colon"
                    ),
                }
            )
        if plugin.payload_required:
            if not isinstance(plugin.dsl_schema, Mapping) or not plugin.dsl_schema:
                errors.append(
                    {
                        "path": f"{path}.dsl_schema",
                        "message": "payload-required actions must define a DSL schema",
                    }
                )
            if plugin.payload_validator is None:
                warnings.append(
                    {
                        "path": f"{path}.payload_validator",
                        "message": (
                            "payload-required action has no payload validator; "
                            "schema-only checks may be weak"
                        ),
                    }
                )
        elif plugin.dsl_schema and not isinstance(plugin.dsl_schema, Mapping):
            errors.append(
                {"path": f"{path}.dsl_schema", "message": "must be a mapping"}
            )
        if plugin.payload_validator is not None and not callable(
            plugin.payload_validator
        ):
            errors.append(
                {
                    "path": f"{path}.payload_validator",
                    "message": "must be callable when provided",
                }
            )
        adapter_fields = {
            "adapter_kind": plugin.adapter_kind,
            "artifact_role": plugin.artifact_role,
            "make_var": plugin.make_var,
        }
        present_adapter_fields = {
            name: value for name, value in adapter_fields.items() if value is not None
        }
        if present_adapter_fields and len(present_adapter_fields) != len(adapter_fields):
            errors.append(
                {
                    "path": path,
                    "message": (
                        "adapter_kind, artifact_role, and make_var must be "
                        "provided together"
                    ),
                }
            )
        for name, value in present_adapter_fields.items():
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    {
                        "path": f"{path}.{name}",
                        "message": "must be a non-empty string when provided",
                    }
                )
        if plugin.runtime_action and plugin.adapter_config() is None:
            warnings.append(
                {
                    "path": path,
                    "message": (
                        "runtime action has no adapter config; candidate artifacts "
                        "must be supplied by a custom path"
                    ),
                }
            )
    for index, classifier in enumerate(registry.gap_actionability_classifiers):
        if not callable(classifier):
            errors.append(
                {
                    "path": f"gap_actionability_classifiers.{index}",
                    "message": "classifier must be callable",
                }
            )
    return {
        "schema_version": 1,
        "kind": REGISTRY_VALIDATION_KIND,
        "valid": not errors,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }


def callable_name(value: Any) -> str:
    module = getattr(value, "__module__", "")
    qualname = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    if module and qualname:
        return f"{module}.{qualname}"
    if qualname:
        return str(qualname)
    return type(value).__name__


def _valid_action_type(value: str) -> bool:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:")
    return bool(value) and all(char in allowed for char in value)


def unknown_gap_actionability(gap: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(gap)
    result["actionability"] = "unknown"
    result["actionability_reason"] = "No registered actionability rule matched."
    result["recommended_action_type"] = None
    result["suggested_payload"] = {}
    return result


def normalize_gap_actionability(
    original_gap: Mapping[str, Any],
    classified_gap: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(original_gap)
    result.update(dict(classified_gap))
    result.setdefault("actionability", "unknown")
    result.setdefault("actionability_reason", "No actionability reason provided.")
    result.setdefault("recommended_action_type", None)
    result.setdefault("suggested_payload", {})
    return result


def load_harness_plugin_registry(
    specs: Iterable[str],
    *,
    base_registry: HarnessPluginRegistry,
    **kwargs: Any,
) -> HarnessPluginRegistry:
    registry = base_registry
    for raw_spec in specs:
        spec = str(raw_spec).strip()
        if not spec:
            continue
        plugin = build_harness_plugin(spec, registry=registry, **kwargs)
        registry = registry.with_plugin_bundle(plugin).with_plugin_spec(spec)
    return registry


def build_harness_plugin(spec: str, **kwargs: Any) -> Any:
    return _build_loaded_plugin(_load_object(spec), **kwargs)


def _load_object(spec: str) -> Any:
    module_name, sep, object_name = spec.partition(":")
    if not sep or not module_name or not object_name:
        raise ValueError(f"plugin spec must be 'module:Object', got {spec!r}")
    module = import_module(module_name)
    obj: Any = module
    for part in object_name.split("."):
        obj = getattr(obj, part)
    return obj


def _build_loaded_plugin(plugin_obj: Any, **kwargs: Any) -> Any:
    if isinstance(plugin_obj, HarnessPluginRegistry):
        return plugin_obj
    if isinstance(plugin_obj, HarnessActionPlugin):
        return plugin_obj
    if not callable(plugin_obj):
        return plugin_obj
    try:
        signature = inspect.signature(plugin_obj)
    except (TypeError, ValueError):
        return plugin_obj()
    parameters = signature.parameters
    accepts_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in parameters.values()
    )
    call_kwargs = dict(kwargs)
    if not accepts_kwargs:
        call_kwargs = {
            name: value for name, value in call_kwargs.items() if name in parameters
        }
    if call_kwargs or _requires_no_required_args(parameters):
        return plugin_obj(**call_kwargs)
    return plugin_obj()


def _requires_no_required_args(
    parameters: Mapping[str, inspect.Parameter],
) -> bool:
    for parameter in parameters.values():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if parameter.default is inspect.Parameter.empty:
            return False
    return True


def _iter_plugin_values(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return value.values()
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return value
    return (value,)
