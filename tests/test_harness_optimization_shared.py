from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libafl_bfm_fuzz" / "py"))

from fuzz_pipeline.harness_candidate_regression import (  # noqa: E402
    build_candidate_promotion_package as build_fuzz_candidate_promotion_package,
    build_candidate_variant_ranking as build_fuzz_candidate_variant_ranking,
)
from harness_optimization.candidate_execution import (  # noqa: E402
    CandidateAcceptanceThresholds,
)
from harness_optimization.candidate_validation import (  # noqa: E402
    build_candidate_promotion_package as build_shared_candidate_promotion_package,
    build_candidate_variant_ranking as build_shared_candidate_variant_ranking,
    combined_variant_evaluation,
)
from harness_optimization.rules import (  # noqa: E402
    build_candidate_final_decision,
    build_candidate_metric_delta,
    safe_action_dsl_schema,
    validate_harness_optimization_proposal,
)


def test_shared_rules_build_generic_metric_delta_and_final_decision() -> None:
    task = {
        "target": "demo",
        "run_id": "run-1",
        "constraints": {
            "allowed_action_types": ["scoreboard_check"],
            "safe_sandbox_action_types": ["scoreboard_check"],
            "safe_action_dsl": safe_action_dsl_schema(),
        },
        "evidence_index": {"span_ids": ["span-1"]},
    }
    proposal = {
        "schema_version": 1,
        "kind": "demo.proposal",
        "proposal_id": "proposal-1",
        "status": "proposed",
        "actions": [
            {
                "action_id": "scoreboard-1",
                "action_type": "scoreboard_check",
                "payload": {
                    "mode": "field_equals",
                    "field": "result.actual",
                    "expected": "ok",
                },
                "evidence_refs": [{"span_id": "span-1"}],
            }
        ],
        "evidence_refs": [{"span_id": "span-1"}],
    }
    validation = validate_harness_optimization_proposal(
        proposal,
        task=task,
        proposal_kind="demo.proposal",
        default_allowed_action_types=("scoreboard_check",),
    )
    candidate_evaluation = {
        "status": "passed",
        "baseline_metrics": {
            "uncovered_line_count": 10,
            "directive_count": 1,
        },
        "candidate_metrics": {
            "uncovered_line_count": 8,
            "directive_count": 2,
        },
        "acceptance_thresholds": {
            "min_improved_metric_count": 1,
            "max_regressed_metric_count": 0,
            "max_flaky_metric_count": 0,
            "accepted_candidate_statuses": ["passed"],
        },
    }
    patch = {
        "status": "applied",
        "candidate_id": "candidate-1",
        "sandbox_dir": "/tmp/candidate-1",
        "summary": {"applied_action_count": 1},
    }
    schema_decision = {"decision": "accepted"}

    metric_delta = build_candidate_metric_delta(
        task=task,
        candidate_evaluation=candidate_evaluation,
        kind="demo.metric_delta",
    )
    final_decision = build_candidate_final_decision(
        task=task,
        proposal=proposal,
        schema_decision=schema_decision,
        patch=patch,
        candidate_evaluation=candidate_evaluation,
        metric_delta=metric_delta,
        kind="demo.final_decision",
    )

    directive_count = next(
        item for item in metric_delta["comparisons"] if item["metric"] == "directive_count"
    )
    assert validation["valid"] is True
    assert metric_delta["kind"] == "demo.metric_delta"
    assert directive_count["role"] == "informational"
    assert directive_count["gates_acceptance"] is False
    assert final_decision["kind"] == "demo.final_decision"
    assert final_decision["decision"] == "accepted_for_review"


def test_shared_rules_reject_non_list_action_evidence_refs() -> None:
    task = {
        "target": "demo",
        "run_id": "run-1",
        "constraints": {
            "allowed_action_types": ["scoreboard_check"],
            "safe_sandbox_action_types": ["scoreboard_check"],
            "safe_action_dsl": safe_action_dsl_schema(),
        },
        "evidence_index": {"span_ids": ["span-1"]},
    }
    proposal = {
        "schema_version": 1,
        "kind": "demo.proposal",
        "proposal_id": "proposal-1",
        "status": "proposed",
        "actions": [
            {
                "action_id": "scoreboard-1",
                "action_type": "scoreboard_check",
                "payload": {
                    "mode": "field_equals",
                    "field": "result.actual",
                    "expected": "ok",
                },
                "evidence_refs": {"span_id": "span-bad"},
            }
        ],
        "evidence_refs": [{"span_id": "span-1"}],
    }

    validation = validate_harness_optimization_proposal(
        proposal,
        task=task,
        proposal_kind="demo.proposal",
        default_allowed_action_types=("scoreboard_check",),
    )

    assert validation["valid"] is False
    assert {
        "path": "actions[0].evidence_refs",
        "message": "expected list",
    } in validation["errors"]


