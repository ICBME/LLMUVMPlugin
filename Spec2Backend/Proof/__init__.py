"""Interactive proof backends for Spec2Backend checks."""

from __future__ import annotations

from typing import Any, Mapping

from Spec2Backend.Checks.model import issue

from .lean4 import Lean4ProofBackend, discover_lean, run_lean_source
from .model import ProofBackend, ProofResult


def run_proof_backend(
    context: Any,
    *,
    proof_backend: str | None,
    proof_options: Mapping[str, Any] | None = None,
) -> ProofResult:
    options = dict(proof_options or {})
    if proof_backend is None:
        return ProofResult(
            "failed",
            issues=(issue("proof", "$", "proof pass requires proof_backend", code="proof_backend_missing"),),
            metadata={"backend": None},
        )
    backend_name = str(proof_backend).lower()
    if backend_name in {"lean", "lean4"}:
        backend = Lean4ProofBackend(
            lean_bin=options.get("lean_bin"),
            timeout_s=float(options.get("timeout_s", 10.0)),
            allowed_axioms=tuple(options.get("allowed_axioms", ("Classical.choice", "Quot.sound", "propext"))),
        )
        return backend.prove_context(context)
    if backend_name == "coq":
        return ProofResult(
            "failed",
            issues=(issue("proof", "$", "Coq proof backend is registered but not implemented in v1", code="coq_unimplemented"),),
            metadata={"backend": "coq", "status": "unimplemented"},
        )
    return ProofResult(
        "failed",
        issues=(issue("proof", "$", f"unsupported proof backend {proof_backend!r}", code="proof_backend_unsupported"),),
        metadata={"backend": proof_backend},
    )


__all__ = [
    "Lean4ProofBackend",
    "ProofBackend",
    "ProofResult",
    "discover_lean",
    "run_lean_source",
    "run_proof_backend",
]
