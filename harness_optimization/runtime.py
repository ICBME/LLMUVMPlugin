from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .io import write_json


TARGET_ENV = "HARNESS_TARGET"
TARGET_CONFIG_ENV = "HARNESS_TARGET_CONFIG"
CORPUS_ENV = "HARNESS_CORPUS"
SHARED_FUNCTIONAL_COVERAGE_OUT_ENV = "HARNESS_FUNCTIONAL_COVERAGE_OUT"

REPLAY_PROBE_CONFIG_ENV = "HARNESS_REPLAY_PROBE_CONFIG"
SCOREBOARD_CHECK_CONFIG_ENV = "HARNESS_SCOREBOARD_CHECK_CONFIG"
COVERAGE_FEEDBACK_TUNING_CONFIG_ENV = "HARNESS_COVERAGE_FEEDBACK_TUNING_CONFIG"
MMIO_READBACK_CONFIG_ENV = "HARNESS_MMIO_READBACK_CONFIG"
RUNTIME_METRICS_OUT_ENV = "HARNESS_RUNTIME_METRICS_OUT"
RUNTIME_ACTION_PLUGINS_ENV = "HARNESS_RUNTIME_ACTION_PLUGINS"

RUNTIME_METRICS_KIND = "harness_optimization.runtime_action_metrics"

# Backward-compatible aliases. New code should prefer the generic names above.
REPLAY_TARGET_ENV = "FUZZ_TARGET"
REPLAY_TARGET_CONFIG_ENV = "FUZZ_TARGET_CONFIG"
REPLAY_CORPUS_ENV = "LIBAFL_CORPUS"
FUNCTIONAL_COVERAGE_OUT_ENV = "UVM_FUNCTIONAL_COVERAGE_OUT"


def replay_target_from_env(
    *,
    target_env: str = TARGET_ENV,
    target_config_env: str = TARGET_CONFIG_ENV,
) -> str:
    target = os.getenv(target_env)
    if target is None and os.getenv(target_config_env) is None:
        raise RuntimeError(
            f"{target_env} or {target_config_env} must be set"
        )
    return target or "dut"


def replay_corpus_from_env(
    target: str | None = None,
    *,
    target_env: str = TARGET_ENV,
    corpus_env: str = CORPUS_ENV,
) -> Path:
    target_name = target or os.getenv(target_env, "dut")
    return Path(os.getenv(corpus_env, f"coverage/{target_name}_corpus.jsonl"))


def functional_coverage_output_from_target(
    target_name: str,
    *,
    env_var: str = SHARED_FUNCTIONAL_COVERAGE_OUT_ENV,
) -> Path:
    default_path = Path("coverage") / f"{target_name}_uvm_functional_coverage.json"
    return Path(os.getenv(env_var, str(default_path)))


def runtime_metrics_path_from_env(
    *,
    env_var: str = RUNTIME_METRICS_OUT_ENV,
) -> Path | None:
    value = os.getenv(env_var)
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
    kind: str = RUNTIME_METRICS_KIND,
) -> dict[str, Any]:
    if path is None:
        return {
            "schema_version": 1,
            "kind": kind,
            "sections": {section: dict(metrics)},
            "summary": dict(metrics),
        }
    payload = read_runtime_metrics(path, kind=kind)
    sections = payload.setdefault("sections", {})
    if not isinstance(sections, dict):
        sections = {}
        payload["sections"] = sections
    value = dict(metrics)
    if config_path is not None:
        value["config"] = str(config_path)
    sections[section] = value
    payload["summary"] = runtime_metrics_summary(payload)
    write_json(path, payload)
    return payload


def read_runtime_metrics(
    path: Path | None,
    *,
    kind: str = RUNTIME_METRICS_KIND,
) -> dict[str, Any]:
    if path is None or not path.exists():
        return {
            "schema_version": 1,
            "kind": kind,
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


def runtime_metric_snapshot(path: Path | None) -> dict[str, float | int]:
    if path is None or not path.exists():
        return {}
    return runtime_metrics_summary(read_runtime_metrics(path))


def runtime_action_plugin_specs_from_env(
    *,
    env_var: str = RUNTIME_ACTION_PLUGINS_ENV,
) -> tuple[str, ...]:
    value = os.getenv(env_var)
    if value is None or not value.strip():
        return ()
    return tuple(
        item.strip()
        for chunk in value.splitlines()
        for item in chunk.split(",")
        if item.strip()
    )


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, int | float) else None


__all__ = [
    "CORPUS_ENV",
    "COVERAGE_FEEDBACK_TUNING_CONFIG_ENV",
    "FUNCTIONAL_COVERAGE_OUT_ENV",
    "MMIO_READBACK_CONFIG_ENV",
    "REPLAY_CORPUS_ENV",
    "REPLAY_PROBE_CONFIG_ENV",
    "REPLAY_TARGET_CONFIG_ENV",
    "REPLAY_TARGET_ENV",
    "SHARED_FUNCTIONAL_COVERAGE_OUT_ENV",
    "TARGET_CONFIG_ENV",
    "TARGET_ENV",
    "RUNTIME_ACTION_PLUGINS_ENV",
    "RUNTIME_METRICS_KIND",
    "RUNTIME_METRICS_OUT_ENV",
    "SCOREBOARD_CHECK_CONFIG_ENV",
    "extra_make_var_value",
    "functional_coverage_output_from_target",
    "merge_runtime_metrics",
    "read_runtime_metrics",
    "replay_corpus_from_env",
    "replay_target_from_env",
    "runtime_action_plugin_specs_from_env",
    "runtime_metric_snapshot",
    "runtime_metrics_path_from_env",
    "runtime_metrics_summary",
]
