from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Protocol

from connector_observe import ObservationContext

from .coverage_feedback import CoverageFeedbackResult
from .run_adapters import RunPathResolver


class EvaluationConfigView(Protocol):
    target: str
    mode: str | None
    round_id: str | None
    evaluation_out: Path | None
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


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
