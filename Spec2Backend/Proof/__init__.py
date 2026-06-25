"""Interactive proof backends for Spec2Backend checks."""

from __future__ import annotations

from typing import Any, Mapping

from Spec2Backend.Checks.model import issue

from .lean4 import Lean4ProofBackend, discover_lean, run_lean_source
from .model import ProofBackend, ProofObligation, ProofPlan, ProofResult
from .plan import build_proof_plan


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
        try:
            timeout_s = float(options.get("timeout_s", 10.0))
            max_subgoals = int(options.get("max_subgoals", 64))
        except Exception as exc:  # noqa: BLE001 - option errors must be reported, not raised
            return ProofResult(
                "failed",
                issues=(issue("proof", "$", f"invalid proof option: {exc}", code="invalid_proof_option"),),
                metadata={"backend": "lean4"},
            )
        backend = Lean4ProofBackend(
            lean_bin=options.get("lean_bin"),
            timeout_s=timeout_s,
            allowed_axioms=tuple(options.get("allowed_axioms", ("Classical.choice", "Quot.sound", "propext"))),
            proof_scope=options.get("proof_scope"),
            max_subgoals=max_subgoals,
            semantic_ir=options.get("semantic_ir"),
            ref_model_plan=options.get("ref_model_plan"),
            wrapper_source=options.get("wrapper_source"),
            wrapper_path=options.get("wrapper_path"),
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
    "ProofObligation",
    "ProofPlan",
    "ProofResult",
    "build_proof_plan",
    "discover_lean",
    "run_lean_source",
    "run_proof_backend",
]
