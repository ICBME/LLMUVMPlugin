"""Automation routing for SemanticSpecIR review findings.

This module keeps the policy for deciding whether a review finding can be
retried with local context and an LLM separate from the repair loop itself.
The repair loop remains responsible for invoking backends and re-running
review after every candidate update.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .claim_extraction import (
    evidence_from_spec_claims,
    extract_spec_claims,
)
from .schema import (
    SourceDocument,
    json_round_trip,
    load_source_documents,
)


DONE = "done"
RULE_REPAIR = "rule_repair"
LLM_REPAIR = "llm_repair"
LLM_FORMALIZE = "llm_formalize"
HUMAN_REQUIRED = "human_required"


AUTO_FORMALIZATION_CODES = {
    "ast_missing",
    "ast_missing_for_obligation",
    "ast_node_missing",
    "condition_text_fallback",
    "missing_protocol_clock",
    "missing_temporal_clock",
    "operation_operand_mismatch",
    "operation_operand_text_fallback",
    "operation_operands_missing",
    "placeholder_signal",
    "placeholder_text_expr",
    "protocol_text_fallback",
    "semantic_claim_placeholder",
    "text_only_behavior",
    "text_only_root",
    "text_response",
    "text_trigger",
}

HUMAN_ONLY_CODES = {
    "blocking_open_question",
    "unknown_reset_polarity",
    "unknown_reset_synchrony",
}

AUTO_GAP_KINDS = {
    "incomplete",
    "unformalized",
}

HUMAN_GAP_KINDS = {
    "ambiguous",
    "conflict",
    "missing_context",
}


@dataclass(frozen=True)
class AutomationPolicyRule:
    name: str
    route: str
    reason: str
    statuses: tuple[str, ...] = ()
    issue_codes: tuple[str, ...] = ()
    gap_kinds: tuple[str, ...] = ()
    formalization_statuses: tuple[str, ...] = ()
    finding_stages: tuple[str, ...] = ()
    requires_uncovered_source_spans: bool = False
    requires_placeholder_only_claims: bool = False


@dataclass(frozen=True)
class AutomationDecision:
    route: str
    reason: str
    claim_ids: tuple[str, ...] = ()
    finding_paths: tuple[str, ...] = ()
    issue_codes: tuple[str, ...] = ()
    policy_rule: str = ""

    @property
    def needs_llm(self) -> bool:
        return self.route in {LLM_REPAIR, LLM_FORMALIZE}

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "reason": self.reason,
            "claim_ids": list(self.claim_ids),
            "finding_paths": list(self.finding_paths),
            "issue_codes": list(self.issue_codes),
            "policy_rule": self.policy_rule,
        }


@dataclass(frozen=True)
class AutomationPolicyGraph:
    rules: tuple[AutomationPolicyRule, ...]

    def decide(self, review: dict[str, Any], semantic_ir: dict[str, Any]) -> AutomationDecision:
        facts = collect_automation_facts(review, semantic_ir)
        for rule in self.rules:
            if policy_rule_matches(rule, facts):
                return AutomationDecision(
                    rule.route,
                    rule.reason,
                    claim_ids=facts["claim_ids"],
                    finding_paths=facts["finding_paths"],
                    issue_codes=tuple(sorted(facts["issue_codes"])),
                    policy_rule=rule.name,
                )
        return AutomationDecision(
            HUMAN_REQUIRED,
            "no automation policy rule matched this review",
            claim_ids=facts["claim_ids"],
            finding_paths=facts["finding_paths"],
            issue_codes=tuple(sorted(facts["issue_codes"])),
            policy_rule="default_human_required",
        )


@dataclass(frozen=True)
class DeterministicRepairResult:
    semantic_ir: dict[str, Any]
    changed: bool
    actions: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "actions": list(self.actions),
        }


DEFAULT_AUTOMATION_POLICY_GRAPH = AutomationPolicyGraph(
    rules=(
        AutomationPolicyRule(
            name="passed",
            route=DONE,
            reason="review already passed",
            statuses=("passed",),
        ),
        AutomationPolicyRule(
            name="failed_schema_traceability_or_consistency",
            route=LLM_REPAIR,
            reason="schema, traceability, or consistency errors may be repairable from supplied sources",
            statuses=("failed",),
            finding_stages=("schema_review", "traceability_review", "semantic_consistency_review", "completeness_review"),
        ),
        AutomationPolicyRule(
            name="human_only_issue_codes",
            route=HUMAN_REQUIRED,
            reason="review contains questions or semantic details that require design intent",
            statuses=("needs_human_input",),
            issue_codes=tuple(sorted(HUMAN_ONLY_CODES)),
        ),
        AutomationPolicyRule(
            name="ambiguous_or_conflicting_formalization",
            route=HUMAN_REQUIRED,
            reason="ambiguous or conflicting formalization status requires human resolution",
            statuses=("needs_human_input",),
            formalization_statuses=("ambiguous", "conflict"),
        ),
        AutomationPolicyRule(
            name="human_gap_kind",
            route=HUMAN_REQUIRED,
            reason="semantic gaps require external context or conflict resolution",
            statuses=("needs_human_input",),
            gap_kinds=tuple(sorted(HUMAN_GAP_KINDS)),
        ),
        AutomationPolicyRule(
            name="uncovered_source_spans",
            route=LLM_REPAIR,
            reason="source semantic spans are uncovered and can be re-extracted from provided spec text",
            statuses=("needs_human_input",),
            requires_uncovered_source_spans=True,
        ),
        AutomationPolicyRule(
            name="local_formalization_gap",
            route=LLM_FORMALIZE,
            reason="blocking findings are local formalization gaps that an LLM may resolve from supplied context",
            statuses=("needs_human_input",),
            issue_codes=tuple(sorted(AUTO_FORMALIZATION_CODES)),
        ),
        AutomationPolicyRule(
            name="auto_gap_kind",
            route=LLM_FORMALIZE,
            reason="semantic gaps are marked incomplete or unformalized and may be resolvable from supplied context",
            statuses=("needs_human_input",),
            gap_kinds=tuple(sorted(AUTO_GAP_KINDS)),
        ),
        AutomationPolicyRule(
            name="placeholder_only_claims",
            route=LLM_FORMALIZE,
            reason="placeholder-only claims may be formalized from supplied context",
            statuses=("needs_human_input",),
            requires_placeholder_only_claims=True,
        ),
    )
)


def classify_review_for_automation(
    review: dict[str, Any],
    semantic_ir: dict[str, Any],
    *,
    policy_graph: AutomationPolicyGraph | None = None,
) -> AutomationDecision:
    """Route a review report to LLM automation or human review."""

    return (policy_graph or DEFAULT_AUTOMATION_POLICY_GRAPH).decide(review, semantic_ir)


def deterministic_repair_semantic_spec_ir(
    semantic_ir: dict[str, Any],
    *,
    spec_paths: tuple[str | Path, ...] = (),
) -> DeterministicRepairResult:
    """Repair mechanical traceability fields without inventing semantics."""

    current = json_round_trip(semantic_ir)
    actions: list[dict[str, Any]] = []
    documents = load_existing_source_documents(spec_paths)
    if documents:
        source_payloads = [document.payload() for document in documents]
        if current.get("sources") != source_payloads:
            current["sources"] = source_payloads
            actions.append({"kind": "refresh_sources", "count": len(source_payloads)})
        expected_claims = extract_spec_claims(documents)
        repair_spec_claims(current, expected_claims, actions)
        repair_evidence(current, actions)
    return DeterministicRepairResult(
        semantic_ir=current,
        changed=bool(actions),
        actions=tuple(actions),
    )


def load_existing_source_documents(spec_paths: tuple[str | Path, ...]) -> tuple[SourceDocument, ...]:
    existing_paths = tuple(path for path in spec_paths if Path(path).exists())
    if not existing_paths:
        return ()
    return load_source_documents(existing_paths)


def repair_spec_claims(
    semantic_ir: dict[str, Any],
    expected_claims: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> None:
    current_claims = semantic_ir.get("spec_claims")
    if not isinstance(current_claims, list):
        semantic_ir["spec_claims"] = deepcopy(expected_claims)
        actions.append({"kind": "rebuild_spec_claims", "count": len(expected_claims)})
        return
    expected_by_id = {
        claim.get("id"): claim
        for claim in expected_claims
        if isinstance(claim.get("id"), str)
    }
    repaired = False
    for index, claim in enumerate(current_claims):
        if not isinstance(claim, dict):
            continue
        expected = expected_by_id.get(claim.get("id"))
        if expected is None:
            continue
        for key in ("fingerprint", "decomposition"):
            if claim.get(key) != expected.get(key):
                claim[key] = deepcopy(expected.get(key))
                repaired = True
        if not isinstance(claim.get("normative"), bool):
            claim["normative"] = expected.get("normative", True)
            repaired = True
    if repaired:
        actions.append({"kind": "repair_claim_metadata"})


def repair_evidence(
    semantic_ir: dict[str, Any],
    actions: list[dict[str, Any]],
) -> None:
    claims = semantic_ir.get("spec_claims")
    if not isinstance(claims, list):
        return
    expected_evidence = evidence_from_spec_claims([claim for claim in claims if isinstance(claim, dict)])
    evidence = semantic_ir.get("evidence")
    if not isinstance(evidence, list):
        semantic_ir["evidence"] = expected_evidence
        actions.append({"kind": "rebuild_evidence", "count": len(expected_evidence)})
        return
    expected_by_claim = {
        tuple(item.get("claim_ids", [])): item
        for item in expected_evidence
        if isinstance(item.get("claim_ids"), list)
    }
    repaired = False
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            continue
        claim_key = tuple(item.get("claim_ids", [])) if isinstance(item.get("claim_ids"), list) else ()
        expected = expected_by_claim.get(claim_key)
        if expected is None:
            continue
        for key in ("source_id", "line_start", "line_end", "quote"):
            if item.get(key) != expected.get(key):
                item[key] = deepcopy(expected.get(key))
                repaired = True
        if not isinstance(item.get("id"), str) or not item["id"]:
            item["id"] = f"ev{index + 1}"
            repaired = True
    if repaired:
        actions.append({"kind": "repair_evidence_traceability"})


def collect_automation_facts(review: dict[str, Any], semantic_ir: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": str(review.get("status") or ""),
        "issue_codes": missing_issue_codes(review),
        "claim_ids": affected_claim_ids(review),
        "finding_paths": finding_paths(review),
        "finding_stages": finding_stages(review),
        "gap_kinds": unresolved_gap_kinds(semantic_ir),
        "formalization_statuses": blocking_formalization_statuses(semantic_ir),
        "has_uncovered_source_spans": has_uncovered_source_spans(review),
        "has_placeholder_only_claims": has_placeholder_only_claims(review),
    }


def policy_rule_matches(rule: AutomationPolicyRule, facts: dict[str, Any]) -> bool:
    if rule.statuses and facts["status"] not in rule.statuses:
        return False
    if rule.issue_codes and not set(rule.issue_codes).intersection(facts["issue_codes"]):
        return False
    if rule.gap_kinds and not set(rule.gap_kinds).intersection(facts["gap_kinds"]):
        return False
    if rule.formalization_statuses and not set(rule.formalization_statuses).intersection(facts["formalization_statuses"]):
        return False
    if rule.finding_stages and not set(rule.finding_stages).intersection(facts["finding_stages"]):
        return False
    if rule.requires_uncovered_source_spans and not facts["has_uncovered_source_spans"]:
        return False
    if rule.requires_placeholder_only_claims and not facts["has_placeholder_only_claims"]:
        return False
    return True


def finding_paths(review: dict[str, Any]) -> tuple[str, ...]:
    findings = review.get("findings", [])
    if not isinstance(findings, list):
        return ()
    return tuple(
        str(finding.get("path"))
        for finding in findings
        if isinstance(finding, dict) and finding.get("path")
    )


def finding_stages(review: dict[str, Any]) -> set[str]:
    findings = review.get("findings", [])
    if not isinstance(findings, list):
        return set()
    return {
        finding["stage"]
        for finding in findings
        if isinstance(finding, dict) and isinstance(finding.get("stage"), str)
    }


def has_uncovered_source_spans(review: dict[str, Any]) -> bool:
    source_coverage = review.get("completeness", {}).get("source_claim_coverage", {})
    uncovered_spans = source_coverage.get("uncovered_spans", []) if isinstance(source_coverage, dict) else []
    return isinstance(uncovered_spans, list) and bool(uncovered_spans)


def has_placeholder_only_claims(review: dict[str, Any]) -> bool:
    completeness = review.get("completeness", {})
    placeholder_only = completeness.get("placeholder_only_claims", []) if isinstance(completeness, dict) else []
    return isinstance(placeholder_only, list) and bool(placeholder_only)


def missing_issue_codes(review: dict[str, Any]) -> set[str]:
    codes: set[str] = set()
    completeness = review.get("completeness", {})
    claim_obligations = completeness.get("claim_obligations", []) if isinstance(completeness, dict) else []
    if isinstance(claim_obligations, list):
        for claim in claim_obligations:
            if not isinstance(claim, dict):
                continue
            for issue in claim.get("missing_obligations", []):
                if isinstance(issue, dict) and isinstance(issue.get("code"), str):
                    codes.add(issue["code"])
    coverage = completeness.get("obligation_coverage", {}) if isinstance(completeness, dict) else {}
    claims = coverage.get("claims", []) if isinstance(coverage, dict) else []
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            obligations = claim.get("obligations", [])
            if not isinstance(obligations, list):
                continue
            for obligation in obligations:
                if not isinstance(obligation, dict):
                    continue
                for issue in obligation.get("issues", []):
                    if isinstance(issue, dict) and isinstance(issue.get("code"), str):
                        codes.add(issue["code"])
    return codes


def affected_claim_ids(review: dict[str, Any]) -> tuple[str, ...]:
    completeness = review.get("completeness", {})
    if not isinstance(completeness, dict):
        return ()
    claim_ids: set[str] = set()
    for key in ("partial_claims", "uncovered_claims", "placeholder_only_claims"):
        value = completeness.get(key, [])
        if isinstance(value, list):
            claim_ids.update(item for item in value if isinstance(item, str))
    claim_obligations = completeness.get("claim_obligations", [])
    if isinstance(claim_obligations, list):
        for item in claim_obligations:
            if isinstance(item, dict) and item.get("status") != "complete":
                claim_id = item.get("claim_id")
                if isinstance(claim_id, str):
                    claim_ids.add(claim_id)
    return tuple(sorted(claim_ids))


def blocking_formalization_statuses(semantic_ir: dict[str, Any]) -> set[str]:
    statuses: set[str] = set()
    elements = semantic_ir.get("semantic_elements", [])
    if not isinstance(elements, list):
        return statuses
    for element in elements:
        if isinstance(element, dict) and isinstance(element.get("formalization_status"), str):
            status = element["formalization_status"]
            if status in {"ambiguous", "conflict", "incomplete", "needs_human_review"}:
                statuses.add(status)
    return statuses


def unresolved_gap_kinds(semantic_ir: dict[str, Any]) -> set[str]:
    kinds: set[str] = set()
    gaps = semantic_ir.get("semantic_gaps", [])
    if not isinstance(gaps, list):
        return kinds
    for gap in gaps:
        if isinstance(gap, dict) and isinstance(gap.get("kind"), str):
            kinds.add(gap["kind"])
    return kinds
