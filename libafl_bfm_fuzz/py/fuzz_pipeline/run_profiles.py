from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RunPlanProfile:
    """Named run-level DAG profile expressed as stage names."""

    name: str
    stage_names: tuple[str, ...]
    description: str = ""
    mode: str | None = None


DEFAULT_RUN_PLAN_PROFILES: Mapping[str, RunPlanProfile] = {
    "generate_and_validate": RunPlanProfile(
        name="generate_and_validate",
        stage_names=("corpus_generation", "corpus_validation"),
        description="Generate a corpus and validate it against the target manifest.",
    ),
    "coverage_run": RunPlanProfile(
        name="coverage_run",
        stage_names=("corpus_generation", "corpus_validation", "coverage_run"),
        description="Generate, validate, and replay the corpus with RTL coverage.",
    ),
    "coverage_report": RunPlanProfile(
        name="coverage_report",
        stage_names=(
            "corpus_generation",
            "corpus_validation",
            "coverage_run",
            "coverage_report",
        ),
        description="Run coverage replay and convert raw coverage into reports.",
    ),
    "feedback_fuzz": RunPlanProfile(
        name="feedback_fuzz",
        stage_names=(
            "corpus_generation",
            "corpus_validation",
            "coverage_run",
            "coverage_report",
            "coverage_feedback",
            "feedback_corpus_generation",
            "feedback_corpus_validation",
            "feedback_replay",
            "round_manifest",
        ),
        description="Full feedback-guided UVM-fuzz round.",
        mode="feedback_fuzz",
    ),
    "feedback_fuzz_with_evaluation": RunPlanProfile(
        name="feedback_fuzz_with_evaluation",
        stage_names=(
            "corpus_generation",
            "corpus_validation",
            "coverage_run",
            "coverage_report",
            "coverage_feedback",
            "feedback_corpus_generation",
            "feedback_corpus_validation",
            "feedback_replay",
            "round_manifest",
            "round_evaluation",
        ),
        description="Full feedback-guided UVM-fuzz round followed by evaluation.",
        mode="feedback_fuzz",
    ),
    "no_feedback": RunPlanProfile(
        name="no_feedback",
        stage_names=(
            "corpus_generation",
            "corpus_validation",
            "coverage_run",
            "coverage_report",
            "round_manifest",
        ),
        description="Baseline UVM-fuzz round without feedback generation or replay.",
        mode="no_feedback",
    ),
    "no_feedback_with_evaluation": RunPlanProfile(
        name="no_feedback_with_evaluation",
        stage_names=(
            "corpus_generation",
            "corpus_validation",
            "coverage_run",
            "coverage_report",
            "round_manifest",
            "round_evaluation",
        ),
        description="Baseline UVM-fuzz round followed by evaluation.",
        mode="no_feedback",
    ),
}

DEFAULT_RUN_PLAN_STAGE_NAMES: Mapping[str, tuple[str, ...]] = {
    name: profile.stage_names for name, profile in DEFAULT_RUN_PLAN_PROFILES.items()
}

DEFAULT_CAMPAIGN_PLAN_PROFILES: Mapping[str, RunPlanProfile] = {
    "campaign_manifest": RunPlanProfile(
        name="campaign_manifest",
        stage_names=("campaign_manifest",),
        description="Write the campaign manifest after all rounds finish.",
    ),
    "campaign_with_evaluation": RunPlanProfile(
        name="campaign_with_evaluation",
        stage_names=("campaign_manifest", "campaign_evaluation"),
        description="Write the campaign manifest and a follow-on evaluation report.",
    ),
}


def profile_stage_names(
    name: str,
    profiles: Mapping[str, RunPlanProfile] = DEFAULT_RUN_PLAN_PROFILES,
) -> tuple[str, ...]:
    try:
        return profiles[name].stage_names
    except KeyError as exc:
        raise ValueError(f"unknown run plan profile: {name}") from exc
