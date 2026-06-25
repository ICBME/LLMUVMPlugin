"""Shared proof backend models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from Spec2Backend.Checks.model import CheckIssue


@dataclass(frozen=True)
class ProofObligation:
    obligation_id: str
    kind: str
    path: str
    left: Any | None = None
    right: Any | None = None
    condition: Any | None = None
    expr: Any | None = None
    rule_id: str | None = None
    trusted_extern_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProofPlan:
    obligations: tuple[ProofObligation, ...]
    scope: tuple[str, ...] = ("expr",)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProofResult:
    status: str
    proved_rules: tuple[str, ...] = ()
    issues: tuple[CheckIssue, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "passed" and not any(issue.blocking for issue in self.issues)


class ProofBackend(Protocol):
    name: str

    def prove_context(self, context: Any) -> ProofResult:
        """Prove obligations extracted from a generic check context."""
