"""Compatibility facade for legacy campaign rollup imports.

New code should prefer ``fuzz_pipeline.harness_evidence.rollup``.
"""

from .harness_evidence.rollup import *  # noqa: F401,F403

__all__ = [
    name
    for name in globals()
    if not name.startswith("__") and name != "annotations"
]
