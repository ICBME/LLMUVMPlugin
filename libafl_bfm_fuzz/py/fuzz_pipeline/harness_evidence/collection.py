from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Sequence

from ConnectGraph import close_observation, observation_context_from_env
from ConnectGraph.topology import (
    topology_out_from_env,
    validate_connector_endpoint,
    write_topology_from_env,
)
from harness_optimization.io import artifact_mapping as _artifact_mapping
from harness_optimization.io import path_sha256  # noqa: F401

from ..orchestrator import PipelineContext, PipelineOrchestrator, external_command_step
from ..topology import FULL_FUZZ_TOPOLOGY


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


def write_observation_topology(path: str | Path | None = None) -> None:
    write_topology_from_env(FULL_FUZZ_TOPOLOGY, path)


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
    validate_connector_endpoint(
        FULL_FUZZ_TOPOLOGY,
        connector_name,
        from_layer,
        to_layer,
    )
    input_artifacts = _artifact_mapping(inputs)
    output_artifacts = _artifact_mapping(outputs)
    observation_context = observation_context_from_env()
    context = PipelineContext(
        run_id=observation_context.run_id,
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
        observation_context,
        topology_out=topology_out_from_env(),
    ).run([step], context)
    return context.values[connector_name]


def command_metrics(result: subprocess.CompletedProcess) -> dict[str, Any]:
    return {"returncode": int(result.returncode)}


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
