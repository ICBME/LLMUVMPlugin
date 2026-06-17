from __future__ import annotations

from typing import Any, Callable

from .plugins import HarnessActionPlugin, HarnessPluginRegistry


PayloadValidator = Callable[[dict[str, Any], str], list[dict[str, str]]]

SCOREBOARD_CHECK_MODES = {
    "actual_equals_expected",
    "record_seen",
    "require_no_error",
    "field_equals",
    "field_range",
}

BUILTIN_DSL_PAYLOAD_ACTION_TYPES = (
    "replay_probe",
    "scoreboard_check",
    "coverage_feedback_tuning",
    "mmio_readback",
)


def builtin_safe_action_dsl_schema() -> dict[str, Any]:
    return {
        "replay_probe": {
            "payload_fields": {
                "fields": "string or list of case./result. field names",
                "signals": "string or list of DUT signal names",
                "probe": "single case/result field alias",
                "sample_on": "optional sampling point string",
                "max_samples": "optional non-negative integer",
                "case_filter": "optional object for future filtering",
            },
            "requires_any": ["fields", "signals", "probe"],
        },
        "scoreboard_check": {
            "modes": sorted(SCOREBOARD_CHECK_MODES),
            "payload_fields": {
                "mode": "check mode",
                "check": "human readable check description",
                "field": "record/result/case field for field modes",
                "expected": "expected value for field_equals",
                "min": "minimum numeric value for field_range",
                "max": "maximum numeric value for field_range",
                "enforce": "bool, fail candidate when check fails",
            },
        },
        "coverage_feedback_tuning": {
            "payload_fields": {
                "max_gap_count": "optional non-negative integer",
                "directive_weight_multiplier": "optional positive number",
                "prioritize": "optional priority label",
                "gap_type": "optional gap category",
                "directive_source": "optional directive source filter",
                "min_weight": "optional directive weight floor",
                "max_weight": "optional directive weight ceiling",
            },
        },
        "mmio_readback": {
            "payload_fields": {
                "registers": "string or list of symbolic register names",
                "addresses": "integer, hex string, or list of addresses",
                "sample_on": "optional sampling point string",
                "max_reads": "optional non-negative integer per run",
                "case_filter": "optional object matching case fields",
            },
            "requires_any": ["registers", "addresses"],
        },
    }


def builtin_action_payload_validators() -> dict[str, PayloadValidator]:
    return {
        "replay_probe": replay_probe_payload_errors,
        "scoreboard_check": scoreboard_check_payload_errors,
        "coverage_feedback_tuning": coverage_feedback_tuning_payload_errors,
        "mmio_readback": mmio_readback_payload_errors,
    }


def builtin_action_plugin(
    action_type: str,
    *,
    adapter_kind: str | None = None,
    artifact_role: str | None = None,
    make_var: str | None = None,
    runtime_action: bool = False,
    safe_for_sandbox: bool = True,
) -> HarnessActionPlugin:
    schema = builtin_safe_action_dsl_schema()
    validators = builtin_action_payload_validators()
    try:
        dsl_schema = schema[action_type]
        payload_validator = validators[action_type]
    except KeyError as exc:
        raise ValueError(f"unknown builtin action type: {action_type!r}") from exc
    return HarnessActionPlugin(
        action_type=action_type,
        safe_for_sandbox=safe_for_sandbox,
        payload_required=True,
        dsl_schema=dsl_schema,
        payload_validator=payload_validator,
        adapter_kind=adapter_kind,
        artifact_role=artifact_role,
        make_var=make_var,
        runtime_action=runtime_action,
    )


def builtin_action_plugin_registry() -> HarnessPluginRegistry:
    registry = HarnessPluginRegistry()
    for action_type in BUILTIN_DSL_PAYLOAD_ACTION_TYPES:
        registry = registry.with_action_plugin(builtin_action_plugin(action_type))
    return registry


