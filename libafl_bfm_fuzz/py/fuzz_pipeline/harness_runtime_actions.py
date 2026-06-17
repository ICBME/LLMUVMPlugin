"""Compatibility facade for runtime action helpers and legacy env contracts.

New code should prefer ``fuzz_pipeline.harness_evidence.runtime_actions`` for
runtime implementations and this module only for the libafl_bfm_fuzz-specific
env var / artifact contract surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from harness_optimization.runtime import (
    COVERAGE_FEEDBACK_TUNING_CONFIG_ENV as SHARED_COVERAGE_FEEDBACK_TUNING_CONFIG_ENV,
    extra_make_var_value,
    FUNCTIONAL_COVERAGE_OUT_ENV as SHARED_FUNCTIONAL_COVERAGE_OUT_ENV,
    functional_coverage_output_from_target as _functional_coverage_output_from_target,
    MMIO_READBACK_CONFIG_ENV as SHARED_MMIO_READBACK_CONFIG_ENV,
    merge_runtime_metrics as _merge_runtime_metrics,
    read_runtime_metrics as _read_runtime_metrics,
    REPLAY_CORPUS_ENV as SHARED_REPLAY_CORPUS_ENV,
    REPLAY_PROBE_CONFIG_ENV as SHARED_REPLAY_PROBE_CONFIG_ENV,
    REPLAY_TARGET_CONFIG_ENV as SHARED_REPLAY_TARGET_CONFIG_ENV,
    REPLAY_TARGET_ENV as SHARED_REPLAY_TARGET_ENV,
    replay_corpus_from_env as _replay_corpus_from_env,
    replay_target_from_env as _replay_target_from_env,
    RUNTIME_ACTION_PLUGINS_ENV as SHARED_RUNTIME_ACTION_PLUGINS_ENV,
    runtime_action_plugin_specs_from_env as _runtime_action_plugin_specs_from_env,
    runtime_metric_snapshot,
    RUNTIME_METRICS_OUT_ENV as SHARED_RUNTIME_METRICS_OUT_ENV,
    runtime_metrics_path_from_env as _runtime_metrics_path_from_env,
    runtime_metrics_summary,
    SCOREBOARD_CHECK_CONFIG_ENV as SHARED_SCOREBOARD_CHECK_CONFIG_ENV,
)

from .harness_evidence.runtime_actions import *  # noqa: F401,F403


REPLAY_TARGET_ENV = SHARED_REPLAY_TARGET_ENV
REPLAY_TARGET_CONFIG_ENV = SHARED_REPLAY_TARGET_CONFIG_ENV
REPLAY_CORPUS_ENV = SHARED_REPLAY_CORPUS_ENV
FUNCTIONAL_COVERAGE_OUT_ENV = SHARED_FUNCTIONAL_COVERAGE_OUT_ENV

REPLAY_PROBE_CONFIG_ENV = SHARED_REPLAY_PROBE_CONFIG_ENV
SCOREBOARD_CHECK_CONFIG_ENV = SHARED_SCOREBOARD_CHECK_CONFIG_ENV
COVERAGE_FEEDBACK_TUNING_CONFIG_ENV = SHARED_COVERAGE_FEEDBACK_TUNING_CONFIG_ENV
MMIO_READBACK_CONFIG_ENV = SHARED_MMIO_READBACK_CONFIG_ENV
RUNTIME_METRICS_OUT_ENV = SHARED_RUNTIME_METRICS_OUT_ENV
RUNTIME_ACTION_PLUGINS_ENV = SHARED_RUNTIME_ACTION_PLUGINS_ENV

RUNTIME_METRICS_KIND = "libafl_bfm_fuzz.harness_runtime_action_metrics"
_COMPAT_EXPORTS = (
    extra_make_var_value,
    runtime_metric_snapshot,
    runtime_metrics_summary,
)


def replay_target_from_env() -> str:
    return _replay_target_from_env(
        target_env=REPLAY_TARGET_ENV,
        target_config_env=REPLAY_TARGET_CONFIG_ENV,
    )


def replay_corpus_from_env(target: str | None = None) -> Path:
    return _replay_corpus_from_env(
        target,
        target_env=REPLAY_TARGET_ENV,
        corpus_env=REPLAY_CORPUS_ENV,
    )


def functional_coverage_output_from_target(target_name: str) -> Path:
    return _functional_coverage_output_from_target(
        target_name,
        env_var=FUNCTIONAL_COVERAGE_OUT_ENV,
    )


def runtime_metrics_path_from_env() -> Path | None:
    return _runtime_metrics_path_from_env(env_var=RUNTIME_METRICS_OUT_ENV)


def runtime_action_plugin_specs_from_env() -> tuple[str, ...]:
    return _runtime_action_plugin_specs_from_env(env_var=RUNTIME_ACTION_PLUGINS_ENV)


def merge_runtime_metrics(
    path: Path | None,
    section: str,
    metrics: Mapping[str, Any],
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    return _merge_runtime_metrics(
        path,
        section,
        metrics,
        config_path=config_path,
        kind=RUNTIME_METRICS_KIND,
    )


def read_runtime_metrics(path: Path | None) -> dict[str, Any]:
    return _read_runtime_metrics(path, kind=RUNTIME_METRICS_KIND)


__all__ = [
    name
    for name in globals()
    if not name.startswith("_") and name != "annotations"
]
