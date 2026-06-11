from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


REPLAY_PROBE_CONFIG_ENV = "HARNESS_REPLAY_PROBE_CONFIG"
SCOREBOARD_CHECK_CONFIG_ENV = "HARNESS_SCOREBOARD_CHECK_CONFIG"
COVERAGE_FEEDBACK_TUNING_CONFIG_ENV = "HARNESS_COVERAGE_FEEDBACK_TUNING_CONFIG"
RUNTIME_METRICS_OUT_ENV = "HARNESS_RUNTIME_METRICS_OUT"


@dataclass(frozen=True)
class RuntimeActionEntry:
    action_id: str
    action_type: str
    payload: dict[str, Any]
    evidence_refs: tuple[dict[str, Any], ...] = ()
    artifact_path: str | None = None
    rationale: Any = None


@dataclass(frozen=True)
class RuntimeActionConfig:
    action_type: str
    path: Path
    entries: tuple[RuntimeActionEntry, ...]


def load_runtime_action_config_from_env(
    env_var: str,
    *,
    action_type: str,
) -> RuntimeActionConfig | None:
    value = os.getenv(env_var)
    if value is None or not value.strip():
        return None
    return load_runtime_action_config(Path(value), action_type=action_type)


def load_runtime_action_config(
    path: Path,
    *,
    action_type: str,
) -> RuntimeActionConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: runtime action config must be a JSON object")
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"{path}: runtime action config requires an entries array")
    entries = tuple(
        _entry_from_json(item, action_type=action_type, path=path, index=index)
        for index, item in enumerate(raw_entries)
    )
    return RuntimeActionConfig(action_type=action_type, path=path, entries=entries)


def runtime_metrics_path_from_env() -> Path | None:
    value = os.getenv(RUNTIME_METRICS_OUT_ENV)
    return Path(value) if value is not None and value.strip() else None


def extra_make_var_value(values: Iterable[str], name: str) -> str | None:
    prefix = f"{name}="
    for item in values:
        text = str(item)
        if text.startswith(prefix):
            value = text[len(prefix) :]
            return value if value else None
    return None


