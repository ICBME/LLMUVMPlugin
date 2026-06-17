from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

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
RUNTIME_HOOK_NAMES = (
    "before_reset",
    "after_reset",
    "before_case",
    "after_execute",
    "sample_after_execute",
    "after_ref_model",
    "after_scoreboard_record",
    "finalize",
)

# Backward-compatible aliases. New code should prefer the generic names above.
REPLAY_TARGET_ENV = "FUZZ_TARGET"
REPLAY_TARGET_CONFIG_ENV = "FUZZ_TARGET_CONFIG"
REPLAY_CORPUS_ENV = "LIBAFL_CORPUS"
FUNCTIONAL_COVERAGE_OUT_ENV = "UVM_FUNCTIONAL_COVERAGE_OUT"

RuntimeActionPluginBuilder = Callable[..., Any]


class RuntimeActionValidator(Protocol):
    def __call__(
        self,
        entry: "RuntimeActionEntry",
        *,
        path: Path,
        index: int,
    ) -> None: ...


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


@dataclass(frozen=True)
class HarnessRuntimeActionManager:
    runtimes: tuple[Any, ...]

    @classmethod
    def from_runtime_list(
        cls,
        runtimes: Iterable[Any],
    ) -> "HarnessRuntimeActionManager":
        return cls(tuple(runtimes))

    def runtime_of_type(self, runtime_type: type[Any]) -> Any | None:
        for runtime in self.runtimes:
            if isinstance(runtime, runtime_type):
                return runtime
        return None

    def hook_capabilities(self) -> dict[str, list[str]]:
        hooks: dict[str, list[str]] = {}
        for runtime in self.runtimes:
            hooks[type(runtime).__name__] = [
                name
                for name in RUNTIME_HOOK_NAMES
                if callable(getattr(runtime, name, None))
            ]
        return hooks

    async def invoke_hook(self, *hook_names: str, **kwargs: Any) -> None:
        for runtime in self.runtimes:
            for hook_name in hook_names:
                hook = getattr(runtime, hook_name, None)
                if not callable(hook):
                    continue
                value = hook(**_hook_kwargs(hook, kwargs))
                if inspect.isawaitable(value):
                    await value

    def invoke_hook_sync(self, *hook_names: str, **kwargs: Any) -> None:
        for runtime in self.runtimes:
            for hook_name in hook_names:
                hook = getattr(runtime, hook_name, None)
                if not callable(hook):
                    continue
                value = hook(**_hook_kwargs(hook, kwargs))
                if inspect.isawaitable(value):
                    _drive_awaitable_from_sync(value)

    async def before_reset(self, *, driver: Any | None = None) -> None:
        await self.invoke_hook("before_reset", driver=driver)

    async def after_reset(self, *, driver: Any | None = None) -> None:
        await self.invoke_hook("after_reset", driver=driver)

    async def before_case(
        self,
        *,
        index: int,
        case: Any,
        driver: Any,
    ) -> None:
        await self.invoke_hook("before_case", index=index, case=case, driver=driver)

    async def after_ref_model(
        self,
        *,
        index: int,
        case: Any,
        expected: Any,
        driver: Any,
    ) -> None:
        await self.invoke_hook(
            "after_ref_model",
            index=index,
            case=case,
            expected=expected,
            driver=driver,
        )

    def after_scoreboard_record_sync(
        self,
        *,
        index: int,
        case: Any,
        result: Any,
        record: Any,
        driver: Any | None = None,
    ) -> None:
        self.invoke_hook_sync(
            "after_scoreboard_record",
            index=index,
            case=case,
            result=result,
            record=record,
            driver=driver,
        )

    async def after_scoreboard_record(
        self,
        *,
        index: int,
        case: Any,
        result: Any,
        record: Any,
        driver: Any,
    ) -> None:
        await self.invoke_hook(
            "after_scoreboard_record",
            index=index,
            case=case,
            result=result,
            record=record,
            driver=driver,
        )

    def finalize_sync(self, *, driver: Any | None = None) -> None:
        self.invoke_hook_sync("finalize", driver=driver)

    async def finalize(self, *, driver: Any | None = None) -> None:
        await self.invoke_hook("finalize", driver=driver)

    async def sample_after_execute(
        self,
        *,
        index: int,
        case: Any,
        result: Any,
        driver: Any,
    ) -> None:
        await self.invoke_hook(
            "after_execute",
            "sample_after_execute",
            index=index,
            case=case,
            result=result,
            driver=driver,
        )


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


