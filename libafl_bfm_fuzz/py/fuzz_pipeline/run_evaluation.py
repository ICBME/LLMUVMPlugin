from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Protocol

from connector_observe import ObservationContext

from .coverage_feedback import CoverageFeedbackResult
from .harness_trace import HarnessTraceBuilder, HarnessTraceOutputs
from .run_adapters import RunPathResolver


class RoundEvaluationBackend(Protocol):
    def run_round(
        self,
        stage_results: dict[str, object],
    ) -> dict[str, Any]:
        ...


class CampaignEvaluationBackend(Protocol):
    def run(
        self,
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class EvaluationBackends:
    round_evaluation: RoundEvaluationBackend | None = None
    campaign_evaluation: CampaignEvaluationBackend | None = None


class EvaluationConfigView(Protocol):
    target: str
    mode: str | None
    round_id: str | None
    evaluation_out: Path | None
    observation_out: Path | None
    monitoring_out: Path | None
    cwd: Path | None


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RunEvaluationAdapter:
    config: EvaluationConfigView
    paths: RunPathResolver
    observation_context: ObservationContext
    round_id: Callable[[], str | None]

    def run_round(
        self,
        stage_results: dict[str, object],
    ) -> dict[str, Any]:
        path = self.paths.path_from_cwd(self.config.evaluation_out)
        if path is None:
            raise ValueError("missing evaluation_out for round evaluation stage")
        payload = self.round_payload(stage_results)
        self._attach_harness_trace(payload, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return payload

    def round_payload(self, stage_results: dict[str, object]) -> dict[str, Any]:
        manifest = _mapping(stage_results.get("round_manifest"))
        feedback = stage_results.get("coverage_feedback")
        return {
            "schema_version": 1,
            "kind": "libafl_bfm_fuzz.round_evaluation",
            "created_at": utc_timestamp(),
            "target": self.config.target,
            "mode": self.config.mode or "feedback_fuzz",
            "round_id": self.round_id(),
            "run_id": self.observation_context.run_id,
            "artifacts": {
                "round_manifest": self.paths.manifest_path(self._round_manifest_path()),
                "evaluation_report": self.paths.manifest_path(self.config.evaluation_out),
            },
            "stage_names": [
                name for name in stage_results if name != "round_evaluation"
            ],
            "stage_returncodes": self._stage_returncodes(stage_results),
            "case_counts": self._case_counts(stage_results),
            "coverage": manifest.get("coverage", {}),
            "feedback": manifest.get("feedback", self._feedback_snapshot(feedback)),
            "manifest_artifacts": manifest.get("artifacts", {}),
        }

    def _attach_harness_trace(
        self,
        payload: dict[str, Any],
        evaluation_path: Path,
    ) -> None:
        artifacts = _mapping(payload.get("manifest_artifacts"))
        observation_events = _resolved_artifact_path(
            artifacts.get("observation_events"),
        ) or self.paths.path_from_cwd(self.config.observation_out)
        _flush_observer(self.observation_context)
        if observation_events is None or not observation_events.exists():
            return
        outputs = HarnessTraceOutputs.from_evaluation_path(evaluation_path)
        try:
            result = HarnessTraceBuilder(
                observation_events=observation_events,
                monitoring=(
                    _resolved_artifact_path(artifacts.get("monitoring"))
                    or self.paths.path_from_cwd(self.config.monitoring_out)
                ),
                round_manifest=self._round_manifest_path(),
            ).write(outputs)
        except Exception as exc:  # noqa: BLE001 - evaluation trace is additive
            payload["harness_trace"] = {
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            return
        payload["harness_trace"] = {
            "status": "ok",
            "artifacts": outputs.to_json(),
            "summary": result.evaluation.get("summary", {}),
            "optimization_hints": result.evaluation.get("optimization_hints", {}),
        }

    def _round_manifest_path(self) -> Path | None:
        return self.paths.optional_artifact("round_manifest")

    def _stage_returncodes(
        self,
        stage_results: dict[str, object],
    ) -> dict[str, int]:
        values: dict[str, int] = {}
        for name, result in stage_results.items():
            if isinstance(result, subprocess.CompletedProcess):
                values[name] = int(result.returncode)
        for name in ("annotate", "write_info"):
            result = stage_results.get(name)
            if isinstance(result, subprocess.CompletedProcess):
                values[name] = int(result.returncode)
        return values

    def _case_counts(self, stage_results: dict[str, object]) -> dict[str, int]:
        values: dict[str, int] = {}
        for name in ("corpus_validation", "feedback_corpus_validation"):
            result = stage_results.get(name)
            if result is None:
                continue
            try:
                values[name] = len(result)  # type: ignore[arg-type]
            except TypeError:
                continue
        return values

    def _feedback_snapshot(self, result: object) -> dict[str, Any]:
        if not isinstance(result, CoverageFeedbackResult):
            return {}
        directives = result.final_directives.get("directives", [])
        return {
            "directive_count": len(directives),
            "directive_source": result.final_directives.get("source"),
            "has_gap_feedback": result.gap_feedback is not None,
            "has_mutation_feedback": result.mutation_feedback is not None,
        }


@dataclass(frozen=True)
class CampaignEvaluationAdapter:
    target: str
    path: Path
    observation_context: ObservationContext
    cwd: Path

    def run(
        self,
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self.payload(campaign_manifest)
        self._attach_harness_trace(payload, campaign_manifest)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return payload

    def payload(self, campaign_manifest: dict[str, Any]) -> dict[str, Any]:
        artifacts = campaign_manifest.get("artifacts", {})
        artifact_map = artifacts if isinstance(artifacts, dict) else {}
        modes = campaign_manifest.get("modes", [])
        mode_items = modes if isinstance(modes, list) else []
        rounds = [
            round_item
            for mode_item in mode_items
            if isinstance(mode_item, dict)
            for round_item in mode_item.get("rounds", [])
            if isinstance(round_item, dict)
        ]
        return {
            "schema_version": 1,
            "kind": "libafl_bfm_fuzz.campaign_evaluation",
            "created_at": utc_timestamp(),
            "target": self.target,
            "run_id": self.observation_context.run_id,
            "cwd": str(self.cwd),
            "artifacts": {
                "campaign_manifest": artifact_map.get("campaign_manifest"),
                "evaluation_report": str(self.path),
            },
            "summary": {
                "mode_count": len(mode_items),
                "round_count": len(rounds),
                "modes": [
                    str(mode_item.get("mode"))
                    for mode_item in mode_items
                    if isinstance(mode_item, dict)
                ],
            },
            "rounds": [
                {
                    "round_id": item.get("round_id"),
                    "round_manifest": item.get("round_manifest"),
                    "coverage": item.get("coverage", {}),
                    "feedback": item.get("feedback", {}),
                }
                for item in rounds
            ],
        }

    def _attach_harness_trace(
        self,
        payload: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> None:
        artifacts = _mapping(campaign_manifest.get("artifacts"))
        observation_events = _resolved_artifact_path(
            artifacts.get("observation_events"),
            cwd=self.cwd,
        )
        _flush_observer(self.observation_context)
        if observation_events is None or not observation_events.exists():
            return
        outputs = HarnessTraceOutputs.from_evaluation_path(self.path)
        try:
            result = HarnessTraceBuilder(
                observation_events=observation_events,
                monitoring=_resolved_artifact_path(
                    artifacts.get("monitoring"),
                    cwd=self.cwd,
                ),
                campaign_manifest=_resolved_artifact_path(
                    artifacts.get("campaign_manifest"),
                    cwd=self.cwd,
                ),
            ).write(outputs)
        except Exception as exc:  # noqa: BLE001 - evaluation trace is additive
            payload["harness_trace"] = {
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            return
        payload["harness_trace"] = {
            "status": "ok",
            "artifacts": outputs.to_json(),
            "summary": result.evaluation.get("summary", {}),
            "optimization_hints": result.evaluation.get("optimization_hints", {}),
        }


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _flush_observer(observation_context: ObservationContext) -> None:
    observer = observation_context.observer
    if observer is None:
        return
    try:
        observer.flush()
    except Exception:
        return


def _resolved_artifact_path(
    value: object,
    *,
    cwd: Path | None = None,
) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or cwd is None:
        return path
    return cwd / path