def merge_runtime_metrics(
    path: Path | None,
    section: str,
    metrics: Mapping[str, Any],
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    if path is None:
        return {"sections": {section: dict(metrics)}, "summary": dict(metrics)}
    payload = read_runtime_metrics(path)
    sections = payload.setdefault("sections", {})
    if not isinstance(sections, dict):
        sections = {}
        payload["sections"] = sections
    value = dict(metrics)
    if config_path is not None:
        value["config"] = str(config_path)
    sections[section] = value
    payload["summary"] = runtime_metrics_summary(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def read_runtime_metrics(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {
            "schema_version": 1,
            "kind": "libafl_bfm_fuzz.harness_runtime_action_metrics",
            "sections": {},
            "summary": {},
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def runtime_metrics_summary(payload: Mapping[str, Any]) -> dict[str, int | float]:
    summary: dict[str, int | float] = {}
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        return summary
    for section, metrics in sections.items():
        if not isinstance(metrics, dict):
            continue
        for key, value in metrics.items():
            if key == "config":
                continue
            number = _number(value)
            if number is not None:
                summary[f"{section}_{key}"] = number
    return summary


class ReplayProbeRuntime:
    def __init__(
        self,
        config: RuntimeActionConfig | None,
        *,
        metrics_out: Path | None = None,
    ):
        self.config = config
        self.metrics_out = metrics_out
        self.sample_count = 0
        self.field_sample_count = 0
        self.signal_request_count = 0
        self.samples: list[dict[str, Any]] = []
        self.action_metrics = _initial_action_metrics(
            config,
            {
                "sample_count": 0,
                "field_sample_count": 0,
                "signal_request_count": 0,
                "unavailable_signal_count": 0,
            },
        )

    @classmethod
    def from_env(cls) -> "ReplayProbeRuntime":
        return cls(
            load_runtime_action_config_from_env(
                REPLAY_PROBE_CONFIG_ENV,
                action_type="replay_probe",
            ),
            metrics_out=runtime_metrics_path_from_env(),
        )

    def sample(self, *, index: int, case: Any, result: Any) -> None:
        if self.config is None:
            return
        for entry in self.config.entries:
            payload = entry.payload
            fields = _probe_fields(payload)
            signals = _string_list(payload.get("signals"), field="signals")
            values = {
                field: _probe_value(field, case=case, result=result)
                for field in fields
            }
            self.sample_count += 1
            self.field_sample_count += len(values)
            self.signal_request_count += len(signals)
            action_metrics = self._action_metrics(entry)
            action_metrics["sample_count"] += 1
            action_metrics["field_sample_count"] += len(values)
            action_metrics["signal_request_count"] += len(signals)
            action_metrics["unavailable_signal_count"] += len(signals)
            if len(self.samples) < 32:
                self.samples.append(
                    {
                        "action_id": entry.action_id,
                        "case_index": index,
                        "fields": values,
                        "signals": [
                            {"name": signal, "status": "unavailable_in_python_runtime"}
                            for signal in signals
                        ],
                    }
                )
        self.flush()

    def metrics(self) -> dict[str, Any]:
        configured = len(self.config.entries) if self.config is not None else 0
        return {
            "configured_count": configured,
            "sample_count": self.sample_count,
            "field_sample_count": self.field_sample_count,
            "signal_request_count": self.signal_request_count,
            "unavailable_signal_count": self.signal_request_count,
            "sample_preview_count": len(self.samples),
            "actions": _sorted_action_metrics(self.action_metrics),
        }

    def _action_metrics(self, entry: RuntimeActionEntry) -> dict[str, Any]:
        return self.action_metrics.setdefault(
            entry.action_id,
            _action_metric_entry(
                entry,
                {
                    "sample_count": 0,
                    "field_sample_count": 0,
                    "signal_request_count": 0,
                    "unavailable_signal_count": 0,
                },
            ),
        )

    def flush(self) -> None:
        if self.config is None:
            return
        merge_runtime_metrics(
            self.metrics_out,
            "replay_probe",
            {**self.metrics(), "samples": list(self.samples)},
            config_path=self.config.path,
        )


class ScoreboardCheckRuntime:
    def __init__(
        self,
        config: RuntimeActionConfig | None,
        *,
        metrics_out: Path | None = None,
    ):
        self.config = config
        self.metrics_out = metrics_out
        self.checked_count = 0
        self.passed_count = 0
        self.failed_count = 0
        self.enforced_failure_count = 0
        self.failures: list[dict[str, Any]] = []
        self.action_metrics = _initial_action_metrics(
            config,
            {
                "checked_count": 0,
                "passed_count": 0,
                "failed_count": 0,
                "enforced_failure_count": 0,
            },
        )

    @classmethod
    def from_env(cls) -> "ScoreboardCheckRuntime":
        return cls(
            load_runtime_action_config_from_env(
                SCOREBOARD_CHECK_CONFIG_ENV,
                action_type="scoreboard_check",
            ),
            metrics_out=runtime_metrics_path_from_env(),
        )

    def evaluate_record(self, record: Any) -> None:
        if self.config is None:
            return
        for entry in self.config.entries:
            passed, reason = _evaluate_scoreboard_entry(entry, record)
            action_metrics = self._action_metrics(entry)
            self.checked_count += 1
            action_metrics["checked_count"] += 1
            if passed:
                self.passed_count += 1
                action_metrics["passed_count"] += 1
            else:
                self.failed_count += 1
                action_metrics["failed_count"] += 1
                failure = {
                    "action_id": entry.action_id,
                    "case_index": getattr(record, "index", None),
                    "reason": reason,
                    "enforced": bool(entry.payload.get("enforce", False)),
                }
                if len(self.failures) < 32:
                    self.failures.append(failure)
                if failure["enforced"]:
                    self.enforced_failure_count += 1
                    action_metrics["enforced_failure_count"] += 1
        self.flush()

    def check(self) -> None:
        if self.enforced_failure_count:
            first = self.failures[0] if self.failures else {}
            raise AssertionError(
                "Harness scoreboard_check saw "
                f"{self.enforced_failure_count} enforced failures; first={first}"
            )

    def metrics(self) -> dict[str, Any]:
        configured = len(self.config.entries) if self.config is not None else 0
        return {
            "configured_count": configured,
            "checked_count": self.checked_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "enforced_failure_count": self.enforced_failure_count,
            "failure_preview_count": len(self.failures),
            "actions": _sorted_action_metrics(self.action_metrics),
        }

    def summary(self) -> dict[str, Any]:
        return {**self.metrics(), "failures": list(self.failures)}

    def flush(self) -> None:
        if self.config is None:
            return
        merge_runtime_metrics(
            self.metrics_out,
            "scoreboard_check",
            self.summary(),
            config_path=self.config.path,
        )

    def _action_metrics(self, entry: RuntimeActionEntry) -> dict[str, Any]:
        return self.action_metrics.setdefault(
            entry.action_id,
            _action_metric_entry(
                entry,
                {
                    "checked_count": 0,
                    "passed_count": 0,
                    "failed_count": 0,
                    "enforced_failure_count": 0,
                },
            ),
        )


class CoverageFeedbackTuningRuntime:
    def __init__(
        self,
        config: RuntimeActionConfig | None,
        *,
        metrics_out: Path | None = None,
    ):
        self.config = config
        self.metrics_out = metrics_out
        self.applied_count = 0
        self.trimmed_gap_count = 0
        self.weighted_directive_count = 0
        self.action_metrics = _initial_action_metrics(
            config,
            {
                "applied_count": 0,
                "trimmed_gap_count": 0,
                "directive_application_count": 0,
                "weighted_directive_count": 0,
            },
        )

    @classmethod
    def from_path(
        cls,
        path: Path | None,
        *,
        metrics_out: Path | None = None,
    ) -> "CoverageFeedbackTuningRuntime":
        config = (
            load_runtime_action_config(path, action_type="coverage_feedback_tuning")
            if path is not None
            else None
        )
        return cls(config, metrics_out=metrics_out)

    def apply_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        if self.config is None:
            return summary
        result = dict(summary)
        rtl_gap_summary = dict(result.get("rtl_gap_summary", {}))
        top_gaps = rtl_gap_summary.get("top_gaps")
        if isinstance(top_gaps, list):
            current_gaps = list(top_gaps)
            for entry in self.config.entries:
                action_metrics = self._action_metrics(entry)
                action_metrics["applied_count"] += 1
                if "max_gap_count" not in entry.payload:
                    continue
                limit = int(entry.payload["max_gap_count"])
                trimmed = max(0, len(current_gaps) - limit)
                self.trimmed_gap_count += trimmed
                action_metrics["trimmed_gap_count"] += trimmed
                current_gaps = current_gaps[:limit]
            rtl_gap_summary["top_gaps"] = current_gaps
            result["rtl_gap_summary"] = rtl_gap_summary
        else:
            for entry in self.config.entries:
                self._action_metrics(entry)["applied_count"] += 1
        self.applied_count += len(self.config.entries)
        result.setdefault("harness_runtime_actions", {})[
            "coverage_feedback_tuning"
        ] = self.metrics()
        self.flush()
        return result

    def apply_directives(self, directives: dict[str, Any]) -> dict[str, Any]:
        if self.config is None:
            return directives
        result = dict(directives)
        items = result.get("directives")
        if isinstance(items, list):
            new_items = []
            for item in items:
                updated = dict(item) if isinstance(item, dict) else item
                directive_weighted = False
                for entry in self.config.entries:
                    action_metrics = self._action_metrics(entry)
                    action_metrics["directive_application_count"] += 1
                    if not isinstance(updated, dict):
                        continue
                    multiplier = _number(entry.payload.get("directive_weight_multiplier"))
                    if multiplier is None:
                        continue
                    weight = _number(updated.get("weight"))
                    if weight is None:
                        continue
                    updated["weight"] = weight * multiplier
                    action_metrics["weighted_directive_count"] += 1
                    directive_weighted = True
                if directive_weighted:
                    self.weighted_directive_count += 1
                new_items.append(updated)
            result["directives"] = new_items
        result.setdefault("harness_runtime_actions", {})[
            "coverage_feedback_tuning"
        ] = self.metrics()
        self.flush()
        return result

    def metrics(self) -> dict[str, Any]:
        configured = len(self.config.entries) if self.config is not None else 0
        return {
            "configured_count": configured,
            "applied_count": self.applied_count,
            "trimmed_gap_count": self.trimmed_gap_count,
            "weighted_directive_count": self.weighted_directive_count,
            "actions": _sorted_action_metrics(self.action_metrics),
        }

    def flush(self) -> None:
        if self.config is None:
            return
        merge_runtime_metrics(
            self.metrics_out,
            "coverage_feedback_tuning",
            self.metrics(),
            config_path=self.config.path,
        )

    def _max_gap_count(self) -> int | None:
        values = [
            int(entry.payload["max_gap_count"])
            for entry in self.config.entries
            if "max_gap_count" in entry.payload
        ]
        return min(values) if values else None

    def _weight_multiplier(self) -> float | None:
        values = [
            float(entry.payload["directive_weight_multiplier"])
            for entry in self.config.entries
            if "directive_weight_multiplier" in entry.payload
        ]
        if not values:
            return None
        multiplier = 1.0
        for value in values:
            multiplier *= value
        return multiplier

    def _action_metrics(self, entry: RuntimeActionEntry) -> dict[str, Any]:
        return self.action_metrics.setdefault(
            entry.action_id,
            _action_metric_entry(
                entry,
                {
                    "applied_count": 0,
                    "trimmed_gap_count": 0,
                    "directive_application_count": 0,
                    "weighted_directive_count": 0,
                },
            ),
        )


def _initial_action_metrics(
    config: RuntimeActionConfig | None,
    template: Mapping[str, int],
) -> dict[str, dict[str, Any]]:
    if config is None:
        return {}
    return {
        entry.action_id: _action_metric_entry(entry, template)
        for entry in config.entries
    }


def _action_metric_entry(
    entry: RuntimeActionEntry,
    template: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "action_id": entry.action_id,
        "action_type": entry.action_type,
        **dict(template),
    }


def _sorted_action_metrics(
    action_metrics: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        (dict(metrics) for metrics in action_metrics.values()),
        key=lambda item: (str(item.get("action_type")), str(item.get("action_id"))),
    )


def _entry_from_json(
    item: Any,
    *,
    action_type: str,
    path: Path,
    index: int,
) -> RuntimeActionEntry:
    if not isinstance(item, dict):
        raise ValueError(f"{path}: entries[{index}] must be a JSON object")
    entry_type = item.get("action_type")
    if entry_type != action_type:
        raise ValueError(
            f"{path}: entries[{index}].action_type must be {action_type!r}, "
            f"got {entry_type!r}"
        )
    payload = item.get("payload")
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: entries[{index}].payload must be a JSON object")
    entry = RuntimeActionEntry(
        action_id=str(item.get("action_id") or f"{action_type}_{index}"),
        action_type=action_type,
        payload=payload,
        evidence_refs=tuple(
            ref for ref in item.get("evidence_refs", []) if isinstance(ref, dict)
        ),
        artifact_path=str(item["artifact_path"]) if item.get("artifact_path") else None,
        rationale=item.get("rationale"),
    )
    _validate_payload(entry, path=path, index=index)
    return entry


def _validate_payload(entry: RuntimeActionEntry, *, path: Path, index: int) -> None:
    payload = entry.payload
    if entry.action_type == "replay_probe":
        fields = _probe_fields(payload)
        signals = _string_list(payload.get("signals"), field="signals")
        if not fields and not signals:
            raise ValueError(
                f"{path}: entries[{index}].payload requires fields, signals, or probe"
            )
        _optional_string(payload, "sample_on", path=path, index=index)
        return
    if entry.action_type == "scoreboard_check":
        _optional_string(payload, "mode", path=path, index=index)
        _optional_string(payload, "check", path=path, index=index)
        if "mode" not in payload and "check" not in payload:
            raise ValueError(
                f"{path}: entries[{index}].payload requires check or mode"
            )
        if "enforce" in payload and not isinstance(payload["enforce"], bool):
            raise ValueError(f"{path}: entries[{index}].payload.enforce must be bool")
        return
    if entry.action_type == "coverage_feedback_tuning":
        if "max_gap_count" in payload:
            max_gap_count = _int_value(payload["max_gap_count"])
            if max_gap_count is None or max_gap_count < 0:
                raise ValueError(
                    f"{path}: entries[{index}].payload.max_gap_count "
                    "must be a non-negative integer"
                )
        if (
            "directive_weight_multiplier" in payload
            and float(payload["directive_weight_multiplier"]) <= 0
        ):
            raise ValueError(
                f"{path}: entries[{index}].payload.directive_weight_multiplier "
                "must be > 0"
            )
        _optional_string(payload, "prioritize", path=path, index=index)


def _optional_string(
    payload: Mapping[str, Any],
    field: str,
    *,
    path: Path,
    index: int,
) -> None:
    if field in payload and not isinstance(payload[field], str):
        raise ValueError(f"{path}: entries[{index}].payload.{field} must be a string")


def _probe_fields(payload: Mapping[str, Any]) -> tuple[str, ...]:
    values = list(_string_list(payload.get("fields"), field="fields"))
    probe = payload.get("probe")
    if isinstance(probe, str):
        values.append(probe)
    return tuple(dict.fromkeys(values))


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a string or list of strings")
    return tuple(value)


def _probe_value(field: str, *, case: Any, result: Any) -> Any:
    if field.startswith("case."):
        return _mapping_value(getattr(case, "data", {}), field[5:])
    if field.startswith("result."):
        return getattr(result, field[7:], None)
    data = getattr(case, "data", {})
    if isinstance(data, dict) and field in data:
        return data[field]
    return getattr(result, field, None)


def _mapping_value(values: Any, key: str) -> Any:
    return values.get(key) if isinstance(values, dict) else None


def _evaluate_scoreboard_entry(
    entry: RuntimeActionEntry,
    record: Any,
) -> tuple[bool, str]:
    mode = str(entry.payload.get("mode") or "actual_equals_expected")
    if mode == "record_seen":
        return True, "record observed"
    if mode == "require_no_error":
        error = getattr(record, "error", None)
        return error is None, str(error or "ok")
    result = getattr(record, "result", None)
    if getattr(record, "error", None) is not None:
        return False, str(getattr(record, "error"))
    if result is None:
        return False, "missing replay result"
    expected = getattr(result, "expected", None)
    actual = getattr(result, "actual", None)
    if expected is None:
        return False, "missing expected result"
    return actual == expected, f"actual={actual} expected={expected}"


def _number(value: Any) -> int | float | None:
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


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("-"):
            return -int(text[1:]) if text[1:].isdigit() else None
        return int(text) if text.isdigit() else None
    return None
