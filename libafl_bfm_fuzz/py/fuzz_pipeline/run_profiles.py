from __future__ import annotations

from typing import Mapping

from harness_optimization.planning import RunPlanProfile, profile_stage_names as _profile_stage_names


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
    "campaign_with_evaluation_and_optimization": RunPlanProfile(
        name="campaign_with_evaluation_and_optimization",
        stage_names=(
            "campaign_manifest",
            "campaign_evaluation",
            "harness_optimization_task",
            "harness_optimization_proposal",
            "harness_optimization_decision",
        ),
        description=(
            "Write campaign evaluation and phase-one harness optimization "
            "task/proposal/decision artifacts."
        ),
    ),
    "campaign_with_evaluation_and_llm_advice": RunPlanProfile(
        name="campaign_with_evaluation_and_llm_advice",
        stage_names=(
            "campaign_manifest",
            "campaign_evaluation",
            "harness_optimization_task",
            "harness_optimization_proposal",
            "harness_optimization_decision",
            "harness_optimization_advice_report",
        ),
        description=(
            "Write campaign evaluation, generate a schema-reviewed harness "
            "optimization proposal, and emit an advice-only report without "
            "sandbox apply or candidate regression."
        ),
    ),
    "campaign_with_evaluation_and_optimization_validation": RunPlanProfile(
        name="campaign_with_evaluation_and_optimization_validation",
        stage_names=(
            "campaign_manifest",
            "campaign_evaluation",
            "harness_optimization_task",
            "harness_optimization_proposal",
            "harness_optimization_decision",
            "harness_optimization_apply",
            "harness_optimization_candidate_evaluation",
            "harness_optimization_metric_delta",
            "harness_optimization_final_decision",
        ),
        description=(
            "Write campaign evaluation, generate a harness optimization proposal, "
            "materialize safe sandbox candidate artifacts, evaluate the candidate, "
            "and emit metric delta plus final review decision artifacts."
        ),
    ),
    "campaign_with_evaluation_and_optimization_real_validation": RunPlanProfile(
        name="campaign_with_evaluation_and_optimization_real_validation",
        stage_names=(
            "campaign_manifest",
            "campaign_evaluation",
            "harness_optimization_task",
            "harness_optimization_proposal",
            "harness_optimization_decision",
            "harness_optimization_apply",
            "harness_optimization_candidate_evaluation",
            "harness_optimization_metric_delta",
            "harness_optimization_final_decision",
        ),
        description=(
            "Run the optimization validation stage sequence; CLI helpers select "
            "the real sandbox candidate regression backend for this profile."
        ),
    ),
}


def profile_stage_names(
    name: str,
    profiles: Mapping[str, RunPlanProfile] = DEFAULT_RUN_PLAN_PROFILES,
) -> tuple[str, ...]:
    return _profile_stage_names(name, profiles)


__all__ = [
    "DEFAULT_CAMPAIGN_PLAN_PROFILES",
    "DEFAULT_RUN_PLAN_PROFILES",
    "DEFAULT_RUN_PLAN_STAGE_NAMES",
    "RunPlanProfile",
    "profile_stage_names",
]
