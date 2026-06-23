"""Shared proof backend models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from Spec2Backend.Checks.model import CheckIssue


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

