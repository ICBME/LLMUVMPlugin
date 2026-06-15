"""Conservative source-claim extraction for SemanticSpecIR drafts."""

from __future__ import annotations

import re
from typing import Any, Iterable

from .representation_ast import (
    representation_requires_human_review,
    semantic_representation_for_claim,
)
from .schema import ALLOWED_SEMANTIC_ELEMENT_KINDS, SourceDocument


def extract_spec_claims(documents: tuple[SourceDocument, ...]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for document in documents:
        in_code_block = False
        for line_no, raw_line in enumerate(document.lines, start=1):
            stripped = raw_line.strip()
            if stripped.startswith("```"):
                in_code_block = not in_code_block
                continue
            if in_code_block or not stripped or stripped.startswith("#"):
                continue
            if is_markdown_table_rule(stripped):
                continue
            for quote in split_claim_quotes(stripped):
                summary = summarize_claim_quote(quote)
                if not summary:
                    continue
                claims.append(
                    {
                        "id": f"claim{len(claims) + 1}",
                        "source_id": document.id,
                        "line_start": line_no,
                        "line_end": line_no,
                        "quote": quote[:240],
                        "summary": summary,
                        "kind": classify_claim_kind(summary),
                        "strength": classify_claim_strength(summary),
                        "normative": True,
                        "subjects": infer_claim_subjects(summary),
                    }
                )
    return claims


def split_claim_quotes(line: str) -> list[str]:
    normalized = line.strip()
    if not normalized:
        return []
    if re.match(r"^[-*]\s+", normalized):
        return [normalized]
    parts = [part.strip() for part in re.split(r"(?<=[.;])\s+", normalized) if part.strip()]
    return parts or [normalized]


def summarize_claim_quote(quote: str) -> str:
    text = re.sub(r"^[-*]\s+", "", quote.strip())
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ;")


def is_markdown_table_rule(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", line))


def classify_claim_kind(summary: str) -> str:
    text = summary.lower()
    if re.match(r"^when\s+[a-z_][a-z0-9_]*\s*(?:==|=|!=)\s*[^,.;]+\s*,\s*[a-z_][a-z0-9_]*\s*(?:==|=)", text):
        return "functional_behavior"
    if any(word in text for word in ("reset", "rst")):
        return "reset"
    if any(word in text for word in ("cycle", "clock", "posedge", "negedge", "pulse", "latency")):
        return "timing"
    if any(word in text for word in ("fsm", "state", "transition")):
        return "state_behavior"
    if any(word in text for word in ("valid", "ready", "handshake", "bus", "transaction", "request", "response")):
        return "protocol"
    if any(
        word in text
        for word in (
            "compute",
            "computes",
            "implement",
            "implements",
            "produce",
            "produces",
            "choose",
            "clear",
            "count",
            "add",
            "xor",
        )
    ):
        return "functional_behavior"
    if re.search(r"\balways\s+outputs?\b", text) or re.search(r"\bbuild\s+a\s+circuit\s+that\b", text):
        return "functional_behavior"
    if re.search(r"\b(and|or|not|xor)\s+gate\b", text):
        return "functional_behavior"
    if any(word in text for word in ("input", "output", "port", "interface")):
        return "interface"
    if any(word in text for word in ("compare", "scoreboard", "expected")):
        return "compare_policy"
    if any(word in text for word in ("must", "shall", "should", "only", "range", "illegal", "constraint")):
        return "constraint"
    return "descriptive"


def classify_claim_strength(summary: str) -> str:
    text = summary.lower()
    if "shall" in text:
        return "shall"
    if "must" in text or "always" in text:
        return "must"
    if "should" in text:
        return "should"
    if any(word in text for word in ("compute", "computes", "implement", "implements", "produce", "produces")):
        return "must"
    return "describes"


def infer_claim_subjects(summary: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", summary)
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "be",
        "before",
        "block",
        "by",
        "if",
        "in",
        "input",
        "is",
        "module",
        "must",
        "of",
        "or",
        "output",
        "over",
        "should",
        "signal",
        "the",
        "to",
        "when",
        "with",
    }
    subjects = []
    for word in words:
        lowered = word.lower()
        if lowered in stop_words:
            continue
        if lowered not in subjects:
            subjects.append(lowered)
    return subjects[:8]


def evidence_from_spec_claims(spec_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for claim in spec_claims:
        claim_id = claim.get("id")
        if not isinstance(claim_id, str):
            continue
        evidence.append(
            {
                "id": f"ev{len(evidence) + 1}",
                "source_id": claim.get("source_id"),
                "line_start": claim.get("line_start"),
                "line_end": claim.get("line_end"),
                "quote": claim.get("quote"),
                "claim_ids": [claim_id],
            }
        )
    return evidence


def semantic_elements_from_claims(
    spec_claims: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    manifest_fields: Iterable[str] = (),
) -> list[dict[str, Any]]:
    evidence_by_claim = {
        str(claim_id): str(item["id"])
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        for claim_id in item.get("claim_ids", [])
        if isinstance(claim_id, str)
    }
    elements: list[dict[str, Any]] = []
    field_names = set(manifest_fields)
    for claim in spec_claims:
        claim_id = claim.get("id")
        if not isinstance(claim_id, str):
            continue
        summary = str(claim.get("summary") or claim.get("quote") or "").strip()
        if not summary:
            continue
        kind = semantic_element_kind_for_claim(claim)
        representation = semantic_representation_for_claim(claim, kind, field_names)
        requires_human_review = representation_requires_human_review(representation)
        element = {
            "id": f"sem{len(elements) + 1}",
            "kind": kind,
            "summary": summary,
            "formalization_status": "needs_human_review" if requires_human_review else "candidate",
            "confidence": 0.35 if requires_human_review else 0.55,
            "subjects": claim.get("subjects", []),
            "representation": representation,
            "evidence": [evidence_by_claim[claim_id]] if claim_id in evidence_by_claim else [],
            "claim_ids": [claim_id],
            "provenance": {
                "source": "rule_based",
                "extractor": "claim_structuring_semantic_extractor",
            },
        }
        elements.append(element)
    return elements


def semantic_element_kind_for_claim(claim: dict[str, Any]) -> str:
    kind = str(claim.get("kind") or "descriptive")
    summary = str(claim.get("summary") or claim.get("quote") or "").lower()
    if kind == "timing":
        return "temporal_behavior"
    if kind == "state_behavior":
        if "fsm" in summary or "state machine" in summary or "--" in summary:
            return "state_machine"
        return "sequential_behavior"
    if kind == "functional_behavior":
        if any(word in summary for word in ("clock", "posedge", "negedge", "flip-flop", "flip flop")):
            return "sequential_behavior"
        return "combinational_behavior"
    if kind in ALLOWED_SEMANTIC_ELEMENT_KINDS:
        return kind
    return "descriptive"


def build_open_questions(
    manifest: Any,
    semantic_elements: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    spec_claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    if not spec_claims:
        questions.append(
            {
                "id": f"q{len(questions) + 1}",
                "blocking": True,
                "status": "open",
                "question": "No source claims were extracted from the provided spec text.",
                "related_items": [],
                "claim_ids": [],
                "suggested_answers": ["provide_behavior_spec", "author_spec_claims"],
            }
        )
    _ = (manifest, semantic_elements, evidence)
    return questions


def semantic_gaps_for_uncovered_claims(
    spec_claims: list[dict[str, Any]],
    semantic_elements: list[dict[str, Any]],
    open_questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    covered = covered_claim_ids_from_elements(semantic_elements)
    covered.update(covered_claim_ids_from_questions(open_questions))
    gaps = []
    for claim in spec_claims:
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or claim_id in covered:
            continue
        gaps.append(
            {
                "id": f"gap{len(gaps) + 1}",
                "kind": "unformalized",
                "reason": f"No SemanticSpecIR element was formalized for spec claim {claim_id}.",
                "resolution": "LLM extraction, human semantic completion, or explicit semantic waiver",
                "claim_ids": [claim_id],
            }
        )
    return gaps


def gap_requires_human_input(gap: dict[str, Any]) -> bool:
    return gap.get("kind") in {"ambiguous", "conflict", "incomplete", "missing_context", "unformalized"}


def covered_claim_ids_from_elements(items: list[dict[str, Any]]) -> set[str]:
    covered: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for claim_id in item.get("claim_ids", []):
            if isinstance(claim_id, str):
                covered.add(claim_id)
    return covered


def covered_claim_ids_from_questions(questions: list[dict[str, Any]]) -> set[str]:
    covered: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            continue
        for claim_id in question.get("claim_ids", []):
            if isinstance(claim_id, str):
                covered.add(claim_id)
    return covered
