from __future__ import annotations

from collections.abc import Callable, Iterable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .optimization import HarnessOptimizationPaths
from .orchestrator import StepSpec
from .planning import RunResults, RunStageFactory, result_stage


class CampaignOptimizationAdapter(Protocol):
    def run_task(
        self,
        campaign_evaluation: dict[str, Any],
        campaign_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def run_proposal(self, task: dict[str, Any]) -> dict[str, Any]:
        ...

    def run_decision(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def run_advice_report(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def run_apply(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        ...

    def run_candidate_evaluation(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        patch: dict[str, Any],
        candidate_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def run_metric_delta(
        self,
        task: dict[str, Any],
        candidate_evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        ...

    def run_final_decision(
        self,
        task: dict[str, Any],
        proposal: dict[str, Any],
        schema_decision: dict[str, Any],
        patch: dict[str, Any],
        candidate_evaluation: dict[str, Any],
        metric_delta: dict[str, Any],
    ) -> dict[str, Any]:
        ...


StepRunner = Callable[[StepSpec], dict[str, Any]]
PathsFactory = Callable[[], HarnessOptimizationPaths]
AdapterFactory = Callable[[HarnessOptimizationPaths], CampaignOptimizationAdapter]


@dataclass(frozen=True)
class CampaignOptimizationStageChain:
    target: str
    modes: tuple[str, ...]
    rounds: int
    context_artifacts: MutableMapping[str, Path]
    step_runner: StepRunner
    paths_factory: PathsFactory
    adapter_factory: AdapterFactory

    def stage_factories(self) -> dict[str, RunStageFactory]:
        return {
            "harness_optimization_task": lambda: result_stage(
                "harness_optimization_task",
                self._run_task_stage,
                requires_results=("campaign_manifest", "campaign_evaluation"),
                produces_results=("harness_optimization_task",),
                input_roles=("evaluation_report",),
                output_roles=("harness_optimization_task",),
            ),
            "harness_optimization_proposal": lambda: result_stage(
                "harness_optimization_proposal",
                self._run_proposal_stage,
                requires_results=("harness_optimization_task",),
                produces_results=("harness_optimization_proposal",),
                input_roles=("harness_optimization_task",),
                output_roles=("harness_optimization_proposal",),
            ),
            "harness_optimization_decision": lambda: result_stage(
                "harness_optimization_decision",
                self._run_decision_stage,
                requires_results=(
                    "harness_optimization_task",
                    "harness_optimization_proposal",
                ),
                produces_results=("harness_optimization_decision",),
                input_roles=(
                    "harness_optimization_task",
                    "harness_optimization_proposal",
                ),
                output_roles=("harness_optimization_decision",),
            ),
            "harness_optimization_advice_report": lambda: result_stage(
                "harness_optimization_advice_report",
                self._run_advice_report_stage,
                requires_results=(
                    "harness_optimization_task",
                    "harness_optimization_proposal",
                    "harness_optimization_decision",
                ),
                produces_results=("harness_optimization_advice_report",),
                input_roles=(
                    "harness_optimization_task",
                    "harness_optimization_proposal",
                    "harness_optimization_decision",
                ),
                output_roles=("harness_optimization_advice_report",),
            ),
            "harness_optimization_apply": lambda: result_stage(
                "harness_optimization_apply",
                self._run_apply_stage,
                merge_mapping=True,
                requires_results=(
                    "harness_optimization_task",
                    "harness_optimization_proposal",
                    "harness_optimization_decision",
                ),
                produces_results=(
                    "harness_optimization_patch",
                    "harness_optimization_candidate_manifest",
                ),
                input_roles=(
                    "harness_optimization_task",
                    "harness_optimization_proposal",
                    "harness_optimization_decision",
                ),
                output_roles=(
                    "harness_optimization_patch",
                    "harness_optimization_candidate_manifest",
                ),
            ),
            "harness_optimization_candidate_evaluation": lambda: result_stage(
                "harness_optimization_candidate_evaluation",
                self._run_candidate_evaluation_stage,
                requires_results=(
                    "harness_optimization_task",
                    "harness_optimization_proposal",
                    "harness_optimization_patch",
                    "harness_optimization_candidate_manifest",
                ),
                produces_results=("harness_optimization_candidate_evaluation",),
                input_roles=(
                    "harness_optimization_patch",
                    "harness_optimization_candidate_manifest",
                ),
                output_roles=("harness_optimization_candidate_evaluation",),
            ),
            "harness_optimization_metric_delta": lambda: result_stage(
                "harness_optimization_metric_delta",
                self._run_metric_delta_stage,
                requires_results=(
                    "harness_optimization_task",
                    "harness_optimization_candidate_evaluation",
                ),
                produces_results=("harness_optimization_metric_delta",),
                input_roles=("harness_optimization_candidate_evaluation",),
                output_roles=("harness_optimization_metric_delta",),
            ),
            "harness_optimization_final_decision": lambda: result_stage(
                "harness_optimization_final_decision",
                self._run_final_decision_stage,
                requires_results=(
                    "harness_optimization_task",
                    "harness_optimization_proposal",
                    "harness_optimization_decision",
                    "harness_optimization_patch",
                    "harness_optimization_candidate_evaluation",
                    "harness_optimization_metric_delta",
                ),
                produces_results=("harness_optimization_final_decision",),
                input_roles=(
                    "harness_optimization_decision",
                    "harness_optimization_patch",
                    "harness_optimization_candidate_evaluation",
                    "harness_optimization_metric_delta",
                ),
                output_roles=("harness_optimization_final_decision",),
            ),
        }

    def manifest_artifacts(self, stage_names: Iterable[str]) -> dict[str, str]:
        names = set(stage_names)
        if "harness_optimization_task" not in names:
            return {}
        paths = self.paths_factory()
        artifacts = paths.to_json()
        artifacts.update(
            {
                role: path
                for role, path in paths.optimizer_io_json().items()
                if Path(path).exists()
            }
        )
        if "harness_optimization_advice_report" in names:
            artifacts.update(paths.advice_json())
        if "harness_optimization_apply" in names:
            artifacts.update(paths.validation_json())
        return artifacts

    def _metadata(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "modes": ",".join(self.modes),
            "rounds": self.rounds,
        }

    def _adapter(self, paths: HarnessOptimizationPaths) -> CampaignOptimizationAdapter:
        return self.adapter_factory(paths)

    def _required_mapping(
        self,
        stage_results: RunResults,
        key: str,
        stage: str,
    ) -> dict[str, Any]:
        value = stage_results.get(key)
        if not isinstance(value, dict):
            raise ValueError(f"{stage} requires {key} result")
        return value

    def _run_task_stage(self, stage_results: RunResults) -> dict[str, Any]:
        paths = self.paths_factory()
        self.context_artifacts["harness_optimization_task"] = paths.task
        manifest = self._required_mapping(
            stage_results,
            "campaign_manifest",
            "harness_optimization_task",
        )
        evaluation = self._required_mapping(
            stage_results,
            "campaign_evaluation",
            "harness_optimization_task",
        )
        step = StepSpec(
            name="harness_optimization_task",
            connector="evaluation_to_harness_optimization_task",
            handler=lambda _context: self._adapter(paths).run_task(
                campaign_evaluation=evaluation,
                campaign_manifest=manifest,
            ),
            input_roles=("evaluation_report",),
            output_roles=("harness_optimization_task",),
            metrics=lambda value: {
                "llm_sample_count": value.get("summary", {}).get("llm_sample_count", 0),
                "allowed_action_type_count": len(
                    value.get("constraints", {}).get("allowed_action_types", [])
                ),
            },
            metadata=self._metadata(),
        )
        return self.step_runner(step)

    def _run_proposal_stage(self, stage_results: RunResults) -> dict[str, Any]:
        paths = self.paths_factory()
        self.context_artifacts["harness_optimization_proposal"] = paths.proposal
        task = self._required_mapping(
            stage_results,
            "harness_optimization_task",
            "harness_optimization_proposal",
        )
        step = StepSpec(
            name="harness_optimization_proposal",
            connector="harness_optimization_task_to_proposal",
            handler=lambda _context: self._adapter(paths).run_proposal(task),
            input_roles=("harness_optimization_task",),
            output_roles=("harness_optimization_proposal",),
            metrics=lambda value: {
                "action_count": len(value.get("actions", [])),
                "proposal_status": value.get("status"),
            },
            metadata=self._metadata(),
        )
        return self.step_runner(step)

    def _run_decision_stage(self, stage_results: RunResults) -> dict[str, Any]:
        paths = self.paths_factory()
        self.context_artifacts["harness_optimization_decision"] = paths.decision
        task = self._required_mapping(
            stage_results,
            "harness_optimization_task",
            "harness_optimization_decision",
        )
        proposal = self._required_mapping(
            stage_results,
            "harness_optimization_proposal",
            "harness_optimization_decision",
        )
        step = StepSpec(
            name="harness_optimization_decision",
            connector="harness_optimization_proposal_to_decision",
            handler=lambda _context: self._adapter(paths).run_decision(task, proposal),
            input_roles=(
                "harness_optimization_task",
                "harness_optimization_proposal",
            ),
            output_roles=("harness_optimization_decision",),
            metrics=lambda value: {
                "decision": value.get("decision"),
                "validation_error_count": value.get("validation", {}).get(
                    "error_count",
                    0,
                ),
            },
            metadata=self._metadata(),
        )
        return self.step_runner(step)

    def _run_advice_report_stage(self, stage_results: RunResults) -> dict[str, Any]:
        paths = self.paths_factory()
        self.context_artifacts["harness_optimization_advice_report"] = (
            paths.advice_report
        )
        task = self._required_mapping(
            stage_results,
            "harness_optimization_task",
            "harness_optimization_advice_report",
        )
        proposal = self._required_mapping(
            stage_results,
            "harness_optimization_proposal",
            "harness_optimization_advice_report",
        )
        decision = self._required_mapping(
            stage_results,
            "harness_optimization_decision",
            "harness_optimization_advice_report",
        )
        step = StepSpec(
            name="harness_optimization_advice_report",
            connector="harness_optimization_decision_to_advice_report",
            handler=lambda _context: self._adapter(paths).run_advice_report(
                task,
                proposal,
                decision,
            ),
            input_roles=(
                "harness_optimization_task",
                "harness_optimization_proposal",
                "harness_optimization_decision",
            ),
            output_roles=("harness_optimization_advice_report",),
            metrics=lambda value: {
                "advice_status": value.get("status"),
                "recommended_action_count": value.get("summary", {}).get(
                    "recommended_action_count",
                    0,
                ),
                "candidate_regression_triggered": value.get(
                    "advice_only_guard",
                    {},
                ).get("candidate_regression_triggered"),
            },
            metadata=self._metadata(),
        )
        return self.step_runner(step)

    def _run_apply_stage(self, stage_results: RunResults) -> dict[str, dict[str, Any]]:
        paths = self.paths_factory()
        self.context_artifacts["harness_optimization_patch"] = paths.patch
        self.context_artifacts["harness_optimization_candidate_manifest"] = (
            paths.candidate_manifest
        )
        task = self._required_mapping(
            stage_results,
            "harness_optimization_task",
            "harness_optimization_apply",
        )
        proposal = self._required_mapping(
            stage_results,
            "harness_optimization_proposal",
            "harness_optimization_apply",
        )
        decision = self._required_mapping(
            stage_results,
            "harness_optimization_decision",
            "harness_optimization_apply",
        )
        step = StepSpec(
            name="harness_optimization_apply",
            connector="harness_optimization_decision_to_candidate_manifest",
            handler=lambda _context: self._adapter(paths).run_apply(
                task,
                proposal,
                decision,
            ),
            input_roles=(
                "harness_optimization_task",
                "harness_optimization_proposal",
                "harness_optimization_decision",
            ),
            output_roles=(
                "harness_optimization_patch",
                "harness_optimization_candidate_manifest",
            ),
            metrics=lambda value: {
                "applied_action_count": value.get("harness_optimization_patch", {})
                .get("summary", {})
                .get("applied_action_count", 0),
                "skipped_action_count": value.get("harness_optimization_patch", {})
                .get("summary", {})
                .get("skipped_action_count", 0),
            },
            metadata=self._metadata(),
        )
        return self.step_runner(step)

    def _run_candidate_evaluation_stage(
        self,
        stage_results: RunResults,
    ) -> dict[str, Any]:
        paths = self.paths_factory()
        self.context_artifacts["harness_optimization_candidate_evaluation"] = (
            paths.candidate_evaluation
        )
        task = self._required_mapping(
            stage_results,
            "harness_optimization_task",
            "harness_optimization_candidate_evaluation",
        )
        proposal = self._required_mapping(
            stage_results,
            "harness_optimization_proposal",
            "harness_optimization_candidate_evaluation",
        )
        patch = self._required_mapping(
            stage_results,
            "harness_optimization_patch",
            "harness_optimization_candidate_evaluation",
        )
        candidate_manifest = self._required_mapping(
            stage_results,
            "harness_optimization_candidate_manifest",
            "harness_optimization_candidate_evaluation",
        )
        step = StepSpec(
            name="harness_optimization_candidate_evaluation",
            connector="harness_candidate_manifest_to_evaluation",
            handler=lambda _context: self._adapter(paths).run_candidate_evaluation(
                task,
                proposal,
                patch,
                candidate_manifest,
            ),
            input_roles=(
                "harness_optimization_patch",
                "harness_optimization_candidate_manifest",
            ),
            output_roles=("harness_optimization_candidate_evaluation",),
            metrics=lambda value: {
                "candidate_status": value.get("status"),
                "candidate_metric_count": len(value.get("candidate_metrics", {})),
            },
            metadata=self._metadata(),
        )
        return self.step_runner(step)

    def _run_metric_delta_stage(self, stage_results: RunResults) -> dict[str, Any]:
        paths = self.paths_factory()
        self.context_artifacts["harness_optimization_metric_delta"] = (
            paths.metric_delta
        )
        task = self._required_mapping(
            stage_results,
            "harness_optimization_task",
            "harness_optimization_metric_delta",
        )
        candidate_evaluation = self._required_mapping(
            stage_results,
            "harness_optimization_candidate_evaluation",
            "harness_optimization_metric_delta",
        )
        step = StepSpec(
            name="harness_optimization_metric_delta",
            connector="harness_candidate_evaluation_to_metric_delta",
            handler=lambda _context: self._adapter(paths).run_metric_delta(
                task,
                candidate_evaluation,
            ),
            input_roles=("harness_optimization_candidate_evaluation",),
            output_roles=("harness_optimization_metric_delta",),
            metrics=lambda value: {
                "improved_metric_count": value.get("summary", {}).get(
                    "improved_metric_count",
                    0,
                ),
                "regressed_metric_count": value.get("summary", {}).get(
                    "regressed_metric_count",
                    0,
                ),
            },
            metadata=self._metadata(),
        )
        return self.step_runner(step)

    def _run_final_decision_stage(self, stage_results: RunResults) -> dict[str, Any]:
        paths = self.paths_factory()
        self.context_artifacts["harness_optimization_final_decision"] = (
            paths.final_decision
        )
        task = self._required_mapping(
            stage_results,
            "harness_optimization_task",
            "harness_optimization_final_decision",
        )
        proposal = self._required_mapping(
            stage_results,
            "harness_optimization_proposal",
            "harness_optimization_final_decision",
        )
        schema_decision = self._required_mapping(
            stage_results,
            "harness_optimization_decision",
            "harness_optimization_final_decision",
        )
        patch = self._required_mapping(
            stage_results,
            "harness_optimization_patch",
            "harness_optimization_final_decision",
        )
        candidate_evaluation = self._required_mapping(
            stage_results,
            "harness_optimization_candidate_evaluation",
            "harness_optimization_final_decision",
        )
        metric_delta = self._required_mapping(
            stage_results,
            "harness_optimization_metric_delta",
            "harness_optimization_final_decision",
        )
        step = StepSpec(
            name="harness_optimization_final_decision",
            connector="harness_metric_delta_to_final_decision",
            handler=lambda _context: self._adapter(paths).run_final_decision(
                task,
                proposal,
                schema_decision,
                patch,
                candidate_evaluation,
                metric_delta,
            ),
            input_roles=(
                "harness_optimization_decision",
                "harness_optimization_patch",
                "harness_optimization_candidate_evaluation",
                "harness_optimization_metric_delta",
            ),
            output_roles=("harness_optimization_final_decision",),
            metrics=lambda value: {
                "final_decision": value.get("decision"),
                "regressed_metric_count": value.get("summary", {}).get(
                    "regressed_metric_count",
                    0,
                ),
            },
            metadata=self._metadata(),
        )
        return self.step_runner(step)


__all__ = [
    "CampaignOptimizationAdapter",
    "CampaignOptimizationStageChain",
]
