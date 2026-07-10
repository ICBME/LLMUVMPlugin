"""Metrics aggregation for Spec2IR real-data evaluation."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .runner import RealDataCaseResult


def aggregate_results(results: Iterable[RealDataCaseResult]) -> dict[str, Any]:
    result_list = list(results)
    total = len(result_list)
    processed = [result for result in result_list if result.status != "not_run"]
    crashed = [result for result in result_list if result.status == "crashed"]
    schema_valid_count = sum(1 for result in processed if result.schema_valid)
    review_status_counts = Counter(
        result.review.get("status", "not_run") if result.review else "not_run"
        for result in processed
    )
    repair_status_counts = Counter(
        result.repair_result.get("status", "not_run") if result.repair_result else "not_run"
        for result in processed
    )
    stage_status_counts = Counter(
        f"{stage.name}:{stage.status}"
        for result in processed
        for stage in result.stages
    )
    llm_generation_passed_count = stage_status_counts.get("llm_generation:passed", 0)
    llm_agent_passed_count = stage_status_counts.get("llm_agent:passed", 0)
    llm_effective_count = llm_generation_passed_count + llm_agent_passed_count
    readiness_status_counts = Counter(
        result.readiness.get("status", "not_run") if result.readiness else "not_run"
        for result in processed
    )
    automation_route_counts: Counter[str] = Counter()
    deterministic_repair_count = 0
    agent_model_call_count = 0
    agent_tool_call_count = 0
    agent_progress_submission_count = 0
    agent_no_progress_submission_count = 0
    agent_regressive_submission_count = 0
    agent_patch_count = 0
    agent_patch_accepted_count = 0
    agent_patch_rejected_count = 0
    agent_patch_operation_count = 0
    agent_context_char_count = 0
    agent_response_char_count = 0
    agent_input_token_count = 0
    agent_output_token_count = 0
    cases_with_claims = 0
    cases_needing_human = 0
    covered_claims = 0
    normative_claims = 0
    for result in processed:
        result_needs_human = False
        if result.repair_result:
            patch_history = result.repair_result.get("patch_history", [])
            if isinstance(patch_history, list):
                agent_patch_count += len(patch_history)
                for patch in patch_history:
                    if not isinstance(patch, dict):
                        continue
                    if patch.get("accepted"):
                        agent_patch_accepted_count += 1
                    else:
                        agent_patch_rejected_count += 1
                    operations = patch.get("patch", {}).get("operations")
                    if isinstance(operations, list):
                        agent_patch_operation_count += len(operations)
            provenance = result.repair_result.get("llm_provenance", {})
            if isinstance(provenance, dict):
                agent_model_call_count += int(provenance.get("attempt_count") or 0)
            attempts = result.repair_result.get("attempts", [])
            if isinstance(attempts, list):
                agent_tool_call_count += sum(
                    1 for attempt in attempts if isinstance(attempt, dict) and attempt.get("tool_call")
                )
                for attempt in attempts:
                    if not isinstance(attempt, dict):
                        continue
                    response = attempt.get("response")
                    if isinstance(response, dict):
                        agent_response_char_count += len(str(response.get("content") or ""))
                        metadata = response.get("metadata")
                        if isinstance(metadata, dict):
                            agent_context_char_count += int(metadata.get("context_char_count") or 0)
                            usage = metadata.get("token_usage")
                            if isinstance(usage, dict):
                                agent_input_token_count += int(
                                    usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                                )
                                agent_output_token_count += int(
                                    usage.get("output_tokens") or usage.get("completion_tokens") or 0
                                )
                    progress = attempt.get("harness_result", {}).get("progress")
                    if not isinstance(progress, dict):
                        continue
                    if progress.get("made_progress"):
                        agent_progress_submission_count += 1
                    if progress.get("no_progress"):
                        agent_no_progress_submission_count += 1
                    if (
                        not progress.get("made_progress")
                        and int(progress.get("introduced_count") or 0)
                        > int(progress.get("resolved_count") or 0)
                    ):
                        agent_regressive_submission_count += 1
            for decision in result.repair_result.get("automation_decisions", []):
                route = str(decision.get("route") or "unknown")
                automation_route_counts[route] += 1
            deterministic_repair_count += len(result.repair_result.get("deterministic_repairs", []))
            if result.repair_result.get("status") == "needs_human_input":
                result_needs_human = True
        if result.review:
            completeness = result.review.get("completeness", {})
            case_normative = len(completeness.get("normative_claims", []) or [])
            case_covered = len(completeness.get("covered_claims", []) or [])
            normative_claims += case_normative
            covered_claims += case_covered
            if case_normative > 0:
                cases_with_claims += 1
            if result.review.get("status") == "needs_human_input":
                result_needs_human = True
        if result_needs_human:
            cases_needing_human += 1
    return {
        "case_count": total,
        "processed_count": len(processed),
        "crash_count": len(crashed),
        "schema_valid_count": schema_valid_count,
        "schema_valid_rate": ratio(schema_valid_count, len(processed)),
        "cases_with_claims": cases_with_claims,
        "claim_extraction_rate": ratio(cases_with_claims, len(processed)),
        "normative_claim_count": normative_claims,
        "covered_claim_count": covered_claims,
        "covered_claim_rate": ratio(covered_claims, normative_claims),
        "human_escalation_count": cases_needing_human,
        "stage_human_signal_count": review_status_counts.get("needs_human_input", 0)
        + repair_status_counts.get("needs_human_input", 0),
        "review_status_counts": dict(sorted(review_status_counts.items())),
        "repair_status_counts": dict(sorted(repair_status_counts.items())),
        "readiness_status_counts": dict(sorted(readiness_status_counts.items())),
        "stage_status_counts": dict(sorted(stage_status_counts.items())),
        "llm_effective_count": llm_effective_count,
        "llm_agent_passed_count": llm_agent_passed_count,
        "llm_agent_failed_count": stage_status_counts.get("llm_agent:failed", 0),
        "llm_generation_passed_count": llm_generation_passed_count,
        "llm_generation_failed_count": stage_status_counts.get("llm_generation:failed", 0),
        "automation_route_counts": dict(sorted(automation_route_counts.items())),
        "deterministic_repair_count": deterministic_repair_count,
        "agent_model_call_count": agent_model_call_count,
        "agent_tool_call_count": agent_tool_call_count,
        "agent_progress_submission_count": agent_progress_submission_count,
        "agent_no_progress_submission_count": agent_no_progress_submission_count,
        "agent_regressive_submission_count": agent_regressive_submission_count,
        "agent_patch_count": agent_patch_count,
        "agent_patch_accepted_count": agent_patch_accepted_count,
        "agent_patch_rejected_count": agent_patch_rejected_count,
        "agent_patch_operation_count": agent_patch_operation_count,
        "agent_context_char_count": agent_context_char_count,
        "agent_response_char_count": agent_response_char_count,
        "agent_input_token_count": agent_input_token_count,
        "agent_output_token_count": agent_output_token_count,
    }


def ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator
