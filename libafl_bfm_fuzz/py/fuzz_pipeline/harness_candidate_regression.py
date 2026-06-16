"""Compatibility facade for legacy candidate regression imports.

New code should prefer ``fuzz_pipeline.harness_evidence.candidate_regression``.
"""

from .harness_evidence.candidate_regression import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]