def test_shared_candidate_validation_uses_generic_kinds() -> None:
    overlay = {
        "variants": [
            {
                "variant_id": "combined",
                "variant_type": "combined_actions",
                "action_ids": ["action-1"],
                "action_types": ["scoreboard_check"],
                "validation_status": "selected_for_regression",
            }
        ]
    }
    ranking = build_shared_candidate_variant_ranking(
        action_overlay=overlay,
        baseline_metrics={"uncovered_line_count": 9},
        candidate_metrics={"uncovered_line_count": 7},
        selected_variant_id="combined",
    )
    promotion = build_shared_candidate_promotion_package(
        task={"target": "demo", "run_id": "run-1"},
        proposal={"proposal_id": "proposal-1", "actions": [], "evidence_refs": []},
        patch={"sandbox_dir": "/tmp/candidate-1"},
        candidate_manifest={"candidate_id": "candidate-1"},
        candidate_metrics={"uncovered_line_count": 7},
        baseline_metrics={"uncovered_line_count": 9},
        ranking=ranking,
        thresholds=CandidateAcceptanceThresholds(min_improved_metric_count=0),
    )

    assert ranking["kind"] == "harness_optimization.candidate_variant_ranking"
    assert promotion["kind"] == "harness_optimization.candidate_promotion_package"
    assert (
        promotion["minimal_promotion_candidate"]["kind"]
        == "harness_optimization.minimal_promotion_candidate"
    )


def test_shared_combined_variant_evaluation_omits_missing_optional_artifacts() -> None:
    evaluation = combined_variant_evaluation(
        overlay={
            "variants": [
                {
                    "variant_id": "combined",
                    "variant_type": "combined_actions",
                }
            ]
        },
        metrics={"uncovered_line_count": 7},
        baseline_metrics={"uncovered_line_count": 9},
        campaign_config=SimpleNamespace(
            campaign_manifest_out=None,
            campaign_evaluation_out=None,
        ),
        run_config_path=Path("/tmp/candidate_regression_config.json"),
        runtime_metrics_path=Path("/tmp/candidate_runtime_metrics.json"),
        adapter_results=(),
    )

    assert evaluation["artifacts"]["candidate_regression_config"].endswith(
        "candidate_regression_config.json"
    )
    assert evaluation["artifacts"]["candidate_runtime_metrics"].endswith(
        "candidate_runtime_metrics.json"
    )
    assert "candidate_campaign_manifest" not in evaluation["artifacts"]
    assert "candidate_campaign_evaluation" not in evaluation["artifacts"]


def test_business_wrappers_preserve_libafl_specific_kinds() -> None:
    overlay = {
        "variants": [
            {
                "variant_id": "combined",
                "variant_type": "combined_actions",
                "action_ids": ["action-1"],
                "action_types": ["scoreboard_check"],
                "validation_status": "selected_for_regression",
            }
        ]
    }
    ranking = build_fuzz_candidate_variant_ranking(
        action_overlay=overlay,
        baseline_metrics={"uncovered_line_count": 9},
        candidate_metrics={"uncovered_line_count": 7},
        selected_variant_id="combined",
    )
    promotion = build_fuzz_candidate_promotion_package(
        task={"target": "demo", "run_id": "run-1"},
        proposal={"proposal_id": "proposal-1", "actions": [], "evidence_refs": []},
        patch={"sandbox_dir": "/tmp/candidate-1"},
        candidate_manifest={"candidate_id": "candidate-1"},
        candidate_metrics={"uncovered_line_count": 7},
        baseline_metrics={"uncovered_line_count": 9},
        ranking=ranking,
        thresholds=CandidateAcceptanceThresholds(min_improved_metric_count=0),
    )

    assert ranking["kind"] == "libafl_bfm_fuzz.harness_candidate_variant_ranking"
    assert promotion["kind"] == "libafl_bfm_fuzz.harness_candidate_promotion_package"
    assert (
        promotion["minimal_promotion_candidate"]["kind"]
        == "libafl_bfm_fuzz.harness_minimal_promotion_candidate"
    )