def replay_probe_payload_errors(
    payload: dict[str, Any],
    path: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    fields = payload.get("fields")
    signals = payload.get("signals")
    probe = payload.get("probe")
    if fields is None and signals is None and probe is None:
        errors.append(
            {
                "path": path,
                "message": "replay_probe payload requires fields, signals, or probe",
            }
        )
    if fields is not None and not _is_string_or_string_list(fields):
        errors.append({"path": f"{path}.fields", "message": "expected string or list"})
    if signals is not None and not _is_string_or_string_list(signals):
        errors.append({"path": f"{path}.signals", "message": "expected string or list"})
    if probe is not None and not isinstance(probe, str):
        errors.append({"path": f"{path}.probe", "message": "expected string"})
    if "sample_on" in payload and not isinstance(payload["sample_on"], str):
        errors.append({"path": f"{path}.sample_on", "message": "expected string"})
    if "max_samples" in payload and not _non_negative_int(payload["max_samples"]):
        errors.append(
            {"path": f"{path}.max_samples", "message": "expected non-negative integer"}
        )
    if "case_filter" in payload and not isinstance(payload["case_filter"], dict):
        errors.append({"path": f"{path}.case_filter", "message": "expected object"})
    return errors


def scoreboard_check_payload_errors(
    payload: dict[str, Any],
    path: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    mode = str(payload.get("mode") or "actual_equals_expected")
    if "mode" in payload and mode not in SCOREBOARD_CHECK_MODES:
        errors.append(
            {
                "path": f"{path}.mode",
                "message": f"expected one of {sorted(SCOREBOARD_CHECK_MODES)}",
            }
        )
    if "mode" not in payload and "check" not in payload:
        errors.append({"path": path, "message": "requires check or mode"})
    if "check" in payload and not isinstance(payload["check"], str):
        errors.append({"path": f"{path}.check", "message": "expected string"})
    if "enforce" in payload and not isinstance(payload["enforce"], bool):
        errors.append({"path": f"{path}.enforce", "message": "expected bool"})
    if mode in {"field_equals", "field_range"} and not isinstance(
        payload.get("field"),
        str,
    ):
        errors.append({"path": f"{path}.field", "message": "expected string"})
    if mode == "field_equals" and "expected" not in payload:
        errors.append({"path": f"{path}.expected", "message": "required"})
    if mode == "field_range":
        if "min" not in payload and "max" not in payload:
            errors.append({"path": path, "message": "field_range requires min or max"})
        for key in ("min", "max"):
            if key in payload and _number_value(payload[key]) is None:
                errors.append({"path": f"{path}.{key}", "message": "expected number"})
    return errors


def coverage_feedback_tuning_payload_errors(
    payload: dict[str, Any],
    path: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    known_keys = {
        "max_gap_count",
        "directive_weight_multiplier",
        "prioritize",
        "gap_type",
        "directive_source",
        "min_weight",
        "max_weight",
    }
    if not any(key in payload for key in known_keys):
        errors.append({"path": path, "message": "requires at least one tuning field"})
    if "max_gap_count" in payload and not _non_negative_int(payload["max_gap_count"]):
        errors.append(
            {
                "path": f"{path}.max_gap_count",
                "message": "expected non-negative integer",
            }
        )
    if "directive_weight_multiplier" in payload and not _positive_number(
        payload["directive_weight_multiplier"]
    ):
        errors.append(
            {
                "path": f"{path}.directive_weight_multiplier",
                "message": "expected positive number",
            }
        )
    for key in ("prioritize", "gap_type", "directive_source"):
        if key in payload and not isinstance(payload[key], str):
            errors.append({"path": f"{path}.{key}", "message": "expected string"})
    for key in ("min_weight", "max_weight"):
        if key in payload and _number_value(payload[key]) is None:
            errors.append({"path": f"{path}.{key}", "message": "expected number"})
    if (
        "min_weight" in payload
        and "max_weight" in payload
        and _number_value(payload["min_weight"]) is not None
        and _number_value(payload["max_weight"]) is not None
        and _number_value(payload["min_weight"]) > _number_value(payload["max_weight"])
    ):
        errors.append(
            {
                "path": path,
                "message": "min_weight must be <= max_weight",
            }
        )
    return errors


def mmio_readback_payload_errors(
    payload: dict[str, Any],
    path: str,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    registers = payload.get("registers")
    addresses = payload.get("addresses")
    if registers is None and addresses is None:
        errors.append(
            {"path": path, "message": "mmio_readback requires registers or addresses"}
        )
    if registers is not None and not _is_string_or_string_list(registers):
        errors.append(
            {"path": f"{path}.registers", "message": "expected string or list"}
        )
    if addresses is not None and not _is_address_or_address_list(addresses):
        errors.append(
            {
                "path": f"{path}.addresses",
                "message": "expected integer, hex string, or list",
            }
        )
    if "sample_on" in payload and not isinstance(payload["sample_on"], str):
        errors.append({"path": f"{path}.sample_on", "message": "expected string"})
    if "max_reads" in payload and not _non_negative_int(payload["max_reads"]):
        errors.append(
            {"path": f"{path}.max_reads", "message": "expected non-negative integer"}
        )
    if "case_filter" in payload and not isinstance(payload["case_filter"], dict):
        errors.append({"path": f"{path}.case_filter", "message": "expected object"})
    return errors


def _is_string_or_string_list(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


def _is_address_or_address_list(value: Any) -> bool:
    if isinstance(value, list):
        return all(_is_address_value(item) for item in value)
    return _is_address_value(value)


def _is_address_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, str):
        try:
            return int(value, 0) >= 0
        except ValueError:
            return False
    return False


def _non_negative_int(value: Any) -> bool:
    number = _number_value(value)
    return (
        number is not None
        and int(number) == number
        and number >= 0
        and not isinstance(value, bool)
    )


def _positive_number(value: Any) -> bool:
    number = _number_value(value)
    return number is not None and number > 0


def _number_value(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None


__all__ = [
    "BUILTIN_DSL_PAYLOAD_ACTION_TYPES",
    "PayloadValidator",
    "SCOREBOARD_CHECK_MODES",
    "builtin_action_payload_validators",
    "builtin_action_plugin",
    "builtin_action_plugin_registry",
    "builtin_safe_action_dsl_schema",
    "coverage_feedback_tuning_payload_errors",
    "mmio_readback_payload_errors",
    "replay_probe_payload_errors",
    "scoreboard_check_payload_errors",
]