def load_runtime_action_config_from_env(
    env_var: str,
    *,
    action_type: str,
    validator: RuntimeActionValidator | None = None,
) -> RuntimeActionConfig | None:
    value = os.getenv(env_var)
    if value is None or not value.strip():
        return None
    return load_runtime_action_config(
        Path(value),
        action_type=action_type,
        validator=validator,
    )


def load_runtime_action_config(
    path: Path,
    *,
    action_type: str,
    validator: RuntimeActionValidator | None = None,
) -> RuntimeActionConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: runtime action config must be a JSON object")
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"{path}: runtime action config requires an entries array")
    entries = tuple(
        _runtime_action_entry_from_json(
            item,
            action_type=action_type,
            path=path,
            index=index,
            validator=validator,
        )
        for index, item in enumerate(raw_entries)
    )
    return RuntimeActionConfig(action_type=action_type, path=path, entries=entries)


def runtime_action_plugins_from_specs(
    plugin_specs: Iterable[str],
    *,
    plugin_builder: RuntimeActionPluginBuilder,
    metrics_out: Path | None = None,
) -> list[Any]:
    runtimes: list[Any] = []
    for spec in plugin_specs:
        runtimes.append(plugin_builder(spec, metrics_out=metrics_out))
    return runtimes


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


def _runtime_action_entry_from_json(
    item: Any,
    *,
    action_type: str,
    path: Path,
    index: int,
    validator: RuntimeActionValidator | None,
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
    if validator is not None:
        validator(entry, path=path, index=index)
    return entry


def _drive_awaitable_from_sync(value: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(value)
        return
    loop.create_task(value)


def _hook_kwargs(hook: Any, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        return dict(kwargs)
    parameters = signature.parameters
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return dict(kwargs)
    return {name: value for name, value in kwargs.items() if name in parameters}


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, int | float) else None


__all__ = [
    "CORPUS_ENV",
    "COVERAGE_FEEDBACK_TUNING_CONFIG_ENV",
    "FUNCTIONAL_COVERAGE_OUT_ENV",
    "HarnessRuntimeActionManager",
    "MMIO_READBACK_CONFIG_ENV",
    "REPLAY_CORPUS_ENV",
    "REPLAY_PROBE_CONFIG_ENV",
    "REPLAY_TARGET_CONFIG_ENV",
    "REPLAY_TARGET_ENV",
    "RUNTIME_HOOK_NAMES",
    "SHARED_FUNCTIONAL_COVERAGE_OUT_ENV",
    "TARGET_CONFIG_ENV",
    "TARGET_ENV",
    "RUNTIME_ACTION_PLUGINS_ENV",
    "RUNTIME_METRICS_KIND",
    "RUNTIME_METRICS_OUT_ENV",
    "RuntimeActionConfig",
    "RuntimeActionEntry",
    "SCOREBOARD_CHECK_CONFIG_ENV",
    "extra_make_var_value",
    "functional_coverage_output_from_target",
    "load_runtime_action_config",
    "load_runtime_action_config_from_env",
    "merge_runtime_metrics",
    "read_runtime_metrics",
    "replay_corpus_from_env",
    "replay_target_from_env",
    "runtime_action_plugins_from_specs",
    "runtime_action_plugin_specs_from_env",
    "runtime_metric_snapshot",
    "runtime_metrics_path_from_env",
    "runtime_metrics_summary",
]
