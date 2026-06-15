"""Shared schema constants and small helpers for SemanticSpecIR."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable


SEMANTIC_SPEC_IR_SCHEMA_VERSION = 5

TRACEABLE_SOURCE_KIND = "natural_language_spec"
ALLOWED_REVIEW_STATUSES = {
    "draft",
    "needs_human_input",
    "accepted",
    "rejected",
}
ALLOWED_CLAIM_KINDS = {
    "compare_policy",
    "constraint",
    "descriptive",
    "functional_behavior",
    "interface",
    "protocol",
    "reset",
    "state_behavior",
    "timing",
}
ALLOWED_CLAIM_STRENGTHS = {
    "describes",
    "must",
    "shall",
    "should",
    "unknown",
}
ALLOWED_CLAIM_OBLIGATION_KINDS = {
    "assumption",
    "behavior",
    "clock",
    "condition",
    "constraint",
    "interface_port",
    "operation",
    "protocol",
    "reset",
    "response",
    "state_transition",
    "timing",
    "trigger",
    "truth_table_row",
}
ALLOWED_FORMALIZATION_STATUSES = {
    "candidate",
    "formalized",
    "ambiguous",
    "incomplete",
    "conflict",
    "needs_human_review",
}
BLOCKING_FORMALIZATION_STATUSES = {
    "ambiguous",
    "incomplete",
    "conflict",
    "needs_human_review",
}
ALLOWED_SEMANTIC_ELEMENT_KINDS = {
    "compare_policy",
    "combinational_behavior",
    "constraint",
    "descriptive",
    "example",
    "functional_behavior",
    "interface",
    "protocol",
    "reset",
    "sequential_behavior",
    "state_behavior",
    "state_machine",
    "temporal_behavior",
    "timing",
}
ALLOWED_SEMANTIC_GAP_KINDS = {
    "ambiguous",
    "conflict",
    "incomplete",
    "missing_context",
    "unformalized",
}


SemanticSpecIRCallable = Callable[[dict[str, Any], str | None], dict[str, Any] | None]


class SemanticSpecIRValidationError(ValueError):
    """Raised when a SemanticSpecIR document fails validation."""


@dataclass(frozen=True)
class SourceDocument:
    id: str
    path: Path
    text: str

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "kind": TRACEABLE_SOURCE_KIND,
            "content_hash": sha256_text(self.text),
            "line_count": len(self.lines),
        }


@dataclass(frozen=True)
class SemanticSpecIRIssue:
    path: str
    message: str

    def format(self) -> str:
        return f"{self.path}: {self.message}"


def load_source_documents(paths: Iterable[str | Path]) -> tuple[SourceDocument, ...]:
    documents = []
    for index, raw_path in enumerate(paths, start=1):
        path = Path(raw_path)
        documents.append(
            SourceDocument(
                id=f"src{index}",
                path=path,
                text=path.read_text(encoding="utf-8", errors="replace"),
            )
        )
    return tuple(documents)


def load_source_lines_by_id(spec_paths: Iterable[str | Path]) -> dict[str, list[str]]:
    result = {}
    for index, raw_path in enumerate(spec_paths, start=1):
        path = Path(raw_path)
        if path.exists():
            result[f"src{index}"] = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return result


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def looks_like_semantic_spec_ir(value: dict[str, Any]) -> bool:
    return {
        "schema_version",
        "target",
        "sources",
        "semantic_elements",
        "evidence",
        "review",
    }.issubset(value)


def json_round_trip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(deepcopy(value), sort_keys=True))


def extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("{"):
        return text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain JSON")
    return match.group(0)
