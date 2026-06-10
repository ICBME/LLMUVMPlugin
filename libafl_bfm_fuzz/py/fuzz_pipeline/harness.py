from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Sequence

from connector_observe import Connector, ObservationContext, observer_from_env
from connector_observe.observers import Observer

from .orchestrator import PipelineContext, PipelineOrchestrator, external_command_step
from .topology import FULL_FUZZ_TOPOLOGY


_context: ObservationContext | None = None
_owned_observer: Observer | None = None
_topology_paths_written: set[str] = set()
_PATH_SHA256_CACHE: dict[tuple[str, int, int], str] = {}
_CASE_METADATA_KEYS = {
    "case_id",
    "case_sha256",
    "corpus_sha256",
    "directive",
    "directive_id",
    "directive_name",
    "origin",
    "testcase_id",
}


def observation_context_from_env() -> ObservationContext:
    global _context, _owned_observer
    if _context is not None:
        return _context

    _owned_observer = observer_from_env()
    base = ObservationContext.from_env(observer=_owned_observer)
    run_id = os.getenv("CONNECTOR_OBSERVE_RUN_ID") or base.run_id
    round_id = os.getenv("CONNECTOR_OBSERVE_ROUND_ID") or base.round_id
    stage_id = os.getenv("CONNECTOR_OBSERVE_STAGE_ID") or base.stage_id
    parent_event_id = os.getenv("CONNECTOR_OBSERVE_PARENT_EVENT_ID") or base.parent_event_id
    _context = ObservationContext(
        run_id=run_id,
        round_id=round_id,
        stage_id=stage_id,
        parent_event_id=parent_event_id,
        observer=base.observer,
        strict=base.strict,
    )
    atexit.register(close_observation)
    return _context


def close_observation() -> None:
    global _context, _owned_observer
    if _owned_observer is not None:
        try:
            _owned_observer.close()
        finally:
            _owned_observer = None
            _context = None


def connector_from_env(name: str, from_layer: str, to_layer: str) -> Connector:
    return Connector.from_context(name, from_layer, to_layer, observation_context_from_env())


def write_observation_topology(path: str | Path | None = None) -> None:
    output_path = path or os.getenv("CONNECTOR_TOPOLOGY_OUT")
    if output_path is None or not str(output_path).strip():
        return

    resolved = str(Path(output_path))
    if resolved in _topology_paths_written:
        return
    topology_path = Path(output_path)
    topology_path.parent.mkdir(parents=True, exist_ok=True)
    topology_path.write_text(
        json.dumps(FULL_FUZZ_TOPOLOGY.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _topology_paths_written.add(resolved)


def run_command(
    command: Sequence[str],
    *,
    connector_name: str,
    from_layer: str,
    to_layer: str,
    inputs: Any = None,
    outputs: Any = None,
    metadata: dict[str, Any] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess:
    _validate_connector_endpoint(connector_name, from_layer, to_layer)
    input_artifacts = _artifact_mapping(inputs)
    output_artifacts = _artifact_mapping(outputs)
    context = PipelineContext(
        run_id=observation_context_from_env().run_id,
        artifacts={**input_artifacts, **output_artifacts},
        metadata=dict(metadata or {}),
    )
    step = external_command_step(
        name=connector_name,
        connector=connector_name,
        command=list(command),
        input_roles=tuple(input_artifacts),
        output_roles=tuple(output_artifacts),
        cwd=Path(cwd) if cwd is not None else None,
    )
    PipelineOrchestrator(
        FULL_FUZZ_TOPOLOGY,
        observation_context_from_env(),
        topology_out=_topology_out_from_env(),
    ).run([step], context)
    return context.values[connector_name]


def command_metrics(result: subprocess.CompletedProcess) -> dict[str, Any]:
    return {"returncode": int(result.returncode)}


def _artifact_mapping(value: Any) -> dict[str, Path]:
    if value is None:
        return {}
    if isinstance(value, dict):
        result = {}
        for role, path in value.items():
            if isinstance(path, dict):
                item_path = path.get("path")
            else:
                item_path = path
            if item_path is not None:
                result[str(role)] = Path(item_path)
        return result
    return {}


def _topology_out_from_env() -> Path | None:
    path = os.getenv("CONNECTOR_TOPOLOGY_OUT")
    return Path(path) if path and path.strip() else None


def _validate_connector_endpoint(connector_name: str, from_layer: str, to_layer: str) -> None:
    for edge in FULL_FUZZ_TOPOLOGY.connectors:
        if edge.name != connector_name:
            continue
        if edge.from_layer != from_layer or edge.to_layer != to_layer:
            raise ValueError(
                f"connector {connector_name!r} endpoint mismatch: "
                f"expected {edge.from_layer}->{edge.to_layer}, got {from_layer}->{to_layer}"
            )
        return
    raise ValueError(f"unknown connector: {connector_name}")


def replay_context_metrics(context: Any) -> dict[str, Any]:
    return {
        "target": str(getattr(context, "target", "")),
        "case_count": len(getattr(context, "cases", []) or []),
    }


def replay_case_metadata(case: Any, *, index: int | None = None) -> dict[str, Any]:
    data = getattr(case, "data", {}) or {}
    value: dict[str, Any] = {
        "target": str(getattr(case, "target", data.get("target", ""))),
        "line_no": int(getattr(case, "line_no", 0) or 0),
        "origin": str(data.get("origin", "unknown")),
        "case_id": case_id(case),
        "case_sha256": case_payload_sha256(case),
    }
    directive_id = directive_id_from_case(case)
    if directive_id is not None:
        value["directive_id"] = directive_id
    if index is not None:
        value["index"] = index
    return value


def case_id(case: Any) -> str:
    data = getattr(case, "data", {}) or {}
    for key in ("case_id", "testcase_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return case_payload_sha256(case)[:16]


def case_payload_sha256(case: Any) -> str:
    data = getattr(case, "data", {}) or {}
    stimulus_data = {
        str(key): value
        for key, value in data.items()
        if str(key) not in _CASE_METADATA_KEYS
    }
    payload = {
        "target": str(getattr(case, "target", data.get("target", ""))),
        "data": stimulus_data,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def directive_id_from_case(case: Any) -> str | None:
    data = getattr(case, "data", {}) or {}
    for key in ("directive_id", "directive_name", "directive"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    origin = data.get("origin")
    if isinstance(origin, str) and origin:
        return origin
    return None


def path_sha256(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
    if cache_key in _PATH_SHA256_CACHE:
        return _PATH_SHA256_CACHE[cache_key]
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    value = digest.hexdigest()
    _PATH_SHA256_CACHE[cache_key] = value
    return value


def replay_result_metrics(result: Any) -> dict[str, Any]:
    actual = getattr(result, "actual", None)
    expected = getattr(result, "expected", None)
    return {
        "has_expected": expected is not None,
        "matched": expected is not None and actual == expected,
        "detail": str(getattr(result, "detail", "")),
    }


def scoreboard_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "checked": int(summary.get("checked", 0)),
        "failures": int(summary.get("failures", 0)),
    }


def functional_coverage_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    coverage = summary.get("coverage", {})
    return {
        "total_cases": int(summary.get("total_cases", 0)),
        "covered": int(coverage.get("covered", 0)) if isinstance(coverage, dict) else 0,
        "total": int(coverage.get("total", 0)) if isinstance(coverage, dict) else 0,
        "percent": coverage.get("percent") if isinstance(coverage, dict) else None,
    }


def _reset_observation_context_for_tests() -> None:
    close_observation()
    _topology_paths_written.clear()
