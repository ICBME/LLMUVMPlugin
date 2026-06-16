"""Compatibility facade for legacy harness optimization imports.

New code should prefer ``fuzz_pipeline.harness_evidence.optimization``.
"""

from .harness_evidence.optimization import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]
