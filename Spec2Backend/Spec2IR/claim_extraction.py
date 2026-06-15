"""Conservative source-claim extraction for SemanticSpecIR drafts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from .representation_ast import (
    representation_requires_human_review,
    semantic_representation_for_claim,
)
from .schema import ALLOWED_SEMANTIC_ELEMENT_KINDS, SourceDocument, sha256_text


CLAIM_DECOMPOSITION_VERSION = 1


@dataclass(frozen=True)
class ClaimCandidate:
    source_id: str
    line_start: int
    line_end: int
    quote: str
    summary: str
    kind_hint: str | None = None
    subjects_hint: list[str] | None = None
    provenance: str = "paragraph"


def extract_spec_claims(documents: tuple[SourceDocument, ...]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for document in documents:
        for candidate in iter_claim_candidates(document):
            claim_id = f"claim{len(claims) + 1}"
            claims.append(claim_from_candidate(candidate, claim_id))
    return claims


def iter_claim_candidates(document: SourceDocument) -> list[ClaimCandidate]:
    candidates: list[ClaimCandidate] = []
    paragraph: list[tuple[int, str]] = []
    list_item: list[tuple[int, str]] = []
    table: list[tuple[int, str]] = []
    in_code_block = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        candidates.extend(claim_candidates_from_text_block(document.id, paragraph, "paragraph"))
        paragraph = []

    def flush_list_item() -> None:
        nonlocal list_item
        if not list_item:
            return
        candidates.extend(claim_candidates_from_text_block(document.id, list_item, "list_item"))
        list_item = []

    def flush_table() -> None:
        nonlocal table
        if not table:
            return
        table_candidates = claim_candidates_from_table(document.id, table)
        if table_candidates:
            candidates.extend(table_candidates)
        else:
            candidates.extend(claim_candidates_from_text_block(document.id, table, "paragraph"))
        table = []

    for line_no, raw_line in enumerate(document.lines, start=1):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            flush_list_item()
            flush_table()
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if not stripped:
            flush_paragraph()
            flush_list_item()
            flush_table()
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            flush_list_item()
            flush_table()
            continue
        if is_table_line(stripped):
            flush_paragraph()
            flush_list_item()
            table.append((line_no, raw_line))
            continue
        flush_table()
        if is_markdown_list_item(stripped):
            flush_paragraph()
            flush_list_item()
            list_item = [(line_no, raw_line)]
            continue
        if list_item and is_indented_continuation(raw_line):
            list_item.append((line_no, raw_line))
            continue
        flush_list_item()
        paragraph.append((line_no, raw_line))

    flush_paragraph()
    flush_list_item()
    flush_table()
    return candidates


def claim_candidates_from_text_block(
    source_id: str,
    lines: list[tuple[int, str]],
    provenance: str,
) -> list[ClaimCandidate]:
    quote = "\n".join(line for _line_no, line in lines).strip()
    text = summarize_claim_quote(quote)
    if not text:
        return []
    candidates: list[ClaimCandidate] = []
    for summary in split_claim_summaries(text, keep_as_one=provenance == "list_item"):
        if not summary:
            continue
        claim_quote = quote_for_summary(summary, quote)
        candidates.append(
            ClaimCandidate(
                source_id=source_id,
                line_start=lines[0][0],
                line_end=lines[-1][0],
                quote=claim_quote[:480],
                summary=summary,
                provenance=provenance,
            )
        )
    return candidates


def claim_candidates_from_table(
    source_id: str,
    lines: list[tuple[int, str]],
) -> list[ClaimCandidate]:
    parsed_rows = [
        (line_no, raw_line, split_table_row(raw_line))
        for line_no, raw_line in lines
        if not is_markdown_table_rule(raw_line.strip())
    ]
    parsed_rows = [
        (line_no, raw_line, cells)
        for line_no, raw_line, cells in parsed_rows
        if len(cells) >= 2
    ]
    if not parsed_rows:
        return []

    line_by_number = {line_no: raw_line for line_no, raw_line in lines}
    header_line, _header_raw, header = parsed_rows[0]
    candidates: list[ClaimCandidate] = []
    for line_no, raw_line, cells in parsed_rows[1:]:
        if len(cells) != len(header):
            continue
        summary = summarize_table_row(header, cells)
        if not summary:
            continue
        subjects = [
            normalize_identifier(cell)
            for cell in header
            if normalize_identifier(cell)
        ]
        candidates.append(
            ClaimCandidate(
                source_id=source_id,
                line_start=header_line,
                line_end=line_no,
                quote=table_row_quote(
                    line_by_number,
                    header_line=header_line,
                    row_line=line_no,
                )[:480],
                summary=summary,
                kind_hint="functional_behavior",
                subjects_hint=subjects,
                provenance=f"table_row:{header_line}",
            )
        )
    return candidates


def claim_from_candidate(candidate: ClaimCandidate, claim_id: str) -> dict[str, Any]:
    kind = candidate.kind_hint or classify_claim_kind(candidate.summary)
    subjects = candidate.subjects_hint or infer_claim_subjects(candidate.summary)
    claim = {
        "id": claim_id,
        "source_id": candidate.source_id,
        "line_start": candidate.line_start,
        "line_end": candidate.line_end,
        "quote": candidate.quote,
        "summary": candidate.summary,
        "kind": kind,
        "strength": classify_claim_strength(candidate.summary),
        "normative": True,
        "subjects": subjects,
        "fingerprint": claim_fingerprint(candidate),
        "decomposition": decompose_claim(candidate.summary, kind, subjects, claim_id),
    }
    return claim


def claim_fingerprint(candidate: ClaimCandidate) -> str:
    normalized = "\n".join(
        [
            candidate.source_id,
            str(candidate.line_start),
            str(candidate.line_end),
            normalize_claim_text(candidate.summary),
            normalize_claim_text(candidate.quote),
        ]
    )
    return sha256_text(normalized)


def split_claim_quotes(line: str) -> list[str]:
    normalized = line.strip()
    if not normalized:
        return []
    if re.match(r"^[-*]\s+", normalized):
        return [normalized]
    parts = [part.strip() for part in re.split(r"(?<=[.;])\s+", normalized) if part.strip()]
    return parts or [normalized]


def split_claim_summaries(text: str, *, keep_as_one: bool = False) -> list[str]:
    if keep_as_one:
        return [text]
    return [
        summarize_claim_quote(part)
        for part in split_claim_quotes(text)
        if summarize_claim_quote(part)
    ]


def summarize_claim_quote(quote: str) -> str:
    text = quote.strip()
    text = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", text)
    text = re.sub(r"\n\s+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ;")


def quote_for_summary(summary: str, block_quote: str) -> str:
    if summary in block_quote:
        return summary
    stripped_summary = summary.rstrip(".;:")
    if stripped_summary and stripped_summary in block_quote:
        return stripped_summary
    pattern = r"\s+".join(re.escape(part) for part in summary.split())
    match = re.search(pattern, block_quote, re.IGNORECASE)
    if match:
        return match.group(0)
    return block_quote


def is_markdown_table_rule(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?", line))


def is_table_line(line: str) -> bool:
    return "|" in line


def is_markdown_list_item(line: str) -> bool:
    return bool(re.match(r"^\s*(?:[-*]|\d+[.)])\s+", line))


def is_indented_continuation(raw_line: str) -> bool:
    return bool(raw_line[:1].isspace()) and bool(raw_line.strip())


def split_table_row(raw_line: str) -> list[str]:
    stripped = raw_line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def summarize_table_row(header: list[str], cells: list[str]) -> str:
    pairs = [
        (normalize_identifier(name), value.strip())
        for name, value in zip(header, cells, strict=False)
        if normalize_identifier(name) and value.strip()
    ]
    if len(pairs) < 2:
        return ""
    conditions = ", ".join(f"{name}={value}" for name, value in pairs[:-1])
    output_name, output_value = pairs[-1]
    return f"Truth table row: when {conditions}, {output_name}={output_value}"


def table_row_quote(
    line_by_number: dict[int, str],
    *,
    header_line: int,
    row_line: int,
) -> str:
    return "\n".join(
        line_by_number[line_no]
        for line_no in range(header_line, row_line + 1)
        if line_no in line_by_number
    ).strip()


def normalize_identifier(text: str) -> str:
    match = re.search(r"[A-Za-z_][A-Za-z0-9_]*", text)
    return match.group(0) if match else ""


def normalize_claim_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def classify_claim_kind(summary: str) -> str:
    text = summary.lower()
    if text.startswith("truth table row:"):
        return "functional_behavior"
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
    if text.startswith("truth table row:"):
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


def decompose_claim(
    summary: str,
    kind: str,
    subjects: list[str],
    claim_id: str,
) -> dict[str, Any]:
    obligations = infer_atomic_obligations(summary, kind, subjects)
    return {
        "version": CLAIM_DECOMPOSITION_VERSION,
        "atomic_obligations": [
            {
                "id": f"{claim_id}.obl{index}",
                **obligation,
            }
            for index, obligation in enumerate(obligations, start=1)
        ],
    }


def infer_atomic_obligations(
    summary: str,
    kind: str,
    subjects: list[str],
) -> list[dict[str, Any]]:
    obligations: list[dict[str, Any]] = []
    add_interface_obligation(summary, subjects, obligations)
    add_when_assignment_obligations(summary, subjects, obligations)
    add_latency_obligations(summary, subjects, obligations)
    add_clock_obligation(summary, subjects, obligations)
    add_reset_obligations(summary, subjects, obligations)
    add_protocol_obligation(summary, subjects, obligations)
    add_truth_table_obligations(summary, subjects, obligations)
    add_operation_obligation(summary, subjects, obligations)
    add_state_transition_obligation(summary, subjects, obligations)
    if not obligations:
        obligations.append(
            atomic_obligation(
                kind=claim_obligation_kind_for_claim(kind),
                text=summary,
                subjects=subjects,
            )
        )
    return dedupe_atomic_obligations(obligations)


def atomic_obligation(
    *,
    kind: str,
    text: str,
    subjects: list[str],
    required: bool = True,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": kind,
        "text": text,
        "subjects": [subject for subject in subjects if isinstance(subject, str)],
        "required": required,
    }
    if attributes:
        payload["attributes"] = attributes
    return payload


def add_interface_obligation(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    match = re.match(
        r"^(?:input|output|inout)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\s+\((?P<width>\d+)\s+bits?\))?",
        summary.strip(),
        re.IGNORECASE,
    )
    if not match:
        return
    obligations.append(
        atomic_obligation(
            kind="interface_port",
            text=summary,
            subjects=[match.group("name")],
            attributes={
                "direction": summary.strip().split()[0].lower(),
                "name": match.group("name"),
                "width": int(match.group("width")) if match.group("width") else 1,
            },
        )
    )


def add_when_assignment_obligations(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    match = re.match(
        r"^when\s+(?P<condition>[^,.;]+)\s*,\s*(?P<effect>[^.;]+)",
        summary.strip(),
        re.IGNORECASE,
    )
    if not match:
        return
    obligations.append(
        atomic_obligation(
            kind="condition",
            text=match.group("condition").strip(),
            subjects=infer_claim_subjects(match.group("condition")),
        )
    )
    obligations.append(
        atomic_obligation(
            kind="response",
            text=match.group("effect").strip(),
            subjects=infer_claim_subjects(match.group("effect")) or subjects,
        )
    )


def add_latency_obligations(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    match = re.search(
        r"(?P<response>[A-Za-z_][A-Za-z0-9_]*)"
        r"(?:\s+signal)?\s+must\s+pulse\s+exactly\s+"
        r"(?P<count>\d+|one|two|three|four|five)\s+cycles?\s+after\s+"
        r"(?P<trigger>[^.;]+)",
        summary,
        re.IGNORECASE,
    )
    if not match:
        return
    obligations.append(
        atomic_obligation(
            kind="trigger",
            text=match.group("trigger").strip(),
            subjects=infer_claim_subjects(match.group("trigger")),
        )
    )
    obligations.append(
        atomic_obligation(
            kind="response",
            text=f"{match.group('response')} pulses",
            subjects=[match.group("response")],
        )
    )
    obligations.append(
        atomic_obligation(
            kind="timing",
            text=f"exactly {match.group('count')} cycle(s)",
            subjects=subjects,
            attributes={"delay_cycles": match.group("count").lower()},
        )
    )


def add_clock_obligation(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    edge_match = re.search(r"\b(posedge|negedge|positive edge|negative edge)\b", summary, re.IGNORECASE)
    clock_match = re.search(r"\b(clock|clk)\b", summary, re.IGNORECASE)
    if not edge_match and not clock_match:
        return
    obligations.append(
        atomic_obligation(
            kind="clock",
            text=summary,
            subjects=[subject for subject in subjects if subject in {"clk", "clock"}] or subjects,
            attributes={"edge": normalize_clock_edge(edge_match.group(1)) if edge_match else "unspecified"},
        )
    )


def normalize_clock_edge(text: str) -> str:
    lowered = text.lower()
    if lowered in {"posedge", "positive edge"}:
        return "posedge"
    if lowered in {"negedge", "negative edge"}:
        return "negedge"
    return lowered


def add_reset_obligations(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    if not re.search(r"\b(reset|rst)\b", summary, re.IGNORECASE):
        return
    obligations.append(
        atomic_obligation(
            kind="reset",
            text=summary,
            subjects=subjects,
        )
    )


def add_protocol_obligation(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    if "valid" not in summary.lower() or "ready" not in summary.lower():
        return
    obligations.append(
        atomic_obligation(
            kind="protocol",
            text=summary,
            subjects=subjects,
            attributes={"protocol": "valid_ready_handshake"},
        )
    )


def add_truth_table_obligations(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    if not summary.lower().startswith("truth table row:"):
        return
    obligations.append(
        atomic_obligation(
            kind="truth_table_row",
            text=summary,
            subjects=subjects,
        )
    )


def add_operation_obligation(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    text = summary.lower()
    operation = ""
    if "not gate" in text:
        operation = "not_gate"
    elif "xor" in text:
        operation = "xor"
    elif "and gate" in text:
        operation = "and_gate"
    elif "or gate" in text:
        operation = "or_gate"
    elif re.search(r"\bsha-?(224|256|384|512)\b", text):
        operation = "sha"
    elif any(word in text for word in ("compute", "computes", "implement", "implements", "produce", "produces")):
        operation = "functional_operation"
    if not operation:
        return
    obligations.append(
        atomic_obligation(
            kind="operation",
            text=summary,
            subjects=subjects,
            attributes={"operation": operation},
        )
    )


def add_state_transition_obligation(
    summary: str,
    subjects: list[str],
    obligations: list[dict[str, Any]],
) -> None:
    if "--" not in summary and "transition" not in summary.lower():
        return
    obligations.append(
        atomic_obligation(
            kind="state_transition",
            text=summary,
            subjects=subjects,
        )
    )


def claim_obligation_kind_for_claim(kind: str) -> str:
    return {
        "interface": "interface_port",
        "protocol": "protocol",
        "reset": "reset",
        "state_behavior": "state_transition",
        "timing": "timing",
        "constraint": "constraint",
        "functional_behavior": "behavior",
    }.get(kind, "behavior")


def dedupe_atomic_obligations(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for obligation in obligations:
        key = (str(obligation.get("kind")), normalize_claim_text(str(obligation.get("text") or "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(obligation)
    return result


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
